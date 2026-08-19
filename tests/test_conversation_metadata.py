from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path

from local_agent_record_janitor.conversation_metadata import (
    parse_thread_source,
    project_label_from_cwd,
    read_conversation_summaries,
    read_legacy_thread_names,
)
from local_agent_record_janitor.models import ConversationSummary, RolloutRecord


class ThreadSourceParsingTests(unittest.TestCase):
    def test_parses_decoded_and_json_encoded_spawn_sources(self) -> None:
        decoded = parse_thread_source(
            {
                "subagent": {
                    "agent_nickname": "Dirac",
                    "agent_role": "worker",
                    "thread_spawn": {
                        "parent_thread_id": "parent-id",
                        "agent_path": "/root/windows-notify",
                    },
                }
            },
            source_label="session_meta.source",
        )
        encoded = parse_thread_source(
            json.dumps(
                {
                    "thread_spawn": {
                        "parent_thread_id": "other-parent",
                    }
                }
            ),
            source_label="threads.source",
        )

        self.assertTrue(decoded.is_subagent)
        self.assertEqual(decoded.parent_thread_ids, ("parent-id",))
        self.assertEqual(decoded.agent_nickname, "Dirac")
        self.assertEqual(decoded.agent_role, "worker")
        self.assertEqual(decoded.agent_path, "/root/windows-notify")
        self.assertEqual(decoded.metadata_sources, ("session_meta.source",))
        self.assertTrue(encoded.is_subagent)
        self.assertEqual(encoded.parent_thread_ids, ("other-parent",))

    def test_plain_marker_and_malformed_json_are_handled_conservatively(self) -> None:
        self.assertTrue(parse_thread_source(" SubAgent ").is_subagent)
        self.assertFalse(parse_thread_source("{not-json").is_subagent)
        self.assertFalse(parse_thread_source({"app_server": {}}).is_subagent)


class ConversationSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()

    def _create_rich_state(self) -> None:
        with closing(
            sqlite3.connect(self.codex_home / "state_5.sqlite")
        ) as connection:
            connection.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT,
                    archived INTEGER,
                    source TEXT,
                    thread_source TEXT,
                    name TEXT,
                    title TEXT,
                    cwd TEXT,
                    git_origin_url TEXT,
                    agent_nickname TEXT,
                    agent_role TEXT,
                    agent_path TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, archived, source, thread_source,
                    name, title, cwd, git_origin_url, agent_nickname,
                    agent_role, agent_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "child-id",
                    "rollout.jsonl",
                    0,
                    json.dumps(
                        {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": "parent-id"
                                }
                            }
                        }
                    ),
                    "subagent",
                    "Custom task name",
                    "Generated title",
                    r"D:\GitRepo\VPS-Toolkit",
                    "https://github.com/example/VPS-Toolkit.git",
                    "Dirac",
                    "worker",
                    "/root/windows-notify",
                ),
            )
            connection.commit()

    def test_legacy_name_reader_filters_ids_and_softens_bad_lines(self) -> None:
        (self.codex_home / "session_index.jsonl").write_text(
            "\n".join(
                (
                    json.dumps({"id": "wanted", "thread_name": "Zulu"}),
                    "{malformed",
                    json.dumps({"id": "other", "thread_name": "Ignored"}),
                    json.dumps({"id": "wanted", "thread_name": "Alpha"}),
                    json.dumps({"id": "wanted", "thread_name": "Zulu"}),
                    json.dumps({"id": "wanted", "thread_name": ""}),
                )
            )
            + "\n",
            encoding="utf-8",
        )

        names = read_legacy_thread_names(
            self.codex_home,
            ["wanted", "missing"],
        )

        self.assertEqual(names, {"wanted": ("Alpha", "Zulu")})
        self.assertEqual(
            read_legacy_thread_names(self.root / "absent-home", ["wanted"]),
            {},
        )

    def test_merges_db_rollout_and_legacy_metadata_without_chat_body(self) -> None:
        self._create_rich_state()
        record = RolloutRecord(
            thread_id="child-id",
            path=self.codex_home / "sessions" / "child.jsonl",
            originator="Codex Desktop",
            source={
                "subagent": {
                    "thread_spawn": {"parent_thread_id": "parent-id"}
                }
            },
            cwd=r"D:\GitRepo\VPS-Toolkit",
            timestamp="2026-07-31T00:00:00Z",
            archived=False,
        )

        summary = read_conversation_summaries(
            self.codex_home,
            ["child-id"],
            rollout_records_by_thread={"child-id": [record]},
            legacy_names={"child-id": "Old index name"},
        )["child-id"]

        self.assertEqual(summary.name, "Custom task name")
        self.assertEqual(summary.title, "Generated title")
        self.assertEqual(summary.display_name, "Custom task name")
        self.assertEqual(summary.display_name_source, "threads.name")
        self.assertEqual(summary.project_label, "VPS-Toolkit")
        self.assertEqual(summary.cwd, r"D:\GitRepo\VPS-Toolkit")
        self.assertEqual(summary.agent_nickname, "Dirac")
        self.assertEqual(summary.agent_role, "worker")
        self.assertEqual(summary.agent_path, "/root/windows-notify")
        self.assertEqual(summary.parent_thread_ids, ("parent-id",))
        self.assertEqual(summary.originator, "Codex Desktop")
        self.assertTrue(summary.is_subagent)
        self.assertTrue(summary.indexed)
        self.assertFalse(summary.archived)
        self.assertIn("threads.name", summary.metadata_sources)
        self.assertIn("session_meta.source", summary.metadata_sources)
        self.assertNotIn("Old index name", summary.metadata_conflicts)

    def test_rollout_only_unknown_and_legacy_fallback_summaries_are_returned(self) -> None:
        record = RolloutRecord(
            thread_id="rollout-only",
            path=self.codex_home / "sessions" / "only.jsonl",
            originator="codex_cli_rs",
            source="app-server",
            cwd="/work/example-project",
            timestamp=None,
            archived=True,
        )

        summaries = read_conversation_summaries(
            self.codex_home,
            ["rollout-only", "legacy-only", "unknown"],
            rollout_records_by_thread={"rollout-only": record},
            legacy_names={"legacy-only": "Legacy title"},
        )

        self.assertEqual(set(summaries), {"rollout-only", "legacy-only", "unknown"})
        self.assertEqual(summaries["rollout-only"].project_label, "example-project")
        self.assertFalse(summaries["rollout-only"].indexed)
        self.assertTrue(summaries["rollout-only"].archived)
        self.assertEqual(summaries["legacy-only"].display_name, "Legacy title")
        self.assertEqual(
            summaries["legacy-only"].display_name_source,
            "session_index.thread_name",
        )
        self.assertIsNone(summaries["unknown"].display_name)
        self.assertFalse(summaries["unknown"].indexed)

    @unittest.skipUnless(os.name == "nt", "Windows extended path comparison")
    def test_windows_extended_cwd_prefix_is_not_a_metadata_conflict(self) -> None:
        self._create_rich_state()
        cwd = self.root / "GitRepo" / "VPS-Toolkit"
        cwd.mkdir(parents=True)
        with closing(
            sqlite3.connect(self.codex_home / "state_5.sqlite")
        ) as connection:
            connection.execute(
                "UPDATE threads SET cwd = ? WHERE id = 'child-id'",
                (str(cwd),),
            )
            connection.commit()
        record = RolloutRecord(
            thread_id="child-id",
            path=self.codex_home / "sessions" / "child.jsonl",
            originator="Codex Desktop",
            source={
                "subagent": {
                    "thread_spawn": {"parent_thread_id": "parent-id"}
                }
            },
            cwd="\\\\?\\" + str(cwd),
            timestamp="2026-08-18T00:00:00Z",
            archived=False,
        )

        summary = read_conversation_summaries(
            self.codex_home,
            ["child-id"],
            rollout_records_by_thread={"child-id": [record]},
        )["child-id"]

        self.assertEqual(summary.cwd, str(cwd))
        self.assertFalse(
            any(
                conflict.startswith("cwd ")
                for conflict in summary.metadata_conflicts
            ),
            summary.metadata_conflicts,
        )

    @unittest.skipUnless(os.name == "nt", "Windows extended path comparison")
    def test_equivalent_windows_cwds_without_database_are_order_stable(self) -> None:
        cwd = self.root / "Repo" / "Project"
        cwd.mkdir(parents=True)
        plain = RolloutRecord(
            thread_id="cwd-only",
            path=self.root / "plain.jsonl",
            originator=None,
            source=None,
            cwd=str(cwd),
            timestamp=None,
            archived=False,
        )
        extended = RolloutRecord(
            thread_id="cwd-only",
            path=self.root / "extended.jsonl",
            originator=None,
            source=None,
            cwd="\\\\?\\" + str(cwd).swapcase(),
            timestamp=None,
            archived=False,
        )

        forward = read_conversation_summaries(
            self.codex_home,
            ["cwd-only"],
            rollout_records_by_thread={"cwd-only": [plain, extended]},
        )["cwd-only"]
        reverse = read_conversation_summaries(
            self.codex_home,
            ["cwd-only"],
            rollout_records_by_thread={"cwd-only": [extended, plain]},
        )["cwd-only"]

        self.assertEqual(forward.cwd, str(cwd))
        self.assertEqual(forward.approval_payload(), reverse.approval_payload())
        self.assertFalse(forward.metadata_conflicts)

    @unittest.skipUnless(os.name == "nt", "Windows extended path comparison")
    def test_missing_extended_cwd_is_not_merged_without_identity_proof(self) -> None:
        plain = RolloutRecord(
            thread_id="missing-cwd",
            path=self.root / "plain.jsonl",
            originator=None,
            source=None,
            cwd=r"Z:\definitely-missing\Repo",
            timestamp=None,
            archived=False,
        )
        extended = RolloutRecord(
            thread_id="missing-cwd",
            path=self.root / "extended.jsonl",
            originator=None,
            source=None,
            cwd=r"\\?\Z:\definitely-missing\Repo",
            timestamp=None,
            archived=False,
        )

        summary = read_conversation_summaries(
            self.codex_home,
            ["missing-cwd"],
            rollout_records_by_thread={"missing-cwd": [plain, extended]},
        )["missing-cwd"]

        self.assertTrue(
            any(value.startswith("cwd ") for value in summary.metadata_conflicts),
            summary.metadata_conflicts,
        )

    def test_conflicts_are_explicit_and_fingerprint_is_order_stable(self) -> None:
        first = RolloutRecord(
            thread_id="conflict",
            path=self.root / "one.jsonl",
            originator="one",
            source={
                "subagent": {
                    "thread_spawn": {"parent_thread_id": "parent-one"}
                }
            },
            cwd="/one/project",
            timestamp=None,
            archived=False,
        )
        second = RolloutRecord(
            thread_id="conflict",
            path=self.root / "two.jsonl",
            originator="two",
            source={
                "subagent": {
                    "thread_spawn": {"parent_thread_id": "parent-two"}
                }
            },
            cwd="/two/project",
            timestamp=None,
            archived=True,
        )

        forward = read_conversation_summaries(
            self.codex_home,
            ["conflict"],
            rollout_records_by_thread={"conflict": [first, second]},
            legacy_names={"conflict": ["Zulu", "Alpha"]},
        )["conflict"]
        reverse = read_conversation_summaries(
            self.codex_home,
            ["conflict"],
            rollout_records_by_thread={"conflict": [second, first]},
            legacy_names={"conflict": ["Alpha", "Zulu"]},
        )["conflict"]

        self.assertEqual(
            forward.parent_thread_ids,
            ("parent-one", "parent-two"),
        )
        self.assertTrue(forward.metadata_conflicts)
        self.assertEqual(forward.approval_payload(), reverse.approval_payload())
        self.assertEqual(forward.metadata_fingerprint, reverse.metadata_fingerprint)
        self.assertRegex(forward.metadata_fingerprint, r"^v1:[0-9a-f]{64}$")

        changed = read_conversation_summaries(
            self.codex_home,
            ["conflict"],
            rollout_records_by_thread={
                "conflict": [
                    first,
                    RolloutRecord(
                        thread_id="conflict",
                        path=self.root / "three.jsonl",
                        originator="three",
                        source=second.source,
                        cwd="/three/project",
                        timestamp=None,
                        archived=True,
                    ),
                ]
            },
            legacy_names={"conflict": ["Alpha", "Different"]},
        )["conflict"]
        self.assertNotEqual(forward.metadata_fingerprint, changed.metadata_fingerprint)

    def test_summary_is_frozen_and_dict_contains_derived_label_and_fingerprint(self) -> None:
        summary = ConversationSummary(
            thread_id="immutable",
            cwd=r"C:\repo\project",
            project_label="project",
        )

        with self.assertRaises(FrozenInstanceError):
            summary.cwd = "changed"  # type: ignore[misc]
        serialized = summary.to_dict()
        self.assertEqual(serialized["project_label"], "project")
        self.assertEqual(serialized["metadata_fingerprint"], summary.metadata_fingerprint)

    def test_fingerprint_binds_raw_database_subagent_evidence(self) -> None:
        self._create_rich_state()
        record = RolloutRecord(
            thread_id="child-id",
            path=self.codex_home / "sessions" / "child.jsonl",
            originator="Codex Desktop",
            source={
                "subagent": {
                    "thread_spawn": {"parent_thread_id": "parent-id"}
                }
            },
            cwd=r"D:\GitRepo\VPS-Toolkit",
            timestamp="2026-07-31T00:00:00Z",
            archived=False,
        )
        before = read_conversation_summaries(
            self.codex_home,
            ["child-id"],
            rollout_records_by_thread={"child-id": [record]},
        )["child-id"]

        with closing(
            sqlite3.connect(self.codex_home / "state_5.sqlite")
        ) as connection:
            connection.execute(
                "UPDATE threads SET source = NULL, thread_source = NULL "
                "WHERE id = ?",
                ("child-id",),
            )
            connection.commit()

        after = read_conversation_summaries(
            self.codex_home,
            ["child-id"],
            rollout_records_by_thread={"child-id": [record]},
        )["child-id"]
        self.assertEqual(before.parent_thread_ids, after.parent_thread_ids)
        self.assertEqual(before.is_subagent, after.is_subagent)
        self.assertNotEqual(
            before.metadata_evidence_fingerprints,
            after.metadata_evidence_fingerprints,
        )
        self.assertNotEqual(
            before.metadata_fingerprint,
            after.metadata_fingerprint,
        )

    def test_project_label_handles_windows_posix_and_roots(self) -> None:
        self.assertEqual(project_label_from_cwd("D:\\repo\\project\\"), "project")
        self.assertEqual(project_label_from_cwd("/repo/project/"), "project")
        self.assertIsNone(project_label_from_cwd("/"))
        self.assertIsNone(project_label_from_cwd("C:\\"))


if __name__ == "__main__":
    unittest.main()
