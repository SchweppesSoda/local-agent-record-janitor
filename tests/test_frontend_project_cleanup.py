from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.frontend_project_cleanup import (
    AionUIProjectRowEvidence,
    FrontendProjectCleanupError,
    FrontendProjectGuardError,
    execute_aionui_project_cleanup,
)
from local_agent_record_janitor.sqlite_identity import (
    row_fingerprint,
    schema_fingerprint,
    table_schema,
)


class FrontendProjectCleanupTests(unittest.TestCase):
    def _database(self, root: Path, size: int) -> Path:
        database = root / f"aionui-{size}.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    working_dir TEXT
                );
                CREATE TABLE acp_session (
                    conversation_id TEXT NOT NULL,
                    session_id TEXT
                );
                """
            )
            connection.executemany(
                "INSERT INTO conversations (id, title, working_dir) VALUES (?, ?, ?)",
                (
                    (f"orphan-{index}", f"title-{index}", str(root))
                    for index in range(size)
                ),
            )
            connection.commit()
        return database

    def _evidence(self, database: Path) -> list[AionUIProjectRowEvidence]:
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            schema = table_schema(connection, "conversations")
            columns = tuple(str(item["name"]) for item in schema)
            schema_hash = schema_fingerprint(schema)
            rows = connection.execute(
                "SELECT * FROM conversations ORDER BY id"
            ).fetchall()
        return [
            AionUIProjectRowEvidence(
                database=database,
                conversation_id=str(row["id"]),
                schema_fingerprint=schema_hash,
                row_fingerprint=row_fingerprint(row, columns),
                expected_zero_acp_session_refs=True,
            )
            for row in rows
        ]

    def test_one_set_guard_delete_verify_for_1_10_100_rows(self) -> None:
        for size in (1, 10, 100):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temporary:
                database = self._database(Path(temporary), size)
                evidence = self._evidence(database)
                observed: list[str] = []
                result = execute_aionui_project_cleanup(
                    evidence,
                    query_observer=observed.append,
                )
                self.assertEqual(result.status, "deleted")
                self.assertEqual(result.deleted_row_count, size)
                self.assertEqual(result.affected_rows, size)
                self.assertEqual(result.guard_query_count, 1)
                self.assertEqual(result.verify_query_count, 1)
                self.assertEqual(result.transaction_count, 1)
                self.assertEqual(
                    sum('FROM "conversations"' in query for query in observed),
                    2,
                )
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                        0,
                    )

    def test_reference_or_row_drift_is_guarded_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary), 1)
            evidence = self._evidence(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "INSERT INTO acp_session (conversation_id, session_id) VALUES (?, ?)",
                    (evidence[0].conversation_id, "still-live"),
                )
                connection.commit()
            phases: list[str] = []
            with self.assertRaises(FrontendProjectGuardError) as raised:
                execute_aionui_project_cleanup(
                    evidence,
                    phase_callback=phases.append,
                )
            self.assertFalse(raised.exception.mutation_started)
            self.assertEqual(phases, [])
            self.assertFalse(list(Path(temporary).glob(".larj-project-*")))
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                    1,
                )

    def test_sql_failure_after_marker_is_known_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary), 1)
            evidence = self._evidence(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER fail_project_delete
                    BEFORE DELETE ON conversations
                    BEGIN
                        SELECT RAISE(ABORT, 'fixture delete failure');
                    END;
                    """
                )
                connection.commit()
            with self.assertRaises(FrontendProjectCleanupError) as raised:
                execute_aionui_project_cleanup(evidence)
            self.assertTrue(raised.exception.mutation_started)
            self.assertTrue(raised.exception.outcome_known_rolled_back)
            self.assertFalse(raised.exception.outcome_unknown)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                    1,
                )

    def test_commit_verification_failure_is_unknown_and_keeps_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary), 1)
            evidence = self._evidence(database)
            with patch(
                "local_agent_record_janitor.frontend_project_cleanup.verify_aionui_project_rows",
                side_effect=RuntimeError("verification unavailable"),
            ):
                with self.assertRaises(FrontendProjectCleanupError) as raised:
                    execute_aionui_project_cleanup(evidence)
            self.assertTrue(raised.exception.mutation_started)
            self.assertTrue(raised.exception.outcome_unknown)
            self.assertFalse(raised.exception.outcome_known_rolled_back)
            backups = list(Path(temporary).glob(".larj-project-*/database.sqlite"))
            self.assertEqual(len(backups), 1)
            backups[0].unlink(missing_ok=True)
            backups[0].parent.rmdir()


if __name__ == "__main__":
    unittest.main()
