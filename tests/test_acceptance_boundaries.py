from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.codex_desktop_state import _relevant_client_names
from local_agent_record_janitor.cleanup_service import partition_actions
from local_agent_record_janitor.frontend_session_cleanup import (
    FrontendSessionCleanupError,
    FrontendSessionGuardError,
    build_cindy_session_delete_evidence,
    execute_cindy_session_cleanup,
)
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.operation_store import OperationStore
from local_agent_record_janitor.planning import (
    ActionImpact,
    ActionKind,
    CandidateAction,
    RiskLevel,
    StorageLocation,
    TargetRef,
    storage_id_for_path,
)


class AcceptanceBoundaryTests(unittest.TestCase):
    @staticmethod
    def _database(root: Path, count: int = 1) -> tuple[Path, tuple[str, ...]]:
        database = root / "frontend.sqlite"
        ids = tuple(f"session-{index:03d}" for index in range(count))
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    sdk_session_id TEXT,
                    agent_kind TEXT NOT NULL,
                    working_dir TEXT
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT,
                    content TEXT,
                    created_at INTEGER,
                    rewind_at INTEGER
                );
                """
            )
            for index, session_id in enumerate(ids):
                connection.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?)",
                    (session_id, "deleted", None, "codex", str(root / "project")),
                )
                connection.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                    (f"message-{index:03d}", session_id, "user", "fixture", index, None),
                )
            connection.commit()
        return database, ids

    @staticmethod
    def _evidence(database: Path, ids: tuple[str, ...]):
        return build_cindy_session_delete_evidence(
            tuple(
                {
                    "database": str(database),
                    "session_id": session_id,
                    "expected_status": "deleted",
                }
                for session_id in ids
            )
        )

    @staticmethod
    def _cindy_processes(profile: Path, process_root: Path):
        executable = process_root / "Programs" / "Cindy" / "Cindy.exe"
        bundled = profile / "codex" / "1.0.0" / "codex.exe"
        executable.parent.mkdir(parents=True, exist_ok=True)
        bundled.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"fixture")
        bundled.write_bytes(b"fixture")
        command = f'--user-data-dir="{profile}"'
        return (
            {
                "process_id": 100,
                "parent_process_id": 1,
                "name": "Cindy.exe",
                "executable_path": str(executable),
                "command_line": f'"{executable}" {command}',
            },
            {
                "process_id": 101,
                "parent_process_id": 100,
                "name": "Cindy.exe",
                "executable_path": str(executable),
                "command_line": f'--type=renderer {command}',
            },
            {
                "process_id": 102,
                "parent_process_id": 101,
                "name": "codex.exe",
                "executable_path": str(bundled),
                "command_line": f'codex.exe app-server {command}',
            },
        )

    @staticmethod
    def _official_processes(process_root: Path):
        app = (
            process_root
            / "WindowsApps"
            / "OpenAI.Codex_26.814.5167.0_x64__fixture"
            / "app"
        )
        chatgpt = app / "ChatGPT.exe"
        codex = app / "resources" / "codex.exe"
        chatgpt.parent.mkdir(parents=True, exist_ok=True)
        codex.parent.mkdir(parents=True, exist_ok=True)
        chatgpt.write_bytes(b"fixture")
        codex.write_bytes(b"fixture")
        return (
            {
                "process_id": 200,
                "parent_process_id": 1,
                "name": "ChatGPT.exe",
                "executable_path": str(chatgpt),
                "command_line": str(chatgpt),
            },
            {
                "process_id": 201,
                "parent_process_id": 200,
                "name": "ChatGPT.exe",
                "executable_path": str(chatgpt),
                "command_line": "--type=renderer",
            },
            {
                "process_id": 202,
                "parent_process_id": 201,
                "name": "codex.exe",
                "executable_path": str(codex),
                "command_line": "codex.exe app-server",
            },
        )

    @unittest.skipUnless(os.name == "nt", "Windows process attribution contract")
    def test_cindy_root_name_is_not_an_ownership_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "owner-process-root-without-brand-name"
            home = profile / "codex-home"
            home.mkdir(parents=True)
            bundled = profile / "codex" / "1.0.0" / "codex.exe"
            bundled.parent.mkdir(
                parents=True, exist_ok=True
            )
            bundled.write_bytes(b"fixture")
            records = self._cindy_processes(profile, root / "bin")
            self.assertEqual(
                _relevant_client_names(
                    profile,
                    records,
                    owner_client="cindy",
                ),
                ("Cindy.exe", "codex.exe"),
            )

    @unittest.skipUnless(os.name == "nt", "Windows process attribution contract")
    def test_official_family_is_ignored_and_cindy_running_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "arbitrary-owner-profile"
            home = profile / "codex-home"
            home.mkdir(parents=True)
            (profile / "codex" / "1.0.0" / "codex.exe").parent.mkdir(
                parents=True, exist_ok=True
            )
            (profile / "codex" / "1.0.0" / "codex.exe").write_bytes(b"fixture")
            database, ids = self._database(profile, 1)
            evidence = self._evidence(database, ids)
            official = self._official_processes(root / "official")
            inspector = lambda path, **kwargs: _relevant_client_names(
                path,
                official,
                owner_client=kwargs.get("owner_client"),
            )
            result = execute_cindy_session_cleanup(
                evidence,
                owner_process_root=profile,
                owner_client="cindy",
                client_inspector=inspector,
            )
            self.assertEqual(result.deleted_session_count, 1)

            blocked_profile = root / "another-owner-profile"
            blocked_home = blocked_profile / "codex-home"
            blocked_home.mkdir(parents=True)
            blocked_bundled = blocked_profile / "codex" / "1.0.0" / "codex.exe"
            blocked_bundled.parent.mkdir(
                parents=True, exist_ok=True
            )
            blocked_bundled.write_bytes(b"fixture")
            blocked_db, blocked_ids = self._database(blocked_profile, 1)
            blocked_evidence = self._evidence(blocked_db, blocked_ids)
            before = blocked_db.read_bytes()
            running = self._cindy_processes(blocked_profile, root / "cindy-bin")
            with self.assertRaises(FrontendSessionGuardError):
                execute_cindy_session_cleanup(
                    blocked_evidence,
                    owner_process_root=blocked_profile,
                    owner_client="cindy",
                    client_inspector=lambda path, **kwargs: _relevant_client_names(
                        path,
                        running,
                        owner_client=kwargs.get("owner_client"),
                    ),
                )
            self.assertEqual(blocked_db.read_bytes(), before)
            with closing(sqlite3.connect(blocked_db)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                    1,
                )

    @unittest.skipUnless(os.name == "nt", "Windows process attribution contract")
    def test_different_cindy_profile_does_not_block_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner_root = root / "renamed-owner-data-root"
            other_root = root / "another-renamed-profile"
            owner_root.mkdir()
            processes = self._cindy_processes(other_root, root / "cindy-bin")
            self.assertEqual(
                _relevant_client_names(
                    owner_root,
                    processes,
                    owner_client="cindy",
                ),
                (),
            )

    @unittest.skipUnless(os.name == "nt", "Windows process attribution contract")
    def test_official_family_is_ignored_without_cindy_bundle_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner_root = root / "owner-root-with-arbitrary-name"
            owner_root.mkdir()
            official = self._official_processes(root / "official")
            self.assertEqual(
                _relevant_client_names(
                    owner_root,
                    official,
                    owner_client="cindy",
                ),
                (),
            )

    def test_batch_sizes_have_constant_snapshot_and_process_preflight_counts(self) -> None:
        import local_agent_record_janitor.frontend_session_cleanup as module

        observed: list[tuple[int, int]] = []
        for count in (1, 10, 100):
            with tempfile.TemporaryDirectory() as temporary:
                database, ids = self._database(Path(temporary), count)
                evidence = self._evidence(database, ids)
                snapshot_calls = 0
                process_calls = 0
                original_snapshot = module._snapshot

                def counted_snapshot(*args, **kwargs):
                    nonlocal snapshot_calls
                    snapshot_calls += 1
                    return original_snapshot(*args, **kwargs)

                def inspect(_path: Path):
                    nonlocal process_calls
                    process_calls += 1
                    return ()

                with patch.object(module, "_snapshot", side_effect=counted_snapshot):
                    execute_cindy_session_cleanup(
                        evidence,
                        owner_process_root=Path(temporary),
                        client_inspector=inspect,
                    )
                observed.append((snapshot_calls, process_calls))
        self.assertEqual(observed, [(2, 1), (2, 1), (2, 1)])

    @staticmethod
    def _native_action(root: Path, action_id: str) -> CandidateAction:
        return CandidateAction(
            action_id=action_id,
            kind=ActionKind.DELETE_CONVERSATION,
            target=TargetRef(storage_id_for_path(root), action_id),
            risk=RiskLevel.HIGH,
            available=True,
            unavailable_reason=None,
            impact=ActionImpact(
                external_storage_root=str(root),
                external_artifact_paths=(str(root / action_id),),
            ),
            snapshot_fingerprint=f"snapshot:{action_id}",
        )

    def _blocked_child_fixture(self, root: Path):
        first_root = root / "first-store"
        second_root = root / "second-store"
        first_root.mkdir()
        second_root.mkdir()
        first = self._native_action(first_root, "first")
        second = self._native_action(second_root, "second")
        storages = (
            StorageLocation(storage_id_for_path(first_root), "first", first_root),
            StorageLocation(storage_id_for_path(second_root), "second", second_root),
        )
        context = SimpleNamespace(
            plan=SimpleNamespace(actions=(first, second), storages=storages),
            actions=(first, second),
        )
        batches = partition_actions((first, second))
        document = {
            "operation_id": "acceptance-retry",
            "plan_sha256": "a" * 64,
            "scope": {},
            "storages": [storage.to_dict() for storage in storages],
            "actions": [first.to_dict(), second.to_dict()],
            "child_batches": [
                {
                    "batch_id": batch.batch_id,
                    "child_operation_id": f"acceptance-retry-{index + 1}",
                    "storage_id": batch.storage_id,
                    "mutation_family": batch.mutation_family,
                    "resource_key": list(batch.resource_key),
                    "action_ids": [str(action.action_id) for action in batch.actions],
                }
                for index, batch in enumerate(batches)
            ],
        }
        live = SimpleNamespace(
            operation_id="acceptance-retry",
            document=document,
            context=context,
            candidates=(first, second),
            client="native",
            adapters=(),
            manual_actions={},
            action_contexts={},
            result=None,
        )
        coordinator = OperationCoordinator(SimpleNamespace())
        stores: list[OperationStore] = []
        for index, batch in enumerate(batches):
            store, _state = coordinator._open_batch_store(
                live,
                batch,
                f"acceptance-retry-{index + 1}",
                first_root if index == 0 else second_root,
                index,
            )
            stores.append(store)
        first_store, second_store = stores
        first_store.append_event(
            {"event": "batch_finished", "goal_status": "complete"},
            state_updates={
                "phase": "finished",
                "goal_status": "complete",
                "goal_satisfied": True,
                "modified": True,
                "mutation_started": False,
            },
        )
        blocker = {
            "blocker_code": "target_client_running",
            "scope": "child:acceptance-retry-2",
            "severity": "error",
            "retryable": True,
            "message": "Cindy.exe is still running",
        }
        second_store.append_event(
            {
                "event": "batch_finished",
                "goal_status": "blocked",
                "blockers": [blocker],
            },
            state_updates={
                "phase": "blocked",
                "goal_status": "blocked",
                "goal_satisfied": False,
                "modified": False,
                "mutation_started": False,
                "blockers": [blocker],
            },
        )
        return coordinator, live, blocker

    def test_blocked_child_can_resume_without_repeating_complete_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            coordinator, live, blocker = self._blocked_child_fixture(Path(temporary))
            before = coordinator._status_for_document(live.document)
            self.assertEqual(before["goal_status"], "blocked")
            self.assertIn(blocker, before["blockers"])

            calls: list[tuple[str, ...]] = []

            class Writer:
                def execute(self, _context, actions, **kwargs):
                    ids = tuple(str(action.action_id) for action in actions)
                    calls.append(ids)
                    callback = kwargs["action_state_callback"]
                    for action in actions:
                        callback("guard_started", action, None)
                        callback("mutation_started", action, None)
                        callback("verified", action, SimpleNamespace(status="deleted"))
                    return SimpleNamespace(
                        results=tuple(SimpleNamespace(status="deleted") for _ in actions),
                        modified=True,
                    )

            coordinator.service = Writer()
            with patch.object(
                coordinator,
                "_terminal_context",
                return_value=(SimpleNamespace(plan=SimpleNamespace(actions=())), None),
            ):
                result = coordinator._execute_live(
                    live,
                    timeout=1,
                    app_server_factory=lambda **_kwargs: None,
                    binary_resolver=lambda _hint: None,
                )
            self.assertEqual(calls, [("second",)])
            self.assertEqual(
                [batch["status"] for batch in result["batches"]],
                ["complete", "complete"],
            )
            self.assertEqual(result["goal_status"], "complete")

    def test_unknown_after_mutation_never_increases_mutation_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, ids = self._database(Path(temporary), 1)
            evidence = self._evidence(database, ids)
            mutation_calls = 0
            import local_agent_record_janitor.frontend_session_cleanup as module

            original_delete = module._delete

            def counted_delete(*args, **kwargs):
                nonlocal mutation_calls
                mutation_calls += 1
                return original_delete(*args, **kwargs)

            def fail_after_commit(phase: str) -> None:
                if phase == "verified":
                    raise RuntimeError("journal write unavailable")

            with patch.object(module, "_delete", side_effect=counted_delete):
                with self.assertRaises(FrontendSessionCleanupError) as raised:
                    execute_cindy_session_cleanup(
                        evidence,
                        phase_callback=fail_after_commit,
                    )
                self.assertTrue(raised.exception.outcome_unknown)
                first_count = mutation_calls
                with self.assertRaises(FrontendSessionGuardError):
                    execute_cindy_session_cleanup(evidence)
            self.assertEqual(mutation_calls, first_count)


if __name__ == "__main__":
    unittest.main()
