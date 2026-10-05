import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.herdr_bound_adapter import HerdrBoundAdapter, SCHEMA
from local_agent_record_janitor import herdr_cleanup
from tests.herdr_support import CODEX_ID, agent_session, pane, snapshot, tab, write_snapshot, recovery_name
from tests.orca_native_support import create_native_schema, add_native_record


@unittest.skipUnless(os.name == "nt", "Qualified held Herdr lifecycle is Windows-only")
class HerdrFullflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.home = self.base / "herdr", self.base / "codex"
        self.root.mkdir(); self.home.mkdir()
        create_native_schema(self.home)
        self.binary = self.base / "synthetic-herdr.exe"
        self.binary.write_bytes(b"never executed: synthetic startup gate identity")
        self.manifest = {"schema_version": SCHEMA, "profile_root": str(self.root),
            "runtime_binaries": [str(self.binary)], "native_stores": [
                {"session": "default", "engine": "codex", "root": str(self.home)}]}
        value = snapshot(self.base / "project", (tab({0: pane(self.base, agent_session(), private="ERASE"),
            1: pane(self.base, agent_session(value="33333333-3333-4333-8333-333333333333"), private="KEEP")}),))
        write_snapshot(self.root / "session.json", value)
        write_snapshot(self.root / "session-backups" / recovery_name(), value)
        self.adapter = HerdrBoundAdapter(self.manifest)
        self.plan_path = self.base / "plan.json"
        self.coordinator = self.coordinator_new()
        for item in (patch("local_agent_record_janitor.office_runtime.processes", return_value=[]),
                     patch.dict(os.environ, {"APPDATA": str(self.base / "appdata"), "LOCALAPPDATA": str(self.base / "localappdata")})):
            item.start(); self.addCleanup(item.stop)

    def coordinator_new(self):
        return OperationCoordinator(CleanupService(client_inspector=lambda *_args, **_kwargs: ()))

    def plan(self):
        result = self.coordinator.plan_operation(client="herdr", record_ids=(CODEX_ID,), engines=("codex",),
            adapters=(self.adapter,), plan_path=self.plan_path, operation_id="herdr-fullflow")
        self.assertEqual(result.get("goal_status"), "ready", result)
        return result

    def test_frontend_only_cold_apply_and_verify(self):
        plan = self.plan()
        self.assertEqual([a["kind"] for a in plan["actions"]], ["delete_herdr_frontend"])
        args = dict(operation_id=plan["operation_id"], plan_path=self.plan_path, plan_sha256=plan["plan_sha256"])
        result = self.coordinator_new().apply_operation(**args, clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        for relative in ("session.json", "session-backups/" + recovery_name()):
            body = (self.root / relative).read_text()
            self.assertNotIn("ERASE", body)
            self.assertIn("KEEP", body)
        verified = self.coordinator_new().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(verified["goal_status"], "complete", verified)

    def test_native_plan_requires_exact_restore_ticket(self):
        add_native_record(self.home, CODEX_ID)
        plan = self.plan()
        self.assertEqual([a["kind"] for a in plan["actions"]], ["delete_conversation", "delete_herdr_frontend"])
        evidence = herdr_cleanup.evidence_from_document(plan)
        reference = next(r for r in self.adapter.snapshot_references().references if r.native_id == CODEX_ID)
        self.assertFalse(herdr_cleanup.permits_reference(reference))
        from dataclasses import replace
        with herdr_cleanup.planning_scope(evidence):
            self.assertTrue(herdr_cleanup.permits_reference(reference))
            self.assertFalse(herdr_cleanup.permits_reference(replace(reference, binding_key="other")))
            self.assertFalse(herdr_cleanup.permits_reference(reference, execution=True))
        with self.assertRaisesRegex(Exception, "execution_ticket_required"):
            herdr_cleanup.execute(evidence, phase_callback=lambda _: self.fail("no standalone writer"))

    def test_native_actions_cannot_grant_their_own_scope(self):
        add_native_record(self.home, CODEX_ID)
        evidence = herdr_cleanup.evidence_from_document(self.plan())
        extra = copy.deepcopy(evidence["native_actions"][0])
        other = "44444444-4444-4444-8444-444444444444"
        extra.update(action_id="unowned", thread_id=other, affected_thread_ids=[other])
        evidence["native_actions"].append(extra)
        evidence["native_targets"][0]["action_ids"].append("unowned")
        evidence["native_targets"][0]["ids"].append(other)
        with self.assertRaisesRegex(herdr_cleanup.codec.HerdrCleanupError, "native_action_owner_unproven"):
            herdr_cleanup.validate(evidence)

    def test_deleted_pi_path_retains_approved_short_alias(self):
        import ctypes
        from local_agent_record_janitor.pi_sessions import _normalized_path
        from local_agent_record_janitor.record_identity import canonical_path
        directory = self.base / "Long Pi Directory With An Approved Alias"
        directory.mkdir()
        transcript = directory / "selected.jsonl"
        transcript.write_text("synthetic alias identity", encoding="utf-8")
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.windll.kernel32.GetShortPathNameW(str(directory.resolve(strict=True)), buffer, len(buffer))
        if not size or size >= len(buffer):
            self.skipTest("Temporary volume does not expose a Windows short-path alias")
        alias = Path(buffer.value) / transcript.name
        canonical = canonical_path(transcript)
        writer_path = _normalized_path(alias)
        if writer_path == _normalized_path(Path(canonical)):
            self.skipTest("Temporary volume does not expose a distinct Windows short-path alias")
        self.assertTrue(os.path.samefile(alias, transcript))
        reference = {"native_record": {"path": str(alias).upper(), "canonical_path": canonical}}
        self.assertTrue(herdr_cleanup.pi_reference_matches_action(reference, writer_path))
        transcript.unlink()
        self.assertTrue(herdr_cleanup.pi_reference_matches_action(reference, writer_path))
        self.assertTrue(herdr_cleanup.pi_reference_matches_action(reference, _normalized_path(Path(canonical))))
        self.assertFalse(herdr_cleanup.pi_reference_matches_action(reference, _normalized_path(directory / "other.jsonl")))
        self.assertFalse(herdr_cleanup.pi_reference_matches_action(reference, _normalized_path(self.base / transcript.name)))

    def test_same_process_retains_other_clients_shared_native_guards(self):
        import sqlite3
        from contextlib import closing
        from local_agent_record_janitor.client_contracts import describe_adapter
        from local_agent_record_janitor.targeted_guard import TargetedReferenceGuard, TargetedGuardError
        profile = self.base / "appdata" / "CindyGlobal"
        profile.mkdir(parents=True)
        home = profile / "codex-home"
        home.mkdir()
        create_native_schema(home)
        add_native_record(home, CODEX_ID)
        with closing(sqlite3.connect(profile / "cindy-local-v1.db")) as db:
            db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, sdk_session_id TEXT, status TEXT, agent_kind TEXT)")
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", ("other-client", CODEX_ID, "active", "codex"))
            db.commit()
        self.manifest["native_stores"][0]["root"] = str(home)
        adapter = HerdrBoundAdapter(self.manifest)
        guards = self.coordinator._with_current_guard_sources((adapter,), "herdr")
        self.assertIn("cindy", {describe_adapter(a).client for a in guards})
        # The union itself does not grant the selected client authority over
        # another owner's reference, even with an otherwise valid closure.
        with self.assertRaises(TargetedGuardError):
            TargetedReferenceGuard(guards, {})._check_home(home, frozenset({CODEX_ID}))

    def test_absent_claude_projects_is_not_a_native_residual(self):
        root = self.base / "empty-claude"
        root.mkdir()
        binding = {"engine": "claude", "root": str(root), "session": "default"}
        target = {"engine": "claude", "root": str(root), "ids": [CODEX_ID], "paths": [], "binding": binding}
        self.assertFalse(herdr_cleanup.native_remaining(target))

    def test_new_recovery_copy_blocks_before_any_write(self):
        plan = self.plan()
        before = (self.root / "session.json").read_bytes()
        (self.root / "session-backups" / recovery_name(7)).write_bytes(before)
        result = self.coordinator_new().apply_operation(operation_id=plan["operation_id"], plan_path=self.plan_path,
            plan_sha256=plan["plan_sha256"], clients_closed=True)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["modified"])
        self.assertEqual((self.root / "session.json").read_bytes(), before)

    def test_held_runtime_conflict_blocks_before_native_or_frontend_mutation(self):
        plan = self.plan()
        with self.binary.open("rb"):
            result = self.coordinator_new().apply_operation(operation_id=plan["operation_id"], plan_path=self.plan_path,
                plan_sha256=plan["plan_sha256"], clients_closed=True)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["modified"])
        self.assertFalse(result["mutation_started"])

    def test_frontend_interruption_is_not_retried_but_exact_after_can_verify(self):
        from local_agent_record_janitor.herdr_lifecycle import Boundary
        plan = self.plan()
        original = Boundary.apply
        def interrupt(boundary, **kwargs):
            original(boundary, **kwargs)
            raise OSError("synthetic failure after last durable file write")
        args = dict(operation_id=plan["operation_id"], plan_path=self.plan_path, plan_sha256=plan["plan_sha256"])
        with patch.object(Boundary, "apply", interrupt):
            first = self.coordinator_new().apply_operation(**args, clients_closed=True)
        self.assertEqual(first["goal_status"], "unknown", first)
        with patch.object(Boundary, "apply", side_effect=AssertionError("must not repeat a started child")):
            again = self.coordinator_new().apply_operation(**args, clients_closed=True)
            self.assertEqual(again["goal_status"], "unknown", again)
            verified = self.coordinator_new().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(verified["goal_status"], "complete", verified)

    def test_native_target_and_file_gate_scope_cannot_be_substituted(self):
        plan = self.plan()
        evidence = herdr_cleanup.evidence_from_document(plan)
        changed = copy.deepcopy(evidence)
        changed["native_targets"][0]["root"] = str(self.base / "outside")
        with self.assertRaisesRegex(Exception, "native_target_binding_changed"):
            with herdr_cleanup.planning_scope(changed):
                self.fail("forged root must not create a ticket")
        changed = copy.deepcopy(evidence)
        changed["lifecycle"]["binaries"] = []
        changed["lifecycle"]["pipe_names"] = []
        from local_agent_record_janitor.herdr_lifecycle import hold
        with self.assertRaisesRegex(Exception, "held_lifecycle_evidence_changed"):
            with hold(changed, census=lambda: []):
                self.fail("omitted gates must not establish a boundary")

    def test_pi_and_claude_use_real_typed_writers_then_remove_frontend(self):
        pi = self.base / "pi"
        sessions = pi / "sessions"
        sessions.mkdir(parents=True)
        transcript = sessions / "selected.jsonl"
        transcript.write_text(json.dumps({"type": "session", "id": "pi-selected", "version": 3,
            "timestamp": "2026-10-01T00:00:00Z", "cwd": str(self.base)}) + "\n", encoding="utf-8")
        keeper = sessions / "keeper.jsonl"
        keeper.write_text(json.dumps({"type": "session", "id": "pi-keeper", "version": 3,
            "timestamp": "2026-10-01T00:00:00Z", "cwd": str(self.base)}) + "\n", encoding="utf-8")
        claude = self.base / "claude"
        cc = claude / "projects" / "synthetic" / (CODEX_ID + ".jsonl")
        cc.parent.mkdir(parents=True)
        cc.write_text(json.dumps({"sessionId": CODEX_ID, "message": "SYNTHETIC PRIVATE"}) + "\n", encoding="utf-8")
        manifest = {**self.manifest, "native_stores": [
            {"session": "default", "engine": "pi", "root": str(sessions), "agent_dir": str(pi)},
            {"session": "default", "engine": "claude", "root": str(claude)}]}
        # This alias has physical proof while present; its exact frozen raw
        # spelling must survive native deletion and a cold continuation.
        pi_locator = str(transcript).upper()
        value = snapshot(self.base, (tab({0: pane(self.base, agent_session("pi", pi_locator, "path")),
            1: pane(self.base, agent_session("claude", CODEX_ID)),
            2: pane(self.base, agent_session("pi", str(keeper), "path"))}),))
        for path in (self.root / "session.json", self.root / "session-backups" / recovery_name()):
            write_snapshot(path, value)
        plan = self.coordinator.plan_operation(client="herdr", record_ids=(pi_locator, CODEX_ID),
            adapters=(HerdrBoundAdapter(manifest),), plan_path=self.plan_path, operation_id="herdr-mixed")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual({a["kind"] for a in plan["actions"]}, {"delete_pi_session", "delete_claude_session", "delete_herdr_frontend"})
        args = dict(operation_id=plan["operation_id"], plan_path=self.plan_path, plan_sha256=plan["plan_sha256"])
        with patch.object(herdr_cleanup, "execute", side_effect=RuntimeError("synthetic pre-mutation stop")):
            first = self.coordinator_new().apply_operation(**args, clients_closed=True)
        self.assertFalse(transcript.exists(), first)
        self.assertFalse(cc.exists(), first)
        result = self.coordinator_new().apply_operation(**args, clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertFalse(transcript.exists())
        self.assertFalse(cc.exists())
        self.assertTrue(keeper.exists())
        verified = self.coordinator_new().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(verified["goal_status"], "complete", verified)
