from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_agent_record_janitor import office_database as office

SELECTED = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
PRIVATE = "OFFICE_PRIVATE_BODY_DO_NOT_EXPORT"


class OfficeDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def fixture(self, client):
        root = self.root / client
        path = root / office.RELATIVE
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db:
            schema = (Path(__file__).parent / "fixtures" / (client + "_cn_schema.sql")).read_text(encoding="utf-8")
            db.executescript("BEGIN;\n" + schema + "\nCOMMIT;")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("INSERT INTO projects(id,name,path) VALUES ('project',?,?)", (PRIVATE, str(self.root / "documents")))
            for suffix, sdk in (("selected", SELECTED), ("other", OTHER)):
                db.execute("INSERT INTO chats(id,name,project_id,ext) VALUES (?,?,'project',?)",
                    ("chat-" + suffix, PRIVATE, json.dumps({"taskStatus": "completed", "awarenessDisabled": True, "isCronChat": False})))
                db.execute("INSERT INTO sub_chats(id,chat_id,session_id,messages,ext) VALUES (?,?,?,?,?)",
                    (suffix + "-child", "chat-" + suffix, sdk, PRIVATE, json.dumps({"contextUsageSnapshot": {"totalTokens": 10}})))
                db.execute("INSERT INTO messages(id,message_id,chat_id,sub_chat_id,sequence,role,parts,searchable_text) VALUES (?,?,?,?,1,'user',?,?)",
                    ("message-" + suffix, "msg-" + suffix, "chat-" + suffix, suffix + "-child", PRIVATE, PRIVATE))
                db.execute("INSERT INTO nudge_logs(id,type,review_type,title,sub_chat_id) VALUES (?,'memory_saved','auto',?,?)",
                    ("nudge-" + suffix, PRIVATE, (suffix + "-child")[:8]))
            db.execute("INSERT INTO app_settings(key,value) VALUES ('auth-token',?)", (PRIVATE,))
            db.commit()
        return root, path

    def test_both_products_cascade_messages_fts_and_owned_nudges_preserving_neighbors(self):
        for client in ("qwenwork", "qoderwork"):
            with self.subTest(client=client):
                root, path = self.fixture(client)
                before = path.read_bytes()
                evidence = office.freeze(client, root, ["chat-selected"], timestamp=100)
                self.assertEqual(path.read_bytes(), before)
                self.assertNotIn(PRIVATE, json.dumps(evidence))
                self.assertEqual(evidence["sdk_ids"], [SELECTED])
                self.assertEqual(evidence["nudge_ids"], ["nudge-selected"])
                phases = []
                office.apply(evidence, phase_callback=phases.append)
                self.assertEqual(phases, ["mutation_started"])
                self.assertEqual(office.remaining(evidence), 0)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("SELECT id FROM chats").fetchall(), [("chat-other",)])
                    self.assertEqual(db.execute("SELECT id FROM sub_chats").fetchall(), [("other-child",)])
                    self.assertEqual(db.execute("SELECT id FROM messages").fetchall(), [("message-other",)])
                    self.assertEqual(db.execute("SELECT id FROM nudge_logs").fetchall(), [("nudge-other",)])
                    self.assertEqual(db.execute("SELECT value FROM app_settings WHERE key='auth-token'").fetchone(), (PRIVATE,))
                    self.assertEqual(db.execute("SELECT count(*) FROM projects").fetchone(), (1,))
                    db.execute("UPDATE app_settings SET value='outside writer' WHERE key='auth-token'")
                    db.commit()
                with self.assertRaisesRegex(office.OfficeDatabaseError, "after_state_unverified"):
                    office.remaining(evidence)
                self.assertEqual(office.remaining(evidence, terminal_verified=True), 0)

    def test_qwen_sidebar_transforms_only_selected_task_identity(self):
        root, path = self.fixture("qwenwork")
        value = {"schemaVersion": 2, "revision": 41,
            "preferences": {"pinned": "manual", "projects": "name", "recent": "created"},
            "orders": {"recent": ["chat-selected", "chat-other"], "projects": {"project": ["chat-selected"]}},
            "pinnedByScope": {"account": {"pinnedTaskIds": ["chat-selected", "chat-other"],
                "order": ["chat-selected"], "entryOrder": ["project:project", "task:chat-selected", "task:chat-other", "cron:unrelated"]}}}
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES ('sidebarTaskLayout',?,1)", (json.dumps(value),))
            db.commit()
        evidence = office.freeze("qwenwork", root, ["chat-selected"], timestamp=123)
        office.apply(evidence, phase_callback=lambda _: None)
        with closing(sqlite3.connect(path)) as db:
            raw, timestamp = db.execute("SELECT value,updated_at FROM app_settings WHERE key='sidebarTaskLayout'").fetchone()
        after = json.loads(raw)
        self.assertEqual(after["revision"], 42)
        self.assertEqual(timestamp, 123)
        self.assertEqual(after["pinnedByScope"]["account"]["entryOrder"], ["project:project", "task:chat-other", "cron:unrelated"])
        self.assertEqual(after["orders"]["projects"]["project"], [])

    def test_qwen_wal_and_replay_tables_cascade_only_selected_chat(self):
        root, path = self.fixture("qwenwork")
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            for suffix in ("selected", "other"):
                chat, child = "chat-" + suffix, suffix + "-child"
                db.execute("INSERT INTO agent_turn_inputs(id,message_id,chat_id,sub_chat_id,source,position,content) VALUES (?,?,?,?, 'user',1,?)",
                    (suffix, "message-" + suffix, chat, child, PRIVATE))
                db.execute("INSERT INTO session_event_log(id,session_id,event_stream_id,runtime_id,event_id,source_event_id,seq,seq_ids,payload,occurred_at,created_at) VALUES (?,?,?,'runtime',?,'source',1,'[1]',?,'2026-10-04',1)",
                    (suffix, chat, suffix, suffix, PRIVATE.encode()))
                db.execute("INSERT INTO session_event_streams(session_id,runtime_id,event_stream_id) VALUES (?,'runtime',?)", (chat, suffix))
                db.execute("INSERT INTO acp_workspace_map(workspace_id,session_id,root) VALUES (?,?,?)", (suffix, chat, str(self.root)))
            db.commit()
            evidence = office.freeze("qwenwork", root, ["chat-selected"])
            office.apply(evidence, phase_callback=lambda _: None)
            for table, column in (("agent_turn_inputs", "chat_id"), ("session_event_log", "session_id"),
                                  ("session_event_streams", "session_id"), ("acp_workspace_map", "session_id")):
                self.assertEqual(db.execute(f"SELECT {column} FROM {table}").fetchall(), [("chat-other",)])
            self.assertEqual(office.remaining(evidence), 0)

    def test_plan_drift_or_changed_schema_refuses_before_checkpoint(self):
        root, path = self.fixture("qoderwork")
        evidence = office.freeze("qoderwork", root, ["chat-selected"])
        with closing(sqlite3.connect(path)) as db:
            db.execute("UPDATE messages SET parts='changed' WHERE id='message-other'")
            db.commit()
        phases = []
        with self.assertRaises(office.OfficeDatabaseError):
            office.apply(evidence, phase_callback=phases.append)
        self.assertEqual(phases, [])
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TRIGGER surprise AFTER DELETE ON chats BEGIN DELETE FROM projects; END")
        with self.assertRaisesRegex(office.OfficeDatabaseError, "schema_unverified"):
            office.freeze("qoderwork", root, ["chat-selected"])

    def test_unrelated_automation_and_typed_import_do_not_block_literal_prompt_mentions(self):
        root, path = self.fixture("qwenwork")
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO scheduled_tasks(id,name,project_id,schedule,payload,source_chat_id) VALUES ('task',?,'project','{}',?,'chat-other')",
                       (PRIVATE, json.dumps({"message": "chat-selected"})))
            db.execute("INSERT INTO data_import_records(id,source_instance,capability,entity_type,source_id,target_id,created_at) VALUES ('import','source','tasks','project','chat-selected','chat-selected',1)")
            db.commit()
        evidence = office.freeze("qwenwork", root, ["chat-selected"])
        office.apply(evidence, phase_callback=lambda _: None)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM scheduled_tasks").fetchone(), (1,))
            self.assertEqual(db.execute("SELECT count(*) FROM data_import_records").fetchone(), (1,))

    def test_selected_remote_automation_and_shared_sdk_ownership_are_explicit_blockers(self):
        mutations = [
            ("INSERT INTO rc_session_mappings(sub_chat_id,chat_id,cwd,remote_session_id) VALUES ('selected-child','chat-selected','/synthetic','remote')", "remote_session"),
            ("INSERT INTO scheduled_tasks(id,name,project_id,schedule,payload,source_chat_id) VALUES ('task','task','project','{}','{}','chat-selected')", "automation_reference"),
            (f"UPDATE sub_chats SET session_id='{SELECTED}' WHERE id='other-child'", "sdk_session_shared"),
            ("UPDATE sub_chats SET id='selected-other' WHERE id='other-child'", "nudge_owner_ambiguous"),
        ]
        for i, (sql, error) in enumerate(mutations):
            with self.subTest(error=error):
                self.root = self.root / str(i)
                root, path = self.fixture("qwenwork")
                with closing(sqlite3.connect(path)) as db:
                    # Renaming a child also renames its message FK in this corruption-free fixture.
                    if error == "nudge_owner_ambiguous":
                        db.execute("UPDATE messages SET sub_chat_id='selected-other',searchable_text=searchable_text WHERE sub_chat_id='other-child'")
                    db.execute(sql)
                    db.commit()
                before = path.read_bytes()
                with self.assertRaisesRegex(office.OfficeDatabaseError, error):
                    office.freeze("qwenwork", root, ["chat-selected"])
                self.assertEqual(path.read_bytes(), before)

    def test_failure_during_checkpoint_rolls_back_and_never_claims_success(self):
        root, path = self.fixture("qoderwork")
        evidence = office.freeze("qoderwork", root, ["chat-selected"])
        def fail(_):
            raise OSError("synthetic durable log failure")
        with self.assertRaises(office.OfficeDatabaseError):
            office.apply(evidence, phase_callback=fail)
        self.assertEqual(office.remaining(evidence), 1)

    def test_independent_fts_ghost_is_not_an_empty_success(self):
        root, path = self.fixture("qwenwork")
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO messages_fts(rowid,searchable_text,chat_id,sub_chat_id,message_id,role) VALUES (999,?,'chat-selected','selected-child','ghost','user')", (PRIVATE,))
            db.commit()
        with self.assertRaisesRegex(office.OfficeDatabaseError, "search_projection_inconsistent"):
            office.freeze("qwenwork", root, ["chat-selected"])

    @unittest.skipUnless(os.name == "nt", "Windows file identity fence")
    def test_database_cannot_be_replaced_while_checkpoint_is_emitted(self):
        root, path = self.fixture("qwenwork")
        evidence = office.freeze("qwenwork", root, ["chat-selected"])
        def checkpoint(_):
            with self.assertRaises(OSError):
                path.rename(path.with_suffix(".replaced"))
        office.apply(evidence, phase_callback=checkpoint)
        self.assertEqual(office.remaining(evidence), 0)


if __name__ == "__main__":
    unittest.main()
