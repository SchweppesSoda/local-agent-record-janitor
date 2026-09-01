from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor import frontend_reference_cleanup as cleanup
from local_agent_record_janitor.adapters import AionUIAdapter, CindyAdapter
from local_agent_record_janitor.frontend_reference_cleanup import (
    FrontendReferenceError,
    execute_frontend_reference_cleanup,
)
from tests.support import create_aionui_database, create_cindy_database


class FrontendReferenceBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()

    def _aionui_evidence(self, database: Path) -> list[dict[str, object]]:
        adapter = AionUIAdapter(
            database=database,
            codex_home=self.codex_home,
            codex_bin_hint=self.root / "codex.exe",
        )
        return [
            dict(session.details["frontend_reference"])
            for session in adapter.list_sessions()
        ]

    def _cindy_evidence(self, database: Path) -> list[dict[str, object]]:
        adapter = CindyAdapter(
            database=database,
            codex_home=self.codex_home,
            cindy_root=self.root,
            codex_bin_hint=self.root / "codex.exe",
        )
        return [
            dict(session.details["frontend_reference"])
            for session in adapter.list_sessions()
        ]

    def _clean(
        self,
        evidence: list[dict[str, object]],
    ) -> cleanup.FrontendReferenceCleanupResult:
        platform = str(evidence[0].get("platform") or "").casefold()
        return execute_frontend_reference_cleanup(
            self.root if platform == "cindy" else self.codex_home,
            evidence,
            client_inspector=lambda _home: (),
            owner_client="cindy" if platform == "cindy" else None,
        )

    def test_aionui_set_guard_and_transaction_count_are_cardinality_independent(self) -> None:
        for size in (1, 10, 100):
            database = self.root / f"aionui-{size}.db"
            create_aionui_database(
                database,
                sessions=[
                    {
                        "conversation_id": f"conversation-{index}",
                        "session_id": f"native-{index}",
                        "agent_id": "agent",
                        "agent_source": "fixture",
                        "session_status": "closed",
                        "last_active_at": index,
                    }
                    for index in range(size)
                ],
                metadata=[("agent", "codex")],
            )
            evidence = self._aionui_evidence(database)
            trace: list[str] = []
            connect = cleanup.sqlite3.connect

            def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
                connection = connect(*args, **kwargs)
                connection.set_trace_callback(trace.append)
                return connection

            with patch.object(cleanup.sqlite3, "connect", side_effect=traced_connect):
                result = self._clean(evidence)

            self.assertEqual(result.removed_reference_count, size)
            self.assertEqual(len(result.reference_results), size)
            self.assertEqual(
                sum("SELECT" in sql and "FROM acp_session WHERE" in sql for sql in trace),
                2,
            )
            self.assertEqual(sum("BEGIN IMMEDIATE" in sql for sql in trace), 1)
            self.assertTrue(all(item["affected_rows"] == 1 for item in result.reference_results))
            self.assertTrue(result.to_dict()["transaction"]["committed"])

    def test_two_frontend_rows_for_one_native_are_two_exact_results(self) -> None:
        database = self.root / "aionui-shared-native.db"
        create_aionui_database(
            database,
            sessions=[
                {
                    "conversation_id": "conversation-one",
                    "session_id": "shared-native",
                    "agent_id": "agent",
                },
                {
                    "conversation_id": "conversation-two",
                    "session_id": "shared-native",
                    "agent_id": "agent",
                },
            ],
            metadata=[("agent", "codex")],
        )

        result = self._clean(self._aionui_evidence(database))

        self.assertEqual(result.deleted_aionui_rows, 2)
        self.assertEqual(
            {item["id"] for item in result.reference_results},
            {"aionui:conversation-one", "aionui:conversation-two"},
        )
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM acp_session").fetchone()[0],
                0,
            )

    def test_cindy_current_and_history_use_set_guards_and_one_transaction(self) -> None:
        database = self.root / "cindy-mixed.db"
        create_cindy_database(
            database,
            [
                {
                    "id": "current-session",
                    "sdk_session_id": "native-current",
                    "status": "deleted",
                    "agent_kind": "codex",
                },
                {
                    "id": "history-session",
                    "sdk_session_id": None,
                    "status": "deleted",
                    "agent_kind": "pi",
                },
            ],
        )
        content = json.dumps(
            {
                "fromAgentKind": "codex",
                "fromSdkSessionId": "native-history",
                "keep": {"value": True},
            },
            separators=(",", ":"),
        )
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                """
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER,
                    rewind_at INTEGER
                )
                """
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "switch-history",
                    "history-session",
                    "agent_switch",
                    content,
                    1,
                    None,
                ),
            )
            connection.commit()

        evidence = self._cindy_evidence(database)
        self.assertEqual(
            {item["operation"] for item in evidence},
            {
                "clear_session_sdk_session_id",
                "remove_agent_switch_from_sdk_session_id",
            },
        )
        trace: list[str] = []
        connect = cleanup.sqlite3.connect

        def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(trace.append)
            return connection

        with patch.object(cleanup.sqlite3, "connect", side_effect=traced_connect):
            result = self._clean(evidence)

        self.assertEqual(result.cleared_cindy_current_references, 1)
        self.assertEqual(result.cleaned_cindy_historical_references, 1)
        self.assertEqual(len(result.verification_results), 2)
        self.assertEqual(sum("SELECT" in sql and "FROM \"sessions\" WHERE" in sql for sql in trace), 2)
        self.assertEqual(sum("SELECT" in sql and "FROM \"messages\" WHERE" in sql for sql in trace), 2)
        self.assertEqual(sum("BEGIN IMMEDIATE" in sql for sql in trace), 1)
        with closing(sqlite3.connect(database)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT sdk_session_id FROM sessions WHERE id = 'current-session'"
                ).fetchone()[0]
            )
            after = connection.execute(
                "SELECT content FROM messages WHERE id = 'switch-history'"
            ).fetchone()[0]
        self.assertNotIn("fromSdkSessionId", json.loads(after))

    def test_native_success_then_frontend_guard_failure_restores_frontend_only(self) -> None:
        database = self.root / "cindy-partial.db"
        create_cindy_database(
            database,
            [
                {
                    "id": "current-session",
                    "sdk_session_id": "native-current",
                    "status": "deleted",
                    "agent_kind": "codex",
                },
            ],
        )
        evidence = self._cindy_evidence(database)
        native_file = self.root / "native-record.jsonl"
        native_file.write_text("native fixture", encoding="utf-8")
        native_file.unlink()
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE sessions SET source = 'drifted' WHERE id = 'current-session'"
            )
            connection.commit()

        with self.assertRaises(FrontendReferenceError):
            self._clean(evidence)

        self.assertFalse(native_file.exists())
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT sdk_session_id, source FROM sessions WHERE id = 'current-session'"
                ).fetchone(),
                ("native-current", "drifted"),
            )

    def test_phase_callback_marks_mutation_only_after_guard_and_verify(self) -> None:
        database = self.root / "aionui-phases.db"
        create_aionui_database(
            database,
            sessions=[
                {
                    "conversation_id": "conversation-one",
                    "session_id": "native-one",
                    "agent_id": "agent",
                },
            ],
            metadata=[("agent", "codex")],
        )
        phases: list[str] = []
        execute_frontend_reference_cleanup(
            self.codex_home,
            self._aionui_evidence(database),
            client_inspector=lambda _home: (),
            phase_callback=phases.append,
        )
        self.assertEqual(phases, ["mutation_started", "verified"])

    def test_guard_drift_emits_no_mutation_marker(self) -> None:
        database = self.root / "aionui-guard-drift.db"
        create_aionui_database(
            database,
            sessions=[
                {
                    "conversation_id": "conversation-one",
                    "session_id": "native-one",
                    "agent_id": "agent",
                },
            ],
            metadata=[("agent", "codex")],
        )
        evidence = self._aionui_evidence(database)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE acp_session SET agent_id = ? WHERE conversation_id = ?",
                ("drifted", "conversation-one"),
            )
            connection.commit()
        phases: list[str] = []
        with self.assertRaises(FrontendReferenceError) as raised:
            execute_frontend_reference_cleanup(
                self.codex_home,
                evidence,
                client_inspector=lambda _home: (),
                phase_callback=phases.append,
            )
        self.assertEqual(phases, [])
        self.assertFalse(raised.exception.mutation_started)
        self.assertFalse(raised.exception.outcome_known_rolled_back)
        self.assertFalse(raised.exception.outcome_unknown)

    def test_sql_failure_after_marker_reports_known_rollback(self) -> None:
        database = self.root / "aionui-known-rollback.db"
        create_aionui_database(
            database,
            sessions=[
                {
                    "conversation_id": "conversation-one",
                    "session_id": "native-one",
                    "agent_id": "agent",
                },
            ],
            metadata=[("agent", "codex")],
        )
        evidence = self._aionui_evidence(database)
        original_apply = cleanup._apply_mutation

        def partial_apply(connection: sqlite3.Connection, mutation: object) -> None:
            original_apply(connection, mutation)  # type: ignore[arg-type]
            raise RuntimeError("synthetic SQL failure")

        phases: list[str] = []
        with patch.object(cleanup, "_apply_mutation", side_effect=partial_apply):
            with self.assertRaises(FrontendReferenceError) as raised:
                execute_frontend_reference_cleanup(
                    self.codex_home,
                    evidence,
                    client_inspector=lambda _home: (),
                    phase_callback=phases.append,
                )
        self.assertEqual(phases, ["mutation_started"])
        self.assertTrue(raised.exception.mutation_started)
        self.assertTrue(raised.exception.outcome_known_rolled_back)
        self.assertFalse(raised.exception.outcome_unknown)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM acp_session").fetchone()[0],
                1,
            )

    def test_restore_failure_reports_unknown_after_marker(self) -> None:
        database = self.root / "aionui-restore-failure.db"
        create_aionui_database(
            database,
            sessions=[
                {
                    "conversation_id": "conversation-one",
                    "session_id": "native-one",
                    "agent_id": "agent",
                },
            ],
            metadata=[("agent", "codex")],
        )
        evidence = self._aionui_evidence(database)
        original_apply = cleanup._apply_mutation

        def partial_apply(connection: sqlite3.Connection, mutation: object) -> None:
            original_apply(connection, mutation)  # type: ignore[arg-type]
            raise RuntimeError("synthetic SQL failure")

        phases: list[str] = []
        with (
            patch.object(cleanup, "_apply_mutation", side_effect=partial_apply),
            patch.object(
                cleanup,
                "_restore_backup",
                return_value="synthetic restore failure",
            ),
        ):
            with self.assertRaises(FrontendReferenceError) as raised:
                execute_frontend_reference_cleanup(
                    self.codex_home,
                    evidence,
                    client_inspector=lambda _home: (),
                    phase_callback=phases.append,
                )
        self.assertEqual(phases, ["mutation_started"])
        self.assertTrue(raised.exception.mutation_started)
        self.assertFalse(raised.exception.outcome_known_rolled_back)
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertIn("unknown", str(raised.exception).casefold())

if __name__ == "__main__":
    unittest.main()
