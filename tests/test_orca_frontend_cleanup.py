from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_agent_record_janitor import orca_frontend as frontend, orca_profile_cleanup as profile
from local_agent_record_janitor.orca_journal_cleanup import OrcaFrontendError
from tests.orca_support import create_profile, make_record, SENTINEL
from tests.test_orca_frontend_json import fixture


class OrcaFrontendCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "Orca"
        self.homes = create_profile(self.root, tabs="recorded")
        self.selected = {"orca_fixture_01"}
        self.profile = self.root / "profiles/local-default"
        self.profile.mkdir(parents=True)
        state = json.loads(json.dumps(fixture()).replace("selected", "orca_fixture_01"))
        self.state = state
        self.raw = (json.dumps(state) + "\n").encode()
        for name in ("orca-data.json", "orca-data.json.bak.0", "orca-data.json.sqlite-export.7.json"):
            (self.profile / name).write_bytes(self.raw)
        (self.root / "orca-data.json").write_bytes(self.raw)
        self.databases = [self.profile / name for name in (
            "profile-state.db", "profile-state.db.backup.1700000000000-11111111-1111-4111-8111-111111111111.db")]
        for path in self.databases:
            with closing(sqlite3.connect(path)) as db:
                db.executescript("PRAGMA user_version=3;" + profile.DDL)
                db.executemany("INSERT INTO profile_state_meta VALUES (?,?)", [("profile_id", "local-default"), ("revision", "1"),
                    ("legacy_json_acceptance", json.dumps({"jsonHash": profile.sha(self.raw.decode()), "acceptedRevision": 1}))])
                db.execute("INSERT INTO profile_state_automation_runs_meta VALUES ('automationRuns','array',1,1,10,?)", (profile.sha("[]"),))
                for key, value in state.items():
                    payload = json.dumps(value)
                    db.execute("INSERT INTO profile_state_documents VALUES (?,?,1,1,10,?)", (key, payload, profile.sha(payload)))
                db.commit()
        legacy = {"schemaVersion": 2, "hostId": "old-host", "records": {
            sid: make_record(home, sid) for sid, home in zip(("orca_fixture_01", "orca_fixture_02"), self.homes)},
            "operations": {}, "unusableRecords": {}, "retiredClaimKeys": [],
            "sessionTabs": [{"tabId": "tab-orca_fixture_01", "sessionId": "orca_fixture_01"}]}
        (self.root / "agent-sessions").mkdir()
        for name in ("agent-sessions.json", "agent-sessions.json.bak"):
            (self.root / "agent-sessions" / name).write_text(json.dumps(legacy), encoding="utf-8")

    def freeze(self):
        return frontend.freeze(self.root, self.selected, timestamp=123)

    def test_all_current_backup_export_and_legacy_sources_are_cleared_and_cold_verified(self):
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        evidence = self.freeze()
        self.assertNotIn(SENTINEL, json.dumps(evidence))
        self.assertEqual({str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertEqual(len(evidence["profile_databases"]), 2)
        self.assertEqual(len(evidence["json_files"]), 6)
        phases = []
        frontend.apply(evidence, phase_callback=phases.append, require_closed=lambda: None)
        self.assertEqual(phases[-1], "verified")
        self.assertEqual(frontend.remaining(json.loads(json.dumps(evidence))), 0)
        for path in self.databases:
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT value FROM profile_state_meta WHERE key='revision'").fetchone(), ("2",))
                workspace = json.loads(db.execute("SELECT payload FROM profile_state_documents WHERE domain='workspaceSession'").fetchone()[0])
                self.assertEqual([tab["entityId"] for tab in workspace["unifiedTabs"]["worktree"]], ["other"])
                self.assertEqual(json.loads(db.execute("SELECT payload FROM profile_state_documents WHERE domain='settings'").fetchone()[0]), self.state["settings"])
        for item in evidence["json_files"]:
            value = json.loads((self.root / item["evidence"]["before"]["path"]).read_bytes())
            if item["family"] == "legacy_records":
                self.assertNotIn("orca_fixture_01", value["records"])
                self.assertIn("orca_fixture_02", value["records"])

    def test_new_restore_source_and_running_writer_block_before_any_mutation(self):
        evidence = self.freeze()
        phases = []
        def running():
            raise OrcaFrontendError("writer_running")
        with self.assertRaisesRegex(OrcaFrontendError, "writer_running"):
            frontend.apply(evidence, phase_callback=phases.append, require_closed=running)
        (self.profile / "orca-data.json.bak.1").write_bytes(self.raw)
        with self.assertRaisesRegex(OrcaFrontendError, "evidence_changed"):
            frontend.apply(evidence, phase_callback=phases.append, require_closed=lambda: None)
        self.assertEqual(phases, [])

    def test_partial_interruption_is_readonly_unknown_and_never_retried(self):
        evidence = self.freeze()
        phases = []
        def checkpoint(phase):
            phases.append(phase)
            if len(phases) == 2:
                raise OSError("synthetic interrupted child")
        with self.assertRaises(OSError):
            frontend.apply(evidence, phase_callback=checkpoint, require_closed=lambda: None)
        self.assertGreater(frontend.remaining(evidence), 0)
        with self.assertRaisesRegex(OrcaFrontendError, "evidence_changed"):
            frontend.apply(evidence, phase_callback=lambda _: None, require_closed=lambda: None)

    def test_profile_identity_mismatch_and_reintroduced_backup_are_visible(self):
        with closing(sqlite3.connect(self.databases[-1])) as db:
            db.execute("UPDATE profile_state_meta SET value='wrong-profile' WHERE key='profile_id'")
            db.commit()
        with self.assertRaisesRegex(OrcaFrontendError, "profile_identity_mismatch"):
            self.freeze()
        with closing(sqlite3.connect(self.databases[-1])) as db:
            db.execute("UPDATE profile_state_meta SET value='local-default' WHERE key='profile_id'")
            db.commit()
        evidence = self.freeze()
        frontend.apply(evidence, phase_callback=lambda _: None, require_closed=lambda: None)
        (self.profile / "orca-data.json.bak.0").write_bytes(self.raw)
        self.assertGreater(frontend.remaining(evidence, terminal_verified=True), 0)

    def test_hook_namespaces_temporaries_and_authority_are_all_checked(self):
        from tests.orca_support import CURRENT_ID
        hooks = self.root / "agent-hooks/com.stablyai.orca"
        hooks.mkdir(parents=True)
        status = hooks / "last-status.json"
        value = {"version": 2, "entries": {}, "authorityCommitments": {}}
        status.write_text(json.dumps(value), encoding="utf-8")
        evidence = self.freeze()
        self.assertIn("agent_hooks_namespaces_not_covered", {e["message"] for e in evidence["covered_errors"]})
        temp = hooks / ".last-status-42-11111111-1111-4111-8111-111111111111.tmp"
        for container, entry in (("entries", {"paneKey": "unrelated:pane", "providerSession": {"id": CURRENT_ID}}),
                                 ("authorityCommitments", {"paneKey": "tab-orca_fixture_01:pane"})):
            with self.subTest(container=container):
                changed = {"version": 2, "entries": {}, "authorityCommitments": {}}
                changed[container][entry["paneKey"]] = entry
                temp.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(OrcaFrontendError, "terminal_hook_binding_unverified"):
                    self.freeze()
                temp.unlink()
        (self.root / "agent-hooks/com.stablyai.orca.dev.1234567890").mkdir()
        phases = []
        with self.assertRaisesRegex(OrcaFrontendError, "frontend_evidence_changed"):
            frontend.apply(evidence, phase_callback=phases.append, require_closed=lambda: None)
        self.assertEqual(phases, [])

    def test_unrelated_hook_spool_and_endpoint_survive_unchanged(self):
        hooks = self.root / "agent-hooks/com.stablyai.orca"
        (hooks / "spool").mkdir(parents=True)
        endpoint = hooks / "endpoint.env"
        endpoint.write_text("\n".join("ORCA_AGENT_HOOK_" + key + "=" + value for key, value in
            (("PORT", "1234"), ("TOKEN", "private-token"), ("ENV", "production"), ("VERSION", "1"),
             ("TRANSPORT", "raw-json-v1"))), encoding="utf-8")
        cmd = hooks / "endpoint.cmd"
        cmd.write_text("\n".join("set " + line for line in endpoint.read_text().splitlines()), encoding="utf-8")
        spool = hooks / "spool/pane-unrelated.jsonl"
        spool.write_text("\n" + json.dumps({"paneKey": "unrelated:pane", "source": "codex", "receivedAt": 100,
            "payload": {"session_id": "unselected", "private": "retained"}}) + "\n", encoding="utf-8")
        before = endpoint.read_bytes(), cmd.read_bytes(), spool.read_bytes()
        evidence = self.freeze()
        self.assertNotIn("private-token", json.dumps(evidence))
        frontend.apply(evidence, phase_callback=lambda _: None, require_closed=lambda: None)
        self.assertEqual(frontend.remaining(evidence), 0)
        self.assertEqual((endpoint.read_bytes(), cmd.read_bytes(), spool.read_bytes()), before)

    def test_unknown_table_in_owned_legacy_journal_is_never_removed(self):
        record = frontend._record_metadata(self.root, "orca_fixture_01", make_record(self.homes[0], "orca_fixture_01"))
        parent = self.root / "agent-session-journal" / hashlib.sha256(record["workspace_id"].encode()).hexdigest()[:32]
        directory = parent / hashlib.sha256(record["session_id"].encode()).hexdigest()[:32]
        directory.mkdir(parents=True)
        with closing(sqlite3.connect(directory / "journal.db")) as db:
            db.execute("CREATE TABLE unrelated (private TEXT)")
            db.commit()
        with self.assertRaisesRegex(OrcaFrontendError, "per_session_journal_schema_unverified"):
            self.freeze()
        self.assertTrue((directory / "journal.db").is_file())


if __name__ == "__main__":
    unittest.main()
