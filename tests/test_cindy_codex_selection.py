import tempfile
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.adapters import CindyAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from tests.support import create_cindy_database, create_thread_index, write_rollout


class CindyCodexSelectionTests(unittest.TestCase):
    def test_project_cleanup_includes_orphan_evidence_and_stale_live_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            parent = "33333333-3333-4333-8333-333333333333"
            child = "44444444-4444-4444-8444-444444444444"
            source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
            path = write_rollout(home, child, originator="cindy", source=source)
            create_thread_index(home, [{"id": child, "rollout_path": str(path),
                                       "source": source, "thread_source": "subagent"}],
                                spawn_edges=[{"parent_thread_id": parent,
                                              "child_thread_id": child, "status": "closed"}])
            database = root / "cindy.db"
            create_cindy_database(database, [{"id": "stale", "sdk_session_id": parent,
                                             "status": "active", "agent_kind": "codex"}])
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("ALTER TABLE sessions ADD COLUMN working_dir TEXT")
                connection.execute("UPDATE sessions SET working_dir = ?", (str(root),))
                connection.commit()
            adapter = CindyAdapter(database=database, codex_home=home,
                                   codex_bin_hint=root / "codex")
            plan = OperationCoordinator(CleanupService()).plan_operation(
                client="cindy", projects=(str(root),), engines=("codex",), adapters=(adapter,),
                plan_path=root / "plan.json",
            )
            self.assertEqual(plan["goal_status"], "ready", plan)
            self.assertEqual({a["kind"] for a in plan["actions"]},
                             {"delete_conversation", "remove_frontend_reference"})
            calls = []

            class Server:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def delete_thread(self, record_id):
                    calls.append(record_id)
                    path.unlink()
                    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                        connection.execute("DELETE FROM threads WHERE id = ?", (record_id,))
                        connection.execute("DELETE FROM thread_spawn_edges WHERE child_thread_id = ?", (record_id,))
                        connection.commit()

            with patch("local_agent_record_janitor.frontend_reference_cleanup._require_clients_closed"):
                result = OperationCoordinator(CleanupService()).apply_operation(
                    operation_id=plan["operation_id"], plan_path=root / "plan.json",
                    clients_closed=True, adapters=(adapter,),
                    app_server_factory=lambda **kwargs: Server(),
                    binary_resolver=lambda hint: root / "codex",
                )
            self.assertEqual(result["goal_status"], "complete", result)
            self.assertEqual(calls, [child])
            with closing(sqlite3.connect(database)) as connection:
                self.assertIsNone(connection.execute("SELECT sdk_session_id FROM sessions").fetchone()[0])

    def test_inventory_record_can_be_selected_without_widening_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / 'codex-home'
            ids = ('11111111-1111-4111-8111-111111111111',
                   '22222222-2222-4222-8222-222222222222')
            paths = [write_rollout(home, record_id, originator='cindy') for record_id in ids]
            create_thread_index(home, [dict(id=i, rollout_path=str(p)) for i, p in zip(ids, paths)])
            database = root / 'cindy.db'
            create_cindy_database(database, [])
            adapter = CindyAdapter(database=database, codex_home=home)
            coordinator = OperationCoordinator(CleanupService())
            plan = coordinator.plan_operation(
                client='cindy', record_ids=(ids[0],), engines=('codex',),
                adapters=(adapter,), plan_path=root / 'plan.json',
            )
            self.assertEqual(plan['goal_status'], 'ready', plan.get('blockers'))
            self.assertEqual({a['target']['thread_id'] for a in plan['actions']}, {ids[0]})
            self.assertTrue(all(p.exists() for p in paths))

            calls = []

            class Server:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def delete_thread(self, record_id):
                    calls.append(record_id)
                    paths[ids.index(record_id)].unlink()
                    with closing(sqlite3.connect(home / 'state_5.sqlite')) as connection:
                        connection.execute('DELETE FROM threads WHERE id = ?', (record_id,))
                        connection.commit()

            # Reconstruct the operation from disk, as separate CLI calls do.
            result = OperationCoordinator(CleanupService()).apply_operation(
                operation_id=plan['operation_id'], plan_path=root / 'plan.json',
                plan_sha256=plan['plan_sha256'], clients_closed=True,
                adapters=(adapter,), app_server_factory=lambda **kwargs: Server(),
                binary_resolver=lambda hint: root / 'codex',
            )
            self.assertEqual(result['goal_status'], 'complete', result)
            self.assertEqual(calls, [ids[0]])
            self.assertFalse(paths[0].exists())
            self.assertTrue(paths[1].exists())
