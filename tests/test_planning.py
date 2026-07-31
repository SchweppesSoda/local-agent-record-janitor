from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_session_janitor.cleaner import ExpectedDeletionScope
from codex_session_janitor.codex_state import rollout_state_fingerprint
from codex_session_janitor.models import Finding, RolloutRecord
from codex_session_janitor.planning import (
    ActionKind,
    RiskLevel,
    ScanStatus,
    build_cleanup_plan,
    normalize_storage_path,
)


def _finding(
    home: Path,
    thread_id: str,
    finding_type: str,
    *,
    indexed: bool = True,
    rollout: RolloutRecord | None = None,
    details: dict | None = None,
    platform: str = "native",
    codex_bin_hint: Path | None = None,
) -> Finding:
    finding_details = {
        "finding_type": finding_type,
        "thread_delete_supported": True,
        "cleanable": True,
    }
    finding_details.update(details or {})
    return Finding(
        platform=platform,
        platform_session_id=f"{platform}-{thread_id}",
        thread_id=thread_id,
        reason=f"reason for {finding_type}",
        platform_db=home / "state_5.sqlite",
        codex_home=home,
        rollout=rollout,
        codex_indexed=indexed,
        codex_bin_hint=codex_bin_hint,
        details=finding_details,
    )


def _record(
    home: Path,
    thread_id: str,
    name: str | None = None,
    *,
    originator: str | None = "codex_cli_rs",
    source=None,
) -> RolloutRecord:
    return RolloutRecord(
        thread_id=thread_id,
        path=home / "sessions" / (name or f"{thread_id}.jsonl"),
        originator=originator,
        source=source,
        cwd=None,
        timestamp=None,
        archived=False,
    )


def _readers(
    *,
    descendants: dict[str, set[str]] | None = None,
    indexed: set[str] | None = None,
    rollouts: dict[str, list[RolloutRecord]] | None = None,
) -> dict:
    descendants = descendants or {}
    indexed = indexed or set()
    rollouts = rollouts or {}
    return {
        "descendant_reader": (
            lambda _home, ids, strict=True: {
                thread_id: set(descendants.get(thread_id, set()))
                for thread_id in ids
            }
        ),
        "index_reader": (
            lambda _home, ids, strict=True: {
                thread_id: {"id": thread_id}
                for thread_id in ids
                if thread_id in indexed
            }
        ),
        "rollout_reader": (
            lambda _home, thread_id: list(rollouts.get(thread_id, ()))
        ),
    }


class CleanupPlanningTests(unittest.TestCase):
    def test_rollout_fingerprints_are_stable_and_track_file_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            record = _record(home, "fingerprinted")
            record.path.parent.mkdir(parents=True)
            record.path.write_text("initial body\n", encoding="utf-8")
            finding = _finding(home, "fingerprinted", "index_missing_rollout")
            readers = _readers(
                indexed={"fingerprinted"},
                rollouts={"fingerprinted": [record]},
            )

            initial_state_fingerprint = rollout_state_fingerprint(record)
            first = build_cleanup_plan([finding], **readers)
            repeated = build_cleanup_plan([finding], **readers)
            record.path.write_text(
                "changed body with a different size\n",
                encoding="utf-8",
            )
            changed_state_fingerprint = rollout_state_fingerprint(record)
            changed = build_cleanup_plan([finding], **readers)

        first_action = next(
            action
            for action in first.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        repeated_action = next(
            action
            for action in repeated.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        changed_action = next(
            action
            for action in changed.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(
            first_action.impact.rollout_state_fingerprints,
            repeated_action.impact.rollout_state_fingerprints,
        )
        self.assertEqual(
            first_action.snapshot_fingerprint,
            repeated_action.snapshot_fingerprint,
        )
        self.assertEqual(
            len(first_action.impact.rollout_state_fingerprints),
            1,
        )
        self.assertEqual(
            first_action.impact.rollout_state_fingerprints,
            (initial_state_fingerprint,),
        )
        self.assertEqual(
            changed_action.impact.rollout_state_fingerprints,
            (changed_state_fingerprint,),
        )
        self.assertTrue(
            first_action.impact.rollout_state_fingerprints[0].startswith(
                "v1:"
            )
        )
        self.assertNotEqual(
            first_action.impact.rollout_state_fingerprints,
            changed_action.impact.rollout_state_fingerprints,
        )
        self.assertNotEqual(
            first_action.snapshot_fingerprint,
            changed_action.snapshot_fingerprint,
        )
        self.assertEqual(
            first_action.impact.to_dict()["rollout_state_fingerprints"],
            list(first_action.impact.rollout_state_fingerprints),
        )

    def test_rollout_fingerprint_failure_blocks_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            record = _record(home, "fingerprint-error")
            record.path.parent.mkdir(parents=True)
            record.path.write_text("current body\n", encoding="utf-8")
            finding = _finding(
                home,
                "fingerprint-error",
                "index_missing_rollout",
            )
            with patch(
                "codex_session_janitor.planning.rollout_state_fingerprint",
                side_effect=OSError("stat access denied"),
            ):
                plan = build_cleanup_plan(
                    [finding],
                    **_readers(
                        indexed={"fingerprint-error"},
                        rollouts={"fingerprint-error": [record]},
                    ),
                )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertFalse(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)
        self.assertEqual(
            delete_action.impact.rollout_state_fingerprints,
            (),
        )
        self.assertIn(
            "stat access denied",
            delete_action.impact.fingerprint_error or "",
        )
        self.assertIn(
            "could not be fingerprinted",
            delete_action.unavailable_reason or "",
        )

    def test_verified_duplicate_delete_is_available_high_and_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            records = [
                _record(home, "duplicate", "one.jsonl"),
                _record(home, "duplicate", "two.jsonl"),
            ]
            for record in records:
                record.path.parent.mkdir(parents=True, exist_ok=True)
                record.path.write_text("parsed by injected reader\n", encoding="utf-8")
            finding = _finding(
                home,
                "duplicate",
                "duplicate_rollout",
                indexed=False,
                rollout=records[0],
                details={
                    "rollout_paths": [str(record.path) for record in records],
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "Official deletion has not been verified to remove "
                        "every duplicate rollout; preserve all copies for "
                        "manual review."
                    ),
                },
            )

            plan = build_cleanup_plan(
                [finding],
                **_readers(rollouts={"duplicate": records}),
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        quarantine_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.QUARANTINE_ARTIFACTS
        )
        self.assertTrue(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.HIGH)
        self.assertTrue(delete_action.requires_explicit_selection)
        self.assertEqual(delete_action.impact.rollout_file_count, 2)
        self.assertFalse(quarantine_action.available)

    def test_verified_path_mismatch_delete_is_available_high_and_explicit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            actual = _record(home, "path-mismatch", "actual.jsonl")
            actual.path.parent.mkdir(parents=True)
            actual.path.write_text("parsed by injected reader\n", encoding="utf-8")
            finding = _finding(
                home,
                "path-mismatch",
                "index_rollout_path_mismatch",
                rollout=actual,
                details={
                    "indexed_rollout_path": str(
                        home / "sessions" / "missing.jsonl"
                    ),
                    "actual_rollout_paths": [str(actual.path)],
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "The alternate rollout may be recoverable; deletion "
                        "is not a safe path repair."
                    ),
                },
            )
            plan = build_cleanup_plan(
                [finding],
                descendant_reader=(
                    lambda _home, ids, strict=True: {
                        thread_id: set() for thread_id in ids
                    }
                ),
                index_reader=(
                    lambda _home, _ids, strict=True: {
                        finding.thread_id: {
                            "id": finding.thread_id,
                            "rollout_path": "sessions/missing.jsonl",
                        }
                    }
                ),
                rollout_reader=lambda _home, _thread_id: [actual],
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        repair_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.REPAIR_INDEX_PATH
        )
        self.assertTrue(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.HIGH)
        self.assertTrue(delete_action.requires_explicit_selection)
        self.assertFalse(repair_action.available)

    def test_integrity_soft_reason_with_extra_tail_remains_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            record = _record(home, "extra-tail")
            record.path.parent.mkdir(parents=True)
            record.path.write_text("current content\n", encoding="utf-8")
            finding = _finding(
                home,
                "extra-tail",
                "duplicate_rollout",
                indexed=False,
                rollout=record,
                details={
                    "rollout_paths": [str(record.path)],
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "Official deletion has not been verified to remove "
                        "every duplicate rollout; preserve all copies for "
                        "manual review. Additional manual approval is needed."
                    ),
                },
            )
            plan = build_cleanup_plan(
                [finding],
                **_readers(rollouts={"extra-tail": [record]}),
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertFalse(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)
        self.assertIn(
            "Additional manual approval",
            delete_action.unavailable_reason or "",
        )

    def test_integrity_delete_blocks_unverified_or_conflicting_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            verified = _record(home, "target", "verified.jsonl")
            unverified = _record(home, "target", "unverified.jsonl")
            indexed_unknown = home / "sessions" / "indexed-unknown.jsonl"
            for path in (verified.path, unverified.path, indexed_unknown):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("current content\n", encoding="utf-8")

            duplicate = _finding(
                home,
                "target",
                "duplicate_rollout",
                indexed=False,
                rollout=verified,
                details={
                    "rollout_paths": [
                        str(verified.path),
                        str(unverified.path),
                    ],
                    "thread_delete_supported": False,
                    "cleanable": False,
                },
            )
            unverified_plan = build_cleanup_plan(
                [duplicate],
                **_readers(rollouts={"target": [verified]}),
            )

            path_mismatch = _finding(
                home,
                "target",
                "index_rollout_path_mismatch",
                rollout=verified,
                details={
                    "actual_rollout_paths": [str(verified.path)],
                    "thread_delete_supported": False,
                    "cleanable": False,
                },
            )
            indexed_unknown_plan = build_cleanup_plan(
                [path_mismatch],
                descendant_reader=(
                    lambda _home, ids, strict=True: {
                        thread_id: set() for thread_id in ids
                    }
                ),
                index_reader=(
                    lambda _home, _ids, strict=True: {
                        "target": {
                            "id": "target",
                            "rollout_path": str(indexed_unknown),
                        }
                    }
                ),
                rollout_reader=lambda _home, _thread_id: [verified],
            )

            metadata_conflict = _finding(
                home,
                "target",
                "index_rollout_metadata_mismatch",
                rollout=verified,
                details={
                    "metadata_thread_id": "different-thread",
                    "thread_delete_supported": False,
                    "cleanable": False,
                },
            )
            metadata_conflict_plan = build_cleanup_plan(
                [duplicate, metadata_conflict],
                **_readers(
                    rollouts={"target": [verified, unverified]},
                ),
            )

        for plan in (
            unverified_plan,
            indexed_unknown_plan,
            metadata_conflict_plan,
        ):
            delete_action = next(
                action
                for action in plan.actions
                if action.kind is ActionKind.DELETE_CONVERSATION
            )
            self.assertFalse(delete_action.available)
            self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)

    def test_integrity_delete_blocks_active_reference(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            record = _record(home, "active-duplicate")
            record.path.parent.mkdir(parents=True)
            record.path.write_text("current content\n", encoding="utf-8")
            finding = _finding(
                home,
                "active-duplicate",
                "duplicate_rollout",
                indexed=False,
                rollout=record,
                details={
                    "rollout_paths": [str(record.path)],
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "live_reference_count": 1,
                },
            )
            plan = build_cleanup_plan(
                [finding],
                **_readers(rollouts={"active-duplicate": [record]}),
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertFalse(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)

    def test_integrity_delete_blocks_conflicting_rollout_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            records = [
                _record(
                    home,
                    "conflicting",
                    "one.jsonl",
                    originator="codex_cli_rs",
                    source=None,
                ),
                _record(
                    home,
                    "conflicting",
                    "two.jsonl",
                    originator="codex_exec",
                    source={"subagent": {"thread_id": "different-parent"}},
                ),
            ]
            for record in records:
                record.path.parent.mkdir(parents=True, exist_ok=True)
                record.path.write_text("current content\n", encoding="utf-8")
            finding = _finding(
                home,
                "conflicting",
                "duplicate_rollout",
                indexed=False,
                rollout=records[0],
                details={
                    "rollout_paths": [str(record.path) for record in records],
                    "thread_delete_supported": False,
                    "cleanable": False,
                },
            )
            plan = build_cleanup_plan(
                [finding],
                **_readers(rollouts={"conflicting": records}),
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertFalse(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)
        self.assertIn("conflicting", delete_action.unavailable_reason or "")

    def test_descendant_metadata_mismatch_blocks_parent_delete_and_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            parent = _finding(home, "parent", "index_missing_rollout")
            child = _finding(
                home,
                "child",
                "index_rollout_metadata_mismatch",
                details={
                    "metadata_thread_id": "unrelated",
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "The indexed file belongs to another thread."
                    ),
                },
            )
            common = {
                "descendants": {"parent": {"child"}},
                "indexed": {"parent", "child"},
            }
            baseline = build_cleanup_plan(
                [parent],
                **_readers(**common),
            )
            plan = build_cleanup_plan(
                [parent, child],
                **_readers(**common),
            )

        baseline_parent_delete = next(
            action
            for action in baseline.actions
            if action.target.thread_id == "parent"
            and action.kind is ActionKind.DELETE_CONVERSATION
        )
        parent_delete = next(
            action
            for action in plan.actions
            if action.target.thread_id == "parent"
            and action.kind is ActionKind.DELETE_CONVERSATION
        )
        child_quarantine = next(
            action
            for action in plan.actions
            if action.target.thread_id == "child"
            and action.kind is ActionKind.QUARANTINE_ARTIFACTS
        )
        child_observation = next(
            observation
            for observation in plan.observations
            if observation.target.thread_id == "child"
        )
        self.assertFalse(parent_delete.available)
        self.assertEqual(parent_delete.risk, RiskLevel.BLOCKED)
        self.assertIn(child_observation.observation_id, parent_delete.observation_ids)
        self.assertEqual(
            parent_delete.impact.affected_thread_ids,
            ("parent", "child"),
        )
        self.assertNotEqual(
            parent_delete.snapshot_fingerprint,
            baseline_parent_delete.snapshot_fingerprint,
        )
        self.assertFalse(child_quarantine.available)
        self.assertEqual(child_quarantine.risk, RiskLevel.HIGH)

    def test_descendant_integrity_anomaly_requires_independent_approval(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            parent = _finding(home, "parent", "index_missing_rollout")
            records = [
                _record(home, "child", "child-one.jsonl"),
                _record(home, "child", "child-two.jsonl"),
            ]
            for record in records:
                record.path.parent.mkdir(parents=True, exist_ok=True)
                record.path.write_text("current content\n", encoding="utf-8")
            child = _finding(
                home,
                "child",
                "duplicate_rollout",
                indexed=False,
                rollout=records[0],
                details={
                    "rollout_paths": [str(record.path) for record in records],
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "Official deletion has not been verified to remove "
                        "every duplicate rollout; preserve all copies for "
                        "manual review."
                    ),
                },
            )
            plan = build_cleanup_plan(
                [parent, child],
                **_readers(
                    descendants={"parent": {"child"}},
                    indexed={"parent"},
                    rollouts={"child": records},
                ),
            )

        parent_delete = next(
            action
            for action in plan.actions
            if action.target.thread_id == "parent"
            and action.kind is ActionKind.DELETE_CONVERSATION
        )
        child_delete = next(
            action
            for action in plan.actions
            if action.target.thread_id == "child"
            and action.kind is ActionKind.DELETE_CONVERSATION
        )
        child_observation = next(
            observation
            for observation in plan.observations
            if observation.target.thread_id == "child"
        )
        self.assertFalse(parent_delete.available)
        self.assertEqual(parent_delete.risk, RiskLevel.BLOCKED)
        self.assertIn("independently", parent_delete.unavailable_reason or "")
        self.assertIn(child_observation.observation_id, parent_delete.observation_ids)
        self.assertTrue(child_delete.available)
        self.assertEqual(child_delete.risk, RiskLevel.HIGH)
        self.assertTrue(child_delete.requires_explicit_selection)

    def test_stale_adapter_rollout_is_observation_not_current_impact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            stale = _record(home, "stale-adapter")
            finding = _finding(
                home,
                "stale-adapter",
                "frontend_deleted_reference",
                rollout=stale,
                platform="aionui",
            )
            plan = build_cleanup_plan(
                [finding],
                **_readers(indexed={"stale-adapter"}),
            )

        observation = plan.observations[0]
        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        expected_scope = ExpectedDeletionScope(
            descendant_thread_ids=delete_action.impact.descendant_thread_ids,
            indexed_thread_ids=delete_action.impact.indexed_thread_ids,
            rollout_paths=delete_action.impact.rollout_paths,
            rollout_state_fingerprints=(
                delete_action.impact.rollout_state_fingerprints
            ),
        )
        self.assertEqual(
            observation.rollout_paths,
            (normalize_storage_path(stale.path),),
        )
        self.assertEqual(delete_action.impact.rollout_paths, ())
        self.assertEqual(delete_action.impact.rollout_file_count, 0)
        self.assertEqual(
            delete_action.impact.rollout_state_fingerprints,
            (),
        )
        self.assertEqual(expected_scope.rollout_paths, ())
        self.assertEqual(expected_scope.rollout_state_fingerprints, ())
        self.assertTrue(delete_action.available)

    def test_current_nonindexed_unverified_adapter_path_is_blocked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            reported = _record(home, "reported-current")
            reported.path.parent.mkdir(parents=True)
            reported.path.write_text(
                "{current metadata is no longer parseable}\n",
                encoding="utf-8",
            )
            finding = _finding(
                home,
                "reported-current",
                "frontend_deleted_reference",
                indexed=False,
                rollout=reported,
                platform="aionui",
                details={
                    "ownership_status": "confirmed",
                    "cascade_check_available": True,
                    "cleanable": True,
                    "thread_delete_supported": True,
                },
            )
            plan = build_cleanup_plan(
                [finding],
                **_readers(),
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(
            delete_action.impact.rollout_paths,
            (normalize_storage_path(reported.path),),
        )
        self.assertFalse(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)
        self.assertIn(
            "metadata did not confirm conversation reported-current",
            delete_action.unavailable_reason or "",
        )

    def test_known_frontend_descendant_is_available_but_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            finding = _finding(
                home,
                "frontend-root",
                "frontend_deleted_reference",
                platform="aionui",
                details={
                    "ownership_status": "confirmed",
                    "cascade_check_available": True,
                    "cascade_safe": False,
                    "has_unreviewed_descendants": True,
                    "cascade_descendant_count": 1,
                    "cleanable": False,
                    "thread_delete_supported": True,
                    "cleanup_blocked_reason": (
                        "Codex thread/delete would cascade into known "
                        "descendant threads."
                    ),
                },
            )
            plan = build_cleanup_plan(
                [finding],
                **_readers(
                    descendants={"frontend-root": {"frontend-child"}},
                    indexed={"frontend-root", "frontend-child"},
                ),
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertTrue(delete_action.available)
        self.assertIsNone(delete_action.unavailable_reason)
        self.assertEqual(
            delete_action.impact.descendant_thread_ids,
            ("frontend-child",),
        )
        self.assertEqual(
            delete_action.impact.indexed_thread_ids,
            ("frontend-child", "frontend-root"),
        )
        self.assertTrue(delete_action.requires_explicit_selection)
        self.assertEqual(delete_action.risk, RiskLevel.REVIEW)

    def test_unavailable_frontend_descendant_graph_remains_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            finding = _finding(
                home,
                "frontend-root",
                "frontend_deleted_reference",
                platform="aionui",
                details={
                    "ownership_status": "confirmed",
                    "cascade_check_available": False,
                    "cascade_safe": False,
                    "has_unreviewed_descendants": True,
                    "cleanable": False,
                    "thread_delete_supported": True,
                    "cleanup_blocked_reason": (
                        "Cascade safety could not be verified because the "
                        "conversation graph is unavailable."
                    ),
                },
            )
            plan = build_cleanup_plan(
                [finding],
                **_readers(indexed={"frontend-root"}),
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertFalse(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)
        self.assertIn("could not be verified", delete_action.unavailable_reason or "")

    def test_filtered_native_descendant_anomalies_block_frontend_parent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            finding = _finding(
                home,
                "frontend-root",
                "frontend_deleted_reference",
                platform="aionui",
                details={
                    "ownership_status": "confirmed",
                    "cascade_check_available": True,
                    "cascade_safe": False,
                    "has_unreviewed_descendants": True,
                    "cascade_descendant_count": 1,
                    "cleanable": False,
                    "thread_delete_supported": True,
                    "cleanup_blocked_reason": (
                        "Codex thread/delete would cascade into known "
                        "descendant threads."
                    ),
                },
            )
            duplicate_records = [
                _record(home, "frontend-child", "duplicate-one.jsonl"),
                _record(home, "frontend-child", "duplicate-two.jsonl"),
            ]
            mismatched_record = _record(
                home,
                "other-thread",
                "indexed-other.jsonl",
            )
            source_conflict_record = _record(
                home,
                "frontend-child",
                "source-conflict.jsonl",
                source={
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "outside-approved-scope"
                        }
                    }
                },
            )
            for record in (
                *duplicate_records,
                mismatched_record,
                source_conflict_record,
            ):
                record.path.parent.mkdir(parents=True, exist_ok=True)
                record.path.write_text("current content\n", encoding="utf-8")

            duplicate_plan = build_cleanup_plan(
                [finding],
                **_readers(
                    descendants={"frontend-root": {"frontend-child"}},
                    indexed={"frontend-root"},
                    rollouts={"frontend-child": duplicate_records},
                ),
            )
            mismatch_plan = build_cleanup_plan(
                [finding],
                descendant_reader=(
                    lambda _home, ids, strict=True: {
                        thread_id: (
                            {"frontend-child"}
                            if thread_id == "frontend-root"
                            else set()
                        )
                        for thread_id in ids
                    }
                ),
                index_reader=(
                    lambda _home, _ids, strict=True: {
                        "frontend-root": {"id": "frontend-root"},
                        "frontend-child": {
                            "id": "frontend-child",
                            "rollout_path": str(mismatched_record.path),
                        },
                    }
                ),
                rollout_reader=(
                    lambda _home, thread_id: (
                        [mismatched_record]
                        if thread_id == "frontend-child"
                        else []
                    )
                ),
            )
            source_plan = build_cleanup_plan(
                [finding],
                **_readers(
                    descendants={"frontend-root": {"frontend-child"}},
                    indexed={"frontend-root"},
                    rollouts={
                        "frontend-child": [source_conflict_record]
                    },
                ),
            )

        for plan in (duplicate_plan, mismatch_plan, source_plan):
            self.assertEqual(
                {observation.target.thread_id for observation in plan.observations},
                {"frontend-root"},
            )
            delete_action = next(
                action
                for action in plan.actions
                if action.kind is ActionKind.DELETE_CONVERSATION
            )
            self.assertFalse(delete_action.available)
            self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)
            self.assertIn(
                "frontend-child",
                delete_action.unavailable_reason or "",
            )

    def test_current_rollout_metadata_changes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            path = home / "sessions" / "same-path.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("current content\n", encoding="utf-8")
            finding = _finding(home, "thread", "index_missing_rollout")
            first_record = _record(home, "thread", "same-path.jsonl")
            second_record = _record(
                home,
                "thread",
                "same-path.jsonl",
                source={"custom": {"version": 2}},
            )
            first = build_cleanup_plan(
                [finding],
                **_readers(
                    indexed={"thread"},
                    rollouts={"thread": [first_record]},
                ),
            )
            second = build_cleanup_plan(
                [finding],
                **_readers(
                    indexed={"thread"},
                    rollouts={"thread": [second_record]},
                ),
            )

        first_action = next(
            action
            for action in first.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        second_action = next(
            action
            for action in second.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(first_action.action_id, second_action.action_id)
        self.assertEqual(
            first_action.impact.rollout_paths,
            second_action.impact.rollout_paths,
        )
        self.assertNotEqual(
            first_action.impact.rollout_state_fingerprints,
            second_action.impact.rollout_state_fingerprints,
        )
        self.assertNotEqual(
            first_action.snapshot_fingerprint,
            second_action.snapshot_fingerprint,
        )

    def test_indexed_damaged_rollout_path_is_content_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            damaged_path = home / "sessions" / "damaged.jsonl"
            damaged_path.parent.mkdir(parents=True)
            damaged_path.write_text(
                "{not valid session metadata}\nrecoverable chat content\n",
                encoding="utf-8",
            )
            finding = _finding(
                home,
                "thread-with-damaged-rollout",
                "frontend_deleted_reference",
                platform="aionui",
            )
            descendants = (
                lambda _home, ids, strict=True: {
                    thread_id: set() for thread_id in ids
                }
            )
            with_index_path = build_cleanup_plan(
                [finding],
                descendant_reader=descendants,
                index_reader=(
                    lambda _home, _ids, strict=True: {
                        finding.thread_id: {
                            "id": finding.thread_id,
                            "rollout_path": "sessions/damaged.jsonl",
                        }
                    }
                ),
            )
            without_index_path = build_cleanup_plan(
                [finding],
                descendant_reader=descendants,
                index_reader=(
                    lambda _home, _ids, strict=True: {
                        finding.thread_id: {"id": finding.thread_id}
                    }
                ),
            )

        expected_path = normalize_storage_path(damaged_path)
        observation = with_index_path.observations[0]
        delete_action = next(
            action
            for action in with_index_path.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        baseline_action = next(
            action
            for action in without_index_path.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(observation.rollout_paths, (expected_path,))
        self.assertEqual(delete_action.impact.rollout_paths, (expected_path,))
        self.assertEqual(delete_action.impact.rollout_file_count, 1)
        self.assertFalse(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)
        self.assertIn(
            "metadata did not confirm",
            delete_action.unavailable_reason or "",
        )
        self.assertNotEqual(
            delete_action.snapshot_fingerprint,
            baseline_action.snapshot_fingerprint,
        )

    def test_frontend_delete_blocks_indexed_path_owned_by_other_id(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            indexed_path = home / "sessions" / "indexed-other.jsonl"
            indexed_path.parent.mkdir(parents=True)
            indexed_path.write_text("current content\n", encoding="utf-8")
            other_record = RolloutRecord(
                thread_id="other-thread",
                path=indexed_path,
                originator="aionui",
                source=None,
                cwd=None,
                timestamp=None,
                archived=False,
            )
            finding = _finding(
                home,
                "frontend-thread",
                "frontend_deleted_reference",
                platform="aionui",
                details={
                    "ownership_status": "confirmed",
                    "cascade_check_available": True,
                    "cleanable": True,
                    "thread_delete_supported": True,
                },
            )
            plan = build_cleanup_plan(
                [finding],
                descendant_reader=(
                    lambda _home, ids, strict=True: {
                        thread_id: set() for thread_id in ids
                    }
                ),
                index_reader=(
                    lambda _home, _ids, strict=True: {
                        finding.thread_id: {
                            "id": finding.thread_id,
                            "rollout_path": str(indexed_path),
                        }
                    }
                ),
                rollout_reader=(
                    lambda _home, _thread_id: [other_record]
                ),
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(
            delete_action.impact.rollout_paths,
            (normalize_storage_path(indexed_path),),
        )
        self.assertFalse(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)
        self.assertIn(
            "did not confirm conversation frontend-thread",
            delete_action.unavailable_reason or "",
        )

    def test_new_frontend_residual_changes_snapshot_for_same_action(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            first_finding = _finding(
                home,
                "shared-thread",
                "frontend_deleted_reference",
                platform="aionui",
            )
            second_finding = _finding(
                home,
                "shared-thread",
                "frontend_deleted_reference",
                platform="aionui",
            )
            second_finding.platform_session_id = "aionui-another-residual"
            readers = _readers(indexed={"shared-thread"})

            first_plan = build_cleanup_plan([first_finding], **readers)
            expanded_plan = build_cleanup_plan(
                [first_finding, second_finding],
                **readers,
            )

        first_action = next(
            action
            for action in first_plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        expanded_action = next(
            action
            for action in expanded_plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(first_action.action_id, expanded_action.action_id)
        self.assertEqual(first_action.impact.frontend_residual_count, 1)
        self.assertEqual(expanded_action.impact.frontend_residual_count, 2)
        self.assertNotEqual(
            first_action.snapshot_fingerprint,
            expanded_action.snapshot_fingerprint,
        )

    def test_same_thread_id_in_two_storages_is_never_merged(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home_a = Path(root) / "home-a"
            home_b = Path(root) / "home-b"
            findings = [
                _finding(home_a, "same-id", "index_missing_rollout"),
                _finding(home_b, "same-id", "index_missing_rollout"),
            ]

            plan = build_cleanup_plan(
                findings,
                **_readers(indexed={"same-id"}),
            )

        targets = {observation.target for observation in plan.observations}
        delete_actions = [
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        ]
        self.assertEqual(len(targets), 2)
        self.assertEqual(len(delete_actions), 2)
        self.assertEqual(len({action.action_id for action in delete_actions}), 2)
        self.assertEqual(len(plan.storages), 2)

    def test_additional_findings_expand_to_stable_independent_observations(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            finding = _finding(
                home,
                "thread-1",
                "index_missing_rollout",
                details={
                    "additional_findings": [
                        {
                            "platform": "native",
                            "platform_session_id": "parent->thread-1",
                            "reason": "broken relation",
                            "details": {
                                "finding_type": "residual_spawn_edge",
                                "parent_thread_id": "parent",
                                "child_thread_id": "thread-1",
                                "thread_delete_supported": False,
                            },
                        }
                    ]
                },
            )
            readers = _readers(indexed={"thread-1"})

            first = build_cleanup_plan([finding], **readers)
            second = build_cleanup_plan([finding], **readers)

        self.assertEqual(
            {observation.finding_type for observation in first.observations},
            {"index_missing_rollout", "residual_spawn_edge"},
        )
        self.assertEqual(
            [item.observation_id for item in first.observations],
            [item.observation_id for item in second.observations],
        )
        self.assertIn(
            ActionKind.REMOVE_BROKEN_RELATION,
            {action.kind for action in first.actions},
        )
        relation_action = next(
            action
            for action in first.actions
            if action.kind is ActionKind.REMOVE_BROKEN_RELATION
        )
        self.assertFalse(relation_action.available)
        self.assertIn("backup", relation_action.unavailable_reason or "")

    def test_residual_relation_with_exact_child_artifact_offers_delete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "explicit-home"
            parent_id = "missing-parent"
            source = {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": parent_id,
                        "depth": 1,
                    }
                }
            }
            record = _record(home, "edge-child", source=source)
            record.path.parent.mkdir(parents=True)
            record.path.write_text("current child content\n", encoding="utf-8")
            finding = _finding(
                home,
                "edge-child",
                "residual_spawn_edge",
                indexed=False,
                rollout=record,
                details={
                    "parent_thread_id": parent_id,
                    "child_thread_id": "edge-child",
                    "edge_status": "closed",
                    "parent_index_missing": True,
                    "child_index_missing": True,
                    "parent_rollout_present": False,
                    "child_rollout_present": True,
                    "source_parent_ids": [parent_id],
                    "source_conflict": False,
                    "subagent_evidence": ["session_meta.source"],
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "thread/delete does not expose a standalone "
                        "spawn-edge cleanup operation."
                    ),
                    "direct_database_edit_supported": False,
                },
            )
            plan = build_cleanup_plan(
                [finding],
                **_readers(rollouts={"edge-child": [record]}),
            )

        by_kind = {action.kind: action for action in plan.actions}
        self.assertIn(ActionKind.REMOVE_BROKEN_RELATION, by_kind)
        self.assertIn(ActionKind.DELETE_CONVERSATION, by_kind)
        self.assertFalse(by_kind[ActionKind.REMOVE_BROKEN_RELATION].available)
        delete_action = by_kind[ActionKind.DELETE_CONVERSATION]
        self.assertTrue(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.HIGH)
        self.assertTrue(delete_action.requires_explicit_selection)
        self.assertEqual(delete_action.impact.affected_thread_ids, ("edge-child",))
        self.assertEqual(delete_action.impact.rollout_file_count, 1)

    def test_residual_relation_delete_requires_safe_child_identity(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "explicit-home"
            exact_record = _record(
                home,
                "conflicted-child",
                source={
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "outside-affected-scope"
                        }
                    }
                },
            )
            exact_record.path.parent.mkdir(parents=True)
            exact_record.path.write_text(
                "current child content\n",
                encoding="utf-8",
            )
            conflict = _finding(
                home,
                "conflicted-child",
                "residual_spawn_edge",
                indexed=False,
                rollout=exact_record,
                details={
                    "parent_thread_id": "edge-parent",
                    "child_thread_id": "conflicted-child",
                    "source_conflict": False,
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "thread/delete does not expose a standalone "
                        "spawn-edge cleanup operation."
                    ),
                },
            )
            missing = _finding(
                home,
                "gone-child",
                "residual_spawn_edge",
                indexed=False,
                details={
                    "parent_thread_id": "edge-parent",
                    "child_thread_id": "gone-child",
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "thread/delete does not expose a standalone "
                        "spawn-edge cleanup operation."
                    ),
                },
            )
            incomplete_record = _record(home, "incomplete-child")
            incomplete_record.path.write_text(
                "current child content\n",
                encoding="utf-8",
            )
            incomplete = _finding(
                home,
                "incomplete-child",
                "residual_spawn_edge",
                indexed=False,
                rollout=incomplete_record,
                details={
                    "parent_thread_id": "edge-parent",
                    "child_thread_id": "incomplete-child",
                    "source_conflict": False,
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "thread/delete does not expose a standalone "
                        "spawn-edge cleanup operation."
                    ),
                },
            )
            conflict_plan = build_cleanup_plan(
                [conflict],
                **_readers(
                    rollouts={"conflicted-child": [exact_record]}
                ),
            )
            missing_plan = build_cleanup_plan(
                [missing],
                **_readers(),
            )
            incomplete_plan = build_cleanup_plan(
                [incomplete],
                **_readers(
                    rollouts={"incomplete-child": [incomplete_record]}
                ),
            )

        conflict_delete = next(
            action
            for action in conflict_plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        incomplete_delete = next(
            action
            for action in incomplete_plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertFalse(conflict_delete.available)
        self.assertEqual(conflict_delete.risk, RiskLevel.BLOCKED)
        self.assertIn(
            "source-parent",
            conflict_delete.unavailable_reason or "",
        )
        self.assertFalse(incomplete_delete.available)
        self.assertEqual(incomplete_delete.risk, RiskLevel.BLOCKED)
        self.assertIn(
            "exact native child-target contract",
            incomplete_delete.unavailable_reason or "",
        )
        self.assertNotIn(
            ActionKind.DELETE_CONVERSATION,
            {action.kind for action in missing_plan.actions},
        )
        self.assertIn(
            ActionKind.REMOVE_BROKEN_RELATION,
            {action.kind for action in missing_plan.actions},
        )

    def test_matching_native_residual_source_parent_allows_child_delete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            parent_id = "indexed-parent"
            child_id = "rollout-only-child"
            source = {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": parent_id,
                        "depth": 1,
                    }
                }
            }
            record = _record(home, child_id, source=source)
            record.path.parent.mkdir(parents=True)
            record.path.write_text(
                "current child content\n",
                encoding="utf-8",
            )
            residual_details = {
                "finding_type": "residual_spawn_edge",
                "parent_thread_id": parent_id,
                "child_thread_id": child_id,
                "edge_status": "closed",
                "parent_index_missing": False,
                "child_index_missing": True,
                "parent_rollout_present": True,
                "child_rollout_present": True,
                "source_parent_ids": [parent_id],
                "source_conflict": False,
                "subagent_evidence": ["session_meta.source"],
                "thread_delete_supported": False,
                "needs_quarantine": False,
                "cleanable": False,
                "cleanup_blocked_reason": (
                    "thread/delete does not expose a standalone "
                    "spawn-edge cleanup operation."
                ),
                "direct_database_edit_supported": False,
            }
            finding = _finding(
                home,
                child_id,
                "rollout_missing_index",
                indexed=False,
                rollout=record,
                details={
                    "additional_findings": [
                        {
                            "platform": "native",
                            "platform_session_id": (
                                f"{parent_id}->{child_id}"
                            ),
                            "reason": "closed residual spawn relation",
                            "details": residual_details,
                        }
                    ]
                },
            )
            plan = build_cleanup_plan(
                [finding],
                **_readers(rollouts={child_id: [record]}),
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertTrue(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.HIGH)
        self.assertTrue(delete_action.requires_explicit_selection)
        self.assertEqual(delete_action.impact.affected_thread_ids, (child_id,))
        self.assertNotIn(parent_id, delete_action.impact.affected_thread_ids)

    def test_exact_missing_parent_orphan_source_exception_is_narrow(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "explicit-home"
            source_parent = "deleted-parent"
            source = {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": source_parent,
                        "depth": 1,
                    }
                }
            }
            record = _record(
                home,
                "orphan-child",
                source=source,
            )
            record.path.parent.mkdir(parents=True)
            record.path.write_text("current child content\n", encoding="utf-8")
            common_details = {
                "parent_indexed": False,
                "parent_rollout_present": False,
                "spawn_edge_present": False,
                "spawn_edge_status": None,
                "source_conflict": False,
                "thread_delete_supported": True,
                "cleanable": True,
                "requires_explicit_selection": True,
                "evidence_strength": "source_consensus",
                "subagent_evidence": [
                    "threads.source",
                    "session_meta.source",
                ],
                "cleanup_blocked_reason": None,
            }
            approved = _finding(
                home,
                "orphan-child",
                "orphaned_subagent_thread",
                rollout=record,
                details={
                    **common_details,
                    "parent_thread_id": source_parent,
                },
            )
            wrong_parent = _finding(
                home,
                "orphan-child",
                "orphaned_subagent_thread",
                rollout=record,
                details={
                    **common_details,
                    "parent_thread_id": "different-parent",
                },
            )
            readers = _readers(
                indexed={"orphan-child"},
                rollouts={"orphan-child": [record]},
            )
            approved_plan = build_cleanup_plan([approved], **readers)
            wrong_parent_plan = build_cleanup_plan([wrong_parent], **readers)
            duplicate_record = _record(
                home,
                "orphan-child",
                "orphan-child-copy.jsonl",
                source=source,
            )
            duplicate_record.path.write_text(
                "duplicate current child content\n",
                encoding="utf-8",
            )
            duplicate = _finding(
                home,
                "orphan-child",
                "duplicate_rollout",
                rollout=record,
                details={
                    "rollout_paths": [
                        str(record.path),
                        str(duplicate_record.path),
                    ],
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "Official deletion has not been verified to remove "
                        "every duplicate rollout; preserve all copies for "
                        "manual review."
                    ),
                },
            )
            combined_plan = build_cleanup_plan(
                [approved, duplicate],
                **_readers(
                    indexed={"orphan-child"},
                    rollouts={
                        "orphan-child": [record, duplicate_record],
                    },
                ),
            )

        approved_delete = next(
            action
            for action in approved_plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        wrong_parent_delete = next(
            action
            for action in wrong_parent_plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        combined_delete = next(
            action
            for action in combined_plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertTrue(approved_delete.available)
        self.assertTrue(approved_delete.requires_explicit_selection)
        self.assertTrue(combined_delete.available)
        self.assertEqual(combined_delete.risk, RiskLevel.HIGH)
        self.assertTrue(combined_delete.requires_explicit_selection)
        self.assertFalse(wrong_parent_delete.available)
        self.assertEqual(wrong_parent_delete.risk, RiskLevel.BLOCKED)
        self.assertIn(
            "source-parent metadata outside",
            wrong_parent_delete.unavailable_reason or "",
        )

    def test_explicit_unknown_home_reforms_storage_and_action_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            explicit_home = Path(root) / "unknown-home"
            finding = _finding(
                explicit_home,
                "explicit-thread",
                "index_missing_rollout",
            )
            plan = build_cleanup_plan(
                [finding],
                **_readers(indexed={"explicit-thread"}),
            )

        self.assertEqual(len(plan.storages), 1)
        storage = plan.storages[0]
        self.assertEqual(
            storage.normalized_path,
            normalize_storage_path(explicit_home),
        )
        observation = plan.observations[0]
        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(observation.target.storage_id, storage.storage_id)
        self.assertEqual(delete_action.target.storage_id, storage.storage_id)
        self.assertEqual(delete_action.target.thread_id, "explicit-thread")
        self.assertTrue(delete_action.available)

    def test_conflicting_bin_hints_block_storage_mutations_and_fingerprint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            first_hint = Path(root) / "bin-one" / "codex.exe"
            second_hint = Path(root) / "bin-two" / "codex.exe"
            conflict_findings = [
                _finding(
                    home,
                    "first-thread",
                    "index_missing_rollout",
                    codex_bin_hint=first_hint,
                ),
                _finding(
                    home,
                    "second-thread",
                    "index_missing_rollout",
                    codex_bin_hint=second_hint,
                ),
            ]
            conflict_plan = build_cleanup_plan(
                conflict_findings,
                **_readers(indexed={"first-thread", "second-thread"}),
            )
            deduplicated_plan = build_cleanup_plan(
                [
                    _finding(
                        home,
                        "deduplicated-thread",
                        "index_missing_rollout",
                        codex_bin_hint=first_hint,
                        details={
                            "codex_bin_hint_candidates": [
                                str(first_hint),
                                str(second_hint),
                            ]
                        },
                    )
                ],
                **_readers(indexed={"deduplicated-thread"}),
            )
            baseline_plan = build_cleanup_plan(
                [
                    _finding(
                        home,
                        "first-thread",
                        "index_missing_rollout",
                        codex_bin_hint=first_hint,
                    ),
                    _finding(
                        home,
                        "second-thread",
                        "index_missing_rollout",
                        codex_bin_hint=first_hint,
                    ),
                ],
                **_readers(indexed={"first-thread", "second-thread"}),
            )

        storage = conflict_plan.storages[0]
        mutable_actions = [
            action
            for action in conflict_plan.actions
            if action.kind is not ActionKind.KEEP
        ]
        self.assertEqual(storage.scan_status, ScanStatus.PARTIAL)
        self.assertIsNone(storage.codex_bin_hint)
        self.assertTrue(storage.errors)
        self.assertIn("Conflicting Codex executable hints", storage.errors[0])
        self.assertTrue(mutable_actions)
        self.assertTrue(all(not action.available for action in mutable_actions))
        self.assertTrue(
            all(action.risk is RiskLevel.BLOCKED for action in mutable_actions)
        )
        self.assertTrue(
            all(
                "Conflicting Codex executable hints"
                in (action.unavailable_reason or "")
                for action in mutable_actions
            )
        )
        self.assertNotEqual(
            conflict_plan.plan_fingerprint,
            baseline_plan.plan_fingerprint,
        )
        storage_json = conflict_plan.to_dict()["storages"][0]
        self.assertEqual(storage_json["errors"], list(storage.errors))
        self.assertIsNone(storage_json["codex_bin_hint"])
        deduplicated_storage = deduplicated_plan.storages[0]
        deduplicated_delete = next(
            action
            for action in deduplicated_plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(
            deduplicated_storage.scan_status,
            ScanStatus.PARTIAL,
        )
        self.assertIn(
            "Conflicting Codex executable hints",
            deduplicated_storage.errors[0],
        )
        self.assertFalse(deduplicated_delete.available)
        self.assertEqual(deduplicated_delete.risk, RiskLevel.BLOCKED)

    def test_equivalent_relative_and_absolute_bin_hints_do_not_conflict(
        self,
    ) -> None:
        home = Path.cwd() / "equivalent-hint-home"
        relative_hint = Path("bin") / "codex.exe"
        absolute_hint = Path.cwd() / relative_hint
        plan = build_cleanup_plan(
            [
                _finding(
                    home,
                    "first-thread",
                    "index_missing_rollout",
                    codex_bin_hint=relative_hint,
                ),
                _finding(
                    home,
                    "second-thread",
                    "index_missing_rollout",
                    codex_bin_hint=absolute_hint,
                ),
            ],
            **_readers(indexed={"first-thread", "second-thread"}),
        )

        storage = plan.storages[0]
        delete_actions = [
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        ]
        self.assertEqual(storage.scan_status, ScanStatus.OK)
        self.assertEqual(storage.errors, ())
        self.assertEqual(
            storage.codex_bin_hint,
            Path(normalize_storage_path(absolute_hint)),
        )
        self.assertEqual(
            storage.to_dict()["codex_bin_hint"],
            normalize_storage_path(relative_hint),
        )
        self.assertTrue(delete_actions)
        self.assertTrue(all(action.available for action in delete_actions))

    def test_integrity_findings_have_specific_structured_actions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            records = {
                "duplicate": [
                    _record(home, "duplicate", "one.jsonl"),
                    _record(home, "duplicate", "two.jsonl"),
                ],
                "path": [_record(home, "path")],
            }
            findings = [
                _finding(
                    home,
                    "duplicate",
                    "duplicate_rollout",
                    rollout=records["duplicate"][0],
                    details={
                        "rollout_paths": [
                            str(record.path) for record in records["duplicate"]
                        ],
                        "thread_delete_supported": False,
                        "cleanable": False,
                    },
                ),
                _finding(
                    home,
                    "path",
                    "index_rollout_path_mismatch",
                    rollout=records["path"][0],
                    details={
                        "actual_rollout_paths": [str(records["path"][0].path)],
                        "thread_delete_supported": False,
                        "cleanable": False,
                    },
                ),
            ]
            plan = build_cleanup_plan(
                findings,
                **_readers(
                    indexed={"duplicate", "path"},
                    rollouts=records,
                ),
            )

        by_target = {}
        for action in plan.actions:
            by_target.setdefault(action.target.thread_id, {})[action.kind] = action
        self.assertIn(ActionKind.QUARANTINE_ARTIFACTS, by_target["duplicate"])
        self.assertIn(ActionKind.DELETE_CONVERSATION, by_target["duplicate"])
        self.assertFalse(
            by_target["duplicate"][ActionKind.QUARANTINE_ARTIFACTS].available
        )
        self.assertEqual(
            by_target["duplicate"][ActionKind.QUARANTINE_ARTIFACTS].risk,
            RiskLevel.HIGH,
        )
        self.assertIn(ActionKind.REPAIR_INDEX_PATH, by_target["path"])
        self.assertFalse(by_target["path"][ActionKind.REPAIR_INDEX_PATH].available)

    def test_impact_and_snapshot_cover_all_associated_task_conversations(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            root_record = _record(home, "root")
            child_record = _record(home, "child")
            changed_child_record = _record(home, "child", "changed.jsonl")
            for record in (root_record, child_record, changed_child_record):
                record.path.parent.mkdir(parents=True, exist_ok=True)
                record.path.write_text("current content\n", encoding="utf-8")
            finding = _finding(
                home,
                "root",
                "orphaned_subagent_thread",
                rollout=root_record,
            )
            common = {
                "descendants": {"root": {"child", "grandchild"}},
                "indexed": {"root", "child", "grandchild"},
            }
            first = build_cleanup_plan(
                [finding],
                **_readers(
                    **common,
                    rollouts={
                        "root": [root_record],
                        "child": [child_record],
                    },
                ),
            )
            second = build_cleanup_plan(
                [finding],
                **_readers(
                    **common,
                    rollouts={
                        "root": [root_record],
                        "child": [changed_child_record],
                    },
                ),
            )

        first_action = next(
            action
            for action in first.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        second_action = next(
            action
            for action in second.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(
            first_action.impact.descendant_thread_ids,
            ("child", "grandchild"),
        )
        self.assertEqual(first_action.impact.index_record_count, 3)
        self.assertEqual(
            first_action.impact.indexed_thread_ids,
            ("child", "grandchild", "root"),
        )
        self.assertEqual(
            first_action.impact.index_record_count,
            len(first_action.impact.indexed_thread_ids),
        )
        self.assertEqual(
            first_action.impact.to_dict()["indexed_thread_ids"],
            ["child", "grandchild", "root"],
        )
        self.assertEqual(first_action.impact.rollout_file_count, 2)
        self.assertEqual(first_action.action_id, second_action.action_id)
        self.assertNotEqual(
            first_action.snapshot_fingerprint,
            second_action.snapshot_fingerprint,
        )

    def test_failure_in_empty_storage_does_not_block_independent_storage(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            failed_home = Path(root) / "failed-home"
            healthy_home = Path(root) / "Cindy"
            finding = _finding(
                healthy_home,
                "healthy",
                "index_missing_rollout",
            )
            report = SimpleNamespace(
                findings=[finding],
                errors=[
                    SimpleNamespace(
                        platform="native",
                        message="state database is unreadable",
                        error_type="OSError",
                        codex_home=failed_home,
                    )
                ],
            )

            plan = build_cleanup_plan(
                report,
                **_readers(indexed={"healthy"}),
            )

        failed_storage = next(
            storage
            for storage in plan.storages
            if storage.path.name.lower() == "failed-home"
        )
        healthy_storage = next(
            storage
            for storage in plan.storages
            if storage.path.name.lower() == "cindy"
        )
        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertEqual(failed_storage.scan_status, ScanStatus.FAILED)
        self.assertEqual(healthy_storage.scan_status, ScanStatus.OK)
        self.assertEqual(healthy_storage.label, "Cindy 专用数据目录")
        self.assertTrue(delete_action.available)
        self.assertEqual(plan.errors, ())

    def test_unassigned_scan_error_is_preserved_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            report = SimpleNamespace(
                findings=[
                    _finding(home, "thread", "index_missing_rollout")
                ],
                errors=[
                    SimpleNamespace(platform="unknown", message="scan failed")
                ],
            )
            plan = build_cleanup_plan(
                report,
                **_readers(indexed={"thread"}),
            )

        delete_action = next(
            action
            for action in plan.actions
            if action.kind is ActionKind.DELETE_CONVERSATION
        )
        self.assertTrue(plan.errors)
        self.assertFalse(delete_action.available)
        self.assertEqual(delete_action.risk, RiskLevel.BLOCKED)


if __name__ == "__main__":
    unittest.main()
