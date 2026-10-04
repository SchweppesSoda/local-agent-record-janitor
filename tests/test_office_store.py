from __future__ import annotations

from contextlib import closing
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from local_agent_record_janitor.cli import main
from local_agent_record_janitor.office_store import OfficeAdapter, PROFILES, SDK_DIRECTORY, build_inventory, read_database
from local_agent_record_janitor.sqlite_utils import connect_readonly

SDK_ID = "11111111-1111-4111-8111-111111111111"
PRIVATE = "OFFICE_PRIVATE_MESSAGE_AND_CREDENTIAL_SENTINEL"


class OfficeStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.appdata = self.root / "appdata"
        for patcher in (patch("pathlib.Path.home", return_value=self.root), patch.dict(os.environ,
            {"APPDATA": str(self.appdata), "XDG_CONFIG_HOME": str(self.appdata), "ORCA_USER_DATA_PATH": "",
             "CODEX_HOME": str(self.root / "absent-codex"), "PI_CODING_AGENT_DIR": str(self.root / "absent-pi"),
             "CLAUDE_CONFIG_DIR": str(self.root / "absent-claude")})):
            patcher.start(); self.addCleanup(patcher.stop)
        self.writer = Mock(side_effect=AssertionError("Office inventory must not launch a writer"))

    def fixture(self, client, *, profile=None, empty=False):
        root = profile or self.appdata / PROFILES[client][0]
        database = root / "data" / "agents.db"
        database.parent.mkdir(parents=True)
        schema = (Path(__file__).parent / "fixtures" / (client + "_cn_schema.sql")).read_text(encoding="utf-8")
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(schema)
            if not empty:
                connection.execute("INSERT INTO projects(id,name,path) VALUES (?,?,?)", ("project-1", PRIVATE, str(self.root / "documents")))
                connection.execute("INSERT INTO chats(id,name,project_id,deleted_at) VALUES (?,?,?,?)", ("chat-1", PRIVATE, "project-1", 123))
                connection.execute("INSERT INTO sub_chats(id,chat_id,session_id,messages) VALUES (?,?,?,?)", ("child-1", "chat-1", SDK_ID, PRIVATE))
                connection.execute("INSERT INTO messages(id,message_id,chat_id,sub_chat_id,sequence,role,parts,searchable_text) VALUES (?,?,?,?,?,?,?,?)",
                    ("message-1", "native-message-1", "chat-1", "child-1", 1, "user", PRIVATE, PRIVATE))
            connection.commit()
        return root

    def invoke(self, *args):
        out, errors = StringIO(), StringIO()
        code = main(list(args), stdout=out, stderr=errors, app_server_factory=self.writer, binary_resolver=self.writer)
        value = json.loads(out.getvalue())
        self.assertNotIn(PRIVATE, out.getvalue() + errors.getvalue())
        self.writer.assert_not_called()
        return code, value

    def test_official_schema_fixtures_public_records_read_no_bodies_and_never_modify_sources(self):
        for client in PROFILES:
            with self.subTest(client=client):
                root = self.fixture(client)
                transcript = self.root / SDK_DIRECTORY[client] / "projects" / "encoded" / (SDK_ID + ".jsonl")
                transcript.parent.mkdir(parents=True)
                transcript.write_text(PRIVATE, encoding="utf-8")
                before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (root / "data" / "agents.db", transcript)}
                def safe_connection(path):
                    connection = connect_readonly(path)
                    def authorizer(action, table, column, *_):
                        if action == sqlite3.SQLITE_READ and table != "sqlite_master" and column in {"parts", "searchable_text", "messages", "encrypted_payload", "value", "name"}:
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK
                    connection.set_authorizer(authorizer)
                    return connection
                with patch("local_agent_record_janitor.office_store.connect_readonly", safe_connection):
                    code, result = self.invoke("records", "--client", client, "--" + client + "-root", str(root), "--json")
                self.assertEqual(code, 0, result)
                self.assertEqual(result["inventory_scope"], "database_metadata_and_known_sdk_paths")
                self.assertEqual(result["count"], 1)
                record = result["targets"][0]
                self.assertEqual(record["record_metadata"]["message_count"], 1)
                self.assertEqual(record["record_metadata"]["sub_chats"][0]["session_id"], SDK_ID)
                self.assertEqual(record["record_metadata"]["sdk_artifacts"][0]["path"], str(transcript))
                self.assertFalse(record["capability"]["native_delete"])
                self.assertFalse(record["capability"]["verify"])
                self.assertEqual(record["action_ids"], [])
                self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})

    def test_plan_run_apply_and_cold_queries_keep_inventory_only_without_false_completion(self):
        for client in PROFILES:
            with self.subTest(client=client):
                root = self.fixture(client)
                path = self.root / (client + "-plan.json")
                database = root / "data" / "agents.db"
                database_before = hashlib.sha256(database.read_bytes()).hexdigest()
                code, plan = self.invoke("delete", "plan", "--client", client, "--" + client + "-root", str(root),
                    "--record-id", "chat-1", "--out", str(path), "--json")
                self.assertNotEqual(code, 0)
                self.assertEqual(plan["goal_status"], "blocked", plan)
                self.assertIn("client_capability_limit", {item["blocker_code"] for item in plan["blockers"]})
                self.assertEqual(plan["actions"], [])
                before = path.read_bytes()
                for verb in ("status", "verify"):
                    _, result = self.invoke("operation", verb, "--operation-id", plan["operation_id"], "--plan", str(path), "--json")
                    self.assertEqual(result["goal_status"], "blocked")
                _, result = self.invoke("delete", "apply", "--operation-id", plan["operation_id"], "--plan", str(path),
                    "--authorized-plan-sha256", plan["plan_sha256"], "--clients-closed", "--json")
                self.assertEqual(result["goal_status"], "blocked")
                self.assertFalse(result["mutation_started"])
                self.assertEqual(path.read_bytes(), before)
                _, run = self.invoke("delete", "run", "--client", client, "--" + client + "-root", str(root),
                    "--record-id", "chat-1", "--clients-closed", "--out", str(self.root / (client + "-run.json")), "--json")
                self.assertEqual(run["goal_status"], "blocked")
                self.assertEqual(run["actions"], [])
                self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), database_before)

    def test_default_profiles_same_id_are_isolated_and_cloud_state_is_visible(self):
        client = "qwenwork"
        first = self.fixture(client)
        second = self.fixture(client, profile=self.appdata / PROFILES[client][1])
        with closing(sqlite3.connect(first / "data" / "agents.db")) as connection:
            connection.execute("INSERT INTO rc_session_mappings(sub_chat_id,chat_id,cwd,remote_session_id) VALUES (?,?,?,?)",
                ("child-1", "chat-1", str(self.root), "remote-1"))
            connection.commit()
        code, result = self.invoke("records", "--client", client, "--appdata", str(self.appdata), "--json")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["count"], 2)
        self.assertEqual(len({row["record_key"]["value"] for row in result["targets"]}), 2)
        self.assertEqual({row["classification"] for row in result["targets"]}, {"partial_remote", "unverified"})
        self.assertEqual({row["record_key"]["store"]["path"] for row in result["targets"]}, {str(first), str(second)})

    def test_repeated_profile_identity_does_not_duplicate_records_or_hide_scope_conflicts(self):
        root = self.fixture("qwenwork")
        adapter = OfficeAdapter(client="qwenwork", profile_root=root)
        same = OfficeAdapter(client="qwenwork", profile_root=Path(str(root).upper()) if os.name == "nt" else root)
        inventory = build_inventory((adapter, same), client="qwenwork")
        self.assertEqual(len(inventory.targets), 1)
        self.assertFalse(any(error.blocks_inventory for error in inventory.errors))
        conflicting = OfficeAdapter(client="qwenwork", profile_root=root, sdk_root=self.root / "different-sdk")
        inventory = build_inventory((adapter, conflicting), client="qwenwork")
        self.assertEqual(len(inventory.targets), 1)
        self.assertIn("office_profile_scope_conflict", {error.message for error in inventory.errors})
        code, result = self.invoke("records", "--client", "qwenwork", "--qwenwork-root", str(root),
            "--qwenwork-root", str(same.profile_root), "--json")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["count"], 1)

    def test_unknown_missing_and_inconsistent_sources_never_become_empty_success(self):
        root = self.fixture("qoderwork")
        with closing(sqlite3.connect(root / "data" / "agents.db")) as connection:
            connection.execute("ALTER TABLE chats ADD COLUMN unknown_restore text")
            connection.commit()
        for candidate in (root, self.root / "missing"):
            code, result = self.invoke("records", "--client", "qoderwork", "--qoderwork-root", str(candidate), "--json")
            self.assertNotEqual(code, 0)
            self.assertEqual(result["goal_status"], "blocked")
            self.assertTrue(result["store_errors"])
        wrong = self.fixture("qwenwork")
        with self.assertRaisesRegex(ValueError, "office_schema_unverified"):
            read_database("qoderwork", wrong)

    def test_empty_supported_store_is_complete_metadata_but_never_deletion_qualified(self):
        root = self.fixture("qwenwork", empty=True)
        inventory = build_inventory((OfficeAdapter(client="qwenwork", profile_root=root),), client="qwenwork")
        self.assertEqual(inventory.targets, ())
        self.assertFalse(any(error.blocks_inventory for error in inventory.errors))
        self.assertFalse(inventory.capabilities["qwenwork"].native_delete)

    def test_orphan_auxiliary_references_are_reported_without_losing_readable_chats(self):
        for client in PROFILES:
            with self.subTest(client=client):
                root = self.fixture(client)
                with closing(sqlite3.connect(root / "data" / "agents.db")) as connection:
                    connection.execute("INSERT INTO nudge_logs(id,type,review_type,title,sub_chat_id) VALUES (?,?,?,?,?)",
                        ("nudge", "review", "test", PRIVATE, "missing-child"))
                    connection.execute("INSERT INTO scheduled_tasks(id,name,project_id,schedule,payload) VALUES (?,?,?,?,?)",
                        ("scheduled", PRIVATE, "project-1", PRIVATE, PRIVATE))
                    connection.execute("INSERT INTO task_run_logs(id,task_id,chat_id,sub_chat_id,run_at,status) VALUES (?,?,?,?,?,?)",
                        ("run", "scheduled", "missing-chat", "child-1", 123, "complete"))
                    connection.commit()
                code, result = self.invoke("records", "--client", client, "--" + client + "-root", str(root), "--json")
                self.assertEqual(code, 0, result)
                self.assertEqual(result["count"], 1)
                messages = {error["message"] for error in result["store_errors"]}
                self.assertTrue({"office_nudge_references_unresolved", "office_task_run_references_unresolved"} <= messages)

    def test_sdk_links_are_not_followed_and_partial_database_metadata_is_retained(self):
        root = self.fixture("qoderwork")
        outside = self.root / "outside"
        outside.mkdir()
        sdk = self.root / SDK_DIRECTORY["qoderwork"]
        sdk.mkdir()
        try:
            (sdk / "projects").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit creating a directory symlink")
        code, result = self.invoke("records", "--client", "qoderwork", "--qoderwork-root", str(root), "--json")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["count"], 1)
        self.assertIn("office_path_redirected", {error["message"] for error in result["store_errors"]})
        self.assertEqual(list(outside.iterdir()), [])
