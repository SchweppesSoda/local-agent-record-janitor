from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.cindy_schedule_cleanup import (
    ScheduleCleanupError, execute, freeze, remaining,
)
from local_agent_record_janitor.adapters.cindy import CindyAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator


class ScheduleCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'cindy.db'
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript('''
                PRAGMA foreign_keys=ON;
                CREATE TABLE schedules (id TEXT PRIMARY KEY, name TEXT, prompt TEXT);
                CREATE TABLE sessions (id TEXT PRIMARY KEY, status TEXT, sdk_session_id TEXT,
                    agent_kind TEXT, working_dir TEXT, title TEXT);
                CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, created_at INTEGER, rewind_at INTEGER);
                INSERT INTO schedules VALUES ('task', 'Keep task', 'secret prompt');
                INSERT INTO sessions VALUES ('chat', 'active', NULL, 'codex', 'C:/work', 'Chat');
            ''')
            db.executescript((Path(__file__).parent / 'fixtures/cindy/schedules_20260929.sql').read_text())
            for index in range(4):
                db.execute('''INSERT INTO schedule_runs
                    (id,schedule_id,session_id,fired_at,finished_at,status,result_text)
                    VALUES (?, 'task', 'chat', ?, ?, 'success', 'secret response')''',
                    (f'run-{index}', index, index + 1))
            db.commit()

    def evidence(self, ids=('run-0', 'run-1')):
        return freeze(self.database, self.home, ids)

    def run_ids(self):
        with closing(sqlite3.connect(self.database)) as db:
            return [r[0] for r in db.execute('SELECT id FROM schedule_runs ORDER BY id')]

    def test_exact_deletion_preserves_tasks_chats_and_newer_runs(self):
        evidence = self.evidence()
        self.assertNotIn('secret', json.dumps(evidence))
        result = execute(evidence, client_inspector=lambda _: ())
        self.assertEqual(result.deleted_ids, ('run-0', 'run-1'))
        self.assertEqual(self.run_ids(), ['run-2', 'run-3'])
        self.assertEqual(remaining(evidence), [])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT id FROM sessions').fetchall(), [('chat',)])
            self.assertEqual(db.execute('SELECT id FROM schedules').fetchall(), [('task',)])
            self.assertEqual(db.execute('SELECT run_id FROM schedule_session_latest_runs').fetchall(), [('run-3',)])
        self.assertFalse(list(self.root.glob('.larj-cindy-runs-*')))

    def test_latest_link_is_recomputed_for_shared_session(self):
        execute(self.evidence(('run-3',)), client_inspector=lambda _: ())
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT run_id FROM schedule_session_latest_runs').fetchall(), [('run-2',)])

    def test_null_session_runs_supported(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE schedule_runs SET session_id=NULL WHERE id='run-0'")
            db.commit()
        execute(self.evidence(('run-0',)), client_inspector=lambda _: ())
        self.assertNotIn('run-0', self.run_ids())

    def test_running_client_and_nonterminal_runs_block(self):
        with self.assertRaisesRegex(RuntimeError, 'Cindy.exe'):
            execute(self.evidence(), client_inspector=lambda _: ('Cindy.exe',))
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE schedule_runs SET status='running' WHERE id='run-0'")
            db.commit()
        with self.assertRaisesRegex(ScheduleCleanupError, 'not terminal'):
            self.evidence()
        self.assertEqual(len(self.run_ids()), 4)

    def test_drift_and_duplicate_selection_block(self):
        evidence = self.evidence()
        with self.assertRaisesRegex(ScheduleCleanupError, 'Duplicate'):
            execute(evidence + evidence, client_inspector=lambda _: ())
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE schedule_runs SET read_at=100 WHERE id='run-3'")
            db.commit()
        with self.assertRaisesRegex(ScheduleCleanupError, 'changed'):
            execute(evidence, client_inspector=lambda _: ())
        self.assertEqual(len(self.run_ids()), 4)

    def test_unknown_trigger_and_inbound_reference_block(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TRIGGER unsafe AFTER DELETE ON schedule_runs BEGIN DELETE FROM schedules; END')
            db.commit()
        with self.assertRaisesRegex(ScheduleCleanupError, 'triggers'):
            self.evidence()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('DROP TRIGGER unsafe')
            db.execute('CREATE TABLE extra (run_id REFERENCES schedule_runs(id) ON DELETE CASCADE)')
            db.commit()
        with self.assertRaisesRegex(ScheduleCleanupError, 'inbound'):
            self.evidence()

    def test_verification_failure_rolls_back_transaction(self):
        evidence = self.evidence()
        from local_agent_record_janitor import cindy_schedule_cleanup as module
        original = module._rows
        calls = 0

        def fail_after_delete(db):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError('injected verification failure')
            return original(db)

        with patch.object(module, '_rows', side_effect=fail_after_delete):
            with self.assertRaises(ScheduleCleanupError) as caught:
                execute(evidence, client_inspector=lambda _: ())
        self.assertTrue(caught.exception.outcome_known_rolled_back)
        self.assertEqual(len(self.run_ids()), 4)
        self.assertFalse(list(self.root.glob('.larj-cindy-runs-*')))

    def test_unknown_postcommit_recovers_without_repeating_delete(self):
        evidence = self.evidence()
        with patch('local_agent_record_janitor.cindy_schedule_cleanup.remaining',
                   side_effect=RuntimeError('postcommit verification unavailable')):
            with self.assertRaises(ScheduleCleanupError) as caught:
                execute(evidence, client_inspector=lambda _: ())
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(self.run_ids(), ['run-2', 'run-3'])
        self.assertEqual(len(list(self.root.glob('.larj-cindy-runs-*'))), 1)
        self.assertEqual(remaining(evidence), [])
        self.assertFalse(list(self.root.glob('.larj-cindy-runs-*')))

    def test_changed_recovery_state_keeps_evidence(self):
        evidence = self.evidence()
        with patch('local_agent_record_janitor.cindy_schedule_cleanup.remaining',
                   side_effect=RuntimeError('postcommit verification unavailable')):
            with self.assertRaises(ScheduleCleanupError):
                execute(evidence, client_inspector=lambda _: ())
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE schedule_runs SET read_at=99 WHERE id='run-3'")
            db.commit()
        with self.assertRaisesRegex(ScheduleCleanupError, 'Recovery state'):
            remaining(evidence)
        self.assertEqual(len(list(self.root.glob('.larj-cindy-runs-*'))), 1)

    def test_wal_store_cleanup_removes_readonly_backup_sidecars(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('PRAGMA journal_mode=WAL')
        execute(self.evidence(), client_inspector=lambda _: ())
        self.assertEqual(self.run_ids(), ['run-2', 'run-3'])
        self.assertFalse(list(self.root.glob('.larj-cindy-runs-*')))

    def test_recovery_after_verified_copy_unlink_requires_exact_poststate(self):
        evidence = self.evidence()
        with patch('local_agent_record_janitor.cindy_schedule_cleanup.remaining',
                   side_effect=RuntimeError('verification interrupted')):
            with self.assertRaises(ScheduleCleanupError):
                execute(evidence, client_inspector=lambda _: ())
        directory = next(self.root.glob('.larj-cindy-runs-*'))
        (directory / 'database.sqlite').unlink()
        (directory / 'database.sqlite-wal').write_bytes(b'')
        (directory / 'database.sqlite-shm').write_bytes(bytes(32768))
        self.assertEqual(remaining(evidence), [])
        self.assertFalse(directory.exists())

    def test_missing_copy_with_nonempty_wal_preserves_evidence(self):
        evidence = self.evidence()
        with patch('local_agent_record_janitor.cindy_schedule_cleanup.remaining',
                   side_effect=RuntimeError('verification interrupted')):
            with self.assertRaises(ScheduleCleanupError):
                execute(evidence, client_inspector=lambda _: ())
        directory = next(self.root.glob('.larj-cindy-runs-*'))
        (directory / 'database.sqlite').unlink()
        (directory / 'database.sqlite-wal').write_bytes(b'unknown')
        with self.assertRaisesRegex(ScheduleCleanupError, 'Missing recovery copy'):
            remaining(evidence)
        self.assertTrue(directory.exists())

    def coordinator(self):
        return OperationCoordinator(CleanupService(client_inspector=lambda _: ()))

    def adapter(self):
        return CindyAdapter(database=self.database, codex_home=self.home,
                            cindy_root=self.root, codex_bin_hint=self.root / 'codex.exe')

    def test_protocol_plan_apply_status_verify_and_no_repeat(self):
        coordinator = self.coordinator()
        path = self.root / 'plan.json'
        adapter = self.adapter()
        plan = coordinator.plan_operation(client='cindy', engines=('codex',),
            record_ids=('schedule-run:run-0', 'schedule-run:run-1'),
            adapters=(adapter,), plan_path=path)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        self.assertEqual(len(plan['actions']), 2)
        self.assertEqual(plan['child_batches'][0]['mutation_family'], 'delete_schedule_run')
        result = coordinator.apply_operation(plan_path=path, clients_closed=True, adapters=(adapter,))
        self.assertEqual(result['goal_status'], 'complete', result)
        for method in ('status_operation', 'verify_operation'):
            result = getattr(self.coordinator(), method)(plan_path=path, adapters=(adapter,))
            self.assertEqual(result['goal_status'], 'complete', result)
        again = self.coordinator().apply_operation(plan_path=path, clients_closed=True, adapters=(adapter,))
        self.assertEqual(again['goal_status'], 'complete', again)
        self.assertEqual(self.run_ids(), ['run-2', 'run-3'])

    def test_prefix_and_project_selection_do_not_authorize_runs(self):
        for index, scope in enumerate(({'record_ids': ('schedule-run:run',)}, {'all_projects': True})):
            plan = self.coordinator().plan_operation(client='cindy', engines=('codex',),
                adapters=(self.adapter(),), plan_path=self.root / f'p{index}.json', **scope)
            self.assertFalse(any(a['kind'] == 'delete_schedule_run' for a in plan.get('actions', [])))

    def test_protocol_unknown_requires_verify_and_does_not_repeat(self):
        path = self.root / 'unknown.json'
        adapter = self.adapter()
        coordinator = self.coordinator()
        plan = coordinator.plan_operation(client='cindy', engines=('codex',),
            record_ids=('schedule-run:run-0',), adapters=(adapter,), plan_path=path)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        with patch('local_agent_record_janitor.cindy_schedule_cleanup.remaining',
                   side_effect=RuntimeError('verification interrupted')):
            result = coordinator.apply_operation(plan_path=path, clients_closed=True, adapters=(adapter,))
        self.assertEqual(result['goal_status'], 'unknown', result)
        with patch('local_agent_record_janitor.cindy_schedule_cleanup.execute',
                   side_effect=AssertionError('must never repeat deletion')):
            result = self.coordinator().apply_operation(plan_path=path, clients_closed=True, adapters=(adapter,))
        self.assertEqual(result['goal_status'], 'unknown', result)
        result = self.coordinator().verify_operation(plan_path=path, adapters=(adapter,))
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertFalse(list(self.root.glob('.larj-cindy-runs-*')))


if __name__ == '__main__':
    unittest.main()
