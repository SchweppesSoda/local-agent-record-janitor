from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_agent_record_janitor.adapters.cindy import CindyAdapter
from local_agent_record_janitor.cindy_schema import (
    CindySchemaError, FTS_TRIGGER_VERSIONS, guard_cindy_triggers,
)
from local_agent_record_janitor.frontend_reference_cleanup import (
    FrontendReferenceError, execute_frontend_reference_cleanup,
)
from local_agent_record_janitor.frontend_session_cleanup import (
    FrontendSessionGuardError, build_cindy_session_delete_evidence,
    execute_cindy_session_cleanup,
)
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from tests.support import create_cindy_database


FIXTURES = Path(__file__).parent / "fixtures" / "cindy"


class CindySchemaTests(unittest.TestCase):
    def auto_review_database(self, root):
        database = self.database(root, "0100")
        with closing(sqlite3.connect(database)) as db:
            db.executescript("ALTER TABLE sessions ADD COLUMN cleared_at INTEGER; ALTER TABLE messages ADD COLUMN agent_meta TEXT; ALTER TABLE messages ADD COLUMN client_id TEXT;")
            db.executescript((FIXTURES / "auto_review_0122.sql").read_text(encoding="utf-8"))
            db.executemany("INSERT INTO auto_review_projections(session_id,lead_id,payload) VALUES (?,?,?)", [
                ("target", "keep", '{"appendEvent":"PRIVATE_CACHE"}'),
                ("keep", "target", '{"appendEvent":"PRIVATE_CACHE"}'),
                ("keep", "keep", '{"appendEvent":"PRIVATE_KEEP_CACHE"}'),
            ])
            db.commit()
        return database

    def test_auto_review_session_operation_preserves_unrelated_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            database = self.auto_review_database(root)
            adapter = CindyAdapter(database=database, codex_home=root / "codex-home", cindy_root=root)
            coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
            plan = coordinator.plan_operation(client="cindy", record_ids=("target",), adapters=(adapter,), plan_path=root / "plan.json")
            self.assertEqual(plan["goal_status"], "ready", plan)
            self.assertNotIn("PRIVATE_CACHE", json.dumps(plan))
            result = OperationCoordinator(CleanupService(client_inspector=lambda *_: ())).apply_operation(
                operation_id=plan["operation_id"], plan_path=root / "plan.json", clients_closed=True, adapters=(adapter,))
            self.assertEqual(result["goal_status"], "complete", result)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT session_id,lead_id,revision,payload FROM auto_review_projections").fetchall(),
                                 [("keep", "keep", 0, '{"appendEvent":"PRIVATE_KEEP_CACHE"}')])
                self.assertEqual(db.execute("SELECT id FROM sessions").fetchall(), [("keep",)])
                self.assertEqual(db.execute("SELECT message_id FROM messages_fts").fetchall(), [("m-keep",)])
            self.assertFalse(list(root.glob(".larj-*")))

    def test_auto_review_projection_drift_invalidates_serialized_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            database = self.auto_review_database(root)
            evidence = build_cindy_session_delete_evidence([{"database": str(database), "session_id": "target", "expected_status": "deleted"}])
            serialized = json.loads(json.dumps([item.to_dict() for item in evidence]))
            with closing(sqlite3.connect(database)) as db:
                db.execute("UPDATE auto_review_projections SET revision=revision+1 WHERE lead_id='target'")
                db.commit()
            before = database.read_bytes()
            with self.assertRaises(FrontendSessionGuardError):
                execute_cindy_session_cleanup(serialized)
            self.assertEqual(before, database.read_bytes())

    def test_auto_review_changed_triggers_and_projection_layout_fail_closed(self):
        alterations = [
            "DROP TRIGGER auto_review_message_delete; CREATE TRIGGER auto_review_message_delete AFTER DELETE ON messages BEGIN DELETE FROM sessions; END;",
            "DROP TRIGGER auto_review_message_insert;",
            "DROP TRIGGER auto_review_message_update;",
            "DROP TRIGGER auto_review_session_clear;",
            "ALTER TABLE auto_review_projections ADD COLUMN unexpected TEXT;",
            "CREATE TRIGGER projection_side_effect AFTER DELETE ON auto_review_projections BEGIN DELETE FROM sessions; END;",
        ]
        for sql in alterations:
            with self.subTest(sql=sql), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve(strict=True)
                database = self.auto_review_database(root)
                with closing(sqlite3.connect(database)) as db:
                    db.executescript(sql)
                before = database.read_bytes()
                with self.assertRaises(FrontendSessionGuardError):
                    build_cindy_session_delete_evidence([{"database": str(database), "session_id": "target", "expected_status": "deleted"}])
                self.assertEqual(before, database.read_bytes())

    def database(self, root, version):
        database = root / "cindy.db"
        create_cindy_database(database, [
            {"id": "target", "sdk_session_id": None, "status": "deleted", "agent_kind": "pi"},
            {"id": "keep", "sdk_session_id": "keep-native", "status": "active", "agent_kind": "codex"},
        ])
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE messages(id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, created_at INTEGER, rewind_at INTEGER)")
            db.executescript((FIXTURES / f"fts_{version}.sql").read_text())
            db.executemany("INSERT INTO messages VALUES (?,?,?,?,1,NULL)", [
                ("m-target", "target", "user", "PRIVATE_TARGET_BODY"),
                ("m-switch", "target", "agent_switch", '{"fromAgentKind":"codex","fromSdkSessionId":"old-native","toAgentKind":"pi"}'),
                ("m-keep", "keep", "user", "PRIVATE_KEEP_BODY"),
            ])
            db.commit()
        (root / "codex-home").mkdir()
        return database

    def evidence(self, root, database):
        adapter = CindyAdapter(database=database, codex_home=root / "codex-home", cindy_root=root)
        return next(item.details["frontend_reference"] for item in adapter.scan() if item.thread_id == "old-native")

    def clean_reference(self, root, evidence):
        return execute_frontend_reference_cleanup(root, [evidence], client_inspector=lambda *_: (), owner_client="cindy")

    def test_upstream_versions_preserve_neighbor_rows_and_search_results(self):
        for version in FTS_TRIGGER_VERSIONS:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve(strict=True)
                database = self.database(root, version)
                evidence = self.evidence(root, database)
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual(guard_cindy_triggers(db, "messages"), version)
                    before = db.execute("SELECT * FROM messages_fts WHERE session_id='keep'").fetchall()
                self.clean_reference(root, evidence)
                seeds = [{"database": str(database), "session_id": "target", "expected_status": "deleted"}]
                plan = build_cindy_session_delete_evidence(seeds)
                self.assertNotIn("PRIVATE_TARGET_BODY", json.dumps([item.to_dict() for item in plan]))
                result = execute_cindy_session_cleanup(plan)
                self.assertEqual(result.deleted_message_count, 2)
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual(db.execute("SELECT id FROM sessions").fetchall(), [("keep",)])
                    self.assertEqual(db.execute("SELECT id FROM messages").fetchall(), [("m-keep",)])
                    self.assertEqual(db.execute("SELECT * FROM messages_fts WHERE session_id='keep'").fetchall(), before)
                    self.assertEqual(db.execute("SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'PRIVATE_KEEP_BODY'").fetchall(), [("m-keep",)])
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM messages_fts WHERE session_id='target'").fetchone()[0], 0)
                    if version >= "0096":
                        self.assertEqual(db.execute("SELECT message_id FROM messages_fts_rows").fetchall(), [("m-keep",)])
                self.assertFalse(list(root.glob(".larj-*")))

    def test_same_trigger_name_with_other_side_effects_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            database = self.database(root, "0096")
            evidence = self.evidence(root, database)
            with closing(sqlite3.connect(database)) as db:
                db.executescript("DROP TRIGGER messages_fts_update; CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN DELETE FROM sessions WHERE id='keep'; END;")
            before = database.read_bytes()
            with self.assertRaises(FrontendReferenceError):
                self.clean_reference(root, evidence)
            with self.assertRaises(FrontendSessionGuardError):
                build_cindy_session_delete_evidence([{"database": str(database), "session_id": "target", "expected_status": "deleted"}])
            self.assertEqual(before, database.read_bytes())
            self.assertFalse(list(root.glob(".larj-*")))

    def test_index_layout_and_transitive_triggers_fail_closed(self):
        for alteration in (
            "CREATE TRIGGER shadow_side_effect AFTER DELETE ON messages_fts_rows BEGIN DELETE FROM sessions; END;",
            "DROP INDEX messages_fts_rows_message_id_idx;",
            "CREATE TRIGGER session_side_effect AFTER DELETE ON sessions BEGIN DELETE FROM messages; END;",
        ):
            with self.subTest(alteration=alteration), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve(strict=True)
                database = self.database(root, "0096")
                with closing(sqlite3.connect(database)) as db:
                    db.executescript(alteration)
                with self.assertRaises(FrontendSessionGuardError):
                    build_cindy_session_delete_evidence([{"database": str(database), "session_id": "target", "expected_status": "deleted"}])

    def test_closed_client_uses_persistent_cjk_fallback_without_installing_functions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            database = self.database(root, "0100")
            with closing(sqlite3.connect(database)) as db:
                guard_cindy_triggers(db, "messages")
                self.assertFalse(db.execute("SELECT 1 FROM pragma_function_list WHERE name='cjk_seg'").fetchall())
            self.clean_reference(root, self.evidence(root, database))
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT content FROM messages WHERE id='m-keep'").fetchone()[0], "PRIVATE_KEEP_BODY")

    def test_rewind_trigger_is_not_activated_by_reference_update_or_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            database = self.database(root, "0100")
            with closing(sqlite3.connect(database)) as db:
                db.executescript("CREATE TABLE embedding_jobs(source TEXT, source_id TEXT); CREATE TABLE chat_messages_vec_v1(embedding BLOB);")
                db.executescript((FIXTURES / "rewind_0034.sql").read_text())
                db.execute("INSERT INTO embedding_jobs VALUES ('chat', 'm-switch')")
                db.execute("INSERT INTO chat_messages_vec_v1 VALUES (X'0102')")
                db.commit()
                self.assertEqual(guard_cindy_triggers(db, "messages"), "0100")
            self.clean_reference(root, self.evidence(root, database))
            with closing(sqlite3.connect(database)) as db:
                db.execute("DELETE FROM messages WHERE id='m-switch'")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM embedding_jobs").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT embedding FROM chat_messages_vec_v1").fetchone()[0], b"\x01\x02")
                db.executescript("DROP TRIGGER trg_chat_rewind_clean_vec; CREATE TRIGGER trg_chat_rewind_clean_vec AFTER UPDATE OF content ON messages BEGIN DELETE FROM embedding_jobs; END;")
                with self.assertRaises(CindySchemaError):
                    guard_cindy_triggers(db, "messages")
