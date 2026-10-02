from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent_record_janitor.adapters import CindyAdapter, NativeIntegrityAdapter
from local_agent_record_janitor.agent_operations import action_binding, result_document
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.cleaner import clean_findings, scan_adapters
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.execution import ExecutionError
from local_agent_record_janitor.gui import build_gui_snapshot, execute_gui_delete
from local_agent_record_janitor.inventory import FrontendSessionRecord, build_session_catalog
from local_agent_record_janitor.manual_delete import (
    ManualDeletePlanError, build_manual_delete_plan, execute_manual_delete,
)
from local_agent_record_janitor.mutation_guard import (
    MutationRootLockedError, MutationScope, UnknownMutationError,
    action_target_ids, mutation_guard, mutation_roots,
    scopes_for_actions,
)
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.operation_store import OperationStore, OperationStoreError, _receipt_sha256, plan_sha256
from local_agent_record_janitor.planning import ActionKind, storage_id_for_path
from local_agent_record_janitor.pi_sessions import build_pi_session_catalog
from tests.support import create_thread_index, write_rollout
from tests.test_cindy_references import create_database
from tests.test_agent_cli import _MutatingServer


FIXTURE = Path(__file__).parent / "fixtures" / "operation_v1.json"


def journal_plan(root: Path, operation: str, action: dict, *, legacy: bool = False) -> dict:
    if legacy:
        plan = json.loads(FIXTURE.read_text(encoding="utf-8"))["agent_plan"]
        text = json.dumps(plan).replace("$STORE_ROOT", root.as_posix()).replace("$FIXTURE_ROOT", root.parent.as_posix())
        text = text.replace(plan["target"]["storage_id"], storage_id_for_path(root))
        plan = json.loads(text)
        plan["authorization"]["root_actions"] = [action]
    else:
        plan = {"schema_version": "larj.child-operation-plan.v1",
                "parent_operation_id": "synthetic-parent",
                "target": {"codex_home": str(root), "storage_id": storage_id_for_path(root)},
                "mutation_family": action["kind"], "actions": [action]}
    plan["operation_id"] = operation
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


def write_journal(root: Path, operation: str, action: dict, *, legacy: bool = False,
                  started: bool = True) -> OperationStore:
    plan = journal_plan(root, operation, action, legacy=legacy)
    store = OperationStore(root, operation)
    store.accept_plan(plan)
    store.write_state({"schema_version": "larj.agent-state.v1", "operation_id": operation,
        "plan_sha256": plan["plan_sha256"], "phase": "preflight", "goal_status": "unknown",
        "goal_satisfied": False, "modified": False, "mutation_started": False,
        "current_action_state": "not_started", "current_action_ids": [], "next_event_sequence": 1})
    store.append_event({"event": "plan_accepted"})
    if started:
        store.append_event({"event": "mutation_started", "action_id": action["action_id"]},
            state_updates={"phase": "recovery_required", "mutation_started": True,
                           "current_action_state": "mutation_started"})
    return store


def basic_action(root: Path, target: str = "synthetic-thread") -> dict:
    return {"action_id": "synthetic-action", "kind": "delete_conversation",
            "storage_id": storage_id_for_path(root), "thread_id": target,
            "affected_thread_ids": [target], "impact": {"affected_thread_ids": [target]}}


class MutationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "store"
        self.home.mkdir()
        self.scope = MutationScope(self.home, frozenset(("synthetic-thread",)))

    def test_legacy_child_unknown_blocks_new_ids_but_allows_unrelated_target_and_store(self) -> None:
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                scope = MutationScope(home, frozenset(("synthetic-thread",)))
                old = write_journal(home, "old-operation", basic_action(home), legacy=legacy)
                fresh = write_journal(home, "fresh-operation", basic_action(home), legacy=not legacy, started=False)
                before = old.events_path.read_bytes()
                with self.assertRaisesRegex(UnknownMutationError, "old-operation"):
                    with mutation_guard((scope,), store=fresh):
                        self.fail("fresh operation must not be admitted")
                self.assertEqual(old.events_path.read_bytes(), before)
                with mutation_guard((MutationScope(home, frozenset(("unrelated",))),)):
                    pass
                independent = home / "independent"
                independent.mkdir()
                with mutation_guard((MutationScope(independent, scope.target_ids),)):
                    pass
                self.assertFalse(old.receipt_path.exists())

    def test_ticket_reuses_only_same_thread_frozen_roots_and_targets(self) -> None:
        store = write_journal(self.home, "current", basic_action(self.home), started=False)
        with mutation_guard((self.scope,), store=store), store.mutation_lock():
            store.append_event({"event": "mutation_started"},
                state_updates={"phase": "executing", "mutation_started": True})
            with mutation_guard((self.scope,)):
                pass
            with mutation_guard((self.scope,), store=store):
                pass
            another = write_journal(self.home, "another-owner", basic_action(self.home), started=False)
            with self.assertRaisesRegex(OperationStoreError, "does not own"):
                with mutation_guard((self.scope,), store=another):
                    pass
            with self.assertRaisesRegex(OperationStoreError, "exceeds"):
                with mutation_guard((MutationScope(self.home, frozenset(("extra",))),)):
                    pass
            other_root = self.root / "other"
            other_root.mkdir()
            with self.assertRaisesRegex(OperationStoreError, "unfrozen"):
                with mutation_guard((MutationScope(other_root, self.scope.target_ids),)):
                    pass
            errors: list[Exception] = []
            def compete() -> None:
                try:
                    with mutation_guard((self.scope,)):
                        self.fail("other thread cannot borrow the ticket")
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=compete)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(errors[0], MutationRootLockedError)
        with self.assertRaises(UnknownMutationError):
            with mutation_guard((self.scope,), store=store):
                pass

    def test_malformed_journal_blocks_root_and_old_unknown_has_no_ttl(self) -> None:
        store = write_journal(self.home, "unknown-old", basic_action(self.home))
        events = [json.loads(line) for line in store.events_path.read_text(encoding="utf-8").splitlines()]
        for event in events:
            event["recorded_at"] = "2000-01-01T00:00:00+00:00"
        store.events_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        with self.assertRaises(UnknownMutationError):
            with mutation_guard((self.scope,)):
                pass
        store.state_path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(UnknownMutationError, "Untrusted sibling"):
            with mutation_guard((MutationScope(self.home, frozenset(("unrelated",))),)):
                pass

    def test_existing_lock_identity_replacement_is_rejected(self) -> None:
        with mutation_roots((self.home,)):
            pass
        lock = self.home / ".local-agent-record-janitor" / "operations" / ".mutation.lock"
        original_open = os.open
        replaced = False
        def replace_before_open(path, flags, *args, **kwargs):
            nonlocal replaced
            if Path(path) == lock and not replaced:
                replacement = lock.parent / "replacement"
                replacement.write_bytes(b"")
                os.replace(replacement, lock)
                replaced = True
            return original_open(path, flags, *args, **kwargs)
        with patch("local_agent_record_janitor.mutation_guard.os.open", side_effect=replace_before_open):
            with self.assertRaisesRegex(OperationStoreError, "identity changed"):
                with mutation_roots((self.home,)):
                    pass

    def _assert_linked_lock_rejected(self, mode: str) -> None:
        with mutation_roots((self.home,)):
            pass
        lock = self.home / ".local-agent-record-janitor" / "operations" / ".mutation.lock"
        outside = self.home / "other-lock"
        if mode == "hardlink":
            os.link(lock, outside)
        else:
            lock.unlink()
            outside.write_bytes(b"")
            try:
                lock.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"Temporary symlink creation unavailable: {exc}")
        with self.assertRaises(OperationStoreError):
            with mutation_roots((self.home,)):
                self.fail("linked lock must not admit mutation")
        self.assertTrue(outside.exists())

    def test_lock_file_hardlink_is_rejected(self) -> None:
        self._assert_linked_lock_rejected("hardlink")

    def test_lock_file_symlink_is_rejected(self) -> None:
        self._assert_linked_lock_rejected("symlink")

    def test_incomplete_checkpoints_and_old_terminal_locks_still_occupy_root(self) -> None:
        for checkpoint in ("plan_only", "guard_started", "blocked_missing_state", "terminal", "receipt"):
            with self.subTest(checkpoint=checkpoint), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                action = basic_action(home)
                store = write_journal(home, "old", action, started=False)
                state = store.read_state()
                if checkpoint == "plan_only":
                    store.state_path.unlink()
                    store.events_path.unlink()
                elif checkpoint in {"guard_started", "blocked_missing_state"}:
                    state.update(phase="executing" if checkpoint == "guard_started" else "blocked",
                                 goal_status="unknown" if checkpoint == "guard_started" else "blocked")
                    state.pop("current_action_state")
                    store.write_state(state)
                else:
                    store.append_event({"event": "operation_finished", "goal_status": "complete"},
                        state_updates={"phase": "finished", "goal_status": "complete", "goal_satisfied": True})
                    if checkpoint == "receipt":
                        result = result_document(subcommand="apply", operation_id=store.operation_id,
                            plan_sha=store.read_plan()["plan_sha256"], goal_status="complete",
                            modified=True, mutation_started=True, details={
                                "verification": {"all_satisfied": True, "verified_action_ids": [action["action_id"]]},
                                "final_scope_verification": {"all_satisfied": True, "scan_complete": True}})
                        store.compact_completed(result)
                    store.lock_path.write_text('{"pid":12345}', encoding="utf-8")
                with self.assertRaises(UnknownMutationError):
                    with mutation_guard((MutationScope(home, frozenset(("synthetic-thread",))),)):
                        self.fail("missing proof or old apply.lock must block")
                if checkpoint == "receipt":
                    with self.assertRaises(UnknownMutationError):
                        with mutation_guard((MutationScope(home, frozenset(("unrelated",))),)):
                            self.fail("compacted locked scope cannot be narrowed")
                elif checkpoint == "blocked_missing_state":
                    with self.assertRaises(UnknownMutationError):
                        with mutation_guard((MutationScope(home, frozenset(("synthetic-thread",))),), store=store):
                            self.fail("missing not_started is not resumable")

    def test_pi_same_frozen_path_blocks_new_header_id_and_missing_file(self) -> None:
        sessions = self.home / "sessions"
        sessions.mkdir()
        path = sessions / "bounded.jsonl"
        service = CleanupService(client_inspector=lambda *_: ())
        def prepare(session_id: str):
            path.write_text(json.dumps({"type": "session", "version": 3, "id": session_id,
                "timestamp": "2026-08-01T00:00:00.000Z", "cwd": str(self.root)}) + "\n", encoding="utf-8")
            catalog = lambda: build_pi_session_catalog(environ={"PI_CODING_AGENT_DIR": str(self.home)},
                cwd=self.root, home=self.root)
            context = service.prepare_sessions("pi", catalog, target_root=self.home)
            return next(action for action in context.plan.actions if action.available)
        old = prepare("prior-native-id")
        write_journal(self.home, "old-pi", action_binding(old))
        current = prepare("current-native-id")
        self.assertNotEqual(old.target.thread_id, current.target.thread_id)
        self.assertEqual(old.impact.external_artifact_paths, current.impact.external_artifact_paths)
        for exists in (True, False):
            with self.subTest(exists=exists):
                if not exists:
                    path.unlink()
                scope = MutationScope(self.home, action_target_ids(current))
                with self.assertRaises(UnknownMutationError):
                    with mutation_guard((scope,)):
                        self.fail("ID drift must not release the frozen artifact path")
        self.assertIsNone(action_target_ids({"kind": "delete_pi_session", "thread_id": "some-id"}))
        self.assertIsNone(action_target_ids({"kind": "unknown_family", "thread_id": "some-id"}))

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 aliases unavailable on this platform")
    def test_pi_root_alias_rebases_frozen_path_without_resolving_artifact(self) -> None:
        import ctypes
        home = self.root / "long-pi-agent-directory"
        (home / "sessions").mkdir(parents=True)
        path = home / "sessions" / "bounded.jsonl"
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.windll.kernel32.GetShortPathNameW(str(home), buffer, len(buffer))
        if not size or os.path.normcase(buffer.value) == os.path.normcase(str(home)):
            self.skipTest("Temporary volume does not expose an 8.3 root alias")
        short = Path(buffer.value)
        self.assertTrue(os.path.samefile(home, short))
        service = CleanupService(client_inspector=lambda *_: ())
        def prepare(anchor, native_id):
            path.write_text(json.dumps({"type": "session", "version": 3, "id": native_id,
                "timestamp": "2026-08-01T00:00:00.000Z", "cwd": str(self.root)}) + "\n", encoding="utf-8")
            catalog = lambda: build_pi_session_catalog(environ={"PI_CODING_AGENT_DIR": str(anchor)},
                cwd=self.root, home=self.root)
            context = service.prepare_sessions("pi", catalog, target_root=anchor)
            action = next(item for item in context.plan.actions if item.available)
            return context, action
        _, old = prepare(home, "old-native")
        write_journal(home, "old-alias-pi", action_binding(old))
        current_context, current = prepare(short, "new-native")
        self.assertTrue(os.path.samefile(old.impact.external_artifact_paths[0], current.impact.external_artifact_paths[0]))
        for exists in (True, False):
            with self.subTest(exists=exists):
                if not exists:
                    path.unlink()
                with self.assertRaises(UnknownMutationError):
                    with mutation_guard(scopes_for_actions(current_context.plan, (current,))):
                        self.fail("proven root alias must retain the same frozen path occupancy")

    def test_coordinator_exit_failure_keeps_attempt_facts_and_cached_unknown(self) -> None:
        native_id = "synthetic-thread"
        rollout = write_rollout(self.home, native_id, originator="codex_cli_rs", source="cli")
        create_thread_index(self.home, [{"id": native_id, "rollout_path": str(rollout), "source": "cli"}])
        coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        adapter = NativeIntegrityAdapter(codex_home=self.home)
        plan = coordinator.plan_operation(client="native", record_ids=(native_id,), engines=("codex",),
            codex_home=self.home, adapters=(adapter,), plan_path=self.root / "exit-failure.json")
        live = coordinator._live[plan["operation_id"]]
        def remove_target(_id):
            rollout.unlink()
            with closing(sqlite3.connect(self.home / "state_5.sqlite")) as database:
                database.execute("DELETE FROM threads WHERE id = ?", (native_id,))
                database.commit()
        server = _MutatingServer(remove_target)
        @contextmanager
        def fail_exit(roots):
            with mutation_roots(roots):
                yield
            raise OperationStoreError("Injected root exit failure")
        with patch("local_agent_record_janitor.operation_coordinator.mutation_roots", fail_exit):
            first = coordinator.apply_operation(client="native", record_ids=(native_id,), engines=("codex",),
                operation_id=plan["operation_id"], plan_path=Path(plan["plan_path"]),
                plan_sha256=plan["plan_sha256"], codex_home=self.home, clients_closed=True,
                app_server_factory=lambda **_: server, binary_resolver=lambda _: Path("synthetic-codex"))
        second = coordinator.apply_operation(client="native", record_ids=(native_id,), engines=("codex",),
            operation_id=plan["operation_id"], plan_path=Path(plan["plan_path"]),
            plan_sha256=plan["plan_sha256"], codex_home=self.home, clients_closed=True)
        self.assertEqual(first["goal_status"], "unknown")
        self.assertTrue(first["modified"])
        self.assertTrue(first["mutation_started"])
        self.assertEqual(len(first["batches"]), 1)
        self.assertEqual(second, first)
        self.assertEqual(server.deleted_thread_ids, [native_id])
        with patch("local_agent_record_janitor.operation_coordinator.mutation_roots", fail_exit):
            verified = coordinator.verify_operation(operation_id=plan["operation_id"],
                plan_path=Path(plan["plan_path"]), adapters=(adapter,), verify_timeout=0)
        self.assertEqual(verified["goal_status"], "unknown")
        self.assertTrue(verified["modified"])
        self.assertTrue(verified["mutation_started"])
        self.assertTrue(verified["batches"])
        self.assertEqual(coordinator.status_operation(operation_id=plan["operation_id"]), verified)
        self.assertEqual(server.deleted_thread_ids, [native_id])

    def test_receipt_reader_serializes_cleanup_with_same_id_generation_change(self) -> None:
        store = write_journal(self.home, "reused-id", basic_action(self.home))
        result = result_document(subcommand="apply", operation_id=store.operation_id,
            plan_sha=store.read_plan()["plan_sha256"], goal_status="complete", modified=True,
            mutation_started=True, details={"verification": {"all_satisfied": True,
                "verified_action_ids": ["synthetic-action"]},
                "final_scope_verification": {"all_satisfied": True, "scan_complete": True}})
        store.append_event({"event": "operation_finished", "goal_status": "complete"},
            state_updates={"phase": "finished", "goal_status": "complete", "goal_satisfied": True})
        store.compact_completed(result)
        receipt = json.loads(store.receipt_path.read_text(encoding="utf-8"))
        before_expiry = datetime.now(timezone.utc)
        after_expiry = before_expiry + timedelta(seconds=2)
        receipt.update(completed_at=(before_expiry - timedelta(days=7)).isoformat(),
                       receipt_expires_at=(before_expiry + timedelta(seconds=1)).isoformat())
        receipt["receipt_sha256"] = _receipt_sha256(receipt)
        store.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        entered, release = threading.Event(), threading.Event()
        original = OperationStore._discard_detailed_files
        values, errors = [], []
        def pause_reader(current, **kwargs):
            if threading.current_thread().name == "receipt-reader":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("reader release timed out")
            return original(current, **kwargs)
        def read_status():
            try:
                output = StringIO()
                main(("agent", "status", "--operation-id", store.operation_id, "--codex-home", str(self.home)),
                    stdout=output, stderr=StringIO())
                values.append(json.loads(output.getvalue()))
            except Exception as exc:
                errors.append(exc)
        with patch("local_agent_record_janitor.operation_store.datetime") as clock, patch.object(
                OperationStore, "_discard_detailed_files", pause_reader):
            clock.now.return_value = before_expiry
            clock.fromisoformat.side_effect = datetime.fromisoformat
            reader = threading.Thread(target=read_status, name="receipt-reader")
            reader.start()
            self.assertTrue(entered.wait(10))
            try:
                clock.now.return_value = after_expiry
                new = journal_plan(self.home, store.operation_id, basic_action(self.home, "new-target"))
                with self.assertRaises(MutationRootLockedError):
                    store.accept_plan(new)
            finally:
                release.set()
                reader.join(10)
            self.assertFalse(reader.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(values[0]["plan_sha256"], result["plan_sha256"])
            store.accept_plan(new)
            store.write_state({"schema_version": "larj.agent-state.v1", "operation_id": store.operation_id,
                "plan_sha256": new["plan_sha256"], "phase": "executing", "goal_status": "unknown",
                "goal_satisfied": False, "modified": False, "mutation_started": True, "next_event_sequence": 1})
            store.append_event({"event": "mutation_started"})
        self.assertEqual(store.read_plan()["plan_sha256"], new["plan_sha256"])
        self.assertTrue(store.read_state()["mutation_started"])
        self.assertEqual(len(store.read_events()), 1)

    def test_status_root_contention_is_unknown_and_retryable_not_not_found(self) -> None:
        store = write_journal(self.home, "status-busy", basic_action(self.home), legacy=True)
        entered, release = threading.Event(), threading.Event()
        def own_root():
            with mutation_roots((self.home,)):
                entered.set()
                release.wait(10)
        owner = threading.Thread(target=own_root)
        owner.start()
        self.assertTrue(entered.wait(10))
        try:
            output = StringIO()
            main(("agent", "status", "--operation-id", store.operation_id, "--codex-home", str(self.home)),
                stdout=output, stderr=StringIO())
            status = json.loads(output.getvalue())
            self.assertEqual(status["goal_status"], "unknown")
            self.assertTrue(status["mutation_started"])
            self.assertEqual(status["blockers"][0]["blocker_code"], "mutation_root_locked")
            self.assertTrue(status["blockers"][0]["retryable"])
        finally:
            release.set()
            owner.join(10)
        self.assertTrue(store.plan_path.exists())

    def test_fresh_plan_cannot_bypass_unknown_and_can_resume_after_conclusive_partial_verify(self) -> None:
        native_id = "synthetic-thread"
        rollout = write_rollout(self.home, native_id, originator="codex_cli_rs", source="cli")
        create_thread_index(self.home, [{"id": native_id, "rollout_path": str(rollout), "source": "cli"}])
        adapter = NativeIntegrityAdapter(codex_home=self.home)
        coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        plan = coordinator.plan_operation(client="native", record_ids=(native_id,), engines=("codex",),
            codex_home=self.home, adapters=(adapter,), plan_path=self.root / "fresh-B.json")
        old = write_journal(self.home, "legacy-A", plan["actions"][0]["binding"], legacy=True)
        writer = Mock(side_effect=AssertionError("unknown sibling must prevent app-server dispatch"))
        args = {"client": "native", "record_ids": (native_id,), "engines": ("codex",),
            "operation_id": plan["operation_id"], "plan_path": Path(plan["plan_path"]),
            "plan_sha256": plan["plan_sha256"], "codex_home": self.home, "clients_closed": True}
        blocked = coordinator.apply_operation(**args, app_server_factory=writer, binary_resolver=writer)
        self.assertEqual(blocked["goal_status"], "blocked")
        self.assertFalse(blocked["mutation_started"])
        self.assertFalse(blocked["modified"])
        self.assertTrue(any(item["blocker_code"] == "store_mutation_outcome_unknown" for item in blocked["blockers"]))
        writer.assert_not_called()
        output = StringIO()
        main(("agent", "verify", "--operation-id", old.operation_id, "--codex-home", str(self.home),
              "--verify-timeout", "0"), adapters=(adapter,), stdout=output, stderr=StringIO(),
             cleanup_service=CleanupService(client_inspector=lambda *_: ()))
        verified = json.loads(output.getvalue())
        self.assertEqual(verified["goal_status"], "completed_with_residuals")
        self.assertTrue(old.receipt_path.exists())
        self.assertTrue(rollout.exists())
        def remove_target(_id):
            rollout.unlink()
            with closing(sqlite3.connect(self.home / "state_5.sqlite")) as database:
                database.execute("DELETE FROM threads WHERE id = ?", (native_id,))
                database.commit()
        server = _MutatingServer(remove_target)
        complete = coordinator.apply_operation(**args, app_server_factory=lambda **_: server,
            binary_resolver=lambda _: Path("synthetic-codex"))
        self.assertEqual(complete["goal_status"], "complete")
        self.assertTrue(complete["mutation_started"])
        self.assertTrue(complete["modified"])
        self.assertEqual(server.deleted_thread_ids, [native_id])

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX fork unavailable on this platform")
    def test_fork_resets_inherited_roots_before_child_acquires_different_root(self) -> None:
        independent = self.root / "other"
        independent.mkdir()
        read_fd, write_fd = os.pipe()
        with mutation_roots((self.home,)):
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    from local_agent_record_janitor.mutation_guard import _thread
                    with mutation_roots((independent,)):
                        pass
                    assert _thread.roots == ()
                    os.write(write_fd, b"ok")
                    os._exit(0)
                except BaseException:
                    os.write(write_fd, b"failed")
                    os._exit(1)
            os.close(write_fd)
            try:
                self.assertEqual(os.read(read_fd, 16), b"ok")
                self.assertEqual(os.waitpid(pid, 0)[1], 0)
                code = "from pathlib import Path; import sys; from local_agent_record_janitor.mutation_guard import mutation_roots, MutationRootLockedError\ntry:\n with mutation_roots((Path(sys.argv[1]),)): pass\nexcept MutationRootLockedError: print('blocked')"
                env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
                probe = subprocess.run([sys.executable, "-B", "-c", code, str(self.home)], env=env,
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(probe.stdout.strip(), "blocked")
            finally:
                os.close(read_fd)

    def _start_process(self, code: str) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join((str(Path(__file__).resolve().parents[1]),
                                            str(Path(__file__).resolve().parents[1] / "src")))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen([sys.executable, "-B", "-u", "-c", code, str(self.home)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(self._stop_process, process)
        ready: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
        line = ready.get(timeout=15)
        if line.strip() != "ready":
            self.fail(f"child did not acquire lock: {line} {process.stderr.read()}")
        return process

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def test_real_two_process_mutex_is_permanent_and_non_inheritable(self) -> None:
        process = self._start_process("""
import os, sys
from pathlib import Path
from local_agent_record_janitor.mutation_guard import mutation_roots, _registry
with mutation_roots((Path(sys.argv[1]),)):
    assert not os.get_inheritable(next(iter(_registry.values())).fd)
    print('ready', flush=True)
    sys.stdin.read(1)
""")
        lock = self.home / ".local-agent-record-janitor" / "operations" / ".mutation.lock"
        identity = lock.stat().st_ino
        with self.assertRaises(MutationRootLockedError):
            with mutation_guard((self.scope,)):
                pass
        process.stdin.write("x")
        process.stdin.flush()
        process.wait(timeout=10)
        self.assertEqual(process.returncode, 0)
        with mutation_guard((self.scope,)):
            pass
        self.assertEqual(lock.stat().st_ino, identity)

    def test_killed_started_process_releases_os_lock_but_journal_and_apply_lock_block(self) -> None:
        create_thread_index(self.home, [])
        process = self._start_process("""
import sys
from pathlib import Path
from tests.test_mutation_guard import write_journal, basic_action
from local_agent_record_janitor.mutation_guard import mutation_roots, mutation_guard, MutationScope
root = Path(sys.argv[1])
with mutation_roots((root,)):
    store = write_journal(root, 'crashed', basic_action(root), legacy=True, started=False)
    with mutation_guard((MutationScope(root, frozenset(('synthetic-thread',))),), store=store), store.mutation_lock():
        store.append_event({'event': 'mutation_started'}, state_updates={'phase': 'executing', 'mutation_started': True})
        print('ready', flush=True)
        sys.stdin.read(1)
""")
        process.kill()
        process.wait(timeout=10)
        store = OperationStore(self.home, "crashed")
        lock_bytes = store.lock_path.read_bytes()
        with mutation_roots((self.home,)):
            pass
        with self.assertRaises(UnknownMutationError):
            with mutation_guard((self.scope,)):
                pass
        output = StringIO()
        main(("agent", "verify", "--operation-id", "crashed", "--codex-home", str(self.home)),
            adapters=(NativeIntegrityAdapter(codex_home=self.home),), stdout=output, stderr=StringIO())
        self.assertEqual(json.loads(output.getvalue())["goal_status"], "unknown")
        self.assertEqual(store.lock_path.read_bytes(), lock_bytes)
        self.assertFalse(store.receipt_path.exists())

    def test_real_frontend_and_native_ids_share_footprint_and_all_direct_writers_are_blocked(self) -> None:
        for native_unknown in (True, False):
            with self.subTest(native_unknown=native_unknown), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                native_id = "native-id"
                rollout = write_rollout(home, native_id, originator="cindy")
                create_thread_index(home, [{"id": native_id, "rollout_path": str(rollout), "source": "cindy"}])
                database = home / "cindy.db"
                create_database(database, [("frontend-ui", native_id, "deleted", "codex")])
                adapter = CindyAdapter(database=database, codex_home=home, cindy_root=home)
                service = CleanupService(client_inspector=lambda *_: ())
                coordinator = OperationCoordinator(service)
                context = coordinator._build_context("cindy", (adapter,),
                    explicit_frontend_ids=("frontend-ui",), engines=("codex",))[0]
                native = next(action for action in context.plan.actions if action.kind is ActionKind.DELETE_CONVERSATION)
                frontend = next(action for action in context.plan.actions if action.kind is ActionKind.DELETE_FRONTEND_SESSION)
                self.assertTrue(frontend.available)
                self.assertTrue({native_id, "frontend-ui"} <= action_target_ids(frontend))
                frozen = native if native_unknown else frontend
                write_journal(home, "prior", action_binding(frozen), legacy=native_unknown)
                selected = frontend if native_unknown else native
                writer = Mock(side_effect=AssertionError("writer must not run"))
                with patch("local_agent_record_janitor.execution.execute_cindy_session_cleanup", writer):
                    with self.assertRaises(ExecutionError) as raised:
                        service.execute(context, (selected,), timeout=1, app_server_factory=writer, binary_resolver=writer)
                self.assertEqual(raised.exception.kind, "store_mutation_outcome_unknown")

                catalog = build_session_catalog((adapter,))
                manual = build_manual_delete_plan(catalog).with_selected_actions((native_id,))
                with self.assertRaises(ManualDeletePlanError) as raised:
                    execute_manual_delete(manual, catalog_builder=lambda: catalog,
                        approved_plan_fingerprint=manual.plan_fingerprint, clients_closed=True,
                        preflight_verified=True, targeted_guards_only=True,
                        app_server_factory=writer, binary_resolver=writer)
                self.assertEqual(raised.exception.kind, "store_mutation_outcome_unknown")
                findings = scan_adapters((adapter,)).findings
                self.assertTrue(findings)
                with self.assertRaises(UnknownMutationError):
                    clean_findings(findings, app_server_factory=writer, binary_resolver=writer)
                desktop = FrontendSessionRecord(platform="codex-desktop", platform_session_id="local:" + native_id,
                    thread_id=native_id, database=home / "sqlite" / "codex-dev.db", codex_home=home,
                    details={"reference_kind": "desktop_host_catalog", "host_id": "local",
                             "snapshot_fingerprint": "desktop:v1:" + "a" * 64,
                             "global_state_reference_count": 2})
                gui_catalog = replace(catalog, records=tuple(replace(record,
                    frontend_sessions=(*record.frontend_sessions, desktop)) for record in catalog.records))
                gui = build_gui_snapshot(gui_catalog).selected_plan((native_id,))
                self.assertTrue(gui.desktop_targets)
                with patch("local_agent_record_janitor.gui.execute_manual_delete", writer):
                    with self.assertRaises(UnknownMutationError):
                        execute_gui_delete(gui, catalog_builder=writer,
                            approved_plan_fingerprint=gui.plan_fingerprint, clients_closed=True,
                            desktop_cleanup_executor=writer, final_verifier=writer)
                writer.assert_not_called()
