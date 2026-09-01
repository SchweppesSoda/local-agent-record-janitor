from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.adapters import NativeIntegrityAdapter
from local_agent_record_janitor.cleanup_service import partition_actions
from local_agent_record_janitor.execution import (
    ExecutionError,
    execute_prevalidated_actions,
)
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.inventory import build_session_catalog
from local_agent_record_janitor.manual_delete import build_manual_delete_plan
from local_agent_record_janitor.models import Finding, RolloutRecord
from local_agent_record_janitor.operation_store import (
    OperationStore,
    plan_sha256,
    write_new_json,
)
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.planning import build_cleanup_plan

from tests.support import create_thread_index, write_rollout


class CoreBatchFastTests(unittest.TestCase):
    @staticmethod
    def _native_fixture(home: Path, count: int) -> tuple[tuple[str, ...], dict[str, tuple[Path, ...]]]:
        home.mkdir(parents=True, exist_ok=True)
        ids = tuple(f"native-{index:03d}" for index in range(count))
        paths: dict[str, tuple[Path, ...]] = {}
        rows: list[dict[str, object]] = []
        for thread_id in ids:
            path = write_rollout(home, thread_id, originator="codex_cli_rs")
            paths[thread_id] = (path,)
            rows.append({"id": thread_id, "rollout_path": str(path)})
        create_thread_index(home, rows)
        return ids, paths

    def test_healthy_native_run_uses_two_catalog_passes_for_all_batch_sizes(self) -> None:
        """A real native fixture gets one plan and one terminal catalog pass."""

        import local_agent_record_janitor.inventory as inventory_module
        import local_agent_record_janitor.adapters.native as native_module

        for count in (1, 10, 100):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                home = root / "codex-home"
                thread_ids, rollout_paths = self._native_fixture(home, count)
                adapter = NativeIntegrityAdapter(codex_home=home)
                server_calls: list[str] = []
                server_instances: list[object] = []

                class DeletingServer:
                    def __enter__(self) -> "DeletingServer":
                        return self

                    def __exit__(self, *_exc_info: object) -> None:
                        return None

                    def delete_thread(self, thread_id: str) -> None:
                        server_calls.append(thread_id)
                        with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                            connection.execute(
                                "DELETE FROM threads WHERE id = ?",
                                (thread_id,),
                            )
                            connection.commit()
                        for path in rollout_paths.get(thread_id, ()):
                            path.unlink(missing_ok=True)

                def app_server_factory(**_kwargs: object) -> DeletingServer:
                    server = DeletingServer()
                    server_instances.append(server)
                    return server

                service = CleanupService()
                coordinator = OperationCoordinator(service)
                plan_path = root / "operation-plan.json"
                with patch.object(
                    inventory_module,
                    "build_session_catalog",
                    wraps=inventory_module.build_session_catalog,
                ) as catalog_builder, patch.object(
                    adapter,
                    "scan",
                    wraps=adapter.scan,
                ) as anomaly_scan, patch.object(
                    native_module,
                    "_scan_all_rollouts",
                    wraps=native_module._scan_all_rollouts,
                ) as anomaly_rollout_scan, patch.object(
                    inventory_module.os,
                    "walk",
                    wraps=inventory_module.os.walk,
                ) as inventory_rollout_walk:
                    result = coordinator.run_operation(
                        client="native",
                        record_ids=thread_ids,
                        adapters=(adapter,),
                        plan_path=plan_path,
                        clients_closed=True,
                        timeout=5,
                        app_server_factory=app_server_factory,
                        binary_resolver=lambda _hint: Path("codex"),
                    )

                self.assertEqual(result.get("goal_status"), "complete")
                self.assertTrue(result.get("modified"))
                self.assertEqual(catalog_builder.call_count, 2)
                self.assertEqual(anomaly_scan.call_count, 0)
                self.assertEqual(anomaly_rollout_scan.call_count, 0)
                self.assertEqual(inventory_rollout_walk.call_count, 2)
                self.assertEqual(len(server_instances), 1)
                self.assertEqual(server_calls, list(thread_ids))
                self.assertEqual(
                    [item["status"] for item in result["batches"][0]["result"]["results"]],
                    ["deleted"] * count,
                )

    def test_anomaly_planning_indexes_default_rollouts_once_per_store(self) -> None:
        """The real default rollout reader is batched across all targets."""

        import local_agent_record_janitor.planning as planning_module

        for count in (1, 10, 100):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                home = root / "codex-home"
                thread_ids, rollout_paths = self._native_fixture(home, count)
                findings = [
                    Finding(
                        platform="native",
                        platform_session_id=f"native-{thread_id}",
                        thread_id=thread_id,
                        reason="duplicate_rollout",
                        platform_db=home / "state_5.sqlite",
                        codex_home=home,
                        rollout=RolloutRecord(
                            thread_id=thread_id,
                            path=rollout_paths[thread_id][0],
                            originator="codex_cli_rs",
                            source="app-server",
                            cwd=str(home.parent),
                            timestamp="2026-07-31T00:00:00Z",
                            archived=False,
                        ),
                        codex_indexed=True,
                        details={
                            "finding_type": "duplicate_rollout",
                            "thread_delete_supported": True,
                            "cleanable": True,
                        },
                    )
                    for thread_id in thread_ids
                ]
                with patch.object(
                    planning_module,
                    "iter_rollouts",
                    wraps=planning_module.iter_rollouts,
                ) as rollout_catalog_reader:
                    plan = build_cleanup_plan(findings)

                self.assertEqual(
                    sum(
                        getattr(action.kind, "value", action.kind)
                        == "delete_conversation"
                        for action in plan.actions
                    ),
                    count,
                )
                self.assertEqual(rollout_catalog_reader.call_count, 1)

    def test_corrupt_unselected_store_does_not_block_selected_native_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good_home = root / "good-codex-home"
            bad_home = root / "bad-codex-home"
            thread_ids, _rollout_paths = self._native_fixture(good_home, 1)
            bad_home.mkdir(parents=True)
            (bad_home / "state_5.sqlite").write_bytes(b"not sqlite")

            catalog = build_session_catalog(
                (
                    NativeIntegrityAdapter(codex_home=good_home),
                    NativeIntegrityAdapter(codex_home=bad_home),
                )
            )
            self.assertTrue(catalog.errors)
            manual_plan = build_manual_delete_plan(catalog)
            coordinator = OperationCoordinator(CleanupService())
            context, _adapters, _catalog, _plan, _manual_actions = (
                coordinator._native_manual_context(
                    (
                        NativeIntegrityAdapter(codex_home=good_home),
                        NativeIntegrityAdapter(codex_home=bad_home),
                    ),
                    catalog,
                    manual_plan,
                )
            )

            selected, blockers = coordinator._select_candidates(
                context,
                {
                    "client": "native",
                    "projects": (),
                    "all_projects": False,
                    "record_ids": thread_ids,
                    "engines": (),
                },
            )

            self.assertEqual(
                [str(action.target.thread_id) for action in selected],
                list(thread_ids),
            )
            self.assertFalse(any(item["code"] == "scan_incomplete" for item in blockers))

            planned = coordinator.plan_operation(
                client="native",
                record_ids=thread_ids,
                adapters=(
                    NativeIntegrityAdapter(codex_home=good_home),
                    NativeIntegrityAdapter(codex_home=bad_home),
                ),
                plan_path=root / "selected-good-plan.json",
            )
            self.assertEqual(planned.get("goal_status"), "ready")
            self.assertEqual(planned.get("counts", {}).get("action_count"), 1)
            self.assertEqual(planned.get("counts", {}).get("blocked_count"), 0)

    def test_operation_events_append_without_replaying_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            plan = {
                "schema_version": "larj.agent-plan.v1",
                "operation_id": "fast-journal",
                "target": {"codex_home": str(home)},
            }
            plan["plan_sha256"] = plan_sha256(plan)
            store = OperationStore(home, "fast-journal")
            store.accept_plan(plan)
            store.write_state(
                {
                    "schema_version": "larj.agent-state.v1",
                    "operation_id": "fast-journal",
                    "plan_sha256": plan["plan_sha256"],
                    "phase": "executing",
                    "goal_status": "unknown",
                    "goal_satisfied": False,
                    "modified": False,
                    "mutation_started": False,
                    "next_event_sequence": 1,
                }
            )

            with patch.object(
                store,
                "read_events",
                side_effect=AssertionError("append must not replay the journal"),
            ):
                for index in range(100):
                    store.append_event(
                        {
                            "event": (
                                "mutation_started"
                                if index == 0
                                else "action_verified"
                            ),
                            "action_id": f"action-{index:03d}",
                        },
                        state_updates={
                            "phase": "executing",
                            "mutation_started": True,
                            "modified": True,
                        },
                    )

            events = store.read_events()
            self.assertEqual(len(events), 100)
            self.assertEqual(
                [event["sequence"] for event in events],
                list(range(1, 101)),
            )
            self.assertEqual(
                store.read_state()["next_event_sequence"], 101  # type: ignore[index]
            )

    def test_partition_actions_keeps_storage_family_and_database_boundaries(self) -> None:
        def action(
            action_id: str,
            kind: str,
            storage_id: str,
            **impact: object,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                action_id=action_id,
                kind=kind,
                target=SimpleNamespace(storage_id=storage_id),
                impact=SimpleNamespace(**impact),
            )

        batches = partition_actions(
            (
                action("native-a", "delete_conversation", "store-a"),
                action("native-b", "delete_conversation", "store-a"),
                action(
                    "frontend-a",
                    "remove_frontend_reference",
                    "store-a",
                    frontend_database_paths=("frontend-a.sqlite",),
                ),
                action(
                    "frontend-b",
                    "remove_frontend_reference",
                    "store-a",
                    frontend_database_paths=("frontend-b.sqlite",),
                ),
                action("native-c", "delete_conversation", "store-b"),
            )
        )

        self.assertEqual(
            [
                (batch.storage_id, batch.mutation_family, batch.resource_key)
                for batch in batches
            ],
            [
                ("store-a", "delete_conversation", ()),
                ("store-b", "delete_conversation", ()),
                ("store-a", "remove_frontend_reference", ("frontend-a.sqlite",)),
                ("store-a", "remove_frontend_reference", ("frontend-b.sqlite",)),
            ],
        )
        self.assertEqual(
            [action.action_id for action in batches[0].actions],
            ["native-a", "native-b"],
        )

    def test_unknown_session_result_stops_remaining_requests(self) -> None:
        actions = tuple(
            SimpleNamespace(
                action_id=f"action-{index}",
                kind="delete_pi_session",
                target=SimpleNamespace(
                    storage_id="pi-store",
                    thread_id=f"session-{index}",
                ),
            )
            for index in range(2)
        )

        class NativePlan:
            plan_fingerprint = "native-plan"

            def with_selected_actions(self, selected: object) -> object:
                return self

        context = SimpleNamespace(
            plan=SimpleNamespace(),
            session_engine="pi",
            session_native_plan=NativePlan(),
            session_catalog_builder=lambda: None,
        )
        attempted: list[str] = []

        def session_executor(native_plan: object, **kwargs: object) -> object:
            callback = kwargs["action_state_callback"]
            for action in actions:
                attempted.append(action.action_id)
                callback("mutation_started", action, None)
                callback(
                    "verified",
                    action,
                    SimpleNamespace(status="unknown"),
                )
            return SimpleNamespace(not_deleted=(), unknown=())

        with self.assertRaises(ExecutionError) as raised:
            execute_prevalidated_actions(
                context,
                actions,
                timeout=1,
                app_server_factory=lambda **_kwargs: None,
                binary_resolver=lambda _hint: None,
                client_inspector=lambda _home: (),
                session_executor=session_executor,
                action_state_callback=lambda _checkpoint, _action, _result: None,
            )

        self.assertEqual(raised.exception.kind, "mutation_outcome_unknown")
        self.assertEqual(attempted, ["action-0"])

    def test_coordinator_unknown_skips_terminal_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            action = SimpleNamespace(
                action_id="unknown-action",
                kind="delete_pi_session",
                available=True,
                target=SimpleNamespace(
                    storage_id="pi-store",
                    thread_id="unknown-session",
                ),
                impact=SimpleNamespace(
                    external_engine="pi",
                    external_storage_root=str(home),
                    external_action_payload={},
                ),
            )
            context = SimpleNamespace(
                actions=(action,),
                plan=SimpleNamespace(
                    actions=(action,),
                    storages=(SimpleNamespace(storage_id="pi-store", path=home),),
                ),
            )

            class FakeStore:
                def __init__(self) -> None:
                    self.state = {"next_event_sequence": 1}

                def read_state(self) -> dict[str, object]:
                    return dict(self.state)

                def append_event(
                    self,
                    _event: object,
                    *,
                    state_updates: object = None,
                ) -> None:
                    if isinstance(state_updates, dict):
                        self.state.update(state_updates)
                    self.state["next_event_sequence"] = int(
                        self.state["next_event_sequence"]
                    ) + 1

                @contextmanager
                def mutation_lock(self):
                    yield

            class FakeService:
                def execute(self, _context: object, selected: object, **kwargs: object):
                    callback = kwargs["action_state_callback"]
                    current = tuple(selected)[0]
                    callback("mutation_started", current, None)
                    callback("verified", current, SimpleNamespace(status="unknown"))
                    return SimpleNamespace(
                        modified=True,
                        session_cleanup=SimpleNamespace(
                            results=(SimpleNamespace(status="unknown"),)
                        ),
                    )

            coordinator = OperationCoordinator(FakeService())
            live = SimpleNamespace(
                operation_id="unknown-operation",
                document={
                    "operation_id": "unknown-operation",
                    "plan_sha256": "a" * 64,
                    "scope": {},
                },
                context=context,
                candidates=(action,),
                client="pi",
                adapters=(),
                result=None,
            )
            terminal_calls: list[str] = []

            def terminal(_live: object):
                terminal_calls.append("scan")
                return None, None

            with patch.object(
                coordinator,
                "_open_batch_store",
                return_value=(FakeStore(), {"next_event_sequence": 1}),
            ), patch.object(coordinator, "_terminal_context", side_effect=terminal):
                result = coordinator._execute_live(
                    live,
                    timeout=1,
                    app_server_factory=lambda **_kwargs: None,
                    binary_resolver=lambda _hint: None,
                )

            self.assertEqual(result["goal_status"], "unknown")
            self.assertEqual(terminal_calls, [])

    def test_pre_mutation_guard_failure_is_blocked_after_prior_batch_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_home = root / "native"
            second_home = root / "frontend"
            first_home.mkdir()
            second_home.mkdir()

            def action(action_id: str, storage_id: str) -> SimpleNamespace:
                return SimpleNamespace(
                    action_id=action_id,
                    kind="delete_conversation",
                    available=True,
                    target=SimpleNamespace(
                        storage_id=storage_id,
                        thread_id=action_id,
                    ),
                    impact=SimpleNamespace(
                        external_storage_root=str(
                            first_home if storage_id == "native-store" else second_home
                        ),
                    ),
                )

            native = action("native", "native-store")
            next_batch = action("next", "frontend-store")
            context = SimpleNamespace(
                actions=(native, next_batch),
                plan=SimpleNamespace(
                    actions=(native, next_batch),
                    storages=(
                        SimpleNamespace(storage_id="native-store", path=first_home),
                        SimpleNamespace(storage_id="frontend-store", path=second_home),
                    ),
                ),
            )

            class FakeStore:
                def __init__(self) -> None:
                    self.state = {"next_event_sequence": 1}

                def read_state(self) -> dict[str, object]:
                    return dict(self.state)

                def append_event(
                    self,
                    _event: object,
                    *,
                    state_updates: object = None,
                ) -> None:
                    if isinstance(state_updates, dict):
                        self.state.update(state_updates)
                    self.state["next_event_sequence"] = int(
                        self.state["next_event_sequence"]
                    ) + 1

                @contextmanager
                def mutation_lock(self):
                    yield

            class FakeService:
                def __init__(self) -> None:
                    self.calls: list[str] = []

                def execute(self, _context: object, selected: object, **kwargs: object):
                    current = tuple(selected)[0]
                    self.calls.append(str(current.action_id))
                    callback = kwargs["action_state_callback"]
                    callback("guard_started", current, None)
                    if current.action_id == "native":
                        callback("mutation_started", current, None)
                        callback(
                            "verified",
                            current,
                            SimpleNamespace(status="deleted"),
                        )
                        return SimpleNamespace(
                            modified=True,
                            session_cleanup=SimpleNamespace(
                                results=(SimpleNamespace(status="deleted"),)
                            ),
                        )
                    raise RuntimeError("frontend guard rejected before mutation")

            service = FakeService()
            coordinator = OperationCoordinator(service)
            stores: list[FakeStore] = []

            def open_batch(*_args: object, **_kwargs: object):
                store = FakeStore()
                stores.append(store)
                return store, store.state

            live = SimpleNamespace(
                operation_id="guard-operation",
                document={
                    "operation_id": "guard-operation",
                    "plan_sha256": "a" * 64,
                    "scope": {},
                },
                context=context,
                candidates=(native, next_batch),
                client="native",
                adapters=(),
                result=None,
            )

            with (
                patch.object(coordinator, "_open_batch_store", side_effect=open_batch),
                patch.object(
                    coordinator,
                    "_terminal_context",
                    return_value=(SimpleNamespace(plan=SimpleNamespace(actions=())), None),
                ),
            ):
                result = coordinator._execute_live(
                    live,
                    timeout=1,
                    app_server_factory=lambda **_kwargs: None,
                    binary_resolver=lambda _hint: None,
                )

            self.assertEqual(service.calls, ["native", "next"])
            self.assertEqual(
                [batch["status"] for batch in result["batches"]],
                ["complete", "blocked"],
            )
            self.assertEqual(result["goal_status"], "blocked")
            self.assertTrue(result["modified"])
            self.assertEqual(len(stores), 2)

    def test_known_rollback_after_prior_native_success_is_blocked_not_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native_home = root / "native"
            frontend_home = root / "frontend"
            native_home.mkdir()
            frontend_home.mkdir()

            def action(action_id: str, storage_id: str, home: Path) -> SimpleNamespace:
                return SimpleNamespace(
                    action_id=action_id,
                    kind="delete_conversation",
                    available=True,
                    target=SimpleNamespace(
                        storage_id=storage_id,
                        thread_id=action_id,
                    ),
                    impact=SimpleNamespace(
                        external_storage_root=str(home),
                    ),
                )

            native = action("native", "native-store", native_home)
            frontend = action("frontend", "frontend-store", frontend_home)
            context = SimpleNamespace(
                actions=(native, frontend),
                plan=SimpleNamespace(
                    actions=(native, frontend),
                    storages=(
                        SimpleNamespace(storage_id="native-store", path=native_home),
                        SimpleNamespace(storage_id="frontend-store", path=frontend_home),
                    ),
                ),
            )

            class FakeStore:
                def __init__(self) -> None:
                    self.state = {"next_event_sequence": 1}

                def read_state(self) -> dict[str, object]:
                    return dict(self.state)

                def append_event(
                    self,
                    _event: object,
                    *,
                    state_updates: object = None,
                ) -> None:
                    if isinstance(state_updates, dict):
                        self.state.update(state_updates)
                    self.state["next_event_sequence"] = (
                        int(self.state["next_event_sequence"]) + 1
                    )

                @contextmanager
                def mutation_lock(self):
                    yield

            class KnownRollback(RuntimeError):
                outcome_known_rolled_back = True

            class FakeService:
                def execute(self, _context: object, selected: object, **kwargs: object):
                    current = tuple(selected)[0]
                    callback = kwargs["action_state_callback"]
                    callback("guard_started", current, None)
                    callback("mutation_started", current, None)
                    if current.action_id == "native":
                        callback(
                            "verified",
                            current,
                            SimpleNamespace(status="deleted"),
                        )
                        return SimpleNamespace(
                            modified=True,
                            results=(SimpleNamespace(status="deleted"),),
                        )
                    raise KnownRollback("frontend transaction rolled back")

            coordinator = OperationCoordinator(FakeService())
            stores: list[FakeStore] = []

            def open_batch(*_args: object, **_kwargs: object):
                store = FakeStore()
                stores.append(store)
                return store, store.state

            live = SimpleNamespace(
                operation_id="known-rollback-operation",
                document={
                    "operation_id": "known-rollback-operation",
                    "plan_sha256": "a" * 64,
                    "scope": {},
                },
                context=context,
                candidates=(native, frontend),
                client="native",
                adapters=(),
                result=None,
            )

            with (
                patch.object(coordinator, "_open_batch_store", side_effect=open_batch),
                patch.object(
                    coordinator,
                    "_terminal_context",
                    return_value=(SimpleNamespace(plan=SimpleNamespace(actions=())), None),
                ),
            ):
                result = coordinator._execute_live(
                    live,
                    timeout=1,
                    app_server_factory=lambda **_kwargs: None,
                    binary_resolver=lambda _hint: None,
                )

            self.assertEqual(
                [batch["status"] for batch in result["batches"]],
                ["complete", "blocked"],
            )
            self.assertEqual(result["goal_status"], "blocked")
            self.assertTrue(result["modified"])
            self.assertTrue(result["mutation_started"])
            self.assertFalse(
                any(
                    blocker["blocker_code"] == "mutation_outcome_unknown"
                    for blocker in result["blockers"]
                )
            )
            self.assertEqual(len(stores), 2)

    def test_all_projects_excludes_actions_without_project_evidence(self) -> None:
        def action(action_id: str, payload: dict[str, str]) -> SimpleNamespace:
            return SimpleNamespace(
                action_id=action_id,
                kind="delete_conversation",
                available=True,
                target=SimpleNamespace(
                    storage_id="native-store",
                    thread_id=action_id,
                ),
                impact=SimpleNamespace(
                    external_action_payload=payload,
                ),
            )

        projectless = action("projectless", {})
        attributed = action("attributed", {"cwd": "/repo/project"})
        context = SimpleNamespace(
            plan=SimpleNamespace(
                actions=(projectless, attributed),
                errors=(),
                conversations=(),
                observations=(),
            )
        )

        selected, blockers = OperationCoordinator(SimpleNamespace())._select_candidates(
            context,
            {
                "all_projects": True,
                "projects": (),
                "record_ids": (),
                "engines": (),
            },
        )

        self.assertEqual([item.action_id for item in selected], ["attributed"])
        self.assertEqual(blockers, [])

    def test_cross_process_unknown_child_never_reexecutes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            child_id = "unknown-operation-1"
            child_plan = {
                "schema_version": "larj.child-operation-plan.v1",
                "operation_id": child_id,
                "target": {
                    "codex_home": str(home),
                    "storage_id": "native-store",
                },
                "parent_operation_id": "unknown-operation",
                "mutation_family": "delete_conversation",
                "actions": [],
            }
            child_plan["plan_sha256"] = plan_sha256(child_plan)
            child_store = OperationStore(home, child_id)
            child_store.accept_plan(child_plan)
            child_store.write_state(
                {
                    "schema_version": "larj.agent-state.v1",
                    "operation_id": child_id,
                    "plan_sha256": child_plan["plan_sha256"],
                    "phase": "executing",
                    "goal_status": "unknown",
                    "goal_satisfied": False,
                    "modified": True,
                    "mutation_started": True,
                    "next_event_sequence": 1,
                }
            )
            child_store.append_event(
                {
                    "event": "mutation_started",
                    "action_id": "record-1",
                },
                state_updates={
                    "phase": "recovery_required",
                    "goal_status": "unknown",
                    "mutation_started": True,
                    "modified": True,
                },
            )

            document = {
                "schema_version": "larj.operation-plan.v1",
                "document_type": "operation_plan",
                "operation_id": "unknown-operation",
                "scope": {
                    "client": "native",
                    "projects": [],
                    "all_projects": False,
                    "record_ids": ["record-1"],
                    "engines": [],
                },
                "storages": [
                    {
                        "storage_id": "native-store",
                        "path": str(home),
                    }
                ],
                "actions": [],
                "child_batches": [
                    {
                        "child_operation_id": child_id,
                        "storage_id": "native-store",
                        "mutation_family": "delete_conversation",
                        "resource_key": [],
                        "action_ids": ["record-1"],
                        "action_count": 1,
                    }
                ],
            }
            document["plan_sha256"] = plan_sha256(document)
            plan_path = home / "operation-plan.json"
            write_new_json(plan_path, document)

            class FakeService:
                def __init__(self) -> None:
                    self.prepare_calls = 0
                    self.execute_calls = 0

                def prepare(self, *_args: object, **_kwargs: object) -> object:
                    self.prepare_calls += 1
                    raise AssertionError("unknown recovery must precede scanning")

                def execute(self, *_args: object, **_kwargs: object) -> object:
                    self.execute_calls += 1
                    raise AssertionError("unknown recovery must not execute")

            service = FakeService()
            coordinator = OperationCoordinator(service)
            result = coordinator.apply_operation(
                operation_id="unknown-operation",
                plan_path=plan_path,
                clients_closed=True,
            )

            self.assertEqual(result["goal_status"], "unknown")
            self.assertEqual(service.prepare_calls, 0)
            self.assertEqual(service.execute_calls, 0)
            self.assertTrue(
                any(
                    blocker["blocker_code"] == "recovery_required"
                    for blocker in result["blockers"]
                )
            )
            self.assertEqual(
                child_store.read_state()["phase"],  # type: ignore[index]
                "recovery_required",
            )

    def test_operation_id_only_status_uses_default_user_state_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            document = {
                "schema_version": "larj.operation-plan.v1",
                "document_type": "operation_plan",
                "operation_id": "status-operation",
                "scope": {},
                "storages": [],
                "actions": [],
                "child_batches": [],
            }
            document["plan_sha256"] = plan_sha256(document)
            plan_path = (
                state_root
                / "local-agent-record-janitor"
                / "plans"
                / "status-operation.json"
            )
            write_new_json(plan_path, document)

            with patch.dict(os.environ, {"LOCALAPPDATA": str(state_root)}):
                result = OperationCoordinator(SimpleNamespace()).status_operation(
                    operation_id="status-operation",
                )

            self.assertEqual(result["operation_id"], "status-operation")
            self.assertEqual(result["goal_status"], "ready")

    def _verify_plan_fixture(
        self,
        root: Path,
        *,
        storage_status: str = "ok",
        fresh_actions: tuple[object, ...] = (),
    ) -> tuple[OperationCoordinator, Path]:
        storage = root / "store"
        storage.mkdir(exist_ok=True)
        action_id = "old-action"
        document = {
            "schema_version": "larj.operation-plan.v1",
            "document_type": "operation_plan",
            "operation_id": "verify-operation",
            "scope": {"client": "native"},
            "storages": [{
                "storage_id": "store",
                "path": str(storage),
                "scan_status": storage_status,
            }],
            "actions": [{
                "action_id": action_id,
                "target": {"storage_id": "store", "thread_id": "thread-1"},
            }],
            "child_batches": [{
                "child_operation_id": "verify-operation-1",
                "storage_id": "store",
                "mutation_family": "delete_conversation",
                "resource_key": [str(storage)],
                "action_ids": [action_id],
            }],
        }
        document["plan_sha256"] = plan_sha256(document)
        plan_path = root / "operation-plan.json"
        write_new_json(plan_path, document)
        context = SimpleNamespace(
            plan=SimpleNamespace(
                actions=fresh_actions,
                scan_complete=True,
                errors=(),
            )
        )
        coordinator = OperationCoordinator(SimpleNamespace())
        coordinator._build_context = lambda *args, **kwargs: (
            context,
            (),
            None,
            None,
            {},
            {},
        )
        return coordinator, plan_path

    @staticmethod
    def _verify_action(action_id: str) -> object:
        return SimpleNamespace(
            action_id=action_id,
            kind="delete_conversation",
            target=SimpleNamespace(storage_id="store", thread_id="thread-1"),
            impact=SimpleNamespace(external_storage_root="PLACEHOLDER"),
        )

    def test_verify_action_disappears_is_complete_without_fresh_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordinator, plan_path = self._verify_plan_fixture(root)
            result = coordinator.verify_operation(
                operation_id="verify-operation",
                plan_path=plan_path,
            )
            self.assertEqual(result["goal_status"], "complete")
            self.assertEqual(result["residual_action_ids"], [])

    def test_verify_reconciles_unknown_child_and_status_reads_complete_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordinator, plan_path = self._verify_plan_fixture(root)
            child_id = "verify-operation-1"
            storage = root / "store"
            child_plan = {
                "schema_version": "larj.child-operation-plan.v1",
                "operation_id": child_id,
                "target": {
                    "codex_home": str(storage),
                    "storage_id": "store",
                },
                "parent_operation_id": "verify-operation",
                "mutation_family": "delete_conversation",
                "actions": [{
                    "action_id": "old-action",
                    "target": {
                        "storage_id": "store",
                        "thread_id": "thread-1",
                    },
                }],
            }
            child_plan["plan_sha256"] = plan_sha256(child_plan)
            child_store = OperationStore(storage, child_id)
            child_store.accept_plan(child_plan)
            child_store.write_state({
                "schema_version": "larj.agent-state.v1",
                "operation_id": child_id,
                "plan_sha256": child_plan["plan_sha256"],
                "phase": "executing",
                "goal_status": "unknown",
                "goal_satisfied": False,
                "modified": True,
                "mutation_started": True,
                "current_action_ids": ["old-action"],
                "current_action_state": "mutation_started",
                "next_event_sequence": 1,
            })
            child_store.append_event(
                {
                    "event": "mutation_started",
                    "action_id": "old-action",
                },
                state_updates={
                    "phase": "recovery_required",
                    "goal_status": "unknown",
                    "goal_satisfied": False,
                },
            )

            result = coordinator.verify_operation(
                operation_id="verify-operation",
                plan_path=plan_path,
            )

            self.assertEqual(result["goal_status"], "complete")
            self.assertIsNotNone(child_store.read_result())
            self.assertEqual(
                child_store.read_result()["goal_status"],  # type: ignore[index]
                "complete",
            )
            fresh_status = OperationCoordinator(SimpleNamespace()).status_operation(
                operation_id="verify-operation",
                plan_path=plan_path,
            )
            self.assertEqual(fresh_status["goal_status"], "complete")

    def test_verify_new_action_id_with_same_signature_is_residual(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = root / "store"
            storage.mkdir()
            fresh = self._verify_action("new-action")
            fresh.impact.external_storage_root = str(storage)  # type: ignore[attr-defined]
            coordinator, plan_path = self._verify_plan_fixture(
                root,
                fresh_actions=(fresh,),
            )
            result = coordinator.verify_operation(
                operation_id="verify-operation",
                plan_path=plan_path,
            )
            self.assertEqual(result["goal_status"], "completed_with_residuals")
            self.assertEqual(result["residual_action_ids"], ["old-action"])

    def test_verify_non_ok_frozen_store_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordinator, plan_path = self._verify_plan_fixture(
                root,
                storage_status="failed",
            )
            result = coordinator.verify_operation(
                operation_id="verify-operation",
                plan_path=plan_path,
            )
            self.assertEqual(result["goal_status"], "unknown")
            self.assertTrue(
                any(
                    blocker["blocker_code"] == "terminal_scan_incomplete"
                    for blocker in result["blockers"]
                )
            )


if __name__ == "__main__":
    unittest.main()
