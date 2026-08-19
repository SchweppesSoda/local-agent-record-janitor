from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.adapters.native import NativeIntegrityAdapter
from local_agent_record_janitor.cleaner import (
    CleanupReport,
    CleanupResult,
    ExpectedDeletionScope,
    finding_key,
    scan_adapters,
)
from local_agent_record_janitor.cli import (
    ActionSelectionError,
    EXIT_CONFIRMATION_REQUIRED,
    EXIT_ERROR,
    EXIT_GOAL_NOT_SATISFIED,
    EXIT_OK,
    NumberSelectionError,
    build_parser,
    create_default_adapters,
    main,
    parse_action_selection,
    parse_number_selection,
    select_candidate_actions,
    _human_message,
    _human_unavailable_reason,
    _conflicting_action_decision,
    _emit_planned_cleanup_result,
    _integrity_delete_approvals,
    _problem_label,
    _write_action_catalog,
    _write_selected_action_plan,
)
from local_agent_record_janitor.models import ConversationSummary, Finding
from local_agent_record_janitor.planning import build_cleanup_plan
from tests.support import create_cindy_database, create_thread_index, write_rollout


class NumberSelectionTests(unittest.TestCase):
    def test_parses_numbers_ranges_and_removes_duplicates(self) -> None:
        self.assertEqual(
            parse_number_selection("5, 1,3-5,3", item_count=5),
            (1, 3, 4, 5),
        )

    def test_rejects_empty_invalid_reversed_and_out_of_range_values(self) -> None:
        for value in ("", "1,,2", "x", "1-x", "3-1", "0", "4"):
            with self.subTest(value=value):
                with self.assertRaises(NumberSelectionError):
                    parse_number_selection(value, item_count=3)

    def test_zero_items_have_no_valid_number(self) -> None:
        with self.assertRaises(NumberSelectionError):
            parse_number_selection("1", item_count=0)

    def test_codex_location_arguments_are_public_and_explained(self) -> None:
        parser = build_parser()
        root_help = parser.format_help()
        self.assertEqual(parser.prog, "local-agent-record-janitor")
        self.assertIn("local-agent-record-janitor", root_help)
        self.assertIn("检查前端残留", root_help)
        self.assertIn("用法：", root_help)
        self.assertIn("命令", root_help)
        self.assertIn("选项", root_help)
        self.assertIn("显示此帮助信息并退出", root_help)
        command_action = next(
            action for action in parser._actions
            if action.dest == "command"
        )
        scan_help = command_action.choices["scan"].format_help()
        self.assertIn("只读检查前端残留", scan_help)
        self.assertIn("用法：", scan_help)
        self.assertIn("选项", scan_help)
        self.assertIn("--codex-home PATH", scan_help)
        self.assertIn("显式扫描一个 Codex 数据目录", scan_help)
        self.assertIn("--codex-bin PATH", scan_help)
        self.assertIn("匹配的 Codex 可执行文件", scan_help)
        self.assertIn("输出完整的机器可读 JSON", scan_help)
        clean_help = command_action.choices["clean"].format_help()
        self.assertIn("跳过 TTY 最终确认提示", clean_help)
        self.assertIn("完整稳定 action ID", clean_help)
        purge_help = command_action.choices["purge"].format_help()
        self.assertIn("原生 Codex、Cindy 和 AionUI", purge_help)
        self.assertIn("--clients-closed", purge_help)

    def test_module_help_uses_utf8_bytes_on_windows_redirects(self) -> None:
        environment = os.environ.copy()
        source_root = Path(__file__).resolve().parents[1] / "src"
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(source_root), existing) if value
        )
        environment["PYTHONIOENCODING"] = (
            "gbk" if os.name == "nt" else "utf-8"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "local_agent_record_janitor",
                "--help",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        rendered = completed.stdout.decode("utf-8", errors="strict")
        self.assertIn("用法：", rendered)
        self.assertIn("检查前端残留", rendered)

    def test_invalid_explicit_codex_binary_is_rejected_before_scan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            invalid_paths = (
                Path(root) / "missing-codex.cmd",
                Path(root),
            )
            for path in invalid_paths:
                for command in ("scan", "clean"):
                    with self.subTest(path=path, command=command):
                        errors = StringIO()
                        with (
                            patch(
                                "local_agent_record_janitor.cli.scan_adapters"
                            ) as scan,
                            redirect_stderr(errors),
                            self.assertRaises(SystemExit) as raised,
                        ):
                            main(
                                (
                                    command,
                                    "--codex-bin",
                                    str(path),
                                ),
                                adapters=(),
                                stdin=StringIO(),
                                stdout=StringIO(),
                                stderr=StringIO(),
                            )
                        self.assertEqual(raised.exception.code, 2)
                        self.assertIn(
                            "Codex 可执行文件必须是现存普通文件",
                            errors.getvalue(),
                        )
                        scan.assert_not_called()

    def test_existing_cmd_and_bat_codex_binary_files_are_accepted(
        self,
    ) -> None:
        parser = build_parser()
        with tempfile.TemporaryDirectory() as root:
            for suffix in (".cmd", ".bat"):
                path = Path(root) / f"codex{suffix}"
                path.write_text("@echo off\n", encoding="utf-8")
                for command in ("scan", "clean"):
                    with self.subTest(
                        suffix=suffix,
                        command=command,
                    ):
                        args = parser.parse_args(
                            (
                                command,
                                "--codex-bin",
                                str(path),
                            )
                        )
                        self.assertEqual(args.codex_bin, path)


class ActionSelectionTests(unittest.TestCase):
    def test_all_selects_only_low_risk_actions(self) -> None:
        self.assertEqual(
            parse_action_selection(
                "ALL",
                risks=("low", "review", "LOW", "high", "blocked"),
            ),
            (1, 3),
        )

    def test_all_excludes_low_action_requiring_individual_selection(self) -> None:
        self.assertEqual(
            parse_action_selection(
                "all",
                risks=("low", "low", "review"),
                requires_explicit_selection=(False, True, False),
            ),
            (1,),
        )

    def test_explicit_number_range_can_include_non_low_actions(self) -> None:
        self.assertEqual(
            parse_action_selection(
                "1-3",
                risks=("low", "review", "high"),
            ),
            (1, 2, 3),
        )

    def test_non_low_action_can_be_selected_individually(self) -> None:
        self.assertEqual(
            parse_action_selection("2", risks=("low", "review", "high")),
            (2,),
        )

    def test_individual_number_can_select_action_requiring_it(self) -> None:
        self.assertEqual(
            parse_action_selection(
                "2",
                risks=("low", "low"),
                requires_explicit_selection=(False, True),
            ),
            (2,),
        )


def _action(
    action_id: str,
    *,
    storage_id: str,
    thread_id: str,
    kind: str = "delete_conversation",
) -> SimpleNamespace:
    return SimpleNamespace(
        action_id=action_id,
        kind=kind,
        target=SimpleNamespace(
            storage_id=storage_id,
            thread_id=thread_id,
        ),
    )


class CandidateActionSelectionTests(unittest.TestCase):
    def test_complete_action_id_selects_one_storage(self) -> None:
        first = _action("delete:one:a", storage_id="one", thread_id="abc")
        second = _action("delete:two:a", storage_id="two", thread_id="abc")
        self.assertEqual(
            select_candidate_actions(
                (first, second),
                action_ids=("delete:one:a",),
            ),
            [first],
        )

    def test_same_conversation_id_in_two_storages_is_ambiguous(self) -> None:
        actions = (
            _action("delete:one:a", storage_id="one", thread_id="abc"),
            _action("delete:two:a", storage_id="two", thread_id="abc"),
        )
        with self.assertRaisesRegex(
            ActionSelectionError,
            "多个保存位置",
        ):
            select_candidate_actions(actions, thread_selectors=("abc",))

    def test_unique_prefix_selects_delete_action_not_keep_action(self) -> None:
        delete = _action(
            "delete:one:a",
            storage_id="one",
            thread_id="abcdef",
        )
        keep = _action(
            "keep:one:a",
            storage_id="one",
            thread_id="abcdef",
            kind="keep",
        )
        self.assertEqual(
            select_candidate_actions(
                (delete, keep),
                thread_selectors=("abcd",),
            ),
            [delete],
        )

    def test_action_and_thread_selectors_cannot_be_combined(self) -> None:
        with self.assertRaisesRegex(
            ActionSelectionError,
            "不能同时使用",
        ):
            select_candidate_actions(
                (),
                action_ids=("action",),
                thread_selectors=("thread",),
            )

    def test_keep_and_delete_for_same_target_conflict(self) -> None:
        delete = _action(
            "delete",
            storage_id="one",
            thread_id="conversation",
        )
        keep = _action(
            "keep",
            storage_id="one",
            thread_id="conversation",
            kind="keep",
        )
        error = _conflicting_action_decision((delete, keep))
        self.assertIsNotNone(error)
        self.assertEqual(error.kind, "conflicting_actions")

    def test_keep_for_different_target_does_not_conflict(self) -> None:
        actions = (
            _action(
                "delete",
                storage_id="one",
                thread_id="conversation-one",
            ),
            _action(
                "keep",
                storage_id="one",
                thread_id="conversation-two",
                kind="keep",
            ),
        )
        self.assertIsNone(_conflicting_action_decision(actions))

    def test_high_integrity_approval_is_narrow_and_target_scoped(self) -> None:
        plan = _display_plan()
        action = plan.actions[0]
        action.risk = "high"
        action.observation_ids = (
            "matching-duplicate",
            "matching-other",
            "wrong-target",
        )
        action_target = action.target
        plan.observations = (
            SimpleNamespace(
                observation_id="matching-duplicate",
                target=action_target,
                finding_type="duplicate_rollout",
            ),
            SimpleNamespace(
                observation_id="matching-other",
                target=action_target,
                finding_type="orphaned_subagent_thread",
            ),
            SimpleNamespace(
                observation_id="wrong-target",
                target=SimpleNamespace(
                    storage_id=action_target.storage_id,
                    thread_id="another-conversation",
                ),
                finding_type="index_rollout_path_mismatch",
            ),
        )
        finding = Finding(
            platform="native",
            platform_session_id="session",
            thread_id=action_target.thread_id,
            reason="test",
            platform_db=Path("state_5.sqlite"),
            codex_home=Path("codex-home"),
        )
        approvals = _integrity_delete_approvals(
            (finding,),
            (action,),
            plan,
        )
        self.assertEqual(
            approvals,
            {finding_key(finding): frozenset({"duplicate_rollout"})},
        )

    def test_residual_approval_requires_exact_artifact_and_safe_source(
        self,
    ) -> None:
        plan = _display_plan()
        action = plan.actions[0]
        action.risk = "high"
        action.impact.rollout_file_count = 1
        action.observation_ids = ("safe",)
        action_target = action.target
        plan.observations = (
            SimpleNamespace(
                observation_id="safe",
                target=action_target,
                platform="native",
                finding_type="residual_spawn_edge",
                details={
                    "parent_thread_id": "parent",
                    "child_thread_id": action_target.thread_id,
                    "edge_status": "closed",
                    "parent_index_missing": False,
                    "child_index_missing": False,
                    "parent_rollout_present": True,
                    "child_rollout_present": True,
                    "source_parent_ids": ["parent"],
                    "subagent_evidence": ["session_meta.source"],
                    "source_conflict": False,
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "thread/delete does not expose a standalone "
                        "spawn-edge cleanup operation."
                    ),
                    "cleanup_blocker_codes": [
                        "standalone_relation_cleanup_unavailable"
                    ],
                    "direct_database_edit_supported": False,
                },
            ),
            SimpleNamespace(
                observation_id="conflicted",
                target=action_target,
                platform="native",
                finding_type="residual_spawn_edge",
                details={
                    "parent_thread_id": "parent",
                    "child_thread_id": action_target.thread_id,
                    "edge_status": "closed",
                    "parent_index_missing": False,
                    "child_index_missing": False,
                    "parent_rollout_present": True,
                    "child_rollout_present": True,
                    "source_parent_ids": ["parent"],
                    "subagent_evidence": ["session_meta.source"],
                    "source_conflict": True,
                    "thread_delete_supported": False,
                    "cleanable": False,
                    "cleanup_blocked_reason": (
                        "thread/delete does not expose a standalone "
                        "spawn-edge cleanup operation."
                    ),
                    "cleanup_blocker_codes": [
                        "standalone_relation_cleanup_unavailable"
                    ],
                    "direct_database_edit_supported": False,
                },
            ),
        )
        finding = Finding(
            platform="native",
            platform_session_id="session",
            thread_id=action_target.thread_id,
            reason="test",
            platform_db=Path("state_5.sqlite"),
            codex_home=Path("codex-home"),
        )

        approvals = _integrity_delete_approvals(
            (finding,),
            (action,),
            plan,
        )
        self.assertEqual(approvals, {})

        positive_plan = _display_plan()
        positive_action = positive_plan.actions[0]
        positive_action.risk = "high"
        positive_action.impact.rollout_file_count = 1
        positive_action.observation_ids = ("only-safe",)
        positive_plan.observations = (
            SimpleNamespace(
                observation_id="only-safe",
                target=positive_action.target,
                platform="native",
                finding_type="residual_spawn_edge",
                details=dict(plan.observations[0].details),
            ),
        )
        self.assertEqual(
            _integrity_delete_approvals(
                (finding,),
                (positive_action,),
                positive_plan,
            ),
            {
                finding_key(finding): frozenset(
                    {"residual_spawn_edge"}
                )
            },
        )

        positive_action.impact.index_record_count = 0
        positive_action.impact.rollout_file_count = 0
        self.assertEqual(
            _integrity_delete_approvals(
                (finding,),
                (positive_action,),
                positive_plan,
            ),
            {},
        )

        positive_action.impact.index_record_count = 1
        del positive_plan.observations[0].details["subagent_evidence"]
        self.assertEqual(
            _integrity_delete_approvals(
                (finding,),
                (positive_action,),
                positive_plan,
            ),
            {},
        )

    def test_real_native_residual_contract_gets_target_scoped_approval(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            codex_home = Path(root) / "codex-home"
            parent_id = "existing-parent"
            child_id = "rollout-only-child"
            write_rollout(
                codex_home,
                child_id,
                originator="test-owner",
                source="app-server",
            )
            create_thread_index(
                codex_home,
                [],
                spawn_edges=[
                    {
                        "parent_thread_id": parent_id,
                        "child_thread_id": child_id,
                        "status": "closed",
                    }
                ],
            )
            findings = NativeIntegrityAdapter(
                codex_home=codex_home
            ).scan()
            plan = build_cleanup_plan(findings)

        action = next(
            item
            for item in plan.actions
            if (
                str(item.target.thread_id) == child_id
                and str(getattr(item.kind, "value", item.kind))
                == "delete_conversation"
            )
        )
        finding = next(
            item for item in findings if item.thread_id == child_id
        )
        self.assertTrue(action.available)
        self.assertEqual(str(action.risk.value), "high")
        self.assertEqual(
            _integrity_delete_approvals((finding,), (action,), plan),
            {
                finding_key(finding): frozenset(
                    {"residual_spawn_edge"}
                )
            },
        )


def _display_plan() -> SimpleNamespace:
    storage = SimpleNamespace(
        storage_id="storage-one",
        label="Codex 默认数据目录",
        path="C:/Codex",
    )
    observation = SimpleNamespace(
        observation_id="observation-one",
        finding_type="index_missing_rollout",
        reason="Codex thread index remains but its rollout is missing",
    )
    impact = SimpleNamespace(
        index_record_count=1,
        rollout_file_count=0,
        descendant_thread_ids=("child-full-id",),
        frontend_references_preserved=True,
        indexed_thread_ids=("conversation-full-id",),
        rollout_paths=(),
        rollout_state_fingerprints=(),
    )
    action = _action(
        "delete:storage-one:conversation",
        storage_id=storage.storage_id,
        thread_id="conversation-full-id",
    )
    action.risk = "low"
    action.available = True
    action.unavailable_reason = None
    action.impact = impact
    action.observation_ids = (observation.observation_id,)
    action.snapshot_fingerprint = "snapshot-one"
    action.to_dict = lambda: {
        "action_id": action.action_id,
        "kind": action.kind,
        "target": {
            "storage_id": action.target.storage_id,
            "thread_id": action.target.thread_id,
        },
        "risk": action.risk,
    }
    root_summary = ConversationSummary(
        thread_id="conversation-full-id",
        display_name="Root conversation",
        display_name_source="threads.title",
        cwd="C:/Projects/root-project",
        project_label="root-project",
        indexed=True,
        archived=False,
        originator="Codex Desktop",
        metadata_sources=("threads.title", "session_meta"),
    )
    child_summary = ConversationSummary(
        thread_id="child-full-id",
        display_name="Review child task",
        display_name_source="threads.name",
        cwd="C:/Projects/child-project",
        project_label="child-project",
        is_subagent=True,
        agent_nickname="Socrates",
        agent_role="reviewer",
        agent_path="/root/review_child",
        parent_thread_ids=("conversation-full-id",),
        indexed=False,
        archived=False,
        originator="codex_cli_rs",
        metadata_sources=("session_meta.source",),
    )
    plan = SimpleNamespace(
        storages=(storage,),
        conversations=(
            SimpleNamespace(target=action.target, summary=root_summary),
            SimpleNamespace(
                target=SimpleNamespace(
                    storage_id=storage.storage_id,
                    thread_id="child-full-id",
                ),
                summary=child_summary,
            ),
        ),
        observations=(observation,),
        actions=(action,),
    )
    plan.to_dict = lambda: {
        "storages": [
            {
                "storage_id": storage.storage_id,
                "label": storage.label,
                "path": storage.path,
            }
        ],
        "observations": [],
        "actions": [action.to_dict()],
        "errors": [],
    }
    return plan


class ActionOutputTests(unittest.TestCase):
    def test_structured_problem_types_have_stable_chinese_labels(self) -> None:
        expected = {
            "index_missing_rollout": "对话列表记录存在，但内容文件缺失",
            "rollout_missing_index": "对话内容文件存在，但列表记录缺失",
            "duplicate_rollout": "同一对话 ID 对应多个内容文件",
            "index_rollout_path_mismatch": (
                "列表记录指向的内容文件路径与实际位置不一致"
            ),
            "index_rollout_metadata_mismatch": (
                "列表记录与内容文件中的对话 ID 不一致"
            ),
            "orphaned_subagent_thread": "关联任务对话的父对话已不存在",
            "residual_spawn_edge": "对话关联记录指向不存在的对话",
            "legacy_index_only": "旧版对话列表记录缺少内容文件",
            "frontend_deleted_reference": (
                "前端对话已删除，但 Codex 对话引用仍存在"
            ),
        }
        for finding_type, label in expected.items():
            with self.subTest(finding_type=finding_type):
                rendered = _problem_label(
                    finding_type,
                    "untranslated English reason",
                )
                self.assertEqual(rendered, label)
                self.assertNotIn("English reason", rendered)

    def test_dynamic_human_messages_normalize_relationship_wording(self) -> None:
        rendered = _human_message(
            "thread/delete cascades to spawned descendant threads, "
            "spawned descendants and descendant threads"
        )
        self.assertNotIn("descendant", rendered.lower())
        self.assertNotIn("threads", rendered.lower())
        self.assertEqual(rendered.count("关联任务对话"), 3)

    def test_catalog_is_grouped_and_shows_problem_action_and_impact(self) -> None:
        output = StringIO()
        _write_action_catalog(_display_plan(), stdout=output, limit=0)
        rendered = output.getvalue()
        self.assertIn("保存位置：Codex 默认数据目录", rendered)
        self.assertIn("低风险", rendered)
        self.assertIn("候选动作：永久删除整条对话", rendered)
        self.assertIn("问题：对话列表记录存在", rendered)
        self.assertIn("由该对话创建的关联任务对话 1 条", rendered)
        self.assertIn("Codex thread 名称：Root conversation", rendered)
        self.assertIn("项目：root-project", rendered)
        self.assertIn("Codex thread 名称：Review child task", rendered)
        self.assertIn("子代理名称：Socrates", rendered)
        self.assertIn("完整 Codex thread ID：child-full-id", rendered)
        self.assertNotIn("thread index remains", rendered)

    def test_catalog_limit_counts_targets_not_actions(self) -> None:
        plan = _display_plan()
        root_action = plan.actions[0]
        keep = _action(
            "keep:storage-one:conversation",
            kind="keep",
            storage_id="storage-one",
            thread_id="conversation-full-id",
        )
        keep.risk = "low"
        keep.available = True
        keep.unavailable_reason = None
        keep.impact = root_action.impact
        keep.observation_ids = root_action.observation_ids
        keep.requires_explicit_selection = False
        second = _action(
            "delete:storage-one:second",
            storage_id="storage-one",
            thread_id="second-conversation",
        )
        second.risk = "low"
        second.available = True
        second.unavailable_reason = None
        second.impact = root_action.impact
        second.observation_ids = root_action.observation_ids
        second.requires_explicit_selection = False
        plan.actions = (root_action, keep, second)

        output = StringIO()
        _write_action_catalog(plan, stdout=output, limit=1)
        rendered = output.getvalue()
        self.assertIn("候选动作：永久删除整条对话", rendered)
        self.assertIn("候选动作：保留，不做更改", rendered)
        self.assertNotIn("second-conversation", rendered)
        self.assertIn("另有 1 个目标未显示", rendered)

    def test_known_unavailable_reasons_are_fully_chinese(self) -> None:
        unimplemented = {
            "remove_broken_relation": (
                "Removing an invalid conversation relation is not "
                "implemented; it requires a verified database backup."
            ),
            "repair_index_path": (
                "Repairing a conversation list path is not implemented."
            ),
            "quarantine_artifacts": (
                "Artifact quarantine is not implemented."
            ),
            "remove_frontend_reference": (
                "Removing a frontend residual reference is not implemented."
            ),
            "repair_legacy_index": (
                "Repairing the legacy aggregate index is not implemented."
            ),
        }
        for kind, reason in unimplemented.items():
            with self.subTest(kind=kind):
                rendered = _human_unavailable_reason(
                    SimpleNamespace(
                        kind=kind,
                        unavailable_reason=reason,
                    )
                )
                self.assertIn("尚未实现", rendered)
                self.assertNotIn("not implemented", rendered)

        blockers = (
            (
                "Cleanup is blocked for this Codex data directory because "
                "current state could not be read completely: sqlite failed",
                "读取不完整",
            ),
            (
                "Deletion is blocked because current conversation content "
                "state could not be fingerprinted exactly: stat failed",
                "精确指纹",
            ),
            (
                "The conversation or an associated task conversation is "
                "still referenced by an active frontend session.",
                "活跃前端",
            ),
            (
                "Conversation identity evidence conflicts with another "
                "owner or parent, so deletion is blocked.",
                "证据冲突",
            ),
            (
                "Associated task conversation child-id has multiple current "
                "content files and must be reviewed independently.",
                "关联任务对话 child-id",
            ),
            (
                "High-risk deletion is blocked because current content files "
                "have conflicting source or parent metadata.",
                "来源或父对话元数据",
            ),
            (
                "The parent still has a native artifact.",
                "父对话仍有本地",
            ),
            (
                "The spawn edge is still open.",
                "打开状态",
            ),
            (
                "No matching spawn edge remains to corroborate the source "
                "metadata.",
                "佐证来源元数据",
            ),
            (
                "No native thread or rollout artifact can be passed to "
                "thread/delete.",
                "没有本地对话列表记录或内容文件",
            ),
            (
                "thread_spawn_edges evidence is unavailable.",
                "无法读取对话关联关系证据",
            ),
            (
                "Codex thread/delete would cascade into known descendant "
                "threads.",
                "级联到已知关联任务对话",
            ),
            (
                "thread/delete would cascade into spawned descendants.",
                "级联到关联任务对话",
            ),
        )
        for reason, expected in blockers:
            with self.subTest(reason=reason):
                rendered = _human_unavailable_reason(
                    SimpleNamespace(
                        kind="delete_conversation",
                        unavailable_reason=reason,
                    )
                )
                self.assertIn(expected, rendered)
                self.assertNotIn("blocked", rendered.lower())
                self.assertNotEqual(rendered, reason)

    def test_real_native_planner_catalog_hides_standard_english_blocker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            codex_home = Path(root) / "codex-home"
            parent_id = "missing-parent"
            child_id = "source-only-child"
            write_rollout(
                codex_home,
                child_id,
                originator="test-owner",
                source={
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                        }
                    }
                },
            )
            create_thread_index(codex_home, [])
            adapter = NativeIntegrityAdapter(codex_home=codex_home)
            plan = build_cleanup_plan(adapter.scan())
            output = StringIO()
            _write_action_catalog(plan, stdout=output, limit=0)

        rendered = output.getvalue()
        self.assertIn("已阻止删除", rendered)
        self.assertNotIn("source-parent metadata outside", rendered)
        self.assertNotIn("No matching spawn edge remains", rendered)

    def test_selected_plan_uses_related_task_conversation_wording(self) -> None:
        plan = _display_plan()
        output = StringIO()
        _write_selected_action_plan(
            plan.actions,
            plan.storages,
            conversations=plan.conversations,
            stdout=output,
        )
        rendered = output.getvalue()
        self.assertIn("将一同删除的关联任务对话：1 条", rendered)
        self.assertIn("警告：级联范围跨项目", rendered)
        self.assertIn("子代理名称：Socrates", rendered)
        self.assertNotIn("descendant", rendered.lower())
        self.assertNotIn("无后代", rendered)

    def test_catalog_shows_failed_storage_even_without_actions(self) -> None:
        plan = SimpleNamespace(
            storages=(
                SimpleNamespace(
                    storage_id="failed-storage",
                    label="Cindy 专用数据目录",
                    path="C:/Cindy/codex-home",
                    scan_status="failed",
                    errors=("spawned descendant threads could not be read",),
                ),
            ),
            observations=(),
            actions=(),
            errors=("unassigned scanner error",),
        )
        output = StringIO()
        _write_action_catalog(plan, stdout=output, limit=0)
        rendered = output.getvalue()
        self.assertIn("保存位置：Cindy 专用数据目录", rendered)
        self.assertIn("状态：扫描失败", rendered)
        self.assertIn("此保存位置的错误：关联任务对话 could not be read", rendered)
        self.assertIn("无法归属到保存位置的计划错误", rendered)
        self.assertIn("没有发现候选动作", rendered)
        self.assertNotIn("descendant", rendered.lower())
        self.assertNotIn("threads", rendered.lower())

    def test_catalog_translates_conflicting_codex_binary_hints(self) -> None:
        raw_error = (
            "Conflicting Codex executable hints were found for one "
            "Codex data directory."
        )
        plan = _display_plan()
        plan.storages[0].scan_status = "partial"
        plan.storages[0].errors = (raw_error,)
        plan.actions[0].available = False
        plan.actions[0].unavailable_reason = (
            "Cleanup is blocked for this Codex data directory because "
            f"current state could not be read completely: {raw_error}"
        )
        output = StringIO()
        _write_action_catalog(plan, stdout=output, limit=0)

        rendered = output.getvalue()
        self.assertNotIn("Conflicting Codex executable hints", rendered)
        self.assertIn("发现多个不同的 Codex 可执行文件", rendered)
        self.assertIn("--codex-bin PATH", rendered)
        self.assertNotIn("当前 Codex 数据目录读取不完整", rendered)

    def test_deleted_after_request_error_has_explicit_human_warning(self) -> None:
        finding = Finding(
            platform="native",
            platform_session_id="session",
            thread_id="conversation",
            reason="test",
            platform_db=Path("state_5.sqlite"),
            codex_home=Path("codex-home"),
        )
        report = CleanupReport(
            planned=[finding],
            results=[
                CleanupResult(
                    finding=finding,
                    status="deleted",
                    request_error="request timed out",
                )
            ],
        )
        output = StringIO()
        _emit_planned_cleanup_result(
            report,
            selected_actions=(),
            json_output=False,
            limit=0,
            stdout=output,
            stderr=StringIO(),
        )
        self.assertIn(
            "警告：请求曾报错，但磁盘验证确认已删除：request timed out",
            output.getvalue(),
        )

    def test_json_result_reuses_catalog_and_includes_action_id(self) -> None:
        plan = _display_plan()
        plan.plan_fingerprint = "result-plan"
        finding = Finding(
            platform="native",
            platform_session_id="session",
            thread_id="conversation-full-id",
            reason="test",
            platform_db=Path("C:/Codex/state_5.sqlite"),
            codex_home=Path("C:/Codex"),
        )
        report = CleanupReport(
            planned=[finding],
            results=[CleanupResult(finding=finding, status="deleted")],
        )
        output = StringIO()
        _emit_planned_cleanup_result(
            report,
            selected_actions=plan.actions,
            plan=plan,
            json_output=True,
            limit=1,
            stdout=output,
            stderr=StringIO(),
        )

        payload = json.loads(output.getvalue())
        result = payload["results"][0]
        self.assertEqual(result["action_id"], plan.actions[0].action_id)
        self.assertEqual(
            [
                item["summary"]["display_name"]
                for item in result["affected_conversations"]
            ],
            ["Root conversation", "Review child task"],
        )
        self.assertEqual(len(payload["planning_conversations"]), 2)


class _EmptyAdapter:
    name = "native"

    def scan(self) -> list[object]:
        return []


class _FindingAdapter:
    name = "native"

    def __init__(self, finding: Finding) -> None:
        self.finding = finding

    def scan(self) -> list[object]:
        return [self.finding]


class _FakeTTY(StringIO):
    def isatty(self) -> bool:
        return True


class _CallbackTTY(_FakeTTY):
    def __init__(self, value: str, callback: object) -> None:
        super().__init__(value)
        self.callback = callback
        self.called = False

    def readline(self, *args: object, **kwargs: object) -> str:
        if not self.called:
            self.called = True
            self.callback()
        return super().readline(*args, **kwargs)


class _EnterMutatingServer:
    def __init__(self, enter_callback: object) -> None:
        self.enter_callback = enter_callback
        self.deleted_thread_ids: list[str] = []

    def __enter__(self) -> _EnterMutatingServer:
        self.enter_callback()
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        self.deleted_thread_ids.append(thread_id)


class MainFlowTests(unittest.TestCase):
    def test_purge_requires_yes_and_clients_closed_before_scanning(self) -> None:
        for argv in (("purge",), ("purge", "--yes")):
            with self.subTest(argv=argv), patch(
                "local_agent_record_janitor.cli.scan_adapters"
            ) as scan:
                output = StringIO()
                errors = StringIO()
                result = main(
                    argv,
                    adapters=(_EmptyAdapter(),),
                    stdin=StringIO(),
                    stdout=output,
                    stderr=errors,
                )
                self.assertEqual(result, EXIT_ERROR)
                self.assertIn(
                    "--yes 和 --clients-closed",
                    output.getvalue() + errors.getvalue(),
                )
                scan.assert_not_called()

    def test_purge_json_noop_is_one_valid_document(self) -> None:
        output = StringIO()
        errors = StringIO()

        result = main(
            ("purge", "--yes", "--clients-closed", "--json"),
            adapters=(_EmptyAdapter(),),
            stdin=StringIO(),
            stdout=output,
            stderr=errors,
        )

        self.assertEqual(result, EXIT_OK, errors.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["command"], "purge")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["goal_status"], "complete")
        self.assertTrue(payload["goal_satisfied"])
        self.assertFalse(payload["modified"])
        self.assertEqual(payload["batch_count"], 0)
        self.assertEqual(payload["executed_action_count"], 0)

    def test_purge_blocked_actions_are_not_reported_as_completed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            codex_home = Path(root) / "codex-home"
            indexed_id = "indexed-thread"
            metadata_id = "metadata-thread"
            rollout = write_rollout(
                codex_home,
                metadata_id,
                originator="Codex Desktop",
            )
            create_thread_index(
                codex_home,
                [{"id": indexed_id, "rollout_path": str(rollout)}],
            )
            mismatch = next(
                finding
                for finding in NativeIntegrityAdapter(
                    codex_home=codex_home
                ).scan()
                if finding.details.get("finding_type")
                == "index_rollout_metadata_mismatch"
            )
            output = StringIO()
            errors = StringIO()

            result = main(
                ("purge", "--yes", "--clients-closed", "--json"),
                adapters=(_FindingAdapter(mismatch),),
                stdin=StringIO(),
                stdout=output,
                stderr=errors,
                app_server_factory=lambda **_kwargs: self.fail(
                    "blocked purge must not start app-server"
                ),
            )

        self.assertEqual(
            result,
            EXIT_GOAL_NOT_SATISFIED,
            output.getvalue() + errors.getvalue(),
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["goal_status"], "blocked")
        self.assertFalse(payload["goal_satisfied"])
        self.assertFalse(payload["modified"])
        self.assertGreater(payload["remaining_problem_count"], 0)
        self.assertGreater(payload["counts"]["blocked_group_count"], 0)
        self.assertTrue(payload["blockers"])
        self.assertIn(
            payload["blockers"][0]["blocker_code"],
            {"action_not_implemented", "action_unavailable"},
        )

    def test_purge_repairs_all_available_codex_residuals_without_selection(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            codex_home = Path(root) / "codex-home"
            create_thread_index(codex_home, [])
            legacy_path = codex_home / "session_index.jsonl"
            legacy_path.write_text(
                json.dumps({"id": "residual-id", "thread_name": "Old title"})
                + "\n",
                encoding="utf-8",
            )
            adapter = NativeIntegrityAdapter(codex_home=codex_home)
            output = StringIO()
            errors = StringIO()

            result = main(
                ("purge", "--yes", "--clients-closed"),
                adapters=(adapter,),
                stdin=StringIO(),
                stdout=output,
                stderr=errors,
            )

            self.assertEqual(result, EXIT_OK, errors.getvalue())
            self.assertEqual(legacy_path.read_text(encoding="utf-8"), "")
            self.assertIn("批量清理完成：1 批，1 个动作", output.getvalue())
            backup_root = (
                codex_home
                / ".local-agent-record-janitor"
                / "legacy-index-backups"
            )
            self.assertFalse(backup_root.exists())

    def test_cindy_codex_home_override_keeps_all_sibling_namespace_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            appdata = Path(temporary_directory) / "AppData"
            root = appdata / "CindyGlobal"
            root.mkdir(parents=True)
            local = root / "cindy-local-v1.db"
            owner = root / "cindy-owner-fixture.db"
            local.touch()
            owner.touch()
            custom_home = Path(temporary_directory) / "custom-codex-home"
            args = build_parser().parse_args(
                (
                    "records", "--platform", "cindy",
                    "--appdata", str(appdata),
                    "--cindy-codex-home", str(custom_home),
                )
            )

            adapters = create_default_adapters(args)

        self.assertEqual({item.database for item in adapters}, {local, owner})
        self.assertTrue(all(item.codex_home == custom_home for item in adapters))

    def test_local_clean_rediscovers_owner_namespace_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            appdata = base / "AppData"
            cindy_root = appdata / "CustomCindy"
            home = cindy_root / "codex-home"
            empty_native = base / "empty-native"
            empty_native.mkdir()
            thread_id = "clean-namespace-drift"
            rollout = write_rollout(home, thread_id, originator="cindy")
            create_thread_index(
                home,
                [{"id": thread_id, "rollout_path": str(rollout)}],
            )
            local = cindy_root / "cindy-local-v1.db"
            owner = cindy_root / "cindy-owner-added-after-preview.db"
            deleted_row = {
                "id": "local-deleted",
                "sdk_session_id": thread_id,
                "status": "deleted",
                "source": "desktop",
                "created_at": 1,
                "updated_at": 2,
                "parent_session_id": None,
                "agent_kind": "codex",
            }
            create_cindy_database(local, [deleted_row])

            def add_owner_namespace() -> None:
                create_cindy_database(
                    owner,
                    [{**deleted_row, "id": "owner-live", "status": "active"}],
                )

            input_stream = _CallbackTTY("确认删除\n", add_owner_namespace)
            output = _FakeTTY()
            errors = StringIO()
            server = _EnterMutatingServer(lambda: None)

            result = main(
                (
                    "clean", "--platform", "cindy",
                    "--thread-id", thread_id,
                    "--appdata", str(appdata),
                    "--codex-home", str(empty_native),
                    "--cindy-root", str(cindy_root),
                    "--cindy-db", str(local),
                ),
                stdin=input_stream,
                stdout=output,
                stderr=errors,
                app_server_factory=lambda **_kwargs: server,
                binary_resolver=lambda _hint: Path("codex"),
            )

        self.assertEqual(result, EXIT_ERROR)
        self.assertTrue(input_stream.called)
        self.assertEqual(server.deleted_thread_ids, [])
        self.assertIn("计划已变化", output.getvalue() + errors.getvalue())

    def test_local_clean_rediscovers_owner_namespace_after_server_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            appdata = base / "AppData"
            cindy_root = appdata / "CustomCindy"
            home = cindy_root / "codex-home"
            empty_native = base / "empty-native"
            empty_native.mkdir()
            thread_id = "clean-post-server-namespace-drift"
            rollout = write_rollout(home, thread_id, originator="cindy")
            create_thread_index(
                home,
                [{"id": thread_id, "rollout_path": str(rollout)}],
            )
            local = cindy_root / "cindy-local-v1.db"
            owner = cindy_root / "cindy-owner-added-after-server-start.db"
            deleted_row = {
                "id": "local-deleted",
                "sdk_session_id": thread_id,
                "status": "deleted",
                "source": "desktop",
                "created_at": 1,
                "updated_at": 2,
                "parent_session_id": None,
                "agent_kind": "codex",
            }
            create_cindy_database(local, [deleted_row])

            def add_owner_namespace() -> None:
                create_cindy_database(
                    owner,
                    [{**deleted_row, "id": "owner-live", "status": "active"}],
                )

            server = _EnterMutatingServer(add_owner_namespace)
            output = _FakeTTY()
            errors = StringIO()

            result = main(
                (
                    "clean", "--platform", "cindy",
                    "--thread-id", thread_id,
                    "--appdata", str(appdata),
                    "--codex-home", str(empty_native),
                    "--cindy-root", str(cindy_root),
                    "--cindy-db", str(local),
                ),
                stdin=_FakeTTY("确认删除\n"),
                stdout=output,
                stderr=errors,
                app_server_factory=lambda **_kwargs: server,
                binary_resolver=lambda _hint: Path("codex"),
            )

        self.assertEqual(result, EXIT_ERROR)
        self.assertEqual(server.deleted_thread_ids, [])
        self.assertIn("未成功 1 条", output.getvalue() + errors.getvalue())

    def test_scan_command_remains_read_only_and_compatible(self) -> None:
        output = StringIO()
        result = main(
            ("scan",),
            adapters=(_EmptyAdapter(),),
            stdin=StringIO(),
            stdout=output,
            stderr=StringIO(),
        )
        self.assertEqual(result, EXIT_OK)
        self.assertIn("未发现 Codex 对话一致性问题。", output.getvalue())

    def test_human_scan_output_uses_clear_chinese_labels(self) -> None:
        finding = Finding(
            platform="native",
            platform_session_id="session",
            thread_id="conversation-full-id",
            reason="Codex thread index remains but its rollout is missing",
            platform_db=Path("state_5.sqlite"),
            codex_home=Path("codex-home"),
            codex_indexed=True,
            details={"finding_type": "index_missing_rollout"},
        )
        output = StringIO()
        result = main(
            ("scan",),
            adapters=(_FindingAdapter(finding),),
            stdin=StringIO(),
            stdout=output,
            stderr=StringIO(),
        )
        self.assertEqual(result, EXIT_OK)
        rendered = output.getvalue()
        self.assertIn("发现 1 个 Codex 对话一致性问题。", rendered)
        self.assertIn("来源", rendered)
        self.assertIn("对话", rendered)
        self.assertIn("现存数据", rendered)
        self.assertIn("列表记录", rendered)
        self.assertNotIn("ARTIFACTS", rendered)
        self.assertNotIn("thread index remains", rendered)

    def test_frontend_scan_problem_uses_platform_fallback_label(self) -> None:
        finding = Finding(
            platform="aionui",
            platform_session_id="session",
            thread_id="conversation-full-id",
            reason="AionUI conversation row is gone but mapping remains",
            platform_db=Path("aionui.sqlite"),
            codex_home=Path("codex-home"),
            codex_indexed=True,
        )
        output = StringIO()
        result = main(
            ("scan",),
            adapters=(_FindingAdapter(finding),),
            stdin=StringIO(),
            stdout=output,
            stderr=StringIO(),
        )
        self.assertEqual(result, EXIT_OK)
        rendered = output.getvalue()
        self.assertIn("前端对话已删除，但 Codex 对话引用仍存在", rendered)
        self.assertNotIn("conversation row is gone", rendered)

    def test_default_json_plan_excludes_action_requiring_individual_selection(
        self,
    ) -> None:
        plan = _display_plan()
        plan.actions[0].requires_explicit_selection = True
        output = StringIO()
        with patch(
            "local_agent_record_janitor.planning.build_cleanup_plan",
            return_value=plan,
        ):
            result = main(
                ("clean", "--json"),
                adapters=(_EmptyAdapter(),),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_OK)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["selected_action_ids"], [])

    def test_yes_without_noninteractive_selector_is_blocked(self) -> None:
        output = StringIO()
        errors = StringIO()
        with patch(
            "local_agent_record_janitor.planning.build_cleanup_plan",
            return_value=_display_plan(),
        ):
            result = main(
                ("clean", "--yes"),
                adapters=(_EmptyAdapter(),),
                stdin=StringIO(),
                stdout=output,
                stderr=errors,
            )
        self.assertEqual(result, EXIT_ERROR)
        self.assertIn("--yes 只跳过最终确认", errors.getvalue())

    def test_noninteractive_high_risk_execution_requires_plan_fingerprint(self) -> None:
        plan = _display_plan()
        plan.actions[0].risk = "high"
        plan.plan_fingerprint = "approved-plan"
        output = StringIO()
        with patch(
            "local_agent_record_janitor.planning.build_cleanup_plan",
            return_value=plan,
        ):
            result = main(
                (
                    "clean",
                    "--json",
                    "--yes",
                    "--action-id",
                    plan.actions[0].action_id,
                ),
                adapters=(_EmptyAdapter(),),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_ERROR)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            payload["error"]["kind"],
            "approval_binding_required",
        )

    def test_legacy_repair_discards_temporary_backup_and_has_no_restore_command(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            codex_home = Path(root) / "codex-home"
            create_thread_index(codex_home, [])
            legacy_path = codex_home / "session_index.jsonl"
            original = (
                json.dumps(
                    {"id": "residual-id", "thread_name": "Old title"}
                )
                + "\n"
            )
            legacy_path.write_text(original, encoding="utf-8")
            adapter = NativeIntegrityAdapter(codex_home=codex_home)
            approved_plan = build_cleanup_plan(scan_adapters((adapter,)))
            repair_action = next(
                action
                for action in approved_plan.actions
                if str(getattr(action.kind, "value", action.kind))
                == "repair_legacy_index"
            )

            blocked_output = StringIO()
            blocked_exit = main(
                (
                    "clean",
                    "--json",
                    "--yes",
                    "--action-id",
                    repair_action.action_id,
                    "--plan-fingerprint",
                    approved_plan.plan_fingerprint,
                ),
                adapters=(adapter,),
                stdin=StringIO(),
                stdout=blocked_output,
                stderr=StringIO(),
            )
            self.assertEqual(blocked_exit, EXIT_ERROR)
            self.assertTrue(json.loads(blocked_output.getvalue())["blocked"])
            self.assertEqual(legacy_path.read_text(encoding="utf-8"), original)

            repair_output = StringIO()
            repair_exit = main(
                (
                    "clean",
                    "--json",
                    "--yes",
                    "--clients-closed",
                    "--action-id",
                    repair_action.action_id,
                    "--plan-fingerprint",
                    approved_plan.plan_fingerprint,
                ),
                adapters=(adapter,),
                stdin=StringIO(),
                stdout=repair_output,
                stderr=StringIO(),
            )
            self.assertEqual(repair_exit, EXIT_OK)
            repair_payload = json.loads(repair_output.getvalue())
            self.assertEqual(repair_payload["status"], "repaired")
            self.assertEqual(legacy_path.read_text(encoding="utf-8"), "")
            repair = repair_payload["repair"]
            self.assertFalse(repair["temporary_backup_retained"])
            self.assertFalse(Path(repair["backup_path"]).exists())
            self.assertNotIn("restore-legacy-index", build_parser().format_help())

    def test_json_yes_without_selector_is_one_complete_document(self) -> None:
        output = StringIO()
        with patch(
            "local_agent_record_janitor.planning.build_cleanup_plan",
            return_value=_display_plan(),
        ):
            result = main(
                ("clean", "--yes", "--json"),
                adapters=(_EmptyAdapter(),),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_ERROR)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["blocked"])
        self.assertEqual(len(payload["actions"]), 1)

    def test_json_yes_with_action_id_writes_only_execution_document(self) -> None:
        plan = _display_plan()
        finding = Finding(
            platform="native",
            platform_session_id="session",
            thread_id=plan.actions[0].target.thread_id,
            reason="test",
            platform_db=Path("state_5.sqlite"),
            codex_home=Path("codex-home"),
        )
        cleanup_report = CleanupReport(planned=[finding])
        output = StringIO()
        with (
            patch(
                "local_agent_record_janitor.planning.build_cleanup_plan",
                side_effect=(plan, plan),
            ),
            patch(
                "local_agent_record_janitor.cli._findings_for_actions",
                return_value=[finding],
            ),
            patch(
                "local_agent_record_janitor.cli.clean_findings",
                return_value=cleanup_report,
            ) as clean,
        ):
            result = main(
                (
                    "clean",
                    "--json",
                    "--yes",
                    "--action-id",
                    plan.actions[0].action_id,
                ),
                adapters=(_EmptyAdapter(),),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_OK)
        rendered = output.getvalue()
        self.assertTrue(rendered.lstrip().startswith("{"), repr(rendered))
        payload = json.loads(rendered)
        self.assertEqual(payload["command"], "clean")
        self.assertNotIn("actions", payload)
        self.assertEqual(payload["planned"], 1)
        self.assertEqual(len(payload["planning_storages"]), 1)
        self.assertIn("planning_errors", payload)
        clean.assert_called_once()
        kwargs = clean.call_args.kwargs
        self.assertTrue(callable(kwargs["pre_delete_validator"]))
        self.assertNotIn("approved_descendants", kwargs)
        self.assertEqual(
            kwargs["expected_scopes"][finding_key(finding)],
            ExpectedDeletionScope(
                descendant_thread_ids=("child-full-id",),
                indexed_thread_ids=("conversation-full-id",),
                rollout_paths=(),
                rollout_state_fingerprints=(),
            ),
        )
        self.assertEqual(kwargs["approved_integrity_deletes"], {})

    def test_cli_scope_fingerprint_blocks_post_enter_rollout_drift(
        self,
    ) -> None:
        mutators = {
            "originator": lambda payload, event: payload.__setitem__(
                "originator",
                "changed-owner",
            ),
            "source": lambda payload, event: payload.__setitem__(
                "source",
                {"changed": "parent"},
            ),
            "body": lambda payload, event: event.__setitem__(
                "body",
                "changed conversation body",
            ),
        }
        for label, mutator in mutators.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                codex_home = Path(root) / "codex-home"
                thread_id = f"fingerprint-{label}"
                rollout_path = write_rollout(
                    codex_home,
                    thread_id,
                    originator="test-owner",
                    source="app-server",
                )
                create_thread_index(codex_home, [])

                def enter(
                    path: Path = rollout_path,
                    update: object = mutator,
                ) -> object:
                    lines = path.read_text(encoding="utf-8").splitlines()
                    metadata = json.loads(lines[0])
                    event = json.loads(lines[1])
                    update(metadata["payload"], event["payload"])
                    path.write_text(
                        json.dumps(metadata)
                        + "\n"
                        + json.dumps(event)
                        + "\n",
                        encoding="utf-8",
                    )

                server = _EnterMutatingServer(enter)
                output = StringIO()
                error_output = StringIO()
                adapter = NativeIntegrityAdapter(codex_home=codex_home)
                approved_fingerprint = build_cleanup_plan(
                    scan_adapters((adapter,))
                ).plan_fingerprint
                result = main(
                    (
                        "clean",
                        "--yes",
                        "--thread-id",
                        thread_id,
                        "--plan-fingerprint",
                        approved_fingerprint,
                    ),
                    adapters=(adapter,),
                    stdin=StringIO(),
                    stdout=output,
                    stderr=error_output,
                    app_server_factory=lambda **_kwargs: server,
                    binary_resolver=lambda _hint: Path("codex"),
                )

                self.assertEqual(result, EXIT_ERROR)
                self.assertEqual(server.deleted_thread_ids, [])
                self.assertIn(
                    "未成功 1 条",
                    output.getvalue(),
                    error_output.getvalue(),
                )

    def test_failed_unrelated_storage_is_visible_but_healthy_action_executes(
        self,
    ) -> None:
        plan = _display_plan()
        failed_storage = SimpleNamespace(
            storage_id="failed-storage",
            label="Cindy 专用数据目录",
            path="C:/Cindy/codex-home",
            scan_status="failed",
            errors=("associated task conversations could not be read",),
        )
        plan.storages = (*plan.storages, failed_storage)
        finding = Finding(
            platform="native",
            platform_session_id="session",
            thread_id=plan.actions[0].target.thread_id,
            reason="test",
            platform_db=Path("state_5.sqlite"),
            codex_home=Path("codex-home"),
        )
        cleanup_report = CleanupReport(planned=[finding])
        output = StringIO()
        with (
            patch(
                "local_agent_record_janitor.planning.build_cleanup_plan",
                side_effect=(plan, plan),
            ),
            patch(
                "local_agent_record_janitor.cli._findings_for_actions",
                return_value=[finding],
            ),
            patch(
                "local_agent_record_janitor.cli.clean_findings",
                return_value=cleanup_report,
            ) as clean,
        ):
            result = main(
                (
                    "clean",
                    "--yes",
                    "--action-id",
                    plan.actions[0].action_id,
                ),
                adapters=(_EmptyAdapter(),),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_OK)
        self.assertIn("保存位置：Cindy 专用数据目录", output.getvalue())
        self.assertIn("状态：扫描失败", output.getvalue())
        clean.assert_called_once()

    def test_json_execution_keeps_unrelated_failed_storage_metadata(
        self,
    ) -> None:
        plan = _display_plan()
        plan.plan_fingerprint = "revalidated-plan"
        plan.errors = ()
        plan.storages = (
            *plan.storages,
            SimpleNamespace(
                storage_id="failed-storage",
                label="Cindy 专用数据目录",
                path="C:/Cindy/codex-home",
                scan_status="failed",
                errors=("scoped read failure",),
            ),
        )
        finding = Finding(
            platform="native",
            platform_session_id="session",
            thread_id=plan.actions[0].target.thread_id,
            reason="test",
            platform_db=Path("state_5.sqlite"),
            codex_home=Path("codex-home"),
        )
        output = StringIO()
        with (
            patch(
                "local_agent_record_janitor.planning.build_cleanup_plan",
                side_effect=(plan, plan),
            ),
            patch(
                "local_agent_record_janitor.cli._findings_for_actions",
                return_value=[finding],
            ),
            patch(
                "local_agent_record_janitor.cli.clean_findings",
                return_value=CleanupReport(planned=[finding]),
            ) as clean,
        ):
            result = main(
                (
                    "clean",
                    "--json",
                    "--yes",
                    "--action-id",
                    plan.actions[0].action_id,
                ),
                adapters=(_EmptyAdapter(),),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_OK)
        payload = json.loads(output.getvalue())
        failed = next(
            storage
            for storage in payload["planning_storages"]
            if storage["storage_id"] == "failed-storage"
        )
        self.assertEqual(failed["scan_status"], "failed")
        self.assertEqual(failed["errors"], ["scoped read failure"])
        self.assertEqual(payload["planning_errors"], [])
        self.assertEqual(payload["plan_fingerprint"], "revalidated-plan")
        clean.assert_called_once()

    def test_complete_action_id_builds_dry_run_plan(self) -> None:
        plan = _display_plan()
        output = StringIO()
        with patch(
            "local_agent_record_janitor.planning.build_cleanup_plan",
            return_value=plan,
        ):
            result = main(
                (
                    "clean",
                    "--action-id",
                    plan.actions[0].action_id,
                ),
                adapters=(_EmptyAdapter(),),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_CONFIRMATION_REQUIRED)
        self.assertIn("最终计划：1 个动作", output.getvalue())
        self.assertIn(
            f"动作 ID：{plan.actions[0].action_id}",
            output.getvalue(),
        )

    def test_tty_selection_and_confirmation_execute_in_same_process(
        self,
    ) -> None:
        plan = _display_plan()
        finding = Finding(
            platform="native",
            platform_session_id="session",
            thread_id=plan.actions[0].target.thread_id,
            reason="test",
            platform_db=Path("state_5.sqlite"),
            codex_home=Path("codex-home"),
        )
        report = CleanupReport(planned=[finding])
        output = _FakeTTY()
        with (
            patch(
                "local_agent_record_janitor.planning.build_cleanup_plan",
                side_effect=(plan, plan),
            ),
            patch(
                "local_agent_record_janitor.cli._findings_for_actions",
                return_value=[finding],
            ),
            patch(
                "local_agent_record_janitor.cli.clean_findings",
                return_value=report,
            ) as clean,
        ):
            result = main(
                ("clean",),
                adapters=(_EmptyAdapter(),),
                stdin=_FakeTTY("1\n确认删除\n"),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_OK)
        self.assertIn("请输入“确认删除”继续", output.getvalue())
        clean.assert_called_once()

    def test_tty_confirmation_cancel_makes_no_changes(self) -> None:
        plan = _display_plan()
        output = _FakeTTY()
        with (
            patch(
                "local_agent_record_janitor.planning.build_cleanup_plan",
                return_value=plan,
            ),
            patch("local_agent_record_janitor.cli.clean_findings") as clean,
        ):
            result = main(
                ("clean",),
                adapters=(_EmptyAdapter(),),
                stdin=_FakeTTY("1\n取消\n"),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_OK)
        self.assertIn("已取消；未做任何更改", output.getvalue())
        clean.assert_not_called()

    def test_tty_confirmation_eof_makes_no_changes(self) -> None:
        plan = _display_plan()
        output = _FakeTTY()
        with (
            patch(
                "local_agent_record_janitor.planning.build_cleanup_plan",
                return_value=plan,
            ),
            patch("local_agent_record_janitor.cli.clean_findings") as clean,
        ):
            result = main(
                ("clean",),
                adapters=(_EmptyAdapter(),),
                stdin=_FakeTTY("1\n"),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_OK)
        self.assertIn("输入已结束，已取消", output.getvalue())
        clean.assert_not_called()

    def test_tty_stdin_with_redirected_stdout_never_prompts_or_executes(
        self,
    ) -> None:
        plan = _display_plan()
        output = StringIO()
        with (
            patch(
                "local_agent_record_janitor.planning.build_cleanup_plan",
                return_value=plan,
            ),
            patch("local_agent_record_janitor.cli.clean_findings") as clean,
        ):
            result = main(
                ("clean",),
                adapters=(_EmptyAdapter(),),
                stdin=_FakeTTY("1\n确认删除\n"),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_CONFIRMATION_REQUIRED)
        self.assertNotIn("请输入要加入计划", output.getvalue())
        self.assertNotIn("请输入“确认删除”", output.getvalue())
        clean.assert_not_called()

    def test_keep_is_a_successful_no_op_decision(self) -> None:
        plan = _display_plan()
        plan.actions[0].kind = "keep"
        plan.actions[0].action_id = "keep:storage-one:conversation"
        output = StringIO()
        with patch(
            "local_agent_record_janitor.planning.build_cleanup_plan",
            return_value=plan,
        ):
            result = main(
                (
                    "clean",
                    "--action-id",
                    plan.actions[0].action_id,
                ),
                adapters=(_EmptyAdapter(),),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(result, EXIT_OK)
        self.assertIn("决定为保留；未做任何更改", output.getvalue())

    def test_snapshot_change_stops_before_app_server_execution(self) -> None:
        initial = _display_plan()
        changed = _display_plan()
        changed.actions[0].snapshot_fingerprint = "snapshot-two"
        output = StringIO()
        errors = StringIO()
        with patch(
            "local_agent_record_janitor.planning.build_cleanup_plan",
            side_effect=(initial, changed),
        ):
            result = main(
                (
                    "clean",
                    "--action-id",
                    initial.actions[0].action_id,
                    "--yes",
                ),
                adapters=(_EmptyAdapter(),),
                stdin=StringIO(),
                stdout=output,
                stderr=errors,
                app_server_factory=lambda *args, **kwargs: self.fail(
                    "app-server must not start after plan drift"
                ),
            )
        self.assertEqual(result, EXIT_ERROR)
        self.assertIn("计划已变化", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
