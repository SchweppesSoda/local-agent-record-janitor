from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_agent_record_janitor import orca_profile_cleanup as profile, frozen_sqlite
from local_agent_record_janitor.orca_journal_cleanup import OrcaFrontendError
from tests.test_orca_frontend_json import fixture


class OrcaProfileCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.relative = "profile-state.db"
        self.path = self.root / self.relative
        self.state = fixture()
        self.before_hash, self.after_hash = "a" * 64, "b" * 64
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("PRAGMA user_version=3;" + profile.DDL)
            db.execute("INSERT INTO profile_state_meta VALUES ('profile_id','synthetic-profile')")
            db.execute("INSERT INTO profile_state_meta VALUES ('revision','1')")
            db.execute("INSERT INTO profile_state_automation_runs_meta VALUES ('automationRuns','document',1,0,0,'')")
            for name, value in self.state.items():
                payload = json.dumps(value)
                db.execute("INSERT INTO profile_state_documents VALUES (?,?,1,1,10,?)", (name, payload, profile.sha(payload)))
            db.commit()

    def freeze(self):
        return profile.freeze(self.root, self.relative, {"selected"}, {"tab-selected"}, set(),
            timestamp=123, json_hashes={self.before_hash: self.after_hash})

    def apply(self, evidence):
        profile.apply(evidence, {"selected"}, {"tab-selected"}, set(), timestamp=123,
            json_hashes={self.before_hash: self.after_hash}, phase_callback=lambda _: None)

    def test_selected_domains_share_new_revision_and_other_payloads_remain_byte_identical(self):
        with closing(sqlite3.connect(self.path)) as db:
            preserved = db.execute("SELECT * FROM profile_state_documents WHERE domain IN ('settings','schemaVersion') ORDER BY domain").fetchall()
            marker = db.execute("SELECT * FROM profile_state_automation_runs_meta").fetchall()
        before = self.path.read_bytes()
        evidence = self.freeze()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("PRESERVE_SECRET", json.dumps(evidence))
        self.apply(evidence)
        self.assertEqual(profile.remaining(evidence, {"selected"}, {"tab-selected"}, set()), 0)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT value FROM profile_state_meta WHERE key='revision'").fetchone(), ("2",))
            self.assertEqual(db.execute("SELECT * FROM profile_state_documents WHERE domain IN ('settings','schemaVersion') ORDER BY domain").fetchall(), preserved)
            self.assertEqual(db.execute("SELECT * FROM profile_state_automation_runs_meta").fetchall(), marker)
            rows = db.execute("SELECT payload,revision,updated_at,content_hash FROM profile_state_documents WHERE domain='workspaceSession'").fetchone()
            self.assertEqual(rows[1:3], (2, 123))
            self.assertEqual(hashlib.sha256(rows[0].encode()).hexdigest(), rows[3])
            self.assertEqual(json.loads(rows[0])["unifiedTabs"]["worktree"][0]["entityId"], "other")

    def test_pending_legacy_acceptance_tracks_exact_rewritten_json_without_dropping_marker(self):
        with closing(sqlite3.connect(self.path)) as db:
            value = {"jsonHash": self.before_hash, "acceptedRevision": 1,
                     "pending": {"jsonHash": self.before_hash, "acceptedRevision": 1}}
            db.execute("INSERT INTO profile_state_meta VALUES ('legacy_json_acceptance',?)", (json.dumps(value),))
            db.commit()
        evidence = self.freeze()
        self.apply(evidence)
        with closing(sqlite3.connect(self.path)) as db:
            result = json.loads(db.execute("SELECT value FROM profile_state_meta WHERE key='legacy_json_acceptance'").fetchone()[0])
        self.assertEqual(result, {"jsonHash": self.after_hash, "acceptedRevision": 2,
                                 "pending": {"jsonHash": self.after_hash, "acceptedRevision": 2}})

    def test_changed_shared_domain_or_unproved_restore_hash_refuses(self):
        evidence = self.freeze()
        with closing(sqlite3.connect(self.path)) as db:
            payload = '{"auth":"CHANGED"}'
            db.execute("UPDATE profile_state_documents SET payload=?,content_hash=? WHERE domain='settings'", (payload, profile.sha(payload)))
            db.commit()
        with self.assertRaisesRegex(frozen_sqlite.FrozenSQLiteError, "before_state_changed"):
            self.apply(evidence)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO profile_state_meta VALUES ('legacy_json_acceptance',?)",
                (json.dumps({"jsonHash": "c" * 64, "acceptedRevision": 1}),))
            db.commit()
        with self.assertRaisesRegex(OrcaFrontendError, "restore_source_unverified"):
            self.freeze()


if __name__ == "__main__":
    unittest.main()
