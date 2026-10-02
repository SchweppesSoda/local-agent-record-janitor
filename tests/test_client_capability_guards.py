from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent_record_janitor.action_registry import ACTION_REGISTRY
from local_agent_record_janitor.adapters import CindyAdapter
from local_agent_record_janitor.cleaner import ScanReport, scan_adapters
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.execution import ExecutionError
from local_agent_record_janitor.inventory import build_session_catalog
from local_agent_record_janitor.manual_delete import (
    ManualDeletePlanError, ManualDeleteSelectionError, build_manual_delete_plan, execute_manual_delete,
)
from local_agent_record_janitor.client_capability_guards import ClientCapabilityLimits, action_frontend_engines
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.planning import (
    ActionImpact, ActionKind, CandidateAction, CleanupPlan, RiskLevel,
    StorageLocation, TargetRef, storage_id_for_path,
)
from local_agent_record_janitor.record_identity import EngineCapability
from tests.support import create_thread_index, write_rollout
from tests.test_cindy_references import create_database
from tests.test_client_inventory import _write_pi_session


class _ReadonlyCindy(CindyAdapter):
    def capability_limit_for(self, engine: str) -> EngineCapability:
        return EngineCapability("cindy", engine, reason="Synthetic read-only profile")


class _ReadonlyPiCindy(CindyAdapter):
    def capability_limit_for(self, engine: str) -> EngineCapability:
        return EngineCapability("cindy", engine, reason="Pi metadata is read-only") if engine == "pi" else super().capability_limit_for(engine)


class ClientCapabilityGuardTests(unittest.TestCase):
    def _profile(self, root: Path, name: str, *, readonly: bool, engine: str = "codex"):
        profile = root / name
        home = profile / "codex-home"
        home.mkdir(parents=True)
        database = profile / "cindy.db"
        create_database(database, [("ui", name, "deleted", engine)])
        if engine == "codex":
            rollout = write_rollout(home, name, originator="cindy")
            create_thread_index(home, [{"id": name, "rollout_path": str(rollout),
                                                          "archived": 0, "source": "cindy"}])
        else:
            _write_pi_session(profile / "pi-agent-home", name)
        cls = _ReadonlyCindy if readonly else CindyAdapter
        return cls(database=database, codex_home=home, cindy_root=profile)

    def test_legacy_scan_and_manual_catalog_keep_readonly_profile_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writable = self._profile(root, "writable", readonly=False)
            readonly = self._profile(root, "readonly", readonly=True)
            report = scan_adapters((writable, readonly))
            by_id = {finding.thread_id: finding for finding in report.findings}
            self.assertTrue(by_id["writable"].details["cleanable"])
            self.assertFalse(by_id["readonly"].details["cleanable"])
            self.assertIn("client_capability_limit", by_id["readonly"].details["cleanup_blocker_codes"])
            catalog = build_session_catalog((writable, readonly))
            records = {record.thread_id: record for record in catalog.records}
            self.assertTrue(records["writable"].deletable)
            self.assertFalse(records["readonly"].deletable)
            self.assertIn("client_capability_limit", records["readonly"].blocker_codes)
            plan = build_manual_delete_plan(catalog)
            self.assertTrue(plan.with_selected_actions(("writable",)).actions[0].available)
            with self.assertRaises(ManualDeleteSelectionError):
                plan.with_selected_actions(("readonly",))

            # A previously approved writable plan cannot pass revalidation
            # after the exact profile's implementation ceiling is reduced.
            approved = plan.with_selected_actions(("writable",))
            downgraded = _ReadonlyCindy(database=writable.database, codex_home=writable.codex_home,
                                       cindy_root=writable.cindy_root)
            writer = Mock(side_effect=AssertionError("manual writer must not be called"))
            with self.assertRaises(ManualDeletePlanError):
                execute_manual_delete(approved, catalog_builder=lambda: build_session_catalog((downgraded,)),
                    approved_plan_fingerprint=approved.plan_fingerprint, clients_closed=True,
                    app_server_factory=writer, binary_resolver=writer)
            writer.assert_not_called()

    def test_real_frontend_reference_uses_pi_limit_instead_of_codex_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = self._profile(root, "pi-only", readonly=False, engine="pi")
            create_thread_index(original.codex_home, [])
            writable = CindyAdapter(database=original.database, codex_home=original.codex_home,
                                     cindy_root=original.cindy_root, backend="pi")
            readonly = _ReadonlyPiCindy(database=original.database, codex_home=original.codex_home,
                                        cindy_root=original.cindy_root, backend="pi")
            service = CleanupService(client_inspector=lambda _root: ())
            context = service.prepare((writable,))
            action = next(a for a in context.plan.actions if a.kind is ActionKind.REMOVE_FRONTEND_REFERENCE)
            self.assertTrue(action.available)
            self.assertIs(ClientCapabilityLimits.from_adapters((writable,)).restrict_plan(context.plan), context.plan)
            limited_context = replace(context, snapshot=replace(context.snapshot, active_adapters=(readonly,)))
            writer = Mock(side_effect=AssertionError("frontend writer must not be called"))
            with patch("local_agent_record_janitor.execution.execute_frontend_reference_cleanup", writer):
                with self.assertRaises(ExecutionError) as raised:
                    service.execute(limited_context, (action,), timeout=1, app_server_factory=writer, binary_resolver=writer)
            self.assertEqual(raised.exception.kind, "client_capability_limit")
            self.assertIn("cindy/pi", str(raised.exception))
            writer.assert_not_called()

            original_evidence = action.impact.frontend_reference_evidence[0]
            cases = (("pi", "codex"), (None,), ("claude-code",))
            for kinds in cases:
                evidence = tuple({**original_evidence, "expected": {
                    "native_session_id": "pi-only", **({"agent_kind": kind} if kind else {})}}
                    for kind in kinds)
                candidate = replace(action, observation_ids=(), impact=replace(action.impact, frontend_reference_evidence=evidence))
                plan = replace(context.plan, actions=(candidate,), observations=())
                limited = ClientCapabilityLimits.from_adapters((readonly,)).restrict_plan(plan)
                self.assertFalse(limited.actions[0].available)
                if kinds == ("claude-code",):
                    self.assertEqual(action_frontend_engines(plan, candidate), ("unsupported:claude-code",))

    def test_service_execution_checks_every_implemented_mutation_family_before_writer_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = self._profile(root, "readonly", readonly=True)
            service = CleanupService(client_inspector=lambda _root: ())
            base = service.prepare_report(ScanReport(), active_adapters=(adapter,))
            writer = Mock(side_effect=AssertionError("writer must not be called"))
            with ExitStack() as stack:
                for name in ("repair_legacy_index", "execute_desktop_state_cleanup", "execute_relation_cleanup",
                             "execute_frontend_reference_cleanup", "execute_cindy_session_cleanup",
                             "execute_native_project_cleanup", "execute_aionui_project_cleanup"):
                    stack.enter_context(patch("local_agent_record_janitor.execution." + name, writer))
                self._assert_families_blocked(adapter, service, base, writer)
            writer.assert_not_called()

    def _assert_families_blocked(self, adapter, service, base, writer):
        for kind, capability in ACTION_REGISTRY.items():
            if not capability.implemented or capability.mutation_family is None:
                continue
            with self.subTest(kind=kind):
                engine = "pi" if kind == "delete_pi_session" else "claude" if kind == "delete_claude_session" else "codex"
                action = CandidateAction("synthetic:" + kind, ActionKind(kind),
                    TargetRef(storage_id_for_path(adapter.codex_home), "readonly"), RiskLevel.HIGH,
                    True, None, ActionImpact(frontend_database_paths=(str(adapter.database),),
                        frontend_session_database_paths=(str(adapter.database),),
                        frontend_project_database_paths=(str(adapter.database),), external_engine=engine,
                        external_storage_root=str(adapter.codex_home)), "snapshot:v1:synthetic")
                plan = CleanupPlan(storages=(StorageLocation(storage_id_for_path(adapter.codex_home),
                    "Synthetic store", adapter.codex_home),), actions=(action,), plan_fingerprint="plan:v1:synthetic")
                context = replace(base, plan=plan, actions=service.typed_actions(plan))
                with self.assertRaises(ExecutionError) as raised:
                    service.execute(context, (action,), timeout=1, app_server_factory=writer,
                        binary_resolver=writer, cleaner=writer, session_executor=writer)
                self.assertEqual(raised.exception.kind, "client_capability_limit")

    def test_coordinator_retains_pi_target_limits_when_merging_one_native_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writable = self._profile(root, "writable", readonly=False, engine="pi")
            readonly = self._profile(root, "readonly", readonly=True, engine="pi")
            coordinator = OperationCoordinator(CleanupService(client_inspector=lambda _root: ()))
            for index, order in enumerate(((writable, readonly), (readonly, writable))):
                blocked = coordinator.plan_operation(client="cindy", record_ids=("readonly",), engines=("pi",),
                    adapters=order, plan_path=root / f"readonly-plan-{index}.json")
                self.assertEqual(blocked["goal_status"], "blocked")
                self.assertEqual(blocked["counts"]["action_count"], 0)
                ready = coordinator.plan_operation(client="cindy", record_ids=("writable",), engines=("pi",),
                    adapters=order, plan_path=root / f"writable-plan-{index}.json")
                self.assertEqual(ready["goal_status"], "ready")
                self.assertTrue(ready["capabilities"]["pi"]["native_delete"])
            only_readonly = coordinator.plan_operation(client="cindy", record_ids=("readonly",), engines=("pi",),
                adapters=(readonly,), plan_path=root / "only-readonly-plan.json")
            self.assertFalse(only_readonly["capabilities"]["pi"]["native_delete"])
            self.assertEqual(only_readonly["goal_status"], "blocked")
            self.assertTrue(only_readonly["blockers"])
            writer = Mock(side_effect=AssertionError("readonly operation must not dispatch"))
            with patch("local_agent_record_janitor.execution.execute_cindy_session_cleanup", writer):
                result = coordinator.run_operation(client="cindy", record_ids=("readonly",), engines=("pi",),
                    adapters=(readonly,), plan_path=root / "readonly-run.json", clients_closed=True,
                    app_server_factory=writer, binary_resolver=writer)
            self.assertEqual(result["goal_status"], "blocked")
            self.assertTrue(result["blockers"])
            writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
