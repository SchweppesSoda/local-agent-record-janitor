from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Callable

from local_agent_record_janitor.adapters import CindyAdapter
from local_agent_record_janitor.adapters.native import NativeIntegrityAdapter
from local_agent_record_janitor.cleaner import (
    ExpectedDeletionScope,
    VerificationResult,
    clean_findings,
    deduplicate_findings,
    finding_key,
    scan_adapters,
)
from local_agent_record_janitor.codex_state import (
    find_thread_rollouts,
    rollout_state_fingerprint,
)
from local_agent_record_janitor.discovery import choose_codex_binary, resolve_cindy_profiles
from local_agent_record_janitor.models import Finding, RolloutRecord
from local_agent_record_janitor.planning import ActionKind, build_cleanup_plan

from tests.support import create_cindy_database, create_thread_index, write_rollout


class FakeAppServer:
    def __init__(
        self,
        callback: Callable[[str], None] | None = None,
        *,
        enter_callback: Callable[[], None] | None = None,
    ) -> None:
        self.callback = callback
        self.enter_callback = enter_callback
        self.deleted_thread_ids: list[str] = []

    def __enter__(self) -> FakeAppServer:
        if self.enter_callback is not None:
            self.enter_callback()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        self.deleted_thread_ids.append(thread_id)
        if self.callback is not None:
            self.callback(thread_id)


class CleanupExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()

    def test_cross_database_live_owner_reference_guards_local_tombstone(self) -> None:
        cindy_root = self.root / "CindyGlobal"
        home = cindy_root / "codex-home"
        home.mkdir(parents=True)
        thread_id = "shared-cross-db-thread"
        rollout = write_rollout(home, thread_id, originator="cindy")
        create_thread_index(home, [{"id": thread_id, "rollout_path": str(rollout)}])
        base_row = {
            "sdk_session_id": thread_id,
            "source": "desktop",
            "created_at": 1,
            "updated_at": 2,
            "parent_session_id": None,
            "agent_kind": "codex",
        }
        local = cindy_root / "cindy-local-v1.db"
        owner = cindy_root / "cindy-owner-fixture.db"
        create_cindy_database(
            local,
            [{**base_row, "id": "local-deleted", "status": "deleted"}],
        )
        create_cindy_database(
            owner,
            [{**base_row, "id": "owner-live", "status": "active"}],
        )
        adapters = [
            CindyAdapter(
                database=profile.database,
                codex_home=profile.codex_home,
                cindy_root=profile.root,
            )
            for profile in resolve_cindy_profiles(self.root, root=cindy_root)
        ]

        scan = scan_adapters(adapters)
        finding = scan.findings[0]
        plan = build_cleanup_plan(scan)
        action = next(
            item for item in plan.actions if item.kind is ActionKind.DELETE_CONVERSATION
        )
        server = FakeAppServer()
        report = clean_findings(
            (finding,),
            explicit_selection=True,
            app_server_factory=lambda **_kwargs: server,
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(len(scan.findings), 1)
        self.assertTrue(finding.details["live_reference_guard"])
        self.assertTrue(finding.details["live_reference_self"])
        self.assertFalse(finding.details["cleanable"])
        self.assertFalse(action.available)
        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.succeeded, 0)

    def finding(
        self,
        thread_id: str,
        *,
        rollout_path: Path | None = None,
    ) -> Finding:
        if rollout_path is None:
            rollout_path = write_rollout(
                self.codex_home,
                thread_id,
                originator="test",
            )
        rollout = RolloutRecord(
            thread_id=thread_id,
            path=rollout_path,
            originator="test",
            source="app-server",
            cwd=str(self.root),
            timestamp=None,
            archived=False,
        )
        return Finding(
            platform="test",
            platform_session_id=f"frontend-{thread_id}",
            thread_id=thread_id,
            reason="test orphan",
            platform_db=self.root / "frontend.sqlite",
            codex_home=self.codex_home,
            rollout=rollout,
            details={
                "cleanable": True,
                "thread_delete_supported": True,
            },
        )

    def duplicate_integrity_finding(
        self,
        thread_id: str = "duplicate-thread",
    ) -> tuple[Finding, tuple[Path, Path]]:
        finding = self.finding(thread_id)
        assert finding.rollout is not None
        archived_path = write_rollout(
            self.codex_home,
            thread_id,
            originator="test",
            archived=True,
        )
        finding.platform = "native"
        finding.details = {
            "finding_type": "duplicate_rollout",
            "rollout_paths": [
                str(finding.rollout.path),
                str(archived_path),
            ],
            "rollout_count": 2,
            "thread_delete_supported": False,
            "needs_quarantine": False,
            "cleanable": False,
            "cleanup_blocked_reason": (
                "Official deletion has not been verified to remove every "
                "duplicate rollout; preserve all copies for manual review."
            ),
            "manual_review_required": True,
        }
        return finding, (finding.rollout.path, archived_path)

    def path_mismatch_integrity_finding(
        self,
        thread_id: str = "path-mismatch",
    ) -> tuple[Finding, Path, Path]:
        finding = self.finding(thread_id)
        assert finding.rollout is not None
        indexed_path = (
            self.codex_home / "sessions" / "missing-indexed-path.jsonl"
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": thread_id,
                    "rollout_path": str(indexed_path),
                }
            ],
        )
        finding.platform = "native"
        finding.codex_indexed = True
        finding.details = {
            "finding_type": "index_rollout_path_mismatch",
            "indexed_rollout_path": str(indexed_path),
            "actual_rollout_paths": [str(finding.rollout.path)],
            "thread_delete_supported": False,
            "needs_quarantine": False,
            "cleanable": False,
            "cleanup_blocked_reason": (
                "The alternate rollout may be recoverable; deletion is not "
                "a safe path repair."
            ),
            "manual_review_required": True,
        }
        return finding, finding.rollout.path, indexed_path

    def clean(
        self,
        findings: list[Finding],
        *,
        server: FakeAppServer | None = None,
        verifier=None,
        approved_descendants=None,
        expected_scopes=None,
        approved_integrity_deletes=None,
        pre_delete_validator=None,
    ):
        server = server or FakeAppServer()
        kwargs = {}
        if verifier is not None:
            kwargs["verifier"] = verifier
        return clean_findings(
            findings,
            app_server_factory=lambda **_kwargs: server,
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
            approved_descendants=approved_descendants,
            expected_scopes=expected_scopes,
            approved_integrity_deletes=approved_integrity_deletes,
            pre_delete_validator=pre_delete_validator,
            **kwargs,
        )

    def test_pre_delete_validator_blocks_request_and_remaining_batch(self) -> None:
        first = self.finding("guarded-first")
        second = self.finding("guarded-second")
        assert first.rollout is not None
        assert second.rollout is not None
        server = FakeAppServer()
        calls: list[str] = []

        def validator(finding: Finding) -> None:
            calls.append(finding.thread_id)
            raise RuntimeError("live frontend reference appeared")

        report = self.clean(
            [first, second],
            server=server,
            expected_scopes={
                finding_key(first): ExpectedDeletionScope(
                    rollout_paths=(str(first.rollout.path),),
                ),
                finding_key(second): ExpectedDeletionScope(
                    rollout_paths=(str(second.rollout.path),),
                ),
            },
            pre_delete_validator=validator,
        )

        self.assertEqual(calls, ["guarded-first"])
        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual([item.status for item in report.results], ["unknown", "unknown"])
        self.assertTrue(
            all("no further deletion request" in (item.error or "") for item in report.results)
        )

    def rewrite_first_payload(
        self,
        path: Path,
        **updates: object,
    ) -> None:
        lines = path.read_text(encoding="utf-8").splitlines()
        first_record = json.loads(lines[0])
        first_record["payload"].update(updates)
        path.write_text(
            "\n".join([json.dumps(first_record), *lines[1:]]) + "\n",
            encoding="utf-8",
        )

    def assert_fingerprint_drift_blocks_delete(
        self,
        thread_id: str,
        mutator: Callable[[Path], None],
    ) -> str:
        finding = self.finding(thread_id)
        assert finding.rollout is not None
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": thread_id,
                    "rollout_path": str(finding.rollout.path),
                }
            ],
        )
        record = find_thread_rollouts(self.codex_home, thread_id)[0]
        expected_fingerprint = rollout_state_fingerprint(record)
        server = FakeAppServer(
            enter_callback=lambda: mutator(finding.rollout.path)
        )

        report = self.clean(
            [finding],
            server=server,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    indexed_thread_ids=(thread_id,),
                    rollout_paths=(str(finding.rollout.path),),
                    rollout_state_fingerprints=(
                        expected_fingerprint,
                    ),
                )
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        return report.results[0].error or ""

    def native_orphan_plan_scope(
        self,
        *,
        parent_id: str = "missing-native-parent",
        child_id: str = "native-orphan-child",
    ) -> tuple[Finding, object, ExpectedDeletionScope]:
        source = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent_id,
                    "depth": 1,
                }
            }
        }
        rollout_path = write_rollout(
            self.codex_home,
            child_id,
            originator="Codex Desktop",
            source=source,
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": child_id,
                    "rollout_path": str(rollout_path),
                    "source": source,
                    "thread_source": "subagent",
                }
            ],
            spawn_edges=[
                {
                    "parent_thread_id": parent_id,
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            ],
        )
        findings = NativeIntegrityAdapter(
            codex_home=self.codex_home,
        ).scan()
        finding = next(
            finding
            for finding in findings
            if (
                finding.thread_id == child_id
                and finding.details.get("finding_type")
                == "orphaned_subagent_thread"
            )
        )
        action = next(
            action
            for action in build_cleanup_plan([finding]).actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        scope = ExpectedDeletionScope(
            descendant_thread_ids=tuple(
                action.impact.descendant_thread_ids
            ),
            indexed_thread_ids=tuple(
                action.impact.indexed_thread_ids
            ),
            rollout_paths=tuple(action.impact.rollout_paths),
            rollout_state_fingerprints=tuple(
                action.impact.rollout_state_fingerprints
            ),
        )
        return finding, action, scope

    def native_residual_plan_scope(
        self,
        *,
        parent_id: str = "residual-parent",
        child_id: str = "residual-child",
    ) -> tuple[Finding, object, ExpectedDeletionScope]:
        parent_path = write_rollout(
            self.codex_home,
            parent_id,
            originator="Codex Desktop",
        )
        child_source = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent_id,
                    "depth": 1,
                }
            }
        }
        write_rollout(
            self.codex_home,
            child_id,
            originator="Codex Desktop",
            source=child_source,
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": parent_id,
                    "rollout_path": str(parent_path),
                }
            ],
            spawn_edges=[
                {
                    "parent_thread_id": parent_id,
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            ],
        )
        scan_report = scan_adapters(
            [
                NativeIntegrityAdapter(
                    codex_home=self.codex_home,
                )
            ]
        )
        finding = next(
            item
            for item in scan_report.findings
            if item.thread_id == child_id
        )
        action = next(
            item
            for item in build_cleanup_plan(scan_report).actions
            if (
                item.kind is ActionKind.DELETE_CONVERSATION
                and item.target.thread_id == child_id
            )
        )
        scope = ExpectedDeletionScope(
            descendant_thread_ids=action.impact.descendant_thread_ids,
            indexed_thread_ids=action.impact.indexed_thread_ids,
            rollout_paths=action.impact.rollout_paths,
            rollout_state_fingerprints=(
                action.impact.rollout_state_fingerprints
            ),
        )
        return finding, action, scope

    def test_request_error_still_runs_verification_and_can_report_deleted(
        self,
    ) -> None:
        finding = self.finding("request-error")
        verification_calls: list[str] = []

        def verifier(target: Finding) -> VerificationResult:
            verification_calls.append(target.thread_id)
            return VerificationResult(deleted=True)

        server = FakeAppServer(
            lambda _thread_id: (_ for _ in ()).throw(
                TimeoutError("delete request timed out")
            )
        )

        report = self.clean([finding], server=server, verifier=verifier)

        self.assertEqual(verification_calls, [finding.thread_id])
        self.assertEqual(report.results[0].status, "deleted")
        self.assertTrue(report.results[0].succeeded)
        self.assertIn("timed out", report.results[0].request_error or "")
        self.assertIsNone(report.results[0].error)

    def test_conflicting_binary_hints_block_entire_home_before_start(
        self,
    ) -> None:
        first = self.finding("hint-conflict-first")
        second = self.finding("hint-conflict-second")
        first.codex_bin_hint = self.root / "bin-a" / "codex.exe"
        second.codex_bin_hint = self.root / "bin-b" / "codex.exe"
        binary_calls: list[Path | None] = []
        factory_calls: list[bool] = []
        server = FakeAppServer()

        report = clean_findings(
            [first, second],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or server
            ),
            binary_resolver=lambda hint: (
                binary_calls.append(hint) or Path("codex")
            ),
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(binary_calls, [])
        self.assertEqual(factory_calls, [])
        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(result.status == "unknown" for result in report.results)
        )
        self.assertTrue(
            all(
                "conflicting Codex executable hints"
                in (result.error or "")
                for result in report.results
            )
        )

    def test_equivalent_relative_and_absolute_binary_hints_do_not_conflict(
        self,
    ) -> None:
        first = self.finding("hint-equivalent-first")
        second = self.finding("hint-equivalent-second")
        absolute_hint = (
            Path.cwd() / "equivalent-bin" / "codex.exe"
        ).resolve()
        relative_hint = Path(
            os.path.relpath(absolute_hint, Path.cwd())
        )
        first.codex_bin_hint = relative_hint
        second.codex_bin_hint = absolute_hint
        binary_calls: list[Path | None] = []
        factory_calls: list[bool] = []
        server = FakeAppServer()

        report = clean_findings(
            [first, second],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or server
            ),
            binary_resolver=lambda hint: (
                binary_calls.append(hint) or Path("codex")
            ),
            verifier=lambda _target: VerificationResult(deleted=True),
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(factory_calls, [True])
        self.assertEqual(
            binary_calls,
            [
                Path(
                    os.path.normcase(
                        os.path.abspath(absolute_hint)
                    )
                )
            ],
        )
        self.assertEqual(
            server.deleted_thread_ids,
            [first.thread_id, second.thread_id],
        )
        self.assertTrue(
            all(result.status == "deleted" for result in report.results)
        )

    def test_conflicting_hints_are_not_hidden_by_target_deduplication(
        self,
    ) -> None:
        first = self.finding("deduplicated-hint-conflict")
        assert first.rollout is not None
        second = self.finding(
            first.thread_id,
            rollout_path=first.rollout.path,
        )
        first.codex_bin_hint = self.root / "bin-a" / "codex.exe"
        second.codex_bin_hint = self.root / "bin-b" / "codex.exe"
        factory_calls: list[bool] = []

        deduplicated = deduplicate_findings([first, second])
        self.assertEqual(
            deduplicated[0].details["codex_bin_hint_candidates"],
            sorted(
                [
                    os.path.normcase(
                        os.path.abspath(first.codex_bin_hint)
                    ),
                    os.path.normcase(
                        os.path.abspath(second.codex_bin_hint)
                    ),
                ]
            ),
        )

        report = clean_findings(
            deduplicated,
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(factory_calls, [])
        self.assertEqual(len(report.planned), 1)
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "conflicting Codex executable hints",
            report.results[0].error or "",
        )

    def test_selected_hint_disappearing_in_resolver_blocks_app_server(
        self,
    ) -> None:
        finding = self.finding("hint-disappears")
        executable = self.root / "bin" / "codex.exe"
        executable.parent.mkdir()
        executable.touch()
        finding.codex_bin_hint = executable
        resolver_calls: list[Path | None] = []
        factory_calls: list[bool] = []
        server = FakeAppServer()

        def resolver(hint: Path | None) -> Path | None:
            resolver_calls.append(hint)
            assert hint is not None
            hint.unlink()
            return choose_codex_binary(hint)

        report = clean_findings(
            [finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or server
            ),
            binary_resolver=resolver,
            verification_attempts=1,
            verification_interval=0,
        )

        self.assertEqual(
            resolver_calls,
            [
                Path(
                    os.path.normcase(
                        os.path.abspath(executable)
                    )
                )
            ],
        )
        self.assertEqual(factory_calls, [])
        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "No Codex executable was found",
            report.results[0].error or "",
        )

    def test_old_boolean_verification_results_map_to_four_state_results(
        self,
    ) -> None:
        finding = self.finding("verification-states")
        cases = [
            (VerificationResult(deleted=True), "deleted"),
            (VerificationResult(deleted=False), "not_deleted"),
            (
                VerificationResult(
                    deleted=False,
                    status="partial",
                    remaining_artifacts=("child.jsonl",),
                ),
                "partial",
            ),
            (
                VerificationResult(
                    deleted=False,
                    error="index unreadable",
                ),
                "unknown",
            ),
        ]

        for verification, expected_status in cases:
            with self.subTest(expected_status):
                report = self.clean(
                    [finding],
                    verifier=lambda _target, result=verification: result,
                )
                self.assertEqual(report.results[0].status, expected_status)

    def test_changed_descendant_closure_blocks_before_app_server_start(
        self,
    ) -> None:
        root_finding = self.finding("root")
        child_path = write_rollout(
            self.codex_home,
            "new-child",
            originator="test",
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": "root",
                    "rollout_path": str(root_finding.rollout.path),
                },
                {"id": "new-child", "rollout_path": str(child_path)},
            ],
            spawn_edges=[
                {
                    "parent_thread_id": "root",
                    "child_thread_id": "new-child",
                }
            ],
        )
        factory_calls: list[bool] = []

        report = clean_findings(
            [root_finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
            approved_descendants={
                finding_key(root_finding): {"previous-child"}
            },
        )

        self.assertEqual(factory_calls, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("differs from the reviewed scope", report.results[0].error or "")
        self.assertIn("new-child", report.results[0].impacted_thread_ids)
        self.assertIn("previous-child", report.results[0].impacted_thread_ids)

    def test_missing_approved_scope_blocks_fail_closed(self) -> None:
        finding = self.finding("missing-scope")
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            approved_descendants={},
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("scope is missing", report.results[0].error or "")

    def test_parent_and_descendant_selected_as_roots_block_entire_group(
        self,
    ) -> None:
        parent_finding, child_path = self._indexed_parent_and_child()
        child_finding = self.finding(
            "child",
            rollout_path=child_path,
        )
        factory_calls: list[bool] = []
        binary_calls: list[bool] = []

        report = clean_findings(
            [parent_finding, child_finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: (
                binary_calls.append(True) or Path("codex")
            ),
            verification_attempts=1,
            verification_interval=0,
            approved_descendants={
                finding_key(parent_finding): {"child"},
                finding_key(child_finding): set(),
            },
        )

        self.assertEqual(factory_calls, [])
        self.assertEqual(binary_calls, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(result.status == "unknown" for result in report.results)
        )
        self.assertTrue(
            all(
                "所选根目标互相包含" in (result.error or "")
                for result in report.results
            )
        )

    def test_roots_with_shared_approved_descendant_block_entire_group(
        self,
    ) -> None:
        first = self.finding("first-root")
        second = self.finding("second-root")
        factory_calls: list[bool] = []

        report = clean_findings(
            [first, second],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
            approved_descendants={
                finding_key(first): {"shared-task"},
                finding_key(second): {"shared-task"},
            },
        )

        self.assertEqual(factory_calls, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(result.status == "unknown" for result in report.results)
        )
        self.assertTrue(
            all(
                "shared-task" in (result.error or "")
                for result in report.results
            )
        )

    def test_one_stale_scope_blocks_other_supported_root_before_start(
        self,
    ) -> None:
        stale_root, _child_path = self._indexed_parent_and_child()
        normal_root = self.finding("normal-root")
        assert normal_root.rollout is not None
        with closing(
            sqlite3.connect(self.codex_home / "state_5.sqlite")
        ) as connection:
            connection.execute(
                "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
                (normal_root.thread_id, str(normal_root.rollout.path)),
            )
            connection.commit()
        factory_calls: list[bool] = []

        report = clean_findings(
            [stale_root, normal_root],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
            approved_descendants={
                finding_key(stale_root): {"old-child"},
                finding_key(normal_root): set(),
            },
        )

        self.assertEqual(factory_calls, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(result.status == "unknown" for result in report.results)
        )
        self.assertTrue(
            all(
                "no supported target was deleted" in (result.error or "")
                for result in report.results
            )
        )

    def test_scope_change_during_server_enter_blocks_all_delete_requests(
        self,
    ) -> None:
        first_root, _child_path = self._indexed_parent_and_child()
        second_root = self.finding("second-root")
        assert second_root.rollout is not None
        with closing(
            sqlite3.connect(self.codex_home / "state_5.sqlite")
        ) as connection:
            connection.execute(
                "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
                (second_root.thread_id, str(second_root.rollout.path)),
            )
            connection.commit()

        def change_scope_on_enter() -> None:
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    """
                    INSERT INTO thread_spawn_edges (
                        parent_thread_id,
                        child_thread_id,
                        status
                    ) VALUES ('root', 'startup-child', 'open')
                    """
                )
                connection.commit()

        server = FakeAppServer(enter_callback=change_scope_on_enter)

        report = self.clean(
            [first_root, second_root],
            server=server,
            approved_descendants={
                finding_key(first_root): {"child"},
                finding_key(second_root): set(),
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(result.status == "unknown" for result in report.results)
        )
        self.assertTrue(
            all(
                "no deletion request was sent" in (result.error or "")
                for result in report.results
            )
        )

    def test_exact_expected_scope_allows_unchanged_target(self) -> None:
        finding = self.finding("exact-scope")
        assert finding.rollout is not None
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            verifier=lambda _target: VerificationResult(deleted=True),
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    rollout_paths=(str(finding.rollout.path),),
                )
            },
        )

        self.assertEqual(server.deleted_thread_ids, [finding.thread_id])
        self.assertEqual(report.results[0].status, "deleted")

    def test_exact_fingerprint_scope_allows_unchanged_target(self) -> None:
        finding = self.finding("exact-fingerprint")
        assert finding.rollout is not None
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": finding.thread_id,
                    "rollout_path": str(finding.rollout.path),
                }
            ],
        )
        record = find_thread_rollouts(
            self.codex_home,
            finding.thread_id,
        )[0]
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            verifier=lambda _target: VerificationResult(deleted=True),
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    indexed_thread_ids=(finding.thread_id,),
                    rollout_paths=(str(finding.rollout.path),),
                    rollout_state_fingerprints=(
                        rollout_state_fingerprint(record),
                    ),
                )
            },
        )

        self.assertEqual(server.deleted_thread_ids, [finding.thread_id])
        self.assertEqual(report.results[0].status, "deleted")

    def test_conversation_metadata_drift_after_startup_blocks_delete(self) -> None:
        finding = self.finding("metadata-drift")
        assert finding.rollout is not None
        finding.codex_indexed = True
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": finding.thread_id,
                    "rollout_path": str(finding.rollout.path),
                    "agent_nickname": "Before",
                }
            ],
        )
        action = next(
            current
            for current in build_cleanup_plan([finding]).actions
            if current.kind is ActionKind.DELETE_CONVERSATION
        )

        def mutate_metadata() -> None:
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    "UPDATE threads SET agent_nickname = ? WHERE id = ?",
                    ("After", finding.thread_id),
                )
                connection.commit()

        server = FakeAppServer(enter_callback=mutate_metadata)
        report = self.clean(
            [finding],
            server=server,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    descendant_thread_ids=(
                        action.impact.descendant_thread_ids
                    ),
                    indexed_thread_ids=action.impact.indexed_thread_ids,
                    rollout_paths=action.impact.rollout_paths,
                    rollout_state_fingerprints=(
                        action.impact.rollout_state_fingerprints
                    ),
                    conversation_metadata_fingerprints=(
                        action.impact.conversation_metadata_fingerprints
                    ),
                )
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("conversation_metadata_fingerprints", report.results[0].error or "")

    def test_native_orphan_plan_scope_executes_through_cleaner(
        self,
    ) -> None:
        finding, action, scope = self.native_orphan_plan_scope()
        self.assertTrue(action.available)
        self.assertTrue(finding.details["cleanable"])
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            verifier=lambda _target: VerificationResult(deleted=True),
            expected_scopes={finding_key(finding): scope},
        )

        self.assertEqual(server.deleted_thread_ids, [finding.thread_id])
        self.assertEqual(report.results[0].status, "deleted")

    def test_edge_inferred_orphan_without_source_parent_executes(
        self,
    ) -> None:
        parent_id = "edge-only-missing-parent"
        child_id = "edge-only-orphan"
        child_path = write_rollout(
            self.codex_home,
            child_id,
            originator="Codex Desktop",
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": child_id,
                    "rollout_path": str(child_path),
                    "thread_source": "subagent",
                }
            ],
            spawn_edges=[
                {
                    "parent_thread_id": parent_id,
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            ],
        )
        finding = next(
            item
            for item in NativeIntegrityAdapter(
                codex_home=self.codex_home,
            ).scan()
            if (
                item.thread_id == child_id
                and item.details.get("finding_type")
                == "orphaned_subagent_thread"
            )
        )
        action = next(
            item
            for item in build_cleanup_plan([finding]).actions
            if item.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertTrue(action.available)
        scope = ExpectedDeletionScope(
            descendant_thread_ids=action.impact.descendant_thread_ids,
            indexed_thread_ids=action.impact.indexed_thread_ids,
            rollout_paths=action.impact.rollout_paths,
            rollout_state_fingerprints=(
                action.impact.rollout_state_fingerprints
            ),
        )
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            verifier=lambda _target: VerificationResult(deleted=True),
            expected_scopes={finding_key(finding): scope},
        )

        self.assertEqual(server.deleted_thread_ids, [child_id])
        self.assertEqual(report.results[0].status, "deleted")

    def test_one_of_two_orphan_siblings_can_execute_independently(
        self,
    ) -> None:
        parent_id = "shared-missing-parent"
        child_ids = ("selected-child", "untouched-sibling")
        rows = []
        edges = []
        paths: dict[str, Path] = {}
        for child_id in child_ids:
            source = {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": parent_id,
                        "depth": 1,
                    }
                }
            }
            paths[child_id] = write_rollout(
                self.codex_home,
                child_id,
                originator="Codex Desktop",
                source=source,
            )
            rows.append(
                {
                    "id": child_id,
                    "rollout_path": str(paths[child_id]),
                    "source": source,
                    "thread_source": "subagent",
                }
            )
            edges.append(
                {
                    "parent_thread_id": parent_id,
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            )
        create_thread_index(
            self.codex_home,
            rows,
            spawn_edges=edges,
        )
        findings = NativeIntegrityAdapter(
            codex_home=self.codex_home,
        ).scan()
        finding = next(
            item
            for item in findings
            if (
                item.thread_id == child_ids[0]
                and item.details.get("finding_type")
                == "orphaned_subagent_thread"
            )
        )
        action = next(
            item
            for item in build_cleanup_plan([finding]).actions
            if item.kind is ActionKind.DELETE_CONVERSATION
        )
        scope = ExpectedDeletionScope(
            descendant_thread_ids=action.impact.descendant_thread_ids,
            indexed_thread_ids=action.impact.indexed_thread_ids,
            rollout_paths=action.impact.rollout_paths,
            rollout_state_fingerprints=(
                action.impact.rollout_state_fingerprints
            ),
        )
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            verifier=lambda _target: VerificationResult(deleted=True),
            expected_scopes={finding_key(finding): scope},
        )

        self.assertEqual(server.deleted_thread_ids, [child_ids[0]])
        self.assertEqual(report.results[0].status, "deleted")
        self.assertTrue(paths[child_ids[1]].is_file())
        with closing(
            sqlite3.connect(self.codex_home / "state_5.sqlite")
        ) as connection:
            sibling_row = connection.execute(
                "SELECT id FROM threads WHERE id = ?",
                (child_ids[1],),
            ).fetchone()
        self.assertEqual(sibling_row, (child_ids[1],))

    def test_duplicate_primary_with_one_orphan_contract_can_execute(
        self,
    ) -> None:
        parent_id = "missing-parent-for-duplicate"
        child_id = "duplicate-orphan-child"
        source = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent_id,
                    "depth": 1,
                }
            }
        }
        active_path = write_rollout(
            self.codex_home,
            child_id,
            originator="Codex Desktop",
            source=source,
        )
        archived_path = write_rollout(
            self.codex_home,
            child_id,
            originator="Codex Desktop",
            source=source,
            archived=True,
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": child_id,
                    "rollout_path": str(active_path),
                    "source": source,
                    "thread_source": "subagent",
                }
            ],
            spawn_edges=[
                {
                    "parent_thread_id": parent_id,
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            ],
        )
        scan_report = scan_adapters(
            [NativeIntegrityAdapter(codex_home=self.codex_home)]
        )
        finding = next(
            item
            for item in scan_report.findings
            if item.thread_id == child_id
        )
        self.assertEqual(
            finding.details.get("finding_type"),
            "duplicate_rollout",
        )
        action = next(
            item
            for item in build_cleanup_plan(scan_report).actions
            if (
                item.kind is ActionKind.DELETE_CONVERSATION
                and item.target.thread_id == child_id
            )
        )
        self.assertTrue(action.available)
        scope = ExpectedDeletionScope(
            descendant_thread_ids=action.impact.descendant_thread_ids,
            indexed_thread_ids=action.impact.indexed_thread_ids,
            rollout_paths=action.impact.rollout_paths,
            rollout_state_fingerprints=(
                action.impact.rollout_state_fingerprints
            ),
        )
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            verifier=lambda _target: VerificationResult(deleted=True),
            expected_scopes={finding_key(finding): scope},
            approved_integrity_deletes={
                finding_key(finding): {"duplicate_rollout"}
            },
        )

        self.assertEqual(
            set(action.impact.rollout_paths),
            {
                str(active_path.resolve()).lower(),
                str(archived_path.resolve()).lower(),
            },
        )
        self.assertEqual(server.deleted_thread_ids, [child_id])
        self.assertEqual(report.results[0].status, "deleted")

    def test_native_orphan_parent_index_reappearance_blocks_delete(
        self,
    ) -> None:
        finding, _action, scope = self.native_orphan_plan_scope()
        parent_id = finding.details["parent_thread_id"]

        def restore_parent_index() -> None:
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
                    (parent_id, None),
                )
                connection.commit()

        server = FakeAppServer(enter_callback=restore_parent_index)
        report = self.clean(
            [finding],
            server=server,
            expected_scopes={finding_key(finding): scope},
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "reappeared in the native index",
            report.results[0].error or "",
        )

    def test_native_orphan_parent_rollout_reappearance_blocks_delete(
        self,
    ) -> None:
        finding, _action, scope = self.native_orphan_plan_scope()
        parent_id = finding.details["parent_thread_id"]

        server = FakeAppServer(
            enter_callback=lambda: write_rollout(
                self.codex_home,
                parent_id,
                originator="Codex Desktop",
            )
        )
        report = self.clean(
            [finding],
            server=server,
            expected_scopes={finding_key(finding): scope},
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "reappeared as a valid rollout",
            report.results[0].error or "",
        )

    def test_native_orphan_incoming_relation_drift_blocks_delete(
        self,
    ) -> None:
        finding, _action, scope = self.native_orphan_plan_scope()

        def change_incoming_relation() -> None:
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    """
                    UPDATE thread_spawn_edges
                    SET parent_thread_id = 'different-parent'
                    WHERE child_thread_id = ?
                    """,
                    (finding.thread_id,),
                )
                connection.commit()

        server = FakeAppServer(enter_callback=change_incoming_relation)
        report = self.clean(
            [finding],
            server=server,
            expected_scopes={finding_key(finding): scope},
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "incoming relation differs",
            report.results[0].error or "",
        )

    def test_native_orphan_multiple_source_parents_block_delete(
        self,
    ) -> None:
        finding, _action, scope = self.native_orphan_plan_scope()
        conflicting_source = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": "different-parent",
                    "depth": 1,
                }
            }
        }

        def conflict_indexed_source() -> None:
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    "UPDATE threads SET source = ? WHERE id = ?",
                    (
                        json.dumps(conflicting_source),
                        finding.thread_id,
                    ),
                )
                connection.commit()

        server = FakeAppServer(enter_callback=conflict_indexed_source)
        report = self.clean(
            [finding],
            server=server,
            expected_scopes={finding_key(finding): scope},
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "conflicting structured source parents",
            report.results[0].error or "",
        )

    def test_native_residual_high_delete_executes_with_target_approval(
        self,
    ) -> None:
        finding, action, scope = self.native_residual_plan_scope()
        self.assertTrue(action.available)
        self.assertEqual(action.risk.value, "high")
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            verifier=lambda _target: VerificationResult(deleted=True),
            expected_scopes={finding_key(finding): scope},
            approved_integrity_deletes={
                finding_key(finding): {"residual_spawn_edge"}
            },
        )

        self.assertEqual(server.deleted_thread_ids, [finding.thread_id])
        self.assertEqual(report.results[0].status, "deleted")

    def test_native_residual_delete_without_target_approval_fails_closed(
        self,
    ) -> None:
        finding, _action, scope = self.native_residual_plan_scope()
        binary_calls: list[bool] = []
        factory_calls: list[bool] = []

        report = clean_findings(
            [finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: (
                binary_calls.append(True) or Path("codex")
            ),
            verification_attempts=1,
            verification_interval=0,
            expected_scopes={finding_key(finding): scope},
        )

        self.assertEqual(binary_calls, [])
        self.assertEqual(factory_calls, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "standalone spawn-edge",
            report.results[0].error or "",
        )

    def test_native_residual_edge_drift_blocks_approved_delete(
        self,
    ) -> None:
        finding, _action, scope = self.native_residual_plan_scope()

        def change_edge_status() -> None:
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    """
                    UPDATE thread_spawn_edges
                    SET status = 'open'
                    WHERE child_thread_id = ?
                    """,
                    (finding.thread_id,),
                )
                connection.commit()

        server = FakeAppServer(enter_callback=change_edge_status)
        report = self.clean(
            [finding],
            server=server,
            expected_scopes={finding_key(finding): scope},
            approved_integrity_deletes={
                finding_key(finding): {"residual_spawn_edge"}
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "incoming edge differs",
            report.results[0].error or "",
        )

    def test_mapping_scope_with_none_fingerprint_remains_compatible(
        self,
    ) -> None:
        finding = self.finding("legacy-none-fingerprint")
        assert finding.rollout is not None
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            verifier=lambda _target: VerificationResult(deleted=True),
            expected_scopes={
                finding_key(finding): {
                    "rollout_paths": [str(finding.rollout.path)],
                    "rollout_state_fingerprints": None,
                }
            },
        )

        self.assertEqual(server.deleted_thread_ids, [finding.thread_id])
        self.assertEqual(report.results[0].status, "deleted")

    def test_originator_drift_with_same_path_and_index_blocks_delete(
        self,
    ) -> None:
        error = self.assert_fingerprint_drift_blocks_delete(
            "originator-drift",
            lambda path: self.rewrite_first_payload(
                path,
                originator="changed-originator",
            ),
        )

        self.assertIn("rollout_state_fingerprints", error)

    def test_full_source_drift_with_same_path_and_index_blocks_delete(
        self,
    ) -> None:
        error = self.assert_fingerprint_drift_blocks_delete(
            "source-drift",
            lambda path: self.rewrite_first_payload(
                path,
                source={"client": {"kind": "changed"}},
            ),
        )

        self.assertIn("rollout_state_fingerprints", error)

    def test_thread_id_drift_with_same_path_and_index_blocks_delete(
        self,
    ) -> None:
        error = self.assert_fingerprint_drift_blocks_delete(
            "thread-id-drift",
            lambda path: self.rewrite_first_payload(
                path,
                id="other-thread-id",
            ),
        )

        self.assertIn("thread identity", error)

    def test_body_append_with_same_path_and_index_blocks_delete(
        self,
    ) -> None:
        def append_body(path: Path) -> None:
            path.write_text(
                path.read_text(encoding="utf-8")
                + '{"type":"event_msg","payload":{"changed":true}}\n',
                encoding="utf-8",
            )

        error = self.assert_fingerprint_drift_blocks_delete(
            "body-append-drift",
            append_body,
        )

        self.assertIn("rollout_state_fingerprints", error)

    def test_stat_only_drift_with_same_path_and_index_blocks_delete(
        self,
    ) -> None:
        def change_stat(path: Path) -> None:
            current = path.stat()
            os.utime(
                path,
                ns=(
                    current.st_atime_ns,
                    current.st_mtime_ns + 1_000_000_000,
                ),
            )

        error = self.assert_fingerprint_drift_blocks_delete(
            "stat-only-drift",
            change_stat,
        )

        self.assertIn("rollout_state_fingerprints", error)

    def test_index_only_missing_rollout_path_matches_empty_expected_paths(
        self,
    ) -> None:
        missing_path = (
            self.codex_home / "sessions" / "missing-index-only.jsonl"
        )
        finding = Finding(
            platform="native",
            platform_session_id="native-index-only",
            thread_id="index-only",
            reason="index remains but rollout is missing",
            platform_db=self.codex_home / "state_5.sqlite",
            codex_home=self.codex_home,
            codex_indexed=True,
            details={
                "finding_type": "index_missing_rollout",
                "indexed_rollout_path": str(missing_path),
                "cleanable": True,
                "thread_delete_supported": True,
            },
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": finding.thread_id,
                    "rollout_path": str(missing_path),
                }
            ],
        )
        server = FakeAppServer()
        action = next(
            action
            for action in build_cleanup_plan([finding]).actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(action.impact.rollout_paths, ())

        report = self.clean(
            [finding],
            server=server,
            verifier=lambda _target: VerificationResult(deleted=True),
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    descendant_thread_ids=tuple(
                        action.impact.descendant_thread_ids
                    ),
                    indexed_thread_ids=tuple(
                        action.impact.indexed_thread_ids
                    ),
                    rollout_paths=tuple(action.impact.rollout_paths),
                )
            },
        )

        self.assertEqual(server.deleted_thread_ids, [finding.thread_id])
        self.assertEqual(report.results[0].status, "deleted")

    def test_missing_index_path_created_during_startup_blocks_group(
        self,
    ) -> None:
        missing_path = (
            self.codex_home / "sessions" / "appeared-on-startup.jsonl"
        )
        finding = Finding(
            platform="native",
            platform_session_id="native-startup-path",
            thread_id="startup-path",
            reason="index remains but rollout is missing",
            platform_db=self.codex_home / "state_5.sqlite",
            codex_home=self.codex_home,
            codex_indexed=True,
            details={
                "finding_type": "index_missing_rollout",
                "indexed_rollout_path": str(missing_path),
                "cleanable": True,
                "thread_delete_supported": True,
            },
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": finding.thread_id,
                    "rollout_path": str(missing_path),
                }
            ],
        )
        normal_finding = self.finding("normal-with-startup-path")
        assert normal_finding.rollout is not None

        def create_path_on_enter() -> None:
            missing_path.parent.mkdir(parents=True, exist_ok=True)
            missing_path.write_text(
                "{metadata became damaged while starting}\n",
                encoding="utf-8",
            )

        server = FakeAppServer(enter_callback=create_path_on_enter)
        report = self.clean(
            [finding, normal_finding],
            server=server,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    indexed_thread_ids=(finding.thread_id,),
                    rollout_paths=(),
                ),
                finding_key(normal_finding): ExpectedDeletionScope(
                    rollout_paths=(str(normal_finding.rollout.path),),
                ),
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(result.status == "unknown" for result in report.results)
        )
        self.assertTrue(
            all(
                "metadata does not belong" in (result.error or "")
                for result in report.results
            )
        )

    def test_approved_duplicate_integrity_delete_can_remove_all_copies(
        self,
    ) -> None:
        finding, rollout_paths = self.duplicate_integrity_finding()

        def delete_all_copies(_thread_id: str) -> None:
            for path in rollout_paths:
                path.unlink()

        server = FakeAppServer(delete_all_copies)
        report = self.clean(
            [finding],
            server=server,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    rollout_paths=tuple(str(path) for path in rollout_paths),
                )
            },
            approved_integrity_deletes={
                finding_key(finding): {"duplicate_rollout"}
            },
        )

        self.assertEqual(report.results[0].status, "deleted")
        self.assertEqual(report.results[0].remaining_artifacts, ())
        self.assertEqual(server.deleted_thread_ids, [finding.thread_id])

    def test_approved_path_mismatch_integrity_delete_can_remove_target(
        self,
    ) -> None:
        finding, actual_path, indexed_path = (
            self.path_mismatch_integrity_finding()
        )
        action = next(
            action
            for action in build_cleanup_plan([finding]).actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(
            action.impact.rollout_paths,
            (
                str(actual_path.resolve()).lower(),
            ),
        )

        def delete_target(_thread_id: str) -> None:
            actual_path.unlink()
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    "DELETE FROM threads WHERE id = ?",
                    (finding.thread_id,),
                )
                connection.commit()

        report = self.clean(
            [finding],
            server=FakeAppServer(delete_target),
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    descendant_thread_ids=tuple(
                        action.impact.descendant_thread_ids
                    ),
                    indexed_thread_ids=tuple(
                        action.impact.indexed_thread_ids
                    ),
                    rollout_paths=tuple(action.impact.rollout_paths),
                )
            },
            approved_integrity_deletes={
                finding_key(finding): {
                    "index_rollout_path_mismatch"
                }
            },
        )

        self.assertEqual(report.results[0].status, "deleted")

    def test_integrity_approval_without_exact_expected_scope_fails_closed(
        self,
    ) -> None:
        finding, _rollout_paths = self.duplicate_integrity_finding()
        factory_calls: list[bool] = []

        report = clean_findings(
            [finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
            approved_integrity_deletes={
                finding_key(finding): {"duplicate_rollout"}
            },
        )

        self.assertEqual(factory_calls, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "requires an exact expected_scopes entry",
            report.results[0].error or "",
        )

    def test_integrity_approval_never_authorizes_metadata_mismatch(
        self,
    ) -> None:
        finding = self.finding("metadata-mismatch")
        assert finding.rollout is not None
        finding.platform = "native"
        finding.details = {
            "finding_type": "index_rollout_metadata_mismatch",
            "indexed_rollout_path": str(finding.rollout.path),
            "metadata_thread_id": "other-thread",
            "thread_delete_supported": False,
            "needs_quarantine": False,
            "cleanable": False,
            "cleanup_blocked_reason": (
                "The indexed file belongs to another thread; automatic "
                "deletion could remove unrelated data."
            ),
        }
        factory_calls: list[bool] = []

        report = clean_findings(
            [finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    rollout_paths=(str(finding.rollout.path),),
                )
            },
            approved_integrity_deletes={
                finding_key(finding): {
                    "index_rollout_metadata_mismatch"
                }
            },
        )

        self.assertEqual(factory_calls, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "unsupported finding type",
            report.results[0].error or "",
        )

    def test_integrity_approval_does_not_bypass_live_reference(
        self,
    ) -> None:
        finding, rollout_paths = self.duplicate_integrity_finding()
        normal_finding = self.finding("normal-alongside-live")
        assert normal_finding.rollout is not None
        finding.details["live_reference_guard"] = True
        finding.details["live_reference_count"] = 1
        finding.details["cleanup_blocked_reason"] = (
            "Official deletion has not been verified to remove every "
            "duplicate rollout; preserve all copies for manual review. "
            "The thread is still live in a frontend."
        )
        factory_calls: list[bool] = []

        report = clean_findings(
            [finding, normal_finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    rollout_paths=tuple(str(path) for path in rollout_paths),
                ),
                finding_key(normal_finding): ExpectedDeletionScope(
                    rollout_paths=(str(normal_finding.rollout.path),),
                ),
            },
            approved_integrity_deletes={
                finding_key(finding): {"duplicate_rollout"}
            },
        )

        self.assertEqual(factory_calls, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(
                "still live" in (result.error or "")
                for result in report.results
            )
        )

    def test_integrity_approval_does_not_bypass_insufficient_ownership(
        self,
    ) -> None:
        finding, rollout_paths = self.duplicate_integrity_finding()
        normal_finding = self.finding("normal-alongside-insufficient")
        assert normal_finding.rollout is not None
        finding.details["ownership_status"] = "insufficient"
        binary_calls: list[bool] = []
        factory_calls: list[bool] = []
        server = FakeAppServer()

        report = clean_findings(
            [finding, normal_finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or server
            ),
            binary_resolver=lambda _hint: (
                binary_calls.append(True) or Path("codex")
            ),
            verification_attempts=1,
            verification_interval=0,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    rollout_paths=tuple(str(path) for path in rollout_paths),
                ),
                finding_key(normal_finding): ExpectedDeletionScope(
                    rollout_paths=(str(normal_finding.rollout.path),),
                ),
            },
            approved_integrity_deletes={
                finding_key(finding): {"duplicate_rollout"}
            },
        )

        self.assertEqual(binary_calls, [])
        self.assertEqual(factory_calls, [])
        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(result.status == "unknown" for result in report.results)
        )

    def test_integrity_approval_does_not_bypass_unavailable_cascade_check(
        self,
    ) -> None:
        finding, rollout_paths = self.duplicate_integrity_finding()
        normal_finding = self.finding("normal-alongside-unavailable")
        assert normal_finding.rollout is not None
        finding.details["cascade_check_available"] = False
        binary_calls: list[bool] = []
        factory_calls: list[bool] = []
        server = FakeAppServer()

        report = clean_findings(
            [finding, normal_finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or server
            ),
            binary_resolver=lambda _hint: (
                binary_calls.append(True) or Path("codex")
            ),
            verification_attempts=1,
            verification_interval=0,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    rollout_paths=tuple(str(path) for path in rollout_paths),
                ),
                finding_key(normal_finding): ExpectedDeletionScope(
                    rollout_paths=(str(normal_finding.rollout.path),),
                ),
            },
            approved_integrity_deletes={
                finding_key(finding): {"duplicate_rollout"}
            },
        )

        self.assertEqual(binary_calls, [])
        self.assertEqual(factory_calls, [])
        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(result.status == "unknown" for result in report.results)
        )

    def test_integrity_approval_does_not_bypass_hard_additional_finding(
        self,
    ) -> None:
        finding, rollout_paths = self.duplicate_integrity_finding()
        finding.details["additional_findings"] = [
            {
                "platform": "native",
                "platform_session_id": "native-hard",
                "reason": "source identity conflict",
                "details": {
                    "finding_type": "orphaned_subagent_thread",
                    "source_conflict": True,
                    "cleanable": False,
                    "thread_delete_supported": True,
                    "cleanup_blocked_reason": (
                        "The source parent conflicts with another thread."
                    ),
                },
            }
        ]
        factory_calls: list[bool] = []

        report = clean_findings(
            [finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    rollout_paths=tuple(str(path) for path in rollout_paths),
                )
            },
            approved_integrity_deletes={
                finding_key(finding): {"duplicate_rollout"}
            },
        )

        self.assertEqual(factory_calls, [])
        self.assertIn(
            "source parent conflicts",
            report.results[0].error or "",
        )

    def test_integrity_approval_does_not_bypass_quarantine_requirement(
        self,
    ) -> None:
        finding, rollout_paths = self.duplicate_integrity_finding()
        finding.details["needs_quarantine"] = True
        factory_calls: list[bool] = []

        report = clean_findings(
            [finding],
            app_server_factory=lambda **_kwargs: (
                factory_calls.append(True) or FakeAppServer()
            ),
            binary_resolver=lambda _hint: Path("codex"),
            verification_attempts=1,
            verification_interval=0,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    rollout_paths=tuple(str(path) for path in rollout_paths),
                )
            },
            approved_integrity_deletes={
                finding_key(finding): {"duplicate_rollout"}
            },
        )

        self.assertEqual(factory_calls, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("manual review", report.results[0].error or "")

    def test_integrity_scope_drift_after_startup_sends_no_delete(
        self,
    ) -> None:
        finding, rollout_paths = self.duplicate_integrity_finding()
        added_path = (
            self.codex_home
            / "sessions"
            / "2026"
            / "07"
            / "31"
            / "startup-copy.jsonl"
        )

        def add_copy_on_enter() -> None:
            added_path.write_text(
                rollout_paths[0].read_text(encoding="utf-8"),
                encoding="utf-8",
            )

        server = FakeAppServer(enter_callback=add_copy_on_enter)
        report = self.clean(
            [finding],
            server=server,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    rollout_paths=tuple(str(path) for path in rollout_paths),
                )
            },
            approved_integrity_deletes={
                finding_key(finding): {"duplicate_rollout"}
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("rollout_paths added", report.results[0].error or "")

    def test_same_path_metadata_identity_drift_blocks_all_roots(
        self,
    ) -> None:
        finding = self.finding("identity-root")
        normal_finding = self.finding("normal-alongside-identity-drift")
        assert finding.rollout is not None
        assert normal_finding.rollout is not None
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": finding.thread_id,
                    "rollout_path": str(finding.rollout.path),
                }
            ],
        )

        def change_identity_on_enter() -> None:
            lines = finding.rollout.path.read_text(
                encoding="utf-8"
            ).splitlines()
            first_record = json.loads(lines[0])
            first_record["payload"]["id"] = "unrelated-thread"
            finding.rollout.path.write_text(
                "\n".join(
                    [json.dumps(first_record), *lines[1:]]
                )
                + "\n",
                encoding="utf-8",
            )

        server = FakeAppServer(enter_callback=change_identity_on_enter)
        report = self.clean(
            [finding, normal_finding],
            server=server,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    indexed_thread_ids=(finding.thread_id,),
                    rollout_paths=(str(finding.rollout.path),),
                ),
                finding_key(normal_finding): ExpectedDeletionScope(
                    rollout_paths=(str(normal_finding.rollout.path),),
                ),
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(result.status == "unknown" for result in report.results)
        )
        self.assertTrue(
            all(
                "metadata does not belong" in (result.error or "")
                for result in report.results
            )
        )

    def test_approved_duplicate_delete_with_one_copy_remaining_is_partial(
        self,
    ) -> None:
        finding, rollout_paths = self.duplicate_integrity_finding()

        report = self.clean(
            [finding],
            server=FakeAppServer(
                lambda _thread_id: rollout_paths[0].unlink()
            ),
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    rollout_paths=tuple(str(path) for path in rollout_paths),
                )
            },
            approved_integrity_deletes={
                finding_key(finding): {"duplicate_rollout"}
            },
        )

        result = report.results[0]
        self.assertEqual(result.status, "partial")
        self.assertNotEqual(result.status, "deleted")
        self.assertIn(str(rollout_paths[1]), result.remaining_artifacts)
        self.assertIsNone(result.request_error)

    def test_startup_added_rollout_and_index_block_all_roots(self) -> None:
        changed_root = self.finding("changed-root")
        normal_root = self.finding("normal-root")
        assert changed_root.rollout is not None
        assert normal_root.rollout is not None

        def add_native_state_on_enter() -> None:
            startup_path = write_rollout(
                self.codex_home,
                changed_root.thread_id,
                originator="test",
                archived=True,
            )
            create_thread_index(
                self.codex_home,
                [
                    {
                        "id": changed_root.thread_id,
                        "rollout_path": str(startup_path),
                        "archived": 1,
                    }
                ],
            )

        server = FakeAppServer(enter_callback=add_native_state_on_enter)
        expected_scopes = {
            finding_key(changed_root): ExpectedDeletionScope(
                rollout_paths=(str(changed_root.rollout.path),),
            ),
            finding_key(normal_root): ExpectedDeletionScope(
                rollout_paths=(str(normal_root.rollout.path),),
            ),
        }

        report = self.clean(
            [changed_root, normal_root],
            server=server,
            expected_scopes=expected_scopes,
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(len(report.results), 2)
        self.assertTrue(
            all(result.status == "unknown" for result in report.results)
        )
        self.assertTrue(
            all(
                "no deletion request was sent" in (result.error or "")
                for result in report.results
            )
        )
        self.assertTrue(
            all(
                "multiple current rollout files" in (result.error or "")
                for result in report.results
            )
        )

    def test_reviewed_scope_overrides_only_legacy_cascade_blocker(self) -> None:
        root_finding, _child_path = self._indexed_parent_and_child()
        root_finding.details.update(
            {
                "cleanable": False,
                "cascade_safe": False,
                "has_unreviewed_descendants": True,
                "cascade_descendant_count": 1,
                "cleanup_blocked_reason": (
                    "thread/delete would cascade into spawned descendants."
                ),
            }
        )
        server = FakeAppServer()

        report = self.clean(
            [root_finding],
            server=server,
            approved_descendants={finding_key(root_finding): {"child"}},
        )

        self.assertEqual(server.deleted_thread_ids, ["root"])
        self.assertEqual(report.results[0].status, "not_deleted")

    def test_exact_integrity_scope_sanitizes_pure_cascade_flags_first(
        self,
    ) -> None:
        root_finding, root_paths = self.duplicate_integrity_finding("root")
        child_path = write_rollout(
            self.codex_home,
            "child",
            originator="test",
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": "root",
                    "rollout_path": str(root_paths[0]),
                },
                {
                    "id": "child",
                    "rollout_path": str(child_path),
                },
            ],
            spawn_edges=[
                {
                    "parent_thread_id": "root",
                    "child_thread_id": "child",
                }
            ],
        )
        root_finding.details.update(
            {
                "cascade_safe": False,
                "has_unreviewed_descendants": True,
                "cascade_descendant_count": 1,
            }
        )
        server = FakeAppServer()

        report = self.clean(
            [root_finding],
            server=server,
            verifier=lambda _finding: VerificationResult(deleted=True),
            expected_scopes={
                finding_key(root_finding): ExpectedDeletionScope(
                    descendant_thread_ids=("child",),
                    indexed_thread_ids=("child", "root"),
                    rollout_paths=tuple(
                        str(path)
                        for path in (*root_paths, child_path)
                    ),
                )
            },
            approved_integrity_deletes={
                finding_key(root_finding): {"duplicate_rollout"}
            },
        )

        self.assertEqual(server.deleted_thread_ids, ["root"])
        self.assertEqual(report.results[0].status, "deleted")

    def test_startup_descendant_duplicate_blocks_even_if_expected(
        self,
    ) -> None:
        root_finding, child_path = self._indexed_parent_and_child()
        assert root_finding.rollout is not None
        duplicate_child_path = (
            self.codex_home
            / "archived_sessions"
            / "2026"
            / "07"
            / "31"
            / "rollout-child.jsonl"
        )

        def duplicate_child_on_enter() -> None:
            write_rollout(
                self.codex_home,
                "child",
                originator="test",
                archived=True,
            )

        server = FakeAppServer(enter_callback=duplicate_child_on_enter)
        report = self.clean(
            [root_finding],
            server=server,
            expected_scopes={
                finding_key(root_finding): ExpectedDeletionScope(
                    descendant_thread_ids=("child",),
                    indexed_thread_ids=("child", "root"),
                    rollout_paths=(
                        str(root_finding.rollout.path),
                        str(child_path),
                        str(duplicate_child_path),
                    ),
                )
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "child has multiple current rollout files",
            report.results[0].error or "",
        )

    def test_startup_descendant_same_path_identity_drift_blocks(
        self,
    ) -> None:
        root_finding, child_path = self._indexed_parent_and_child()
        assert root_finding.rollout is not None

        def change_child_identity_on_enter() -> None:
            lines = child_path.read_text(encoding="utf-8").splitlines()
            first_record = json.loads(lines[0])
            first_record["payload"]["id"] = "other-child"
            child_path.write_text(
                "\n".join([json.dumps(first_record), *lines[1:]]) + "\n",
                encoding="utf-8",
            )

        server = FakeAppServer(enter_callback=change_child_identity_on_enter)
        report = self.clean(
            [root_finding],
            server=server,
            expected_scopes={
                finding_key(root_finding): ExpectedDeletionScope(
                    descendant_thread_ids=("child",),
                    indexed_thread_ids=("child", "root"),
                    rollout_paths=(
                        str(root_finding.rollout.path),
                        str(child_path),
                    ),
                )
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "metadata does not belong",
            report.results[0].error or "",
        )

    def test_descendant_index_cannot_borrow_other_affected_identity(
        self,
    ) -> None:
        root_finding, child_path = self._indexed_parent_and_child()
        assert root_finding.rollout is not None

        def retarget_child_index_on_enter() -> None:
            child_path.unlink()
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    "UPDATE threads SET rollout_path = ? WHERE id = 'child'",
                    (str(root_finding.rollout.path),),
                )
                connection.commit()

        server = FakeAppServer(
            enter_callback=retarget_child_index_on_enter
        )
        report = self.clean(
            [root_finding],
            server=server,
            expected_scopes={
                finding_key(root_finding): ExpectedDeletionScope(
                    descendant_thread_ids=("child",),
                    indexed_thread_ids=("child", "root"),
                    rollout_paths=(str(root_finding.rollout.path),),
                )
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "child existing indexed rollout metadata does not confirm",
            report.results[0].error or "",
        )

    def test_startup_descendant_external_source_parent_blocks(
        self,
    ) -> None:
        root_finding, child_path = self._indexed_parent_and_child()
        assert root_finding.rollout is not None

        def change_child_source_on_enter() -> None:
            lines = child_path.read_text(encoding="utf-8").splitlines()
            first_record = json.loads(lines[0])
            first_record["payload"]["source"] = {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": "external-parent",
                    }
                }
            }
            child_path.write_text(
                "\n".join([json.dumps(first_record), *lines[1:]]) + "\n",
                encoding="utf-8",
            )

        server = FakeAppServer(enter_callback=change_child_source_on_enter)
        report = self.clean(
            [root_finding],
            server=server,
            expected_scopes={
                finding_key(root_finding): ExpectedDeletionScope(
                    descendant_thread_ids=("child",),
                    indexed_thread_ids=("child", "root"),
                    rollout_paths=(
                        str(root_finding.rollout.path),
                        str(child_path),
                    ),
                )
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "outside the approved affected scope",
            report.results[0].error or "",
        )

    def test_reviewed_scope_does_not_override_live_reference_blocker(
        self,
    ) -> None:
        root_finding, _child_path = self._indexed_parent_and_child()
        root_finding.details.update(
            {
                "cleanable": False,
                "cascade_safe": False,
                "cascade_descendant_count": 1,
                "cleanup_blocked_reason": (
                    "thread/delete would cascade into a descendant that is "
                    "still live in a frontend."
                ),
            }
        )
        server = FakeAppServer()

        report = self.clean(
            [root_finding],
            server=server,
            approved_descendants={finding_key(root_finding): {"child"}},
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("still live", report.results[0].error or "")

    def test_verification_covers_root_and_approved_descendant_as_partial(
        self,
    ) -> None:
        root_finding, child_path = self._indexed_parent_and_child()

        def delete_root_only(_thread_id: str) -> None:
            assert root_finding.rollout is not None
            root_finding.rollout.path.unlink()
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute("DELETE FROM threads WHERE id = 'root'")
                connection.commit()

        report = self.clean(
            [root_finding],
            server=FakeAppServer(delete_root_only),
            approved_descendants={finding_key(root_finding): {"child"}},
        )

        result = report.results[0]
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.impacted_thread_ids, ("child", "root"))
        self.assertTrue(
            any(str(child_path) == artifact for artifact in result.remaining_artifacts)
        )

    def test_request_error_after_cascade_can_verify_full_scope_deleted(
        self,
    ) -> None:
        root_finding, child_path = self._indexed_parent_and_child()

        def delete_then_timeout(_thread_id: str) -> None:
            assert root_finding.rollout is not None
            root_finding.rollout.path.unlink()
            child_path.unlink()
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    "DELETE FROM threads WHERE id IN ('root', 'child')"
                )
                connection.execute("DELETE FROM thread_spawn_edges")
                connection.commit()
            raise TimeoutError("response timed out after deletion")

        report = self.clean(
            [root_finding],
            server=FakeAppServer(delete_then_timeout),
            approved_descendants={finding_key(root_finding): {"child"}},
        )

        result = report.results[0]
        self.assertEqual(result.status, "deleted")
        self.assertIn("timed out", result.request_error or "")
        self.assertEqual(result.impacted_thread_ids, ("child", "root"))

    def test_dangling_incoming_spawn_edge_makes_result_partial(self) -> None:
        child_finding = self.finding("child")
        assert child_finding.rollout is not None
        create_thread_index(
            self.codex_home,
            [
                {"id": "parent"},
                {
                    "id": "child",
                    "rollout_path": str(child_finding.rollout.path),
                },
            ],
            spawn_edges=[
                {
                    "parent_thread_id": "parent",
                    "child_thread_id": "child",
                }
            ],
        )

        def delete_child_but_leave_edge(_thread_id: str) -> None:
            assert child_finding.rollout is not None
            child_finding.rollout.path.unlink()
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute("DELETE FROM threads WHERE id = 'child'")
                connection.commit()

        report = self.clean(
            [child_finding],
            server=FakeAppServer(delete_child_but_leave_edge),
            approved_descendants={finding_key(child_finding): set()},
        )

        result = report.results[0]
        self.assertEqual(result.status, "partial")
        self.assertIn(
            "spawn-edge:parent->child",
            result.remaining_artifacts,
        )

    def test_partial_result_when_index_is_deleted_but_rollout_remains(
        self,
    ) -> None:
        finding = self.finding("partly-removed-root")
        assert finding.rollout is not None
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": finding.thread_id,
                    "rollout_path": str(finding.rollout.path),
                }
            ],
        )

        def delete_index_only(_thread_id: str) -> None:
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    "DELETE FROM threads WHERE id = ?",
                    (finding.thread_id,),
                )
                connection.commit()

        report = self.clean(
            [finding],
            server=FakeAppServer(delete_index_only),
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    indexed_thread_ids=(finding.thread_id,),
                    rollout_paths=(str(finding.rollout.path),),
                )
            },
        )

        self.assertEqual(report.results[0].status, "partial")
        self.assertIn(
            str(finding.rollout.path),
            report.results[0].remaining_artifacts,
        )

    def test_indexed_rollout_path_with_broken_metadata_is_still_verified(
        self,
    ) -> None:
        broken_path = (
            self.codex_home
            / "sessions"
            / "2026"
            / "07"
            / "31"
            / "broken-metadata.jsonl"
        )
        broken_path.parent.mkdir(parents=True)
        broken_path.write_text("{not-json}\n", encoding="utf-8")
        finding = Finding(
            platform="test",
            platform_session_id="frontend-broken-metadata",
            thread_id="broken-metadata",
            reason="indexed content metadata is damaged",
            platform_db=self.root / "frontend.sqlite",
            codex_home=self.codex_home,
            codex_indexed=True,
            details={
                "cleanable": True,
                "thread_delete_supported": True,
            },
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": finding.thread_id,
                    "rollout_path": str(broken_path),
                }
            ],
        )

        def delete_index_only(_thread_id: str) -> None:
            with closing(
                sqlite3.connect(self.codex_home / "state_5.sqlite")
            ) as connection:
                connection.execute(
                    "DELETE FROM threads WHERE id = ?",
                    (finding.thread_id,),
                )
                connection.commit()

        report = self.clean(
            [finding],
            server=FakeAppServer(delete_index_only),
            approved_descendants={
                finding_key(finding): set(),
            },
        )

        self.assertEqual(report.results[0].status, "partial")
        self.assertIn(
            str(broken_path),
            report.results[0].remaining_artifacts,
        )

    def test_exact_scope_blocks_indexed_rollout_with_broken_metadata(
        self,
    ) -> None:
        broken_path = (
            self.codex_home
            / "sessions"
            / "2026"
            / "07"
            / "31"
            / "broken-exact-metadata.jsonl"
        )
        broken_path.parent.mkdir(parents=True)
        broken_path.write_text("{not-json}\n", encoding="utf-8")
        finding = Finding(
            platform="test",
            platform_session_id="frontend-broken-exact-metadata",
            thread_id="broken-exact-metadata",
            reason="indexed content metadata is damaged",
            platform_db=self.root / "frontend.sqlite",
            codex_home=self.codex_home,
            codex_indexed=True,
            details={
                "cleanable": True,
                "thread_delete_supported": True,
            },
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": finding.thread_id,
                    "rollout_path": str(broken_path),
                }
            ],
        )
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            expected_scopes={
                finding_key(finding): ExpectedDeletionScope(
                    indexed_thread_ids=(finding.thread_id,),
                    rollout_paths=(str(broken_path),),
                )
            },
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn(
            "metadata does not belong",
            report.results[0].error or "",
        )

    def test_unreadable_index_schema_makes_verification_unknown_before_delete(
        self,
    ) -> None:
        finding = self.finding("missing-threads-table")
        with closing(
            sqlite3.connect(self.codex_home / "state_5.sqlite")
        ) as connection:
            connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
            connection.commit()
        server = FakeAppServer()

        report = self.clean(
            [finding],
            server=server,
            approved_descendants={finding_key(finding): set()},
        )

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("required table 'threads'", report.results[0].error or "")

    def test_incompatible_spawn_schema_blocks_without_delete_request(
        self,
    ) -> None:
        finding = self.finding("malformed-schema")
        with closing(
            sqlite3.connect(self.codex_home / "state_5.sqlite")
        ) as connection:
            connection.execute(
                """
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT NOT NULL
                )
                """
            )
            connection.commit()
        server = FakeAppServer()

        report = self.clean([finding], server=server)

        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(report.results[0].status, "unknown")
        self.assertIn("child_thread_id", report.results[0].error or "")

    def _indexed_parent_and_child(self) -> tuple[Finding, Path]:
        root_finding = self.finding("root")
        child_path = write_rollout(
            self.codex_home,
            "child",
            originator="test",
        )
        assert root_finding.rollout is not None
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": "root",
                    "rollout_path": str(root_finding.rollout.path),
                },
                {"id": "child", "rollout_path": str(child_path)},
            ],
            spawn_edges=[
                {
                    "parent_thread_id": "root",
                    "child_thread_id": "child",
                }
            ],
        )
        return root_finding, child_path


class ScanFailureTests(unittest.TestCase):
    def test_adapter_failure_retains_affected_codex_home(self) -> None:
        class BrokenAdapter:
            name = "broken"
            codex_home = Path("affected-codex-home")

            def scan(self):
                raise RuntimeError("database unreadable")

        report = scan_adapters([BrokenAdapter()])  # type: ignore[list-item]

        self.assertEqual(report.errors[0].codex_home, Path("affected-codex-home"))
        self.assertEqual(
            report.errors[0].to_dict()["codex_home"],
            "affected-codex-home",
        )


if __name__ == "__main__":
    unittest.main()
