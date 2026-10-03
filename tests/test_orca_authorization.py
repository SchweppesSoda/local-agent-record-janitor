from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.orca_authorization import coordinator_scope, execution_scope, permits
from local_agent_record_janitor.orca_target_safety import _schema_metadata, evidence_for_actions
from local_agent_record_janitor.mutation_guard import scopes_for_frozen_plan, mutation_guard, MutationScope, UnknownMutationError
from local_agent_record_janitor.operation_store import plan_sha256
from tests.orca_native_support import create_native_schema
from tests.test_mutation_guard import write_journal, basic_action


class OrcaAuthorizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve(strict=True)

    def proof_document(self):
        action = {"action_id": "fixed-action", "kind": "delete_conversation",
                  "target": {"storage_id": "fixed-store", "thread_id": "target"},
                  "impact": {"affected_thread_ids": ["target", "child"],
                             "rollout_paths": [str(self.home / "sessions" / "target.jsonl")]}}
        evidence = {"schema_version": "larj.orca-target-safety.v1", "native_delete": True,
                    "api_boundary": "validated_fixed_runtime", "action_id": "fixed-action", "preflight_complete": True,
                    "frozen": {"home": str(self.home), "record_id": "target", "affected_thread_ids": ["child", "target"],
                               "rollout_paths": action["impact"]["rollout_paths"]}}
        return {"actions": [action], "target_safety_evidence": [evidence],
                "storages": [{"storage_id": "fixed-store", "path": str(self.home)}]}

    def test_preflight_boolean_and_fake_hash_cannot_grant_execution(self):
        evidence = {"schema_version": "larj.orca-target-safety.v1", "native_delete": False,
                    "api_boundary": "not_validated", "preflight_complete": True,
                    "frozen": {"home": str(self.home), "affected_thread_ids": ["target"]}}
        with self.assertRaises(ValueError):
            with coordinator_scope([evidence], execution=True, plan_sha256="x" * 64):
                self.fail("unvalidated preflight must never grant a writer")
        self.assertFalse(permits(self.home, ["target"], execution=True))
        with self.assertRaises(ValueError):
            with coordinator_scope([evidence]):
                self.fail("planning ticket also needs the fixed admitted boundary")

    def test_current_action_cannot_expand_home_kind_ids_or_rollout_scope(self):
        document = self.proof_document()
        self.assertEqual(len(evidence_for_actions(document, document["actions"])), 1)
        for field, replacement in (("storage_id", "other-home"), ("thread_id", "other-target")):
            current = copy.deepcopy(document["actions"])
            current[0]["target"][field] = replacement
            with self.assertRaises(ValueError):
                evidence_for_actions(document, current)
        for field, replacement in (("affected_thread_ids", ["target"]), ("rollout_paths", [])):
            current = copy.deepcopy(document["actions"])
            current[0]["impact"][field] = replacement
            with self.assertRaises(ValueError):
                evidence_for_actions(document, current)
        current = copy.deepcopy(document["actions"])
        current[0]["kind"] = "delete_native_project"
        with self.assertRaises(ValueError):
            evidence_for_actions(document, current)

    def test_child_v2_unknown_occupies_shared_root_but_other_home_can_continue(self):
        document = self.proof_document()
        action = document["actions"][0]
        store = write_journal(self.home, "orca-startup-unknown", basic_action(self.home))
        child = {"schema_version": "larj.child-operation-plan.v2", "operation_id": store.operation_id,
                 "parent_operation_id": "parent", "target": {"codex_home": str(self.home), "storage_id": "fixed-store"},
                 "mutation_family": "delete_conversation", "actions": [action],
                 "startup_boundary": {"schema_version": "larj.orca-startup-boundary.v1", "coordination_scope": "root_wide",
                     "home": str(self.home), "target_safety_evidence": document["target_safety_evidence"]}}
        child["plan_sha256"] = plan_sha256(child)
        # Create a fresh genuine v2 journal, preserving the old v1 helper's
        # existing scope behavior in its own regression tests.
        from local_agent_record_janitor.operation_store import OperationStore
        new = OperationStore(self.home, "orca-v2-unknown")
        new.accept_plan(child | {"operation_id": new.operation_id,
            "plan_sha256": plan_sha256(child | {"operation_id": new.operation_id})})
        plan = new.read_plan()
        new.write_state({"schema_version": "larj.agent-state.v1", "operation_id": new.operation_id,
            "plan_sha256": plan["plan_sha256"], "phase": "recovery_required", "goal_status": "unknown",
            "goal_satisfied": False, "modified": False, "mutation_started": True,
            "current_action_state": "mutation_started", "current_action_ids": [], "next_event_sequence": 1})
        new.append_event({"event": "mutation_started"})
        self.assertIsNone(scopes_for_frozen_plan(plan)[0].target_ids)
        with self.assertRaises(UnknownMutationError):
            with mutation_guard((MutationScope(self.home, frozenset(("independent-target",))),)):
                self.fail("shared startup unknown must occupy the home")
        independent = self.home / "another-home"
        independent.mkdir()
        with mutation_guard((MutationScope(independent, frozenset(("independent-target",))),)):
            pass

    def test_fixed_schema_fixture_passes_and_schema_or_migration_change_rejects(self):
        create_native_schema(self.home)
        from contextlib import closing
        import sqlite3
        for name in ("state_5.sqlite", "logs_2.sqlite", "memories_1.sqlite", "queue_1.sqlite", "goals_1.sqlite"):
            self.assertTrue(_schema_metadata(self.home / name))
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("UPDATE _sqlx_migrations SET success=0 WHERE version=58")
            connection.commit()
        with self.assertRaises(ValueError):
            _schema_metadata(self.home / "state_5.sqlite")

    def test_missing_or_incomplete_native_backfill_is_not_startup_eligible(self):
        from contextlib import closing
        import sqlite3
        from local_agent_record_janitor.orca_target_safety import _startup_control
        create_native_schema(self.home)
        self.assertEqual(_startup_control(self.home)["backfill_state"][1], "complete")
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("UPDATE backfill_state SET status='pending'")
            connection.commit()
            with self.assertRaises(ValueError):
                _startup_control(self.home)
            connection.execute("DELETE FROM backfill_state")
            connection.commit()
            with self.assertRaises(ValueError):
                _startup_control(self.home)

    def test_deferred_close_error_and_unknown_stop_cannot_publish_deleted(self):
        from tests.test_cleaner import CleanupExecutionTests, FakeAppServer
        from local_agent_record_janitor.cleaner import clean_findings, VerificationResult
        fixture = CleanupExecutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        class CloseError(FakeAppServer):
            def __exit__(self, *_args):
                raise OSError("synthetic close failure")
        phases = []
        first, second = fixture.finding("first"), fixture.finding("second")
        for server, result in ((CloseError(), VerificationResult(deleted=True)),
                               (FakeAppServer(), VerificationResult(deleted=False, status="unknown"))):
            phases.clear()
            report = clean_findings([first, second], app_server_factory=lambda **_: server,
                binary_resolver=lambda _: Path("codex"), verifier=lambda _: result,
                verification_attempts=1, verification_interval=0, defer_verification_until_close=True,
                action_state_callback=lambda phase, *_: phases.append(phase))
            self.assertTrue(all(value.status == "unknown" for value in report.results))
            if isinstance(server, CloseError):
                self.assertNotIn("verified", phases)
            else:
                self.assertEqual(server.deleted_thread_ids, ["first"])


if __name__ == "__main__":
    unittest.main()
