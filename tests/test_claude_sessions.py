from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_session_janitor.claude_sessions import (
    _MAX_JSONL_LINE,
    ClaudeSelectionError,
    build_claude_multi_root_catalog,
    build_claude_session_catalog,
    resolve_claude_paths,
    select_claude_sessions,
)


ONE = "11111111-1111-4111-8111-111111111111"
TWO = "22222222-2222-4222-8222-222222222222"


class ClaudeSessionInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.config = self.home / ".claude"

    def write_transcript(self, session_id: str, project: str = "project", *, body: str = "secret body") -> Path:
        path = self.config / "projects" / project / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sessionId": session_id, "type": "user", "message": body}) + "\n", encoding="utf-8")
        return path

    def reference(self, session_id: str, *, kind: str = "current", status: str = "active", root: Path | None = None):
        values = dict(backend="claude", sdk_session_id=session_id, reference_kind=kind,
                      session_status=status, cindy_session_id="front-1")
        if root is not None:
            values["config_dir"] = root
        return SimpleNamespace(**values)

    def test_path_precedence(self) -> None:
        explicit = resolve_claude_paths(environ={"CLAUDE_CONFIG_DIR": str(self.root / "env")}, home=self.home,
                                        config_dir=self.root / "explicit")
        environment = resolve_claude_paths(environ={"CLAUDE_CONFIG_DIR": str(self.root / "env")}, home=self.home)
        default = resolve_claude_paths(environ={}, home=self.home)
        self.assertEqual((explicit.config_dir, explicit.config_dir_source), (self.root / "explicit", "argument"))
        self.assertEqual((environment.config_dir, environment.config_dir_source), (self.root / "env", "environment"))
        self.assertEqual((default.config_dir, default.config_dir_source), (self.config, "default"))

    def test_metadata_only_inventory_merges_project_copies_and_auxiliary_manifest(self) -> None:
        one = self.write_transcript(ONE, "-a-", body="do not expose this")
        two = self.write_transcript(ONE, "-b-")
        sidecar = one.parent / ONE
        (sidecar / "subagents").mkdir(parents=True)
        (sidecar / "subagents" / "agent.jsonl").write_text("tool secret", encoding="utf-8")
        (sidecar / "tool-results").mkdir()
        (sidecar / "tool-results" / "result.txt").write_text("result secret", encoding="utf-8")
        for name in ("file-history", "tasks", "debug", "session-env", "todos"):
            target = self.config / name / ONE
            target.mkdir(parents=True)
            (target / "owned.txt").write_text(name, encoding="utf-8")
        current_debug = self.config / "debug" / f"{ONE}.txt"
        current_debug.write_text("current debug", encoding="utf-8")
        current_todo = self.config / "todos" / f"{ONE}-agent-main_1.json"
        current_todo.write_text("current todo", encoding="utf-8")
        (self.config / "todos" / f"{ONE}-agent-.json").write_text(
            "empty token shared", encoding="utf-8"
        )
        (self.config / "todos" / f"{ONE}-agent-bad!.json").write_text(
            "unsafe token shared", encoding="utf-8"
        )
        (self.config / "debug" / f"{ONE}.txt.bak").write_text(
            "similar prefix shared", encoding="utf-8"
        )

        catalog = build_claude_session_catalog(environ={}, home=self.home)

        self.assertEqual(catalog.errors, ())
        self.assertEqual(len(catalog.records), 1)
        record = catalog.records[0]
        self.assertEqual(record.transcript_paths, (one, two))
        relative = {item.relative_path for item in record.manifest}
        self.assertIn(f"projects/-a-/{ONE}/subagents/agent.jsonl", relative)
        self.assertIn(f"projects/-a-/{ONE}/tool-results/result.txt", relative)
        self.assertIn(f"tasks/{ONE}/owned.txt", relative)
        self.assertIn(f"debug/{ONE}.txt", relative)
        self.assertIn(f"todos/{ONE}-agent-main_1.json", relative)
        self.assertNotIn(f"todos/{ONE}-agent-.json", relative)
        self.assertNotIn(f"todos/{ONE}-agent-bad!.json", relative)
        self.assertNotIn(f"debug/{ONE}.txt.bak", relative)
        rendered = json.dumps(catalog.to_dict())
        self.assertNotIn("do not expose this", rendered)
        self.assertNotIn("result secret", rendered)

    def test_live_deleted_unreferenced_and_frontend_only_classification(self) -> None:
        self.write_transcript(ONE)
        self.write_transcript(TWO)
        refs = (
            self.reference(ONE, root=self.config),
            self.reference(TWO, status="deleted", root=self.config),
            self.reference("33333333-3333-4333-8333-333333333333", root=self.config),
        )
        catalog = build_claude_session_catalog(config_dir=self.config, frontend_references=refs)
        by_id = {item.session_id: item for item in catalog.records}
        self.assertEqual(by_id[ONE].classification, "live_current_reference")
        self.assertFalse(by_id[ONE].deletable)
        self.assertEqual(by_id[TWO].classification, "deleted_frontend_reference")
        self.assertTrue(by_id[TWO].deletable)
        missing = by_id["33333333-3333-4333-8333-333333333333"]
        self.assertEqual(missing.classification, "frontend_only")
        self.assertEqual(missing.manifest, ())
        self.assertFalse(missing.deletable)

    def test_historical_reference_blocks_and_no_reference_is_unreferenced(self) -> None:
        self.write_transcript(ONE)
        historical = build_claude_session_catalog(
            config_dir=self.config,
            frontend_references=(self.reference(ONE, kind="agent_switch", root=self.config),),
        )
        self.assertEqual(historical.records[0].classification, "live_historical_reference")
        self.assertFalse(historical.records[0].deletable)
        plain = build_claude_session_catalog(config_dir=self.config)
        self.assertEqual(plain.records[0].classification, "unreferenced")
        self.assertTrue(plain.records[0].deletable)

    def test_rootless_reference_is_visible_but_ambiguous_across_roots(self) -> None:
        second = self.root / "other-claude"
        self.write_transcript(ONE)
        path = second / "projects" / "other" / f"{ONE}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"sessionId": ONE}) + "\n", encoding="utf-8")
        aggregate = build_claude_multi_root_catalog(
            (self.config, second), frontend_references=(self.reference(ONE),),
        )
        self.assertEqual(len(aggregate.records), 2)
        self.assertEqual(len({item.action_id for item in aggregate.records}), 2)
        self.assertTrue(all(item.classification == "inventory_incomplete" for item in aggregate.records))
        self.assertTrue(all(item.frontend_reference_snapshot[0]["root_ambiguous"] for item in aggregate.records))
        self.assertTrue(all(not item.deletable for item in aggregate.records))

    def test_malformed_transcript_blocks_otherwise_good_inventory(self) -> None:
        self.write_transcript(ONE)
        bad = self.config / "projects" / "bad" / f"{TWO}.jsonl"
        bad.parent.mkdir(parents=True)
        bad.write_text("{private invalid payload\n", encoding="utf-8")
        catalog = build_claude_session_catalog(config_dir=self.config)
        self.assertTrue(catalog.errors)
        self.assertFalse(catalog.records[0].deletable)
        self.assertEqual(catalog.records[0].classification, "inventory_incomplete")
        self.assertNotIn("private invalid payload", json.dumps(catalog.to_dict()))

    def test_transcript_line_limit_is_bounded_for_eof_and_newline(self) -> None:
        path = self.config / "projects" / "bounded" / f"{ONE}.jsonl"
        path.parent.mkdir(parents=True)
        prefix = (f'{{"sessionId":"{ONE}","message":"').encode("utf-8")
        suffix = b'"}'

        exact_eof = prefix + b"x" * (
            _MAX_JSONL_LINE - len(prefix) - len(suffix)
        ) + suffix
        self.assertEqual(len(exact_eof), _MAX_JSONL_LINE)
        path.write_bytes(exact_eof)
        eof_catalog = build_claude_session_catalog(config_dir=self.config)
        self.assertEqual(eof_catalog.errors, ())
        self.assertEqual(eof_catalog.records[0].transcript_line_count, 1)

        exact_newline = prefix + b"x" * (
            _MAX_JSONL_LINE - 1 - len(prefix) - len(suffix)
        ) + suffix + b"\n"
        self.assertEqual(len(exact_newline), _MAX_JSONL_LINE)
        path.write_bytes(exact_newline)
        newline_catalog = build_claude_session_catalog(config_dir=self.config)
        self.assertEqual(newline_catalog.errors, ())
        self.assertEqual(newline_catalog.records[0].transcript_line_count, 1)

        over_limit_without_newline = prefix + b"private-over-limit-" + b"x" * (
            _MAX_JSONL_LINE
        )
        path.write_bytes(over_limit_without_newline)
        rejected = build_claude_session_catalog(config_dir=self.config)
        self.assertEqual(rejected.records, ())
        self.assertTrue(rejected.errors)
        self.assertIn("over-sized", rejected.errors[0].message)
        self.assertNotIn("private-over-limit", json.dumps(rejected.to_dict()))

    def test_selection_rejects_all_ambiguity_and_repeated_selector(self) -> None:
        self.write_transcript(ONE)
        self.write_transcript(TWO)
        catalog = build_claude_session_catalog(config_dir=self.config)
        with self.assertRaises(ClaudeSelectionError):
            select_claude_sessions(catalog, ("all",))
        with self.assertRaises(ClaudeSelectionError):
            select_claude_sessions(catalog, ("",))
        with self.assertRaises(ClaudeSelectionError):
            select_claude_sessions(catalog, (ONE, ONE))
        self.assertEqual(select_claude_sessions(catalog, (ONE,))[0].session_id, ONE)

    @unittest.skipUnless(hasattr(os, "symlink"), "platform lacks symlink support")
    def test_parent_alias_uses_physical_root_for_reference_and_action_identity(self) -> None:
        physical = self.root / "physical" / ".claude"
        transcript = physical / "projects" / "project" / f"{ONE}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"sessionId": ONE}) + "\n", encoding="utf-8")
        alias_parent = self.root / "alias-parent"
        try:
            os.symlink(physical.parent, alias_parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        alias_root = alias_parent / ".claude"
        reference = self.reference(ONE, root=physical)

        through_alias = build_claude_session_catalog(
            config_dir=alias_root, frontend_references=(reference,)
        )
        direct = build_claude_session_catalog(
            config_dir=physical, frontend_references=(reference,)
        )

        self.assertEqual(through_alias.errors, ())
        self.assertEqual(through_alias.config_dir, physical.resolve(strict=True))
        self.assertEqual(
            through_alias.records[0].classification,
            "live_current_reference",
        )
        self.assertEqual(
            through_alias.records[0].action_id, direct.records[0].action_id
        )
        self.assertEqual(
            through_alias.records[0].approval_payload()["config_dir"],
            direct.records[0].approval_payload()["config_dir"],
        )

        aggregate = build_claude_multi_root_catalog(
            (alias_root, physical), frontend_references=(reference,)
        )
        self.assertEqual(len(aggregate.records), 1)
        self.assertTrue(aggregate.root_errors)

    @unittest.skipUnless(hasattr(os, "symlink"), "platform lacks symlink support")
    def test_dangling_parent_alias_is_a_blocking_canonicalization_failure(self) -> None:
        alias_parent = self.root / "dangling-parent"
        try:
            os.symlink(
                self.root / "missing-target",
                alias_parent,
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

        catalog = build_claude_session_catalog(
            config_dir=alias_parent / ".claude"
        )
        self.assertEqual(catalog.records, ())
        self.assertTrue(catalog.errors)
        self.assertTrue(catalog.errors[0].blocks_delete)

    @unittest.skipUnless(hasattr(os, "symlink"), "platform lacks symlink support")
    def test_alias_qualified_reference_failure_blocks_physical_root_only(self) -> None:
        self.write_transcript(ONE)
        other = self.root / "other-claude"
        other_path = other / "projects" / "project" / f"{TWO}.jsonl"
        other_path.parent.mkdir(parents=True)
        other_path.write_text(json.dumps({"sessionId": TWO}) + "\n", encoding="utf-8")
        alias_parent = self.root / "failure-alias-parent"
        try:
            os.symlink(self.config.parent, alias_parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        failure = SimpleNamespace(
            config_dir=alias_parent / ".claude",
            message="reference database unreadable",
            blocks_delete=True,
        )

        aggregate = build_claude_multi_root_catalog(
            (self.config, other), reference_errors=(failure,)
        )
        by_root = {record.config_dir: record for record in aggregate.records}
        physical = self.config.resolve(strict=True)
        self.assertEqual(by_root[physical].classification, "inventory_incomplete")
        self.assertFalse(by_root[physical].deletable)
        self.assertEqual(by_root[other].classification, "unreferenced")
        self.assertTrue(by_root[other].deletable)

    @unittest.skipUnless(hasattr(os, "symlink"), "platform lacks symlink support")
    def test_manifest_symlink_is_rejected(self) -> None:
        transcript = self.write_transcript(ONE)
        sidecar = transcript.parent / ONE
        sidecar.mkdir()
        outside = self.root / "outside"
        outside.write_text("outside", encoding="utf-8")
        try:
            os.symlink(outside, sidecar / "linked")
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        catalog = build_claude_session_catalog(config_dir=self.config)
        self.assertTrue(catalog.errors)
        self.assertEqual(catalog.records, ())

    def test_current_auxiliary_patterns_reject_non_file_nodes(self) -> None:
        self.write_transcript(ONE)
        self.write_transcript(TWO)
        (self.config / "debug" / f"{ONE}.txt").mkdir(parents=True)
        (self.config / "todos" / f"{TWO}-agent-main.json").mkdir(
            parents=True
        )
        catalog = build_claude_session_catalog(config_dir=self.config)
        self.assertEqual(catalog.records, ())
        self.assertGreaterEqual(len(catalog.errors), 2)
        self.assertTrue(all(
            "unknown node type" in error.message for error in catalog.errors
        ))

    @unittest.skipUnless(hasattr(os, "symlink"), "platform lacks symlink support")
    def test_current_auxiliary_pattern_rejects_link(self) -> None:
        self.write_transcript(ONE)
        debug = self.config / "debug"
        debug.mkdir()
        outside = self.root / "outside-debug.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            os.symlink(outside, debug / f"{ONE}.txt")
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        catalog = build_claude_session_catalog(config_dir=self.config)
        self.assertEqual(catalog.records, ())
        self.assertTrue(catalog.errors)
        self.assertIn("symlink or reparse", catalog.errors[0].message)


if __name__ == "__main__":
    unittest.main()
