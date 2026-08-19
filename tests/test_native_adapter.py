from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent_record_janitor.adapters import (
    NativeIntegrityAdapter,
    NativeIntegrityError,
)
from local_agent_record_janitor.cleaner import scan_adapters

from tests.support import create_thread_index, write_rollout


class NativeIntegrityAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()

    def adapter(self) -> NativeIntegrityAdapter:
        return NativeIntegrityAdapter(
            codex_home=self.codex_home,
            codex_bin_hint=self.root / "does-not-exist",
        )

    @staticmethod
    def by_type(findings, finding_type: str):
        return [
            finding
            for finding in findings
            if finding.details["finding_type"] == finding_type
        ]

    def test_detects_nine_index_rows_without_rollout_bodies(self) -> None:
        # This is the small deterministic equivalent of the nine stale native
        # index rows observed during the original cleanup.
        rows = [
            {
                "id": f"index-only-{number}",
                "rollout_path": str(
                    self.codex_home / "sessions" / f"missing-{number}.jsonl"
                ),
            }
            for number in range(9)
        ]
        create_thread_index(self.codex_home, rows)

        findings = self.adapter().scan()
        stale = self.by_type(findings, "index_missing_rollout")

        self.assertEqual(len(stale), 9)
        self.assertEqual(
            {finding.thread_id for finding in stale},
            {f"index-only-{number}" for number in range(9)},
        )
        self.assertTrue(
            all(finding.details["thread_delete_supported"] for finding in stale)
        )
        self.assertTrue(all(finding.details["cleanable"] for finding in stale))
        self.assertTrue(all(not finding.details["needs_quarantine"] for finding in stale))

    def test_detects_active_and_archived_rollouts_without_index_rows(self) -> None:
        create_thread_index(self.codex_home, [])
        active = write_rollout(
            self.codex_home, "rollout-only-active", originator="Codex Desktop"
        )
        archived = write_rollout(
            self.codex_home,
            "rollout-only-archived",
            originator="Codex Desktop",
            archived=True,
        )

        findings = self.adapter().scan()
        residuals = self.by_type(findings, "rollout_missing_index")

        self.assertEqual(len(residuals), 2)
        by_id = {finding.thread_id: finding for finding in residuals}
        self.assertEqual(by_id["rollout-only-active"].rollout.path, active)
        self.assertEqual(by_id["rollout-only-archived"].rollout.path, archived)
        self.assertTrue(by_id["rollout-only-archived"].codex_archived)
        self.assertTrue(
            all(finding.details["thread_delete_supported"] for finding in residuals)
        )
        self.assertTrue(all(finding.details["cleanable"] for finding in residuals))
        self.assertTrue(
            all(not finding.details["needs_quarantine"] for finding in residuals)
        )

    def test_detects_stale_index_path_when_thread_is_found_elsewhere(self) -> None:
        thread_id = "moved-thread"
        actual = write_rollout(
            self.codex_home, thread_id, originator="Codex Desktop"
        )
        stale = self.codex_home / "sessions" / "old-location.jsonl"
        create_thread_index(
            self.codex_home,
            [{"id": thread_id, "rollout_path": str(stale)}],
        )

        findings = self.adapter().scan()
        mismatches = self.by_type(findings, "index_rollout_path_mismatch")

        self.assertEqual(len(mismatches), 1)
        finding = mismatches[0]
        self.assertEqual(finding.rollout.path, actual)
        self.assertEqual(finding.details["indexed_rollout_path"], str(stale))
        self.assertFalse(finding.details["thread_delete_supported"])
        self.assertFalse(finding.details["cleanable"])
        self.assertIn("recoverable", finding.details["cleanup_blocked_reason"])
        self.assertEqual(
            self.by_type(findings, "index_missing_rollout"),
            [],
        )

    def test_valid_relative_index_path_is_not_reported(self) -> None:
        thread_id = "relative-path"
        rollout = write_rollout(
            self.codex_home, thread_id, originator="Codex Desktop"
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": thread_id,
                    "rollout_path": str(rollout.relative_to(self.codex_home)),
                }
            ],
        )

        self.assertEqual(self.adapter().scan(), [])

    def test_duplicate_rollouts_are_visible_and_never_auto_cleaned(self) -> None:
        thread_id = "duplicate-thread"
        active = write_rollout(
            self.codex_home, thread_id, originator="Codex Desktop"
        )
        archived = write_rollout(
            self.codex_home,
            thread_id,
            originator="Codex Desktop",
            archived=True,
        )
        create_thread_index(
            self.codex_home,
            [{"id": thread_id, "rollout_path": str(active)}],
        )

        duplicates = self.by_type(
            self.adapter().scan(), "duplicate_rollout"
        )

        self.assertEqual(len(duplicates), 1)
        finding = duplicates[0]
        self.assertEqual(
            set(finding.details["rollout_paths"]),
            {str(active), str(archived)},
        )
        self.assertEqual(finding.details["rollout_count"], 2)
        self.assertFalse(finding.details["cleanable"])
        self.assertFalse(finding.details["thread_delete_supported"])

    def test_index_file_metadata_for_another_thread_is_not_trusted(self) -> None:
        indexed_id = "indexed-id"
        metadata_id = "metadata-id"
        rollout = write_rollout(
            self.codex_home, metadata_id, originator="Codex Desktop"
        )
        create_thread_index(
            self.codex_home,
            [{"id": indexed_id, "rollout_path": str(rollout)}],
        )

        findings = self.adapter().scan()
        mismatches = self.by_type(
            findings, "index_rollout_metadata_mismatch"
        )

        self.assertEqual(len(mismatches), 1)
        mismatch = mismatches[0]
        self.assertEqual(mismatch.thread_id, indexed_id)
        self.assertEqual(mismatch.details["metadata_thread_id"], metadata_id)
        self.assertFalse(mismatch.details["cleanable"])
        # The metadata ID is independently visible as a rollout-only thread.
        rollout_only = self.by_type(findings, "rollout_missing_index")
        self.assertEqual(
            [finding.thread_id for finding in rollout_only], [metadata_id]
        )

    def test_detects_small_fixture_of_orphaned_spawned_threads(self) -> None:
        # Four children exercise the same metadata pattern as the previously
        # observed batch of 58 orphaned subagent rollouts without making the
        # unit test unnecessarily large.
        missing_parent = "deleted-parent"
        rows = []
        edges = []
        for number in range(4):
            child_id = f"orphan-child-{number}"
            source = {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": missing_parent,
                        "depth": 1,
                    }
                }
            }
            rollout = write_rollout(
                self.codex_home,
                child_id,
                originator="Codex Desktop",
                source=source,
            )
            rows.append(
                {
                    "id": child_id,
                    "rollout_path": str(rollout),
                    "source": source,
                    "thread_source": "subagent",
                }
            )
            edges.append(
                {
                    "parent_thread_id": missing_parent,
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            )
        create_thread_index(self.codex_home, rows, spawn_edges=edges)

        findings = self.adapter().scan()
        orphans = self.by_type(findings, "orphaned_subagent_thread")

        self.assertEqual(len(orphans), 4)
        self.assertEqual(
            {finding.thread_id for finding in orphans},
            {f"orphan-child-{number}" for number in range(4)},
        )
        self.assertTrue(all(finding.details["cleanable"] for finding in orphans))
        self.assertTrue(
            all(
                finding.details["parent_indexed"] is False
                and finding.details["parent_rollout_present"] is False
                for finding in orphans
            )
        )
        # A proven orphan finding already carries the dangling edge evidence;
        # do not double-report the same edge.
        self.assertEqual(self.by_type(findings, "residual_spawn_edge"), [])

    def test_parent_index_without_rollout_is_one_parent_problem(self) -> None:
        parent_id = "broken-parent"
        child_id = "child-of-broken-parent"
        child_source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": parent_id, "depth": 1}
            }
        }
        child_rollout = write_rollout(
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
                    "rollout_path": str(
                        self.codex_home / "sessions" / "missing-parent.jsonl"
                    ),
                },
                {
                    "id": child_id,
                    "rollout_path": str(child_rollout),
                    "source": child_source,
                    "thread_source": "subagent",
                },
            ],
            spawn_edges=[
                {
                    "parent_thread_id": parent_id,
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            ],
        )

        findings = self.adapter().scan()
        parent = self.by_type(findings, "index_missing_rollout")[0]

        self.assertFalse(parent.details["cleanable"])
        self.assertEqual(parent.details["spawn_descendant_edge_count"], 1)
        self.assertEqual(
            self.by_type(findings, "orphaned_subagent_thread"), []
        )
        self.assertEqual(self.by_type(findings, "residual_spawn_edge"), [])

    def test_parent_rollout_without_index_is_one_parent_problem(self) -> None:
        parent_id = "rollout-only-parent"
        child_id = "child-of-rollout-only-parent"
        parent_rollout = write_rollout(
            self.codex_home,
            parent_id,
            originator="Codex Desktop",
        )
        child_source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": parent_id, "depth": 1}
            }
        }
        child_rollout = write_rollout(
            self.codex_home,
            child_id,
            originator="Codex Desktop",
            source=child_source,
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": child_id,
                    "rollout_path": str(child_rollout),
                    "source": child_source,
                    "thread_source": "subagent",
                },
            ],
            spawn_edges=[
                {
                    "parent_thread_id": parent_id,
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            ],
        )

        findings = self.adapter().scan()
        rollout_only = self.by_type(findings, "rollout_missing_index")

        self.assertEqual(len(rollout_only), 1)
        self.assertEqual(rollout_only[0].thread_id, parent_id)
        self.assertIsNotNone(rollout_only[0].rollout)
        self.assertEqual(rollout_only[0].rollout.path, parent_rollout)
        self.assertEqual(
            self.by_type(findings, "orphaned_subagent_thread"), []
        )
        self.assertEqual(self.by_type(findings, "residual_spawn_edge"), [])

    def test_rollout_only_child_of_complete_parent_is_one_child_problem(self) -> None:
        parent_id = "complete-parent"
        child_id = "rollout-only-child"
        parent_rollout = write_rollout(
            self.codex_home,
            parent_id,
            originator="Codex Desktop",
        )
        child_source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": parent_id, "depth": 1}
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
            [{"id": parent_id, "rollout_path": str(parent_rollout)}],
            spawn_edges=[
                {
                    "parent_thread_id": parent_id,
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            ],
        )

        findings = self.adapter().scan()
        rollout_only = self.by_type(findings, "rollout_missing_index")

        self.assertEqual(len(rollout_only), 1)
        self.assertEqual(rollout_only[0].thread_id, child_id)
        self.assertEqual(self.by_type(findings, "residual_spawn_edge"), [])
        self.assertEqual(
            self.by_type(findings, "orphaned_subagent_thread"), []
        )

    def test_open_edge_blocks_orphan_autoclean(self) -> None:
        parent_id = "missing-open-parent"
        child_id = "open-child"
        source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": parent_id, "depth": 1}
            }
        }
        rollout = write_rollout(
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
                    "rollout_path": str(rollout),
                    "source": source,
                    "thread_source": "subagent",
                }
            ],
            spawn_edges=[
                {
                    "parent_thread_id": parent_id,
                    "child_thread_id": child_id,
                    "status": "open",
                }
            ],
        )

        orphan = self.by_type(
            self.adapter().scan(), "orphaned_subagent_thread"
        )[0]

        self.assertTrue(orphan.details["thread_delete_supported"])
        self.assertFalse(orphan.details["cleanable"])
        self.assertIn("still open", orphan.details["cleanup_blocked_reason"])

    def test_source_consensus_orphan_is_cleanable_by_explicit_selection(self) -> None:
        parent_id = "source-only-missing-parent"
        child_id = "source-only-child"
        source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": parent_id, "depth": 1}
            }
        }
        rollout = write_rollout(
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
                    "rollout_path": str(rollout),
                    "source": source,
                    "thread_source": "subagent",
                }
            ],
        )

        orphan = self.by_type(
            self.adapter().scan(), "orphaned_subagent_thread"
        )[0]

        self.assertFalse(orphan.details["spawn_edge_present"])
        self.assertTrue(orphan.details["thread_delete_supported"])
        self.assertTrue(orphan.details["cleanable"])
        self.assertTrue(orphan.details["requires_explicit_selection"])
        self.assertEqual(
            orphan.details["evidence_strength"], "source_consensus"
        )
        self.assertIsNone(orphan.details["cleanup_blocked_reason"])

    def test_source_only_orphan_with_stale_index_path_remains_report_only(
        self,
    ) -> None:
        parent_id = "stale-path-missing-parent"
        child_id = "stale-path-child"
        source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": parent_id, "depth": 1}
            }
        }
        rollout = write_rollout(
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
                    "rollout_path": str(
                        self.codex_home / "sessions" / "stale-child.jsonl"
                    ),
                    "source": source,
                    "thread_source": "subagent",
                }
            ],
        )

        orphan = self.by_type(
            self.adapter().scan(), "orphaned_subagent_thread"
        )[0]

        self.assertFalse(orphan.details["cleanable"])
        self.assertFalse(orphan.details["requires_explicit_selection"])
        self.assertEqual(orphan.details["evidence_strength"], "source_only")
        self.assertIn(
            "no matching spawn edge",
            orphan.details["cleanup_blocked_reason"].lower(),
        )
        self.assertTrue(rollout.is_file())

    def test_source_only_orphan_without_subagent_thread_source_is_report_only(
        self,
    ) -> None:
        parent_id = "thread-source-missing-parent"
        child_id = "thread-source-child"
        source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": parent_id, "depth": 1}
            }
        }
        rollout = write_rollout(
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
                    "rollout_path": str(rollout),
                    "source": source,
                    "thread_source": "user",
                }
            ],
        )

        orphan = self.by_type(
            self.adapter().scan(), "orphaned_subagent_thread"
        )[0]

        self.assertFalse(orphan.details["cleanable"])
        self.assertEqual(orphan.details["evidence_strength"], "source_only")
        self.assertFalse(orphan.details["requires_explicit_selection"])

    def test_source_only_orphan_requires_explicit_parent_in_both_sources(
        self,
    ) -> None:
        parent_id = "one-sided-missing-parent"
        child_id = "one-sided-child"
        indexed_source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": parent_id, "depth": 1}
            }
        }
        rollout = write_rollout(
            self.codex_home,
            child_id,
            originator="Codex Desktop",
            source={"subagent": {"other": "guardian"}},
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": child_id,
                    "rollout_path": str(rollout),
                    "source": indexed_source,
                    "thread_source": "subagent",
                }
            ],
        )

        orphan = self.by_type(
            self.adapter().scan(), "orphaned_subagent_thread"
        )[0]

        self.assertFalse(orphan.details["cleanable"])
        self.assertEqual(orphan.details["evidence_strength"], "source_only")
        self.assertIn(
            "no matching spawn edge",
            orphan.details["cleanup_blocked_reason"].lower(),
        )

    def test_spawn_descendant_blocks_cascading_orphan_delete(self) -> None:
        missing_parent = "gone-root"
        child_id = "orphan-parent"
        grandchild_id = "healthy-grandchild"
        child_source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": missing_parent, "depth": 1}
            }
        }
        grandchild_source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": child_id, "depth": 2}
            }
        }
        child_rollout = write_rollout(
            self.codex_home,
            child_id,
            originator="Codex Desktop",
            source=child_source,
        )
        grandchild_rollout = write_rollout(
            self.codex_home,
            grandchild_id,
            originator="Codex Desktop",
            source=grandchild_source,
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": child_id,
                    "rollout_path": str(child_rollout),
                    "source": child_source,
                    "thread_source": "subagent",
                },
                {
                    "id": grandchild_id,
                    "rollout_path": str(grandchild_rollout),
                    "source": grandchild_source,
                    "thread_source": "subagent",
                },
            ],
            spawn_edges=[
                {
                    "parent_thread_id": missing_parent,
                    "child_thread_id": child_id,
                    "status": "closed",
                },
                {
                    "parent_thread_id": child_id,
                    "child_thread_id": grandchild_id,
                    "status": "closed",
                },
            ],
        )

        orphans = self.by_type(
            self.adapter().scan(), "orphaned_subagent_thread"
        )
        child = next(finding for finding in orphans if finding.thread_id == child_id)

        self.assertFalse(child.details["cleanable"])
        self.assertEqual(child.details["spawn_descendant_edge_count"], 1)
        self.assertIn("cascade", child.details["cleanup_blocked_reason"])

    def test_unproven_child_with_missing_parent_is_only_a_residual_edge(self) -> None:
        child_id = "ordinary-root-thread"
        rollout = write_rollout(
            self.codex_home,
            child_id,
            originator="Codex Desktop",
            source="app-server",
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": child_id,
                    "rollout_path": str(rollout),
                    "source": "vscode",
                    "thread_source": "user",
                }
            ],
            spawn_edges=[
                {
                    "parent_thread_id": "missing-parent",
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            ],
        )

        findings = self.adapter().scan()

        self.assertEqual(
            self.by_type(findings, "orphaned_subagent_thread"), []
        )
        residual = self.by_type(findings, "residual_spawn_edge")
        self.assertEqual(len(residual), 1)
        self.assertTrue(residual[0].details["cleanable"])
        self.assertFalse(residual[0].details["thread_delete_supported"])
        self.assertTrue(
            residual[0].details["direct_database_edit_supported"]
        )
        self.assertIsInstance(residual[0].details["relation_evidence"], dict)

    def test_detects_spawn_edge_whose_child_endpoint_is_gone(self) -> None:
        parent_id = "healthy-parent"
        parent_rollout = write_rollout(
            self.codex_home, parent_id, originator="Codex Desktop"
        )
        create_thread_index(
            self.codex_home,
            [{"id": parent_id, "rollout_path": str(parent_rollout)}],
            spawn_edges=[
                {
                    "parent_thread_id": parent_id,
                    "child_thread_id": "gone-child",
                    "status": "closed",
                }
            ],
        )

        residuals = self.by_type(
            self.adapter().scan(), "residual_spawn_edge"
        )

        self.assertEqual(len(residuals), 1)
        self.assertTrue(residuals[0].details["child_index_missing"])
        self.assertTrue(residuals[0].has_codex_artifacts)
        self.assertTrue(
            residuals[0].details["direct_database_edit_supported"]
        )

    def test_conflicting_source_parent_makes_edge_residual_not_orphan(self) -> None:
        first_parent = "edge-parent"
        source_parent = "metadata-parent"
        child_id = "conflicted-child"
        first_rollout = write_rollout(
            self.codex_home, first_parent, originator="Codex Desktop"
        )
        source_rollout = write_rollout(
            self.codex_home, source_parent, originator="Codex Desktop"
        )
        source = {
            "subagent": {
                "thread_spawn": {"parent_thread_id": source_parent, "depth": 1}
            }
        }
        child_rollout = write_rollout(
            self.codex_home,
            child_id,
            originator="Codex Desktop",
            source=source,
        )
        create_thread_index(
            self.codex_home,
            [
                {"id": first_parent, "rollout_path": str(first_rollout)},
                {"id": source_parent, "rollout_path": str(source_rollout)},
                {
                    "id": child_id,
                    "rollout_path": str(child_rollout),
                    "source": source,
                    "thread_source": "subagent",
                },
            ],
            spawn_edges=[
                {
                    "parent_thread_id": first_parent,
                    "child_thread_id": child_id,
                    "status": "closed",
                }
            ],
        )

        findings = self.adapter().scan()

        self.assertEqual(
            self.by_type(findings, "orphaned_subagent_thread"), []
        )
        residual = self.by_type(findings, "residual_spawn_edge")[0]
        self.assertTrue(residual.details["source_conflict"])
        self.assertEqual(
            residual.details["source_parent_ids"], [source_parent]
        )

    def test_legacy_index_only_entries_are_aggregated(self) -> None:
        live_id = "live-thread"
        live_rollout = write_rollout(
            self.codex_home, live_id, originator="Codex Desktop"
        )
        create_thread_index(
            self.codex_home,
            [{"id": live_id, "rollout_path": str(live_rollout)}],
        )
        legacy = self.codex_home / "session_index.jsonl"
        legacy.write_text(
            "\n".join(
                [
                    json.dumps({"id": live_id, "thread_name": "live"}),
                    json.dumps({"id": "legacy-gone", "thread_name": "gone"}),
                    json.dumps({"id": "legacy-gone", "thread_name": "duplicate"}),
                    "{malformed",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        findings = self.adapter().scan()
        legacy_findings = self.by_type(findings, "legacy_index_only")

        self.assertEqual(len(legacy_findings), 1)
        finding = legacy_findings[0]
        self.assertEqual(finding.thread_id, "legacy-session-index")
        self.assertEqual(finding.details["residual_thread_count"], 1)
        self.assertEqual(
            finding.details["residual_thread_ids_sample"], ["legacy-gone"]
        )
        self.assertEqual(finding.details["duplicate_entry_count"], 1)
        self.assertEqual(finding.details["malformed_line_count"], 1)
        self.assertFalse(finding.details["cleanable"])
        self.assertFalse(finding.details["thread_delete_supported"])
        # Relationship/legacy artifacts remain visible through the generic
        # scan filter without pretending that a threads row exists.
        report = scan_adapters([self.adapter()])
        self.assertEqual(len(report.findings), 1)
        self.assertTrue(report.findings[0].has_codex_artifacts)

    def test_missing_corrupt_or_incompatible_state_database_is_safe(self) -> None:
        write_rollout(
            self.codex_home, "valid-rollout", originator="Codex Desktop"
        )

        self.assertEqual(self.adapter().scan(), [])

        state_db = self.codex_home / "state_5.sqlite"
        state_db.write_text("not a sqlite database", encoding="utf-8")
        with self.assertRaises(NativeIntegrityError):
            self.adapter().scan()
        report = scan_adapters([self.adapter()])
        self.assertEqual(report.findings, [])
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].platform, "native")
        self.assertEqual(report.errors[0].error_type, "NativeIntegrityError")

        state_db.unlink()
        create_thread_index(
            self.codex_home,
            [],
            include_threads_table=False,
        )
        with self.assertRaises(NativeIntegrityError):
            self.adapter().scan()
        report = scan_adapters([self.adapter()])
        self.assertEqual(len(report.errors), 1)

    def test_scan_closes_readonly_database_connection(self) -> None:
        create_thread_index(self.codex_home, [])

        self.adapter().scan()

        # Windows cannot rename an open SQLite database.  This also guards the
        # less visible error paths in which the read-only connection leaked.
        state_db = self.codex_home / "state_5.sqlite"
        renamed = self.codex_home / "state_5-renamed.sqlite"
        state_db.rename(renamed)
        self.assertTrue(renamed.is_file())


if __name__ == "__main__":
    unittest.main()
