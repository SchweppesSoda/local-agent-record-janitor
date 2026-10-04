import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor import paseo_cleanup, paseo_cleanup_files as files
from local_agent_record_janitor.paseo_bound_adapter import PaseoBoundAdapter, SCHEMA
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from tests.orca_native_support import create_native_schema, add_native_record

AGENT = "11111111-1111-4111-8111-111111111111"
KEEP = "22222222-2222-4222-8222-222222222222"
NATIVE = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"


def agent(identifier, native, cwd):
    return {"id": identifier, "provider": "codex", "cwd": str(cwd), "createdAt": "2026-10-05T00:00:00Z",
        "updatedAt": "2026-10-05T00:00:00Z", "labels": {}, "lastStatus": "closed", "config": {},
        "persistence": {"provider": "codex", "sessionId": native, "nativeHandle": native,
                        "metadata": {"cwd": str(cwd), "asyncQuestions": {"private": "PRIVATE_METADATA"}}},
        "runtimeInfo": {"provider": "codex", "sessionId": native}, "title": "PRIVATE " + identifier}


def schedule(identifier, *, owned=False):
    return {"id": identifier, "name": "private schedule", "prompt": "private prompt",
        "cadence": {"type": "every", "everyMs": 1000},
        "target": {"type": "agent", "agentId": AGENT if owned else KEEP}, "status": "paused",
        "createdAt": "t", "updatedAt": "t", "nextRunAt": None, "lastRunAt": "t", "pausedAt": "t",
        "expiresAt": None, "maxRuns": 3, "runs": [
            {"id": "run1", "scheduledFor": "slot1", "startedAt": "start1", "endedAt": "end1",
             "status": "succeeded", "agentId": AGENT, "workspaceId": "workspace", "output": "PRIVATE_RESULT", "error": None},
            {"id": "run2", "scheduledFor": "slot2", "startedAt": "start2", "endedAt": "end2",
             "status": "failed", "agentId": KEEP, "output": "KEEP_OUTPUT", "error": "KEEP_ERROR"}]}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(files.encode(value))


class PaseoFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        write(self.root / "agents" / (AGENT + ".json"), agent(AGENT, NATIVE, self.root))
        write(self.root / "agents" / (KEEP + ".json"), agent(KEEP, OTHER, self.root))

    def test_all_snapshots_atomic_copies_and_run_ledger(self):
        raw = (self.root / "agents" / (AGENT + ".json")).read_bytes()
        write(self.root / "agents/project/old.json", agent(AGENT, NATIVE, self.root))
        pending = self.root / "agents" / ("." + AGENT + ".json.424242.123." + KEEP + ".tmp")
        pending.write_bytes(raw)
        owned = schedule("owned", owned=True)
        owned["runs"][1]["agentId"] = AGENT
        write(self.root / "schedules/owned.json", owned)
        kept = schedule("kept")
        write(self.root / "schedules/kept.json", kept)
        keeper = (self.root / "agents" / (KEEP + ".json")).read_bytes()
        evidence = files.freeze(self.root, [AGENT])
        files.apply(evidence, phase_callback=lambda _: None)
        self.assertEqual(files.remaining(evidence), 0)
        self.assertFalse(pending.exists())
        self.assertFalse((self.root / "agents/project/old.json").exists())
        self.assertFalse((self.root / "schedules/owned.json").exists())
        self.assertEqual((self.root / "agents" / (KEEP + ".json")).read_bytes(), keeper)
        kept["runs"][0].update(agentId=None, output=None, error=None)
        self.assertEqual(json.loads((self.root / "schedules/kept.json").read_bytes()), kept)

    def test_running_run_and_new_atomic_copy_block(self):
        value = schedule("running")
        value["runs"][0]["status"] = "running"
        write(self.root / "schedules/running.json", value)
        with self.assertRaisesRegex(ValueError, "schedule_run_not_finished"):
            files.freeze(self.root, [AGENT])
        value["runs"][0]["status"] = "failed"
        write(self.root / "schedules/running.json", value)
        evidence = files.freeze(self.root, [AGENT])
        write(self.root / "agents/project/new.json", agent(AGENT, NATIVE, self.root))
        with self.assertRaisesRegex(ValueError, "file_closure_changed"):
            files.apply(evidence, phase_callback=lambda _: self.fail("must block before mutation"))

    def test_owned_schedule_with_foreign_history_is_not_deleted(self):
        write(self.root / "schedules/shared.json", schedule("shared", owned=True))
        with self.assertRaisesRegex(ValueError, "schedule_has_unselected_runs"):
            files.freeze(self.root, [AGENT])

    def test_partial_file_apply_resumes_without_repeating_completed_deletes(self):
        write(self.root / "agents/project/copy.json", agent(AGENT, NATIVE, self.root))
        write(self.root / "schedules/kept.json", schedule("kept"))
        evidence = files.freeze(self.root, [AGENT])
        original = files.frozen_files._delete_frozen
        calls = []
        def interrupted(root, before):
            calls.append(before["path"])
            if len(calls) == 2:
                raise OSError("synthetic stop")
            return original(root, before)
        with patch.object(files.frozen_files, "_delete_frozen", side_effect=interrupted):
            with self.assertRaises(OSError):
                files.apply(evidence, phase_callback=lambda _: None)
        self.assertGreater(files.remaining(evidence), 0)
        completed = calls[0]
        def resume(root, before):
            self.assertNotEqual(before["path"], completed)
            return original(root, before)
        with patch.object(files.frozen_files, "_delete_frozen", side_effect=resume):
            files.apply(evidence, phase_callback=lambda _: None)
        self.assertEqual(files.remaining(evidence), 0)

    def test_unknown_nested_schema_and_lossy_json_numbers_block(self):
        for raw in (b'{"x":-0}', b'{"x":-0.0}', b'{"x":1e999}', b'{"x":9007199254740992}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                files.decode(raw)
        base = agent(AGENT, NATIVE, self.root)
        for container in ("owner", "config", "persistence", "runtimeInfo"):
            value = copy.deepcopy(base)
            value.setdefault(container, {"kind": "daemon", "daemonId": "d", "executionId": "e"})["newRestoreField"] = "unknown"
            with self.subTest(container=container), self.assertRaises(ValueError):
                files._agent(value)


@unittest.skipUnless(os.name == "nt", "Qualified Paseo startup lease is Windows-only")
class PaseoFullflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve(strict=True)
        self.root, self.home = self.base / "paseo", self.base / "codex"
        self.root.mkdir(); self.home.mkdir()
        (self.root / "server-id").write_text("srv_test\n", encoding="utf-8")
        create_native_schema(self.home)
        self.binary = self.base / "synthetic-paseo.exe"
        self.binary.write_bytes(b"synthetic identity gate, never executed")
        self.manifest = {"schema_version": SCHEMA, "profile_root": str(self.root), "server_id": "srv_test",
            "runtime_binaries": [str(self.binary)], "desktop_profiles": [], "native_stores": [
                {"agent_id": AGENT, "engine": "codex", "root": str(self.home)},
                {"agent_id": KEEP, "engine": "codex", "root": str(self.home)}]}
        write(self.root / "agents" / (AGENT + ".json"), agent(AGENT, NATIVE, self.base))
        write(self.root / "agents" / (KEEP + ".json"), agent(KEEP, OTHER, self.base))
        self.adapter = PaseoBoundAdapter(self.manifest)
        self.plan_path = self.base / "plan.json"
        for item in (patch("local_agent_record_janitor.office_runtime.processes", return_value=[]),
                     patch.dict(os.environ, {"APPDATA": str(self.base / "appdata"), "LOCALAPPDATA": str(self.base / "localappdata")})):
            item.start(); self.addCleanup(item.stop)

    def coordinator(self):
        return OperationCoordinator(CleanupService(client_inspector=lambda *_args, **_kwargs: ()))

    def plan(self):
        result = self.coordinator().plan_operation(client="paseo", record_ids=(AGENT,), engines=("codex",),
            adapters=(self.adapter,), plan_path=self.plan_path, operation_id="paseo-fullflow")
        self.assertEqual(result.get("goal_status"), "ready", result)
        return result

    def test_frontend_only_cold_apply_and_verify(self):
        plan = self.plan()
        self.assertEqual([a["kind"] for a in plan["actions"]], [paseo_cleanup.KIND])
        result = self.coordinator().apply_operation(operation_id=plan["operation_id"], plan_path=self.plan_path,
            plan_sha256=plan["plan_sha256"], clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertFalse((self.root / "agents" / (AGENT + ".json")).exists())
        self.assertTrue((self.root / "agents" / (KEEP + ".json")).exists())
        self.assertFalse((self.root / "paseo.pid").exists())
        result = self.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(result["goal_status"], "complete", result)

    def test_native_dependency_and_exact_ticket(self):
        add_native_record(self.home, NATIVE)
        plan = self.plan()
        self.assertEqual([a["kind"] for a in plan["actions"]], ["delete_conversation", paseo_cleanup.KIND])
        evidence = paseo_cleanup.evidence_from_document(plan)
        reference = next(r for r in self.adapter.snapshot_references().references if r.frontend_id == AGENT)
        self.assertFalse(paseo_cleanup.permits_reference(reference))
        with paseo_cleanup.planning_scope(evidence):
            self.assertTrue(paseo_cleanup.permits_reference(reference))
            self.assertFalse(paseo_cleanup.permits_reference(reference, execution=True))
        with self.assertRaisesRegex(ValueError, "execution_ticket_required"):
            paseo_cleanup.execute(evidence, phase_callback=lambda _: self.fail("standalone denied"))

    def test_pid_startup_gate_and_profile_binding_drift(self):
        plan = self.plan()
        (self.root / "paseo.pid").write_text('{}', encoding="utf-8")
        result = self.coordinator().apply_operation(operation_id=plan["operation_id"], plan_path=self.plan_path,
            plan_sha256=plan["plan_sha256"], clients_closed=True)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["modified"])
        self.assertTrue((self.root / "agents" / (AGENT + ".json")).exists())

    def test_frozen_scope_must_match_present_server_references(self):
        evidence = paseo_cleanup.evidence_from_document(self.plan())
        snapshot = self.adapter.snapshot_references(refresh=True)
        paseo_cleanup.check_source_references(self.adapter, evidence, snapshot.references)
        for mode in ("drop", "rebind"):
            changed = copy.deepcopy(evidence)
            if mode == "drop":
                changed["references"] = []
            else:
                for reference in changed["references"]:
                    reference["native_id"] = OTHER
                    reference["native_record"]["record_id"] = OTHER
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "source_reference_scope_changed"):
                paseo_cleanup.check_source_references(self.adapter, changed, snapshot.references)

    def test_native_actions_cannot_grant_their_own_scope(self):
        add_native_record(self.home, NATIVE)
        evidence = paseo_cleanup.evidence_from_document(self.plan())
        extra = copy.deepcopy(evidence["native_actions"][0])
        extra.update(action_id="unowned", thread_id=OTHER, affected_thread_ids=[OTHER])
        evidence["native_actions"].append(extra)
        evidence["native_targets"][0]["action_ids"].append("unowned")
        evidence["native_targets"][0]["ids"].append(OTHER)
        with self.assertRaisesRegex(ValueError, "native_action_owner_unproven"):
            paseo_cleanup.validate(evidence)


if __name__ == '__main__':
    unittest.main()
