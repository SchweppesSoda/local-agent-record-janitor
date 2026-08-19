from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any, TextIO

from .adapters import AionUIAdapter, CindyAdapter
from .adapters.base import FrontendAdapter
from .adapter_factory import create_default_adapters as _create_default_adapters
from .blocker_codes import (
    STANDALONE_RELATION_CLEANUP_UNAVAILABLE,
    exact_blocker_codes,
)
from .cleanup_service import (
    CleanupService,
    filter_candidate_platforms,
    filter_supplied_adapters,
    selected_platforms,
)
from .cleaner import (
    AppServerFactory,
    BinaryResolver,
    CleanupReport,
    ExpectedDeletionScope,
    ScanReport,
    ThreadSelectionError,
    clean_findings,
    finding_key,
    scan_adapters,
    select_findings,
)
from .codex_app_server import CodexAppServer
from .codex_desktop_state import ClientInspector, DesktopStateError
from .conversation_metadata import (
    read_conversation_summaries,
    read_legacy_thread_names,
)
from .discovery import (
    choose_codex_binary,
    default_appdata,
    default_codex_home,
    discover_aionui_databases,
    resolve_cindy_profiles,
)
from .models import Finding
from .path_identity import canonical_existing_path_key
from .rendering import safe_single_line
from .execution import ExecutionError
from .legacy_index import (
    LegacyIndexError,
    LegacyIndexOperationError,
    LegacyIndexRepairResult,
    LegacyIndexRestoreResult,
    repair_legacy_index,
    restore_legacy_index,
)


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIRMATION_REQUIRED = 2
EXIT_GOAL_NOT_SATISFIED = 3
DEFAULT_HUMAN_LIMIT = 20
MANUAL_DELETE_CONFIRMATION = "客户端已关闭并确认永久删除"
PI_DELETE_CONFIRMATION = "Pi 客户端已关闭并确认永久删除"
CLAUDE_DELETE_CONFIRMATION = "Claude Code 客户端已关闭并确认永久删除"

_RISK_ORDER = ("low", "review", "high", "blocked")
_RISK_LABELS = {
    "low": "低风险",
    "review": "需复核",
    "high": "高风险",
    "blocked": "已阻止",
}
_ACTION_LABELS = {
    "delete_conversation": "永久删除整条对话",
    "remove_broken_relation": "清除无效的对话关联记录",
    "repair_index_path": "修复对话列表路径",
    "repair_legacy_index": "修复旧版聚合索引",
    "quarantine_artifacts": "隔离对话数据",
    "remove_frontend_reference": "清除前端残留引用",
    "remove_desktop_state": "清除 Codex Desktop 宿主残留状态",
    "keep": "保留，不做更改",
}
_PROBLEM_LABELS = {
    "index_missing_rollout": "对话列表记录存在，但内容文件缺失",
    "rollout_missing_index": "对话内容文件存在，但列表记录缺失",
    "duplicate_rollout": "同一对话 ID 对应多个内容文件",
    "index_rollout_path_mismatch": "列表记录指向的内容文件路径与实际位置不一致",
    "index_rollout_metadata_mismatch": "列表记录与内容文件中的对话 ID 不一致",
    "orphaned_subagent_thread": "关联任务对话的父对话已不存在",
    "residual_spawn_edge": "对话关联记录指向不存在的对话",
    "legacy_index_only": "旧版对话列表记录缺少内容文件",
    "frontend_deleted_reference": "前端对话已删除，但 Codex 对话引用仍存在",
    "desktop_state_orphan": (
        "原生 thread 已删除，但 Codex Desktop 目录/UI 状态仍残留"
    ),
}
_UNIMPLEMENTED_ACTION_REASONS = {
    "remove_broken_relation": (
        "单独清除无效对话关联尚未实现；执行前需要可验证的数据库备份、"
        "结构检查和事务保护"
    ),
    "repair_index_path": (
        "修复对话列表路径尚未实现；执行前必须重新验证内容身份并准备"
        "可恢复的数据库备份"
    ),
    "quarantine_artifacts": (
        "隔离对话数据尚未实现；需要可恢复的隔离清单和还原流程"
    ),
}
_BLOCKED_REASON_LABELS = (
    (
        "could not be assigned to a codex data directory",
        "存在无法归属到具体 Codex 数据目录的扫描失败，已阻止全部清理动作",
    ),
    (
        "conflicting codex executable hints",
        "同一 Codex 数据目录发现多个不同的 Codex 可执行文件；"
        "程序不会猜测，请用 --codex-bin PATH 明确指定",
    ),
    (
        "current state could not be read completely",
        "当前 Codex 数据目录读取不完整，已阻止清理动作",
    ),
    (
        "could not be fingerprinted exactly",
        "无法为当前内容文件生成包含身份、来源和文件状态的精确指纹，已阻止删除",
    ),
    (
        "still referenced by an active frontend session",
        "该对话或其关联任务对话仍被活跃前端会话引用，已阻止删除",
    ),
    (
        "legacy aggregate index finding is not a real conversation id",
        "旧版聚合索引项不是真实对话 ID，不能提交给官方删除接口",
    ),
    (
        "indexed content file belongs to a different conversation",
        "列表记录对应的内容文件属于其他对话，为保护无关数据已阻止删除",
    ),
    (
        "identity evidence conflicts with another owner or parent",
        "对话身份、所有者或父对话证据冲突，已阻止删除",
    ),
    (
        "ownership is not confirmed for this codex data directory",
        "无法确认该对话属于当前 Codex 数据目录，已阻止删除",
    ),
    (
        "requires quarantine or manual identity review",
        "该问题需要先隔离数据或人工核验身份，已阻止删除",
    ),
    (
        "scope could not be verified for this target",
        "无法验证该目标的完整关联任务对话范围，已阻止删除",
    ),
    (
        "explicitly read-only or unsafe",
        "同一对话还有明确不允许修改或不安全的问题，已阻止高风险删除",
    ),
    (
        "official conversation deletion cannot safely repair this observation",
        "适配器确认官方对话删除无法安全修复该问题，已阻止执行",
    ),
    (
        "no native conversation list record or content file is available",
        "没有可验证的本地对话列表记录或内容文件可作为官方删除目标",
    ),
    (
        "no deletion target is present",
        "当前不存在可验证的删除目标",
    ),
    (
        "metadata did not confirm conversation",
        "当前内容文件元数据无法确认目标对话身份，已阻止删除",
    ),
    (
        "source-parent metadata outside or conflicting",
        "内容文件的来源或父对话超出完整批准范围或互相冲突，已阻止删除",
    ),
    (
        "conflicting structured source parents",
        "对话存在互相冲突的结构化父对话来源，已阻止删除",
    ),
    (
        "current content file metadata could not confirm",
        "当前内容文件元数据无法确认目标对话身份，已阻止高风险删除",
    ),
    (
        "current content files have conflicting originator metadata",
        "当前内容文件的创建来源互相冲突，已阻止高风险删除",
    ),
    (
        "current content files have conflicting source or parent metadata",
        "当前内容文件的来源或父对话元数据互相冲突，已阻止高风险删除",
    ),
    (
        "no current conversation list record or metadata-verified content file",
        "当前没有列表记录或身份验证通过的内容文件，已阻止高风险删除",
    ),
    (
        "parent still has a native artifact",
        "父对话仍有本地列表记录或内容文件，不能把该对话判为孤立，已阻止删除",
    ),
    (
        "spawn edge is still open",
        "对话关联关系仍处于打开状态，已阻止删除",
    ),
    (
        "no matching spawn edge remains",
        "没有匹配的对话关联记录可佐证来源元数据，已阻止删除",
    ),
    (
        "no native thread or rollout artifact",
        "没有本地对话列表记录或内容文件可提交给官方删除接口",
    ),
    (
        "thread_spawn_edges evidence is unavailable",
        "无法读取对话关联关系证据，已阻止删除",
    ),
    (
        "would cascade into known descendant",
        "官方删除会级联到已知关联任务对话，必须先核对并批准完整范围",
    ),
    (
        "would cascade into spawned descendants",
        "官方删除会级联到关联任务对话，必须先核对并批准完整范围",
    ),
    (
        "legacy index entries are not thread/delete targets",
        "旧版索引项不是官方对话删除目标",
    ),
    (
        "residual relation deletion is blocked because the exact native "
        "child-target contract is not verified",
        "残留关系的现存子对话不满足完整、精确的本地目标契约，已阻止整条对话删除",
    ),
)


class AgentArgumentError(ValueError):
    """An Agent-only parser error that must be returned as structured JSON."""


class _ChineseArgumentParser(argparse.ArgumentParser):
    """Use Chinese framing around argparse's public help text."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"
        for action in self._actions:
            if action.dest == "help":
                action.help = "显示此帮助信息并退出"

    def format_usage(self) -> str:
        return super().format_usage().replace("usage: ", "用法：", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage: ", "用法：", 1)

    def error(self, message: str) -> None:
        if bool(getattr(self, "_agent_json_errors", False)):
            raise AgentArgumentError(message)
        super().error(message)


class NumberSelectionError(ValueError):
    """Raised when a temporary interactive-number selection is invalid."""


class ActionSelectionError(ValueError):
    """Raised when a durable action or conversation selector is unsafe."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "invalid",
        selector: str = "",
        matches: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.selector = selector
        self.matches = tuple(matches)


def parse_number_selection(value: str, *, item_count: int) -> tuple[int, ...]:
    """Parse one-based numbers and inclusive ranges into stable indexes.

    The returned values remain one-based because these numbers are temporary
    UI handles, not durable target identifiers.
    """

    if item_count < 0:
        raise ValueError("item_count must not be negative")
    raw_value = value.strip()
    if not raw_value:
        raise NumberSelectionError("请输入至少一个编号。")

    selected: set[int] = set()
    for raw_part in raw_value.split(","):
        part = raw_part.strip()
        if not part:
            raise NumberSelectionError("编号列表中包含空项。")
        if "-" in part:
            if part.count("-") != 1:
                raise NumberSelectionError(f"无效的编号范围：{part}")
            raw_start, raw_end = (token.strip() for token in part.split("-", 1))
            if not raw_start.isdecimal() or not raw_end.isdecimal():
                raise NumberSelectionError(f"无效的编号范围：{part}")
            start, end = int(raw_start), int(raw_end)
            if start > end:
                raise NumberSelectionError(f"编号范围必须从小到大：{part}")
            numbers = range(start, end + 1)
        else:
            if not part.isdecimal():
                raise NumberSelectionError(f"无效的编号：{part}")
            numbers = (int(part),)

        for number in numbers:
            if number < 1 or number > item_count:
                raise NumberSelectionError(
                    f"编号 {number} 超出可选范围 1-{item_count}。"
                )
            selected.add(number)
    return tuple(sorted(selected))


def parse_action_selection(
    value: str,
    *,
    risks: Sequence[str],
    requires_explicit_selection: Sequence[bool] | None = None,
) -> tuple[int, ...]:
    """Parse an action selection while keeping bulk choice low-risk only."""

    normalized = value.strip().lower()
    if normalized == "all":
        explicit_flags = tuple(
            requires_explicit_selection or (False,) * len(risks)
        )
        if len(explicit_flags) != len(risks):
            raise ValueError(
                "requires_explicit_selection must match risks"
            )
        return tuple(
            index
            for index, (risk, explicit) in enumerate(
                zip(risks, explicit_flags, strict=True),
                start=1,
            )
            if str(risk).lower() == "low" and not explicit
        )

    return parse_number_selection(value, item_count=len(risks))


def select_candidate_actions(
    actions: Sequence[Any],
    *,
    action_ids: Sequence[str] = (),
    thread_selectors: Sequence[str] = (),
) -> list[Any]:
    """Select stable action IDs or unambiguous conversation targets."""

    requested_actions = [value.strip() for value in action_ids if value.strip()]
    requested_threads = [
        value.strip() for value in thread_selectors if value.strip()
    ]
    if requested_actions and requested_threads:
        raise ActionSelectionError(
            "--action-id 与 --thread-id 不能同时使用。",
            kind="conflicting_selectors",
        )

    if requested_actions:
        by_id = {str(action.action_id): action for action in actions}
        selected: set[str] = set()
        for selector in requested_actions:
            if selector not in by_id:
                raise ActionSelectionError(
                    f"未找到动作 ID：{selector}",
                    kind="not_found",
                    selector=selector,
                )
            selected.add(selector)
        return [
            action for action in actions if str(action.action_id) in selected
        ]

    if not requested_threads:
        return []

    delete_actions = [
        action
        for action in actions
        if _enum_value(action.kind) == "delete_conversation"
    ]
    selected_targets: set[tuple[str, str]] = set()
    for selector in requested_threads:
        exact = [
            action
            for action in delete_actions
            if str(action.target.thread_id) == selector
        ]
        matches = exact or [
            action
            for action in delete_actions
            if str(action.target.thread_id).startswith(selector)
        ]
        identities = {
            (
                str(action.target.storage_id),
                str(action.target.thread_id),
            )
            for action in matches
        }
        if not identities:
            raise ActionSelectionError(
                f"未找到对话：{selector}",
                kind="not_found",
                selector=selector,
            )
        if len(identities) != 1:
            rendered = [
                f"{thread_id} @ {storage_id}"
                for storage_id, thread_id in sorted(identities)
            ]
            raise ActionSelectionError(
                "该对话选择器在多个保存位置或多个对话中匹配，"
                "请改用完整动作 ID。",
                kind="ambiguous",
                selector=selector,
                matches=rendered,
            )
        selected_targets.update(identities)
    return [
        action
        for action in delete_actions
        if (
            str(action.target.storage_id),
            str(action.target.thread_id),
        )
        in selected_targets
    ]


def _conflicting_action_decision(
    actions: Sequence[Any],
) -> ActionSelectionError | None:
    by_target: dict[tuple[str, str], list[Any]] = {}
    for action in actions:
        key = (
            str(action.target.storage_id),
            str(action.target.thread_id),
        )
        by_target.setdefault(key, []).append(action)
    for (storage_id, thread_id), target_actions in by_target.items():
        kinds = {_enum_value(action.kind) for action in target_actions}
        if "keep" in kinds and len(kinds) > 1:
            return ActionSelectionError(
                "同一对话不能同时选择“保留”和其他动作："
                f"{thread_id} @ {storage_id}",
                kind="conflicting_actions",
                matches=[
                    str(action.action_id) for action in target_actions
                ],
            )
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = _ChineseArgumentParser(
        prog="local-agent-record-janitor",
        description=(
            "检查前端残留，只读盘点本地 Agent 记录（Codex thread、"
            "Pi Agent/Claude Code session），"
            "并保守清理明确选择且重验证通过的目标。"
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        title="命令",
    )

    scan = subparsers.add_parser(
        "scan",
        help="只读扫描，不修改任何数据",
        description="只读检查前端残留和 Codex 本地对话状态。",
    )
    _add_common_arguments(scan)

    records = subparsers.add_parser(
        "records",
        help="只读列出本地 Agent 记录（Codex thread、Pi/Claude session）",
        description=(
            "只读聚合 Codex 本地记录、Cindy/AionUI 引用以及 "
            "Pi Agent/Claude Code 会话；"
            "不会启动 app-server，也不会修改任何数据库或文件。"
        ),
    )
    _add_common_arguments(records)

    delete = subparsers.add_parser(
        "delete",
        help="逐项永久删除 Codex thread 或 Pi/Claude Code session",
        description=(
            "列出正常和异常的本地 Agent 记录，逐项选择存储身份明确的目标，"
            "经客户端关闭确认和执行前精确重验证后调用官方删除接口；"
            "仅 --platform pi 时删除精确批准的 Pi 会话文件；"
            "仅 --platform claude 时删除精确批准的 Claude 会话文件清单。"
        ),
    )
    _add_common_arguments(delete)
    delete.add_argument(
        "--yes",
        action="store_true",
        help="跳过 TTY 最终确认提示；不能替代目标选择或客户端关闭声明",
    )
    delete.add_argument(
        "--action-id",
        action="append",
        default=[],
        metavar="ACTION_ID",
        help=(
            "选择 records/delete 计划中的完整稳定删除操作 ID；可重复，"
            "不能与 --thread-id 同时使用"
        ),
    )
    delete.add_argument(
        "--session-id",
        action="append",
        default=[],
        metavar="ID",
        help=(
            "仅 --platform pi/claude：选择完整会话 ID；"
            "可重复，不能与 --action-id 同时使用"
        ),
    )
    delete.add_argument(
        "--plan-fingerprint",
        metavar="FINGERPRINT",
        help="非 TTY 执行时回传此前预览得到的完整所选计划指纹",
    )
    delete.add_argument(
        "--clients-closed",
        action="store_true",
        help=(
            "确认使用所选 Codex thread 或 Pi/Claude Code session 的"
            "相关客户端均已关闭"
        ),
    )
    delete.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        metavar="SECONDS",
        help="app-server 请求超时秒数（默认：30）",
    )

    clean = subparsers.add_parser(
        "clean",
        help="生成动作计划，清理原生 thread 或经验证的 Desktop 宿主残留",
        description=(
            "生成保守的动作计划，并通过官方 Codex app-server "
            "清理明确选择且重验证通过的对话；原生记录已消失但 "
            "Desktop 宿主目录仍残留时，使用独立的高风险精确清理动作。"
        ),
    )
    _add_common_arguments(clean)
    clean.add_argument(
        "--yes",
        action="store_true",
        help="跳过 TTY 最终确认提示并执行；不能替代目标选择",
    )
    clean.add_argument(
        "--action-id",
        action="append",
        default=[],
        metavar="ACTION_ID",
        help=(
            "从计划 JSON 选择一个完整稳定 action ID；可重复，"
            "不能与 --thread-id 同时使用"
        ),
    )
    clean.add_argument(
        "--plan-fingerprint",
        metavar="FINGERPRINT",
        help=(
            "绑定此前 clean --json 展示的完整计划指纹；非交互执行 "
            "review/high 风险动作时必须提供"
        ),
    )
    clean.add_argument(
        "--clients-closed",
        action="store_true",
        help=(
            "确认使用同一 Codex 数据目录的 Codex/AionUI/Cindy 客户端均已关闭；"
            "非交互修复旧版索引时必须提供"
        ),
    )
    clean.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        metavar="SECONDS",
        help="app-server 请求超时秒数（默认：30）",
    )

    purge = subparsers.add_parser(
        "purge",
        help="一键清理原生 Codex、Cindy 和 AionUI 的可执行残留",
        description=(
            "连续重扫并清理原生 Codex、Cindy 和 AionUI 中已经判定为异常、"
            "当前可安全执行的本地记录；不会删除正常对话，也不会触碰 Pi 或 "
            "Claude Code 会话。每批仍执行计划指纹复核，Desktop/旧索引修改仍"
            "创建可验证备份，真正受阻的目标会保留。"
        ),
    )
    _add_common_arguments(
        purge,
        codex_only=True,
        allow_thread_selector=False,
    )
    purge.add_argument(
        "--yes",
        action="store_true",
        help="确认执行当前扫描中所有可执行的异常记录清理动作",
    )
    purge.add_argument(
        "--clients-closed",
        action="store_true",
        help="确认 Codex/ChatGPT Desktop、Cindy 和 AionUI 均已完全退出",
    )
    purge.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        metavar="SECONDS",
        help="app-server 请求超时秒数（默认：30）",
    )

    agent = subparsers.add_parser(
        "agent",
        help="供 AGENTS 使用的非交互、可审计清理接口",
        description=(
            "只输出结构化 JSON；通过独立 plan 哈希授权、持久化 operation "
            "状态和只读 verify，避免崩溃后重复发送删除。"
        ),
    )
    agent._agent_json_errors = True
    agent_subparsers = agent.add_subparsers(
        dest="agent_command",
        required=True,
        title="Agent 命令",
    )

    agent_doctor = agent_subparsers.add_parser(
        "doctor",
        help="只读核验目标存储、扫描和客户端状态",
    )
    agent_doctor._agent_json_errors = True
    _add_common_arguments(
        agent_doctor,
        codex_only=False,
        allow_thread_selector=False,
    )
    agent_doctor.set_defaults(action_id=[], session_id=[])

    agent_plan = agent_subparsers.add_parser(
        "plan",
        help="生成不可变的单批清理授权计划",
    )
    agent_plan._agent_json_errors = True
    _add_common_arguments(
        agent_plan,
        codex_only=False,
        allow_thread_selector=False,
    )
    agent_plan.add_argument(
        "--action-id",
        action="append",
        default=[],
        metavar="ACTION_ID",
        help="仅 Pi/Claude：选择完整、存储限定的删除动作 ID；可重复",
    )
    agent_plan.add_argument(
        "--session-id",
        action="append",
        default=[],
        metavar="ID",
        help="仅 Pi/Claude：选择完整会话 ID；可重复且不得存在歧义",
    )
    agent_plan.add_argument(
        "--operation",
        choices=("purge",),
        default="purge",
        help="当前支持 purge（默认：purge）",
    )
    agent_plan.add_argument(
        "--out",
        type=Path,
        required=True,
        metavar="PLAN.json",
        help="新计划文件路径；为防止授权混淆，绝不覆盖现有文件",
    )

    agent_apply = agent_subparsers.add_parser(
        "apply",
        help="按计划哈希执行冻结动作集合",
    )
    agent_apply._agent_json_errors = True
    agent_apply.add_argument("--plan", type=Path, required=True)
    agent_apply.add_argument(
        "--authorized-plan-sha256",
        metavar="SHA256",
        help="复核后原样回传计划中的 plan_sha256",
    )
    agent_apply.add_argument(
        "--clients-closed",
        action="store_true",
        help="确认目标数据目录相关客户端均已关闭",
    )
    agent_apply.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        metavar="SECONDS",
        help="单次 app-server 请求超时（默认：30）",
    )
    agent_apply.add_argument(
        "--verify-timeout",
        type=_nonnegative_int,
        default=180,
        metavar="SECONDS",
        help="保留给验证策略的总时限（默认：180）",
    )

    agent_status = agent_subparsers.add_parser(
        "status",
        help="只读返回已持久化的 operation 状态",
    )
    agent_status._agent_json_errors = True
    agent_status.add_argument("--operation-id", required=True)
    agent_status.add_argument("--codex-home", type=Path, metavar="PATH")
    agent_status.add_argument(
        "--operation-home",
        type=Path,
        metavar="PATH",
        help="Pi/Claude operation 所在的 Agent/config 根目录",
    )

    agent_verify = agent_subparsers.add_parser(
        "verify",
        help="不重发删除，只读核验冻结目标是否已消失",
    )
    agent_verify._agent_json_errors = True
    agent_verify.add_argument("--operation-id", required=True)
    agent_verify.add_argument("--codex-home", type=Path, metavar="PATH")
    agent_verify.add_argument(
        "--operation-home",
        type=Path,
        metavar="PATH",
        help="Pi/Claude operation 所在的 Agent/config 根目录",
    )
    agent_verify.add_argument(
        "--verify-timeout",
        type=_nonnegative_int,
        default=180,
        metavar="SECONDS",
        help="总核验时限（默认：180）",
    )

    restore = subparsers.add_parser(
        "restore-legacy-index",
        help="从 Janitor 的可验证备份还原旧版聚合索引",
        description=(
            "仅当当前文件仍是对应修复产生的版本时，才从指定备份还原；"
            "还原前也会创建新的可验证备份。"
        ),
    )
    restore.add_argument(
        "--backup-id",
        required=True,
        metavar="BACKUP_ID",
        help="repair_legacy_index 结果中的完整备份 ID",
    )
    restore.add_argument(
        "--codex-home",
        type=Path,
        metavar="PATH",
        help="备份所属 Codex 数据目录（默认使用 CODEX_HOME 或 ~/.codex）",
    )
    restore.add_argument(
        "--yes",
        action="store_true",
        help="跳过 TTY 最终确认提示并执行",
    )
    restore.add_argument(
        "--clients-closed",
        action="store_true",
        help="确认使用同一数据目录的相关客户端均已关闭",
    )
    restore.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读 JSON",
    )
    return parser


def _add_common_arguments(
    parser: argparse.ArgumentParser,
    *,
    codex_only: bool = False,
    allow_thread_selector: bool = True,
) -> None:
    parser.add_argument(
        "--platform",
        action="append",
        choices=(
            ("all", "aionui", "cindy", "native")
            if codex_only
            else ("all", "aionui", "cindy", "native", "pi", "claude")
        ),
        help="选择扫描来源；可重复（默认：all）",
    )
    if allow_thread_selector:
        parser.add_argument(
            "--thread-id",
            action="append",
            default=[],
            metavar="ID_OR_PREFIX",
            help="选择完整对话 ID 或唯一前缀；可重复",
        )
    else:
        parser.set_defaults(thread_id=[])
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出完整的机器可读 JSON",
    )
    parser.add_argument(
        "--limit",
        type=_nonnegative_int,
        default=DEFAULT_HUMAN_LIMIT,
        metavar="COUNT",
        help=(
            "人类可读输出中 scan 最多显示的问题数，records/delete/clean "
            "最多显示的记录或候选目标数，purge 最多显示的单批目标数；"
            "最终计划和执行结果始终完整；0 表示全部"
            f"（默认：{DEFAULT_HUMAN_LIMIT}；JSON 不受限制）"
        ),
    )
    parser.add_argument("--appdata", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--codex-home",
        type=Path,
        metavar="PATH",
        help=(
            "显式扫描一个 Codex 数据目录（默认使用 CODEX_HOME 或 ~/.codex）"
        ),
    )
    parser.add_argument(
        "--codex-bin",
        type=_existing_codex_binary,
        metavar="PATH",
        help=(
            "显式指定与该数据目录匹配的 Codex 可执行文件；"
            "必须是现存普通文件"
        ),
    )
    parser.add_argument("--aionui-db", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--aionui-codex-home", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cindy-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cindy-db", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cindy-codex-home", type=Path, help=argparse.SUPPRESS)
    if not codex_only:
        parser.add_argument(
            "--pi-agent-dir",
            type=Path,
            metavar="PATH",
            help="Pi Agent 数据目录（否则 PI_CODING_AGENT_DIR，再否则 ~/.pi/agent）",
        )
        parser.add_argument(
            "--pi-session-dir",
            type=Path,
            metavar="PATH",
            help="Pi session JSONL 根目录（优先于环境变量、settings.json 与 agentDir 默认值）",
        )
        parser.add_argument(
            "--claude-config-dir",
            type=Path,
            metavar="PATH",
            help="Claude Code 配置根目录（否则 CLAUDE_CONFIG_DIR，再否则 ~/.claude）",
        )


def create_default_adapters(args: argparse.Namespace) -> list[FrontendAdapter]:
    return _create_default_adapters(args)


def main(
    argv: Sequence[str] | None = None,
    *,
    adapters: Iterable[FrontendAdapter] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    app_server_factory: AppServerFactory = CodexAppServer,
    binary_resolver: BinaryResolver = choose_codex_binary,
    pi_catalog_builder: Any | None = None,
    pi_delete_executor: Any | None = None,
    claude_catalog_builder: Any | None = None,
    claude_delete_executor: Any | None = None,
    client_inspector: ClientInspector | None = None,
    cleanup_service: CleanupService | None = None,
) -> int:
    input_stream = stdin or sys.stdin
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except AgentArgumentError as exc:
        document = {
                "schema_version": "larj.agent-result.v1",
                "document_type": "operation_result",
                "command": "agent",
                "subcommand": "invalid",
                "mode": "agent",
                "phase": "failed",
                "operation_id": "unaccepted",
                "plan_sha256": "",
                "goal_status": "unknown",
                "goal_satisfied": False,
                "modified": False,
                "mutation_started": False,
                "blockers": [
                    {
                        "blocker_code": "invalid_agent_arguments",
                        "scope": "agent_command",
                        "severity": "error",
                        "retryable": False,
                        "remediation": (
                            "Correct the Agent command arguments and retry; "
                            "no mutation was attempted."
                        ),
                        "message": str(exc),
                    }
                ],
                "counts": {
                    "finding_count": 0,
                    "issue_group_count": 0,
                    "root_action_count": 0,
                    "affected_thread_count": 0,
                    "artifact_count": 0,
                    "blocked_group_count": 0,
                    "legacy_residual_line_count": 0,
                    "legacy_residual_unique_thread_count": 0,
                },
            }
        json.dump(
            document,
            output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        output.write("\n")
        return EXIT_ERROR

    if args.command == "agent":
        from .agent_cli import run_agent_command

        return run_agent_command(
            args,
            supplied_adapters=adapters,
            stdout=output,
            stderr=error_output,
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
            client_inspector=client_inspector,
            cleanup_service=cleanup_service,
        )

    service = cleanup_service or CleanupService(
        scanner=scan_adapters,
        client_inspector=client_inspector,
    )

    if args.command == "restore-legacy-index":
        return _run_legacy_index_restore(
            args,
            stdin=input_stream,
            stdout=output,
            stderr=error_output,
        )

    unsupported_engine = _explicit_unsupported_scan_engine(args.platform)
    if args.command in {"scan", "clean", "purge"} and unsupported_engine:
        return _emit_fatal_error(
            args.command,
            RuntimeError(
                f"{unsupported_engine} 当前仅支持 records 和 delete；"
                f"scan/clean/purge 不支持 --platform "
                f"{unsupported_engine.lower().split()[0]}。"
            ),
            json_output=args.json,
            stdout=output,
            stderr=error_output,
        )

    if args.command == "purge":
        if args.thread_id:
            return _emit_fatal_error(
                "purge",
                RuntimeError(
                    "purge 固定处理当前扫描中全部可执行异常；"
                    "不接受 --thread-id。需要逐项选择时请使用 clean。"
                ),
                json_output=args.json,
                stdout=output,
                stderr=error_output,
            )
        if not args.yes or not args.clients_closed:
            return _emit_fatal_error(
                "purge",
                RuntimeError(
                    "purge 只允许在相关客户端全部退出后批量执行；"
                    "请同时提供 --yes 和 --clients-closed。"
                ),
                json_output=args.json,
                stdout=output,
                stderr=error_output,
            )

    if args.command == "delete" and _has_explicit_pi(args.platform):
        if not _is_exact_pi_platform(args.platform):
            return _emit_fatal_error(
                "delete",
                RuntimeError(
                    "Pi 删除不能与其他 --platform 混用；请仅使用 --platform pi。"
                ),
                json_output=args.json,
                stdout=output,
                stderr=error_output,
                )
        if adapters is not None and pi_catalog_builder is None:
            return _emit_fatal_error(
                "delete",
                RuntimeError("注入 adapters 时必须同时注入 Pi catalog builder；未读取本机 Pi home。"),
                json_output=args.json, stdout=output, stderr=error_output,
            )
        return _run_pi_delete(
            args,
            stdin=input_stream,
            stdout=output,
            stderr=error_output,
            catalog_builder=pi_catalog_builder,
            delete_executor=pi_delete_executor,
            cleanup_service=service,
        )

    if args.command == "delete" and _has_explicit_claude(args.platform):
        if not _is_exact_claude_platform(args.platform):
            return _emit_fatal_error(
                "delete",
                RuntimeError(
                    "Claude 删除不能与其他 --platform 混用；"
                    "请仅使用 --platform claude。"
                ),
                json_output=args.json, stdout=output, stderr=error_output,
            )
        if adapters is not None and claude_catalog_builder is None:
            return _emit_fatal_error(
                "delete",
                RuntimeError("注入 adapters 时必须同时注入 Claude catalog builder；未读取本机 Claude home。"),
                json_output=args.json, stdout=output, stderr=error_output,
            )
        return _run_claude_delete(
            args, stdin=input_stream, stdout=output, stderr=error_output,
            catalog_builder=claude_catalog_builder,
            delete_executor=claude_delete_executor,
            cleanup_service=service,
        )

    if args.command == "records" and adapters is not None:
        missing_builder: str | None = None
        if _is_exact_pi_platform(args.platform) and pi_catalog_builder is None:
            missing_builder = "Pi"
        elif _is_exact_claude_platform(args.platform) and claude_catalog_builder is None:
            missing_builder = "Claude"
        if missing_builder is not None:
            return _emit_fatal_error(
                "records",
                RuntimeError(
                    f"注入 adapters 时，records --platform {missing_builder.lower()} "
                    f"必须同时注入 {missing_builder} catalog builder；"
                    "未读取本机会话目录。"
                ),
                json_output=args.json,
                stdout=output,
                stderr=error_output,
            )

    adapter_builder: Callable[[], list[FrontendAdapter]] | None = None
    try:
        if args.command in {"clean", "delete", "purge"}:
            if adapters is not None:
                # All supplied adapters participate as live-reference guards;
                # --platform filters candidates only after the protected scan.
                active_adapters = list(adapters)
            else:
                guard_args = argparse.Namespace(**vars(args))
                guard_args.platform = ["all"]
                adapter_builder = lambda: create_default_adapters(guard_args)
                active_adapters = adapter_builder()
        else:
            active_adapters = (
                _filter_supplied_adapters(adapters, args.platform)
                if adapters is not None
                else create_default_adapters(args)
            )
    except Exception as exc:
        return _emit_fatal_error(
            args.command,
            exc,
            json_output=args.json,
            stdout=output,
            stderr=error_output,
        )

    if args.command == "records":
        return _run_records(
            args,
            active_adapters=active_adapters,
            stdout=output,
            stderr=error_output,
            pi_catalog_builder=pi_catalog_builder,
            include_pi=(
                _records_include_pi(args.platform)
                and (
                    adapters is None
                    or pi_catalog_builder is not None
                )
            ),
            claude_catalog_builder=claude_catalog_builder,
            include_claude=(
                _records_include_claude(args.platform)
                and (
                    adapters is None
                    or claude_catalog_builder is not None
                )
            ),
        )
    if args.command == "delete":
        if args.session_id:
            return _emit_fatal_error(
                "delete",
                RuntimeError("--session-id 仅可用于 --platform pi 或 --platform claude。"),
                json_output=args.json, stdout=output, stderr=error_output,
            )
        return _run_manual_delete(
            args,
            active_adapters=active_adapters,
            stdin=input_stream,
            stdout=output,
            stderr=error_output,
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
            adapter_builder=adapter_builder,
        )

    scan_report = service.scan(
        active_adapters,
        platforms=(
            args.platform
            if args.command in {"clean", "purge"}
            else None
        ),
    ).report
    if args.command in {"clean", "purge"}:
        if args.command == "purge":
            return _run_codex_purge(
                args,
                scan_report=scan_report,
                active_adapters=active_adapters,
                stdin=input_stream,
                stdout=output,
                stderr=error_output,
                app_server_factory=app_server_factory,
                binary_resolver=binary_resolver,
                adapter_builder=adapter_builder,
                client_inspector=service.client_inspector,
                cleanup_service=service,
            )
        return _run_planned_cleanup(
            args,
            scan_report=scan_report,
            active_adapters=active_adapters,
            stdin=input_stream,
            stdout=output,
            stderr=error_output,
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
            adapter_builder=adapter_builder,
            client_inspector=service.client_inspector,
            cleanup_service=service,
        )

    try:
        selected = select_findings(scan_report.findings, args.thread_id)
    except ThreadSelectionError as exc:
        return _emit_selection_error(
            args.command,
            exc,
            scan_report=scan_report,
            json_output=args.json,
            stdout=output,
            stderr=error_output,
        )
    selected_report = ScanReport(findings=selected, errors=scan_report.errors)
    _emit_scan(
        selected_report,
        json_output=args.json,
        limit=args.limit,
        stdout=output,
        stderr=error_output,
    )
    return EXIT_OK if selected_report.ok else EXIT_ERROR


def _run_codex_purge(
    args: argparse.Namespace,
    *,
    scan_report: ScanReport,
    active_adapters: Sequence[FrontendAdapter],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    app_server_factory: AppServerFactory,
    binary_resolver: BinaryResolver,
    adapter_builder: Callable[[], Sequence[FrontendAdapter]] | None = None,
    client_inspector: ClientInspector | None = None,
    cleanup_service: CleanupService | None = None,
) -> int:
    """Execute every currently available Codex cleanup in safe-sized batches."""

    service = cleanup_service or CleanupService(
        client_inspector=client_inspector,
    )

    current_report = scan_report
    completed_batches: list[dict[str, Any]] = []
    seen_batches: set[tuple[str, tuple[str, ...]]] = set()
    executed_action_count = 0

    for batch_number in range(1, 1025):
        try:
            plan = service.plan(
                service.snapshot_from_report(
                    current_report,
                    active_adapters=active_adapters,
                    platforms=args.platform,
                )
            )
        except Exception as exc:
            return _emit_fatal_error(
                "purge",
                exc,
                json_output=args.json,
                stdout=stdout,
                stderr=stderr,
            )
        if not plan.scan_complete:
            from .agent_operations import plan_counts, structured_blocker

            counts = plan_counts(
                findings=current_report.findings,
                actions=plan.actions,
            )
            payload = {
                "command": "purge",
                "status": "unknown",
                "goal_status": "unknown",
                "goal_satisfied": False,
                "modified": executed_action_count > 0,
                "batch_count": len(completed_batches),
                "executed_action_count": executed_action_count,
                "remaining_problem_count": counts["issue_group_count"],
                "remaining_blocked_or_unavailable_action_count": counts[
                    "blocked_group_count"
                ],
                "counts": counts,
                "blockers": [
                    structured_blocker(
                        "scan_incomplete",
                        scope="purge",
                        remediation=(
                            "Resolve every scan error and run a fresh plan; "
                            "do not infer cleanup success."
                        ),
                        message="; ".join(str(value) for value in plan.errors),
                    )
                ],
                "remaining_plan_fingerprint": plan.plan_fingerprint,
                "batches": completed_batches,
            }
            if args.json:
                _write_json(payload, stdout)
            else:
                stderr.write(
                    "完整扫描未成功，批量清理结果未知；"
                    "没有继续猜测或绕过阻断。\n"
                )
            return EXIT_ERROR

        batch = _next_purge_action_batch(plan.actions)
        if batch is None:
            from .action_registry import action_capability
            from .agent_operations import plan_counts, structured_blocker

            unavailable = [
                action
                for action in plan.actions
                if _enum_value(action.kind) != "keep"
                and (
                    not action.executable
                    or not action_capability(action.kind).implemented
                    or action_capability(action.kind).mutation_family is None
                )
            ]
            counts = plan_counts(
                findings=current_report.findings,
                actions=plan.actions,
            )
            blockers = [
                structured_blocker(
                    (
                        "action_not_implemented"
                        if not action_capability(action.kind).implemented
                        else "action_unavailable"
                    ),
                    scope=f"action:{action.action_id}",
                    remediation=(
                        "Resolve the structured safety condition and create a "
                        "fresh plan; the action was not executed."
                    ),
                    message=str(action.unavailable_reason or "Action unavailable"),
                    action_id=str(action.action_id),
                )
                for action in unavailable
            ]
            if unavailable:
                goal_status = (
                    "completed_with_residuals"
                    if executed_action_count
                    else "blocked"
                )
                legacy_status = goal_status
            else:
                goal_status = "complete"
                legacy_status = "completed"
            payload = {
                "command": "purge",
                "status": legacy_status,
                "goal_status": goal_status,
                "goal_satisfied": goal_status == "complete",
                "modified": executed_action_count > 0,
                "batch_count": len(completed_batches),
                "executed_action_count": executed_action_count,
                "remaining_blocked_or_unavailable_action_count": len(unavailable),
                "remaining_problem_count": counts["blocked_group_count"],
                "counts": counts,
                "blockers": blockers,
                "remaining_plan_fingerprint": plan.plan_fingerprint,
                "batches": completed_batches,
            }
            if args.json:
                _write_json(payload, stdout)
            else:
                if goal_status == "complete":
                    stdout.write(
                        "批量清理完成："
                        f"{len(completed_batches)} 批，"
                        f"{executed_action_count} 个动作。"
                    )
                else:
                    stdout.write(
                        "批量清理未达到目标："
                        f"已执行 {len(completed_batches)} 批、"
                        f"{executed_action_count} 个动作。"
                    )
                if unavailable:
                    stdout.write(
                        f"仍有 {len(unavailable)} 个动作因安全条件不足或尚未实现而保留。"
                    )
                stdout.write("\n")
            return (
                EXIT_OK
                if goal_status == "complete"
                else EXIT_GOAL_NOT_SATISFIED
            )

        mutation_kind, action_ids = batch
        signature = (mutation_kind, action_ids)
        if signature in seen_batches:
            return _emit_fatal_error(
                "purge",
                RuntimeError(
                    "执行后的重新扫描仍返回完全相同的动作批次；"
                    "为避免重复修改，批量清理已停止。"
                ),
                json_output=args.json,
                stdout=stdout,
                stderr=stderr,
            )
        seen_batches.add(signature)

        if not args.json:
            stdout.write(
                f"批次 {batch_number}：{_ACTION_LABELS.get(mutation_kind, mutation_kind)} "
                f"{len(action_ids)} 个目标。\n"
            )
        batch_args = argparse.Namespace(**vars(args))
        batch_args.command = "clean"
        batch_args.action_id = list(action_ids)
        batch_args.thread_id = []
        batch_args.plan_fingerprint = plan.plan_fingerprint

        batch_stdout = StringIO() if args.json else stdout
        batch_stderr = StringIO() if args.json else stderr
        result = _run_planned_cleanup(
            batch_args,
            scan_report=current_report,
            active_adapters=active_adapters,
            stdin=stdin,
            stdout=batch_stdout,
            stderr=batch_stderr,
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
            adapter_builder=adapter_builder,
            client_inspector=service.client_inspector,
            cleanup_service=service,
        )
        batch_document = {
            "batch": batch_number,
            "mutation_kind": mutation_kind,
            "action_ids": list(action_ids),
        }
        if args.json:
            batch_document["result"] = _purge_stream_document(batch_stdout)
            error_document = _purge_stream_document(batch_stderr)
            if error_document is not None:
                batch_document["error_output"] = error_document
        completed_batches.append(batch_document)
        if result != EXIT_OK:
            if args.json:
                from .agent_operations import plan_counts, structured_blocker

                counts = plan_counts(
                    findings=current_report.findings,
                    actions=plan.actions,
                )
                _write_json(
                    {
                        "command": "purge",
                        "status": "failed",
                        "goal_status": "unknown",
                        "goal_satisfied": False,
                        "modified": executed_action_count > 0,
                        "exit_code": result,
                        "batch_count": len(completed_batches),
                        "executed_action_count": executed_action_count,
                        "remaining_problem_count": counts[
                            "issue_group_count"
                        ],
                        "remaining_blocked_or_unavailable_action_count": counts[
                            "blocked_group_count"
                        ],
                        "counts": counts,
                        "blockers": [
                            structured_blocker(
                                "batch_outcome_unknown",
                                scope=f"batch:{batch_number}",
                                remediation=(
                                    "Re-scan and verify the selected action IDs "
                                    "before attempting any further mutation."
                                ),
                            )
                        ],
                        "batches": completed_batches,
                    },
                    stdout,
                )
            else:
                stderr.write(
                    f"批量清理在第 {batch_number} 批停止；"
                    "后续目标未执行。\n"
                )
            return result

        executed_action_count += len(action_ids)
        recheck_adapters = (
            list(adapter_builder())
            if adapter_builder is not None
            else active_adapters
        )
        current_report = service.scan(
            recheck_adapters,
            platforms=args.platform,
        ).report

    return _emit_fatal_error(
        "purge",
        RuntimeError("批量清理超过 1024 批，已按安全上限停止。"),
        json_output=args.json,
        stdout=stdout,
        stderr=stderr,
    )


def _next_purge_action_batch(
    actions: Sequence[Any],
) -> tuple[str, tuple[str, ...]] | None:
    from .action_registry import ACTION_REGISTRY, action_capability

    executable = [
        action
        for action in actions
        if bool(getattr(action, "executable", False))
        and action_capability(action.kind).implemented
        and action_capability(action.kind).mutation_family is not None
    ]
    mutation_families = tuple(
        dict.fromkeys(
            capability.mutation_family
            for capability in ACTION_REGISTRY.values()
            if capability.implemented and capability.mutation_family is not None
        )
    )
    for mutation_kind in mutation_families:
        matching = [
            action
            for action in executable
            if action_capability(action.kind).mutation_family == mutation_kind
        ]
        if not matching:
            continue
        if mutation_kind == "repair_legacy_index":
            matching = [min(matching, key=lambda action: str(action.action_id))]
        elif mutation_kind == "remove_desktop_state":
            storage_id = min(str(action.target.storage_id) for action in matching)
            matching = [
                action
                for action in matching
                if str(action.target.storage_id) == storage_id
            ]
        return (
            mutation_kind,
            tuple(sorted(str(action.action_id) for action in matching)),
        )
    return None


def _purge_stream_document(stream: TextIO) -> object | None:
    rendered = stream.getvalue().strip() if hasattr(stream, "getvalue") else ""
    if not rendered:
        return None
    try:
        return json.loads(rendered)
    except json.JSONDecodeError:
        return rendered


def _run_records(
    args: argparse.Namespace,
    *,
    active_adapters: Sequence[FrontendAdapter],
    stdout: TextIO,
    stderr: TextIO,
    pi_catalog_builder: Any | None = None,
    include_pi: bool = True,
    claude_catalog_builder: Any | None = None,
    include_claude: bool = True,
) -> int:
    """Build and render the full read-only session catalog."""

    try:
        catalog: Any | None = None
        conversations: tuple[Any, ...] = ()
        unmapped_sessions: tuple[Any, ...] = ()
        if (_is_exact_pi_platform(args.platform) or _is_exact_claude_platform(args.platform)) and args.thread_id:
            engine_label = "Pi" if _is_exact_pi_platform(args.platform) else "Claude"
            raise ActionSelectionError(
                f"{engine_label} records 不支持 --thread-id；"
                "请列出原生会话后使用 delete --session-id 或 --action-id。",
                kind="native_session_thread_selector_unsupported",
            )
        if not (_is_exact_pi_platform(args.platform) or _is_exact_claude_platform(args.platform)):
            from .inventory import build_session_catalog

            catalog = build_session_catalog(active_adapters)
            platform_conversations = _platform_visible_conversations(
                tuple(catalog.conversations),
                args.platform,
                adapters=active_adapters,
            )
            conversations = _filter_record_conversations(
                platform_conversations,
                tuple(args.thread_id),
            )
            unmapped_sessions = _platform_visible_frontend_sessions(
                tuple(catalog.unmapped_frontend_sessions),
                args.platform,
            )
        pi_catalog = (
            _build_pi_catalog(args, pi_catalog_builder)
            if include_pi
            else None
        )
        claude_catalog = (
            _build_claude_catalog(args, claude_catalog_builder)
            if include_claude
            else None
        )
    except Exception as exc:
        return _emit_fatal_error(
            "records",
            exc,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    if args.json:
        payload = _catalog_payload(catalog) if catalog is not None else {
            "schema_version": 1,
            "records": [],
            "unmapped_frontend_sessions": [],
            "errors": [],
            "count": 0,
        }
        payload["command"] = "records"
        payload["records"] = [
            _object_dict(conversation) for conversation in conversations
        ]
        payload["unmapped_frontend_sessions"] = [
            _object_dict(session) for session in unmapped_sessions
        ]
        payload["count"] = len(conversations)
        if pi_catalog is not None:
            payload.update(_pi_catalog_payload(pi_catalog))
        if claude_catalog is not None:
            payload.update(_claude_catalog_payload(claude_catalog))
        payload["total_count"] = len(conversations) + int(
            payload.get("pi_count", 0)
        ) + int(payload.get("claude_count", 0))
        # JSON is deliberately never truncated by --limit.
        _write_json(payload, stdout)
    else:
        if catalog is not None:
            _write_records_catalog(
                catalog,
                conversations=conversations,
                stdout=stdout,
                stderr=stderr,
                limit=args.limit,
                unmapped_sessions=unmapped_sessions,
            )
        if pi_catalog is not None:
            _write_pi_session_catalog(pi_catalog, stdout=stdout, stderr=stderr, limit=args.limit)
        if claude_catalog is not None:
            _write_claude_session_catalog(
                claude_catalog, stdout=stdout, stderr=stderr, limit=args.limit
            )
    catalog_failures = tuple(catalog.failures) if catalog is not None else ()
    pi_failures = tuple(getattr(pi_catalog, "failures", getattr(pi_catalog, "errors", ())) or ())
    claude_failures = tuple(getattr(claude_catalog, "failures", getattr(claude_catalog, "errors", ())) or ())
    return EXIT_ERROR if catalog_failures or pi_failures or claude_failures else EXIT_OK


def _build_pi_catalog(args: argparse.Namespace, builder: Any | None = None) -> Any:
    """Call Pi's public catalog builder; CLI never parses transcripts itself."""

    if builder is not None:
        # Preserve the original injected-builder call contract.
        return builder(
            agent_dir=args.pi_agent_dir,
            session_root=args.pi_session_dir,
        )

    from .pi_sessions import (
        build_pi_root_qualified_catalog,
        build_pi_session_inventory,
    )

    appdata = (args.appdata or default_appdata()).expanduser()
    cindy_profiles = resolve_cindy_profiles(
        appdata,
        root=args.cindy_root,
        database=args.cindy_db,
        codex_home=args.cindy_codex_home,
    )

    if args.pi_agent_dir is not None or args.pi_session_dir is not None:
        # Explicit path options deliberately limit the view to one user-chosen
        # Pi storage rather than unexpectedly adding Cindy roots.  They do not
        # bypass ownership qualification when that one root belongs to Cindy.
        return build_pi_root_qualified_catalog(
            agent_dir=args.pi_agent_dir,
            session_root=args.pi_session_dir,
            cindy_profiles=cindy_profiles,
        )
    return build_pi_session_inventory(
        standalone_options={},
        cindy_profiles=cindy_profiles,
    )


def _pi_catalog_payload(catalog: Any) -> dict[str, Any]:
    raw = _object_dict(catalog)
    sessions = raw.get("records", raw.get("sessions", ()))
    failures = raw.get("errors", raw.get("failures", ()))
    frontend_only = raw.get("frontend_only_references", ())
    return {
        "pi_sessions": list(sessions),
        "pi_failures": list(failures),
        "pi_frontend_only_references": list(frontend_only),
        "pi_count": len(sessions),
    }


def _build_claude_catalog(args: argparse.Namespace, builder: Any | None = None) -> Any:
    """Build Claude's root-qualified inventory without parsing transcripts here."""

    if builder is not None:
        return builder(claude_config_dir=args.claude_config_dir)

    from .cindy_references import (
        CindyReferenceFailure,
        build_cindy_reference_catalog,
    )
    from .claude_sessions import (
        build_claude_multi_root_catalog,
        resolve_claude_paths,
    )
    from .discovery import CindyProfile

    effective = resolve_claude_paths(config_dir=args.claude_config_dir)
    roots: list[Path] = [effective.config_dir]
    explicit_root = args.claude_config_dir is not None
    appdata = (args.appdata or default_appdata()).expanduser()
    profiles: list[Any] = list(
        resolve_cindy_profiles(
            appdata,
            root=args.cindy_root,
            database=args.cindy_db,
            codex_home=args.cindy_codex_home,
        )
    )
    if (
        args.cindy_root is None
        and args.cindy_db is None
        and args.cindy_codex_home is None
    ):
        known_profile_roots = {
            _path_identity(profile.root) for profile in profiles
        }
        for brand in ("CindyGlobal", "Cindy", "CindyDev", "xdt-maker"):
            root = appdata / brand
            if _path_identity(root) in known_profile_roots:
                continue
            try:
                has_claude_home = (root / "claude-home").is_dir()
            except OSError:
                has_claude_home = False
            if has_claude_home:
                # Keep a surviving dedicated Claude store visible even if the
                # frontend DB and codex-home have already been removed.
                profiles.append(
                    CindyProfile(
                        root=root,
                        database=root / "cindy-local-v1.db",
                        codex_home=root / "codex-home",
                    )
                )

    default_root = Path.home() / ".claude"
    qualified_references: list[dict[str, Any]] = []
    reference_failures: list[Any] = []
    seen_databases: set[str] = set()
    for profile in profiles:
        database_key = _path_identity(profile.database)
        if database_key in seen_databases:
            continue
        seen_databases.add(database_key)
        reference_catalog = build_cindy_reference_catalog(
            profile.database, profile_root=profile.root
        )
        cc_references = tuple(reference_catalog.for_backend("claude"))
        dedicated_root = profile.root / "claude-home"
        try:
            dedicated_exists = dedicated_root.is_dir()
        except OSError as exc:
            dedicated_exists = False
            reference_failures.append(
                CindyReferenceFailure(
                    profile.database,
                    profile.root,
                    type(exc).__name__,
                    "Could not inspect Cindy Claude storage root",
                )
            )

        if dedicated_exists and not explicit_root:
            # A surviving dedicated store is inventory-worthy even when it is
            # stale.  It does not, by itself, decide where production Cindy's
            # references belong.
            roots.append(dedicated_root)

        target_root: Path | None
        production_profile = profile.root.name.casefold() in {"cindyglobal", "cindy"}
        if production_profile and effective.config_dir_source in {"default", "environment"}:
            # Production Cindy always uses Claude's ordinary default root.
            # The inherited CLAUDE_CONFIG_DIR, when present, is the effective
            # shared root.  A stale profile/claude-home must never steal refs.
            target_root = effective.config_dir
            if cc_references and not explicit_root:
                roots.append(target_root)
        elif production_profile and _path_identity(effective.config_dir) == _path_identity(default_root):
            # An explicitly supplied literal default root is still uniquely
            # attributable to production Cindy.
            target_root = default_root
        elif production_profile:
            target_root = None
            if cc_references:
                reference_failures.append(
                    CindyReferenceFailure(
                        profile.database,
                        profile.root,
                        "AmbiguousStorageRoot",
                        "Explicit Claude config root cannot be uniquely attributed to production Cindy",
                    )
                )
        elif dedicated_exists:
            target_root = dedicated_root
        else:
            target_root = None
            if cc_references:
                reference_failures.append(
                    CindyReferenceFailure(
                        profile.database,
                        profile.root,
                        "AmbiguousStorageRoot",
                        "Cindy Claude references have no unique Claude config root",
                    )
                )

        for failure in reference_catalog.failures:
            if target_root is None:
                reference_failures.append(failure)
            else:
                reference_failures.append(
                    {
                        **failure.to_dict(),
                        "config_dir": str(target_root),
                    }
                )
        if target_root is None:
            continue
        if explicit_root and _path_identity(target_root) != _path_identity(effective.config_dir):
            # A user-limited single root must not be decorated with evidence
            # belonging to another config directory.
            continue
        for reference in cc_references:
            qualified = reference.to_dict()
            qualified["claude_config_dir"] = str(target_root)
            qualified_references.append(qualified)

    unique_roots: list[Path] = []
    seen_roots: set[str] = set()
    for root in roots:
        key = _path_identity(root)
        if key not in seen_roots:
            seen_roots.add(key)
            unique_roots.append(root)
    return build_claude_multi_root_catalog(
        unique_roots,
        frontend_references=qualified_references,
        reference_errors=reference_failures,
    )


def _claude_catalog_payload(catalog: Any) -> dict[str, Any]:
    raw = _object_dict(catalog)
    sessions = raw.get("records", raw.get("sessions", ()))
    failures = raw.get("errors", raw.get("failures", ()))
    return {
        "claude_sessions": list(sessions),
        "claude_failures": list(failures),
        "claude_count": len(sessions),
    }


def _storage_kind_explanation(value: object) -> str:
    explanations = {
        "standalone": "独立 Pi Agent 数据目录",
        "cindy": "Cindy 配置目录内的 Pi Agent 数据",
    }
    normalized = _enum_value(value).strip().lower()
    return explanations.get(normalized, "无法确认，需要复核")


def _classification_explanation(value: object) -> str:
    explanations = {
        "unreferenced": "未发现 Cindy 正在使用这个会话",
        "deleted_frontend_reference": (
            "没有活跃 Cindy 会话正在使用；仅有已删除的 Cindy 会话记录"
        ),
        "live_current_reference": "仍被活跃 Cindy 会话当前使用",
        "live_historical_reference": (
            "仍被活跃 Cindy 会话作为历史切换或派生会话保留"
        ),
        "frontend_only": "Cindy 仍有引用，但本地会话文件已不存在",
        "inventory_incomplete": "无法确认 Cindy 是否仍在使用：会话盘点不完整",
    }
    normalized = _enum_value(value).strip().lower()
    return explanations.get(normalized, "无法确认 Cindy 是否仍在使用，需要复核")


def _delete_eligibility_label(classification: object, deletable: bool) -> str:
    if _enum_value(classification).strip().lower() == "frontend_only":
        return "没有文件可删"
    return "可以选择删除" if deletable else "当前不能选择删除"


def _human_backend_blocker(value: object) -> str:
    raw = str(value)
    lowered = raw.lower()
    translations = (
        ("currently references this pi session", "活跃 Cindy 会话当前正在使用这个 Pi 会话"),
        (
            "historically references this pi session",
            "活跃 Cindy 会话仍保留这个 Pi 历史切换或派生会话",
        ),
        (
            "retains a historical reference to this pi session",
            "活跃 Cindy 会话仍保留这个 Pi 历史切换或派生会话",
        ),
        ("current active session", "Pi 客户端将它标记为当前正在写入的会话"),
        ("session id is duplicated", "同一 Pi session ID 对应多个文件，无法唯一选定"),
        (
            "frontend reference has no local claude transcript",
            "Cindy 仍有引用，但本地 Claude 会话文件已不存在",
        ),
        ("live_current_reference", "活跃 Cindy 会话当前正在使用这个 Claude 会话"),
        (
            "live_historical_reference",
            "活跃 Cindy 会话仍保留这个 Claude 历史切换或派生会话",
        ),
        (
            "pi_session_file identifies this as the current active session",
            "Pi 客户端将它标记为当前正在写入的会话",
        ),
        (
            "unable to establish physical identity",
            "无法确认会话文件的真实位置，不能安全确定删除范围",
        ),
        (
            "unable to establish physical ownership",
            "无法确认会话数据目录的真实归属，不能安全确定删除范围",
        ),
    )
    for needle, translated in translations:
        if needle in lowered:
            return safe_single_line(translated, max_width=240)
    if re.search(r"[\u3400-\u9fff]", raw):
        return _human_message(raw)
    return "会话清单存在异常，无法安全确认删除范围"


def _human_backend_error(value: object, *, backend: str) -> str:
    """Keep backend exception codes in JSON while making terminal errors useful."""

    raw = str(value)
    lowered = " ".join(raw.lower().split())
    label = "Pi" if backend == "pi" else "Claude Code"
    translations = (
        ("at least one explicit", f"请明确选择至少一个 {label} 会话"),
        ("must be non-empty strings", "删除目标不能为空"),
        ("must be non-empty", "删除目标不能为空"),
        ("never accepts an all selector", "不支持使用 all 批量删除"),
        ("never accepts all", "不支持使用 all 批量删除"),
        ("matched no action", "未找到对应的可删除会话；请重新查看会话清单"),
        ("matched no pi action", "未找到对应的可删除 Pi 会话；请重新查看会话清单"),
        ("is ambiguous", "该选择对应多个保存位置；请使用完整的删除操作 ID"),
        ("selected more than once", "同一会话被重复选择"),
        ("clients-closed", f"必须先确认 {label} 客户端已关闭"),
        ("clients closed", f"必须先确认 {label} 客户端已关闭"),
        ("fingerprint does not match", "删除计划已经变化；请重新预览后再确认"),
        ("plan fingerprint", "删除计划已经变化；请重新预览后再确认"),
        ("plan changed after approval", "批准后的删除计划已经变化；没有删除任何文件"),
        ("changed after approval", "批准后的会话文件或 Cindy 使用状态已经变化；没有删除任何文件"),
        ("became unsafe", "批准后的会话路径已不再安全；没有删除任何文件"),
        ("changed node type", "批准后的会话路径类型已经变化；没有删除任何文件"),
        ("changed during", "会话文件在安全校验期间发生变化；没有删除任何文件"),
        ("content changed", "会话文件内容在批准后发生变化；没有删除任何文件"),
        ("stat changed", "会话文件状态在批准后发生变化；没有删除任何文件"),
        ("escaped", "会话路径超出批准的数据目录；没有删除任何文件"),
        ("outside", "会话路径超出批准的数据目录；没有删除任何文件"),
        ("symlink", "会话路径包含链接或重定向点；为保护数据没有删除任何文件"),
        ("reparse point", "会话路径包含链接或重定向点；为保护数据没有删除任何文件"),
        ("invalid", "会话清单数据无效，不能安全执行删除"),
    )
    for needle, translated in translations:
        if needle in lowered:
            return safe_single_line(translated, max_width=240)
    if re.search(r"[\u3400-\u9fff]", raw):
        return _human_message(raw)
    return f"{label} 删除安全检查未通过；没有删除任何文件，请重新查看会话清单"


def _delete_result_status_label(value: object) -> str:
    labels = {
        "deleted": "已删除",
        "not_deleted": "未删除",
        "unknown": "删除结果未确认",
    }
    return labels.get(_enum_value(value).strip().lower(), "删除结果未确认")


def _frontend_reference_usage(reference: object) -> str:
    live = bool(_record_field(reference, "is_live", "live", default=False))
    kind = _enum_value(
        _record_field(reference, "reference_kind", default="current")
    ).strip().lower()
    if live and kind in {"historical", "agent_switch", "switch", "parked"}:
        return "仍被活跃 Cindy 会话作为历史切换或派生会话保留"
    if live:
        return "仍被活跃 Cindy 会话当前使用"
    return "没有活跃 Cindy 会话正在使用；仅有已删除的 Cindy 会话记录"


def _write_pi_session_catalog(
    catalog: Any,
    *,
    stdout: TextIO,
    stderr: TextIO,
    limit: int,
) -> None:
    sessions = tuple(sorted(
        getattr(catalog, "sessions", getattr(catalog, "records", ())) or (),
        key=lambda session: _path_identity(
            _record_field(session, "path", default=".")
        ),
    ))
    failures = tuple(getattr(catalog, "failures", getattr(catalog, "errors", ())) or ())
    stdout.write(f"\nPi Agent 会话：{len(sessions)} 条（只读；不显示正文）。\n")
    visible, hidden = _visible_items(sessions, limit)
    for session in visible:
        stdout.write(
            "  - 会话 ID："
            f"{_display_value(_record_field(session, 'session_id', default='-'), max_width=None)}\n"
            "    会话文件："
            f"{_display_value(_record_field(session, 'path', default='-'), max_width=220)}\n"
        )
        timestamp = _record_field(session, "timestamp", default=None)
        cwd = _record_field(session, "cwd", default=None)
        if timestamp:
            stdout.write(f"    时间：{_display_value(timestamp)}\n")
        if cwd:
            stdout.write(f"    工作目录：{_display_value(cwd, max_width=220)}\n")
        storage_kind = _record_field(session, "storage_kind", default="standalone")
        classification = _record_field(
            session, "reference_classification", "classification", default="unreferenced"
        )
        stdout.write(
            f"    存储位置：{_storage_kind_explanation(storage_kind)}\n"
            f"    Cindy 使用情况：{_classification_explanation(classification)}\n"
        )
        profile_root = _record_field(session, "cindy_profile_root", default=None)
        if profile_root:
            stdout.write(f"    Cindy 配置目录：{_display_value(profile_root, max_width=220)}\n")
        deletable = bool(_record_field(session, "deletable", "delete_supported", default=False))
        stdout.write(f"    删除资格：{_delete_eligibility_label(classification, deletable)}\n")
        blockers = tuple(_record_field(session, "blockers", default=()) or ())
        if not deletable:
            seen_reasons: set[str] = set()
            for blocker in blockers or (_classification_explanation(classification),):
                reason = _human_backend_blocker(blocker)
                if reason not in seen_reasons:
                    seen_reasons.add(reason)
                    stdout.write(f"      不能删除原因：{reason}\n")
        action_id = _record_field(session, "action_id", default=None)
        if action_id and deletable:
            stdout.write(
                "    删除操作 ID："
                f"{_display_value(action_id, max_width=None)}\n"
            )
    frontend_only = tuple(getattr(catalog, "frontend_only_references", ()) or ())
    if frontend_only:
        stdout.write(f"  Cindy 中没有对应本地文件的 Pi 会话：{len(frontend_only)} 条。\n")
        visible_refs, hidden_refs = _visible_items(frontend_only, limit)
        for reference in visible_refs:
            stdout.write(
                "    - 会话 ID："
                f"{_display_value(_record_field(reference, 'native_session_id', default='-'), max_width=None)}\n"
                "      Cindy 配置目录："
                f"{_display_value(_record_field(reference, 'profile_root', default='-'), max_width=180)}\n"
                f"      Cindy 使用情况：{_frontend_reference_usage(reference)}\n"
                "      文件情况：本地 Pi 会话文件已不存在\n"
                "      删除资格：没有文件可删\n"
            )
        if hidden_refs:
            stdout.write(f"    ... 另有 {hidden_refs} 条没有本地文件的 Cindy 引用未显示。\n")
    if hidden:
        stdout.write(f"  ... 另有 {hidden} 条 Pi 会话未显示；请使用 --json 或增大 --limit。\n")
    for failure in failures:
        stderr.write(
            "错误：Pi 会话清单读取失败："
            f"{_human_backend_blocker(_record_field(failure, 'message', default=failure))}\n"
        )


def _write_claude_session_catalog(
    catalog: Any,
    *,
    stdout: TextIO,
    stderr: TextIO,
    limit: int,
) -> None:
    sessions = tuple(
        sorted(
            getattr(catalog, "sessions", getattr(catalog, "records", ())) or (),
            key=lambda session: (
                _path_identity(_record_field(session, "config_dir", default=".")),
                str(_record_field(session, "session_id", default="")),
            ),
        )
    )
    failures = tuple(getattr(catalog, "failures", getattr(catalog, "errors", ())) or ())
    stdout.write(f"\nClaude Code 会话：{len(sessions)} 条（只读；不显示正文）。\n")
    visible, hidden = _visible_items(sessions, limit)
    for session in visible:
        classification = _record_field(
            session, "classification", "reference_classification", default="unreferenced"
        )
        stdout.write(
            "  - 会话 ID："
            f"{_display_value(_record_field(session, 'session_id', default='-'), max_width=None)}\n"
            "    Claude 配置目录："
            f"{_display_value(_record_field(session, 'config_dir', default='-'), max_width=220)}\n"
            "    Cindy 使用情况："
            f"{_classification_explanation(classification)}\n"
        )
        transcripts = tuple(_record_field(session, "transcript_paths", default=()) or ())
        stdout.write(f"    本地会话文件：{len(transcripts)} 份\n")
        deletable = bool(_record_field(session, "deletable", "delete_supported", default=False))
        stdout.write(f"    删除资格：{_delete_eligibility_label(classification, deletable)}\n")
        blockers = tuple(_record_field(session, "blockers", default=()) or ())
        if not deletable:
            seen_reasons: set[str] = set()
            for blocker in blockers or (_classification_explanation(classification),):
                reason = _human_backend_blocker(blocker)
                if reason not in seen_reasons:
                    seen_reasons.add(reason)
                    stdout.write(f"      不能删除原因：{reason}\n")
        action_id = _record_field(session, "action_id", default=None)
        if action_id and deletable:
            stdout.write(
                "    删除操作 ID："
                f"{_display_value(action_id, max_width=None)}\n"
            )
    if hidden:
        stdout.write(f"  ... 另有 {hidden} 条 Claude 会话未显示；请使用 --json 或增大 --limit。\n")
    for failure in failures:
        stderr.write(
            "错误：Claude Code 会话清单读取失败："
            f"{_human_backend_blocker(_record_field(failure, 'message', default=failure))}\n"
        )


def _run_pi_delete(
    args: argparse.Namespace,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    catalog_builder: Any | None,
    delete_executor: Any | None,
    cleanup_service: CleanupService,
) -> int:
    """Handle Pi's separate, file-level delete contract without Codex RPC."""

    try:
        from .pi_delete import build_pi_delete_plan, execute_pi_delete

        catalog = _build_pi_catalog(args, catalog_builder)
        catalog_factory = lambda: _build_pi_catalog(args, catalog_builder)
        cleanup_context = cleanup_service.prepare_session_catalog(
            "pi",
            catalog,
            catalog_builder=catalog_factory,
        )
        plan = cleanup_context.session_native_plan
        assert plan is not None
        if args.thread_id:
            raise ActionSelectionError(
                "--platform pi 请使用 --session-id 或 --action-id，不能使用 --thread-id。",
                kind="pi_selector_required",
            )
        if args.action_id and args.session_id:
            raise ActionSelectionError(
                "Pi 删除中 --action-id 与 --session-id 不能同时使用。",
                kind="conflicting_selectors",
            )
    except Exception as exc:
        return _emit_pi_delete_error(exc, plan=None, json_output=args.json, stdout=stdout, stderr=stderr)

    tty = (
        not args.json
        and bool(getattr(stdin, "isatty", lambda: False)())
        and bool(getattr(stdout, "isatty", lambda: False)())
    )
    selectors = tuple(args.action_id or args.session_id)
    try:
        if not selectors and tty:
            _write_pi_session_catalog(catalog, stdout=stdout, stderr=stderr, limit=args.limit)
            actions, hidden = _visible_items(tuple(plan.executable_actions), args.limit)
            _write_pi_delete_action_catalog(actions, stdout=stdout)
            if hidden:
                stdout.write(f"另有 {hidden} 条 Pi 删除目标未进入本次编号；请增大 --limit。\n")
            if not actions:
                stdout.write("没有可安全删除的 Pi 会话；未做任何更改。\n")
                return EXIT_ERROR if tuple(plan.errors) else EXIT_OK
            stdout.write("\n请输入要永久删除的 Pi 会话编号（例如 1,3-5；不支持 all）：")
            stdout.flush()
            raw = stdin.readline()
            if raw == "":
                stdout.write("\n输入已结束，已取消；未做任何更改。\n")
                return EXIT_OK
            numbers = parse_number_selection(raw, item_count=len(actions))
            selectors = tuple(str(actions[index - 1].action_id) for index in numbers)
        elif not selectors:
            raise ActionSelectionError(
                "Pi 删除不支持 all 或隐式批量选择；请使用 --session-id、--action-id 或 TTY 编号。",
                kind="selection_required",
            )
        selected_plan = plan.with_selected_actions(selectors)
    except Exception as exc:
        return _emit_pi_delete_error(exc, plan=plan, json_output=args.json, stdout=stdout, stderr=stderr)

    plan_rendered = False
    if not args.yes:
        if args.json:
            _write_json(_pi_delete_preview(selected_plan, confirmation_required=True), stdout)
        else:
            _write_pi_delete_plan(selected_plan, stdout=stdout)
            plan_rendered = True
        if not tty:
            return EXIT_CONFIRMATION_REQUIRED
        stdout.write(f"请输入“{PI_DELETE_CONFIRMATION}”继续，输入其他内容取消：")
        stdout.flush()
        if stdin.readline().strip() != PI_DELETE_CONFIRMATION:
            stdout.write("已取消；未做任何更改。\n")
            return EXIT_OK
        clients_closed = True
    else:
        if not args.clients_closed:
            return _emit_pi_delete_error(
                ActionSelectionError(
                    "使用 --yes 执行 Pi 永久删除时必须同时提供 --clients-closed。",
                    kind="clients_not_closed",
                ),
                plan=selected_plan,
                json_output=args.json,
                stdout=stdout,
                stderr=stderr,
            )
        clients_closed = True

    approved_fingerprint = str(selected_plan.plan_fingerprint or "")
    if not tty:
        if not args.plan_fingerprint:
            return _emit_pi_delete_error(
                ActionSelectionError("非 TTY Pi 删除必须提供 --plan-fingerprint。", kind="fingerprint_required"),
                plan=selected_plan, json_output=args.json, stdout=stdout, stderr=stderr,
            )
        approved_fingerprint = str(args.plan_fingerprint)
        if approved_fingerprint != str(selected_plan.plan_fingerprint):
            return _emit_pi_delete_error(
                ActionSelectionError("Pi 计划指纹与当前精确文件状态不一致；请重新预览。", kind="fingerprint_mismatch"),
                plan=selected_plan, json_output=args.json, stdout=stdout, stderr=stderr,
            )
    elif not args.json and not plan_rendered:
        _write_pi_delete_plan(selected_plan, stdout=stdout)

    try:
        selected_ids = {
            str(action.action_id) for action in selected_plan.actions
        }
        candidates = tuple(
            action
            for action in cleanup_context.plan.actions
            if str(action.action_id) in selected_ids
        )
        outcome = cleanup_service.execute(
            cleanup_context,
            candidates,
            timeout=0.0,
            app_server_factory=None,
            binary_resolver=None,
            session_executor=delete_executor or execute_pi_delete,
            session_preflight_verified=False,
        )
        result = outcome.session_cleanup
        if result is None:
            raise RuntimeError("Pi cleanup service returned no session result")
    except Exception as exc:
        return _emit_pi_delete_error(exc, plan=selected_plan, json_output=args.json, stdout=stdout, stderr=stderr)
    _emit_pi_delete_result(result, selected_plan, json_output=args.json, stdout=stdout)
    return EXIT_OK if not tuple(getattr(result, "not_deleted", ())) and not tuple(getattr(result, "unknown", ())) else EXIT_ERROR


def _pi_delete_preview(plan: Any, *, confirmation_required: bool) -> dict[str, Any]:
    return {
        "command": "delete",
        "platform": "pi",
        "confirmation_required": confirmation_required,
        "plan": _object_dict(plan),
        "plan_fingerprint": str(getattr(plan, "plan_fingerprint", "") or ""),
        "selected_actions": [_object_dict(action) for action in tuple(plan.actions)],
    }


def _write_pi_delete_plan(plan: Any, *, stdout: TextIO) -> None:
    stdout.write("\nPi 永久删除最终计划（每项只删除一个会话文件）：\n")
    for action in tuple(plan.actions):
        stdout.write(
            "  - 会话 ID：" f"{_display_value(action.session_id, max_width=None)}\n"
            "    会话文件：" f"{_display_value(action.path, max_width=220)}\n"
            "    删除操作 ID：" f"{_display_value(action.action_id, max_width=None)}\n"
        )
    stdout.write(
        "  所选计划指纹：" f"{_display_value(plan.plan_fingerprint, max_width=None)}\n"
        "  永久性：只删除上述 Pi 会话文件；不会修改登录信息、设置或服务端历史。\n"
    )


def _write_pi_delete_action_catalog(actions: Sequence[Any], *, stdout: TextIO) -> None:
    """Number exactly the sequence accepted by the TTY selector."""

    stdout.write("\n可永久删除的 Pi 会话（不支持 all）：\n")
    for index, action in enumerate(actions, start=1):
        stdout.write(
            f"  {index}. 会话 ID：{_display_value(action.session_id, max_width=None)}\n"
            f"     会话文件：{_display_value(action.path, max_width=220)}\n"
            f"     删除操作 ID：{_display_value(action.action_id, max_width=None)}\n"
        )


def _emit_pi_delete_error(error: Exception, *, plan: Any | None, json_output: bool, stdout: TextIO, stderr: TextIO) -> int:
    if json_output:
        payload: dict[str, Any] = {
            "command": "delete", "platform": "pi",
            "error": {"type": type(error).__name__, "kind": str(getattr(error, "kind", "invalid")), "message": str(error)},
        }
        if plan is not None:
            payload.update(_pi_delete_preview(plan, confirmation_required=False))
            payload["error"] = {"type": type(error).__name__, "kind": str(getattr(error, "kind", "invalid")), "message": str(error)}
        _write_json(payload, stdout)
    else:
        stderr.write(f"错误：{_human_backend_error(error, backend='pi')}\n")
    return EXIT_ERROR


def _emit_pi_delete_result(result: Any, plan: Any, *, json_output: bool, stdout: TextIO) -> None:
    if json_output:
        _write_json({**_pi_delete_preview(plan, confirmation_required=False), **_object_dict(result)}, stdout)
        return
    deleted = len(tuple(getattr(result, "deleted", ()) or ()))
    failures = len(tuple(getattr(result, "not_deleted", ()) or ())) + len(tuple(getattr(result, "unknown", ()) or ()))
    stdout.write(f"Pi 永久删除完成：已验证删除 {deleted} 条，未确认或失败 {failures} 条。\n")
    for item in tuple(getattr(result, "results", ()) or ()):
        stdout.write(
            f"  {_delete_result_status_label(item.status)}："
            f"会话 ID {_display_value(item.session_id, max_width=None)}；"
            f"会话文件 {_display_value(item.path, max_width=220)}\n"
        )
        if item.error:
            stdout.write(f"    原因：{_human_backend_error(item.error, backend='pi')}\n")


def _run_claude_delete(
    args: argparse.Namespace,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    catalog_builder: Any | None,
    delete_executor: Any | None,
    cleanup_service: CleanupService,
) -> int:
    """Handle Claude's separate, manifest-level local delete contract."""

    try:
        from .claude_delete import build_claude_delete_plan, execute_claude_delete

        catalog = _build_claude_catalog(args, catalog_builder)
        catalog_factory = lambda: _build_claude_catalog(args, catalog_builder)
        cleanup_context = cleanup_service.prepare_session_catalog(
            "claude",
            catalog,
            catalog_builder=catalog_factory,
        )
        plan = cleanup_context.session_native_plan
        assert plan is not None
        if args.thread_id:
            raise ActionSelectionError(
                "--platform claude 只可使用 --session-id 或 --action-id，"
                "不能使用 --thread-id。",
                kind="claude_thread_selector_unsupported",
            )
        if args.action_id and args.session_id:
            raise ActionSelectionError(
                "Claude 删除中 --action-id 与 --session-id 不能同时使用。",
                kind="conflicting_selectors",
            )
    except Exception as exc:
        return _emit_claude_delete_error(
            exc, plan=None, json_output=args.json, stdout=stdout, stderr=stderr
        )

    tty = (
        not args.json
        and bool(getattr(stdin, "isatty", lambda: False)())
        and bool(getattr(stdout, "isatty", lambda: False)())
    )
    selectors = tuple(args.action_id or args.session_id)
    try:
        if not selectors and tty:
            _write_claude_session_catalog(
                catalog, stdout=stdout, stderr=stderr, limit=args.limit
            )
            actions, hidden = _visible_items(tuple(plan.executable_actions), args.limit)
            _write_claude_delete_action_catalog(actions, stdout=stdout)
            if hidden:
                stdout.write(
                    f"另有 {hidden} 条 Claude 删除目标未进入本次编号；"
                    "请增大 --limit。\n"
                )
            if not actions:
                stdout.write("没有可安全删除的 Claude 会话；未做任何更改。\n")
                return EXIT_ERROR if tuple(plan.errors) else EXIT_OK
            stdout.write(
                "\n请输入要永久删除的 Claude 会话编号"
                "（例如 1,3-5；不支持 all）："
            )
            stdout.flush()
            raw = stdin.readline()
            if raw == "":
                stdout.write("\n输入已结束，已取消；未做任何更改。\n")
                return EXIT_OK
            numbers = parse_number_selection(raw, item_count=len(actions))
            selectors = tuple(str(actions[index - 1].action_id) for index in numbers)
        elif not selectors:
            raise ActionSelectionError(
                "Claude 删除不支持 all 或隐式批量选择；"
                "请使用 --session-id、--action-id 或 TTY 编号。",
                kind="selection_required",
            )
        selected_plan = plan.with_selected_actions(selectors)
    except Exception as exc:
        return _emit_claude_delete_error(
            exc, plan=plan, json_output=args.json, stdout=stdout, stderr=stderr
        )

    plan_rendered = False
    if not args.yes:
        if args.json:
            _write_json(
                _claude_delete_preview(selected_plan, confirmation_required=True), stdout
            )
        else:
            _write_claude_delete_plan(selected_plan, stdout=stdout)
            plan_rendered = True
        if not tty:
            return EXIT_CONFIRMATION_REQUIRED
        stdout.write(
            f"请输入“{CLAUDE_DELETE_CONFIRMATION}”继续，输入其他内容取消："
        )
        stdout.flush()
        if stdin.readline().strip() != CLAUDE_DELETE_CONFIRMATION:
            stdout.write("已取消；未做任何更改。\n")
            return EXIT_OK
        clients_closed = True
    else:
        if not args.clients_closed:
            return _emit_claude_delete_error(
                ActionSelectionError(
                    "使用 --yes 执行 Claude 永久删除时必须同时提供 --clients-closed。",
                    kind="clients_not_closed",
                ),
                plan=selected_plan,
                json_output=args.json,
                stdout=stdout,
                stderr=stderr,
            )
        clients_closed = True

    approved_fingerprint = str(selected_plan.plan_fingerprint or "")
    if not tty:
        if not args.plan_fingerprint:
            return _emit_claude_delete_error(
                ActionSelectionError(
                    "非 TTY Claude 删除必须提供 --plan-fingerprint。",
                    kind="fingerprint_required",
                ),
                plan=selected_plan,
                json_output=args.json,
                stdout=stdout,
                stderr=stderr,
            )
        approved_fingerprint = str(args.plan_fingerprint)
        if approved_fingerprint != str(selected_plan.plan_fingerprint):
            return _emit_claude_delete_error(
                ActionSelectionError(
                    "Claude 计划指纹与当前 session manifest 不一致；请重新预览。",
                    kind="fingerprint_mismatch",
                ),
                plan=selected_plan,
                json_output=args.json,
                stdout=stdout,
                stderr=stderr,
            )
    elif not args.json and not plan_rendered:
        _write_claude_delete_plan(selected_plan, stdout=stdout)

    try:
        selected_ids = {
            str(action.action_id) for action in selected_plan.actions
        }
        candidates = tuple(
            action
            for action in cleanup_context.plan.actions
            if str(action.action_id) in selected_ids
        )
        outcome = cleanup_service.execute(
            cleanup_context,
            candidates,
            timeout=0.0,
            app_server_factory=None,
            binary_resolver=None,
            session_executor=delete_executor or execute_claude_delete,
            session_preflight_verified=False,
        )
        result = outcome.session_cleanup
        if result is None:
            raise RuntimeError("Claude cleanup service returned no session result")
    except Exception as exc:
        return _emit_claude_delete_error(
            exc,
            plan=selected_plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    _emit_claude_delete_result(
        result, selected_plan, json_output=args.json, stdout=stdout
    )
    return (
        EXIT_OK
        if not tuple(getattr(result, "not_deleted", ()))
        and not tuple(getattr(result, "unknown", ()))
        else EXIT_ERROR
    )


def _claude_delete_preview(plan: Any, *, confirmation_required: bool) -> dict[str, Any]:
    return {
        "command": "delete",
        "platform": "claude",
        "confirmation_required": confirmation_required,
        "plan": _object_dict(plan),
        "plan_fingerprint": str(getattr(plan, "plan_fingerprint", "") or ""),
        "selected_actions": [_object_dict(action) for action in tuple(plan.actions)],
        "shared_records_preserved": True,
    }


def _write_claude_delete_plan(plan: Any, *, stdout: TextIO) -> None:
    stdout.write("\nClaude Code 永久删除最终计划（精确文件清单）：\n")
    for action in tuple(plan.actions):
        stdout.write(
            f"  - 会话 ID：{_display_value(action.session_id, max_width=None)}\n"
            f"    Claude 配置目录：{_display_value(action.config_dir, max_width=220)}\n"
            f"    将删除的会话专属文件或目录：{len(tuple(action.manifest))} 个\n"
            f"    删除操作 ID：{_display_value(action.action_id, max_width=None)}\n"
        )
    stdout.write(
        f"  所选计划指纹：{_display_value(plan.plan_fingerprint, max_width=None)}\n"
        "  共享数据保留：登录信息、设置、插件、技能、代理、命令、"
        "项目记忆、CLAUDE.md、统计缓存、共享历史记录和索引均不修改。\n"
    )


def _write_claude_delete_action_catalog(actions: Sequence[Any], *, stdout: TextIO) -> None:
    stdout.write("\n可永久删除的 Claude Code 会话（不支持 all）：\n")
    for index, action in enumerate(actions, start=1):
        stdout.write(
            f"  {index}. 会话 ID：{_display_value(action.session_id, max_width=None)}\n"
            f"     Claude 配置目录：{_display_value(action.config_dir, max_width=220)}\n"
            f"     删除操作 ID：{_display_value(action.action_id, max_width=None)}\n"
        )


def _emit_claude_delete_error(
    error: Exception,
    *,
    plan: Any | None,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if json_output:
        error_payload = {
            "type": type(error).__name__,
            "kind": str(getattr(error, "kind", "invalid")),
            "message": str(error),
        }
        payload: dict[str, Any] = {
            "command": "delete",
            "platform": "claude",
            "error": error_payload,
        }
        if plan is not None:
            payload.update(
                _claude_delete_preview(plan, confirmation_required=False)
            )
            payload["error"] = error_payload
        _write_json(payload, stdout)
    else:
        stderr.write(f"错误：{_human_backend_error(error, backend='claude')}\n")
    return EXIT_ERROR


def _emit_claude_delete_result(
    result: Any, plan: Any, *, json_output: bool, stdout: TextIO
) -> None:
    if json_output:
        _write_json(
            {
                **_claude_delete_preview(plan, confirmation_required=False),
                **_object_dict(result),
            },
            stdout,
        )
        return
    deleted = len(tuple(getattr(result, "deleted", ()) or ()))
    failures = len(tuple(getattr(result, "not_deleted", ()) or ())) + len(
        tuple(getattr(result, "unknown", ()) or ())
    )
    stdout.write(
        f"Claude Code 永久删除完成：已验证删除 {deleted} 条，"
        f"未确认或失败 {failures} 条；共享记录已保留。\n"
    )
    for item in tuple(getattr(result, "results", ()) or ()):
        stdout.write(
            f"  {_delete_result_status_label(item.status)}："
            f"会话 ID {_display_value(item.session_id, max_width=None)}；"
            "Claude 配置目录 "
            f"{_display_value(item.config_dir, max_width=220)}\n"
        )
        if item.error:
            stdout.write(
                f"    原因：{_human_backend_error(item.error, backend='claude')}\n"
            )


def _has_explicit_pi(platforms: Sequence[str] | None) -> bool:
    return bool(platforms and "pi" in platforms)


def _is_exact_pi_platform(platforms: Sequence[str] | None) -> bool:
    return bool(platforms) and set(platforms) == {"pi"}


def _records_include_pi(platforms: Sequence[str] | None) -> bool:
    return not platforms or "all" in platforms or "pi" in platforms


def _has_explicit_claude(platforms: Sequence[str] | None) -> bool:
    return bool(platforms and "claude" in platforms)


def _is_exact_claude_platform(platforms: Sequence[str] | None) -> bool:
    return bool(platforms) and set(platforms) == {"claude"}


def _records_include_claude(platforms: Sequence[str] | None) -> bool:
    return not platforms or "all" in platforms or "claude" in platforms


def _explicit_unsupported_scan_engine(
    platforms: Sequence[str] | None,
) -> str | None:
    if _has_explicit_pi(platforms):
        return "Pi Agent"
    if _has_explicit_claude(platforms):
        return "Claude Code"
    return None


def _run_manual_delete(
    args: argparse.Namespace,
    *,
    active_adapters: Sequence[FrontendAdapter],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    app_server_factory: AppServerFactory,
    binary_resolver: BinaryResolver,
    adapter_builder: Callable[[], Sequence[FrontendAdapter]] | None = None,
) -> int:
    """Preview and execute explicit, fingerprint-bound manual deletions."""

    try:
        from .inventory import build_session_catalog
        from .manual_delete import (
            build_manual_delete_plan,
            execute_manual_delete,
        )

        catalog = build_session_catalog(active_adapters)
        plan = build_manual_delete_plan(catalog)
        visible_conversations = _platform_visible_conversations(
            tuple(catalog.conversations),
            args.platform,
            adapters=active_adapters,
        )
        visible_unmapped = _platform_visible_frontend_sessions(
            tuple(catalog.unmapped_frontend_sessions),
            args.platform,
        )
        eligible_action_ids = {
            str(conversation.action_id)
            for conversation in visible_conversations
        }
    except Exception as exc:
        return _emit_fatal_error(
            "delete",
            exc,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    tty = (
        not args.json
        and bool(getattr(stdin, "isatty", lambda: False)())
        and bool(getattr(stdout, "isatty", lambda: False)())
    )
    interactive_selection = tty and not args.action_id and not args.thread_id

    try:
        if args.action_id and args.thread_id:
            raise ActionSelectionError(
                "--action-id 与 --thread-id 不能同时使用。",
                kind="conflicting_selectors",
            )
        selectors = tuple(args.action_id or args.thread_id)
        if interactive_selection:
            _write_records_catalog(
                catalog,
                conversations=visible_conversations,
                stdout=stdout,
                stderr=stderr,
                limit=args.limit,
                unmapped_sessions=visible_unmapped,
            )
            available_actions = tuple(
                action
                for action in plan.actions
                if bool(action.available)
                and str(action.action_id) in eligible_action_ids
            )
            selection_actions, hidden_actions = _visible_items(
                available_actions,
                args.limit,
            )
            _write_manual_action_catalog(
                plan,
                actions=selection_actions,
                stdout=stdout,
                limit=0,
            )
            if hidden_actions:
                stdout.write(
                    f"  ... 另有 {hidden_actions} 条目标未进入本次编号；"
                    "请增大 --limit 后再选择。\n"
                )
            if not selection_actions:
                stdout.write("没有可选择的永久删除目标；未做任何更改。\n")
                return EXIT_ERROR if tuple(catalog.failures) else EXIT_OK
            stdout.write(
                "\n请输入要永久删除的编号或范围（例如 1,3-5；不支持 all）："
            )
            stdout.flush()
            raw_selection = stdin.readline()
            if raw_selection == "":
                stdout.write("\n输入已结束，已取消；未做任何更改。\n")
                return EXIT_OK
            numbers = parse_number_selection(
                raw_selection,
                item_count=len(selection_actions),
            )
            selectors = tuple(
                str(action.action_id)
                for index, action in enumerate(selection_actions, start=1)
                if index in numbers
            )
        elif not selectors:
            raise ActionSelectionError(
                "delete 不支持 all 或隐式批量选择；请使用 --thread-id、"
                "--action-id 或 TTY 交互编号逐项选择。",
                kind="selection_required",
            )

        selected_plan = plan.with_selected_actions(selectors)
        outside_view = tuple(
            action
            for action in selected_plan.actions
            if str(action.action_id) not in eligible_action_ids
        )
        if outside_view:
            raise ActionSelectionError(
                "所选目标不属于 --platform 指定的记录视图；"
                "aionui/cindy 视图只允许选择该前端映射的对话。",
                kind="platform_mismatch",
                matches=tuple(str(action.action_id) for action in outside_view),
            )
    except Exception as exc:
        return _emit_manual_delete_error(
            exc,
            catalog=catalog,
            plan=plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    approved_fingerprint = str(selected_plan.plan_fingerprint or "")
    if not args.json:
        _write_manual_delete_plan(selected_plan, stdout=stdout)

    if not args.yes:
        if not tty:
            if args.json:
                _write_manual_delete_preview(
                    selected_plan,
                    catalog=catalog,
                    confirmation_required=True,
                    stdout=stdout,
                )
            else:
                stdout.write(
                    "未做任何更改。非 TTY 执行必须回传以上计划指纹，并同时提供 "
                    "--clients-closed --yes。\n"
                )
            return EXIT_CONFIRMATION_REQUIRED
        stdout.write(
            f"请输入“{MANUAL_DELETE_CONFIRMATION}”继续，输入其他内容取消："
        )
        stdout.flush()
        confirmation = stdin.readline()
        if confirmation.strip() != MANUAL_DELETE_CONFIRMATION:
            stdout.write("已取消；未做任何更改。\n")
            return EXIT_OK
        clients_closed = True
    else:
        if not args.clients_closed:
            return _emit_manual_delete_error(
                ActionSelectionError(
                    "使用 --yes 执行永久删除时必须同时提供 --clients-closed。",
                    kind="clients_not_closed",
                ),
                catalog=catalog,
                plan=selected_plan,
                json_output=args.json,
                stdout=stdout,
                stderr=stderr,
            )
        clients_closed = True
        if not tty:
            if not args.plan_fingerprint:
                return _emit_manual_delete_error(
                    ActionSelectionError(
                        "非 TTY 执行必须提供此前预览得到的 --plan-fingerprint。",
                        kind="fingerprint_required",
                    ),
                    catalog=catalog,
                    plan=selected_plan,
                    json_output=args.json,
                    stdout=stdout,
                    stderr=stderr,
                )
            approved_fingerprint = str(args.plan_fingerprint)
            if approved_fingerprint != str(selected_plan.plan_fingerprint):
                return _emit_manual_delete_error(
                    ActionSelectionError(
                        "计划指纹与当前所选目标、级联范围或引用快照不一致；"
                        "请重新预览。",
                        kind="fingerprint_mismatch",
                    ),
                    catalog=catalog,
                    plan=selected_plan,
                    json_output=args.json,
                    stdout=stdout,
                    stderr=stderr,
                )

    def rebuild_catalog() -> Any:
        latest_adapters = (
            list(adapter_builder())
            if adapter_builder is not None
            else active_adapters
        )
        return build_session_catalog(latest_adapters)

    try:
        report = execute_manual_delete(
            selected_plan,
            catalog_builder=rebuild_catalog,
            approved_plan_fingerprint=approved_fingerprint,
            clients_closed=clients_closed,
            timeout=args.timeout,
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
        )
    except Exception as exc:
        return _emit_manual_delete_error(
            exc,
            catalog=catalog,
            plan=selected_plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    _emit_manual_delete_result(
        report,
        plan=selected_plan,
        json_output=args.json,
        stdout=stdout,
        stderr=stderr,
    )
    return EXIT_OK if report.ok else EXIT_ERROR


def _object_dict(value: Any) -> dict[str, Any]:
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        result = converter()
        if isinstance(result, dict):
            return result
    if isinstance(value, Mapping):
        return dict(value)
    return {
        key: item
        for key, item in vars(value).items()
        if not key.startswith("_")
    }


def _catalog_payload(catalog: Any) -> dict[str, Any]:
    payload = _object_dict(catalog)
    # Keep these documented keys stable even for a third-party catalog
    # implementation whose serializer omits an empty collection.
    payload.setdefault(
        "records",
        [_object_dict(item) for item in catalog.conversations],
    )
    payload.setdefault(
        "unmapped_frontend_sessions",
        [_object_dict(item) for item in catalog.unmapped_frontend_sessions],
    )
    payload.setdefault(
        "errors",
        [_object_dict(item) for item in catalog.failures],
    )
    payload.setdefault("count", len(catalog.conversations))
    return payload


def _filter_record_conversations(
    conversations: Sequence[Any],
    selectors: Sequence[str],
) -> tuple[Any, ...]:
    requested = tuple(selector.strip() for selector in selectors if selector.strip())
    if not requested:
        return tuple(conversations)
    selected: list[Any] = []
    selected_keys: set[tuple[str, str]] = set()
    for selector in requested:
        exact = [
            item for item in conversations if str(item.thread_id) == selector
        ]
        matches = exact or [
            item
            for item in conversations
            if str(item.thread_id).startswith(selector)
        ]
        if not matches:
            raise ActionSelectionError(
                f"未找到对话：{selector}",
                kind="not_found",
                selector=selector,
            )
        for item in matches:
            key = (str(item.codex_home), str(item.thread_id))
            if key not in selected_keys:
                selected.append(item)
                selected_keys.add(key)
    return tuple(selected)


def _platform_visible_conversations(
    conversations: Sequence[Any],
    platforms: Sequence[str] | None,
    *,
    adapters: Sequence[Any] | None = None,
) -> tuple[Any, ...]:
    """Apply source-view semantics without pruning the safety catalog.

    ``native`` and ``all`` expose the complete native inventory for the
    adapters' homes. Cindy's dedicated Codex homes are themselves source
    evidence, while shared-home AionUI records require a frontend mapping.
    The underlying catalog remains complete for deletion guards.
    """

    selected = _selected_platforms(platforms)
    if not platforms or "all" in platforms:
        return tuple(conversations)
    native_homes = {
        _path_identity(getattr(adapter, "codex_home"))
        for adapter in tuple(adapters or ())
        if str(getattr(adapter, "name", "")).lower() == "native"
        and getattr(adapter, "codex_home", None) is not None
    }
    cindy_homes = {
        _path_identity(getattr(adapter, "codex_home"))
        for adapter in tuple(adapters or ())
        if str(getattr(adapter, "name", "")).lower() == "cindy"
        and getattr(adapter, "codex_home", None) is not None
    }
    return tuple(
        conversation
        for conversation in conversations
        if (
            "native" in selected
            and (
                not adapters
                or _path_identity(conversation.codex_home) in native_homes
            )
        )
        or (
            "cindy" in selected
            and _path_identity(conversation.codex_home) in cindy_homes
        )
        or any(
            str(_record_field(session, "platform", default="")).lower()
            in selected
            for session in tuple(getattr(conversation, "frontend_sessions", ()) or ())
        )
    )


def _path_identity(value: Any) -> str:
    return canonical_existing_path_key(Path(value).expanduser())


def _platform_visible_frontend_sessions(
    sessions: Sequence[Any],
    platforms: Sequence[str] | None,
) -> tuple[Any, ...]:
    selected = _selected_platforms(platforms)
    return tuple(
        session
        for session in sessions
        if str(_record_field(session, "platform", default="")).lower()
        in selected
    )


def _write_records_catalog(
    catalog: Any,
    *,
    conversations: Sequence[Any],
    stdout: TextIO,
    stderr: TextIO,
    limit: int,
    unmapped_sessions: Sequence[Any] | None = None,
) -> None:
    unmapped = tuple(
        catalog.unmapped_frontend_sessions
        if unmapped_sessions is None
        else unmapped_sessions
    )
    stdout.write(
        f"会话记录：{len(conversations)} 条 Codex 对话，"
        f"{len(unmapped)} 条未映射前端记录。\n"
    )
    visible, hidden = _visible_items(conversations, limit)
    grouped: dict[str, list[Any]] = {}
    for conversation in visible:
        grouped.setdefault(str(conversation.codex_home), []).append(conversation)
    for codex_home, items in grouped.items():
        stdout.write(
            "\nCodex 数据目录："
            f"{_display_value(codex_home, max_width=220)}\n"
        )
        for conversation in items:
            _write_managed_conversation(conversation, stdout=stdout)
    if hidden:
        stdout.write(
            f"... 另有 {hidden} 条对话未显示；请使用 --json 或增大 --limit。\n"
        )

    if unmapped:
        stdout.write("\n未分配或无效的前端会话映射（不可删除）：\n")
        unmapped_visible, unmapped_hidden = _visible_items(unmapped, limit)
        for session in unmapped_visible:
            stdout.write(
                "  - "
                f"{_display_value(_record_field(session, 'platform', default='frontend'))} "
                f"session={_display_value(_record_field(session, 'platform_session_id', 'session_id', default='-'), max_width=None)} "
                f"status={_display_value(_record_field(session, 'status', default='unknown'))} "
                f"db={_display_value(_record_field(session, 'platform_db', 'database', default='-'), max_width=180)}\n"
            )
        if unmapped_hidden:
            stdout.write(f"  ... 另有 {unmapped_hidden} 条未显示。\n")
    for failure in tuple(catalog.failures):
        stderr.write(
            "错误：清单来源读取失败："
            f"{_display_value(_record_field(failure, 'platform', 'source', default='unknown'))}："
            f"{_human_message(_record_field(failure, 'message', default=failure))}\n"
        )


def _write_managed_conversation(conversation: Any, *, stdout: TextIO) -> None:
    summary = getattr(conversation, "summary", None)
    name = _record_field(
        summary,
        "display_name",
        "name",
        "title",
        default=None,
    )
    project = _record_field(summary, "project_label", default=None)
    cwd = _record_field(summary, "cwd", default=None)
    thread_id = str(conversation.thread_id)
    stdout.write(
        "  - "
        f"{_display_value(name or '(未命名对话)')}"
        f"  [{_display_value(project or '未知项目')}]\n"
        "    ID："
        f"{_display_value(thread_id, max_width=None)}\n"
    )
    if cwd:
        stdout.write(
            "    工作目录："
            f"{_display_value(cwd, max_width=220)}\n"
        )
    rollouts = tuple(getattr(conversation, "rollouts", ()) or ())
    archived = _record_field(summary, "archived", default=None)
    state = [
        f"indexed={'yes' if bool(getattr(conversation, 'indexed', False)) else 'no'}",
        f"legacy_indexed={'yes' if bool(getattr(conversation, 'legacy_indexed', False)) else 'no'}",
        f"rollouts={len(rollouts)}",
        f"archived={archived if archived is not None else 'unknown'}",
        f"artifacts={'yes' if bool(getattr(conversation, 'artifact_present', False)) else 'no'}",
    ]
    stdout.write(f"    Codex 状态：{', '.join(state)}\n")
    originator = _record_field(summary, "originator", default=None)
    if originator:
        stdout.write(f"    来源：{_display_value(originator)}\n")
    descendants = tuple(
        str(value)
        for value in getattr(conversation, "descendant_thread_ids", ())
    )
    if descendants:
        stdout.write(
            f"    永久删除将级联 {len(descendants)} 条关联任务对话："
            f"{', '.join(_display_value(value, max_width=None) for value in descendants)}\n"
        )
    if bool(getattr(conversation, "cascade_unknown", False)):
        stdout.write("    级联范围：无法完整确认，禁止删除。\n")
    frontend_sessions = tuple(
        getattr(conversation, "frontend_sessions", ()) or ()
    )
    for session in frontend_sessions:
        stdout.write(
            "    前端引用（只读保留）："
            f"{_display_value(_record_field(session, 'platform', default='frontend'))} "
            f"session={_display_value(_record_field(session, 'platform_session_id', 'session_id', default='-'), max_width=None)} "
            f"status={_display_value(_record_field(session, 'status', default='unknown'))} "
            f"live={'yes' if bool(_record_field(session, 'is_live', 'live', default=False)) else 'no'}\n"
        )
    blockers = tuple(getattr(conversation, "blockers", ()) or ())
    if blockers:
        stdout.write(
            "    不可删除："
            f"{'; '.join(_human_message(value) for value in blockers)}\n"
        )
    elif bool(getattr(conversation, "delete_supported", False)):
        stdout.write(
            "    删除动作 ID："
            f"{_display_value(getattr(conversation, 'action_id', ''), max_width=None)}\n"
        )
    else:
        stdout.write("    不可删除：没有可验证的 SQLite thread 行或 rollout。\n")


def _record_field(
    value: Any,
    *names: str,
    default: Any = None,
) -> Any:
    if value is None:
        return default
    for name in names:
        if isinstance(value, Mapping) and name in value:
            candidate = value[name]
        else:
            candidate = getattr(value, name, None)
        if candidate not in (None, ""):
            return candidate
    return default


def _write_manual_action_catalog(
    plan: Any,
    *,
    actions: Sequence[Any],
    stdout: TextIO,
    limit: int,
) -> None:
    stdout.write("\n可永久删除的根目标（不支持 all）：\n")
    visible, hidden = _visible_items(actions, limit)
    for index, action in enumerate(visible, start=1):
        stdout.write(
            f"  {index}. {_display_value(action.thread_id, max_width=None)}\n"
            "     Codex 数据目录："
            f"{_display_value(action.codex_home, max_width=220)}\n"
            "     action ID："
            f"{_display_value(action.action_id, max_width=None)}；"
            f"级联范围 {len(tuple(action.affected_thread_ids))} 条\n"
        )
    if hidden:
        stdout.write(
            f"  ... 另有 {hidden} 条目标未显示；请增大 --limit 后再选择。\n"
        )


def _write_manual_delete_plan(plan: Any, *, stdout: TextIO) -> None:
    stdout.write("\n永久删除最终计划：\n")
    for action in tuple(plan.actions):
        stdout.write(
            "  - 根对话："
            f"{_display_value(action.thread_id, max_width=None)}\n"
            "    Codex 数据目录："
            f"{_display_value(action.codex_home, max_width=220)}\n"
            "    完整影响范围："
            f"{', '.join(_display_value(value, max_width=None) for value in action.affected_thread_ids)}\n"
            "    action ID："
            f"{_display_value(action.action_id, max_width=None)}\n"
        )
        frontend_sessions = tuple(action.frontend_sessions)
        if frontend_sessions:
            stdout.write(
                f"    警告：{len(frontend_sessions)} 条 Cindy/AionUI 前端引用"
                "不会被删除，执行后可能成为孤立映射。\n"
            )
            for session in frontend_sessions:
                stdout.write(
                    "      - "
                    f"{_display_value(_record_field(session, 'platform', default='frontend'))} "
                    f"session={_display_value(_record_field(session, 'platform_session_id', 'session_id', default='-'), max_width=None)} "
                    f"db={_display_value(_record_field(session, 'platform_db', 'database', default='-'), max_width=180)}（保留）\n"
                )
    stdout.write(
        "  所选计划指纹："
        f"{_display_value(plan.plan_fingerprint, max_width=None)}\n"
        "  永久性：thread/delete 不可撤销，并会删除以上关联任务对话；"
        "第三方数据库行不会被改写。\n"
    )


def _write_manual_delete_preview(
    plan: Any,
    *,
    catalog: Any,
    confirmation_required: bool,
    stdout: TextIO,
) -> None:
    _write_json(
        {
            "command": "delete",
            "confirmation_required": confirmation_required,
            "plan": _object_dict(plan),
            "plan_fingerprint": str(plan.plan_fingerprint or ""),
            "selected_actions": [
                _object_dict(action) for action in tuple(plan.actions)
            ],
            "catalog_failures": [
                _object_dict(item) for item in tuple(catalog.failures)
            ],
            "third_party_references_deleted": False,
        },
        stdout,
    )


def _emit_manual_delete_error(
    error: Exception,
    *,
    catalog: Any,
    plan: Any,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if json_output:
        _write_json(
            {
                "command": "delete",
                "error": {
                    "type": type(error).__name__,
                    "kind": str(getattr(error, "kind", "invalid")),
                    "message": str(error),
                },
                "plan": _object_dict(plan),
                "plan_fingerprint": str(
                    getattr(plan, "plan_fingerprint", "") or ""
                ),
                "catalog_failures": [
                    _object_dict(item) for item in tuple(catalog.failures)
                ],
                "third_party_references_deleted": False,
            },
            stdout,
        )
    else:
        stderr.write(f"错误：{_human_message(error)}\n")
    return EXIT_ERROR


def _emit_manual_delete_result(
    report: CleanupReport,
    *,
    plan: Any,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    if json_output:
        _write_json(
            {
                "command": "delete",
                "confirmation_required": False,
                "plan_fingerprint": str(plan.plan_fingerprint or ""),
                "selected_actions": [
                    _object_dict(action) for action in tuple(plan.actions)
                ],
                **report.to_dict(),
                "third_party_references_deleted": False,
                "retained_frontend_sessions": [
                    _object_dict(session)
                    for action in tuple(plan.actions)
                    for session in tuple(action.frontend_sessions)
                ],
            },
            stdout,
        )
        return
    stdout.write(
        f"永久删除完成：已验证删除 {report.succeeded} 条根目标，"
        f"未成功 {report.failed} 条。\n"
    )
    action_by_target = {
        (str(action.codex_home), str(action.thread_id)): action
        for action in tuple(plan.actions)
    }
    labels = {
        "deleted": "已删除",
        "not_deleted": "未删除",
        "partial": "部分删除",
        "unknown": "无法确认",
    }
    for result in report.results:
        stdout.write(
            f"  {labels.get(result.status, result.status)} "
            f"{_display_value(result.finding.thread_id, max_width=None)}"
        )
        if result.error:
            stdout.write(f"：{_human_message(result.error)}")
        stdout.write("\n")
        action = action_by_target.get(
            (str(result.finding.codex_home), str(result.finding.thread_id))
        )
        if action is not None and tuple(action.frontend_sessions):
            stdout.write(
                f"    注意：{len(tuple(action.frontend_sessions))} 条第三方前端引用"
                "仍保留，未从 Cindy/AionUI 数据库删除。\n"
            )
        for artifact in result.remaining_artifacts:
            stdout.write(
                "    仍存在："
                f"{_display_value(artifact, max_width=240)}\n"
            )
    _write_scan_errors(
        ScanReport(findings=[], errors=report.scan_errors),
        stderr,
    )


def _run_legacy_index_restore(
    args: argparse.Namespace,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    codex_home = (args.codex_home or default_codex_home()).expanduser()
    tty = (
        not args.json
        and bool(getattr(stdin, "isatty", lambda: False)())
        and bool(getattr(stdout, "isatty", lambda: False)())
    )
    if not args.yes:
        if args.json:
            _write_json(
                {
                    "command": "restore-legacy-index",
                    "confirmation_required": True,
                    "codex_home": str(codex_home),
                    "backup_id": str(args.backup_id),
                },
                stdout,
            )
            return EXIT_CONFIRMATION_REQUIRED
        stdout.write(
            "还原预览：\n"
            "  Codex 数据目录："
            f"{_display_value(codex_home, max_width=220)}\n"
            "  备份 ID："
            f"{_display_value(args.backup_id, max_width=None)}\n"
            "  条件：当前索引哈希必须仍等于该备份记录的修复后哈希；"
            "还原前会再创建一份备份。\n"
        )
        if not tty:
            stdout.write(
                "未做任何更改。关闭相关客户端后，使用同一命令加上 "
                "--clients-closed --yes 执行。\n"
            )
            return EXIT_CONFIRMATION_REQUIRED
        stdout.write(
            "请输入“客户端已关闭并确认还原”继续，输入其他内容取消："
        )
        stdout.flush()
        confirmation = stdin.readline()
        if confirmation.strip() != "客户端已关闭并确认还原":
            stdout.write("已取消；未做任何更改。\n")
            return EXIT_OK
    elif not args.clients_closed:
        error = LegacyIndexError(
            "--yes restore requires --clients-closed after all related clients are closed"
        )
        return _emit_legacy_index_error(
            command="restore-legacy-index",
            error=error,
            action=None,
            plan=None,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    try:
        result = restore_legacy_index(
            codex_home,
            backup_id=args.backup_id,
        )
    except LegacyIndexError as exc:
        return _emit_legacy_index_error(
            command="restore-legacy-index",
            error=exc,
            action=None,
            plan=None,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    _emit_legacy_restore_result(
        result,
        json_output=args.json,
        stdout=stdout,
    )
    return EXIT_OK


def _run_planned_cleanup(
    args: argparse.Namespace,
    *,
    scan_report: ScanReport,
    active_adapters: Sequence[FrontendAdapter],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    app_server_factory: AppServerFactory,
    binary_resolver: BinaryResolver,
    adapter_builder: Callable[[], Sequence[FrontendAdapter]] | None = None,
    client_inspector: ClientInspector | None = None,
    cleanup_service: CleanupService | None = None,
) -> int:
    from .planning import normalize_storage_path

    service = cleanup_service or CleanupService(
        client_inspector=client_inspector,
    )

    try:
        plan = service.prepare_report(
            scan_report,
            active_adapters=active_adapters,
            platforms=args.platform,
            adapter_builder=adapter_builder,
        ).plan
    except Exception as exc:
        return _emit_fatal_error(
            "clean",
            exc,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    tty = (
        not args.json
        and bool(getattr(stdin, "isatty", lambda: False)())
        and bool(getattr(stdout, "isatty", lambda: False)())
    )
    interactive = tty and not args.action_id and not args.thread_id
    try:
        if args.action_id or args.thread_id:
            selected_actions = select_candidate_actions(
                plan.actions,
                action_ids=args.action_id,
                thread_selectors=args.thread_id,
            )
        elif interactive:
            _write_action_catalog(plan, stdout=stdout, limit=args.limit)
            stdout.write(
                "\n请输入要加入计划的动作编号或范围"
                "（例如 1,3-5；all 仅选择低风险动作）："
            )
            stdout.flush()
            raw_selection = stdin.readline()
            if raw_selection == "":
                stdout.write("\n输入已结束，已取消；未做任何更改。\n")
                return EXIT_OK
            numbers = parse_action_selection(
                raw_selection,
                risks=tuple(_enum_value(action.risk) for action in plan.actions),
                requires_explicit_selection=tuple(
                    bool(
                        getattr(
                            action,
                            "requires_explicit_selection",
                            False,
                        )
                    )
                    for action in plan.actions
                ),
            )
            selected_actions = [
                action for index, action in enumerate(plan.actions, start=1)
                if index in numbers
            ]
            if raw_selection.strip().lower() == "all":
                selected_actions = [
                    action
                    for action in selected_actions
                    if action.available
                    and _enum_value(action.kind) == "delete_conversation"
                ]
        elif args.yes:
            raise ActionSelectionError(
                "--yes 只跳过最终确认；执行前仍须使用 --thread-id、"
                "--action-id 或交互编号明确选择目标。",
                kind="selection_required",
            )
        else:
            # Preserve the useful non-interactive dry-run behavior without
            # silently broadening it to actions that retain chat content.
            selected_actions = [
                action
                for action in plan.actions
                if _enum_value(action.risk) == "low"
                and action.available
                and _enum_value(action.kind) == "delete_conversation"
                and not bool(
                    getattr(
                        action,
                        "requires_explicit_selection",
                        False,
                    )
                )
            ]
    except (ActionSelectionError, NumberSelectionError) as exc:
        if not interactive and not args.json:
            _emit_plan_catalog(
                plan,
                selected_actions=(),
                confirmation_required=False,
                json_output=args.json,
                limit=args.limit,
                stdout=stdout,
            )
        return _emit_action_selection_error(
            exc,
            plan=plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    conflict = _conflicting_action_decision(selected_actions)
    if conflict is not None:
        return _emit_action_selection_error(
            conflict,
            plan=plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    unavailable = [
        action
        for action in selected_actions
        if not action.available
        or _enum_value(action.kind)
        not in {
            "delete_conversation",
            "repair_legacy_index",
            "remove_desktop_state",
            "remove_frontend_reference",
            "keep",
        }
    ]
    if unavailable:
        reason = "；".join(
            f"{action.action_id}："
            f"{_human_unavailable_reason(action)}"
            for action in unavailable
        )
        if not args.json and not interactive:
            _emit_plan_catalog(
                plan,
                selected_actions=selected_actions,
                confirmation_required=False,
                json_output=False,
                limit=args.limit,
                stdout=stdout,
            )
        return _emit_action_selection_error(
            ActionSelectionError(
                f"所选动作中包含不可执行项：{reason}",
                kind="unavailable",
                matches=[str(action.action_id) for action in unavailable],
            ),
            plan=plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    approval_bound_actions = [
        action
        for action in selected_actions
        if _enum_value(action.kind) != "keep"
        and _enum_value(action.risk) in {"review", "high"}
    ]
    approval_plan_fingerprint = ""
    if args.yes and not tty and approval_bound_actions:
        approval_plan_fingerprint = str(
            getattr(args, "plan_fingerprint", "") or ""
        ).strip()
        current_fingerprint = str(
            getattr(plan, "plan_fingerprint", "") or ""
        )
        if not approval_plan_fingerprint:
            return _emit_action_selection_error(
                ActionSelectionError(
                    "非交互执行需复核的动作时，必须提供此前 clean --json "
                    "展示的 --plan-fingerprint。",
                    kind="approval_binding_required",
                    matches=[
                        str(action.action_id)
                        for action in approval_bound_actions
                    ],
                ),
                plan=plan,
                json_output=args.json,
                stdout=stdout,
                stderr=stderr,
            )
        if approval_plan_fingerprint != current_fingerprint:
            return _emit_action_selection_error(
                ActionSelectionError(
                    "提供的计划指纹与当前计划不一致；请重新运行 clean --json "
                    "并复核完整影响范围。",
                    kind="plan_fingerprint_mismatch",
                    selector=approval_plan_fingerprint,
                    matches=[current_fingerprint],
                ),
                plan=plan,
                json_output=args.json,
                stdout=stdout,
                stderr=stderr,
            )

    mutation_actions = [
        action
        for action in selected_actions
        if _enum_value(action.kind)
        in {
            "delete_conversation",
            "repair_legacy_index",
            "remove_desktop_state",
            "remove_frontend_reference",
        }
    ]
    mutation_kinds = {
        _enum_value(action.kind)
        for action in mutation_actions
    }
    if len(mutation_kinds) > 1:
        return _emit_action_selection_error(
            ActionSelectionError(
                "原生 thread 删除、旧版聚合索引修复、Desktop 宿主残留"
                "清理和前端引用清理不能混在同一执行中；"
                "请分别复核和执行。",
                kind="mixed_mutation_kinds",
                matches=[
                    str(action.action_id)
                    for action in mutation_actions
                ],
            ),
            plan=plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    legacy_actions = [
        action
        for action in mutation_actions
        if _enum_value(action.kind) == "repair_legacy_index"
    ]
    desktop_actions = [
        action
        for action in mutation_actions
        if _enum_value(action.kind) == "remove_desktop_state"
    ]
    frontend_actions = [
        action
        for action in mutation_actions
        if _enum_value(action.kind) == "remove_frontend_reference"
    ]
    if len(legacy_actions) > 1:
        return _emit_action_selection_error(
            ActionSelectionError(
                "一次只能修复一个旧版聚合索引文件；请逐个执行。",
                kind="multiple_legacy_repairs",
                matches=[
                    str(action.action_id)
                    for action in legacy_actions
                ],
            ),
            plan=plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    if desktop_actions and len(
        {str(action.target.storage_id) for action in desktop_actions}
    ) > 1:
        return _emit_action_selection_error(
            ActionSelectionError(
                "一次 Desktop 宿主残留清理只能处理一个 Codex 数据目录；"
                "请按数据目录分别执行。",
                kind="multiple_desktop_storages",
                matches=[str(action.action_id) for action in desktop_actions],
            ),
            plan=plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    if frontend_actions and len(
        {
            path
            for action in frontend_actions
            for path in getattr(
                action.impact,
                "frontend_database_paths",
                (),
            )
        }
    ) > 1:
        return _emit_action_selection_error(
            ActionSelectionError(
                "一次前端引用清理只能处理一个物理数据库；请按数据库分别执行。",
                kind="multiple_frontend_storages",
                matches=[str(action.action_id) for action in frontend_actions],
            ),
            plan=plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    if not interactive:
        if not (args.json and args.yes and mutation_actions):
            _emit_plan_catalog(
                plan,
                selected_actions=selected_actions,
                confirmation_required=bool(mutation_actions and not args.yes),
                json_output=args.json,
                limit=args.limit,
                stdout=stdout,
            )
    else:
        _write_selected_action_plan(
            selected_actions,
            plan.storages,
            conversations=getattr(plan, "conversations", ()),
            stdout=stdout,
        )

    if not mutation_actions:
        if not args.json:
            if selected_actions:
                stdout.write("所选对话决定为保留；未做任何更改。\n")
            else:
                stdout.write("没有动作进入执行计划；未做任何更改。\n")
        return EXIT_OK
    if (
        legacy_actions or desktop_actions or frontend_actions
    ) and args.yes and not args.clients_closed:
        mutation_label = (
            "清理 Codex Desktop 宿主残留"
            if desktop_actions
            else "清理前端残留引用"
            if frontend_actions
            else "修复旧版聚合索引"
        )
        return _emit_action_selection_error(
            ActionSelectionError(
                f"使用 --yes {mutation_label}前，必须先关闭使用同一数据目录的 "
                "Codex、AionUI 和 Cindy 客户端，并显式提供 --clients-closed。",
                kind="clients_closed_ack_required",
                matches=[
                    str(action.action_id)
                    for action in (
                        legacy_actions or desktop_actions or frontend_actions
                    )
                ],
            ),
            plan=plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    if not args.yes:
        if tty:
            if legacy_actions:
                stdout.write(
                    "\n请先关闭使用同一数据目录的 Codex、AionUI 和 Cindy。"
                    "修复会创建可验证备份。请输入“客户端已关闭并确认修复”继续，"
                    "输入其他内容取消："
                )
                required_confirmation = "客户端已关闭并确认修复"
            elif desktop_actions:
                stdout.write(
                    "\n请先关闭使用同一数据目录的 Codex/ChatGPT Desktop、"
                    "AionUI 和 Cindy。清理会创建可验证备份。请输入"
                    "“客户端已关闭并确认清理桌面残留”继续，输入其他内容取消："
                )
                required_confirmation = "客户端已关闭并确认清理桌面残留"
            elif frontend_actions:
                stdout.write(
                    "\n请先关闭使用该数据目录的前端客户端。清理只会移除"
                    "已批准的精确残留引用。请输入“客户端已关闭并确认清理前端引用”"
                    "继续，输入其他内容取消："
                )
                required_confirmation = "客户端已关闭并确认清理前端引用"
            else:
                stdout.write(
                    "\n删除不可恢复。请输入“确认删除”继续，"
                    "输入其他内容取消："
                )
                required_confirmation = "确认删除"
            stdout.flush()
            confirmation = stdin.readline()
            if confirmation.strip() != required_confirmation:
                if confirmation == "":
                    stdout.write("\n输入已结束，已取消；未做任何更改。\n")
                else:
                    stdout.write("已取消；未做任何更改。\n")
                return EXIT_OK
        else:
            if not args.json:
                if legacy_actions:
                    stdout.write(
                        "未做任何更改。确认文件、严格清单和输出哈希后，"
                        "关闭相关客户端，再使用同一 action ID、计划指纹、"
                        "--clients-closed 与 --yes 执行。\n"
                    )
                elif desktop_actions:
                    stdout.write(
                        "未做任何更改。复核 Desktop 宿主目录行、全局状态引用"
                        "和计划指纹后，关闭相关客户端，再使用同一 action ID、"
                        "计划指纹、--clients-closed 与 --yes 执行。\n"
                    )
                elif frontend_actions:
                    stdout.write(
                        "未做任何更改。复核前端数据库、精确行/字段和计划指纹后，"
                        "关闭相关客户端，再使用同一 action ID、计划指纹、"
                        "--clients-closed 与 --yes 执行。\n"
                    )
                else:
                    stdout.write(
                        "未做任何更改。确认目标与影响范围后，"
                        "使用同一选择并加上 --yes 执行。\n"
                    )
            return EXIT_CONFIRMATION_REQUIRED

    # Rebuild the structured plan immediately before mutation. Action IDs
    # identify targets; snapshot fingerprints prove their reviewed scope.
    revalidated_adapters = (
        list(adapter_builder())
        if adapter_builder is not None
        else active_adapters
    )
    try:
        revalidated_context = service.prepare(
            revalidated_adapters,
            platforms=args.platform,
            adapter_builder=adapter_builder,
        )
        revalidated_report = revalidated_context.report
        revalidated_plan = revalidated_context.plan
    except Exception as exc:
        return _emit_fatal_error(
            "clean",
            exc,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    if (
        approval_plan_fingerprint
        and str(revalidated_plan.plan_fingerprint)
        != approval_plan_fingerprint
    ):
        return _emit_action_selection_error(
            ActionSelectionError(
                "执行前重新扫描得到的完整计划与已批准计划指纹不一致，"
                "已停止执行；请重新运行 clean --json 并复核。",
                kind="plan_changed",
                selector=approval_plan_fingerprint,
                matches=[str(revalidated_plan.plan_fingerprint)],
            ),
            plan=revalidated_plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    fresh_by_id = {
        str(action.action_id): action
        for action in revalidated_plan.actions
    }
    changed: list[str] = []
    fresh_actions: list[Any] = []
    for action in mutation_actions:
        fresh = fresh_by_id.get(str(action.action_id))
        if (
            fresh is None
            or not fresh.available
            or fresh.snapshot_fingerprint != action.snapshot_fingerprint
        ):
            changed.append(str(action.action_id))
        else:
            fresh_actions.append(fresh)
    if changed:
        message = (
            "删除前重新扫描发现计划已变化，已停止执行。"
            f"请重新查看动作：{', '.join(changed)}"
        )
        return _emit_action_selection_error(
            ActionSelectionError(
                message,
                kind="plan_changed",
                matches=changed,
            ),
            plan=revalidated_plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    try:
        outcome = service.execute(
            revalidated_context,
            fresh_actions,
            timeout=args.timeout,
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
            # Keep these compatibility seams while third-party callers and
            # tests migrate to the shared execution module.
            finding_mapper=_findings_for_actions,
            integrity_approval_builder=_integrity_delete_approvals,
            desktop_fingerprint_resolver=(
                _desktop_state_snapshot_fingerprint
            ),
            cleaner=clean_findings,
        )
    except ExecutionError as exc:
        return _emit_action_selection_error(
            ActionSelectionError(
                str(exc),
                kind=exc.kind,
                matches=list(exc.matches),
            ),
            plan=revalidated_plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    except LegacyIndexError as exc:
        action = fresh_actions[0] if fresh_actions else None
        return _emit_legacy_index_error(
            command="clean",
            error=exc,
            action=action,
            plan=revalidated_plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    except DesktopStateError as exc:
        return _emit_fatal_error(
            "clean",
            exc,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )

    if outcome.legacy_repair is not None:
        _emit_legacy_repair_result(
            outcome.legacy_repair,
            action=fresh_actions[0],
            plan=revalidated_plan,
            json_output=args.json,
            stdout=stdout,
        )
        return EXIT_OK

    if outcome.desktop_cleanup is not None:
        desktop_result = outcome.desktop_cleanup
        if args.json:
            _write_json(outcome.audit_payload(), stdout)
        else:
            stdout.write(
                "Codex Desktop 宿主残留已清理："
                f"{len(desktop_result.thread_ids)} 个 task，"
                f"{desktop_result.deleted_catalog_rows} 条目录记录，"
                f"{desktop_result.removed_global_state_references} 条精确 UI 引用。\n"
                f"可验证备份：{desktop_result.backup_directory}\n"
            )
        return EXIT_OK

    if outcome.frontend_cleanup is not None:
        frontend_result = outcome.frontend_cleanup
        if args.json:
            _write_json(outcome.audit_payload(), stdout)
        else:
            stdout.write(
                "前端残留引用已精确清理："
                f"{frontend_result.removed_reference_count} 条；"
                "临时回滚副本已删除。\n"
            )
        return EXIT_OK

    cleanup_report = outcome.cleanup_report
    assert cleanup_report is not None
    _emit_planned_cleanup_result(
        cleanup_report,
        selected_actions=fresh_actions,
        plan=revalidated_plan,
        json_output=args.json,
        limit=args.limit,
        stdout=stdout,
        stderr=stderr,
    )
    return EXIT_OK if cleanup_report.ok else EXIT_ERROR


def _desktop_state_snapshot_fingerprint(action: Any, plan: Any) -> str:
    observation_ids = {
        str(value) for value in getattr(action, "observation_ids", ())
    }
    matches = [
        observation
        for observation in getattr(plan, "observations", ())
        if str(getattr(observation, "observation_id", ""))
        in observation_ids
        and str(getattr(observation, "finding_type", ""))
        == "desktop_state_orphan"
        and str(getattr(observation.target, "thread_id", ""))
        == str(action.target.thread_id)
    ]
    if len(matches) != 1:
        raise ActionSelectionError(
            "Desktop 宿主残留动作没有唯一的已批准状态快照。",
            kind="evidence_changed",
            matches=[str(action.action_id)],
        )
    fingerprint = matches[0].details.get(
        "desktop_state_snapshot_fingerprint"
    )
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ActionSelectionError(
            "Desktop 宿主残留动作缺少完整状态指纹。",
            kind="evidence_changed",
            matches=[str(action.action_id)],
        )
    return fingerprint


def _integrity_delete_approvals(
    findings: Sequence[Finding],
    actions: Sequence[Any],
    plan: Any,
) -> dict[tuple[str, str], frozenset[str]]:
    observations = {
        str(observation.observation_id): observation
        for observation in plan.observations
    }
    approvals: dict[tuple[str, str], frozenset[str]] = {}
    for finding, action in zip(findings, actions, strict=True):
        if (
            _enum_value(action.risk) != "high"
            or not bool(action.available)
            or _enum_value(action.kind) != "delete_conversation"
        ):
            continue
        target_key = (
            str(action.target.storage_id),
            str(action.target.thread_id),
        )
        action_observation_ids = {
            str(observation_id)
            for observation_id in action.observation_ids
        }
        target_observations = [
            observation
            for observation_id in action.observation_ids
            if (
                (observation := observations.get(str(observation_id)))
                is not None
                and (
                    str(observation.target.storage_id),
                    str(observation.target.thread_id),
                )
                == target_key
            )
        ]
        finding_types = {
            str(observation.finding_type)
            for observation in target_observations
            if str(observation.finding_type)
            in {
                "duplicate_rollout",
                "index_rollout_path_mismatch",
            }
        }
        residual_observations = [
            observation
            for observation in observations.values()
            if (
                (
                    str(observation.target.storage_id),
                    str(observation.target.thread_id),
                )
                == target_key
                and str(observation.finding_type)
                == "residual_spawn_edge"
            )
        ]
        if (
            len(residual_observations) == 1
            and str(residual_observations[0].observation_id)
            in action_observation_ids
            and _residual_delete_approval_is_narrow(
                residual_observations[0],
                action,
            )
        ):
            finding_types.add("residual_spawn_edge")
        if finding_types:
            approvals[finding_key(finding)] = frozenset(finding_types)
    return approvals


def _residual_delete_approval_is_narrow(
    observation: Any,
    action: Any,
) -> bool:
    """Authorize only an exact, artifact-backed residual endpoint delete."""

    details = observation.details
    target_thread_id = str(action.target.thread_id)
    if (
        str(observation.platform).lower() != "native"
        or str(observation.target.thread_id) != target_thread_id
    ):
        return False
    if any(
        details.get(key) is True
        for key in (
            "needs_quarantine",
            "originator_conflict",
            "source_conflict",
            "identity_conflict",
            "metadata_mismatch",
            "active_reference",
            "live_reference_guard",
        )
    ):
        return False
    relation_only = exact_blocker_codes(
        details,
        STANDALONE_RELATION_CLEANUP_UNAVAILABLE,
    )
    impact = action.impact
    has_exact_target_artifact = bool(
        impact.index_record_count or impact.rollout_file_count
    )
    parent_id = details.get("parent_thread_id")
    declared_parent_ids = details.get("source_parent_ids")
    evidence = details.get("subagent_evidence")
    artifact_flag_names = (
        "parent_index_missing",
        "child_index_missing",
        "parent_rollout_present",
        "child_rollout_present",
    )
    declared_parent_set = (
        {
            item
            for item in declared_parent_ids
            if isinstance(item, str) and item
        }
        if isinstance(declared_parent_ids, (list, tuple, set, frozenset))
        else set()
    )
    target_is_indexed = target_thread_id in tuple(
        str(item)
        for item in getattr(impact, "indexed_thread_ids", ())
    )
    return (
        isinstance(parent_id, str)
        and bool(parent_id)
        and details.get("child_thread_id") == target_thread_id
        and isinstance(
            declared_parent_ids,
            (list, tuple, set, frozenset),
        )
        and declared_parent_set <= {parent_id}
        and isinstance(evidence, (list, tuple, set, frozenset))
        and all(
            isinstance(item, str) and bool(item)
            for item in evidence
        )
        and all(
            type(details.get(name)) is bool
            for name in artifact_flag_names
        )
        and isinstance(details.get("edge_status"), str)
        and details["edge_status"].lower() == "closed"
        and details.get("source_conflict") is False
        and details.get("child_index_missing") is (not target_is_indexed)
        and (
            details.get("child_rollout_present") is False
            or bool(impact.rollout_file_count)
        )
        and details.get("thread_delete_supported") is False
        and details.get("cleanable") is False
        and details.get("direct_database_edit_supported") is False
        and relation_only
        and (
            details.get("child_rollout_present") is True
            or details.get("child_index_missing") is False
        )
        and has_exact_target_artifact
    )


def _findings_for_actions(
    actions: Sequence[Any],
    plan: Any,
    findings: Sequence[Finding],
) -> list[Finding]:
    from .planning import normalize_storage_path

    storage_paths = {
        str(storage.storage_id): normalize_storage_path(storage.path)
        for storage in plan.storages
    }
    by_target: dict[tuple[str, str], Finding] = {}
    for finding in findings:
        key = (
            normalize_storage_path(finding.codex_home),
            finding.thread_id,
        )
        by_target.setdefault(key, finding)

    selected: list[Finding] = []
    for action in actions:
        storage_path = storage_paths.get(str(action.target.storage_id))
        finding = by_target.get(
            (storage_path, str(action.target.thread_id))
        )
        if finding is not None:
            selected.append(finding)
    return selected


def _selected_platforms(values: Sequence[str] | None) -> set[str]:
    return selected_platforms(values)


def _filter_supplied_adapters(
    adapters: Iterable[FrontendAdapter],
    platforms: Sequence[str] | None,
) -> list[FrontendAdapter]:
    return filter_supplied_adapters(adapters, platforms)


def _filter_candidate_platforms(
    report: ScanReport,
    platforms: Sequence[str] | None,
) -> ScanReport:
    return filter_candidate_platforms(report, platforms)


def _emit_scan(
    report: ScanReport,
    *,
    json_output: bool,
    limit: int,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    summaries = _scan_conversation_summaries(report.findings)
    if json_output:
        from .planning import storage_id_for_path

        payload = {
            "command": "scan",
            **report.to_dict(),
            "conversations": [
                {
                    "target": {
                        "storage_id": storage_id_for_path(home),
                        "thread_id": thread_id,
                    },
                    "codex_home": home,
                    "summary": summary.to_dict(),
                }
                for (home, thread_id), summary in sorted(
                    summaries.items()
                )
            ],
        }
        _write_json(payload, stdout)
        return
    if report.findings:
        stdout.write(
            f"发现 {len(report.findings)} 个 Codex 对话一致性问题。\n"
        )
        _write_finding_table(
            report.findings,
            summaries=summaries,
            stdout=stdout,
            limit=limit,
        )
    else:
        stdout.write("未发现 Codex 对话一致性问题。\n")
    _write_scan_errors(report, stderr)


def _scan_conversation_summaries(
    findings: Sequence[Finding],
) -> dict[tuple[str, str], Any]:
    from .planning import normalize_storage_path

    ids_by_home: dict[str, set[str]] = {}
    records_by_home: dict[str, dict[str, list[Any]]] = {}
    path_by_home: dict[str, Path] = {}
    for finding in findings:
        if finding.details.get("finding_type") == "legacy_index_only":
            continue
        home = normalize_storage_path(finding.codex_home)
        path_by_home[home] = Path(home)
        ids_by_home.setdefault(home, set()).add(finding.thread_id)
        if finding.rollout is not None:
            records_by_home.setdefault(home, {}).setdefault(
                finding.thread_id,
                [],
            ).append(finding.rollout)
    summaries: dict[tuple[str, str], Any] = {}
    for home, thread_ids in ids_by_home.items():
        try:
            current = read_conversation_summaries(
                path_by_home[home],
                thread_ids,
                rollout_records_by_thread=records_by_home.get(home, {}),
                legacy_names=read_legacy_thread_names(
                    path_by_home[home],
                    thread_ids,
                ),
                strict=False,
            )
        except Exception:
            current = {}
        for thread_id, summary in tuple(current.items()):
            if summary.display_name is not None:
                continue
            desktop_title = next(
                (
                    title
                    for finding in findings
                    if normalize_storage_path(finding.codex_home) == home
                    and finding.thread_id == thread_id
                    and finding.details.get("finding_type")
                    == "desktop_state_orphan"
                    for title in finding.details.get(
                        "desktop_catalog_titles", ()
                    )
                    if isinstance(title, str) and title
                ),
                None,
            )
            if desktop_title is None:
                continue
            current[thread_id] = replace(
                summary,
                title=desktop_title,
                display_name=desktop_title,
                display_name_source=(
                    "codex-desktop.local_thread_catalog"
                ),
                metadata_sources=tuple(
                    dict.fromkeys(
                        (
                            *summary.metadata_sources,
                            "codex-desktop.local_thread_catalog",
                        )
                    )
                ),
            )
        summaries.update(
            ((home, thread_id), summary)
            for thread_id, summary in current.items()
        )
    return summaries


def _write_finding_table(
    findings: Sequence[Finding],
    *,
    summaries: Mapping[tuple[str, str], Any] | None = None,
    stdout: TextIO,
    limit: int,
) -> None:
    if not findings:
        return
    from .planning import normalize_storage_path

    summaries = summaries or {}
    stdout.write(
        f"{'来源':<9} {'对话':<16} {'会话名称':<24} "
        f"{'项目':<18} {'现存数据':<16} 问题\n"
    )
    visible, hidden = _visible_items(findings, limit)
    for finding in visible:
        artifact_state = _artifact_state(finding)
        finding_type = finding.details.get("finding_type")
        if not isinstance(finding_type, str) or not finding_type:
            finding_type = (
                "frontend_deleted_reference"
                if finding.platform.lower() in {"aionui", "cindy"}
                else ""
            )
        summary = summaries.get(
            (
                normalize_storage_path(finding.codex_home),
                finding.thread_id,
            )
        )
        stdout.write(
            f"{_display_value(finding.platform):<9} "
            f"{_short_thread_id(str(finding.thread_id)):<16} "
            f"{_display_value(getattr(summary, 'display_name', None), max_width=22):<24} "
            f"{_display_value(getattr(summary, 'project_label', None), max_width=16):<18} "
            f"{artifact_state:<16} "
            f"{_problem_label(finding_type, finding.reason)}\n"
        )
        if summary is not None:
            stdout.write(
                "          目录："
                f"{_display_value(getattr(summary, 'cwd', None), max_width=220)}\n"
            )
    if hidden:
        stdout.write(
            f"... 另有 {hidden} 个问题未显示；"
            "请使用 --json 或增大 --limit。\n"
        )


def _conversation_lookup(plan: Any) -> dict[tuple[str, str], Any]:
    lookup: dict[tuple[str, str], Any] = {}
    entries = getattr(plan, "conversations", None)
    if entries is None:
        entries = plan if isinstance(plan, (list, tuple)) else ()
    for entry in entries:
        target = getattr(entry, "target", None)
        summary = getattr(entry, "summary", None)
        if target is None or summary is None:
            continue
        lookup[
            (
                str(getattr(target, "storage_id", "")),
                str(getattr(target, "thread_id", "")),
            )
        ] = summary
    return lookup


def _conversation_entry_dict(entry: Any) -> dict[str, Any]:
    serializer = getattr(entry, "to_dict", None)
    if callable(serializer):
        return serializer()
    target = getattr(entry, "target", None)
    summary = getattr(entry, "summary", None)
    return {
        "target": {
            "storage_id": str(getattr(target, "storage_id", "")),
            "thread_id": str(getattr(target, "thread_id", "")),
        },
        "summary": (
            summary.to_dict()
            if callable(getattr(summary, "to_dict", None))
            else None
        ),
    }


def _display_value(
    value: object | None,
    *,
    unknown: str = "未知",
    max_width: int | None = 160,
) -> str:
    rendered = safe_single_line(value, max_width=max_width)
    return rendered if rendered.strip() else unknown


def _state_label(value: object | None) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


def _cwd_identity(value: object | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return os.path.normcase(os.path.normpath(value))


def _write_conversation_summary(
    *,
    stdout: TextIO,
    summary: Any | None,
    thread_id: str,
    relationship: str,
    indent: str,
) -> None:
    """Render all approval-relevant identity fields on logical single lines."""

    display_name = getattr(summary, "display_name", None)
    display_source = getattr(summary, "display_name_source", None)
    project_label = getattr(summary, "project_label", None)
    cwd = getattr(summary, "cwd", None)
    git_origin = getattr(summary, "git_origin_url", None)
    parent_ids = tuple(getattr(summary, "parent_thread_ids", ()) or ())
    conflicts = tuple(getattr(summary, "metadata_conflicts", ()) or ())
    sources = tuple(getattr(summary, "metadata_sources", ()) or ())
    is_subagent = getattr(summary, "is_subagent", None)

    stdout.write(
        f"{indent}Codex thread 名称：{_display_value(display_name)}"
        f"（来源：{_display_value(display_source)}）\n"
    )
    stdout.write(
        f"{indent}项目：{_display_value(project_label)}；"
        f"工作目录：{_display_value(cwd, max_width=220)}\n"
    )
    stdout.write(
        f"{indent}Git 来源：{_display_value(git_origin, max_width=220)}\n"
    )
    stdout.write(
        f"{indent}完整 Codex thread ID："
        f"{_display_value(thread_id, max_width=None)}\n"
    )
    stdout.write(
        f"{indent}关系：{_display_value(relationship)}；"
        f"子代理：{_state_label(is_subagent)}\n"
    )
    stdout.write(
        f"{indent}子代理名称："
        f"{_display_value(getattr(summary, 'agent_nickname', None))}；"
        f"角色：{_display_value(getattr(summary, 'agent_role', None))}；"
        f"路径：{_display_value(getattr(summary, 'agent_path', None), max_width=220)}\n"
    )
    stdout.write(
        f"{indent}父 thread ID："
        f"{_display_value(', '.join(str(value) for value in parent_ids), max_width=220)}\n"
    )
    stdout.write(
        f"{indent}状态：已索引 {_state_label(getattr(summary, 'indexed', None))}；"
        f"已归档 {_state_label(getattr(summary, 'archived', None))}；"
        f"originator "
        f"{_display_value(getattr(summary, 'originator', None))}\n"
    )
    stdout.write(
        f"{indent}元数据来源："
        f"{_display_value(', '.join(str(value) for value in sources), max_width=220)}\n"
    )
    if conflicts:
        rendered_conflicts = "；".join(
            _display_value(value, max_width=180)
            for value in conflicts
        )
        stdout.write(f"{indent}元数据冲突：{rendered_conflicts}\n")
    else:
        stdout.write(f"{indent}元数据冲突：无\n")


def _legacy_observation_for_action(
    action: Any,
    observations: Mapping[str, Any],
) -> Any | None:
    for observation_id in getattr(action, "observation_ids", ()):
        observation = observations.get(str(observation_id))
        if (
            observation is not None
            and str(getattr(observation, "finding_type", ""))
            == "legacy_index_only"
        ):
            return observation
    return None


def _write_action_catalog(
    plan: Any,
    *,
    stdout: TextIO,
    limit: int,
) -> None:
    """Display target cards; ``limit`` counts targets, never action rows."""

    actions = list(plan.actions)
    numbered = {
        str(action.action_id): index
        for index, action in enumerate(actions, start=1)
    }
    observations = {
        str(observation.observation_id): observation
        for observation in plan.observations
    }
    summaries = _conversation_lookup(plan)
    target_keys = list(
        dict.fromkeys(
            (
                str(action.target.storage_id),
                str(action.target.thread_id),
            )
            for action in actions
        )
    )
    visible_targets, hidden = _visible_items(target_keys, limit)

    for storage in plan.storages:
        storage_id = str(storage.storage_id)
        storage_target_keys = [
            key
            for key in visible_targets
            if key[0] == storage_id
        ]
        storage_status = _enum_value(getattr(storage, "scan_status", "ok"))
        storage_errors = tuple(getattr(storage, "errors", ()))
        show_storage_status = (
            storage_status in {"failed", "partial"}
            or bool(storage_errors)
        )
        if not storage_target_keys and not show_storage_status:
            continue
        stdout.write(
            f"\n保存位置：{_display_value(storage.label)}\n"
        )
        stdout.write(
            f"  目录：{_display_value(storage.path, max_width=220)}\n"
        )
        if show_storage_status:
            status_label = {
                "failed": "扫描失败",
                "partial": "扫描不完整",
            }.get(storage_status, storage_status)
            stdout.write(
                f"  状态：{_display_value(status_label)}\n"
            )
            for error in storage_errors:
                stdout.write(
                    f"  此保存位置的错误：{_human_message(error)}\n"
                )

        for target_key in storage_target_keys:
            target_actions = [
                action
                for action in actions
                if (
                    str(action.target.storage_id),
                    str(action.target.thread_id),
                )
                == target_key
            ]
            if not target_actions:
                continue
            thread_id = target_key[1]
            legacy_observation = next(
                (
                    current
                    for action in target_actions
                    if (
                        current := _legacy_observation_for_action(
                            action,
                            observations,
                        )
                    )
                    is not None
                ),
                None,
            )
            stdout.write("\n  目标：\n")
            if legacy_observation is not None:
                details = getattr(legacy_observation, "details", {})
                stdout.write("    资源类型：旧版聚合索引文件（不是会话）\n")
                stdout.write(
                    "    文件："
                    f"{_display_value(details.get('legacy_index_path'), max_width=220)}\n"
                )
                stdout.write(
                    "    条目："
                    f"{_display_value(details.get('entry_count'))} 条；"
                    "待移除残留 ID："
                    f"{_display_value(details.get('residual_thread_count'))} 个；"
                    "重复："
                    f"{_display_value(details.get('duplicate_entry_count'))} 条；"
                    "格式错误："
                    f"{_display_value(details.get('malformed_line_count'))} 行\n"
                )
            else:
                _write_conversation_summary(
                    stdout=stdout,
                    summary=summaries.get(target_key),
                    thread_id=thread_id,
                    relationship="删除候选根 thread",
                    indent="    ",
                )

            reasons = [
                _problem_label(
                    str(getattr(observation, "finding_type", "")),
                    getattr(observation, "reason", ""),
                )
                for action in target_actions
                for observation_id in getattr(action, "observation_ids", ())
                if (
                    observation := observations.get(str(observation_id))
                )
                is not None
            ]
            if reasons:
                stdout.write(
                    "    问题："
                    f"{'；'.join(dict.fromkeys(reasons))}\n"
                )

            stdout.write("    候选动作：\n")
            for action in target_actions:
                number = numbered[str(action.action_id)]
                risk = _enum_value(action.risk)
                action_label = _ACTION_LABELS.get(
                    _enum_value(action.kind),
                    _enum_value(action.kind),
                )
                stdout.write(
                    f"      [{number}] "
                    f"{_display_value(_RISK_LABELS.get(risk, risk))}｜"
                    f"候选动作：{_display_value(action_label)}\n"
                )
                impact = action.impact
                if action.available:
                    stdout.write(
                        "          动作 ID："
                        f"{_display_value(action.action_id, max_width=None)}\n"
                    )
                else:
                    reason = _human_unavailable_reason(action)
                    stdout.write(f"          仅供查看：{reason}\n")
                if bool(
                    getattr(
                        action,
                        "requires_explicit_selection",
                        False,
                    )
                ):
                    stdout.write(
                        "          选择要求：必须逐项选择，"
                        "不会由 all 或默认低风险计划纳入\n"
                    )
                if _enum_value(action.kind) == "delete_conversation":
                    descendants = tuple(
                        str(value)
                        for value in impact.descendant_thread_ids
                    )
                    stdout.write(
                        "          仍存在的数据："
                        f"对话列表记录 {impact.index_record_count} 条，"
                        f"对话内容文件 {impact.rollout_file_count} 个，"
                        "由该对话创建的关联任务对话 "
                        f"{len(descendants)} 条\n"
                    )
                    if descendants:
                        stdout.write("          级联影响明细：\n")
                    for descendant_id in descendants:
                        descendant_key = (storage_id, descendant_id)
                        _write_conversation_summary(
                            stdout=stdout,
                            summary=summaries.get(descendant_key),
                            thread_id=descendant_id,
                            relationship=(
                                "由根 thread "
                                f"{_display_value(thread_id, max_width=None)} "
                                "创建、将一同删除的关联任务 thread"
                            ),
                            indent="            ",
                        )
    if hidden:
        stdout.write(
            f"\n... 另有 {hidden} 个目标未显示；"
            "请使用 --json 或增大 --limit。\n"
        )
    global_errors = tuple(getattr(plan, "errors", ()))
    for error in global_errors:
        stdout.write(
            "\n无法归属到保存位置的计划错误："
            f"{_human_message(error)}\n"
        )
    if not actions:
        stdout.write("\n没有发现候选动作。\n")


def _write_selected_action_plan(
    actions: Sequence[Any],
    storages: Sequence[Any],
    *,
    conversations: Sequence[Any] = (),
    stdout: TextIO,
) -> None:
    storage_by_id = {
        str(storage.storage_id): storage
        for storage in storages
    }
    summary_by_target = _conversation_lookup(conversations)
    stdout.write(f"\n最终计划：{len(actions)} 个动作。\n")
    for action in actions:
        storage = storage_by_id[str(action.target.storage_id)]
        impact = action.impact
        if (
            getattr(action, "resource_kind", "conversation")
            == "legacy_index"
            or _enum_value(action.kind) == "repair_legacy_index"
        ):
            inventory = getattr(action, "legacy_inventory", None)
            stdout.write(
                "- 修复旧版聚合索引（文件资源，不是会话）\n"
                "  动作 ID："
                f"{_display_value(action.action_id, max_width=None)}\n"
                "  保存位置："
                f"{_display_value(storage.label)}"
                "（"
                f"{_display_value(storage.path, max_width=220)}）\n"
                "  文件："
                f"{_display_value(getattr(impact, 'resource_path', None), max_width=220)}\n"
                "  审批快照："
                f"{_display_value(action.snapshot_fingerprint, max_width=None)}\n"
                "  原始 SHA-256："
                f"{_display_value(getattr(impact, 'legacy_original_sha256', None), max_width=None)}\n"
                "  预期 SHA-256："
                f"{_display_value(getattr(impact, 'legacy_expected_sha256', None), max_width=None)}\n"
                "  将移除的残留行："
                f"{getattr(impact, 'legacy_residual_line_count', 0)} 行\n"
            )
            residual_ids = tuple(
                str(value)
                for value in getattr(
                    impact,
                    "legacy_residual_thread_ids",
                    (),
                )
            )
            stdout.write("  将移除的残留 ID：\n")
            for thread_id in residual_ids:
                stdout.write(
                    "    - "
                    f"{_display_value(thread_id, max_width=None)}\n"
                )
            if inventory is not None:
                stdout.write(
                    "  严格清单："
                    f"总行数 {inventory.line_count}；"
                    f"保留 {inventory.expected_line_count}；"
                    f"格式错误 {inventory.malformed_line_count}；"
                    f"live 重复 {inventory.duplicate_live_line_count}\n"
                )
            stdout.write(
                "  安全要求：关闭使用同一数据目录的所有客户端；"
                "执行前会在独占锁内重算清单，先持久化备份和清单，再原子替换。\n"
            )
            continue
        stdout.write(
            f"- {_display_value(_ACTION_LABELS.get(_enum_value(action.kind), _enum_value(action.kind)))}"
            "：对话 "
            f"{_display_value(action.target.thread_id, max_width=None)}\n"
            "  动作 ID："
            f"{_display_value(action.action_id, max_width=None)}\n"
            "  保存位置："
            f"{_display_value(storage.label)}"
            "（"
            f"{_display_value(storage.path, max_width=220)}）\n"
            f"  对话列表记录：{impact.index_record_count} 条；"
            f"对话内容文件：{impact.rollout_file_count} 个\n"
        )
        root_key = (
            str(action.target.storage_id),
            str(action.target.thread_id),
        )
        _write_conversation_summary(
            stdout=stdout,
            summary=summary_by_target.get(root_key),
            thread_id=str(action.target.thread_id),
            relationship="最终计划根 thread",
            indent="  ",
        )
        related = tuple(str(value) for value in impact.descendant_thread_ids)
        root_summary = summary_by_target.get(root_key)
        root_cwd_identity = _cwd_identity(
            getattr(root_summary, "cwd", None)
        )
        cross_project = [
            descendant_id
            for descendant_id in related
            if (
                descendant_summary := summary_by_target.get(
                    (str(action.target.storage_id), descendant_id)
                )
            )
            is not None
            and root_cwd_identity is not None
            and _cwd_identity(getattr(descendant_summary, "cwd", None))
            is not None
            and _cwd_identity(getattr(descendant_summary, "cwd", None))
            != root_cwd_identity
        ]
        if cross_project:
            stdout.write(
                "  警告：级联范围跨项目；请逐项核对以下关联任务 thread："
                + ", ".join(
                    _display_value(value, max_width=None)
                    for value in cross_project
                )
                + "\n"
            )
        stdout.write(
            "  将一同删除的关联任务对话："
            f"{len(related)} 条"
        )
        if related:
            stdout.write(
                "（"
                + ", ".join(
                    _display_value(value, max_width=None)
                    for value in related
                )
                + "）"
            )
        stdout.write("\n")
        for descendant_id in related:
            _write_conversation_summary(
                stdout=stdout,
                summary=summary_by_target.get(
                    (str(action.target.storage_id), descendant_id)
                ),
                thread_id=descendant_id,
                relationship=(
                    "由根 thread "
                    f"{_display_value(action.target.thread_id, max_width=None)} "
                    "创建、将一同删除的关联任务 thread"
                ),
                indent="    ",
            )
        stdout.write(
            "  前端残留引用："
            f"{'保留' if impact.frontend_references_preserved else '不保留'}\n"
        )


def _emit_plan_catalog(
    plan: Any,
    *,
    selected_actions: Sequence[Any],
    confirmation_required: bool,
    json_output: bool,
    limit: int,
    stdout: TextIO,
) -> None:
    if json_output:
        payload = {
            "command": "clean",
            "confirmation_required": confirmation_required,
            **plan.to_dict(),
            "selected_action_ids": [
                str(action.action_id) for action in selected_actions
            ],
        }
        _write_json(payload, stdout)
        return
    _write_action_catalog(plan, stdout=stdout, limit=limit)
    if selected_actions:
        _write_selected_action_plan(
            selected_actions,
            plan.storages,
            conversations=getattr(plan, "conversations", ()),
            stdout=stdout,
        )


def _emit_action_selection_error(
    error: Exception,
    *,
    plan: Any | None = None,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if json_output:
        payload = {
            "command": "clean",
            "blocked": True,
            "error": {
                "type": "action_selection_error",
                "kind": getattr(error, "kind", "invalid"),
                "selector": getattr(error, "selector", ""),
                "matches": list(getattr(error, "matches", ())),
                "message": str(error),
            },
        }
        if plan is not None:
            payload.update(plan.to_dict())
        _write_json(payload, stdout)
    else:
        stderr.write(f"错误：{_human_message(error)}\n")
    return EXIT_ERROR


def _actions_by_cleanup_result(
    *,
    selected_actions: Sequence[Any],
    plan: Any | None,
) -> dict[tuple[str, str], Any]:
    if plan is None:
        counts: dict[str, int] = {}
        for action in selected_actions:
            thread_id = str(action.target.thread_id)
            counts[thread_id] = counts.get(thread_id, 0) + 1
        return {
            ("", str(action.target.thread_id)): action
            for action in selected_actions
            if counts[str(action.target.thread_id)] == 1
        }

    from .planning import normalize_storage_path

    storage_paths = {
        str(storage.storage_id): normalize_storage_path(storage.path)
        for storage in plan.storages
    }
    return {
        (
            storage_paths.get(str(action.target.storage_id), ""),
            str(action.target.thread_id),
        ): action
        for action in selected_actions
    }


def _emit_legacy_index_error(
    *,
    command: str,
    error: LegacyIndexError,
    action: Any | None,
    plan: Any | None,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if json_output:
        error_payload: dict[str, Any] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        if isinstance(error, LegacyIndexOperationError):
            error_payload.update(
                {
                    "state": error.state,
                    "backup_id": error.backup_id,
                    "current_sha256": error.current_sha256,
                }
            )
        payload: dict[str, Any] = {
            "command": command,
            "blocked": True,
            "error": error_payload,
        }
        if action is not None:
            payload["selected_action"] = action.to_dict()
        if plan is not None:
            payload["plan_fingerprint"] = str(
                getattr(plan, "plan_fingerprint", "")
            )
        _write_json(payload, stdout)
    else:
        stderr.write(
            "错误：旧版聚合索引操作已停止："
            f"{_human_message(error)}\n"
        )
        if isinstance(error, LegacyIndexOperationError) and error.backup_id:
            stderr.write(
                "可用备份 ID："
                f"{_display_value(error.backup_id, max_width=None)}；"
                f"当前状态：{_display_value(error.state)}\n"
            )
    return EXIT_ERROR


def _emit_legacy_repair_result(
    result: LegacyIndexRepairResult,
    *,
    action: Any,
    plan: Any,
    json_output: bool,
    stdout: TextIO,
) -> None:
    if json_output:
        _write_json(
            {
                "command": "clean",
                "status": "repaired",
                "action_id": str(action.action_id),
                "selected_action": action.to_dict(),
                "plan_fingerprint": str(plan.plan_fingerprint),
                "repair": result.to_dict(),
            },
            stdout,
        )
        return
    stdout.write(
        "旧版聚合索引修复完成："
        f"移除 {result.removed_line_count} 行，"
        f"涉及 {len(result.removed_thread_ids)} 个残留 ID。\n"
    )
    stdout.write(
        "  动作 ID："
        f"{_display_value(action.action_id, max_width=None)}\n"
        "  文件："
        f"{_display_value(result.index_path, max_width=220)}\n"
        "  备份 ID："
        f"{_display_value(result.backup_id, max_width=None)}\n"
        "  备份文件："
        f"{_display_value(result.backup_path, max_width=220)}\n"
        "  清单："
        f"{_display_value(result.manifest_path, max_width=220)}\n"
    )
    stdout.write("  已移除的残留 ID：\n")
    for thread_id in result.removed_thread_ids:
        stdout.write(
            "    - "
            f"{_display_value(thread_id, max_width=None)}\n"
        )


def _emit_legacy_restore_result(
    result: LegacyIndexRestoreResult,
    *,
    json_output: bool,
    stdout: TextIO,
) -> None:
    if json_output:
        _write_json(
            {
                "command": "restore-legacy-index",
                "status": "restored",
                "restore": result.to_dict(),
            },
            stdout,
        )
        return
    stdout.write(
        "旧版聚合索引已还原。\n"
        "  文件："
        f"{_display_value(result.index_path, max_width=220)}\n"
        "  来源备份 ID："
        f"{_display_value(result.source_backup_id, max_width=None)}\n"
        "  还原前新备份 ID："
        f"{_display_value(result.restore_backup_id, max_width=None)}\n"
        "  还原前备份文件："
        f"{_display_value(result.restore_backup_path, max_width=220)}\n"
    )


def _emit_planned_cleanup_result(
    report: CleanupReport,
    *,
    selected_actions: Sequence[Any],
    plan: Any | None = None,
    json_output: bool,
    limit: int,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    action_lookup = _actions_by_cleanup_result(
        selected_actions=selected_actions,
        plan=plan,
    )
    summary_lookup = _conversation_lookup(plan) if plan is not None else {}
    storage_id_by_path: dict[str, str] = {}
    if plan is not None:
        from .planning import normalize_storage_path

        storage_id_by_path = {
            normalize_storage_path(storage.path): str(storage.storage_id)
            for storage in plan.storages
        }

    def action_for_result(result: Any) -> Any | None:
        if plan is not None:
            normalized_home = normalize_storage_path(
                result.finding.codex_home
            )
            return action_lookup.get(
                (normalized_home, str(result.finding.thread_id))
            )
        return action_lookup.get(("", str(result.finding.thread_id)))

    def affected_conversations(result: Any, action: Any | None) -> list[dict[str, Any]]:
        if action is None or plan is None:
            return []
        storage_id = storage_id_by_path.get(
            normalize_storage_path(result.finding.codex_home),
            str(action.target.storage_id),
        )
        root_id = str(action.target.thread_id)
        affected_ids = tuple(
            str(value)
            for value in getattr(
                action.impact,
                "affected_thread_ids",
                (root_id, *getattr(action.impact, "descendant_thread_ids", ())),
            )
        )
        rendered: list[dict[str, Any]] = []
        for thread_id in affected_ids:
            summary = summary_lookup.get((storage_id, thread_id))
            rendered.append(
                {
                    "relationship": (
                        "root" if thread_id == root_id else "descendant"
                    ),
                    "target": {
                        "storage_id": storage_id,
                        "thread_id": thread_id,
                    },
                    "summary": (
                        summary.to_dict() if summary is not None else None
                    ),
                }
            )
        return rendered

    if json_output:
        report_payload = report.to_dict()
        report_payload["results"] = [
            {
                **result.to_dict(),
                "action_id": (
                    str(action.action_id)
                    if (action := action_for_result(result)) is not None
                    else None
                ),
                "affected_conversations": affected_conversations(
                    result,
                    action,
                ),
            }
            for result in report.results
        ]
        payload = {
            "command": "clean",
            "confirmation_required": False,
            "selected_actions": [
                action.to_dict() for action in selected_actions
            ],
            **report_payload,
        }
        if plan is not None:
            payload["planning_storages"] = [
                (
                    storage.to_dict()
                    if callable(getattr(storage, "to_dict", None))
                    else {
                        "storage_id": str(storage.storage_id),
                        "label": str(storage.label),
                        "path": str(storage.path),
                        "scan_status": _enum_value(
                            getattr(storage, "scan_status", "ok")
                        ),
                        "errors": list(getattr(storage, "errors", ())),
                    }
                )
                for storage in plan.storages
            ]
            payload["planning_errors"] = list(
                getattr(plan, "errors", ())
            )
            payload["planning_conversations"] = [
                _conversation_entry_dict(conversation)
                for conversation in getattr(plan, "conversations", ())
            ]
            payload["plan_fingerprint"] = str(
                getattr(plan, "plan_fingerprint", "")
            )
        _write_json(payload, stdout)
        return

    stdout.write(
        f"清理完成：已验证删除 {report.succeeded} 条，"
        f"未成功 {report.failed} 条，共计划 {len(report.planned)} 条。\n"
    )
    visible = report.results
    hidden = 0
    status_labels = {
        "deleted": "已删除",
        "not_deleted": "未删除",
        "partial": "部分删除",
        "unknown": "无法确认",
    }
    for result in visible:
        action = action_for_result(result)
        status = getattr(
            result,
            "status",
            "deleted" if result.succeeded else "not_deleted",
        )
        stdout.write(
            f"{_display_value(status_labels.get(status, status)):<8} "
            f"{_display_value(result.finding.platform):<8} "
            f"{_short_thread_id(str(result.finding.thread_id))}"
        )
        if result.error:
            stdout.write(f"  {_human_message(result.error)}")
        stdout.write("\n")
        if action is not None:
            stdout.write(
                "          动作 ID："
                f"{_display_value(action.action_id, max_width=None)}\n"
            )
            storage_id = storage_id_by_path.get(
                normalize_storage_path(result.finding.codex_home),
                str(action.target.storage_id),
            )
            root_id = str(action.target.thread_id)
            _write_conversation_summary(
                stdout=stdout,
                summary=summary_lookup.get((storage_id, root_id)),
                thread_id=root_id,
                relationship="清理结果根 thread",
                indent="          ",
            )
            for descendant_id in getattr(
                action.impact,
                "descendant_thread_ids",
                (),
            ):
                descendant_id = str(descendant_id)
                _write_conversation_summary(
                    stdout=stdout,
                    summary=summary_lookup.get(
                        (storage_id, descendant_id)
                    ),
                    thread_id=descendant_id,
                    relationship=(
                        "由根 thread "
                        f"{_display_value(root_id, max_width=None)} "
                        "创建、受本次清理影响的关联任务 thread"
                    ),
                    indent="            ",
                )
        if status == "deleted" and result.request_error:
            stdout.write(
                "          警告：请求曾报错，但磁盘验证确认已删除："
                f"{_human_message(result.request_error)}\n"
            )
        for artifact in result.remaining_artifacts:
            stdout.write(
                "          仍存在："
                f"{_display_value(artifact, max_width=240)}\n"
            )
    if hidden:
        stdout.write(
            f"... 另有 {hidden} 条结果未显示；"
            "请使用 --json 或增大 --limit。\n"
        )
    _write_scan_errors(
        ScanReport(findings=[], errors=report.scan_errors),
        stderr,
    )


def _write_scan_errors(report: ScanReport, stderr: TextIO) -> None:
    for error in report.errors:
        stderr.write(
            f"错误：{_display_value(error.platform)}："
            f"{_display_value(error.error_type)}："
            f"{_human_message(error.message)}\n"
        )


def _emit_selection_error(
    command: str,
    error: ThreadSelectionError,
    *,
    scan_report: ScanReport,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if json_output:
        _write_json(
            {
                "command": command,
                "error": {
                    "type": "thread_selection_error",
                    "kind": error.kind,
                    "selector": error.selector,
                    "matches": list(error.matches),
                    "homes": list(error.homes),
                    "message": str(error),
                },
                "errors": [item.to_dict() for item in scan_report.errors],
            },
            stdout,
        )
    else:
        if error.kind == "not_found":
            message = f"对话选择器“{error.selector}”未匹配任何问题"
        elif error.kind == "ambiguous":
            message = (
                f"对话选择器“{error.selector}”匹配多个目标"
                f"（共 {len(error.matches)} 个）"
            )
        else:
            message = f"对话选择器“{error.selector}”无效"
        if error.matches:
            abbreviated_targets: list[str] = []
            for index, thread_id in enumerate(error.matches):
                target = _short_thread_id(thread_id)
                if index < len(error.homes):
                    target = f"{target} @ {error.homes[index]}"
                abbreviated_targets.append(target)
            abbreviated = ", ".join(abbreviated_targets)
            message = f"{message}：{abbreviated}"
        stderr.write(f"错误：{_human_message(message)}\n")
        _write_scan_errors(scan_report, stderr)
    return EXIT_ERROR


def _emit_fatal_error(
    command: str,
    error: Exception,
    *,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    message = str(error) or repr(error)
    if json_output:
        _write_json(
            {
                "command": command,
                "error": {
                    "type": type(error).__name__,
                    "message": message,
                },
            },
            stdout,
        )
    else:
        stderr.write(
            f"错误：{type(error).__name__}："
            f"{_human_message(message)}\n"
        )
    return EXIT_ERROR


def _artifact_state(finding: Finding) -> str:
    values: list[str] = []
    if finding.rollout is not None:
        values.append("内容文件")
    if finding.codex_indexed:
        values.append("列表记录")
    if finding.details.get("diagnostic_artifact_present") is True:
        values.append("诊断数据")
    return "、".join(values) or "无"


def _problem_label(finding_type: str, fallback: object) -> str:
    """Return stable Chinese wording for a structured cleanup problem."""

    return _PROBLEM_LABELS.get(finding_type, _human_message(fallback))


def _human_unavailable_reason(action: Any) -> str:
    """Translate planner policy reasons without changing JSON fidelity."""

    reason = str(
        getattr(action, "unavailable_reason", "")
        or "当前条件下不能执行"
    )
    kind = _enum_value(getattr(action, "kind", ""))
    lowered = " ".join(reason.lower().split())
    if "not implemented" in lowered:
        return _UNIMPLEMENTED_ACTION_REASONS.get(
            kind,
            "该动作尚未实现，当前仅供查看",
        )
    if lowered.startswith("associated task conversation "):
        match = re.match(
            r"associated task conversation\s+(\S+)",
            reason,
            flags=re.IGNORECASE,
        )
        target = (
            " " + _display_value(match.group(1), max_width=None)
            if match
            else ""
        )
        return _human_message(
            f"关联任务对话{target} 的身份、来源、完整性或级联范围存在异常，"
            "必须单独复核，已阻止父对话删除"
        )
    for needle, label in _BLOCKED_REASON_LABELS:
        if needle in lowered:
            return label
    return _human_message(reason)


def _visible_items(items: Sequence[Any], limit: int) -> tuple[Sequence[Any], int]:
    if limit == 0 or len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


def _short_thread_id(thread_id: str) -> str:
    thread_id = safe_single_line(thread_id, max_width=None)
    if len(thread_id) <= 12:
        return thread_id
    return f"{thread_id[:8]}...{thread_id[-4:]}"


def _enum_value(value: object) -> str:
    raw_value = getattr(value, "value", value)
    return str(raw_value)


def _human_message(value: object) -> str:
    """Normalize relationship terminology in human-readable output."""

    message = str(value)
    if "conflicting codex executable hints" in message.lower():
        return (
            "同一 Codex 数据目录发现多个不同的 Codex 可执行文件；"
            "程序不会猜测，请用 --codex-bin PATH 明确指定"
        )
    for pattern in (
        r"\bspawned descendant threads?\b",
        r"\bspawned descendant conversations?\b",
        r"\bspawned descendants?\b",
        r"\bdescendant threads?\b",
        r"\bdescendants?\b",
        r"后代(?:线程|对话)?",
    ):
        message = re.sub(
            pattern,
            "关联任务对话",
            message,
            flags=re.IGNORECASE,
        )
    return safe_single_line(message, max_width=240)


def _write_json(payload: dict[str, Any], output: TextIO) -> None:
    json.dump(
        payload,
        output,
        ensure_ascii=False,
        indent=2,
        default=_json_default,
    )
    output.write("\n")


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _existing_codex_binary(value: str) -> Path:
    path = Path(value).expanduser()
    try:
        is_file = path.is_file()
    except OSError as exc:
        raise argparse.ArgumentTypeError(
            "无法检查 Codex 可执行文件“"
            f"{safe_single_line(path, max_width=220)}”："
            f"{safe_single_line(exc, max_width=220)}"
        ) from exc
    if not is_file:
        raise argparse.ArgumentTypeError(
            "Codex 可执行文件必须是现存普通文件：“"
            f"{safe_single_line(path, max_width=220)}”"
        )
    return path


__all__ = [
    "ActionSelectionError",
    "DEFAULT_HUMAN_LIMIT",
    "EXIT_CONFIRMATION_REQUIRED",
    "EXIT_ERROR",
    "EXIT_OK",
    "MANUAL_DELETE_CONFIRMATION",
    "NumberSelectionError",
    "build_parser",
    "create_default_adapters",
    "main",
    "parse_action_selection",
    "parse_number_selection",
    "select_candidate_actions",
]
