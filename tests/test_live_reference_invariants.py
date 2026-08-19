from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from local_agent_record_janitor.cleaner import VerificationResult, scan_adapters
from local_agent_record_janitor.inventory import (
    FrontendSessionRecord,
    SessionCatalog,
    build_codex_thread_catalog,
    build_session_catalog,
    select_codex_threads,
    select_managed_conversations,
)
from local_agent_record_janitor.manual_delete import (
    ManualDeleteSelectionError,
    build_manual_delete_plan,
    execute_manual_delete,
)
from local_agent_record_janitor.models import Finding
from local_agent_record_janitor.path_identity import canonical_existing_path_key
from local_agent_record_janitor.planning import (
    ActionKind,
    build_cleanup_plan,
)

from tests.support import create_thread_index, write_rollout


class StaticFrontendAdapter:
    name = "static-frontend"

    def __init__(
        self,
        *,
        codex_home: Path,
        records: tuple[FrontendSessionRecord, ...] = (),
        codex_bin_hint: Path | None = None,
    ) -> None:
        self.codex_home = codex_home
        self.database = codex_home.parent / "frontend.sqlite"
        self.codex_bin_hint = codex_bin_hint
        self._records = records

    def list_sessions(self) -> list[FrontendSessionRecord]:
        return list(self._records)


class TrackingAppServer:
    def __init__(self) -> None:
        self.deleted_thread_ids: list[str] = []

    def __enter__(self) -> TrackingAppServer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        self.deleted_thread_ids.append(thread_id)


class LiveFrontendReferenceInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        root_rollout = write_rollout(
            self.home,
            "root-thread",
            originator="codex_cli_rs",
        )
        child_rollout = write_rollout(
            self.home,
            "child-thread",
            originator="codex_cli_rs",
        )
        create_thread_index(
            self.home,
            [
                {"id": "root-thread", "rollout_path": str(root_rollout)},
                {"id": "child-thread", "rollout_path": str(child_rollout)},
            ],
            spawn_edges=[
                {
                    "parent_thread_id": "root-thread",
                    "child_thread_id": "child-thread",
                }
            ],
        )

    def frontend(
        self,
        thread_id: str,
        *,
        live: bool,
        platform: str = "aionui",
    ) -> FrontendSessionRecord:
        return FrontendSessionRecord(
            platform=platform,
            platform_session_id=f"{platform}-{thread_id}",
            thread_id=thread_id,
            database=self.root / f"{platform}.sqlite",
            codex_home=self.home,
            backend="codex",
            status="active" if live else "deleted",
            updated_at_ms=123,
            is_live=live,
        )

    def catalog(
        self,
        *records: FrontendSessionRecord,
    ) -> SessionCatalog:
        return build_session_catalog(
            [
                StaticFrontendAdapter(
                    codex_home=self.home,
                    records=tuple(records),
                )
            ]
        )

    def test_canonical_function_names_are_direct_compatibility_aliases(self) -> None:
        self.assertIs(build_codex_thread_catalog, build_session_catalog)
        self.assertIs(select_codex_threads, select_managed_conversations)

    def test_live_aionui_root_reference_blocks_delete(self) -> None:
        catalog = self.catalog(self.frontend("root-thread", live=True))
        root = next(
            record for record in catalog.records if record.thread_id == "root-thread"
        )

        self.assertFalse(root.deletable)
        self.assertTrue(
            any("live frontend reference" in blocker.lower() for blocker in root.blockers)
        )

    def test_live_aionui_child_reference_blocks_parent_cascade(self) -> None:
        catalog = self.catalog(self.frontend("child-thread", live=True))
        root = next(
            record for record in catalog.records if record.thread_id == "root-thread"
        )

        self.assertEqual(root.descendant_thread_ids, ("child-thread",))
        self.assertFalse(root.deletable)
        self.assertTrue(
            any(
                "live frontend references" in blocker.lower()
                and "child-thread" in blocker
                for blocker in root.blockers
            )
        )

    def test_non_live_frontend_references_do_not_block(self) -> None:
        catalog = self.catalog(
            self.frontend("root-thread", live=False),
            self.frontend("child-thread", live=False),
        )

        self.assertTrue(all(record.deletable for record in catalog.records))

    def test_manual_plan_distrusts_forged_deletable_live_child(self) -> None:
        catalog = self.catalog(self.frontend("child-thread", live=True))
        forged = replace(
            catalog,
            records=tuple(
                replace(record, deletable=True, blockers=())
                for record in catalog.records
            ),
        )

        plan = build_manual_delete_plan(forged)
        root_action = next(
            action for action in plan.actions if action.thread_id == "root-thread"
        )

        self.assertFalse(root_action.available)
        self.assertTrue(
            any("live aionui frontend reference" in reason.lower() for reason in root_action.unavailable_reasons)
        )
        with self.assertRaises(ManualDeleteSelectionError):
            plan.with_selected_actions((root_action.action_id,))

    def test_live_aionui_drift_after_approval_sends_zero_delete_requests(self) -> None:
        approved_catalog = self.catalog(self.frontend("child-thread", live=False))
        live_catalog = self.catalog(self.frontend("child-thread", live=True))
        selected = build_manual_delete_plan(approved_catalog).with_selected_actions(
            ("root-thread",)
        )
        calls = 0

        def catalog_builder() -> SessionCatalog:
            nonlocal calls
            calls += 1
            return approved_catalog if calls == 1 else live_catalog

        server = TrackingAppServer()
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
        self.assertIn("live aionui frontend reference", report.results[0].error or "")


class StoreWideRuntimeInvariantTests(unittest.TestCase):
    def test_no_finding_adapter_hint_is_retained_and_conflict_blocks_action(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "codex-home"
            home.mkdir()
            hint_a = root / "runtime-a" / "codex.exe"
            hint_b = root / "runtime-b" / "codex.exe"
            finding = Finding(
                platform="adapter-a",
                platform_session_id="frontend-thread",
                thread_id="thread-id",
                reason="Codex thread index remains but its rollout is missing",
                platform_db=root / "adapter-a.sqlite",
                codex_home=home,
                codex_indexed=True,
                codex_bin_hint=hint_a,
                details={
                    "finding_type": "index_missing_rollout",
                    "thread_delete_supported": True,
                    "cleanable": True,
                },
            )

            adapter_a = StaticFrontendAdapter(
                codex_home=home,
                codex_bin_hint=hint_a,
            )
            adapter_a.name = "adapter-a"
            adapter_a.scan = lambda: [finding]  # type: ignore[attr-defined]
            adapter_a.live_thread_ids = ()  # type: ignore[attr-defined]
            adapter_b = StaticFrontendAdapter(
                codex_home=home,
                codex_bin_hint=hint_b,
            )
            adapter_b.name = "adapter-b"
            adapter_b.scan = lambda: []  # type: ignore[attr-defined]
            adapter_b.live_thread_ids = ()  # type: ignore[attr-defined]

            report = scan_adapters([adapter_a, adapter_b])  # type: ignore[list-item]
            candidates = report.findings[0].details["codex_bin_hint_candidates"]
            plan = build_cleanup_plan(report)
            delete_action = next(
                action
                for action in plan.actions
                if action.kind is ActionKind.DELETE_CONVERSATION
            )

        self.assertEqual(
            set(candidates),
            {
                canonical_existing_path_key(hint_a),
                canonical_existing_path_key(hint_b),
            },
        )
        self.assertTrue(
            any("conflicting codex executable hints" in error.lower() for error in plan.storages[0].errors)
        )
        self.assertFalse(delete_action.available)


if __name__ == "__main__":
    unittest.main()
