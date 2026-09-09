import copy
import json
import os
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
from local_agent_record_janitor.native_project_cleanup import (
    NativeProjectError, STATE_FILES, discover_native_projects,
    execute_native_project_cleanup, remaining_native_project_markers,
    verify_native_project_recovery,
)
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from tests.support import create_thread_index


class NativeProjectCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "native"
        self.home.mkdir()
        create_thread_index(self.home, [])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as c:
            c.execute("ALTER TABLE threads ADD COLUMN cwd TEXT")
            c.commit()
        self.root = Path(self.temp.name) / "missing-project"
        self.project_id = "g-p-test-vegetables"
        self.host = "local:" + str(self.home)
        self.data = {
            "local-projects": {
                self.project_id: {"id": self.project_id, "name": "蔬菜", "rootPaths": [str(self.root)], "createdAt": 1, "updatedAt": 2},
                "keep": {"id": "keep", "name": "Keep", "rootPaths": [str(self.home)]},
            },
            "electron-persisted-atom-state": {"sidebar-project-expanded-v1-chatgpt:" + self.project_id: True, "unrelated": {"prompt": "PRIVATE BODY"}},
            "app-server-project-id-by-legacy-project-id-by-host": {self.host: {self.project_id: "mapped-native-id", "keep": "keep-mapping"}},
            "other": [1, {"keep": False}],
        }
        self.write()

    def write(self):
        for name in STATE_FILES:
            (self.home / name).write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")

    def evidence(self):
        return next(p.evidence for p in discover_native_projects(self.home) if p.project_id == self.project_id)

    def run_cleanup(self, evidence=None, **kwargs):
        return execute_native_project_cleanup((evidence or self.evidence(),), client_inspector=lambda _: (), **kwargs)

    def bytes(self):
        return [(self.home / n).read_bytes() for n in STATE_FILES]

    def test_removes_all_approved_keys_from_both_copies_only(self):
        phases = []
        evidence = self.evidence()
        self.assertNotIn("PRIVATE BODY", json.dumps(evidence))
        result = self.run_cleanup(evidence, phase_callback=phases.append)
        self.assertEqual(result.entries_removed, 6)
        self.assertEqual(phases, ["guard_started", "mutation_started", "verified"])
        expected = copy.deepcopy(self.data)
        del expected["local-projects"][self.project_id]
        del expected["electron-persisted-atom-state"]["sidebar-project-expanded-v1-chatgpt:" + self.project_id]
        del expected["app-server-project-id-by-legacy-project-id-by-host"][self.host][self.project_id]
        for name in STATE_FILES:
            self.assertEqual(json.loads((self.home / name).read_bytes()), expected)
        self.assertFalse(list(self.home.glob(".larj-project-*")))
        self.assertEqual(remaining_native_project_markers(self.home, (self.project_id,)), ())

    def test_changed_backup_and_new_backup_block(self):
        evidence = self.evidence()
        (self.home / STATE_FILES[1]).write_text("{}", encoding="utf-8")
        before = self.bytes()
        with self.assertRaises(NativeProjectError):
            self.run_cleanup(evidence)
        self.assertEqual(self.bytes(), before)
        (self.home / STATE_FILES[1]).unlink()
        evidence = self.evidence()
        self.write()
        with self.assertRaises(NativeProjectError):
            self.run_cleanup(evidence)

    def test_live_directory_and_unknown_reference_block(self):
        evidence = self.evidence()
        self.root.mkdir()
        with self.assertRaises(NativeProjectError):
            self.run_cleanup(evidence)
        self.root.rmdir()
        self.data["unknown-ref"] = self.project_id
        self.write()
        self.assertTrue(next(p for p in discover_native_projects(self.home) if p.project_id == self.project_id).blockers)
        with self.assertRaises(NativeProjectError):
            self.run_cleanup()

    def test_existing_native_project_blocks(self):
        evidence = self.evidence()
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as c:
            c.execute("CREATE TABLE projects (id TEXT)")
            c.execute("INSERT INTO projects VALUES ('mapped-native-id')")
            c.commit()
        with self.assertRaises(NativeProjectError):
            self.run_cleanup(evidence)

    def test_running_client_blocks_without_writes(self):
        before = self.bytes()
        with self.assertRaises(NativeProjectError):
            execute_native_project_cleanup((self.evidence(),), client_inspector=lambda _: ("ChatGPT.exe",))
        self.assertEqual(self.bytes(), before)

    def test_second_replace_failure_restores_both_files(self):
        before = self.bytes()
        replace = os.replace
        calls = 0

        def fail_once(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected replacement failure")
            return replace(source, destination)

        with patch("local_agent_record_janitor.native_project_cleanup.os.replace", side_effect=fail_once):
            with self.assertRaises(NativeProjectError) as raised:
                self.run_cleanup()
        self.assertTrue(raised.exception.outcome_known_rolled_back)
        self.assertEqual(self.bytes(), before)
        self.assertFalse(list(self.home.glob(".larj-project-*")))

    def test_concurrent_change_after_write_retains_recovery_evidence(self):
        evidence = self.evidence()
        replace = os.replace
        calls = 0

        def change_after_write(source, destination):
            nonlocal calls
            calls += 1
            result = replace(source, destination)
            if calls == 1:
                Path(destination).write_text('{"external-change":true}', encoding="utf-8")
            return result

        with patch("local_agent_record_janitor.native_project_cleanup.os.replace", side_effect=change_after_write):
            with self.assertRaises(NativeProjectError) as raised:
                self.run_cleanup()
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertTrue(list(self.home.glob(".larj-project-rollback-*")))
        with self.assertRaises(NativeProjectError):
            verify_native_project_recovery(self.home, (evidence,))

    def test_ancestor_link_is_not_proof_of_missing_local_directory(self):
        # Mock only lstat's reparse attribute, avoiding privileged symlink setup.
        original = Path.lstat
        from types import SimpleNamespace

        def linked(path):
            result = original(path)
            if path == self.root.parent:
                return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
            return result

        with patch.object(Path, "lstat", linked):
            item = next(p for p in discover_native_projects(self.home) if p.project_id == self.project_id)
        self.assertTrue(item.blockers)

    def test_cross_copy_mapped_reference_is_inventory_only(self):
        backup = copy.deepcopy(self.data)
        del backup["app-server-project-id-by-legacy-project-id-by-host"][self.host][self.project_id]
        backup["unrecognized"] = "mapped-native-id"
        (self.home / STATE_FILES[1]).write_text(json.dumps(backup))
        with self.assertRaises(NativeProjectError):
            self.run_cleanup()

    def test_all_projects_excludes_live_environment_registration(self):
        adapter = NativeIntegrityAdapter(codex_home=self.home)
        plan = OperationCoordinator(CleanupService()).plan_operation(
            client="native", all_projects=True, adapters=(adapter,),
            plan_path=Path(self.temp.name) / "all.json")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual(plan["counts"]["action_count"], 1)

    def test_ambiguous_environment_name_blocks_and_exact_id_succeeds(self):
        self.data["local-projects"]["another"] = {"id": "another", "name": "蔬菜", "rootPaths": [str(self.root / "another")]}
        self.write()
        adapter = NativeIntegrityAdapter(codex_home=self.home)
        plan = OperationCoordinator(CleanupService()).plan_operation(
            client="native", projects=("蔬菜",), adapters=(adapter,),
            plan_path=Path(self.temp.name) / "ambiguous.json")
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertIn("ambiguous_project", [b["blocker_code"] for b in plan["blockers"]])

    def test_unknown_operation_never_replays_and_verify_preserves_bad_state(self):
        adapter = NativeIntegrityAdapter(codex_home=self.home)
        coordinator = OperationCoordinator(CleanupService())
        plan_path = Path(self.temp.name) / "unknown.json"
        plan = coordinator.plan_operation(client="native", record_ids=(self.project_id,), adapters=(adapter,), plan_path=plan_path)
        replace = os.replace
        calls = 0

        def corrupt_global_state(source, destination):
            nonlocal calls
            result = replace(source, destination)
            if Path(destination) == self.home / STATE_FILES[0] and not calls:
                calls += 1
                Path(destination).write_text("{}")
            return result

        with patch("local_agent_record_janitor.native_project_cleanup.running_related_clients", return_value=()), patch(
            "local_agent_record_janitor.native_project_cleanup.os.replace", side_effect=corrupt_global_state
        ):
            result = coordinator.apply_operation(operation_id=plan["operation_id"], plan_path=plan_path, clients_closed=True, adapters=(adapter,))
        self.assertEqual(result["goal_status"], "unknown", result)
        with patch("local_agent_record_janitor.execution.execute_native_project_cleanup", side_effect=AssertionError("must not replay")):
            repeated = coordinator.apply_operation(operation_id=plan["operation_id"], plan_path=plan_path, clients_closed=True, adapters=(adapter,))
        self.assertNotEqual(repeated["goal_status"], "complete", repeated)
        verify = coordinator.verify_operation(operation_id=plan["operation_id"], plan_path=plan_path, adapters=(adapter,))
        self.assertEqual(verify["goal_status"], "unknown", verify)

    def test_verify_checks_loose_markers_and_missing_frozen_files(self):
        adapter = NativeIntegrityAdapter(codex_home=self.home)
        coordinator = OperationCoordinator(CleanupService())
        plan = coordinator.plan_operation(client="native", record_ids=(self.project_id,), adapters=(adapter,), plan_path=Path(self.temp.name) / "verify.json")
        self.run_cleanup()
        data = json.loads((self.home / STATE_FILES[0]).read_bytes())
        data["loose-reference"] = self.project_id
        (self.home / STATE_FILES[0]).write_text(json.dumps(data))
        context = coordinator._build_context("native", (adapter,))[0]
        self.assertEqual(coordinator._residual_action_ids(plan, context), [plan["actions"][0]["action_id"]])
        (self.home / STATE_FILES[1]).unlink()
        with self.assertRaises(NativeProjectError):
            coordinator._residual_action_ids(plan, context)

    def test_duplicate_json_keys_are_rejected(self):
        (self.home / STATE_FILES[0]).write_text('{"local-projects":{},"local-projects":{}}')
        with self.assertRaises(NativeProjectError):
            discover_native_projects(self.home)

    def test_recovery_verifies_exact_after_state_then_removes_temporary_copies(self):
        evidence = self.evidence()
        replace = os.replace
        calls = 0

        def fail_rollback(source, destination):
            nonlocal calls
            calls += 1
            if calls > 2:
                raise OSError("injected rollback failure")
            return replace(source, destination)

        with patch("local_agent_record_janitor.native_project_cleanup.os.replace", side_effect=fail_rollback), patch(
            "local_agent_record_janitor.native_project_cleanup.remaining_native_project_markers", side_effect=OSError("verification interrupted")
        ):
            with self.assertRaises(NativeProjectError) as raised:
                self.run_cleanup(evidence)
        self.assertTrue(raised.exception.outcome_unknown)
        before_verify = self.bytes()
        self.assertTrue(list(self.home.glob(".larj-project-recovery-*")))
        verify_native_project_recovery(self.home, (evidence,))
        self.assertEqual(self.bytes(), before_verify)
        self.assertFalse(list(self.home.glob(".larj-project-*")))

    def test_multi_project_batch_removes_only_frozen_projects(self):
        second_id = "another-project"
        self.data["local-projects"][second_id] = {"id": second_id, "name": "Second", "rootPaths": [str(Path(self.temp.name) / "second-missing")]}
        self.write()
        projects = [p for p in discover_native_projects(self.home) if not p.blockers]
        self.assertEqual(len(projects), 2)
        result = execute_native_project_cleanup(tuple(p.evidence for p in projects), client_inspector=lambda _: ())
        self.assertEqual(result.entries_removed, 8)
        for name in STATE_FILES:
            self.assertEqual(set(json.loads((self.home / name).read_bytes())["local-projects"]), {"keep"})

    def test_direct_inventory_exposes_stale_registration(self):
        from local_agent_record_janitor.client_inventory import build_client_inventory
        adapter = NativeIntegrityAdapter(codex_home=self.home)
        inventory = build_client_inventory((adapter,), client="native")
        self.assertEqual(len(inventory.project_items), 2)
        self.assertTrue(any(t.record_id == self.project_id for t in inventory.targets))

    def test_backup_only_registration_is_discovered(self):
        (self.home / STATE_FILES[0]).write_text("{}")
        result = self.run_cleanup()
        self.assertEqual(result.entries_removed, 3)

    def test_records_and_high_level_plan_apply_verify(self):
        adapter = NativeIntegrityAdapter(codex_home=self.home)
        output = StringIO()
        exit_code = main(("records", "--client", "native", "--json"), adapters=(adapter,), stdout=output, stderr=StringIO())
        self.assertEqual(exit_code, 0, output.getvalue())
        self.assertIn(self.project_id, output.getvalue())
        self.assertNotIn("PRIVATE BODY", output.getvalue())
        coordinator = OperationCoordinator(CleanupService())
        plan_path = Path(self.temp.name) / "plan.json"
        plan = coordinator.plan_operation(client="native", record_ids=(self.project_id,), adapters=(adapter,), plan_path=plan_path)
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual(plan["counts"]["action_count"], 1)
        self.assertEqual(plan["child_batches"][0]["mutation_family"], "delete_native_project")
        with patch("local_agent_record_janitor.native_project_cleanup.running_related_clients", return_value=()):
            result = coordinator.apply_operation(operation_id=plan["operation_id"], plan_path=plan_path, clients_closed=True, adapters=(adapter,))
        self.assertEqual(result["goal_status"], "complete", result)
        verify = OperationCoordinator(CleanupService()).verify_operation(operation_id=plan["operation_id"], plan_path=plan_path, codex_home=self.home, adapters=(adapter,))
        self.assertEqual(verify["goal_status"], "complete", verify)


if __name__ == "__main__":
    unittest.main()
