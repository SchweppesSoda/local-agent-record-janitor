import copy
import hashlib
import json
import os
from contextlib import closing
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch

from local_agent_record_janitor.adapters import OrcaAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from tests import test_orca_frontend_cleanup as frontend_tests
from tests.orca_native_support import create_native_schema, add_native_record
from tests.orca_support import CURRENT_ID, HISTORY_ID


@unittest.skipUnless(os.name == "nt", "The qualified Orca native combination is Windows-only")
class OrcaFullflowTests(unittest.TestCase):
    def setUp(self):
        fixture = frontend_tests.OrcaFrontendCleanupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.home = fixture.root, fixture.homes[0]
        create_native_schema(self.home)
        self.rollouts = [add_native_record(self.home, sid) for sid in (CURRENT_ID, HISTORY_ID)]
        self.binary = self.root.parent / "synthetic-codex.exe"
        self.binary.write_bytes(b"synthetic identity, never launched")
        self.adapter = OrcaAdapter(profile_root=self.root, codex_bin_hint=self.binary)
        self.plan_path = self.root.parent / "full-plan.json"
        for patcher in (
            patch("local_agent_record_janitor.orca_runtime.PINNED_BINARY_SHA256", hashlib.sha256(self.binary.read_bytes()).hexdigest()),
            patch("local_agent_record_janitor.orca_target_safety._windows_process_snapshot", return_value=()),
            patch("local_agent_record_janitor.orca_target_safety._system_configuration_root", return_value=self.root.parent / "system-config"),
            patch.dict(os.environ, {"APPDATA": str(self.root.parent / "appdata"), "LOCALAPPDATA": str(self.root.parent / "localappdata"),
                "CODEX_HOME": str(self.root.parent / "default-home"), "ORCA_USER_DATA_PATH": ""}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_args, **_kwargs: ()))

    def plan(self):
        return self.coordinator.plan_operation(client="orca", record_ids=("orca_fixture_01",), engines=("codex",),
            adapters=(self.adapter,), plan_path=self.plan_path, operation_id="orca-fullflow")

    def native_gone(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("DELETE FROM threads")
            db.commit()
        for path in self.rollouts:
            path.unlink()

    def test_native_absence_includes_orphan_rollouts_and_legacy_index(self):
        from local_agent_record_janitor.orca_cleanup import _native_absent
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("DELETE FROM threads")
            db.commit()
        self.assertFalse(_native_absent(self.home, [CURRENT_ID]))
        for path in self.rollouts:
            path.unlink()
        index = self.home / "session_index.jsonl"
        index.write_text(json.dumps({"id": CURRENT_ID}) + "\n", encoding="utf-8")
        self.assertFalse(_native_absent(self.home, [CURRENT_ID]))
        index.write_bytes(b"")
        self.assertTrue(_native_absent(self.home, [CURRENT_ID]))

    def test_frontend_only_full_apply_and_cold_verify(self):
        self.native_gone()
        plan = self.plan()
        self.assertEqual(plan.get("goal_status"), "ready", plan)
        self.assertEqual([a["kind"] for a in plan["actions"]], ["delete_orca_frontend"])
        args = dict(operation_id=plan["operation_id"], plan_path=self.plan_path, plan_sha256=plan["plan_sha256"])
        result = OperationCoordinator(CleanupService(client_inspector=lambda *_args, **_kwargs: ())).apply_operation(**args, clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        cold = OperationCoordinator(CleanupService(client_inspector=lambda *_args, **_kwargs: ()))
        self.assertEqual(cold.status_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)["goal_status"], "complete")
        verified = cold.verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(verified["goal_status"], "complete", verified)

    def test_action_binding_and_classification_are_recomputed_and_compared(self):
        from local_agent_record_janitor.orca_cleanup import validate_action
        plan = self.plan()
        action = next(a for a in self.coordinator._live[plan["operation_id"]].candidates if a.kind.value == "delete_orca_frontend")
        validate_action(plan, action)
        for field in ("binding", "classification"):
            changed = copy.deepcopy(plan)
            next(a for a in changed["actions"] if a["kind"] == "delete_orca_frontend")[field] = "changed"
            with self.assertRaisesRegex(Exception, "approved_action_changed"):
                validate_action(changed, action)

    def test_unknown_native_operation_blocks_even_frontend_only_deletion(self):
        from tests.test_mutation_guard import write_journal, basic_action
        self.native_gone()
        plan = self.plan()
        write_journal(self.home, "unfinished-native", basic_action(self.home))
        before = (self.root / "orca-data.json").read_bytes()
        result = self.coordinator.apply_operation(operation_id=plan["operation_id"], plan_path=self.plan_path,
            plan_sha256=plan["plan_sha256"], clients_closed=True)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["mutation_started"])
        self.assertEqual((self.root / "orca-data.json").read_bytes(), before)

    def test_direct_frontend_call_without_operation_ticket_is_rejected(self):
        from local_agent_record_janitor.orca_cleanup import execute
        plan = self.plan()
        evidence = next(a for a in plan["actions"] if a["kind"] == "delete_orca_frontend")["impact"]["external_action_payload"]["orca_frontend_evidence"]
        with self.assertRaisesRegex(Exception, "frontend_execution_ticket_required"):
            execute(evidence, client_inspector=lambda *_: (), phase_callback=lambda _: self.fail("unexpected write"))

    def test_partial_frontend_is_not_retried_and_full_after_state_can_be_verified(self):
        self.native_gone()
        plan = self.plan()
        args = dict(operation_id=plan["operation_id"], plan_path=self.plan_path, plan_sha256=plan["plan_sha256"])
        from local_agent_record_janitor import orca_journal_cleanup
        original = orca_journal_cleanup.apply
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("synthetic interruption after journal commit")
        with patch.object(orca_journal_cleanup, "apply", side_effect=interrupted):
            result = self.coordinator.apply_operation(**args, clients_closed=True)
        self.assertEqual(result["goal_status"], "unknown", result)
        cold = OperationCoordinator(CleanupService(client_inspector=lambda *_args, **_kwargs: ()))
        with patch("local_agent_record_janitor.orca_cleanup.execute", side_effect=AssertionError("must not repeat")):
            retry = cold.apply_operation(**args, clients_closed=True)
            self.assertEqual(retry["goal_status"], "unknown", retry)
            verified = cold.verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(verified["goal_status"], "complete", verified)
        self.assertTrue(verified["modified"])

    def test_full_plan_freezes_native_chain_frontend_copies_and_exact_dependencies(self):
        before = [path.read_bytes() for path in self.rollouts]
        result = self.plan()
        self.assertEqual(result.get("goal_status"), "ready", result)
        self.assertEqual(result["schema_version"], "larj.operation-plan.v3")
        native = [a for a in result["actions"] if a["kind"] == "delete_conversation"]
        frontend = [a for a in result["actions"] if a["kind"] == "delete_orca_frontend"]
        self.assertEqual({a["target"]["thread_id"] for a in native}, {CURRENT_ID, HISTORY_ID})
        self.assertEqual(len(frontend), 1)
        self.assertEqual(set(frontend[0]["impact"]["external_action_payload"]["requires_action_ids"]), {a["action_id"] for a in native})
        self.assertEqual([path.read_bytes() for path in self.rollouts], before)
        self.assertNotIn("ORCA_PRIVATE_OPTIONS_SENTINEL", json.dumps(result))

    def test_reference_ticket_is_exact_and_never_converts_unselected_or_changed_bindings(self):
        from dataclasses import replace
        from local_agent_record_janitor.orca_authorization import coordinator_scope, permits_reference
        plan = self.plan()
        self.assertEqual(plan.get("goal_status"), "ready", plan)
        refs = self.adapter.snapshot_references(refresh=True).references
        selected = next(r for r in refs if r.frontend_id == "orca_fixture_01")
        with coordinator_scope(plan["target_safety_evidence"]):
            self.assertTrue(permits_reference(selected))
            self.assertFalse(permits_reference(replace(selected, binding_key="changed")))
            self.assertFalse(permits_reference(replace(selected, frontend_id="orca_fixture_02")))
        self.assertFalse(permits_reference(selected))


if __name__ == "__main__":
    unittest.main()
