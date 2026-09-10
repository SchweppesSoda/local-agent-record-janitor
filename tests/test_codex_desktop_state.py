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
from local_agent_record_janitor.cleaner import scan_adapters, verify_finding_deleted
from local_agent_record_janitor.cli import EXIT_OK, main
from local_agent_record_janitor.codex_desktop_state import (
    DesktopStateError,
    _relevant_client_names,
    execute_desktop_state_cleanup,
    read_desktop_state,
)
from local_agent_record_janitor.inventory import build_session_catalog
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.models import Finding
from local_agent_record_janitor.planning import ActionKind, RiskLevel, build_cleanup_plan

from tests.support import create_thread_index, write_rollout


THREAD_ID = "019f9873-d075-7940-aa54-f30c5028524f"


class CodexDesktopStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.codex_home = Path(self.temporary_directory.name) / "codex-home"
        self.codex_home.mkdir()
        create_thread_index(self.codex_home, [])
        self.database = self.codex_home / "sqlite" / "codex-dev.db"
        self.database.parent.mkdir()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE local_thread_catalog (
                    host_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    display_title TEXT,
                    missing_candidate INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (host_id, thread_id)
                );
                CREATE TABLE local_thread_catalog_metadata (
                    id INTEGER PRIMARY KEY,
                    catalog_revision INTEGER NOT NULL
                );
                INSERT INTO local_thread_catalog_metadata VALUES (1, 30);
                """
            )
            connection.execute(
                "INSERT INTO local_thread_catalog "
                "(host_id, thread_id, display_title) VALUES ('local', ?, ?)",
                (THREAD_ID, "残留任务标题"),
            )
            connection.commit()
        self.state_path = self.codex_home / ".codex-global-state.json"
        self.state_path.write_text(
            json.dumps(
                {
                    "projectless-thread-ids": [THREAD_ID, "healthy"],
                    f"thread-permissions-{THREAD_ID}": {"mode": "workspace"},
                    "prompt-history": [f"请检查文字里提到的 {THREAD_ID}"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def adapter(self) -> NativeIntegrityAdapter:
        return NativeIntegrityAdapter(codex_home=self.codex_home)

    def _json_only_state(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM local_thread_catalog")
            connection.commit()
        self.state_path.with_suffix(".json.bak").write_bytes(self.state_path.read_bytes())

    def test_exact_json_only_operation_applies_and_recovers_without_native_delete(self) -> None:
        self._json_only_state()
        adapter = self.adapter()
        service = CleanupService(client_inspector=lambda *_: ())
        coordinator = OperationCoordinator(service)
        plan_path = self.codex_home.parent / "json-only-plan.json"
        plan = coordinator.plan_operation(client="native", record_ids=(THREAD_ID,),
            adapters=(adapter,), plan_path=plan_path)
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual(len(plan["actions"]), 1)
        action = plan["actions"][0]
        self.assertEqual(action["kind"], "remove_desktop_state")
        self.assertEqual(action["impact"]["desktop_catalog_record_count"], 0)
        self.assertEqual(action["impact"]["desktop_global_state_reference_count"], 4)
        self.assertEqual(adapter.desktop_thread_ids, ())
        with patch("local_agent_record_janitor.codex_desktop_state.running_related_clients", return_value=()), patch(
            "local_agent_record_janitor.codex_app_server.CodexAppServer.delete_thread",
            side_effect=AssertionError("JSON-only cleanup must not call thread/delete"),
        ):
            result = OperationCoordinator(service).apply_operation(
                operation_id=plan["operation_id"], plan_path=plan_path,
                clients_closed=True, adapters=(adapter,))
        self.assertEqual(result["goal_status"], "complete", result)
        verified = OperationCoordinator(service).verify_operation(
            operation_id=plan["operation_id"], plan_path=plan_path, adapters=(adapter,))
        self.assertEqual(verified["goal_status"], "complete", verified)
        for path in (self.state_path, self.state_path.with_suffix(".json.bak")):
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["projectless-thread-ids"], ["healthy"])
            self.assertIn(THREAD_ID, value["prompt-history"][0])

    def test_json_only_discovery_requires_full_explicit_id(self) -> None:
        self._json_only_state()
        for index, scope in enumerate(({"all_projects": True}, {"record_ids": (THREAD_ID[:8],)})):
            plan = OperationCoordinator(CleanupService()).plan_operation(
                client="native", adapters=(self.adapter(),),
                plan_path=self.codex_home.parent / f"unscoped-{index}.json", **scope)
            self.assertFalse(plan["actions"], plan)

    def test_json_only_cleanup_rejects_changed_reference_fingerprint(self) -> None:
        self._json_only_state()
        state = read_desktop_state(self.codex_home, (THREAD_ID,)).threads[THREAD_ID]
        self.state_path.write_text(json.dumps({"projectless-thread-ids": [THREAD_ID]}), encoding="utf-8")
        before = self.state_path.read_bytes()
        with self.assertRaises(DesktopStateError):
            execute_desktop_state_cleanup(self.codex_home,
                {THREAD_ID: state.snapshot_fingerprint}, client_inspector=lambda *_: ())
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_json_only_cleanup_refuses_open_clients(self) -> None:
        self._json_only_state()
        state = read_desktop_state(self.codex_home, (THREAD_ID,)).threads[THREAD_ID]
        before = self.state_path.read_bytes()
        with self.assertRaises(DesktopStateError):
            execute_desktop_state_cleanup(self.codex_home,
                {THREAD_ID: state.snapshot_fingerprint}, client_inspector=lambda *_: ("Codex",))
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_catalog_disappearing_after_plan_is_still_drift(self) -> None:
        state = read_desktop_state(self.codex_home, (THREAD_ID,)).threads[THREAD_ID]
        self._json_only_state()
        with self.assertRaises(DesktopStateError):
            execute_desktop_state_cleanup(self.codex_home,
                {THREAD_ID: state.snapshot_fingerprint}, client_inspector=lambda *_: ())

    def test_json_only_cleanup_rejects_reappearing_native_rollout(self) -> None:
        self._json_only_state()
        state = read_desktop_state(self.codex_home, (THREAD_ID,)).threads[THREAD_ID]
        rollout = write_rollout(self.codex_home, THREAD_ID, originator="Codex Desktop")
        before = self.state_path.read_bytes()
        with self.assertRaises(DesktopStateError):
            execute_desktop_state_cleanup(self.codex_home,
                {THREAD_ID: state.snapshot_fingerprint}, client_inspector=lambda *_: ())
        self.assertTrue(rollout.exists())
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_json_only_cleanup_rolls_back_both_state_files_on_write_failure(self) -> None:
        self._json_only_state()
        state = read_desktop_state(self.codex_home, (THREAD_ID,)).threads[THREAD_ID]
        before = {path: path.read_bytes() for path in
            (self.state_path, self.state_path.with_suffix(".json.bak"))}
        from local_agent_record_janitor import codex_desktop_state as desktop
        real_write = desktop._atomic_write_json

        def fail_backup_write(path, value):
            if path == self.state_path.with_suffix(".json.bak"):
                raise OSError("injected write failure")
            return real_write(path, value)

        with patch.object(desktop, "_atomic_write_json", side_effect=fail_backup_write):
            with self.assertRaises(DesktopStateError):
                execute_desktop_state_cleanup(self.codex_home,
                    {THREAD_ID: state.snapshot_fingerprint}, client_inspector=lambda *_: ())
        for path, expected in before.items():
            self.assertEqual(path.read_bytes(), expected)

    def _cindy_process_records(
        self,
        root: Path,
    ) -> tuple[dict[str, object], ...]:
        executable = root / "Programs" / "Cindy" / "Cindy.exe"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"cindy")
        user_data = root / "CindyGlobal"
        (user_data / "codex-home").mkdir(parents=True, exist_ok=True)
        bundled = user_data / "codex" / "0.145.0" / "codex.exe"
        bundled.parent.mkdir(parents=True, exist_ok=True)
        bundled.write_bytes(b"codex")
        return (
            {
                "process_id": 100,
                "parent_process_id": 1,
                "name": "Cindy.exe",
                "executable_path": str(executable),
                "command_line": f'"{executable}"',
            },
            {
                "process_id": 101,
                "parent_process_id": 100,
                "name": "Cindy.exe",
                "executable_path": str(executable),
                "command_line": (
                    f'--type=renderer --user-data-dir="{user_data}"'
                ),
            },
            {
                "process_id": 102,
                "parent_process_id": 101,
                "name": "codex.exe",
                "executable_path": str(bundled),
                "command_line": "codex.exe app-server",
            },
        )

    def _official_codex_process_records(
        self,
        root: Path,
    ) -> tuple[dict[str, object], ...]:
        app_root = (
            root
            / "WindowsApps"
            / "OpenAI.Codex_26.814.5167.0_x64__fixture"
            / "app"
        )
        executable = app_root / "ChatGPT.exe"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"chatgpt")
        bundled = app_root / "resources" / "codex.exe"
        bundled.parent.mkdir(parents=True, exist_ok=True)
        bundled.write_bytes(b"codex")
        return (
            {
                "process_id": 200,
                "parent_process_id": 1,
                "name": "ChatGPT.exe",
                "executable_path": str(executable),
                "command_line": f'"{executable}"',
            },
            {
                "process_id": 201,
                "parent_process_id": 200,
                "name": "ChatGPT.exe",
                "executable_path": str(executable),
                "command_line": "--type=renderer",
            },
            {
                "process_id": 202,
                "parent_process_id": 201,
                "name": "codex.exe",
                "executable_path": str(bundled),
                "command_line": "codex.exe app-server",
            },
        )

    def test_native_home_ignores_proven_separate_cindy_family(self) -> None:
        root = Path(self.temporary_directory.name) / "processes"
        records = self._cindy_process_records(root) + (
            {
                "process_id": 200,
                "parent_process_id": 1,
                "name": "ChatGPT.exe",
                "executable_path": None,
                "command_line": "ChatGPT.exe",
            },
        )
        native_home = root / "native" / ".codex"
        native_home.mkdir(parents=True)

        self.assertEqual(
            _relevant_client_names(native_home, records),
            ("ChatGPT.exe",),
        )

    def test_cindy_store_blocks_cindy_and_bundled_codex(self) -> None:
        root = Path(self.temporary_directory.name) / "processes"
        records = self._cindy_process_records(root)
        cindy_root = root / "CindyGlobal"

        self.assertEqual(
            _relevant_client_names(cindy_root, records, owner_client="cindy"),
            ("Cindy.exe", "codex.exe"),
        )

    def test_cindy_store_ignores_proven_official_codex_family(self) -> None:
        root = Path(self.temporary_directory.name) / "processes"
        self._cindy_process_records(root)
        records = self._official_codex_process_records(root)
        cindy_root = root / "CindyGlobal"

        self.assertEqual(
            _relevant_client_names(cindy_root, records, owner_client="cindy"),
            (),
        )

    def test_cindy_store_ignores_unrelated_chatgpt_without_metadata(self) -> None:
        root = Path(self.temporary_directory.name) / "processes"
        self._cindy_process_records(root)
        cindy_root = root / "CindyGlobal"
        records = (
            {
                "process_id": 200,
                "parent_process_id": 1,
                "name": "ChatGPT.exe",
                "executable_path": str(root / "missing" / "ChatGPT.exe"),
                "command_line": "ChatGPT.exe",
            },
        )

        self.assertEqual(
            _relevant_client_names(cindy_root, records, owner_client="cindy"),
            (),
        )

    def test_cindy_store_ignores_unrelated_metadata_gaps_but_blocks_unknown_cindy(
        self,
    ) -> None:
        root = Path(self.temporary_directory.name) / "processes"
        owner_root = root / "renamed-owner-root"
        owner_root.mkdir(parents=True)
        records = (
            {
                "name": "ChatGPT.exe",
                "executable_path": None,
                "command_line": None,
            },
            {
                "process_id": 201,
                "parent_process_id": 1,
                "name": "AionUI.exe",
                "executable_path": None,
                "command_line": None,
            },
            {
                "process_id": 202,
                "parent_process_id": 1,
                "name": "codex.exe",
                "executable_path": None,
                "command_line": None,
            },
        )
        self.assertEqual(
            _relevant_client_names(owner_root, records, owner_client="cindy"),
            (),
        )

        other_cindy = self._cindy_process_records(
            root / "other-process-root",
        )
        self.assertEqual(
            _relevant_client_names(
                owner_root,
                other_cindy + records,
                owner_client="cindy",
            ),
            (),
        )

        incomplete_cindy = (
            {
                "process_id": 300,
                "parent_process_id": 1,
                "name": "Cindy.exe",
                "executable_path": None,
                "command_line": None,
            },
        )
        self.assertEqual(
            _relevant_client_names(
                owner_root,
                incomplete_cindy,
                owner_client="cindy",
            ),
            ("Cindy.exe",),
        )

    def test_unproven_or_orphan_related_processes_still_block(self) -> None:
        root = Path(self.temporary_directory.name) / "processes"
        native_home = root / "native" / ".codex"
        native_home.mkdir(parents=True)
        records = (
            {
                "process_id": 100,
                "parent_process_id": 1,
                "name": "Cindy.exe",
                "executable_path": str(root / "missing" / "Cindy.exe"),
                "command_line": None,
            },
            {
                "process_id": 200,
                "parent_process_id": 999,
                "name": "codex.exe",
                "executable_path": None,
                "command_line": "codex.exe app-server",
            },
        )

        self.assertEqual(
            _relevant_client_names(native_home, records),
            ("Cindy.exe", "codex.exe"),
        )

    def test_identity_check_failure_keeps_cindy_family_blocking(self) -> None:
        root = Path(self.temporary_directory.name) / "processes"
        records = self._cindy_process_records(root)
        native_home = root / "native" / ".codex"
        native_home.mkdir(parents=True)

        with patch(
            "local_agent_record_janitor.codex_desktop_state._same_existing_path",
            return_value=None,
        ):
            self.assertEqual(
                _relevant_client_names(native_home, records),
                ("Cindy.exe", "codex.exe"),
            )

    def test_relative_cindy_user_data_dir_cannot_prove_another_store(self) -> None:
        root = Path(self.temporary_directory.name) / "processes"
        records = [dict(item) for item in self._cindy_process_records(root)]
        relative_name = f"relative-{root.parent.name}"
        records[1]["command_line"] = (
            f'--type=renderer --user-data-dir="{relative_name}"'
        )
        native_home = root / "native" / ".codex"
        native_home.mkdir(parents=True)
        relative_home = Path.cwd() / relative_name / "codex-home"
        relative_home.mkdir(parents=True, exist_ok=True)
        self.addCleanup(
            lambda: relative_home.parent.rmdir()
            if relative_home.parent.exists()
            and not any(relative_home.parent.iterdir())
            else None
        )
        self.addCleanup(
            lambda: relative_home.rmdir()
            if relative_home.exists() and not any(relative_home.iterdir())
            else None
        )

        self.assertEqual(
            _relevant_client_names(native_home, records),
            ("Cindy.exe", "codex.exe"),
        )

    def test_relative_or_environment_executable_paths_remain_blocking(self) -> None:
        root = Path(self.temporary_directory.name) / "processes"
        native_home = root / "native" / ".codex"
        native_home.mkdir(parents=True)
        for executable in (r"Programs\Cindy\Cindy.exe", r"%LOCALAPPDATA%\Cindy.exe"):
            with self.subTest(executable=executable):
                records = [dict(item) for item in self._cindy_process_records(root)]
                records[0]["executable_path"] = executable
                records[1]["executable_path"] = executable
                self.assertEqual(
                    _relevant_client_names(native_home, records),
                    ("Cindy.exe", "codex.exe"),
                )

    def test_scan_inventory_and_plan_include_desktop_only_ghost(self) -> None:
        report = scan_adapters([self.adapter()])

        self.assertEqual(len(report.errors), 0)
        ghost = next(
            finding
            for finding in report.findings
            if finding.details.get("finding_type") == "desktop_state_orphan"
        )
        self.assertEqual(ghost.thread_id, THREAD_ID)
        self.assertEqual(ghost.details["desktop_catalog_record_count"], 1)
        self.assertEqual(ghost.details["desktop_global_state_reference_count"], 2)

        catalog = build_session_catalog([self.adapter()])
        record = next(item for item in catalog.records if item.thread_id == THREAD_ID)
        self.assertFalse(record.artifact_present)
        self.assertTrue(record.desktop_state_present)
        self.assertEqual(record.summary.display_name, "残留任务标题")
        self.assertFalse(record.deletable)

        plan = build_cleanup_plan(report)
        self.assertEqual(
            plan.conversations[0].summary.display_name,
            "残留任务标题",
        )
        action = next(
            item
            for item in plan.actions
            if item.kind is ActionKind.REMOVE_DESKTOP_STATE
        )
        self.assertTrue(action.available)
        self.assertEqual(action.risk, RiskLevel.HIGH)
        self.assertTrue(action.requires_explicit_selection)
        self.assertEqual(action.impact.desktop_catalog_record_count, 1)
        self.assertEqual(action.impact.desktop_global_state_reference_count, 2)
        self.assertFalse(action.impact.frontend_references_preserved)

        scan_output = StringIO()
        self.assertEqual(
            main(
                ["scan", "--platform", "native", "--json"],
                adapters=[self.adapter()],
                stdin=StringIO(),
                stdout=scan_output,
                stderr=StringIO(),
            ),
            EXIT_OK,
        )
        scan_payload = json.loads(scan_output.getvalue())
        self.assertEqual(
            scan_payload["conversations"][0]["summary"]["display_name"],
            "残留任务标题",
        )

    def test_cleanup_removes_exact_structured_refs_but_preserves_prompt_text(self) -> None:
        state = read_desktop_state(self.codex_home, (THREAD_ID,)).threads[THREAD_ID]
        with patch(
            "local_agent_record_janitor.codex_desktop_state.running_related_clients",
            return_value=(),
        ):
            result = execute_desktop_state_cleanup(
                self.codex_home,
                {THREAD_ID: state.snapshot_fingerprint},
            )

        self.assertEqual(result.deleted_catalog_rows, 1)
        self.assertEqual(result.removed_global_state_references, 2)
        self.assertFalse(result.temporary_backup_retained)
        self.assertFalse(result.backup_directory.exists())
        remaining = read_desktop_state(self.codex_home, (THREAD_ID,)).threads[THREAD_ID]
        self.assertFalse(remaining.present)
        global_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(global_state["projectless-thread-ids"], ["healthy"])
        self.assertIn(THREAD_ID, global_state["prompt-history"][0])

    def test_cleanup_refuses_when_native_evidence_reappears(self) -> None:
        state = read_desktop_state(self.codex_home, (THREAD_ID,)).threads[THREAD_ID]
        rollout = write_rollout(
            self.codex_home,
            THREAD_ID,
            originator="Codex Desktop",
        )
        with closing(
            sqlite3.connect(self.codex_home / "state_5.sqlite")
        ) as connection:
            connection.execute(
                "INSERT INTO threads (id, rollout_path, archived) "
                "VALUES (?, ?, 0)",
                (THREAD_ID, str(rollout)),
            )
            connection.commit()

        with self.assertRaises(DesktopStateError):
            execute_desktop_state_cleanup(
                self.codex_home,
                {THREAD_ID: state.snapshot_fingerprint},
            )

    def test_present_incompatible_desktop_catalog_fails_closed(self) -> None:
        self.database.unlink()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
            connection.commit()

        with self.assertRaises(DesktopStateError):
            read_desktop_state(self.codex_home)

    def test_native_delete_verification_reports_desktop_ghost_as_partial(self) -> None:
        finding = Finding(
            platform="native",
            platform_session_id=THREAD_ID,
            thread_id=THREAD_ID,
            reason="deleted native record",
            platform_db=self.codex_home / "state_5.sqlite",
            codex_home=self.codex_home,
            details={
                "planned_expected_artifacts": [
                    f"index:{self.codex_home / 'state_5.sqlite'}"
                ]
            },
        )

        verification = verify_finding_deleted(finding)

        self.assertEqual(verification.status, "partial")
        self.assertTrue(
            any(
                marker.startswith("desktop-catalog:")
                for marker in verification.remaining_artifacts
            )
        )

    def test_clean_cli_executes_fingerprint_bound_desktop_action(self) -> None:
        preview_output = StringIO()
        preview_status = main(
            ["clean", "--platform", "native", "--json"],
            adapters=[self.adapter()],
            stdin=StringIO(),
            stdout=preview_output,
            stderr=StringIO(),
        )
        preview = json.loads(preview_output.getvalue())
        action = next(
            item
            for item in preview["actions"]
            if item["kind"] == "remove_desktop_state"
        )

        execute_output = StringIO()
        with patch(
            "local_agent_record_janitor.codex_desktop_state.running_related_clients",
            return_value=(),
        ):
            execute_status = main(
                [
                    "clean",
                    "--platform",
                    "native",
                    "--action-id",
                    action["action_id"],
                    "--plan-fingerprint",
                    preview["plan_fingerprint"],
                    "--clients-closed",
                    "--yes",
                    "--json",
                ],
                adapters=[self.adapter()],
                stdin=StringIO(),
                stdout=execute_output,
                stderr=StringIO(),
            )

        self.assertEqual(preview_status, EXIT_OK)
        self.assertEqual(execute_status, EXIT_OK)
        result = json.loads(execute_output.getvalue())
        self.assertEqual(result["mutation_kind"], "remove_desktop_state")
        self.assertEqual(result["result"]["status"], "cleaned")


if __name__ == "__main__":
    unittest.main()
