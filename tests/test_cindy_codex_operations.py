from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.cleaner import VerificationResult
from local_agent_record_janitor.adapters import CindyAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.codex_state import read_spawn_descendants
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from tests.support import create_cindy_database, create_thread_index


class CindyCodexOperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "cindy" / "codex-home"
        self.home.mkdir(parents=True)
        self.database = self.home.parent / "cindy.db"
        self.paths = {}
        rows = []
        for record_id, project, parent in (("delete", "ProxyConfig", None),
                                          ("child", "ProxyConfig", "delete"),
                                          ("keep", "other", None)):
            path = self.home / "sessions" / f"rollout-{record_id}.jsonl"
            path.parent.mkdir(exist_ok=True)
            source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "app-server"
            metadata = {"id": record_id, "cwd": str(self.root / project), "source": source}
            path.write_text(json.dumps({"type": "session_meta", "payload": metadata}) +
                            '\n{"type":"event_msg","payload":{"text":"BODY_SECRET"}}\n', encoding="utf-8")
            self.paths[record_id] = path
            rows.append({"id": record_id, "rollout_path": str(path), "source": json.dumps(source)})
        create_thread_index(self.home, rows)
        create_cindy_database(self.database, [
            {"id": "ui-delete", "sdk_session_id": "delete", "agent_kind": "codex", "status": "active"},
            {"id": "ui-child", "sdk_session_id": "child", "agent_kind": "codex", "status": "active"},
            {"id": "ui-keep", "sdk_session_id": "keep", "agent_kind": "codex", "status": "active"},
        ])
        self.official = self.root / "official"
        create_thread_index(self.official, [{"id": "delete", "rollout_path": "official-must-survive"}])
        self.official_before = (self.official / "state_5.sqlite").read_bytes()
        self.calls = []

    def adapter(self):
        return CindyAdapter(database=self.database, codex_home=self.home,
                            cindy_root=self.home.parent, codex_bin_hint=self.root / "codex")

    def coordinator(self):
        return OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))

    def plan(self, **scope):
        return self.coordinator().plan_operation(client="cindy", adapters=(self.adapter(),),
            plan_path=self.root / "plan.json", **scope)

    def server(self, **_kwargs):
        fixture = self
        class Server:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def delete_thread(self, record_id):
                fixture.calls.append(record_id)
                ids = {record_id, *read_spawn_descendants(fixture.home, (record_id,))[record_id]}
                with closing(sqlite3.connect(fixture.home / "state_5.sqlite")) as db:
                    db.executemany("DELETE FROM threads WHERE id=?", ((value,) for value in ids))
                    db.commit()
                for value in ids:
                    fixture.paths[value].unlink(missing_ok=True)
        return Server()

    def apply(self, plan, **kwargs):
        return self.coordinator().apply_operation(operation_id=plan["operation_id"],
            plan_path=Path(plan["plan_path"]), adapters=(self.adapter(),),
            clients_closed=True, app_server_factory=self.server,
            binary_resolver=lambda _: self.root / "codex", **kwargs)

    def test_inventory_project_plan_and_fresh_apply_preserve_other_store(self):
        out = StringIO()
        status = main(["records", "--client", "cindy", "--project", "ProxyConfig", "--json"],
                      adapters=(self.adapter(),), stdout=out, stderr=StringIO())
        self.assertEqual(status, 0, out.getvalue())
        self.assertEqual({r["record_id"] for r in json.loads(out.getvalue())["records"]}, {"delete", "child"})
        plan = self.plan(projects=(str(self.root / "ProxyConfig"),))
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertNotIn("BODY_SECRET", json.dumps(plan))
        self.assertIn("remove_frontend_reference", {a["kind"] for a in plan["actions"]})
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertTrue(result["modified"])
        self.assertEqual(self.calls, ["delete"])
        self.assertFalse(self.paths["child"].exists())
        self.assertTrue(self.paths["keep"].exists())
        self.assertEqual((self.official / "state_5.sqlite").read_bytes(), self.official_before)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(dict(db.execute("SELECT id,sdk_session_id FROM sessions")),
                             {"ui-delete": None, "ui-child": None, "ui-keep": "keep"})

    def test_record_scope_and_engine_filter(self):
        plan = self.plan(record_ids=("delete",))
        self.assertEqual(plan["goal_status"], "ready", plan)
        ids = {a["target"]["thread_id"] for a in plan["actions"]}
        self.assertEqual(ids, {"delete"})
        excluded = self.coordinator().plan_operation(client="cindy", engines=("pi",),
            record_ids=("delete",), adapters=(self.adapter(),), plan_path=self.root / "pi.json")
        self.assertEqual(excluded["goal_status"], "blocked", excluded)
        self.assertFalse(excluded["actions"])

    def test_child_only_selection_never_absorbs_parent(self):
        plan = self.plan(record_ids=("child",))
        self.assertEqual(plan["goal_status"], "ready", plan)
        result = self.apply(plan)
        self.assertIn(result["goal_status"], {"blocked", "unknown"}, result)
        self.assertFalse(result["mutation_started"])
        self.assertEqual(self.calls, [])
        self.assertTrue(self.paths["delete"].exists())
        self.assertTrue(self.paths["keep"].exists())
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(dict(db.execute("SELECT id,sdk_session_id FROM sessions")),
                             {"ui-delete": "delete", "ui-child": "child", "ui-keep": "keep"})

    def test_unknown_record_stays_blocked(self):
        plan = self.plan(record_ids=("missing",))
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertFalse(plan["actions"])
        self.assertEqual(plan["blockers"][0]["blocker_code"], "record_not_found")

    def test_new_frontend_reference_blocks_before_native_mutation(self):
        plan = self.plan(record_ids=("delete",))
        self.assertEqual(plan["goal_status"], "ready", plan)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("INSERT INTO sessions(id,sdk_session_id,status,agent_kind) VALUES('new','delete','active','codex')")
            db.commit()
        result = self.apply(plan)
        self.assertIn(result["goal_status"], {"blocked", "unknown"}, result)
        self.assertFalse(result["mutation_started"])
        self.assertEqual(self.calls, [])

    def test_unknown_native_never_retries_or_clears_frontend(self):
        plan = self.plan(record_ids=("delete",))
        self.assertEqual(plan["goal_status"], "ready", plan)
        def unknown_server(**kwargs):
            server = self.server(**kwargs)
            def delete(record_id):
                self.calls.append(record_id)
                raise TimeoutError("unknown mutation")
            server.delete_thread = delete
            return server
        coordinator = self.coordinator()
        with patch('local_agent_record_janitor.manual_delete.verify_finding_deleted',
                   return_value=VerificationResult(deleted=False, status='unknown', error='unreadable')):
            result = coordinator.apply_operation(operation_id=plan["operation_id"], plan_path=Path(plan["plan_path"]),
                adapters=(self.adapter(),), clients_closed=True, app_server_factory=unknown_server,
                binary_resolver=lambda _: self.root / "codex")
        self.assertEqual(result["goal_status"], "unknown", result)
        repeated = self.apply(plan)
        self.assertEqual(repeated["goal_status"], "unknown", repeated)
        self.assertEqual(self.calls, ["delete"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT sdk_session_id FROM sessions WHERE id='ui-delete'").fetchone()[0], "delete")

    def test_project_evidence_is_qualified_by_physical_store(self):
        def entry(store, project):
            return SimpleNamespace(target=SimpleNamespace(storage_id=store, thread_id="shared"),
                                   summary=SimpleNamespace(cwd=project))
        context = SimpleNamespace(plan=SimpleNamespace(
            conversations=(entry("cindy-one", "/one/ProxyConfig"),
                           entry("cindy-two", "/two/Other")), observations=()))
        action = SimpleNamespace(kind="delete_conversation", target=SimpleNamespace(storage_id="cindy-one", thread_id="shared"))
        self.assertEqual(OperationCoordinator._project_values(context, action), ("/one/ProxyConfig",))

    def test_verified_partial_cascade_can_delete_missing_parent_child(self):
        # Simulate an already verified partial server deletion in a temporary
        # store. The parent is gone; the child's indexed rollout remains.
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("DELETE FROM threads WHERE id='delete'")
            db.commit()
        self.paths["delete"].unlink()
        # This is the supported guardian orphan schema: independent indexed
        # role plus an exact top-level parent in the rollout.
        source = {"subagent": {"other": "guardian"}}
        metadata = {"id": "child", "cwd": str(self.root / "ProxyConfig"),
                    "source": source, "parent_thread_id": "delete", "thread_source": "guardian_review"}
        self.paths["child"].write_text(json.dumps({"type": "session_meta", "payload": metadata}) + "\n", encoding="utf-8")
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET source=? WHERE id='child'", (json.dumps(source),))
            db.commit()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DELETE FROM sessions WHERE id='ui-child'")
            db.commit()
        plan = self.plan(record_ids=("child",))
        self.assertEqual(plan["goal_status"], "ready", plan)
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertEqual(self.calls, ["child"])
        self.assertFalse(self.paths["child"].exists())
        self.assertTrue(self.paths["keep"].exists())
        self.assertEqual((self.official / "state_5.sqlite").read_bytes(), self.official_before)
