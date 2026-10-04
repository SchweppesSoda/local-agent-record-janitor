from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_agent_record_janitor import orca_journal_cleanup as journal, frozen_sqlite
from tests.orca_support import create_profile, SENTINEL


class OrcaJournalCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "profile"
        create_profile(self.root, accounts=2, tabs="recorded")
        self.path = self.root / journal.RELATIVE
        self.selected = {"orca_fixture_01"}
        self.key = "client\0" + "1700000000000-" + "a" * 32
        self.row = {"callerKey": "client", "operationId": "1700000000000-" + "a" * 32,
            "operationTimestamp": 1700000000000, "recordedAt": 1700000000000,
            "expiresAt": 1800000000000, "fingerprint": SENTINEL,
            "outcome": {"status": "succeeded", "sessionId": "orca_fixture_01", "launch": {"private": SENTINEL}}}
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            for sid in ("orca_fixture_01", "orca_fixture_02"):
                db.execute("INSERT INTO journal_rows VALUES (?,'epoch',1,1,?)", (sid, SENTINEL))
                db.execute("INSERT INTO journal_sessions VALUES (?,'workspace-fixture','epoch')", (sid,))
                db.execute("INSERT INTO journal_repairs VALUES (?,'epoch',1,1)", (sid,))
                db.execute("INSERT INTO journal_imports VALUES (?,'epoch',1)", (sid,))
                db.execute("INSERT INTO journal_set_aside VALUES (?,'epoch',1)", (sid,))
            db.execute("INSERT INTO agent_session_operations VALUES (?,?)", (self.key, json.dumps(self.row)))
            db.execute("INSERT INTO agent_session_retired_claim_keys VALUES ('keep-key',1)")
            db.commit()

    def test_private_plan_and_exact_wal_transaction_preserve_neighbor_and_replay_key(self):
        before = self.path.read_bytes()
        evidence = journal.freeze(self.root, self.selected)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(SENTINEL, json.dumps(evidence))
        phases = []
        journal.apply(evidence, self.selected, phase_callback=phases.append)
        self.assertEqual(phases, ["mutation_started"])
        self.assertEqual(journal.remaining(evidence, self.selected), 0)
        with closing(sqlite3.connect(self.path)) as db:
            for table in journal.SESSION_TABLES:
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table} WHERE session_id='orca_fixture_01'").fetchone()[0], 0)
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table} WHERE session_id='orca_fixture_02'").fetchone()[0], 1)
            value = json.loads(db.execute("SELECT row_json FROM agent_session_operations WHERE operation_key=?", (self.key,)).fetchone()[0])
            self.assertEqual(value["outcome"], {"status": "unknown"})
            self.assertEqual(value["expiresAt"], self.row["expiresAt"])
            self.assertEqual(value["operationId"], self.row["operationId"])
            self.assertNotIn(SENTINEL, json.dumps(value))
            self.assertEqual(db.execute("SELECT value FROM agent_session_store_meta WHERE key='session_tabs_recorded'").fetchone()[0], "ignored")
            self.assertEqual(db.execute("SELECT * FROM agent_session_retired_claim_keys").fetchall(), [("keep-key", 1)])

    def test_unknown_trigger_and_drift_cannot_change_the_frozen_transaction(self):
        evidence = journal.freeze(self.root, self.selected)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE journal_rows SET row_json='outside writer' WHERE session_id='orca_fixture_02'")
            db.commit()
        phases = []
        with self.assertRaisesRegex(frozen_sqlite.FrozenSQLiteError, "before_state_changed"):
            journal.apply(evidence, self.selected, phase_callback=phases.append)
        self.assertEqual(phases, [])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER surprise AFTER DELETE ON agent_session_records BEGIN DELETE FROM journal_rows; END")
        with self.assertRaisesRegex(journal.OrcaFrontendError, "writer_schema_unverified"):
            journal.freeze(self.root, self.selected)

    def test_checkpoint_failure_preserves_rows_and_after_hash_protects_unselected_data(self):
        evidence = journal.freeze(self.root, self.selected)
        def fail(_):
            raise OSError("synthetic checkpoint interruption")
        with self.assertRaises(OSError):
            journal.apply(evidence, self.selected, phase_callback=fail)
        self.assertGreater(journal.remaining(evidence, self.selected), 0)
        journal.apply(evidence, self.selected, phase_callback=lambda _: None)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE journal_rows SET row_json='changed' WHERE session_id='orca_fixture_02'")
            db.commit()
        with self.assertRaisesRegex(frozen_sqlite.FrozenSQLiteError, "after_state_unverified"):
            journal.remaining(evidence, self.selected)
        self.assertEqual(journal.remaining(evidence, self.selected, terminal_verified=True), 0)

    def test_unselected_operation_retains_replay_and_only_drops_selected_replacement(self):
        value = {**self.row, "outcome": {"status": "succeeded", "sessionId": "orca_fixture_02",
            "conversationCommand": {"command": "clear", "state": "completed", "replacementSessionId": "orca_fixture_01"},
            "rewind": {"itemId": "keep-item", "epoch": "keep-epoch"}}}
        after = journal.operation(self.key, value, self.selected)
        self.assertEqual(after["fingerprint"], value["fingerprint"])
        self.assertEqual(after["outcome"], {**value["outcome"], "conversationCommand": {"command": "clear", "state": "completed"}})
        self.assertEqual(journal.operation(self.key, after, self.selected), after)

    def test_unrelated_known_launch_is_preserved_and_selected_secondary_launch_is_tombstoned(self):
        launch = {"outcome": {"kind": "structured", "sessionId": "orca_fixture_02", "handle": "keep-handle"},
            "worktreeId": "worktree", "receipt": {"mode": "structured", "preferred": "structured", "reason": "user_default", "detail": ""}}
        value = {**self.row, "outcome": {"status": "succeeded", "sessionId": "orca_fixture_02", "launch": launch}}
        self.assertEqual(journal.operation(self.key, value, self.selected), value)
        launch["outcome"]["sessionId"] = "orca_fixture_01"
        self.assertEqual(journal.operation(self.key, value, self.selected)["outcome"], {"status": "unknown"})

    def test_invalid_operation_id_cannot_be_written_as_an_unreadable_tombstone(self):
        value = {**self.row, "operationId": "invalid"}
        with self.assertRaisesRegex(journal.OrcaFrontendError, "ledger_shape_unverified"):
            journal.operation("client\0invalid", value, self.selected)


if __name__ == "__main__":
    unittest.main()
