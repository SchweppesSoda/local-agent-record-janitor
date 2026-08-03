from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_session_janitor.claude_delete import (
    ClaudeDeletePlanError,
    ClaudeDeleteSelectionError,
    build_claude_delete_plan,
    execute_claude_delete,
)
from codex_session_janitor.claude_sessions import build_claude_session_catalog


ONE = "11111111-1111-4111-8111-111111111111"
TWO = "22222222-2222-4222-8222-222222222222"


class ClaudeDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / ".claude"

    def write_session(self, session_id: str, project: str = "project") -> Path:
        transcript = self.config / "projects" / project / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"sessionId": session_id, "message": "private"}) + "\n", encoding="utf-8")
        sidecar = transcript.parent / session_id / "subagents"
        sidecar.mkdir(parents=True)
        (sidecar / "one.jsonl").write_text("private subagent", encoding="utf-8")
        tools = transcript.parent / session_id / "tool-results"
        tools.mkdir()
        (tools / "one.txt").write_text("private result", encoding="utf-8")
        task = self.config / "tasks" / session_id
        task.mkdir(parents=True, exist_ok=True)
        (task / "task.json").write_text("private task", encoding="utf-8")
        return transcript

    def catalog(self):
        return build_claude_session_catalog(config_dir=self.config)

    def test_plan_selection_and_fingerprint_are_backend_specific(self) -> None:
        self.write_session(ONE)
        self.write_session(TWO)
        plan = build_claude_delete_plan(self.catalog())
        self.assertTrue(all(action.available for action in plan.actions))
        with self.assertRaises(ClaudeDeleteSelectionError):
            plan.with_selected_actions(("all",))
        with self.assertRaises(ClaudeDeleteSelectionError):
            plan.with_selected_actions((ONE, ONE))
        selected = plan.with_selected_actions((ONE,))
        self.assertTrue(selected.plan_fingerprint.startswith("sha256:"))
        self.assertIn("kind", selected.actions[0].approval_payload())

    def test_exact_delete_removes_all_project_copies_and_owned_aux_but_preserves_shared_bytes(self) -> None:
        first = self.write_session(ONE, "a")
        second = self.write_session(ONE, "b")
        current_debug = self.config / "debug" / f"{ONE}.txt"
        current_debug.parent.mkdir()
        current_debug.write_text("session debug", encoding="utf-8")
        current_todo = self.config / "todos" / f"{ONE}-agent-main_1.json"
        current_todo.parent.mkdir()
        current_todo.write_text("session todo", encoding="utf-8")
        shared = {
            ".credentials.json": b"credentials",
            "settings.json": b"settings",
            "stats-cache.json": b"stats",
            "history.jsonl": b"history",
            "projects/a/CLAUDE.md": b"memory",
            ".claude.json": b"identity",
            "plugins/shared": b"plugin",
            "skills/shared": b"skill",
            "agents/shared": b"agent",
            "commands/shared": b"command",
            f"debug/{TWO}.txt": b"other session debug",
            f"debug/{ONE}.txt.bak": b"similar debug prefix",
            f"todos/{TWO}-agent-main_1.json": b"other session todo",
            f"todos/{ONE}-agent-.json": b"empty token shared",
            f"todos/{ONE}-agent-bad!.json": b"unsafe token shared",
            f"todos/{ONE}-agent-main_1.json.bak": b"todo backup shared",
        }
        for relative, contents in shared.items():
            path = self.config / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        plan = build_claude_delete_plan(self.catalog()).with_selected_actions((ONE,))
        result = execute_claude_delete(
            plan, catalog_builder=self.catalog,
            approved_plan_fingerprint=plan.plan_fingerprint or "", clients_closed=True,
        )
        self.assertEqual(result.results[0].status, "deleted")
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertFalse((first.parent / ONE).exists())
        self.assertFalse((self.config / "tasks" / ONE).exists())
        self.assertFalse(current_debug.exists())
        self.assertFalse(current_todo.exists())
        for relative, contents in shared.items():
            self.assertEqual((self.config / relative).read_bytes(), contents)
        self.assertTrue(result.results[0].preserved_shared_records)

    def test_clients_closed_fingerprint_and_manifest_drift_are_fail_closed(self) -> None:
        transcript = self.write_session(ONE)
        plan = build_claude_delete_plan(self.catalog()).with_selected_actions((ONE,))
        with self.assertRaises(ClaudeDeletePlanError):
            execute_claude_delete(plan, catalog_builder=self.catalog,
                                  approved_plan_fingerprint=plan.plan_fingerprint or "", clients_closed=False)
        with self.assertRaises(ClaudeDeletePlanError):
            execute_claude_delete(plan, catalog_builder=self.catalog,
                                  approved_plan_fingerprint="sha256:wrong", clients_closed=True)
        transcript.write_text(json.dumps({"sessionId": ONE, "message": "changed"}) + "\n", encoding="utf-8")
        with self.assertRaises(ClaudeDeletePlanError):
            execute_claude_delete(plan, catalog_builder=self.catalog,
                                  approved_plan_fingerprint=plan.plan_fingerprint or "", clients_closed=True)
        self.assertTrue(transcript.exists())

    def test_partial_failure_is_unknown_and_reports_remaining_paths(self) -> None:
        self.write_session(ONE)
        plan = build_claude_delete_plan(self.catalog()).with_selected_actions((ONE,))
        calls = 0

        def unlink(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("denied")
            path.unlink()

        result = execute_claude_delete(
            plan, catalog_builder=self.catalog,
            approved_plan_fingerprint=plan.plan_fingerprint or "", clients_closed=True,
            unlink_fn=unlink,
        )
        self.assertEqual(result.results[0].status, "unknown")
        self.assertTrue(result.results[0].deleted_paths)
        self.assertTrue(result.results[0].not_deleted_paths)

    def test_rogue_sidecar_child_added_during_hash_causes_zero_mutation(self) -> None:
        transcript = self.write_session(ONE)
        sidecar = transcript.parent / ONE
        injected = False

        def read_and_inject(path: Path) -> bytes:
            nonlocal injected
            if not injected:
                injected = True
                (sidecar / "rogue.txt").write_text("unapproved", encoding="utf-8")
            return path.read_bytes()

        result, unlinks, rmdirs = self.execute_with_observed_mutations(
            read_and_inject
        )
        self.assertEqual(result.results[0].status, "not_deleted")
        self.assertEqual(unlinks, [])
        self.assertEqual(rmdirs, [])
        self.assertTrue(transcript.exists())
        self.assertIn("directory changed", result.results[0].error or "")

    def test_rogue_injected_at_final_scope_entry_causes_zero_mutation(self) -> None:
        transcript = self.write_session(ONE)
        sidecar = transcript.parent / ONE
        plan = build_claude_delete_plan(self.catalog()).with_selected_actions((ONE,))
        file_count = sum(
            item.node_type == "file" for item in plan.actions[0].manifest
        )
        reads = 0
        injected = False

        def read(path: Path) -> bytes:
            nonlocal reads
            reads += 1
            return path.read_bytes()

        def lstat_and_inject(path) -> os.stat_result:
            nonlocal injected
            lexical = Path(path)
            if (
                reads == file_count
                and lexical == self.config
                and not injected
            ):
                injected = True
                (sidecar / "phase-boundary-rogue.txt").write_text(
                    "unapproved", encoding="utf-8"
                )
            return os.lstat(path)

        result, unlinks, rmdirs = self.execute_with_observed_mutations(
            read, lstat_fn=lstat_and_inject, plan=plan
        )
        self.assertTrue(injected)
        self.assertEqual(result.results[0].status, "not_deleted")
        self.assertEqual(unlinks, [])
        self.assertEqual(rmdirs, [])
        self.assertTrue(transcript.exists())
        self.assertTrue((sidecar / "phase-boundary-rogue.txt").exists())

    def test_new_project_copy_added_during_hash_causes_zero_mutation(self) -> None:
        transcript = self.write_session(ONE)
        injected = False

        def read_and_inject(path: Path) -> bytes:
            nonlocal injected
            if not injected:
                injected = True
                copy = self.config / "projects" / "late" / f"{ONE}.jsonl"
                copy.parent.mkdir(parents=True)
                copy.write_text(
                    json.dumps({"sessionId": ONE, "message": "late"}) + "\n",
                    encoding="utf-8",
                )
            return path.read_bytes()

        result, unlinks, rmdirs = self.execute_with_observed_mutations(
            read_and_inject
        )
        self.assertEqual(result.results[0].status, "not_deleted")
        self.assertEqual(unlinks, [])
        self.assertEqual(rmdirs, [])
        self.assertTrue(transcript.exists())
        self.assertIn("exact session scope changed", result.results[0].error or "")

    def test_new_current_auxiliary_targets_added_during_hash_cause_zero_mutation(self) -> None:
        transcript = self.write_session(ONE)
        injected = False

        def read_and_inject(path: Path) -> bytes:
            nonlocal injected
            if not injected:
                injected = True
                debug = self.config / "debug" / f"{ONE}.txt"
                debug.parent.mkdir()
                debug.write_text("late debug", encoding="utf-8")
                todo = self.config / "todos" / f"{ONE}-agent-late_1.json"
                todo.parent.mkdir()
                todo.write_text("late todo", encoding="utf-8")
            return path.read_bytes()

        result, unlinks, rmdirs = self.execute_with_observed_mutations(
            read_and_inject
        )
        self.assertEqual(result.results[0].status, "not_deleted")
        self.assertEqual(unlinks, [])
        self.assertEqual(rmdirs, [])
        self.assertTrue(transcript.exists())
        self.assertIn("exact session scope changed", result.results[0].error or "")

    def test_current_auxiliary_targets_added_after_preview_cause_zero_mutation(self) -> None:
        transcript = self.write_session(ONE)
        plan = build_claude_delete_plan(self.catalog()).with_selected_actions((ONE,))
        debug = self.config / "debug" / f"{ONE}.txt"
        debug.parent.mkdir()
        debug.write_text("late debug", encoding="utf-8")
        todo = self.config / "todos" / f"{ONE}-agent-late_1.json"
        todo.parent.mkdir()
        todo.write_text("late todo", encoding="utf-8")
        unlinks: list[Path] = []
        rmdirs: list[Path] = []

        with self.assertRaises(ClaudeDeletePlanError):
            execute_claude_delete(
                plan,
                catalog_builder=self.catalog,
                approved_plan_fingerprint=plan.plan_fingerprint or "",
                clients_closed=True,
                unlink_fn=lambda path: unlinks.append(path),
                rmdir_fn=lambda path: rmdirs.append(path),
            )
        self.assertEqual(unlinks, [])
        self.assertEqual(rmdirs, [])
        self.assertTrue(transcript.exists())

    def test_change_after_first_unlink_is_reported_unknown(self) -> None:
        transcript = self.write_session(ONE)
        sidecar = transcript.parent / ONE
        plan = build_claude_delete_plan(self.catalog()).with_selected_actions((ONE,))
        calls = 0

        def unlink_and_race(path: Path) -> None:
            nonlocal calls
            calls += 1
            path.unlink()
            if calls == 1:
                (sidecar / "late-race.txt").write_text("late", encoding="utf-8")

        result = execute_claude_delete(
            plan,
            catalog_builder=self.catalog,
            approved_plan_fingerprint=plan.plan_fingerprint or "",
            clients_closed=True,
            unlink_fn=unlink_and_race,
        )
        self.assertEqual(result.results[0].status, "unknown")
        self.assertTrue(result.results[0].deleted_paths)
        self.assertTrue(result.results[0].not_deleted_paths)
        self.assertTrue((sidecar / "late-race.txt").exists())

    def test_replacement_directory_after_last_child_unlink_is_not_removed(self) -> None:
        transcript = self.config / "projects" / "project" / f"{ONE}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"sessionId": ONE}) + "\n", encoding="utf-8")
        sidecar = transcript.parent / ONE
        sidecar.mkdir()
        owned = sidecar / "owned.txt"
        owned.write_text("approved", encoding="utf-8")
        original_inode = sidecar.stat().st_ino
        plan = build_claude_delete_plan(self.catalog()).with_selected_actions((ONE,))
        replaced = False

        def unlink_and_replace(path: Path) -> None:
            nonlocal replaced
            path.unlink()
            if path == owned:
                sidecar.rmdir()
                sidecar.mkdir()
                replaced = True

        result = execute_claude_delete(
            plan,
            catalog_builder=self.catalog,
            approved_plan_fingerprint=plan.plan_fingerprint or "",
            clients_closed=True,
            unlink_fn=unlink_and_replace,
        )

        self.assertTrue(replaced)
        self.assertEqual(result.results[0].status, "unknown")
        self.assertIn("was replaced", result.results[0].error or "")
        self.assertTrue(sidecar.is_dir())
        self.assertNotEqual(sidecar.stat().st_ino, original_inode)

    def test_zero_directory_file_id_is_unavailable_before_any_mutation(self) -> None:
        transcript = self.write_session(ONE)
        normal_catalog = self.catalog()
        record = normal_catalog.records[0]
        zero_manifest = tuple(
            replace(entry, stat_ino=0)
            if entry.node_type == "directory"
            else entry
            for entry in record.manifest
        )
        zero_catalog = replace(
            normal_catalog,
            records=(replace(record, manifest=zero_manifest),),
        )

        unavailable = build_claude_delete_plan(zero_catalog)
        self.assertFalse(unavailable.actions[0].available)
        self.assertTrue(any(
            "no reliable directory file ID" in reason
            for reason in unavailable.actions[0].unavailable_reasons
        ))
        self.assertTrue(transcript.exists())

        approved = build_claude_delete_plan(normal_catalog).with_selected_actions((ONE,))
        unlinks: list[Path] = []
        rmdirs: list[Path] = []
        with self.assertRaises(ClaudeDeletePlanError):
            execute_claude_delete(
                approved,
                catalog_builder=lambda: zero_catalog,
                approved_plan_fingerprint=approved.plan_fingerprint or "",
                clients_closed=True,
                unlink_fn=lambda path: unlinks.append(path),
                rmdir_fn=lambda path: rmdirs.append(path),
            )
        self.assertEqual(unlinks, [])
        self.assertEqual(rmdirs, [])
        self.assertTrue(transcript.exists())

    def execute_with_observed_mutations(
        self, read_bytes_fn, *, lstat_fn=os.lstat, plan=None
    ):
        plan = plan or build_claude_delete_plan(
            self.catalog()
        ).with_selected_actions((ONE,))
        unlinks: list[Path] = []
        rmdirs: list[Path] = []

        def unlink(path: Path) -> None:
            unlinks.append(path)
            path.unlink()

        def rmdir(path: Path) -> None:
            rmdirs.append(path)
            path.rmdir()

        result = execute_claude_delete(
            plan,
            catalog_builder=self.catalog,
            approved_plan_fingerprint=plan.plan_fingerprint or "",
            clients_closed=True,
            lstat_fn=lstat_fn,
            read_bytes_fn=read_bytes_fn,
            unlink_fn=unlink,
            rmdir_fn=rmdir,
        )
        return result, unlinks, rmdirs


if __name__ == "__main__":
    unittest.main()
