from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.adapters import NativeIntegrityAdapter
from local_agent_record_janitor.cleaner import (
    CleanupReport,
    CleanupResult,
    VerificationResult,
)
from local_agent_record_janitor.cli import EXIT_ERROR, EXIT_OK, build_parser, main
from local_agent_record_janitor.codex_desktop_state import DesktopCleanupResult
from local_agent_record_janitor.gui import (
    GUI_DELETE_CONFIRMATION,
    build_gui_snapshot,
    confirmation_summary,
    execute_gui_delete,
)
from local_agent_record_janitor.inventory import (
    FrontendSessionRecord,
    ManagedConversation,
    SessionCatalog,
    build_session_catalog,
)
from local_agent_record_janitor.models import ConversationSummary, Finding

from tests.support import create_thread_index, write_rollout


class GuiSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        home = Path("C:/CodexHome")
        reference = FrontendSessionRecord(
            platform="codex-desktop",
            platform_session_id="local:root-thread",
            thread_id="root-thread",
            database=Path("C:/Codex/state.sqlite"),
            codex_home=home,
            status="cataloged",
            title="Root title",
            is_live=False,
            details={
                "reference_kind": "desktop_host_catalog",
                "host_id": "local",
                "snapshot_fingerprint": "desktop:v1:" + "a" * 64,
                "global_state_reference_count": 2,
            },
        )
        root = ManagedConversation(
            codex_home=home,
            thread_id="root-thread",
            summary=ConversationSummary(
                thread_id="root-thread",
                display_name="Root title",
                project_label="project-a",
                cwd="C:/work/a",
                archived=False,
                indexed=True,
            ),
            frontend_sessions=(reference,),
            descendant_thread_ids=("child-thread",),
            thread_index={"id": "root-thread", "rollout_path": None},
            indexed=True,
            artifact_present=True,
            deletable=True,
        )
        child = ManagedConversation(
            codex_home=home,
            thread_id="child-thread",
            summary=ConversationSummary(
                thread_id="child-thread",
                display_name="Child title",
                indexed=True,
            ),
            thread_index={"id": "child-thread", "rollout_path": None},
            indexed=True,
            artifact_present=True,
            deletable=True,
        )
        blocked = ManagedConversation(
            codex_home=home,
            thread_id="legacy-only",
            summary=ConversationSummary(
                thread_id="legacy-only",
                display_name="Legacy only",
            ),
            legacy_indexed=True,
            artifact_present=False,
            deletable=False,
            blockers=("legacy-index-only",),
        )
        self.catalog = SessionCatalog(records=(root, child, blocked))

    def test_snapshot_exposes_checkbox_rows_without_message_bodies(self) -> None:
        snapshot = build_gui_snapshot(self.catalog)
        by_id = {row.thread_id: row for row in snapshot.rows}

        self.assertEqual(len(snapshot.rows), 3)
        self.assertTrue(by_id["root-thread"].available)
        self.assertEqual(by_id["root-thread"].affected_count, 2)
        self.assertEqual(by_id["root-thread"].reference_count, 1)
        self.assertIn("root title", by_id["root-thread"].search_text)
        self.assertFalse(by_id["legacy-only"].available)
        self.assertIn("legacy-index-only", by_id["legacy-only"].details_text())

    def test_snapshot_notice_keeps_unmapped_and_failure_details_visible(self) -> None:
        home = Path("C:/CodexHome")
        unmapped = FrontendSessionRecord(
            platform="aionui",
            platform_session_id="unassigned",
            thread_id=None,
            database=Path("C:/AionUi/aionui.db"),
            codex_home=home,
            status="active",
        )
        snapshot = build_gui_snapshot(
            SessionCatalog(
                records=self.catalog.records,
                unmapped_frontend_sessions=(unmapped,),
            )
        )

        self.assertIn("未映射前端记录", snapshot.notice_text())
        self.assertIn("unassigned", snapshot.notice_text())

    def test_selected_plan_includes_exact_desktop_cleanup(self) -> None:
        snapshot = build_gui_snapshot(self.catalog)
        root = next(row for row in snapshot.rows if row.thread_id == "root-thread")
        plan = snapshot.selected_plan((root.action_id,))
        text = confirmation_summary(plan)

        self.assertEqual(plan.actions[0].affected_thread_ids, ("child-thread", "root-thread"))
        self.assertIn("root-thread", text)
        self.assertIn("child-thread", text)
        self.assertEqual(len(plan.desktop_targets), 1)
        self.assertIn("精确清理 1 条 Codex Desktop", text)
        self.assertIn("2 条结构化 UI 引用", text)
        self.assertIn("仍将保留 0 条 Cindy/AionUI", text)
        self.assertIn(str(plan.plan_fingerprint), text)

    def test_overlapping_checked_roots_are_rejected(self) -> None:
        snapshot = build_gui_snapshot(self.catalog)
        root = next(row for row in snapshot.rows if row.thread_id == "root-thread")
        child = next(row for row in snapshot.rows if row.thread_id == "child-thread")

        with self.assertRaisesRegex(ValueError, "overlap|cascade"):
            snapshot.selected_plan((root.action_id, child.action_id))

    def test_execution_cleans_desktop_after_native_desktop_only_partial(self) -> None:
        snapshot = build_gui_snapshot(self.catalog)
        root = next(row for row in snapshot.rows if row.thread_id == "root-thread")
        plan = snapshot.selected_plan((root.action_id,))
        finding = Finding(
            platform="native",
            platform_session_id="root-thread",
            thread_id="root-thread",
            reason="manual delete",
            platform_db=Path("C:/CodexHome/state_5.sqlite"),
            codex_home=Path("C:/CodexHome"),
        )
        native_report = CleanupReport(
            planned=[finding],
            results=[
                CleanupResult(
                    finding=finding,
                    status="partial",
                    remaining_artifacts=(
                        "desktop-catalog:C:/Codex/state.sqlite:local:root-thread",
                    ),
                )
            ],
        )
        events: list[str] = []

        def desktop_cleanup(home, approved, **_kwargs):
            events.append("desktop")
            self.assertEqual(set(approved), {"root-thread"})
            return DesktopCleanupResult(
                codex_home=Path(home),
                thread_ids=("root-thread",),
                deleted_catalog_rows=1,
                removed_global_state_references=2,
                backup_id="backup",
                backup_directory=Path("C:/backup"),
            )

        with patch(
            "local_agent_record_janitor.gui.execute_manual_delete",
            side_effect=lambda *_args, **_kwargs: (
                events.append("native") or native_report
            ),
        ):
            report = execute_gui_delete(
                plan,
                catalog_builder=lambda: self.catalog,
                approved_plan_fingerprint=plan.plan_fingerprint,
                clients_closed=True,
                desktop_cleanup_executor=desktop_cleanup,
                desktop_state_reader=lambda _home, _ids: SimpleNamespace(
                    threads={"root-thread": SimpleNamespace(present=True)}
                ),
                final_verifier=lambda _finding: VerificationResult(
                    deleted=True,
                    status="deleted",
                    checked_thread_ids=("child-thread", "root-thread"),
                ),
            )

        self.assertEqual(events, ["native", "desktop"])
        self.assertTrue(report.ok)
        self.assertEqual(report.results[0].status, "deleted")
        self.assertEqual(report.desktop_results[0].deleted_catalog_rows, 1)

    def test_desktop_snapshot_drift_blocks_native_request(self) -> None:
        snapshot = build_gui_snapshot(self.catalog)
        root_row = next(
            row for row in snapshot.rows if row.thread_id == "root-thread"
        )
        plan = snapshot.selected_plan((root_row.action_id,))
        root_record = self.catalog.records[0]
        reference = root_record.frontend_sessions[0]
        changed_reference = replace(
            reference,
            details={
                **dict(reference.details),
                "snapshot_fingerprint": "desktop:v1:" + "b" * 64,
            },
        )
        changed_catalog = replace(
            self.catalog,
            records=(
                replace(root_record, frontend_sessions=(changed_reference,)),
                *self.catalog.records[1:],
            ),
        )

        with patch(
            "local_agent_record_janitor.gui.execute_manual_delete"
        ) as native_delete:
            with self.assertRaisesRegex(ValueError, "changed after approval"):
                execute_gui_delete(
                    plan,
                    catalog_builder=lambda: changed_catalog,
                    approved_plan_fingerprint=plan.plan_fingerprint,
                    clients_closed=True,
                )

        native_delete.assert_not_called()

    def test_real_desktop_writer_completes_compound_gui_deletion(self) -> None:
        thread_id = "019f9873-d075-7940-aa54-f30c5028524f"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex-home"
            rollout = write_rollout(
                home,
                thread_id,
                originator="Codex Desktop",
            )
            index = create_thread_index(
                home,
                [
                    {
                        "id": thread_id,
                        "rollout_path": str(rollout),
                        "source": "app-server",
                    }
                ],
            )
            desktop_database = home / "sqlite" / "codex-dev.db"
            desktop_database.parent.mkdir()
            with closing(sqlite3.connect(desktop_database)) as connection:
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
                    (thread_id, "Compound GUI target"),
                )
                connection.commit()
            state_path = home / ".codex-global-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "projectless-thread-ids": [thread_id, "healthy"],
                        f"thread-permissions-{thread_id}": {
                            "mode": "workspace"
                        },
                        "prompt-history": [f"text mentions {thread_id}"],
                    }
                ),
                encoding="utf-8",
            )

            catalog = build_session_catalog(
                [NativeIntegrityAdapter(codex_home=home)]
            )
            snapshot = build_gui_snapshot(catalog)
            row = next(item for item in snapshot.rows if item.thread_id == thread_id)
            plan = snapshot.selected_plan((row.action_id,))
            finding = Finding(
                platform="native",
                platform_session_id=thread_id,
                thread_id=thread_id,
                reason="manual delete",
                platform_db=index,
                codex_home=home,
            )

            def delete_native(*_args, **_kwargs):
                rollout.unlink()
                with closing(sqlite3.connect(index)) as connection:
                    connection.execute(
                        "DELETE FROM threads WHERE id = ?",
                        (thread_id,),
                    )
                    connection.commit()
                return CleanupReport(
                    planned=[finding],
                    results=[
                        CleanupResult(
                            finding=finding,
                            status="partial",
                            remaining_artifacts=(
                                f"desktop-catalog:{desktop_database}:local:{thread_id}",
                            ),
                        )
                    ],
                )

            with patch(
                "local_agent_record_janitor.gui.execute_manual_delete",
                side_effect=delete_native,
            ):
                report = execute_gui_delete(
                    plan,
                    catalog_builder=lambda: catalog,
                    approved_plan_fingerprint=plan.plan_fingerprint,
                    clients_closed=True,
                    client_inspector=lambda _home: (),
                )

            self.assertTrue(report.ok)
            self.assertEqual(report.results[0].status, "deleted")
            self.assertEqual(report.desktop_results[0].deleted_catalog_rows, 1)
            self.assertEqual(
                report.desktop_results[0].removed_global_state_references,
                2,
            )
            self.assertFalse(report.desktop_results[0].backup_directory.exists())
            with closing(sqlite3.connect(desktop_database)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM local_thread_catalog WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()[0]
            self.assertEqual(count, 0)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["projectless-thread-ids"], ["healthy"])
            self.assertIn(thread_id, state["prompt-history"][0])


class GuiCliTests(unittest.TestCase):
    def test_parser_advertises_gui_command(self) -> None:
        self.assertIn("gui", build_parser().format_help())
        args = build_parser().parse_args(["gui", "--timeout", "12"])
        self.assertEqual(args.command, "gui")
        self.assertEqual(args.platform, ["all"])
        self.assertEqual(args.timeout, 12.0)

    def test_gui_dispatch_receives_reusable_adapter_builder(self) -> None:
        adapter = object()
        calls: list[object] = []

        def runner(args, **kwargs):
            calls.append(args)
            self.assertEqual(kwargs["adapter_builder"](), [adapter])
            self.assertEqual(kwargs["adapter_builder"](), [adapter])
            return EXIT_OK

        status = main(
            ["gui"],
            adapters=iter((adapter,)),
            stdout=StringIO(),
            stderr=StringIO(),
            gui_runner=runner,
        )

        self.assertEqual(status, EXIT_OK)
        self.assertEqual(len(calls), 1)

    def test_gui_start_failure_is_reported_without_traceback(self) -> None:
        errors = StringIO()

        def runner(_args, **_kwargs):
            raise RuntimeError("display unavailable")

        status = main(
            ["gui"],
            adapters=(),
            stdout=StringIO(),
            stderr=errors,
            gui_runner=runner,
        )

        self.assertEqual(status, EXIT_ERROR)
        self.assertIn("display unavailable", errors.getvalue())

    def test_confirmation_phrase_matches_existing_manual_delete_contract(self) -> None:
        from local_agent_record_janitor.cli import MANUAL_DELETE_CONFIRMATION

        self.assertEqual(GUI_DELETE_CONFIRMATION, MANUAL_DELETE_CONFIRMATION)


if __name__ == "__main__":
    unittest.main()
