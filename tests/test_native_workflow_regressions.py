from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.adapters import NativeIntegrityAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.codex_state import read_native_lineage, read_spawn_descendants
from local_agent_record_janitor.inventory import build_session_catalog
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.operation_store import plan_sha256, write_new_json
from tests.support import create_thread_index


class NativeWorkflowRegressions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "official"
        self.home.mkdir()
        self.paths = {}

    def fixture(self, rows):
        indexed = []
        for record_id, project, parent in rows:
            path = self.home / "sessions" / f"rollout-{record_id}.jsonl"
            path.parent.mkdir(exist_ok=True)
            source = {"subagent": {"other": "guardian"}} if parent else "app-server"
            metadata = {"id": record_id, "cwd": str(self.root / project), "source": source}
            if parent:
                metadata.update(parent_thread_id=parent, thread_source="guardian_review")
            path.write_text(json.dumps({"type": "session_meta", "payload": metadata}) + '\n' +
                '{"type":"event_msg","payload":{"text":"TRANSCRIPT_SECRET"}}\n', encoding="utf-8")
            self.paths[record_id] = path
            indexed.append({"id": record_id, "rollout_path": str(path), "source": json.dumps(source)})
        create_thread_index(self.home, indexed)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("ALTER TABLE threads ADD COLUMN title TEXT")
            db.execute("UPDATE threads SET title=?", ("The following is the Codex agent history >>> TRANSCRIPT START TITLE_SECRET" * 100,))
            db.commit()
        desktop = self.home / "sqlite" / "codex-dev.db"
        desktop.parent.mkdir()
        with closing(sqlite3.connect(desktop)) as db:
            db.execute("CREATE TABLE local_thread_catalog(host_id TEXT, thread_id TEXT, display_title TEXT, PRIMARY KEY(host_id,thread_id))")
            db.executemany("INSERT INTO local_thread_catalog VALUES('local',?,?)",
                ((key, f"UI {key}") for key, _, parent in rows if not parent))
            db.commit()
        self.adapter = NativeIntegrityAdapter(codex_home=self.home)

    def test_project_and_record_selection_titles_and_counts(self):
        self.fixture([("a", "work", None), ("b", "work", None), ("c", "other", None)])
        for scope, expected in ((["--project", str(self.root / "work")], {"a", "b"}), (["--record-id", "a"], {"a"})):
            out = StringIO()
            status = main(["records", "--client", "native", "--codex-home", str(self.home), "--json", *scope],
                adapters=(self.adapter,), stdout=out, stderr=StringIO())
            self.assertEqual(status, 0, out.getvalue())
            data = json.loads(out.getvalue())
            self.assertEqual({r["record_id"] for r in data["records"]}, expected)
            self.assertEqual(data["count"], len(expected))
            self.assertEqual(sum(g["native_record_count"] for g in data["groups"]), len(expected))
            self.assertNotIn("TITLE_SECRET", out.getvalue())
            self.assertNotIn("TRANSCRIPT_SECRET", out.getvalue())
            for row in data["records"]:
                self.assertEqual(row["display_name"], "UI " + row["record_id"])

    def test_guardian_multilevel_lineage_and_preserved_root(self):
        self.fixture([("delete", "work", None), ("child", "work", "delete"), ("grandchild", "work", "child"),
                      ("keep", "work", None), ("kept-child", "work", "keep")])
        descendants = read_spawn_descendants(self.home, ("delete", "keep"))
        self.assertEqual(descendants["delete"], {"child", "grandchild"})
        self.assertEqual(descendants["keep"], {"kept-child"})
        catalog = build_session_catalog((self.adapter,))
        child = next(r for r in catalog.records if r.thread_id == "child")
        self.assertEqual(child.summary.parent_thread_ids, ("delete",))
        self.assertTrue(child.summary.is_subagent)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES('keep','child','completed')")
            db.commit()
        catalog = build_session_catalog((self.adapter,))
        self.assertFalse(next(r for r in catalog.records if r.thread_id == "delete").deletable)
        self.assertIn("lineage_conflict", next(r for r in catalog.records if r.thread_id == "child").blocker_codes)

    def test_sqlite_only_parent_is_in_the_same_graph(self):
        self.fixture([("root", "work", None), ("child", "work", None)])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET source=? WHERE id='child'", (json.dumps({"subagent":{"thread_spawn":{"parent_thread_id":"root"}}}),))
            db.commit()
        self.assertEqual(read_spawn_descendants(self.home, ("root",))["root"], {"child"})

    def server(self, calls):
        fixture = self
        class Server:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def delete_thread(self, record_id):
                calls.append(record_id)
                ids = {record_id, *read_spawn_descendants(fixture.home, (record_id,))[record_id]}
                with closing(sqlite3.connect(fixture.home / "state_5.sqlite")) as db:
                    db.executemany("DELETE FROM threads WHERE id=?", ((value,) for value in ids))
                    db.commit()
                for value in ids:
                    fixture.paths[value].unlink(missing_ok=True)
        return lambda **_: Server()

    def test_apply_and_verify_restore_official_home_and_report_desktop_residual(self):
        self.fixture([("delete", "work", None), ("keep", "work", None)])
        plan_path = self.root / "plan.json"
        planner = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        plan = planner.plan_operation(client="native", record_ids=("delete",), adapters=(self.adapter,), plan_path=plan_path)
        self.assertEqual(plan["goal_status"], "ready", plan)
        cindy = self.root / "cindy"
        create_thread_index(cindy, [])
        original = (cindy / "state_5.sqlite").read_bytes()
        calls = []
        with patch.dict(os.environ, {"CODEX_HOME": str(cindy)}):
            fresh = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
            result = fresh.apply_operation(operation_id=plan["operation_id"], plan_path=plan_path, clients_closed=True,
                app_server_factory=self.server(calls), binary_resolver=lambda _: Path("codex"))
            self.assertEqual(result["goal_status"], "completed_with_residuals", result)
            self.assertTrue(result["modified"])
            verifier = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
            verified = verifier.verify_operation(operation_id=plan["operation_id"], plan_path=plan_path)
            self.assertEqual(verified["goal_status"], "completed_with_residuals", verified)
            self.assertTrue(verified["modified"])
            mismatch = verifier.verify_operation(operation_id=plan["operation_id"], plan_path=plan_path, codex_home=cindy)
            self.assertNotEqual(mismatch["goal_status"], "complete")
        self.assertEqual(calls, ["delete"])
        self.assertEqual((cindy / "state_5.sqlite").read_bytes(), original)

    def test_run_finishes_desktop_residual_with_fresh_plan_and_preserves_keep(self):
        self.fixture([("delete", "work", None), ("child", "work", "delete"), ("keep", "work", None), ("kept-child", "work", "keep")])
        calls = []
        service = CleanupService(client_inspector=lambda *_: ())
        coordinator = OperationCoordinator(service)
        with patch("local_agent_record_janitor.codex_desktop_state.running_related_clients", return_value=()):
            result = coordinator.run_operation(client="native", record_ids=("delete",), adapters=(self.adapter,),
                plan_path=self.root / "run.json", clients_closed=True, app_server_factory=self.server(calls),
                binary_resolver=lambda _: Path("codex"))
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertTrue(result["modified"])
        self.assertEqual(len(result["rounds"]), 2)
        self.assertNotEqual(result["rounds"][0]["plan_sha256"], result["rounds"][1]["plan_sha256"])
        self.assertEqual(calls, ["delete"])
        remaining = build_session_catalog((self.adapter,))
        self.assertEqual({r.thread_id for r in remaining.records}, {"keep", "kept-child"})

    def test_verify_missing_scan_evidence_cannot_report_complete(self):
        self.fixture([("keep", "work", None)])
        coordinator = OperationCoordinator(CleanupService())
        plan = coordinator.plan_operation(client="native", record_ids=("keep",), adapters=(self.adapter,), plan_path=self.root / "verify.json")
        with patch.object(coordinator, "_terminal_context", return_value=(SimpleNamespace(plan=SimpleNamespace(actions=(), scan_complete=True)), None)):
            result = coordinator.verify_operation(operation_id=plan["operation_id"])
        self.assertEqual(result["goal_status"], "unknown")

    def test_global_reference_without_catalog_is_still_a_residual(self):
        self.fixture([("delete", "work", None)])
        coordinator = OperationCoordinator(CleanupService())
        plan = coordinator.plan_operation(client="native", record_ids=("delete",), adapters=(self.adapter,), plan_path=self.root / "global.json")
        self.server([])().delete_thread("delete")
        with closing(sqlite3.connect(self.home / "sqlite" / "codex-dev.db")) as db:
            db.execute("DELETE FROM local_thread_catalog")
            db.commit()
        (self.home / ".codex-global-state.json").write_text(json.dumps({"pinned-thread-ids": ["delete"]}), encoding="utf-8")
        result = coordinator.verify_operation(operation_id=plan["operation_id"])
        self.assertEqual(result["goal_status"], "completed_with_residuals", result)
        self.assertTrue(result["residual_action_ids"])

    def test_run_keeps_original_scope_when_only_some_residuals_are_cleanable(self):
        self.fixture([("a", "work", None), ("b", "work", None), ("keep", "work", None)])
        calls = []
        server = self.server(calls)()
        delete = server.delete_thread
        def leave_rollout(record_id):
            raw = self.paths[record_id].read_bytes()
            delete(record_id)
            if record_id == "b":
                self.paths[record_id].write_bytes(raw)
        server.delete_thread = leave_rollout
        coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        with patch("local_agent_record_janitor.codex_desktop_state.running_related_clients", return_value=()):
            result = coordinator.run_operation(client="native", record_ids=("a", "b"), adapters=(self.adapter,),
                plan_path=self.root / "mixed.json", clients_closed=True, app_server_factory=lambda **_: server,
                binary_resolver=lambda _: Path("codex"))
        self.assertEqual(result["goal_status"], "completed_with_residuals", result)
        self.assertTrue(result["residual_action_ids"])
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(calls, ["a", "b"])
        self.assertTrue(self.paths["b"].is_file())

    def test_structured_thread_source_conflicts_with_rollout_parent(self):
        self.fixture([("child", "work", "deleted-parent")])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET thread_source=?", (json.dumps({"subagent":{"thread_spawn":{"parent_thread_id":"keep"}}}),))
            db.commit()
        info = read_native_lineage(self.home)["child"]
        self.assertEqual(set(info.parent_thread_ids), {"deleted-parent", "keep"})
        self.assertTrue(info.metadata_conflicts)
        coordinator = OperationCoordinator(CleanupService())
        plan = coordinator.plan_operation(client="native", record_ids=("child",), adapters=(self.adapter,), plan_path=self.root / "conflict.json")
        self.assertEqual(plan["goal_status"], "blocked", plan)

    def test_deleted_parent_guardian_can_be_explicitly_deleted_with_exact_evidence(self):
        self.fixture([("child", "work", "deleted-parent"), ("keep", "work", None)])
        coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        plan = coordinator.plan_operation(client="native", record_ids=("child",), adapters=(self.adapter,), plan_path=self.root / "orphan.json")
        self.assertEqual(plan["goal_status"], "ready", plan)
        calls = []
        result = coordinator.apply_operation(operation_id=plan["operation_id"], clients_closed=True,
            app_server_factory=self.server(calls), binary_resolver=lambda _: Path("codex"))
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertEqual(calls, ["child"])
        self.assertTrue(self.paths["keep"].is_file())

    def test_reappeared_parent_blocks_frozen_orphan_deletion(self):
        self.fixture([("child", "work", "deleted-parent")])
        coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        plan = coordinator.plan_operation(client="native", record_ids=("child",), adapters=(self.adapter,), plan_path=self.root / "reappeared.json")
        self.assertEqual(plan["goal_status"], "ready", plan)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("INSERT INTO threads (id,rollout_path) VALUES ('deleted-parent','missing.jsonl')")
            db.commit()
        calls = []
        result = coordinator.apply_operation(operation_id=plan["operation_id"], clients_closed=True,
            app_server_factory=self.server(calls), binary_resolver=lambda _: Path("codex"))
        self.assertIn(result["goal_status"], {"blocked", "unknown"}, result)
        self.assertFalse(result["mutation_started"])
        self.assertEqual(calls, [])

    def test_plan_names_never_expose_guardian_history(self):
        self.fixture([("a", "work", None)])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("ALTER TABLE threads ADD COLUMN name TEXT")
            db.execute("UPDATE threads SET name='The following is the Codex agent history NAME_BODY_SECRET'")
            db.commit()
        coordinator = OperationCoordinator(CleanupService())
        plan = coordinator.plan_operation(client="native", record_ids=("a",), adapters=(self.adapter,), plan_path=self.root / "names.json")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertNotIn("NAME_BODY_SECRET", json.dumps(plan))

    def test_incompatible_index_role_blocks_guardian_orphan(self):
        self.fixture([("child", "work", "deleted-parent")])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET thread_source='cli'")
            db.commit()
        self.assertTrue(read_native_lineage(self.home)["child"].metadata_conflicts)
        plan = OperationCoordinator(CleanupService()).plan_operation(client="native", record_ids=("child",),
            adapters=(self.adapter,), plan_path=self.root / "role.json")
        self.assertEqual(plan["goal_status"], "blocked", plan)

    def test_frontend_coverage_requires_exact_database_and_discovery_family(self):
        from local_agent_record_janitor.planning import StorageLocation, ScanStatus
        from local_agent_record_janitor.record_identity import canonical_path
        from local_agent_record_janitor.operation_coordinator import OperationCoordinatorError
        db = self.home / "a.sqlite"
        storage = StorageLocation(storage_id="store", label="fixture", path=self.home, scan_status=ScanStatus.OK)
        doc = {"storages": [storage.to_dict()], "child_batches": [{"storage_id": "store"}],
               "actions": [{"kind": "delete_project_item", "impact": {"frontend_project_database_paths": [str(db)]}}]}
        for coverage in (((canonical_path(db), "sessions"),), ((canonical_path(self.home / "b.sqlite"), "projects"),)):
            with self.subTest(coverage=coverage), self.assertRaises(OperationCoordinatorError):
                OperationCoordinator._assert_store_coverage(doc, SimpleNamespace(
                    plan=SimpleNamespace(storages=(storage,)), frontend_scan_coverage=coverage))
        OperationCoordinator._assert_store_coverage(doc, SimpleNamespace(
            plan=SimpleNamespace(storages=(storage,)), frontend_scan_coverage=((canonical_path(db), "projects"),)))
