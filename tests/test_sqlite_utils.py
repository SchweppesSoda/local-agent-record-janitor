from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from local_agent_record_janitor.sqlite_utils import connect_readonly, table_exists


class ReadOnlySQLiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Path(self.temporary_directory.name) / "frontend.db"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE records (id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO records (id) VALUES ('original')")
            connection.commit()

    def test_connection_can_read_and_detect_tables(self) -> None:
        with closing(connect_readonly(self.database)) as connection:
            self.assertTrue(table_exists(connection, "records"))
            self.assertFalse(table_exists(connection, "missing"))
            value = connection.execute("SELECT id FROM records").fetchone()["id"]
        self.assertEqual(value, "original")

    def test_connection_rejects_writes(self) -> None:
        with closing(connect_readonly(self.database)) as connection:
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                connection.execute("INSERT INTO records (id) VALUES ('unexpected')")

        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute("SELECT id FROM records").fetchall()
        self.assertEqual(rows, [("original",)])


if __name__ == "__main__":
    unittest.main()
