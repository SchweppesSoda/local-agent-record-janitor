from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.orca_discovery import (
    OrcaDiscoveryError, default_orca_root, local_orca_path, prove_account_home,
    prove_runtime_home, reverse_account_profile,
)
from local_agent_record_janitor.orca_metadata import OrcaMetadataError, parse_orca_record, read_orca_journal
from tests.orca_support import SENTINEL, create_profile, make_record


class OrcaMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True) / "orca"
        self.homes = create_profile(self.root)
        self.database = self.root / "agent-session-journal.db"

    def test_tab_coverage_and_empty_chain_keep_all_handle_metadata_without_writes(self):
        for tabs in ("recorded", "recorded-empty", "unknown", "unrecorded"):
            with self.subTest(tabs=tabs):
                root = self.root.parent / tabs
                create_profile(root, tabs=tabs)
                before = {p: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                          for p in root.rglob("*") if p.is_file()}
                records, errors = read_orca_journal(root / self.database.name, root)
                self.assertEqual((len(records), sum(len(r.handles) for r in records)), (3, 4))
                self.assertEqual([e.message for e in errors], [] if tabs.startswith("recorded") else ["session_tabs_coverage_unknown"])
                self.assertNotIn(SENTINEL, repr(records) + repr(errors))
                self.assertEqual(before, {p: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                                          for p in root.rglob("*") if p.is_file()})

    def test_view_cannot_expand_into_chat_body(self):
        record = make_record(self.homes[0])
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE agent_session_records")
            connection.execute("INSERT INTO journal_rows VALUES (?, 'fixture', 0, 0, ?)", (record["sessionId"], json.dumps(record)))
            connection.execute("CREATE VIEW agent_session_records AS SELECT seq AS rowid,session_id,row_json AS record_json FROM journal_rows")
            connection.commit()
        with patch("local_agent_record_janitor.orca_metadata.connect_readonly", wraps=self._authorized_connection) as connect:
            records, errors = read_orca_journal(self.database, self.root)
        self.assertTrue(connect.called)
        self.assertEqual(records, ())
        self.assertEqual(errors[0].message, "journal_metadata_schema_incomplete")
        self.assertEqual(self.body_reads, [])

    def _authorized_connection(self, path):
        from local_agent_record_janitor.sqlite_utils import connect_readonly
        connection = connect_readonly(path)
        self.body_reads = []
        def authorizer(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == "journal_rows":
                self.body_reads.append(column)
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorizer)
        return connection

    def test_bad_row_does_not_hide_independent_valid_records_or_echo_json(self):
        bad_rows = ("{bad", '{"schemaVersion":2,"secret":' + "9" * 5000 + "}",
                    json.dumps({"schemaVersion": 3, "secret": SENTINEL}))
        for raw in bad_rows:
            with self.subTest(raw=raw[:20]):
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute("INSERT OR REPLACE INTO agent_session_records VALUES ('bad_fixture', ?)", (raw,))
                    connection.commit()
                records, errors = read_orca_journal(self.database, self.root)
                self.assertEqual(len(records), 3)
                self.assertEqual(len(errors), 1)
                self.assertNotIn(SENTINEL, json.dumps([e.to_dict() for e in errors]))

    def test_missing_unique_record_identity_is_unsupported_schema(self):
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute("SELECT * FROM agent_session_records").fetchall()
            connection.execute("DROP TABLE agent_session_records")
            connection.execute("CREATE TABLE agent_session_records(session_id TEXT,record_json TEXT)")
            connection.executemany("INSERT INTO agent_session_records VALUES (?, ?)", [*rows, rows[0]])
            connection.commit()
        records, errors = read_orca_journal(self.database, self.root)
        self.assertEqual(records, ())
        self.assertEqual(errors[0].message, "journal_metadata_schema_incomplete")

    def test_required_shape_chain_and_optional_bounds(self):
        base = make_record(self.homes[0])
        mutations = (
            lambda r: r.update(schemaVersion=True),
            lambda r: r["lease"].update(sessionId="another-fixture"),
            lambda r: r["providerHandleChain"][1].update(origin="resumed"),
            lambda r: r["providerHandleChain"][1].update(mintedAtFence=True),
            lambda r: r.update(launchArgs=[123]),
            lambda r: r.update(conversationName={"secret": SENTINEL}),
            lambda r: r.update(conversationName="a  b"),
            lambda r: r["providerHandleChain"][0]["handle"].update(threadId="😀" * 300),
            lambda r: (r["providerHandleChain"][0]["handle"].update(threadId="x" * 512),
                       r["providerHandleChain"][1].update(forkedFromKey='codex:"' + "x" * 512 + '"')),
        )
        for mutation in mutations:
            record = copy.deepcopy(base)
            mutation(record)
            with self.subTest(mutation=mutation), self.assertRaises(OrcaMetadataError):
                parse_orca_record(record["sessionId"], record)
        base.update(launchArgs=["resume", SENTINEL], conversationName="valid name")
        self.assertNotIn(SENTINEL, repr(parse_orca_record(base["sessionId"], base)))

    def test_schema_and_metadata_are_read_in_one_transaction(self):
        from local_agent_record_janitor.sqlite_utils import connect_readonly
        trace = []
        def traced(path):
            connection = connect_readonly(path)
            connection.set_trace_callback(trace.append)
            return connection
        with patch("local_agent_record_janitor.orca_metadata.connect_readonly", side_effect=traced):
            read_orca_journal(self.database, self.root)
        self.assertLess(trace.index("BEGIN"), trace.index("PRAGMA user_version"))


class OrcaDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True) / "orca"
        self.home = create_profile(self.root, accounts=1)[0]

    def test_account_marker_and_runtime_metadata_have_distinct_proofs(self):
        self.assertEqual(prove_account_home(self.root, self.home), self.home)
        self.assertEqual(reverse_account_profile(self.home), self.root)
        runtime = self.root / "codex-runtime-home" / "home"
        (runtime / "sessions").mkdir(parents=True)
        self.assertIsNone(reverse_account_profile(runtime))
        self.assertEqual(prove_runtime_home(self.root, str(runtime), host="local", wsl_distro=None), runtime)
        (self.home / ".orca-managed-home").write_text("wrong-id", encoding="utf-8")
        with self.assertRaises(OrcaDiscoveryError):
            prove_account_home(self.root, self.home)

    def test_foreign_location_is_rejected_before_path_or_fs_access(self):
        invalid = [(str(self.home), "ssh:remote", "local"), (str(self.home), "local", "wsl")]
        invalid += [(raw, "local", "local") for raw in
                    (("/home/remote", "\\home\\remote", "\\\\server\\share\\home") if os.name == "nt" else ("C:\\remote\\home", "\\\\server\\share"))]
        for raw, host, namespace in invalid:
            with self.subTest(raw=raw, host=host), patch("local_agent_record_janitor.orca_discovery.Path", side_effect=AssertionError("Path reached")):
                with self.assertRaises(ValueError):
                    local_orca_path(raw, host=host, path_namespace=namespace)

    def test_empty_environment_falls_back_but_relative_and_whitespace_do_not(self):
        fallback = default_orca_root(appdata=self.root.parent, environ={})
        self.assertEqual(default_orca_root(appdata=self.root.parent, environ={"ORCA_USER_DATA_PATH": ""}), fallback)
        for raw in ("  ", "relative/profile"):
            with self.assertRaises(OrcaDiscoveryError):
                default_orca_root(appdata=self.root.parent, environ={"ORCA_USER_DATA_PATH": raw})
        self.assertEqual(default_orca_root(environ={"ORCA_USER_DATA_PATH": str(self.root)}), self.root)

    def test_marker_read_failure_is_not_missing_evidence_success(self):
        with patch("pathlib.Path.open", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                prove_account_home(self.root, self.home)
