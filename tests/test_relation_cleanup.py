from __future__ import annotations

import sqlite3
import tempfile
import unittest
import json
from contextlib import closing
from io import StringIO
from pathlib import Path
from unittest import mock

from local_agent_record_janitor.adapters.native import NativeIntegrityAdapter
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.planning import (
    ActionKind,
    build_cleanup_plan,
)
from local_agent_record_janitor.relation_cleanup import (
    RelationCleanupError,
    execute_relation_cleanup,
    verify_relation_evidence,
)
from tests.support import create_thread_index, write_rollout


class RelationCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.codex_home = Path(self.temporary.name) / "codex-home"
        live_parent = write_rollout(
            self.codex_home,
            "live-parent",
            originator="test",
        )
        live_child = write_rollout(
            self.codex_home,
            "live-child",
            originator="test",
        )
        self.database = create_thread_index(
            self.codex_home,
            [
                {"id": "live-parent", "rollout_path": str(live_parent)},
                {"id": "live-child", "rollout_path": str(live_child)},
            ],
            spawn_edges=[
                {
                    "parent_thread_id": "missing-parent",
                    "child_thread_id": "missing-child",
                    "status": "closed",
                },
                {
                    "parent_thread_id": "live-parent",
                    "child_thread_id": "live-child",
                    "status": "open",
                },
            ],
        )

    def _action(self):
        findings = NativeIntegrityAdapter(codex_home=self.codex_home).scan()
        plan = build_cleanup_plan(findings)
        return next(
            action
            for action in plan.actions
            if action.kind is ActionKind.REMOVE_BROKEN_RELATION
            and action.target.thread_id == "missing-child"
        )

    def _rows(self) -> list[tuple[str, str, str]]:
        with closing(sqlite3.connect(self.database)) as connection:
            return list(
                connection.execute(
                    "SELECT parent_thread_id, child_thread_id, status "
                    "FROM thread_spawn_edges ORDER BY child_thread_id"
                )
            )

    def test_exact_relation_is_executable_and_only_approved_row_changes(self) -> None:
        action = self._action()
        self.assertTrue(action.available)
        self.assertTrue(action.requires_explicit_selection)
        self.assertEqual(len(action.impact.relation_evidence), 1)

        result = execute_relation_cleanup(
            self.codex_home,
            action.impact.relation_evidence,
            client_inspector=lambda _home: (),
        )

        self.assertEqual(result.removed_relation_count, 1)
        self.assertEqual(
            self._rows(),
            [("live-parent", "live-child", "open")],
        )
        self.assertEqual(
            verify_relation_evidence(action.impact.relation_evidence),
            (),
        )
        self.assertEqual(
            list(self.codex_home.glob(".larj-relation-*")),
            [],
        )

    def test_row_drift_blocks_without_mutation(self) -> None:
        action = self._action()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE thread_spawn_edges SET status='open' "
                "WHERE child_thread_id='missing-child'"
            )
            connection.commit()
        before = self._rows()

        with self.assertRaises(RelationCleanupError):
            execute_relation_cleanup(
                self.codex_home,
                action.impact.relation_evidence,
                client_inspector=lambda _home: (),
            )

        self.assertEqual(self._rows(), before)

    def test_schema_drift_blocks_without_mutation(self) -> None:
        action = self._action()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("ALTER TABLE thread_spawn_edges ADD COLUMN note TEXT")
            connection.commit()
        before = self._rows()

        with self.assertRaises(RelationCleanupError):
            execute_relation_cleanup(
                self.codex_home,
                action.impact.relation_evidence,
                client_inspector=lambda _home: (),
            )

        self.assertEqual(self._rows(), before)

    def test_transaction_failure_restores_database_and_discards_backup(self) -> None:
        action = self._action()
        before = self._rows()
        with mock.patch(
            "local_agent_record_janitor.relation_cleanup._delete_relation",
            side_effect=RuntimeError("injected failure"),
        ):
            with self.assertRaises(RelationCleanupError):
                execute_relation_cleanup(
                    self.codex_home,
                    action.impact.relation_evidence,
                    client_inspector=lambda _home: (),
                )

        self.assertEqual(self._rows(), before)
        self.assertEqual(
            list(self.codex_home.glob(".larj-relation-*")),
            [],
        )

    def test_running_target_client_blocks_before_backup(self) -> None:
        action = self._action()

        with self.assertRaises(RelationCleanupError):
            execute_relation_cleanup(
                self.codex_home,
                action.impact.relation_evidence,
                client_inspector=lambda _home: ("Codex.exe (pid 42)",),
            )

        self.assertEqual(len(self._rows()), 2)
        self.assertEqual(
            list(self.codex_home.glob(".larj-relation-*")),
            [],
        )

    def test_duplicate_relation_identity_is_blocked_without_row_evidence(self) -> None:
        home = Path(self.temporary.name) / "duplicate-home"
        home.mkdir()
        database = home / "state_5.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)"
            )
            connection.execute(
                "CREATE TABLE thread_spawn_edges ("
                "parent_thread_id TEXT, child_thread_id TEXT, status TEXT)"
            )
            connection.executemany(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                [
                    ("missing-parent", "missing-child", "closed"),
                    ("missing-parent", "missing-child", "closed"),
                ],
            )
            connection.commit()

        findings = NativeIntegrityAdapter(codex_home=home).scan()
        residuals = [
            finding
            for finding in findings
            if finding.details.get("finding_type") == "residual_spawn_edge"
        ]
        self.assertEqual(len(residuals), 2)
        self.assertTrue(
            all(finding.details.get("relation_evidence") is None for finding in residuals)
        )
        self.assertTrue(
            all(not finding.details.get("cleanable") for finding in residuals)
        )
        actions = [
            action
            for action in build_cleanup_plan(findings).actions
            if action.kind is ActionKind.REMOVE_BROKEN_RELATION
        ]
        self.assertTrue(actions)
        self.assertTrue(all(not action.available for action in actions))

    def test_agent_plan_apply_verifies_relation_and_compacts_receipt(self) -> None:
        adapter = NativeIntegrityAdapter(codex_home=self.codex_home)
        plan_path = Path(self.temporary.name) / "relation-plan.json"
        plan_output = StringIO()
        code = main(
            [
                "agent",
                "plan",
                "--platform",
                "native",
                "--codex-home",
                str(self.codex_home),
                "--out",
                str(plan_path),
            ],
            adapters=[adapter],
            stdout=plan_output,
            stderr=StringIO(),
            client_inspector=lambda _home: (),
        )
        self.assertEqual(code, 0)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(
            plan["authorization"]["mutation_kind"],
            "remove_broken_relation",
        )

        output = StringIO()
        code = main(
            [
                "agent",
                "apply",
                "--plan",
                str(plan_path),
                "--authorized-plan-sha256",
                plan["plan_sha256"],
                "--clients-closed",
                "--verify-timeout",
                "0",
            ],
            adapters=[adapter],
            stdout=output,
            stderr=StringIO(),
            app_server_factory=lambda **_kwargs: self.fail(
                "relation cleanup must not start app-server"
            ),
            client_inspector=lambda _home: (),
        )

        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["goal_status"], "complete")
        self.assertEqual(
            result["execution_result"]["result"]["removed_relation_count"],
            1,
        )
        operation = (
            self.codex_home
            / ".local-agent-record-janitor"
            / "operations"
            / plan["operation_id"]
        )
        self.assertEqual(
            {path.name for path in operation.iterdir()},
            {"receipt.json"},
        )


if __name__ == "__main__":
    unittest.main()
