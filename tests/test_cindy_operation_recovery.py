from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.cleanup_service import partition_actions
from local_agent_record_janitor.frontend_session_cleanup import (
    build_cindy_session_delete_evidence,
)
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.operation_store import OperationStore, write_new_json
from local_agent_record_janitor.planning import (
    ActionImpact,
    ActionKind,
    CandidateAction,
    CleanupPlan,
    RiskLevel,
    ScanStatus,
    StorageLocation,
    TargetRef,
    storage_id_for_path,
)
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.record_identity import canonical_path


class CindyOperationRecoveryTests(unittest.TestCase):
    @staticmethod
    def _database(root: Path, sdk_session_id: str | None) -> tuple[Path, str]:
        database = root / "cindy.sqlite"
        session_id = "cindy-session"
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    sdk_session_id TEXT,
                    agent_kind TEXT NOT NULL,
                    working_dir TEXT
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT,
                    content TEXT,
                    created_at INTEGER,
                    rewind_at INTEGER
                );
                """
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?)",
                (session_id, "deleted", sdk_session_id, "codex", str(root / "project")),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                ("message-1", session_id, "user", "fixture", 1, None),
            )
            connection.commit()
        return database, session_id

    @staticmethod
    def _context(
        database: Path,
        owner_root: Path,
        session_id: str,
        *,
        evidence: tuple[dict[str, object], ...],
        include_reference: bool,
        action_id: str = "frozen-session-action",
    ) -> tuple[SimpleNamespace, CandidateAction, CandidateAction | None]:
        storage_id = storage_id_for_path(database.parent)
        session_evidence = evidence
        item = session_evidence[0]
        session = CandidateAction(
            action_id=action_id,
            kind=ActionKind.DELETE_FRONTEND_SESSION,
            target=TargetRef(storage_id, session_id),
            risk=RiskLevel.HIGH,
            available=True,
            unavailable_reason=None,
            impact=ActionImpact(
                frontend_session_database_paths=(str(database),),
                frontend_session_evidence=session_evidence,
                owner_client="cindy",
                owner_process_root=str(owner_root),
                resource_path=str(database),
                external_engine="codex",
                external_storage_root=str(database.parent),
                external_action_payload={
                    "frontend_session_id": session_id,
                    "status": "deleted",
                    "owner_client": "cindy",
                    "owner_process_root": str(owner_root),
                },
            ),
            snapshot_fingerprint=":".join(
                (
                    str(item["session_row_fingerprint"]),
                    str(item["schema_bundle_fingerprint"]),
                    str(item["message_id_fingerprint"]),
                )
            ),
            resource_kind="frontend_session",
        )
        reference = CandidateAction(
            action_id="completed-reference-action",
            kind=ActionKind.REMOVE_FRONTEND_REFERENCE,
            target=TargetRef(storage_id, "reference"),
            risk=RiskLevel.HIGH,
            available=True,
            unavailable_reason=None,
            impact=ActionImpact(
                frontend_database_paths=(str(database),),
                resource_path=str(database),
            ),
            snapshot_fingerprint="reference-snapshot",
        )
        actions = (reference, session) if include_reference else (session,)
        storage = StorageLocation(
            storage_id=storage_id,
            label="Cindy fixture",
            path=database.parent,
            scan_status=ScanStatus.OK,
        )
        plan = CleanupPlan(storages=(storage,), actions=actions)
        context = SimpleNamespace(
            frontend_scan_coverage=((canonical_path(database), "sessions"),),
            snapshot=SimpleNamespace(snapshot_id="cindy-fixture"),
            plan=plan,
            actions=actions,
        )
        return context, session, reference

    def _prepare_operation(
        self,
        root: Path,
        *,
        sdk_after_clear: str | None,
    ) -> tuple[Path, dict[str, object], Path, OperationStore, list[dict[str, object]]]:
        database, session_id = self._database(root, "native-session")
        owner_root = root / "CindyGlobal"
        owner_root.mkdir()
        evidence = tuple(
            item.to_dict()
            for item in build_cindy_session_delete_evidence(
                (
                    {
                        "database": str(database),
                        "session_id": session_id,
                        "expected_status": "deleted",
                    },
                )
            )
        )
        initial_context, session, _reference = self._context(
            database,
            owner_root,
            session_id,
            evidence=evidence,
            include_reference=True,
        )
        coordinator = OperationCoordinator(SimpleNamespace())
        operation_id = "cindy-recovery"
        plan_path = root / "operation-plan.json"
        document = coordinator._make_plan_document(
            operation_id,
            {"client": "cindy", "record_ids": [session_id]},
            initial_context,
            initial_context.plan.actions,
            (),
            plan_path=plan_path,
            operation_home=None,
            codex_home=None,
            active_adapters=(),
        )
        write_new_json(plan_path, document)
        initial_live = SimpleNamespace(
            operation_id=operation_id,
            document=document,
            context=initial_context,
        )
        batches = partition_actions(initial_context.plan.actions)
        stores: list[OperationStore] = []
        for index, batch in enumerate(batches):
            store, _ = coordinator._open_batch_store(
                initial_live,
                batch,
                f"{operation_id}-{index + 1}",
                database.parent,
                index,
            )
            stores.append(store)
        stores[0].append_event(
            {"event": "batch_finished", "goal_status": "complete"},
            state_updates={
                "phase": "finished",
                "goal_status": "complete",
                "goal_satisfied": True,
                "modified": True,
                "mutation_started": False,
            },
        )
        blocker = {
            "blocker_code": "target_client_running",
            "scope": f"child:{operation_id}-2",
            "severity": "error",
            "retryable": True,
            "message": "Cindy.exe is still running",
        }
        stores[1].append_event(
            {
                "event": "batch_finished",
                "goal_status": "blocked",
                "blockers": [blocker],
            },
            state_updates={
                "phase": "blocked",
                "goal_status": "blocked",
                "goal_satisfied": False,
                "modified": False,
                "mutation_started": False,
                "current_action_state": "not_started",
                "blockers": [blocker],
            },
        )
        before_reference_events = stores[0].read_events()
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE sessions SET sdk_session_id=? WHERE id=?",
                (sdk_after_clear, session_id),
            )
            connection.commit()
        current_evidence = tuple(
            item.to_dict()
            for item in build_cindy_session_delete_evidence(
                (
                    {
                        "database": str(database),
                        "session_id": session_id,
                        "expected_status": "deleted",
                    },
                )
            )
        )
        fresh_context, _current_session, _ = self._context(
            database,
            owner_root,
            session_id,
            evidence=current_evidence,
            include_reference=False,
            action_id="fresh-session-action",
        )
        return (
            database,
            document,
            plan_path,
            stores[0],
            [before_reference_events, fresh_context, session, owner_root],
        )

    def _apply(self, root: Path, sdk_after_clear: str | None) -> dict[str, object]:
        database, document, plan_path, reference_store, extras = (
            self._prepare_operation(root, sdk_after_clear=sdk_after_clear)
        )
        fresh_context = extras[1]
        fresh = OperationCoordinator(
            CleanupService(client_inspector=lambda _path: ())
        )
        with (
            patch.object(
                fresh,
                "_build_context",
                return_value=(fresh_context, (), None, None, {}, {}),
            ),
            patch.object(
                fresh,
                "_terminal_context",
                return_value=(SimpleNamespace(plan=SimpleNamespace(actions=(), storages=fresh_context.plan.storages), frontend_scan_coverage=fresh_context.frontend_scan_coverage), None),
            ),
        ):
            result = fresh.apply_operation(
                client="cindy",
                record_ids=(str(extras[2].target.thread_id),),
                operation_id=str(document["operation_id"]),
                plan_path=plan_path,
                plan_sha256=str(document["plan_sha256"]),
                clients_closed=True,
                adapters=(),
            )
        result["_reference_event_count"] = len(reference_store.read_events())
        result["_reference_event_count_before"] = len(extras[0])
        result["_database"] = database
        return result

    def test_cross_process_resume_uses_frozen_session_evidence_and_skips_complete_child(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._apply(Path(temporary), None)
            self.assertEqual(result["goal_status"], "complete")
            self.assertEqual(result["_reference_event_count"], result["_reference_event_count_before"])
            self.assertEqual(len(result["batches"]), 1)
            self.assertEqual(
                result["batches"][0]["child_operation_id"],
                "cindy-recovery-2",
            )
            self.assertEqual(result["batches"][0]["status"], "complete")
            database = result["_database"]
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                    0,
                )

    def test_non_null_sdk_replacement_remains_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._apply(Path(temporary), "replacement-session")
            self.assertEqual(result["goal_status"], "blocked")
            self.assertTrue(
                any(
                    blocker.get("blocker_code") == "target_state_changed"
                    for blocker in result.get("blockers", ())
                )
            )
            database = result["_database"]
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT sdk_session_id FROM sessions"
                    ).fetchone()[0],
                    "replacement-session",
                )

    def test_guard_checkpoint_without_mutation_is_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _database, document, _plan_path, _reference_store, extras = (
                self._prepare_operation(root, sdk_after_clear=None)
            )
            fresh_context = extras[1]
            coordinator = OperationCoordinator(SimpleNamespace())
            candidates, blockers = coordinator._bind_fresh_candidates(
                document,
                fresh_context,
                skip_child_ids={"cindy-recovery-1"},
            )
            self.assertEqual(blockers, [])

            class GuardBlockedService:
                def execute(self, _context, actions, **kwargs):
                    action = tuple(actions)[0]
                    kwargs["action_state_callback"]("guard_started", action, None)
                    raise RuntimeError("Cindy.exe is still running")

            coordinator.service = GuardBlockedService()
            live = SimpleNamespace(
                operation_id="cindy-recovery",
                document=document,
                context=fresh_context,
                candidates=tuple(candidates),
                client="cindy",
                adapters=(),
                manual_actions={},
                result=None,
            )
            with patch.object(
                coordinator,
                "_terminal_context",
                return_value=(SimpleNamespace(plan=SimpleNamespace(actions=(), storages=fresh_context.plan.storages), frontend_scan_coverage=fresh_context.frontend_scan_coverage), None),
            ):
                result = coordinator._execute_live(
                    live,
                    timeout=1,
                    app_server_factory=lambda **_kwargs: None,
                    binary_resolver=lambda _hint: None,
                )
            self.assertEqual(result["goal_status"], "blocked")
            state = OperationStore(
                root,
                "cindy-recovery-2",
            ).read_state()
            self.assertEqual(state["current_action_state"], "not_started")
            self.assertFalse(state["mutation_started"])
            self.assertFalse(state.get("attempted", False))

    def test_changed_session_rebind_validation_is_linear(self) -> None:
        for count in (1, 10, 100):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                database = root / "cindy.sqlite"
                owner_root = root / "CindyGlobal"
                storage_id = storage_id_for_path(root)

                def make_action(action_id: str, index: int, sdk: str | None):
                    evidence = {
                        "database": str(database),
                        "session_id": f"session-{index}",
                        "expected_status": "deleted",
                        "session_schema_fingerprint": "schema",
                        "session_row_fingerprint": f"row-{index}-{sdk or 'null'}",
                        "stable_session_row_fingerprint": f"stable-{index}",
                        "expected_sdk_session_id": sdk,
                        "schema_bundle_fingerprint": "bundle",
                        "message_id_fingerprint": f"message-{index}",
                    }
                    snapshot = ":".join(
                        (
                            evidence["session_row_fingerprint"],
                            evidence["schema_bundle_fingerprint"],
                            evidence["message_id_fingerprint"],
                        )
                    )
                    return CandidateAction(
                        action_id=action_id,
                        kind=ActionKind.DELETE_FRONTEND_SESSION,
                        target=TargetRef(storage_id, evidence["session_id"]),
                        risk=RiskLevel.HIGH,
                        available=True,
                        unavailable_reason=None,
                        impact=ActionImpact(
                            frontend_session_database_paths=(str(database),),
                            frontend_session_evidence=(evidence,),
                            owner_client="cindy",
                            owner_process_root=str(owner_root),
                            resource_path=str(database),
                            external_engine="codex",
                            external_storage_root=str(root),
                        ),
                        snapshot_fingerprint=snapshot,
                        resource_kind="frontend_session",
                    )

                frozen_actions = tuple(
                    make_action(f"frozen-{index}", index, "native")
                    for index in range(count)
                )
                fresh_actions = tuple(
                    make_action(f"fresh-{index}", index, None)
                    for index in range(count)
                )
                coordinator = OperationCoordinator(SimpleNamespace())
                document = {
                    "actions": [
                        coordinator._action_document(
                            SimpleNamespace(plan=SimpleNamespace(observations=())),
                            action,
                        )
                        for action in frozen_actions
                    ],
                    "child_batches": [
                        {
                            "child_operation_id": "linear-1",
                            "storage_id": storage_id,
                            "mutation_family": "delete_frontend_session",
                            "resource_key": [str(database), str(owner_root)],
                            "action_ids": [
                                action.action_id for action in frozen_actions
                            ],
                        },
                    ],
                }
                context = SimpleNamespace(
                    plan=SimpleNamespace(actions=fresh_actions)
                )
                with patch.object(
                    OperationCoordinator,
                    "_frontend_session_transition_allowed",
                    wraps=OperationCoordinator._frontend_session_transition_allowed,
                ) as validation:
                    rebound, blockers = coordinator._bind_fresh_candidates(
                        document,
                        context,
                    )
                self.assertEqual(blockers, [])
                self.assertEqual(len(rebound), count)
                self.assertEqual(validation.call_count, count)


if __name__ == "__main__":
    unittest.main()
