from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.adapters.base import FrontendBatchSnapshot
from local_agent_record_janitor.cleaner import ScanReport
from local_agent_record_janitor.cleanup_service import CleanupContext, CleanupService, StoreSnapshot
from local_agent_record_janitor.client_inventory import build_client_engine_contexts, build_client_inventory
from local_agent_record_janitor.core_types import StorageKind, StorageRef
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.planning import (
    ActionImpact,
    ActionKind,
    CandidateAction,
    CleanupPlan,
    RiskLevel,
    StorageLocation,
    TargetRef,
    storage_id_for_path,
)
from local_agent_record_janitor.record_identity import EngineCapability
from local_agent_record_janitor.inventory import FrontendSessionRecord


@dataclass
class _CompositeAdapter:
    codex_home: Path
    database: Path
    rows: tuple[FrontendSessionRecord, ...]
    catalogs: dict[str, object]
    snapshot_calls: int = 0
    frontend_reads: int = 0

    name: str = "cindy"
    client: str = "cindy"
    engine: str = "codex"

    def __post_init__(self) -> None:
        self._snapshot: FrontendBatchSnapshot | None = None

    def snapshot_sessions(self, *, all_backends: bool = False, refresh: bool = False) -> FrontendBatchSnapshot:
        del all_backends
        self.snapshot_calls += 1
        if self._snapshot is None or refresh:
            self.frontend_reads += 1
            self._snapshot = FrontendBatchSnapshot(
                client=self.client,
                database=self.database,
                records=self.rows,
                fingerprint="frontend:v1:test",
            )
        return self._snapshot

    def list_sessions(self) -> list[FrontendSessionRecord]:
        raise AssertionError("the cached frontend snapshot must be used")

    def native_catalog_for(self, engine: str) -> object:
        return self.catalogs[engine]

    def registered_capability(self, engine: str) -> EngineCapability:
        return EngineCapability(
            "cindy",
            engine,
            native_delete=True,
            frontend_reference_delete=True,
        )


class _FakeWriterService:
    def __init__(self, contexts: dict[str, CleanupContext], *, unknown_first: bool) -> None:
        self.contexts = contexts
        self.unknown_first = unknown_first
        self.prepare_calls = 0
        self.session_prepare_calls: list[str] = []
        self.executions: list[tuple[str | None, tuple[str, ...]]] = []
        self._typed = CleanupService().typed_actions

    def prepare(self, _adapters: object, *, platforms: object) -> CleanupContext:
        del platforms
        self.prepare_calls += 1
        return _empty_context()

    def prepare_session_catalog(self, engine: str, _catalog: object, **_kwargs: object) -> CleanupContext:
        self.session_prepare_calls.append(engine)
        return self.contexts[engine]

    def typed_actions(self, plan: CleanupPlan) -> tuple[object, ...]:
        return self._typed(plan)

    def execute(self, context: CleanupContext, actions: tuple[object, ...], **_kwargs: object) -> object:
        engine = context.session_engine
        action_ids = tuple(str(action.action_id) for action in actions)
        self.executions.append((engine, action_ids))
        unknown = self.unknown_first and len(self.executions) == 1
        status = "unknown" if unknown else "deleted"
        return SimpleNamespace(
            results=tuple(SimpleNamespace(status=status) for _ in actions),
            modified=not unknown,
        )


def _empty_context() -> CleanupContext:
    snapshot = StoreSnapshot(
        snapshot_id="snapshot:base",
        captured_at="2026-01-01T00:00:00+00:00",
        platforms=("cindy",),
        storages=(),
        records=(),
        evidence=(),
        scan_complete=True,
        blocker_codes=(),
        report=ScanReport(),
        active_adapters=(),
    )
    return CleanupContext(snapshot=snapshot, plan=CleanupPlan(), actions=())


def _native_context(engine: str, root: Path, record_id: str, action_id: str) -> CleanupContext:
    storage_id = storage_id_for_path(root)
    kind = ActionKind.DELETE_PI_SESSION if engine == "pi" else ActionKind.DELETE_CLAUDE_SESSION
    candidate = CandidateAction(
        action_id=action_id,
        kind=kind,
        target=TargetRef(storage_id, record_id),
        risk=RiskLevel.HIGH,
        available=True,
        unavailable_reason=None,
        impact=ActionImpact(
            affected_thread_ids=(record_id,),
            external_engine=engine,
            external_storage_root=str(root),
            external_artifact_paths=(str(root / record_id),),
        ),
        snapshot_fingerprint=f"snapshot:{engine}",
        resource_kind=f"{engine}_session",
    )
    snapshot = StoreSnapshot(
        snapshot_id=f"snapshot:{engine}",
        captured_at="2026-01-01T00:00:00+00:00",
        platforms=(engine,),
        storages=(StorageRef(storage_id, StorageKind.JSONL if engine == "pi" else StorageKind.MANIFEST, root, engine),),
        records=(),
        evidence=(),
        scan_complete=True,
        blocker_codes=(),
        report=ScanReport(),
        active_adapters=(),
    )
    plan = CleanupPlan(
        storages=(StorageLocation(storage_id, f"{engine} test store", root),),
        actions=(candidate,),
    )
    service = CleanupService()
    return CleanupContext(snapshot=snapshot, plan=plan, actions=service.typed_actions(plan), session_engine=engine)


class ClientOperationIntegrationTests(unittest.TestCase):
    def test_non_native_success_outcomes_set_top_level_status_and_modified(self) -> None:
        coordinator = OperationCoordinator(SimpleNamespace())
        cases = (
            ("legacy_repair", "repaired"),
            ("desktop_cleanup", "cleaned"),
            ("frontend_cleanup", "cleaned"),
            ("relation_cleanup", "cleaned"),
        )
        for field_name, expected_status in cases:
            with self.subTest(field=field_name):
                result = SimpleNamespace(
                    to_dict=lambda status=expected_status: {"status": status},
                )
                outcome = SimpleNamespace(**{field_name: result})
                statuses = coordinator._outcome_statuses(outcome)
                self.assertEqual(statuses, {expected_status})
                self.assertTrue(
                    bool(statuses.intersection({"deleted", "cleaned", "repaired"}))
                )

    def test_cindy_pi_and_claude_child_contexts_stop_after_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend_home = root / "cindy-home"
            frontend_home.mkdir()
            database = root / "cindy.db"
            pi_root = root / "pi-sessions"
            claude_root = root / "claude-config"
            pi_root.mkdir()
            claude_root.mkdir()
            pi_artifact = pi_root / "pi-1"
            claude_artifact = claude_root / "claude-1.jsonl"
            pi_artifact.write_text("metadata", encoding="utf-8")
            claude_artifact.write_text("metadata", encoding="utf-8")
            rows = (
                FrontendSessionRecord(
                    platform="cindy",
                    platform_session_id="cindy-pi",
                    thread_id="pi-1",
                    database=database,
                    codex_home=frontend_home,
                    backend="pi",
                    details={"working_dir": str(root / "pi-project")},
                ),
                FrontendSessionRecord(
                    platform="cindy",
                    platform_session_id="cindy-claude",
                    thread_id="claude-1",
                    database=database,
                    codex_home=frontend_home,
                    backend="claude",
                    details={"working_dir": str(root / "claude-project")},
                ),
            )
            pi_record = SimpleNamespace(
                session_id="pi-1",
                session_root=pi_root,
                path=pi_artifact,
                cwd=str(root / "pi-project"),
                cindy_references=({"cindy_session_id": "cindy-pi"},),
                action_id="pi-action",
                deletable=True,
            )
            claude_record = SimpleNamespace(
                session_id="claude-1",
                config_dir=claude_root,
                transcript_paths=(claude_artifact,),
                project_paths=(root / "claude-project",),
                manifest=(),
                frontend_references=({"cindy_session_id": "cindy-claude"},),
                action_id="claude-action",
                deletable=True,
            )
            adapter = _CompositeAdapter(
                frontend_home,
                database,
                rows,
                {
                    "pi": SimpleNamespace(records=(pi_record,), session_root=pi_root),
                    "claude": SimpleNamespace(records=(claude_record,), config_dir=claude_root),
                },
            )
            # Build the real client inventory/context projection once up front;
            # the operation coordinator then consumes those bound contexts.
            inventory = build_client_inventory((adapter,), client="cindy")
            engine_contexts = build_client_engine_contexts(
                (adapter,), client="cindy", inventory=inventory
            )
            self.assertEqual({context.engine for context in engine_contexts}, {"pi", "claude"})
            contexts = {
                engine: _native_context(
                    engine,
                    pi_root if engine == "pi" else claude_root,
                    "pi-1" if engine == "pi" else "claude-1",
                    "pi-action" if engine == "pi" else "claude-action",
                )
                for engine in ("pi", "claude")
            }
            service = _FakeWriterService(contexts, unknown_first=True)
            coordinator = OperationCoordinator(service)
            plan_path = root / "operation-plan.json"
            with patch(
                "local_agent_record_janitor.client_inventory.build_client_inventory",
                return_value=inventory,
            ), patch(
                "local_agent_record_janitor.client_inventory.build_client_engine_contexts",
                return_value=engine_contexts,
            ):
                result = coordinator.run_operation(
                    client="cindy",
                    record_ids=("pi-1", "claude-1"),
                    adapters=(adapter,),
                    plan_path=plan_path,
                    clients_closed=True,
                )
                live = coordinator._live[str(result["operation_id"])]
                self.assertEqual(
                    {action.action_id for action in live.candidates},
                    {"pi-action", "claude-action"},
                )
                self.assertEqual(
                    {action.kind.value for action in live.candidates},
                    {"delete_pi_session", "delete_claude_session"},
                )
            self.assertEqual(result["goal_status"], "unknown")
            self.assertEqual(len(result["batches"]), 1)
            self.assertEqual(len(service.executions), 1)
            first_engine, first_action_ids = service.executions[0]
            self.assertIn(first_engine, {"pi", "claude"})
            self.assertEqual(first_action_ids, ("pi-action" if first_engine == "pi" else "claude-action",))

            # A fresh operation proves that the same composite plan can route
            # both child batches to their own native writer contexts.
            success_service = _FakeWriterService(contexts, unknown_first=False)
            success_coordinator = OperationCoordinator(success_service)
            with patch(
                "local_agent_record_janitor.client_inventory.build_client_inventory",
                return_value=inventory,
            ), patch(
                "local_agent_record_janitor.client_inventory.build_client_engine_contexts",
                return_value=engine_contexts,
            ):
                success = success_coordinator.run_operation(
                    client="cindy",
                    record_ids=("pi-1", "claude-1"),
                    adapters=(adapter,),
                    plan_path=root / "success-plan.json",
                    clients_closed=True,
                )
            self.assertEqual(
                {engine for engine, _action_ids in success_service.executions},
                {"pi", "claude"},
            )
            self.assertEqual(
                {action_id for _engine, action_ids in success_service.executions for action_id in action_ids},
                {"pi-action", "claude-action"},
            )
            self.assertEqual(service.prepare_calls, 1)
            self.assertEqual(adapter.frontend_reads, 1)
            self.assertEqual(adapter.snapshot_sessions(), adapter.snapshot_sessions())


if __name__ == "__main__":
    unittest.main()
