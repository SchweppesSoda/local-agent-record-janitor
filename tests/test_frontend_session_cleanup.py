from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from local_agent_record_janitor.frontend_session_cleanup import (
    FrontendSessionGuardError,
    build_cindy_session_delete_evidence,
    execute_cindy_session_cleanup,
)
from local_agent_record_janitor.adapters.cindy import CindyAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator


class FrontendSessionCleanupTests(unittest.TestCase):
    def _database(self, root: Path, deleted_count: int) -> tuple[Path, tuple[str, ...]]:
        database = root / "cindy.sqlite"
        deleted = tuple(f"deleted-{index:03d}" for index in range(deleted_count))
        with closing(sqlite3.connect(database)) as db:
            db.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    status TEXT,
                    sdk_session_id TEXT,
                    agent_kind TEXT NOT NULL,
                    working_dir TEXT,
                    parent_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT,
                    content TEXT,
                    created_at INTEGER,
                    rewind_at INTEGER
                );
                CREATE TABLE schedule_runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL
                );
                CREATE TABLE messages_fts (message_id TEXT, session_id TEXT);
                CREATE TABLE messages_fts_rows (message_id TEXT);
                CREATE TABLE embedding_jobs (
                    rowid INTEGER PRIMARY KEY,
                    source TEXT,
                    source_id TEXT,
                    vec_table TEXT
                );
                CREATE TABLE media_refs (id TEXT PRIMARY KEY, origin_session_id TEXT);
                CREATE TABLE ghost_cards (id TEXT PRIMARY KEY, session_id TEXT);
                CREATE TABLE skill_usage_sources (id TEXT PRIMARY KEY, session_id TEXT);
                CREATE TABLE skill_usage_exposures (id TEXT PRIMARY KEY, session_id TEXT);
                """
            )
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,NULL)",
                ("active", "active", "native-active", "codex", "C:/active"),
            )
            for index, session_id in enumerate(deleted):
                message_id = f"message-{index:03d}"
                db.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?,NULL)",
                    (session_id, "deleted", None, "codex", "C:/deleted"),
                )
                db.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                    (message_id, session_id, "user", "body-not-read", index, None),
                )
                db.execute("INSERT INTO messages_fts VALUES (?,?)", (message_id, session_id))
                db.execute("INSERT INTO messages_fts_rows VALUES (?)", (message_id,))
                db.execute(
                    "INSERT INTO embedding_jobs VALUES (?,?,?,?)",
                    (index + 1, "chat", message_id, "chat_messages_vec_v1"),
                )
                db.execute("INSERT INTO media_refs VALUES (?,?)", (f"media-{index}", session_id))
                db.execute("INSERT INTO ghost_cards VALUES (?,?)", (f"ghost-{index}", session_id))
                db.execute("INSERT INTO skill_usage_sources VALUES (?,?)", (f"source-{index}", session_id))
                db.execute("INSERT INTO skill_usage_exposures VALUES (?,?)", (f"exposure-{index}", session_id))
            db.commit()
        return database, deleted

    @staticmethod
    def _seeds(database: Path, ids: tuple[str, ...], status: str = "deleted") -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "database": str(database),
                "session_id": session_id,
                "expected_status": status,
            }
            for session_id in ids
        )

    def test_deleted_rows_and_dependencies_are_removed_in_one_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, deleted = self._database(Path(temporary), 2)
            evidence = build_cindy_session_delete_evidence(
                self._seeds(database, deleted)
            )
            phases: list[str] = []
            result = execute_cindy_session_cleanup(
                evidence,
                phase_callback=phases.append,
            )
            self.assertEqual(result.deleted_session_count, 2)
            self.assertEqual(result.deleted_message_count, 2)
            self.assertEqual(phases, ["mutation_started", "verified"])
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(
                    db.execute("SELECT id FROM sessions ORDER BY id").fetchall(),
                    [("active",)],
                )
                for table in (
                    "messages",
                    "messages_fts",
                    "messages_fts_rows",
                    "embedding_jobs",
                    "media_refs",
                    "ghost_cards",
                    "skill_usage_sources",
                    "skill_usage_exposures",
                ):
                    self.assertEqual(
                        db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0],
                        0,
                        table,
                    )
                self.assertEqual(
                    db.execute("SELECT session_id FROM schedule_runs ORDER BY id").fetchall(),
                    [],
                )

    def test_active_session_is_never_accepted_for_hard_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, _deleted = self._database(Path(temporary), 1)
            with self.assertRaises(FrontendSessionGuardError):
                build_cindy_session_delete_evidence(
                    self._seeds(database, ("active",), status="active")
                )
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM sessions WHERE id='active'").fetchone()[0],
                    1,
                )

    def test_snapshot_pass_count_is_constant_for_1_10_100_rows(self) -> None:
        import local_agent_record_janitor.frontend_session_cleanup as module
        from unittest.mock import patch

        observed: list[int] = []
        for count in (1, 10, 100):
            with tempfile.TemporaryDirectory() as temporary:
                database, deleted = self._database(Path(temporary), count)
                calls = 0
                original = module._snapshot

                def counted(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    return original(*args, **kwargs)

                with patch.object(module, "_snapshot", side_effect=counted):
                    module.build_cindy_session_delete_evidence(
                        self._seeds(database, deleted)
                    )
                observed.append(calls)
        self.assertEqual(observed, [1, 1, 1])

    def test_archived_session_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, deleted = self._database(Path(temporary), 1)
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "UPDATE sessions SET status = 'archived' WHERE id = ?",
                    (deleted[0],),
                )
                db.commit()
            with self.assertRaises(FrontendSessionGuardError):
                build_cindy_session_delete_evidence(
                    self._seeds(database, deleted, status="archived")
                )
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM sessions WHERE id = ?",
                        (deleted[0],),
                    ).fetchone()[0],
                    1,
                )

    def test_set_null_reference_is_updated_by_same_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, deleted = self._database(Path(temporary), 1)
            evidence = build_cindy_session_delete_evidence(
                self._seeds(database, deleted)
            )
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "INSERT INTO schedule_runs VALUES (?, ?)",
                    ("run-1", deleted[0]),
                )
                db.commit()
            phases: list[str] = []
            result = execute_cindy_session_cleanup(
                evidence,
                phase_callback=phases.append,
            )
            self.assertEqual(result.deleted_session_count, 1)
            self.assertEqual(phases, ["mutation_started", "verified"])
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM sessions WHERE id = ?",
                        (deleted[0],),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT session_id FROM schedule_runs WHERE id = 'run-1'"
                    ).fetchone(),
                    (None,),
                )

    def test_prior_authorized_reference_clear_keeps_session_delete_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, deleted = self._database(Path(temporary), 1)
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "UPDATE sessions SET sdk_session_id='native-original' WHERE id=?",
                    (deleted[0],),
                )
                db.commit()
            evidence = build_cindy_session_delete_evidence(
                self._seeds(database, deleted)
            )
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "UPDATE sessions SET sdk_session_id=NULL WHERE id=?",
                    (deleted[0],),
                )
                db.commit()
            result = execute_cindy_session_cleanup(evidence)
            self.assertEqual(result.deleted_session_count, 1)

    def test_replacement_sdk_session_id_is_still_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, deleted = self._database(Path(temporary), 1)
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "UPDATE sessions SET sdk_session_id='native-original' WHERE id=?",
                    (deleted[0],),
                )
                db.commit()
            evidence = build_cindy_session_delete_evidence(
                self._seeds(database, deleted)
            )
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "UPDATE sessions SET sdk_session_id='native-replacement' WHERE id=?",
                    (deleted[0],),
                )
                db.commit()
            with self.assertRaises(FrontendSessionGuardError):
                execute_cindy_session_cleanup(evidence)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT sdk_session_id FROM sessions WHERE id=?",
                        (deleted[0],),
                    ).fetchone(),
                    ("native-replacement",),
                )

    def test_client_check_uses_explicit_owner_process_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "renamed-cindy-data"
            owner_process_root = data_root / "renamed-engine-home"
            owner_process_root.mkdir(parents=True)
            database, deleted = self._database(data_root, 1)
            evidence = build_cindy_session_delete_evidence(
                self._seeds(database, deleted)
            )
            inspected: list[Path] = []
            inspected_clients: list[str] = []

            def inspect(path: Path, *, owner_client: str) -> tuple[str, ...]:
                inspected.append(path)
                inspected_clients.append(owner_client)
                if path != owner_process_root:
                    return ("ChatGPT.exe", "codex.exe")
                return ()

            result = execute_cindy_session_cleanup(
                evidence,
                owner_client="cindy",
                owner_process_root=owner_process_root,
                client_inspector=inspect,
            )

            self.assertEqual(result.deleted_session_count, 1)
            self.assertEqual(
                inspected,
                [owner_process_root],
            )
            self.assertEqual(inspected_clients, ["cindy"])

    def test_running_cindy_still_blocks_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "renamed-cindy-data"
            owner_process_root = data_root / "renamed-engine-home"
            owner_process_root.mkdir(parents=True)
            database, deleted = self._database(data_root, 1)
            evidence = build_cindy_session_delete_evidence(
                self._seeds(database, deleted)
            )

            with self.assertRaisesRegex(FrontendSessionGuardError, "Cindy.exe"):
                execute_cindy_session_cleanup(
                    evidence,
                    owner_process_root=owner_process_root,
                    client_inspector=lambda _path: ("Cindy.exe",),
                )

            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM sessions WHERE id=?",
                        (deleted[0],),
                    ).fetchone()[0],
                    1,
                )

    def test_high_level_cindy_plan_emits_session_delete_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, deleted = self._database(root, 2)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            adapter = CindyAdapter(
                database=database,
                codex_home=codex_home,
                cindy_root=root,
                codex_bin_hint=root / "codex.exe",
            )
            coordinator = OperationCoordinator(
                CleanupService(client_inspector=lambda _path: ())
            )
            document = coordinator.plan_operation(
                client="cindy",
                all_projects=True,
                adapters=(adapter,),
                plan_path=root / "plan.json",
            )
            self.assertIn("actions", document, document)
            actions = [
                action
                for action in document["actions"]
                if action["kind"] == "delete_frontend_session"
            ]
            self.assertEqual(
                {action["target"]["thread_id"] for action in actions},
                set(deleted),
            )
            self.assertEqual(
                {
                    action["impact"]["external_action_payload"]["status"]
                    for action in actions
                },
                {"deleted"},
            )
            self.assertEqual(
                {action["impact"]["owner_process_root"] for action in actions},
                {str(root)},
            )
            self.assertEqual(
                {action["impact"]["owner_client"] for action in actions},
                {"cindy"},
            )
            self.assertEqual(
                [batch["mutation_family"] for batch in document["child_batches"]],
                ["delete_frontend_session"],
            )
            self.assertTrue(
                document["capabilities"]["codex"]["frontend_session_delete"]
            )
            self.assertTrue(document["capabilities"]["codex"]["native_delete"])

    def test_capability_stays_enabled_when_no_deleted_rows_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, _deleted = self._database(root, 0)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            adapter = CindyAdapter(
                database=database,
                codex_home=codex_home,
                cindy_root=root,
                codex_bin_hint=root / "codex.exe",
            )
            document = OperationCoordinator(
                CleanupService(client_inspector=lambda _path: ())
            ).plan_operation(
                client="cindy",
                all_projects=True,
                adapters=(adapter,),
                plan_path=root / "plan.json",
            )
            self.assertTrue(
                document["capabilities"]["codex"]["frontend_session_delete"]
            )
            self.assertTrue(document["capabilities"]["codex"]["native_delete"])

    def test_high_level_record_selector_matches_frontend_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, deleted = self._database(root, 2)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            adapter = CindyAdapter(
                database=database,
                codex_home=codex_home,
                cindy_root=root,
                codex_bin_hint=root / "codex.exe",
            )
            coordinator = OperationCoordinator(
                CleanupService(client_inspector=lambda _path: ())
            )
            document = coordinator.plan_operation(
                client="cindy",
                record_ids=(deleted[0],),
                adapters=(adapter,),
                plan_path=root / "plan.json",
            )
            self.assertEqual(document["goal_status"], "ready")
            self.assertEqual(document["counts"]["action_count"], 1)
            self.assertEqual(
                document["actions"][0]["target"]["thread_id"],
                deleted[0],
            )
            result = coordinator.apply_operation(
                operation_id=document["operation_id"],
                plan_path=root / "plan.json",
                clients_closed=True,
                adapters=(adapter,),
            )
            self.assertEqual(result["goal_status"], "complete", result)
            self.assertTrue(result["goal_satisfied"])
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT id FROM sessions WHERE status='deleted' ORDER BY id"
                    ).fetchall(),
                    [(deleted[1],)],
                )


if __name__ == "__main__":
    unittest.main()
