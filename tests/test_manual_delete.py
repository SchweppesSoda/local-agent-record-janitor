from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_session_janitor.cleaner import VerificationResult
from codex_session_janitor.codex_state import find_thread_rollouts, read_thread_index
from codex_session_janitor.conversation_metadata import read_conversation_summaries
from codex_session_janitor.inventory import (
    FrontendSessionRecord,
    InventoryFailure,
    ManagedConversation,
    SessionCatalog,
)
from codex_session_janitor.manual_delete import (
    ManualDeletePlanError,
    ManualDeleteSelectionError,
    build_manual_delete_plan,
    execute_manual_delete,
)
from codex_session_janitor.models import ConversationSummary

from tests.support import create_thread_index, write_rollout


class FakeAppServer:
    def __init__(self) -> None:
        self.deleted_thread_ids: list[str] = []

    def __enter__(self) -> FakeAppServer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        self.deleted_thread_ids.append(thread_id)


class ManualDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.frontend_db = self.root / "frontend.sqlite"
        self.frontend_db.write_bytes(b"third-party-data-must-remain-read-only")

    def frontend(
        self,
        thread_id: str,
        *,
        status: str = "active",
        title: str = "display only",
    ) -> FrontendSessionRecord:
        return FrontendSessionRecord(
            platform="cindy",
            platform_session_id=f"cindy-{thread_id}",
            thread_id=thread_id,
            database=self.frontend_db,
            codex_home=self.home,
            backend="codex",
            status=status,
            updated_at_ms=123,
            title=title,
            is_live=status == "active",
        )

    def record(
        self,
        thread_id: str,
        *,
        home: Path | None = None,
        indexed: bool = True,
        rollout: bool = True,
        descendants: tuple[str, ...] = (),
        frontends: tuple[FrontendSessionRecord, ...] = (),
        deletable: bool = True,
        cascade_unknown: bool = False,
        blockers: tuple[str, ...] = (),
        legacy_indexed: bool = False,
    ) -> ManagedConversation:
        home = home or self.home
        rollouts = ()
        if rollout:
            write_rollout(home, thread_id, originator="test")
            rollouts = tuple(find_thread_rollouts(home, thread_id))
        return ManagedConversation(
            codex_home=home,
            thread_id=thread_id,
            summary=ConversationSummary(thread_id=thread_id, indexed=indexed),
            rollouts=rollouts,
            frontend_sessions=frontends,
            descendant_thread_ids=descendants,
            thread_index={"id": thread_id} if indexed else None,
            indexed=indexed,
            legacy_indexed=legacy_indexed,
            artifact_present=indexed or bool(rollouts),
            deletable=deletable,
            cascade_unknown=cascade_unknown,
            blockers=blockers,
            codex_bin_hints=(),
        )

    def execution_catalog(
        self,
        *,
        frontend_status: str = "deleted",
    ) -> SessionCatalog:
        root_id = "root-thread"
        child_id = "child-thread"
        root_path = write_rollout(self.home, root_id, originator="test")
        child_path = write_rollout(self.home, child_id, originator="test")
        database = self.home / "state_5.sqlite"
        if database.exists():
            database.unlink()
        create_thread_index(
            self.home,
            [
                {"id": root_id, "rollout_path": str(root_path)},
                {"id": child_id, "rollout_path": str(child_path)},
            ],
            spawn_edges=[
                {
                    "parent_thread_id": root_id,
                    "child_thread_id": child_id,
                }
            ],
        )
        records_by_thread = {
            thread_id: tuple(find_thread_rollouts(self.home, thread_id))
            for thread_id in (root_id, child_id)
        }
        summaries = read_conversation_summaries(
            self.home,
            (root_id, child_id),
            rollout_records_by_thread=records_by_thread,
            strict=True,
        )
        indexes = read_thread_index(
            self.home,
            (root_id, child_id),
            strict=True,
        )
        root_frontend = self.frontend(
            root_id,
            status=frontend_status,
        )
        return SessionCatalog(
            records=(
                ManagedConversation(
                    codex_home=self.home,
                    thread_id=root_id,
                    summary=summaries[root_id],
                    rollouts=records_by_thread[root_id],
                    frontend_sessions=(root_frontend,),
                    descendant_thread_ids=(child_id,),
                    thread_index=indexes[root_id],
                    indexed=True,
                    artifact_present=True,
                    deletable=True,
                ),
                ManagedConversation(
                    codex_home=self.home,
                    thread_id=child_id,
                    summary=summaries[child_id],
                    rollouts=records_by_thread[child_id],
                    thread_index=indexes[child_id],
                    indexed=True,
                    artifact_present=True,
                    deletable=True,
                ),
            )
        )

    def single_native_catalog(
        self,
        thread_id: str,
        *,
        duplicate: bool = False,
        index_mismatch: bool = False,
    ) -> SessionCatalog:
        active_path = write_rollout(self.home, thread_id, originator="test")
        if duplicate:
            write_rollout(
                self.home,
                thread_id,
                originator="test",
                archived=True,
            )
        indexed_path = (
            self.home / "sessions" / "missing-indexed-rollout.jsonl"
            if index_mismatch
            else active_path
        )
        create_thread_index(
            self.home,
            [{"id": thread_id, "rollout_path": str(indexed_path)}],
        )
        rollouts = tuple(find_thread_rollouts(self.home, thread_id))
        index = read_thread_index(self.home, (thread_id,), strict=True)[thread_id]
        summary = read_conversation_summaries(
            self.home,
            (thread_id,),
            rollout_records_by_thread={thread_id: rollouts},
            strict=True,
        )[thread_id]
        return SessionCatalog(
            records=(
                ManagedConversation(
                    codex_home=self.home,
                    thread_id=thread_id,
                    summary=summary,
                    rollouts=rollouts,
                    thread_index=index,
                    indexed=True,
                    artifact_present=True,
                    deletable=True,
                ),
            )
        )

    def test_action_id_is_stable_and_title_is_not_approval_identity(self) -> None:
        first = self.record(
            "stable-thread",
            frontends=(
                self.frontend(
                    "stable-thread",
                    status="deleted",
                    title="first",
                ),
            ),
        )
        second_frontend = replace(first.frontend_sessions[0], title="second")
        second = replace(first, frontend_sessions=(second_frontend,))

        first_plan = build_manual_delete_plan(SessionCatalog(records=(first,)))
        first_action = first_plan.actions[0]
        second_action = build_manual_delete_plan(
            SessionCatalog(records=(second,))
        ).actions[0]

        self.assertEqual(first_action.action_id, second_action.action_id)
        self.assertEqual(first_action.action_id, first.action_id)
        selected = first_plan.with_selected_actions((first.action_id,))
        self.assertEqual(selected.actions[0].codex_home, first.codex_home.resolve())
        self.assertEqual(selected.actions[0].thread_id, first.thread_id)
        self.assertEqual(
            first_action.frontend_snapshot_fingerprint,
            second_action.frontend_snapshot_fingerprint,
        )
        self.assertEqual(first_action.risk, "high")

    def test_exact_scope_binds_root_descendant_and_native_fingerprints(self) -> None:
        catalog = self.execution_catalog()
        action = next(
            item
            for item in build_manual_delete_plan(catalog).actions
            if item.thread_id == "root-thread"
        )

        self.assertTrue(action.available)
        self.assertEqual(
            action.affected_thread_ids,
            ("child-thread", "root-thread"),
        )
        self.assertEqual(
            action.expected_scope.descendant_thread_ids,
            ("child-thread",),
        )
        self.assertEqual(
            action.expected_scope.indexed_thread_ids,
            ("child-thread", "root-thread"),
        )
        self.assertEqual(
            len(action.expected_scope.rollout_state_fingerprints or ()),
            2,
        )
        self.assertEqual(
            len(action.expected_scope.conversation_metadata_fingerprints or ()),
            2,
        )

    def test_selection_requires_unique_explicit_target_and_approved_scope(self) -> None:
        other_home = self.root / "other-home"
        other_home.mkdir()
        plan = build_manual_delete_plan(
            SessionCatalog(
                records=(
                    self.record("same-thread"),
                    self.record("same-thread", home=other_home),
                )
            )
        )

        with self.assertRaises(ManualDeleteSelectionError):
            plan.with_selected_actions(("same-thread",))
        with self.assertRaises(ManualDeleteSelectionError):
            plan.with_selected_actions(("all",))
        selected = plan.with_selected_actions((plan.actions[0].action_id,))
        self.assertTrue(selected.selected)
        self.assertIsNotNone(selected.plan_fingerprint)
        self.assertEqual(len(selected.actions), 1)

    def test_unavailable_record_classes_and_storage_failure_fail_closed(self) -> None:
        cases = (
            self.record(
                "legacy-only",
                indexed=False,
                rollout=False,
                deletable=False,
                legacy_indexed=True,
            ),
            self.record(
                "frontend-only",
                indexed=False,
                rollout=False,
                deletable=False,
                frontends=(self.frontend("frontend-only"),),
            ),
            self.record("unknown-cascade", cascade_unknown=True),
        )
        failure = InventoryFailure(
            source="state",
            codex_home=self.home,
            message="storage unreadable",
        )
        plan = build_manual_delete_plan(
            SessionCatalog(records=cases, errors=(failure,))
        )

        self.assertTrue(all(not action.available for action in plan.actions))
        for action in plan.actions:
            with self.assertRaises(ManualDeleteSelectionError):
                plan.with_selected_actions((action.action_id,))

    def test_malformed_catalog_makes_otherwise_valid_action_unavailable(self) -> None:
        class MalformedCatalog:
            conversations = (self.record("otherwise-valid"),)

        plan = build_manual_delete_plan(MalformedCatalog())

        self.assertTrue(plan.errors)
        self.assertEqual(plan.executable_actions, ())
        with self.assertRaises(ManualDeleteSelectionError):
            plan.with_selected_actions((plan.actions[0].action_id,))

    def test_inconsistent_catalog_cannot_hide_live_cindy_descendant(self) -> None:
        root = self.record("root", descendants=("child",), deletable=True)
        child = self.record(
            "child",
            frontends=(self.frontend("child", status="active"),),
            deletable=True,
            blockers=(),
        )

        plan = build_manual_delete_plan(SessionCatalog(records=(root, child)))
        action = next(item for item in plan.actions if item.thread_id == "root")

        self.assertFalse(action.available)
        self.assertTrue(
            any(
                "live Cindy current or historical reference" in reason
                for reason in action.unavailable_reasons
            )
        )
        with self.assertRaises(ManualDeleteSelectionError):
            plan.with_selected_actions((action.action_id,))

    def test_overlapping_selected_roots_are_rejected(self) -> None:
        parent = self.record("parent", descendants=("child",))
        child = self.record("child")
        plan = build_manual_delete_plan(
            SessionCatalog(records=(parent, child))
        )

        with self.assertRaises(ManualDeletePlanError):
            plan.with_selected_actions(
                (plan.actions[0].action_id, plan.actions[1].action_id)
            )

    def test_execution_rebuild_fingerprint_drift_blocks_before_server(self) -> None:
        catalog = self.execution_catalog()
        selected = build_manual_delete_plan(catalog).with_selected_actions(
            ("root-thread",)
        )
        changed_root = replace(
            catalog.records[0],
            summary=replace(catalog.records[0].summary, title="drift"),
        )
        changed = replace(catalog, records=(changed_root, catalog.records[1]))
        factory_calls: list[bool] = []

        with self.assertRaises(ManualDeletePlanError):
            execute_manual_delete(
                selected,
                catalog_builder=lambda: changed,
                approved_plan_fingerprint=selected.plan_fingerprint or "",
                clients_closed=True,
                app_server_factory=lambda **_kwargs: (
                    factory_calls.append(True) or FakeAppServer()
                ),
            )

        self.assertEqual(factory_calls, [])

    def test_immediate_frontend_snapshot_drift_blocks_delete_request(self) -> None:
        catalog = self.execution_catalog()
        selected = build_manual_delete_plan(catalog).with_selected_actions(
            ("root-thread",)
        )
        changed_frontend = replace(
            catalog.records[0].frontend_sessions[0],
            status="archived",
            is_live=False,
        )
        changed_root = replace(
            catalog.records[0],
            frontend_sessions=(changed_frontend,),
        )
        changed = replace(catalog, records=(changed_root, catalog.records[1]))
        calls = 0

        def catalog_builder() -> SessionCatalog:
            nonlocal calls
            calls += 1
            return catalog if calls == 1 else changed

        server = FakeAppServer()
        report = execute_manual_delete(
            selected,
            catalog_builder=catalog_builder,
            approved_plan_fingerprint=selected.plan_fingerprint or "",
            clients_closed=True,
            app_server_factory=lambda **_kwargs: server,
            binary_resolver=lambda _hint: Path("codex"),
            verifier=lambda _finding: VerificationResult(deleted=True),
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("frontend reference snapshot changed", report.results[0].error or "")

    def test_immediate_legacy_index_membership_drift_blocks_delete_request(
        self,
    ) -> None:
        catalog = self.execution_catalog()
        selected = build_manual_delete_plan(catalog).with_selected_actions(
            ("root-thread",)
        )
        changed_root = replace(catalog.records[0], legacy_indexed=True)
        changed = replace(catalog, records=(changed_root, catalog.records[1]))
        calls = 0

        def catalog_builder() -> SessionCatalog:
            nonlocal calls
            calls += 1
            return catalog if calls == 1 else changed

        server = FakeAppServer()
        report = execute_manual_delete(
            selected,
            catalog_builder=catalog_builder,
            approved_plan_fingerprint=selected.plan_fingerprint or "",
            clients_closed=True,
            app_server_factory=lambda **_kwargs: server,
            binary_resolver=lambda _hint: Path("codex"),
            verifier=lambda _finding: VerificationResult(deleted=True),
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("inventory snapshot changed", report.results[0].error or "")

    def test_successful_execution_uses_app_server_and_preserves_frontend_db(self) -> None:
        catalog = self.execution_catalog()
        selected = build_manual_delete_plan(catalog).with_selected_actions(
            ("root-thread",)
        )
        server = FakeAppServer()
        before = self.frontend_db.read_bytes()

        report = execute_manual_delete(
            selected,
            catalog_builder=lambda: catalog,
            approved_plan_fingerprint=selected.plan_fingerprint or "",
            clients_closed=True,
            app_server_factory=lambda **_kwargs: server,
            binary_resolver=lambda _hint: Path("codex"),
            verifier=lambda _finding: VerificationResult(deleted=True),
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(server.deleted_thread_ids, ["root-thread"])
        self.assertEqual(report.results[0].status, "deleted")
        self.assertEqual(self.frontend_db.read_bytes(), before)

    def test_default_post_delete_verifier_fails_closed_on_inventory_error(self) -> None:
        catalog = self.execution_catalog()
        selected = build_manual_delete_plan(catalog).with_selected_actions(
            ("root-thread",)
        )
        incomplete = replace(
            catalog,
            errors=(
                InventoryFailure(
                    source="codex-rollouts",
                    codex_home=self.home,
                    message="permission denied after deletion",
                ),
            ),
        )
        calls = 0

        def catalog_builder() -> SessionCatalog:
            nonlocal calls
            calls += 1
            return catalog if calls <= 2 else incomplete

        report = execute_manual_delete(
            selected,
            catalog_builder=catalog_builder,
            approved_plan_fingerprint=selected.plan_fingerprint or "",
            clients_closed=True,
            app_server_factory=lambda **_kwargs: FakeAppServer(),
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("Post-delete inventory was incomplete", report.results[0].error or "")

    def test_root_duplicate_rollout_executes_only_with_exact_integrity_approval(self) -> None:
        catalog = self.single_native_catalog("duplicate-root", duplicate=True)
        plan = build_manual_delete_plan(catalog)
        action = plan.actions[0]
        self.assertEqual(action.integrity_approvals, ("duplicate_rollout",))
        selected = plan.with_selected_actions((action.action_id,))
        server = FakeAppServer()

        report = execute_manual_delete(
            selected,
            catalog_builder=lambda: catalog,
            approved_plan_fingerprint=selected.plan_fingerprint or "",
            clients_closed=True,
            app_server_factory=lambda **_kwargs: server,
            binary_resolver=lambda _hint: Path("codex"),
            verifier=lambda _finding: VerificationResult(deleted=True),
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(server.deleted_thread_ids, ["duplicate-root"])
        self.assertEqual(report.results[0].status, "deleted")

    def test_root_missing_index_path_mismatch_gets_exact_integrity_approval(self) -> None:
        catalog = self.single_native_catalog(
            "mismatch-root",
            index_mismatch=True,
        )
        plan = build_manual_delete_plan(catalog)
        action = plan.actions[0]
        self.assertEqual(
            action.integrity_approvals,
            ("index_rollout_path_mismatch",),
        )
        selected = plan.with_selected_actions((action.action_id,))
        server = FakeAppServer()

        report = execute_manual_delete(
            selected,
            catalog_builder=lambda: catalog,
            approved_plan_fingerprint=selected.plan_fingerprint or "",
            clients_closed=True,
            app_server_factory=lambda **_kwargs: server,
            binary_resolver=lambda _hint: Path("codex"),
            verifier=lambda _finding: VerificationResult(deleted=True),
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(server.deleted_thread_ids, ["mismatch-root"])
        self.assertEqual(report.results[0].status, "deleted")

    def test_invalid_indexed_rollout_path_cannot_receive_missing_path_approval(
        self,
    ) -> None:
        catalog = self.single_native_catalog(
            "invalid-mismatch-root",
            index_mismatch=True,
        )
        indexed_path = self.home / "sessions" / "missing-indexed-rollout.jsonl"
        indexed_path.mkdir()

        action = build_manual_delete_plan(catalog).actions[0]

        self.assertFalse(action.available)
        self.assertEqual(action.integrity_approvals, ())
        self.assertTrue(
            any("not a regular file" in reason for reason in action.unavailable_reasons)
        )

    def test_execution_requires_clients_closed_and_matching_fingerprint(self) -> None:
        record = self.record("guarded")
        selected = build_manual_delete_plan(
            SessionCatalog(records=(record,))
        ).with_selected_actions(("guarded",))

        for clients_closed, fingerprint in (
            (False, selected.plan_fingerprint or ""),
            (True, "v1:not-approved"),
        ):
            with self.assertRaises(ManualDeletePlanError):
                execute_manual_delete(
                    selected,
                    catalog_builder=lambda: SessionCatalog(records=(record,)),
                    approved_plan_fingerprint=fingerprint,
                    clients_closed=clients_closed,
                )


if __name__ == "__main__":
    unittest.main()
