"""Public typed readers protect exact local stores without becoming scanners."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent_record_janitor.adapters import CindyAdapter, NativeIntegrityAdapter
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.client_contracts import (
    ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle,
    ReferenceSnapshot, SourceFailure,
)
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.client_capability_guards import restrict_cleanup_context
from local_agent_record_janitor.client_capability_guards import ClientCapabilityLimits
from local_agent_record_janitor.gui import build_gui_snapshot, execute_gui_delete
from local_agent_record_janitor.inventory import build_session_catalog
from local_agent_record_janitor.manual_delete import (
    ManualDeletePlanError, build_manual_delete_plan, execute_manual_delete,
)
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.path_identity import canonical_existing_path_key
from local_agent_record_janitor.record_identity import EngineCapability, RecordKey, StoreKey
from local_agent_record_janitor.targeted_guard import TargetedGuardError, TargetedReferenceGuard
from tests.support import create_cindy_database, create_thread_index, write_rollout


class TypedReader:
    """Only the public ClientAdapter methods; no legacy name/home/scan facade."""

    def __init__(self, root: Path, *, stores=(), references=(), errors=(), writable=False):
        self.source = root / "metadata.json"
        self.descriptor = ClientDescriptor("cindy", profile_root=root,
            sources=(self.source, root / "secondary.json"), native_stores=tuple(stores),
            inventory_engines=("codex",), capability_limits=(EngineCapability(
                "cindy", "codex", native_delete=writable, verify=False),))
        self.references = tuple(references)
        self.errors = tuple(errors)
        self.refresh_calls: list[bool] = []

    def describe_client(self):
        return self.descriptor

    def snapshot_references(self, *, refresh=False):
        self.refresh_calls.append(refresh)
        return ReferenceSnapshot(self.descriptor, self.references, self.errors)

    def native_catalog_for(self, _engine):
        # Guard discovery must never add a candidate native catalog pass.
        return None

    def reference(self, thread_id, *, store=None, frontend_id=None, kind=ReferenceKind.RESTORE,
                  lifecycle=ReferenceLifecycle.RESTORABLE, complete=True):
        return ClientReference("cindy", self.source, frontend_id or thread_id + "-ui",
            thread_id, "codex", "codex", "binding:" + (frontend_id or thread_id),
            native_record=RecordKey(store, thread_id) if store is not None else None,
            kind=kind, lifecycle=lifecycle, evidence_complete=complete)


class TypedClientGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.home = self.root / "native"
        self.home.mkdir()
        self.thread = "11111111-1111-4111-8111-111111111111"
        self.path = write_rollout(self.home, self.thread, originator="codex_cli_rs", source="cli")
        create_thread_index(self.home, [{"id": self.thread, "rollout_path": str(self.path), "source": "cli"}])
        self.native = NativeIntegrityAdapter(codex_home=self.home)
        self.store = StoreKey("codex", self.home)
        self.service = CleanupService(client_inspector=lambda _root: ())

    def readonly(self):
        return TypedReader(self.root / "profile", stores=(self.store,))

    def invoke(self, argv, adapters):
        output = StringIO()
        result = main(argv, adapters=adapters, cleanup_service=self.service,
                      stdout=output, stderr=StringIO(), stdin=StringIO())
        return result, json.loads(output.getvalue())

    def test_selected_rootless_reference_keeps_profile_coverage_error(self):
        reader = TypedReader(self.root / "profile")
        reader.references = (reader.reference(self.thread, complete=False),)
        reader.errors = (SourceFailure(str(reader.descriptor.sources[1]), "Secondary snapshot unsupported",
                                       profile_root=reader.descriptor.profile_root),)
        for scope in ([], ["--record-id", self.thread]):
            status, result = self.invoke(["records", "--client", "cindy", "--json", *scope], (reader,))
            self.assertEqual(status, 1)
            self.assertEqual(result["goal_status"], "blocked")
            self.assertFalse(result["goal_satisfied"])
            self.assertEqual(len(result["store_errors"]), 1)

    def test_explicit_store_error_is_not_broadened_by_shared_profile_source(self):
        other = StoreKey("codex", self.root / "other")
        reader = TypedReader(self.root / "profile", stores=(self.store, other))
        reader.references = (reader.reference("one-id", store=self.store), reader.reference("two-id", store=other))
        for precise in (True, False):
            reader.errors = (SourceFailure(str(reader.source), "Inventory gap",
                profile_root=reader.descriptor.profile_root, store=self.store if precise else None),)
            status, result = self.invoke(["records", "--client", "cindy", "--record-id", "cindy:two-id-ui", "--json"], (reader,))
            self.assertEqual(status, 0 if precise else 1)
            self.assertEqual(len(result["store_errors"]), 0 if precise else 1)
            self.assertEqual(len(result["errors"]), 1)  # No lossy catalog clone.
        status, result = self.invoke(["records", "--client", "cindy", "--record-id", "one-id", "--json"], (reader,))
        self.assertEqual(status, 1)
        self.assertEqual(len(result["store_errors"]), 1)

    def test_unrelated_profile_error_does_not_pollute_selected_reference(self):
        first = TypedReader(self.root / "first")
        first.references = (first.reference("first"),)
        second = TypedReader(self.root / "second")
        second.references = (second.reference("second", complete=False),)
        second.errors = (SourceFailure(str(second.source), "Other profile unsupported", profile_root=second.descriptor.profile_root),)
        status, result = self.invoke(["records", "--client", "cindy", "--record-id", "first", "--json"], (first, second))
        self.assertEqual(status, 0)
        self.assertEqual(result["store_errors"], [])
        self.assertEqual(len(result["errors"]), 1)

    def test_failed_cached_reader_blocks_only_its_declared_store(self):
        other = StoreKey("codex", self.root / "other-store")
        for stores, blocked in (((other,), False), ((), False), ((self.store,), True)):
            reader = TypedReader(self.root / "profile", stores=stores, writable=True)
            reader.snapshot_references = Mock(side_effect=OSError("private fixture detail"))
            with self.subTest(stores=stores):
                reasons = ClientCapabilityLimits.from_adapters((reader,)).reasons("codex", "native_delete", native_root=self.home)
                self.assertEqual(bool(reasons), blocked)
                self.assertNotIn("private fixture detail", str(reasons))

    def test_native_plan_uses_guards_without_cataloging_their_other_stores(self):
        reader = self.readonly()
        reader.native_catalog_for = Mock(side_effect=AssertionError("guard must not scan candidates"))
        plan = OperationCoordinator(self.service).plan_operation(client="native", record_ids=(self.thread,),
            adapters=(self.native, reader), plan_path=self.root / "blocked.json")
        self.assertEqual(plan["goal_status"], "blocked")
        self.assertEqual(plan["counts"]["action_count"], 0)
        self.assertIn("client_capability_limit", str(plan["blockers"]))
        reader.native_catalog_for.assert_not_called()
        self.assertFalse(any(reader.refresh_calls))  # Cached metadata may supply exact source failures.

    def test_native_records_only_catalog_native_candidates_but_keep_known_ceilings(self):
        profile = self.root / "other-profile"
        home = profile / "home"
        home.mkdir(parents=True)
        other_path = write_rollout(home, "other-native", originator="cindy")
        create_thread_index(home, [{"id": "other-native", "rollout_path": str(other_path)}])
        database = profile / "cindy.db"
        create_cindy_database(database, [{"id": "other-ui", "sdk_session_id": "other-native", "status": "deleted", "agent_kind": "codex"}])
        other = CindyAdapter(database=database, codex_home=home, cindy_root=profile)
        with patch.object(other, "list_sessions", side_effect=AssertionError("guard store must not become a candidate")):
            status, result = self.invoke(["records", "--client", "native", "--json"], (self.native, other, self.readonly()))
        self.assertEqual(status, 0)
        self.assertEqual([item["record_id"] for item in result["targets"]], [self.thread])
        self.assertFalse(result["targets"][0]["capability"]["native_delete"])
        self.assertEqual(result["targets"][0]["action_ids"], [])
        self.assertIn("client_capability_limit", result["targets"][0]["blocker_codes"])

    def test_fresh_and_same_process_apply_retain_guards_and_hash_and_readonly_verify(self):
        for fresh in (False, True):
            with self.subTest(fresh=fresh):
                coordinator = OperationCoordinator(self.service)
                plan = coordinator.plan_operation(client="native", record_ids=(self.thread,), adapters=(self.native,),
                                                  plan_path=self.root / f"approved-{fresh}.json")
                before = Path(plan["plan_path"]).read_bytes()
                if fresh:
                    coordinator = OperationCoordinator(self.service)
                writer = Mock(side_effect=AssertionError("readonly guard must prevent writer"))
                args = dict(operation_id=plan["operation_id"], plan_path=Path(plan["plan_path"]))
                limited = (self.native, self.readonly())
                applied = coordinator.apply_operation(**args, scope=plan["scope"], plan_sha256=plan["plan_sha256"],
                    adapters=limited, clients_closed=True, app_server_factory=writer, binary_resolver=writer)
                self.assertEqual(applied["goal_status"], "blocked")
                self.assertFalse(applied["mutation_started"])
                writer.assert_not_called()
                verified = coordinator.verify_operation(**args, adapters=limited, verify_timeout=0)
                self.assertEqual(verified["goal_status"], "completed_with_residuals")
                self.assertEqual(verified["plan_sha256"], plan["plan_sha256"])
                self.assertEqual(Path(plan["plan_path"]).read_bytes(), before)

    def test_pure_typed_selected_client_is_structurally_blocked_for_plan_and_run(self):
        reader = self.readonly()
        reader.references = (reader.reference(self.thread, store=self.store),)
        writer = Mock(side_effect=AssertionError("metadata client must not execute"))
        coordinator = OperationCoordinator(self.service)
        for command in ("plan", "run"):
            output = StringIO()
            code = main(["delete", command, "--client", "cindy", "--record-id", self.thread,
                         "--out", str(self.root / f"typed-{command}.json"), "--clients-closed", "--json"],
                        adapters=(reader,), operation_coordinator=coordinator,
                        stdout=output, stderr=StringIO(), stdin=StringIO(),
                        app_server_factory=writer, binary_resolver=writer)
            result = json.loads(output.getvalue())
            self.assertNotEqual(code, 0)
            self.assertEqual(result["goal_status"], "blocked")
            self.assertIn("client_capability_limit", str(result["blockers"]))
            self.assertNotIn("scan", str(result.get("error", "")))
        writer.assert_not_called()

    def test_targeted_guard_uses_exact_store_lifecycle_and_frozen_descendants(self):
        reader = TypedReader(self.root / "profile", stores=(self.store,), writable=True)
        catalog = build_session_catalog((self.native,))
        action = build_manual_delete_plan(catalog).with_selected_actions((self.thread,)).actions[0]
        guard = TargetedReferenceGuard((reader,), {(canonical_existing_path_key(self.home), self.thread): frozenset((self.thread, "child"))})
        for kind, lifecycle, complete in (
            (ReferenceKind.CURRENT, ReferenceLifecycle.LIVE, True),
            (ReferenceKind.HISTORY, ReferenceLifecycle.HISTORICAL, True),
            (ReferenceKind.RESTORE, ReferenceLifecycle.RESTORABLE, True),
            (ReferenceKind.CURRENT, ReferenceLifecycle.UNKNOWN, True),
            (ReferenceKind.CURRENT, ReferenceLifecycle.DELETED, False),
            (ReferenceKind.RESTORE, ReferenceLifecycle.DELETED, True),
            (ReferenceKind.UNKNOWN, ReferenceLifecycle.DELETED, True),
        ):
            reader.references = (reader.reference("child", store=self.store, kind=kind, lifecycle=lifecycle, complete=complete),)
            with self.subTest(kind=kind, lifecycle=lifecycle, complete=complete), self.assertRaises(TargetedGuardError):
                guard.check_manual(action)
        reader.references = (reader.reference(self.thread, store=self.store, kind=ReferenceKind.CURRENT,
                                              lifecycle=ReferenceLifecycle.DELETED),)
        guard.check_manual(action)
        reader.references = (reader.reference(self.thread), replace(reader.reference(self.thread), host="ssh:fixture", opaque_native_locator="/remote/home"))
        guard.check_manual(action)
        unrelated = StoreKey("codex", self.root / "other")
        reader.references = (reader.reference(self.thread, store=unrelated),)
        reader.errors = (SourceFailure(str(reader.source), "Other store failed", store=unrelated),)
        guard.check_manual(action)
        reader.errors = (SourceFailure(str(reader.source), "Selected store failed", store=self.store),)
        with self.assertRaises(TargetedGuardError):
            guard.check_manual(action)
        self.assertTrue(any(reader.refresh_calls))
        rootless = TypedReader(self.root / "rootless")
        rootless_guard = TargetedReferenceGuard((rootless,), guard.affected_thread_ids)
        rootless.errors = (SourceFailure(str(rootless.source), "No native root coverage", profile_root=rootless.descriptor.profile_root),)
        rootless_guard.check_manual(action)  # Profile-only failure cannot join this store.
        rootless.errors = (SourceFailure(str(rootless.source), "Exact store failed", store=self.store),)
        with self.assertRaises(TargetedGuardError):
            rootless_guard.check_manual(action)

    def test_manual_and_gui_fresh_restore_block_before_native_or_desktop_writer(self):
        reader = TypedReader(self.root / "profile", stores=(self.store,), writable=True)
        reader.references = (reader.reference(self.thread, store=self.store),)
        catalog = build_session_catalog((self.native, reader))
        plan = build_manual_delete_plan(catalog).with_selected_actions((self.thread,))
        writer = Mock(side_effect=AssertionError("restorable reference must prevent writer"))
        for gui in (False, True):
            with self.subTest(gui=gui), self.assertRaises(TargetedGuardError):
                if gui:
                    approved = build_gui_snapshot(catalog).selected_plan((plan.actions[0].action_id,))
                    execute_gui_delete(approved, catalog_builder=lambda: catalog,
                        approved_plan_fingerprint=approved.plan_fingerprint, clients_closed=True,
                        app_server_factory=writer, binary_resolver=writer, desktop_cleanup_executor=writer)
                else:
                    execute_manual_delete(plan, catalog_builder=lambda: catalog,
                        approved_plan_fingerprint=plan.plan_fingerprint, clients_closed=True,
                        app_server_factory=writer, binary_resolver=writer)
        writer.assert_not_called()

    def test_qualified_reference_without_a_descriptor_store_still_blocks_writer(self):
        reader = TypedReader(self.root / "profile", writable=True)
        reader.references = (reader.reference(self.thread, store=self.store),)
        catalog = build_session_catalog((self.native, reader))
        plan = build_manual_delete_plan(catalog).with_selected_actions((self.thread,))
        writer = Mock(side_effect=AssertionError("qualified ref must not be ignored"))
        with self.assertRaises(TargetedGuardError):
            execute_manual_delete(plan, catalog_builder=lambda: catalog,
                approved_plan_fingerprint=plan.plan_fingerprint, clients_closed=True,
                app_server_factory=writer, binary_resolver=writer)
        writer.assert_not_called()
        self.assertEqual([refresh for refresh in reader.refresh_calls if refresh], [True])
        coordinator = OperationCoordinator(self.service)
        approved = coordinator.plan_operation(client="native", record_ids=(self.thread,),
            adapters=(self.native, reader), plan_path=self.root / "qualified-without-store.json")
        self.assertEqual(approved["goal_status"], "ready")
        result = coordinator.apply_operation(operation_id=approved["operation_id"], plan_path=Path(approved["plan_path"]),
            scope=approved["scope"], plan_sha256=approved["plan_sha256"], clients_closed=True,
            app_server_factory=writer, binary_resolver=writer)
        self.assertEqual(result["goal_status"], "blocked")
        self.assertFalse(result["mutation_started"])
        self.assertTrue(any(reader.refresh_calls))
        writer.assert_not_called()

    def test_direct_service_builder_cannot_discard_a_known_readonly_guard(self):
        coordinator = OperationCoordinator(self.service)
        context, *_ = coordinator._build_context("native", (self.native,))
        reader = self.readonly()
        base = replace(context, adapter_builder=lambda: (self.native,))
        protected = restrict_cleanup_context(base, (reader,), self.service.typed_actions)
        self.assertIn(reader, protected.adapter_builder())
        writer = Mock(side_effect=AssertionError("service must check its guards"))
        with self.assertRaises(Exception) as raised:
            self.service.execute(protected, tuple(context.plan.actions), app_server_factory=writer,
                                 binary_resolver=writer, cleaner=writer, timeout=1)
        self.assertIn("client_capability_limit", str(raised.exception))
        writer.assert_not_called()

    def test_direct_service_checks_fresh_restore_before_opening_writer(self):
        duplicate = self.home / "sessions" / "duplicate.jsonl"
        duplicate.write_bytes(self.path.read_bytes())
        context = self.service.prepare((self.native,))
        actions = tuple(action for action in context.plan.actions if action.kind.value == "delete_conversation")
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0].available)
        reader = TypedReader(self.root / "profile", stores=(self.store,), writable=True)
        reader.references = (reader.reference(self.thread, store=self.store),)
        protected = restrict_cleanup_context(context, (reader,), self.service.typed_actions)
        writer = Mock(side_effect=AssertionError("fresh restore must prevent writer"))
        with self.assertRaises(TargetedGuardError):
            self.service.execute(protected, actions, app_server_factory=writer,
                                 binary_resolver=writer, cleaner=writer, timeout=1)
        writer.assert_not_called()

    def test_native_parent_child_stays_healthy_without_a_child_frontend_binding(self):
        child = "22222222-2222-4222-8222-222222222222"
        child_path = write_rollout(self.home, child, originator="codex_cli_rs", source={"subagent": {"thread_spawn": {"parent_thread_id": self.thread}}})
        (self.home / "state_5.sqlite").unlink()
        create_thread_index(self.home, [
            {"id": self.thread, "rollout_path": str(self.path), "source": "cli"},
            {"id": child, "rollout_path": str(child_path), "source": {"subagent": {"thread_spawn": {"parent_thread_id": self.thread}}}},
        ], spawn_edges=[{"parent_thread_id": self.thread, "child_thread_id": child, "status": "closed"}])
        unrelated = TypedReader(self.root / "unrelated", stores=(StoreKey("codex", self.root / "other"),))
        unrelated.references = (unrelated.reference(self.thread),)
        status, result = self.invoke(["records", "--client", "native", "--json"], (self.native, unrelated))
        self.assertEqual(status, 0)
        target = next(t for t in result["targets"] if t["record_id"] == child)
        self.assertEqual(target["classification"], "healthy")
        self.assertEqual(target["parent_thread_ids"], [self.thread])
        self.assertEqual(target["frontend_reference_ids"], [])
        self.assertTrue(target["capability"]["native_delete"])

    def test_default_discovery_keeps_known_frontend_guards_separate_from_candidates(self):
        reader = self.readonly()
        calls = []
        def factory(args):
            calls.append(args.platform)
            return [self.native, reader]
        with patch("local_agent_record_janitor.adapter_factory.create_default_adapters", factory):
            plan = OperationCoordinator(self.service).plan_operation(client="native", record_ids=(self.thread,),
                codex_home=self.home, plan_path=self.root / "default.json")
        self.assertEqual(calls, [["all"]])
        self.assertEqual(plan["goal_status"], "blocked")
        self.assertFalse(any(reader.refresh_calls))


if __name__ == "__main__":
    unittest.main()
