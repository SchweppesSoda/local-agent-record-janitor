from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_agent_record_janitor.adapters import NativeIntegrityAdapter
from local_agent_record_janitor.inventory import build_session_catalog
from tests.support import create_thread_index
import tests.test_cindy_codex_operations as cindy_tests


class CindyEmptyCompletionTests(unittest.TestCase):
    def test_last_native_records_complete_and_fresh_verify_keeps_completion(self):
        fixture = cindy_tests.CindyCodexOperationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plan = fixture.plan(record_ids=("delete", "keep"))
        result = fixture.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertEqual(set(fixture.calls), {"delete", "keep"})
        self.assertTrue(all(not path.exists() for path in fixture.paths.values()))
        with closing(sqlite3.connect(fixture.database)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 3)
            self.assertEqual(database.execute(
                "SELECT COUNT(*) FROM sessions WHERE sdk_session_id IS NOT NULL"
            ).fetchone()[0], 0)
        verified = fixture.coordinator().verify_operation(
            operation_id=plan["operation_id"], plan_path=Path(plan["plan_path"]),
            adapters=(fixture.adapter(),),
        )
        self.assertEqual(verified["goal_status"], "complete", verified)

    def test_empty_store_coverage_requires_readable_supported_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "native"
            create_thread_index(home, [])
            adapter = NativeIntegrityAdapter(codex_home=home)
            complete = build_session_catalog((adapter,))
            self.assertFalse(complete.errors)
            self.assertEqual(complete.scanned_native_homes, (home,))
            with closing(sqlite3.connect(home / "state_5.sqlite")) as database:
                database.execute("DROP TABLE thread_spawn_edges")
                database.commit()
            unsupported = build_session_catalog((adapter,))
            self.assertTrue(unsupported.errors)
            self.assertEqual(unsupported.scanned_native_homes, ())
            (home / "state_5.sqlite").write_bytes(b"unreadable database")
            corrupt = build_session_catalog((adapter,))
            self.assertTrue(corrupt.errors)
            self.assertEqual(corrupt.scanned_native_homes, ())

    def test_nonexistent_home_is_not_successful_empty_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "missing"
            catalog = build_session_catalog((NativeIntegrityAdapter(codex_home=home),))
            self.assertEqual(catalog.scanned_native_homes, ())

    def test_existing_home_without_state_database_is_not_successful_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            catalog = build_session_catalog((NativeIntegrityAdapter(codex_home=home),))
            self.assertEqual(catalog.scanned_native_homes, ())


if __name__ == "__main__":
    unittest.main()
