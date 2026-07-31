from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, TextIO

from .adapters import AionUIAdapter, CindyAdapter
from .adapters.base import FrontendAdapter
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
from .discovery import (
    choose_codex_binary,
    default_appdata,
    default_codex_home,
)
from .models import Finding


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIRMATION_REQUIRED = 2
DEFAULT_HUMAN_LIMIT = 20

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
    "remove_frontend_reference": (
        "清除该前端的残留引用尚未实现；删除 Codex 对话不会清除这条前端记录"
    ),
    "repair_legacy_index": (
        "修复旧版聚合索引尚未实现；聚合索引项不能作为对话删除目标"
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
        prog="codex-session-janitor",
        description=(
            "检查前端残留和 Codex 本地对话状态，生成保守的动作计划，"
            "并通过官方接口安全清理明确选择的目标。"
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

    clean = subparsers.add_parser(
        "clean",
        help="生成动作计划，并通过官方 Codex app-server 清理明确选择的对话",
        description=(
            "生成保守的动作计划，并通过官方 Codex app-server "
            "清理明确选择且重验证通过的对话。"
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
        "--timeout",
        type=_positive_float,
        default=30.0,
        metavar="SECONDS",
        help="app-server 请求超时秒数（默认：30）",
    )
    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--platform",
        action="append",
        choices=("all", "aionui", "cindy", "native"),
        help="选择扫描来源；可重复（默认：all）",
    )
    parser.add_argument(
        "--thread-id",
        action="append",
        default=[],
        metavar="ID_OR_PREFIX",
        help="选择完整对话 ID 或唯一前缀；可重复",
    )
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
            "人类可读输出最多显示的问题数；0 表示全部"
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


def create_default_adapters(args: argparse.Namespace) -> list[FrontendAdapter]:
    appdata = (args.appdata or default_appdata()).expanduser()
    native_codex_home = (args.codex_home or default_codex_home()).expanduser()
    codex_bin = args.codex_bin.expanduser() if args.codex_bin else None
    selected = _selected_platforms(args.platform)
    adapters: list[FrontendAdapter] = []

    if "aionui" in selected:
        aionui_db = args.aionui_db or (
            appdata / "AionUi" / "aionui" / "aionui-backend.db"
        )
        aionui_home = args.aionui_codex_home or native_codex_home
        adapters.append(
            AionUIAdapter(
                database=aionui_db,
                codex_home=aionui_home,
                codex_bin_hint=codex_bin,
            )
        )

    if "cindy" in selected:
        cindy_root = args.cindy_root or (appdata / "CindyGlobal")
        cindy_db = args.cindy_db or (cindy_root / "cindy-local-v1.db")
        cindy_home = args.cindy_codex_home or (cindy_root / "codex-home")
        adapters.append(
            CindyAdapter(
                database=cindy_db,
                codex_home=cindy_home,
                cindy_root=cindy_root,
                codex_bin_hint=codex_bin,
            )
        )

    if "native" in selected:
        try:
            from .adapters import NativeIntegrityAdapter
        except ImportError as exc:
            raise RuntimeError(
                "The native integrity adapter is unavailable in this build"
            ) from exc
        adapters.append(
            NativeIntegrityAdapter(
                codex_home=native_codex_home,
                codex_bin_hint=codex_bin,
            )
        )
    return adapters


def main(
    argv: Sequence[str] | None = None,
    *,
    adapters: Iterable[FrontendAdapter] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    app_server_factory: AppServerFactory = CodexAppServer,
    binary_resolver: BinaryResolver = choose_codex_binary,
) -> int:
    input_stream = stdin or sys.stdin
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "clean":
            if adapters is not None:
                # All supplied adapters participate as live-reference guards;
                # --platform filters candidates only after the protected scan.
                active_adapters = list(adapters)
            else:
                guard_args = argparse.Namespace(**vars(args))
                guard_args.platform = ["all"]
                active_adapters = create_default_adapters(guard_args)
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

    scan_report = scan_adapters(active_adapters)
    if args.command == "clean":
        scan_report = _filter_candidate_platforms(scan_report, args.platform)
        return _run_planned_cleanup(
            args,
            scan_report=scan_report,
            active_adapters=active_adapters,
            stdin=input_stream,
            stdout=output,
            stderr=error_output,
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
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
) -> int:
    from .planning import build_cleanup_plan

    try:
        plan = build_cleanup_plan(scan_report)
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
        not in {"delete_conversation", "keep"}
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

    mutation_actions = [
        action
        for action in selected_actions
        if _enum_value(action.kind) == "delete_conversation"
    ]
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
            stdout=stdout,
        )

    if not mutation_actions:
        if not args.json:
            if selected_actions:
                stdout.write("所选对话决定为保留；未做任何更改。\n")
            else:
                stdout.write("没有动作进入执行计划；未做任何更改。\n")
        return EXIT_OK
    if not args.yes:
        if tty:
            stdout.write(
                "\n删除不可恢复。请输入“确认删除”继续，"
                "输入其他内容取消："
            )
            stdout.flush()
            confirmation = stdin.readline()
            if confirmation.strip() != "确认删除":
                if confirmation == "":
                    stdout.write("\n输入已结束，已取消；未做任何更改。\n")
                else:
                    stdout.write("已取消；未做任何更改。\n")
                return EXIT_OK
        else:
            if not args.json:
                stdout.write(
                    "未做任何更改。确认目标与影响范围后，"
                    "使用同一选择并加上 --yes 执行。\n"
                )
            return EXIT_CONFIRMATION_REQUIRED

    # Rebuild the structured plan immediately before mutation. Action IDs
    # identify targets; snapshot fingerprints prove their reviewed scope.
    revalidated_report = scan_adapters(active_adapters)
    revalidated_report = _filter_candidate_platforms(
        revalidated_report,
        args.platform,
    )
    try:
        revalidated_plan = build_cleanup_plan(revalidated_report)
    except Exception as exc:
        return _emit_fatal_error(
            "clean",
            exc,
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

    selected_findings = _findings_for_actions(
        fresh_actions,
        revalidated_plan,
        revalidated_report.findings,
    )
    if len(selected_findings) != len(fresh_actions):
        return _emit_action_selection_error(
            ActionSelectionError(
                "无法把重新验证的动作唯一映射回扫描证据，已停止执行。",
                kind="evidence_changed",
            ),
            plan=revalidated_plan,
            json_output=args.json,
            stdout=stdout,
            stderr=stderr,
        )
    expected_scopes = {
        finding_key(finding): ExpectedDeletionScope(
            descendant_thread_ids=tuple(
                action.impact.descendant_thread_ids
            ),
            indexed_thread_ids=tuple(
                getattr(action.impact, "indexed_thread_ids", ())
            ),
            rollout_paths=tuple(action.impact.rollout_paths),
            rollout_state_fingerprints=tuple(
                action.impact.rollout_state_fingerprints
            ),
        )
        for finding, action in zip(selected_findings, fresh_actions, strict=True)
    }
    approved_integrity_deletes = _integrity_delete_approvals(
        selected_findings,
        fresh_actions,
        revalidated_plan,
    )
    cleanup_report = clean_findings(
        selected_findings,
        timeout=args.timeout,
        app_server_factory=app_server_factory,
        binary_resolver=binary_resolver,
        explicit_selection=True,
        expected_scopes=expected_scopes,
        approved_integrity_deletes=approved_integrity_deletes,
    )
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
    explicit = details.get("cleanup_blocked_reason")
    relation_only = (
        isinstance(explicit, str)
        and " ".join(explicit.lower().split())
        == (
            "thread/delete does not expose a standalone spawn-edge "
            "cleanup operation."
        )
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
    if not values or "all" in values:
        return {"aionui", "cindy", "native"}
    return set(values)


def _filter_supplied_adapters(
    adapters: Iterable[FrontendAdapter],
    platforms: Sequence[str] | None,
) -> list[FrontendAdapter]:
    supplied = list(adapters)
    if not platforms or "all" in platforms:
        return supplied
    selected = _selected_platforms(platforms)
    return [
        adapter
        for adapter in supplied
        if str(getattr(adapter, "name", "")).lower() in selected
    ]


def _filter_candidate_platforms(
    report: ScanReport,
    platforms: Sequence[str] | None,
) -> ScanReport:
    if not platforms or "all" in platforms:
        return report
    selected = _selected_platforms(platforms)
    return ScanReport(
        findings=[
            finding
            for finding in report.findings
            if finding.platform.lower() in selected
        ],
        # Guard/scanner failures remain global cleanup blockers even when the
        # corresponding platform is not a requested candidate source.
        errors=list(report.errors),
    )


def _emit_scan(
    report: ScanReport,
    *,
    json_output: bool,
    limit: int,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    if json_output:
        payload = {"command": "scan", **report.to_dict()}
        _write_json(payload, stdout)
        return
    if report.findings:
        stdout.write(
            f"发现 {len(report.findings)} 个 Codex 对话一致性问题。\n"
        )
        _write_finding_table(report.findings, stdout=stdout, limit=limit)
    else:
        stdout.write("未发现 Codex 对话一致性问题。\n")
    _write_scan_errors(report, stderr)


def _write_finding_table(
    findings: Sequence[Finding],
    *,
    stdout: TextIO,
    limit: int,
) -> None:
    if not findings:
        return
    stdout.write(f"{'来源':<9} {'对话':<16} {'现存数据':<16} 问题\n")
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
        stdout.write(
            f"{finding.platform:<9} {finding.short_thread_id():<16} "
            f"{artifact_state:<16} "
            f"{_problem_label(finding_type, finding.reason)}\n"
        )
    if hidden:
        stdout.write(
            f"... 另有 {hidden} 个问题未显示；"
            "请使用 --json 或增大 --limit。\n"
        )


def _write_action_catalog(
    plan: Any,
    *,
    stdout: TextIO,
    limit: int,
) -> None:
    """Display every observation's candidate actions by storage and risk."""

    actions = list(plan.actions)
    numbered = {
        str(action.action_id): index
        for index, action in enumerate(actions, start=1)
    }
    observations = {
        str(observation.observation_id): observation
        for observation in plan.observations
    }
    visible_ids = {
        str(action.action_id)
        for action in _visible_items(actions, limit)[0]
    }
    hidden = len(actions) - len(visible_ids)

    for storage in plan.storages:
        storage_actions = [
            action
            for action in actions
            if str(action.target.storage_id) == str(storage.storage_id)
            and str(action.action_id) in visible_ids
        ]
        storage_status = _enum_value(getattr(storage, "scan_status", "ok"))
        storage_errors = tuple(getattr(storage, "errors", ()))
        show_storage_status = (
            storage_status in {"failed", "partial"}
            or bool(storage_errors)
        )
        if not storage_actions and not show_storage_status:
            continue
        stdout.write(f"\n保存位置：{storage.label}\n")
        stdout.write(f"  目录：{storage.path}\n")
        if show_storage_status:
            status_label = {
                "failed": "扫描失败",
                "partial": "扫描不完整",
            }.get(storage_status, storage_status)
            stdout.write(f"  状态：{status_label}\n")
            for error in storage_errors:
                stdout.write(
                    f"  此保存位置的错误：{_human_message(error)}\n"
                )
        for risk in _RISK_ORDER:
            risk_actions = [
                action
                for action in storage_actions
                if _enum_value(action.risk) == risk
            ]
            if not risk_actions:
                continue
            stdout.write(f"  {_RISK_LABELS.get(risk, risk)}：\n")
            for action in risk_actions:
                number = numbered[str(action.action_id)]
                action_label = _ACTION_LABELS.get(
                    _enum_value(action.kind),
                    _enum_value(action.kind),
                )
                target = _short_thread_id(str(action.target.thread_id))
                stdout.write(
                    f"    [{number}] 对话 {target}｜候选动作：{action_label}\n"
                )
                reasons = [
                    _problem_label(
                        str(
                            getattr(
                                observations[str(item)],
                                "finding_type",
                                "",
                            )
                        ),
                        observations[str(item)].reason,
                    )
                    for item in action.observation_ids
                    if str(item) in observations
                ]
                if reasons:
                    stdout.write(f"        问题：{'；'.join(dict.fromkeys(reasons))}\n")
                impact = action.impact
                stdout.write(
                    "        仍存在的数据："
                    f"对话列表记录 {impact.index_record_count} 条，"
                    f"对话内容文件 {impact.rollout_file_count} 个，"
                    "由该对话创建的关联任务对话 "
                    f"{len(impact.descendant_thread_ids)} 条\n"
                )
                if action.available:
                    stdout.write(
                        f"        动作 ID：{action.action_id}\n"
                    )
                else:
                    reason = _human_unavailable_reason(action)
                    stdout.write(f"        仅供查看：{reason}\n")
                if bool(
                    getattr(
                        action,
                        "requires_explicit_selection",
                        False,
                    )
                ):
                    stdout.write(
                        "        选择要求：必须逐项选择，"
                        "不会由 all 或默认低风险计划纳入\n"
                    )
    if hidden:
        stdout.write(
            f"\n... 另有 {hidden} 个候选动作未显示；"
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
    stdout: TextIO,
) -> None:
    storage_by_id = {
        str(storage.storage_id): storage
        for storage in storages
    }
    stdout.write(f"\n最终计划：{len(actions)} 个动作。\n")
    for action in actions:
        storage = storage_by_id[str(action.target.storage_id)]
        impact = action.impact
        stdout.write(
            f"- {_ACTION_LABELS.get(_enum_value(action.kind), _enum_value(action.kind))}"
            f"：对话 {action.target.thread_id}\n"
            f"  动作 ID：{action.action_id}\n"
            f"  保存位置：{storage.label}（{storage.path}）\n"
            f"  对话列表记录：{impact.index_record_count} 条；"
            f"对话内容文件：{impact.rollout_file_count} 个\n"
        )
        related = tuple(str(value) for value in impact.descendant_thread_ids)
        stdout.write(
            "  将一同删除的关联任务对话："
            f"{len(related)} 条"
        )
        if related:
            stdout.write(f"（{', '.join(related)}）")
        stdout.write("\n")
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
    if json_output:
        payload = {
            "command": "clean",
            "confirmation_required": False,
            "selected_actions": [
                action.to_dict() for action in selected_actions
            ],
            **report.to_dict(),
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
            payload["plan_fingerprint"] = str(
                getattr(plan, "plan_fingerprint", "")
            )
        _write_json(payload, stdout)
        return

    stdout.write(
        f"清理完成：已验证删除 {report.succeeded} 条，"
        f"未成功 {report.failed} 条，共计划 {len(report.planned)} 条。\n"
    )
    visible, hidden = _visible_items(report.results, limit)
    status_labels = {
        "deleted": "已删除",
        "not_deleted": "未删除",
        "partial": "部分删除",
        "unknown": "无法确认",
    }
    for result in visible:
        status = getattr(
            result,
            "status",
            "deleted" if result.succeeded else "not_deleted",
        )
        stdout.write(
            f"{status_labels.get(status, status):<8} "
            f"{result.finding.platform:<8} "
            f"{result.finding.short_thread_id()}"
        )
        if result.error:
            stdout.write(f"  {_human_message(result.error)}")
        stdout.write("\n")
        if status == "deleted" and result.request_error:
            stdout.write(
                "          警告：请求曾报错，但磁盘验证确认已删除："
                f"{_human_message(result.request_error)}\n"
            )
        for artifact in result.remaining_artifacts:
            stdout.write(f"          仍存在：{artifact}\n")
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
            f"错误：{error.platform}：{error.error_type}："
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
        target = f" {match.group(1)}" if match else ""
        return (
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
    return message


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
            f"无法检查 Codex 可执行文件“{path}”：{exc}"
        ) from exc
    if not is_file:
        raise argparse.ArgumentTypeError(
            f"Codex 可执行文件必须是现存普通文件：“{path}”"
        )
    return path


__all__ = [
    "ActionSelectionError",
    "DEFAULT_HUMAN_LIMIT",
    "EXIT_CONFIRMATION_REQUIRED",
    "EXIT_ERROR",
    "EXIT_OK",
    "NumberSelectionError",
    "build_parser",
    "create_default_adapters",
    "main",
    "parse_action_selection",
    "parse_number_selection",
    "select_candidate_actions",
]
