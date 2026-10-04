from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.adapters import NativeIntegrityAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.mutation_guard import MutationScope, UnknownMutationError, mutation_guard
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.operation_store import OperationStore, plan_sha256
from tests.support import create_thread_index, write_rollout
from tests.test_mutation_guard import write_journal


class ChildRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "native"
        self.home.mkdir()
        self.thread_id = "synthetic-native"
        self.rollout = write_rollout(self.home, self.thread_id, originator="codex_cli_rs")
        create_thread_index(self.home, [{"id": self.thread_id, "rollout_path": str(self.rollout)}])
        self.coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        document = self.coordinator.plan_operation(client="native", record_ids=(self.thread_id,),
            codex_home=self.home, adapters=(NativeIntegrityAdapter(codex_home=self.home),),
            plan_path=self.root / "parent.json")
        self.assertEqual(document["goal_status"], "ready")
        self.action = document["actions"][0]
        (self.root / "parent.json").unlink()
        self.store = write_journal(self.home, "child-recovery-1", self.action, started=False)
        self.store.append_event({"event": "batch_finished", "goal_status": "unknown"},
            state_updates={"phase": "recovery_required", "goal_status": "unknown"})
        self.args = {"operation_id": self.store.operation_id, "codex_home": self.home}

    def remove_native(self):
        self.rollout.unlink()
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("DELETE FROM threads WHERE id=?", (self.thread_id,))
            connection.commit()

    def snapshot_journal(self):
        return {p.name: p.read_bytes() for p in self.store.directory.iterdir() if p.is_file()}

    def verify(self):
        with patch.object(self.coordinator.service, "execute", side_effect=AssertionError("No record writes during recovery")):
            return self.coordinator.verify_operation(**self.args)

    def test_status_and_verify_unattempted_unknown_with_surviving_record(self):
        before = self.snapshot_journal()
        status = self.coordinator.status_operation(**self.args)
        self.assertEqual(status["goal_status"], "unknown")
        self.assertEqual(before, self.snapshot_journal())
        with self.assertRaises(UnknownMutationError):
            with mutation_guard((MutationScope(self.home, frozenset((self.thread_id,))),)):
                self.fail("Unknown target must remain blocked")
        result = self.verify()
        self.assertEqual(result["goal_status"], "completed_with_residuals", result)
        self.assertFalse(result["modified"])
        self.assertFalse(result["mutation_started"])
        self.assertEqual(result["residual_action_ids"], [self.action["action_id"]])
        self.assertEqual(self.store.plan_path.read_bytes(), before["plan.json"])
        self.assertTrue(self.store.events_path.read_bytes().startswith(before["events.jsonl"]))
        self.assertFalse(self.store.receipt_path.exists())
        self.assertTrue(self.rollout.exists())
        with mutation_guard((MutationScope(self.home, frozenset((self.thread_id,))),)):
            pass
        self.assertEqual(self.coordinator.status_operation(**self.args)["goal_status"], "completed_with_residuals")

    def test_absent_record_is_complete_without_rebuilding_parent(self):
        before = self.store.plan_path.read_bytes()
        self.remove_native()
        result = self.verify()
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertTrue(result["goal_satisfied"])
        self.assertEqual(self.store.plan_path.read_bytes(), before)
        self.assertEqual(result["plan_sha256"], self.store.read_plan()["plan_sha256"])
        self.assertFalse((self.root / "parent.json").exists())
        self.assertEqual(self.verify()["goal_status"], "complete")

    def test_attempt_marker_is_preserved(self):
        self.store.append_event({"event": "mutation_started"},
            state_updates={"mutation_started": True, "current_action_state": "mutation_started"})
        result = self.verify()
        self.assertEqual(result["goal_status"], "completed_with_residuals", result)
        self.assertTrue(result["mutation_started"])
        self.assertTrue(self.store.read_state()["mutation_started"])

    def test_existing_apply_lock_is_never_removed(self):
        self.store.lock_path.write_text("existing owner", encoding="utf-8")
        before = self.snapshot_journal()
        result = self.verify()
        self.assertEqual(result["goal_status"], "unknown")
        self.assertEqual(before, self.snapshot_journal())

    def test_tampered_plan_and_truncated_events_remain_unknown(self):
        original = self.store.plan_path.read_bytes()
        plan = json.loads(original)
        plan["actions"] = []
        self.store.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertEqual(self.verify()["goal_status"], "unknown")
        self.store.plan_path.write_bytes(original)
        self.store.events_path.write_bytes(b"")
        before = self.snapshot_journal()
        self.assertEqual(self.verify()["goal_status"], "unknown")
        self.assertEqual(before, self.snapshot_journal())

    def test_incomplete_native_scan_does_not_release_the_journal(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("DROP TABLE threads")
            connection.commit()
        before = self.snapshot_journal()
        result = self.verify()
        self.assertEqual(result["goal_status"], "unknown", result)
        self.assertEqual(before, self.snapshot_journal())

    def test_remaining_legacy_index_and_catalog_free_ui_are_not_success(self):
        self.remove_native()
        legacy = self.home / "session_index.jsonl"
        legacy.write_text(json.dumps({"id": self.thread_id, "thread_name": "metadata"})+"\n", encoding="utf-8")
        self.assertEqual(self.verify()["goal_status"], "completed_with_residuals")
        legacy.unlink()
        sqlite_dir = self.home / "sqlite"
        sqlite_dir.mkdir()
        with closing(sqlite3.connect(sqlite_dir / "codex-dev.db")) as connection:
            connection.execute("CREATE TABLE local_thread_catalog (host_id TEXT, thread_id TEXT, display_title TEXT, missing_candidate INTEGER DEFAULT 0, PRIMARY KEY(host_id, thread_id))")
            connection.execute("CREATE TABLE local_thread_catalog_metadata (id INTEGER PRIMARY KEY, catalog_revision INTEGER)")
            connection.execute("INSERT INTO local_thread_catalog_metadata VALUES (1, 1)")
            connection.commit()
        state = self.home / ".codex-global-state.json"
        state.write_text(json.dumps({"projectless-thread-ids": [self.thread_id]}), encoding="utf-8")
        before = state.read_bytes()
        self.assertEqual(self.verify()["goal_status"], "completed_with_residuals")
        self.assertEqual(state.read_bytes(), before)

    def test_failed_verification_and_conflicting_terminal_state_keep_evidence(self):
        before = self.snapshot_journal()
        with patch.object(self.coordinator, "_build_context", side_effect=OSError("unreadable inventory")):
            self.assertEqual(self.verify()["goal_status"], "unknown")
        self.assertEqual(before, self.snapshot_journal())
        state = self.store.read_state()
        state.update(goal_status="complete", goal_satisfied=True, phase="finished")
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        before = self.snapshot_journal()
        self.assertEqual(self.coordinator.status_operation(**self.args)["goal_status"], "unknown")
        self.assertEqual(before, self.snapshot_journal())

    def test_frontend_evidence_cannot_enter_native_only_recovery(self):
        action = json.loads(json.dumps(self.action))
        action["impact"]["frontend_database_paths"] = [str(self.root / "frontend.sqlite")]
        store = write_journal(self.home, "mixed-child", action)
        result = self.coordinator.verify_operation(operation_id=store.operation_id, codex_home=self.home)
        self.assertEqual(result["goal_status"], "unknown")
        self.assertEqual(store.read_state()["goal_status"], "unknown")

    def test_foreign_or_malformed_footprints_remain_unknown(self):
        overrides = [
            {"rollout_paths": [str(self.home / ".." / "foreign.jsonl")]},
            {"external_action_payload": []},
            {"affected_thread_ids": []},
        ]
        for index, override in enumerate(overrides):
            with self.subTest(override=override):
                action = json.loads(json.dumps(self.action))
                action["impact"].update(override)
                store = write_journal(self.home, f"malformed-child-{index}", action)
                before = store.events_path.read_bytes()
                result = self.coordinator.verify_operation(operation_id=store.operation_id, codex_home=self.home)
                self.assertEqual(result["goal_status"], "unknown")
                self.assertEqual(store.events_path.read_bytes(), before)

    def test_copied_plan_and_conflicting_home_are_rejected(self):
        copied = self.root / "copied-plan.json"
        copied.write_bytes(self.store.plan_path.read_bytes())
        before = self.snapshot_journal()
        result = self.coordinator.verify_operation(**self.args, plan_path=copied)
        self.assertEqual(result["goal_status"], "unknown")
        other = self.root / "other"
        other.mkdir()
        result = self.coordinator.verify_operation(operation_id=self.store.operation_id,
            codex_home=other, plan_path=self.store.plan_path)
        self.assertEqual(result["goal_status"], "unknown")
        self.assertEqual(before, self.snapshot_journal())

    def test_unsupported_family_or_orca_boundary_requires_parent(self):
        original = self.store.read_plan()
        for override in ({"mutation_family": "remove_desktop_state"},
                         {"schema_version": "larj.child-operation-plan.v2"}):
            with self.subTest(override=override):
                changed = {**original, **override}
                changed["plan_sha256"] = plan_sha256(changed)
                state = self.store.read_state()
                events = self.store.read_events()
                self.store.plan_path.write_text(json.dumps(changed), encoding="utf-8")
                state["plan_sha256"] = changed["plan_sha256"]
                self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
                self.store.events_path.write_text("".join(json.dumps({**e, "plan_sha256": changed["plan_sha256"]})+"\n" for e in events), encoding="utf-8")
                before = self.snapshot_journal()
                self.assertEqual(self.verify()["goal_status"], "unknown")
                self.assertEqual(before, self.snapshot_journal())
                self.store.plan_path.write_text(json.dumps(original), encoding="utf-8")
                state["plan_sha256"] = original["plan_sha256"]
                self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
                self.store.events_path.write_text("".join(json.dumps({**e, "plan_sha256": original["plan_sha256"]})+"\n" for e in events), encoding="utf-8")

    def test_child_plan_never_becomes_apply_authorization(self):
        self.remove_native()
        self.assertEqual(self.verify()["goal_status"], "complete")
        result = self.coordinator.apply_operation(**self.args, plan_path=self.store.plan_path,
            clients_closed=True, app_server_factory=lambda **_: self.fail("No replay"))
        self.assertFalse(result["goal_satisfied"])

    def test_operation_cli_accepts_exact_child_id_and_keeps_metadata_only(self):
        output, errors = StringIO(), StringIO()
        code = main(("operation", "verify", "--operation-id", self.store.operation_id,
                     "--codex-home", str(self.home), "--progress", "--json"),
                    stdout=output, stderr=errors, operation_coordinator=self.coordinator)
        self.assertEqual(code, 3)
        result = json.loads(output.getvalue())
        self.assertEqual(result["recovery_scope"], "child_only")
        self.assertNotIn("actions", result)
        self.assertIn('"stage":"inventory"', errors.getvalue())


if __name__ == "__main__":
    unittest.main()
