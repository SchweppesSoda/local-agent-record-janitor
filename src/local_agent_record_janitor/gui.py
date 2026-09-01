"""Small, human-operated GUI for selective Codex thread deletion.

The GUI is deliberately a facade over the existing inventory and manual
delete services.  It never reads conversation bodies and it does not own a
second deletion implementation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cleaner import (
    AppServerFactory,
    BinaryResolver,
    CleanupReport,
    CleanupResult,
    VerificationResult,
    verify_finding_deleted,
)
from .codex_app_server import CodexAppServer
from .codex_desktop_state import (
    ClientInspector,
    DesktopCleanupResult,
    execute_desktop_state_cleanup,
    read_desktop_state,
)
from .discovery import choose_codex_binary
from .inventory import SessionCatalog, build_session_catalog
from .manual_delete import (
    ManualDeleteAction,
    ManualDeletePlan,
    ManualDeletePlanError,
    build_manual_delete_plan,
    execute_manual_delete,
)
from .path_identity import canonical_existing_path_key
from .rendering import safe_single_line


GUI_DELETE_CONFIRMATION = "客户端已关闭并确认永久删除"


class GuiUnavailableError(RuntimeError):
    """Raised when the selected Python runtime cannot open a Tk window."""


@dataclass(frozen=True)
class GuiConversationRow:
    """Display-only metadata for one storage-qualified Codex thread."""

    action_id: str
    thread_id: str
    codex_home: str
    title: str
    project: str
    cwd: str
    lifecycle: str
    source: str
    availability: str
    available: bool
    affected_thread_ids: tuple[str, ...]
    rollout_paths: tuple[str, ...]
    desktop_references: tuple[str, ...]
    retained_frontend_references: tuple[str, ...]
    blockers: tuple[str, ...]
    indexed: bool
    legacy_indexed: bool
    archived: bool | None

    @property
    def affected_count(self) -> int:
        return len(self.affected_thread_ids)

    @property
    def rollout_count(self) -> int:
        return len(self.rollout_paths)

    @property
    def reference_count(self) -> int:
        return len(self.desktop_references) + len(
            self.retained_frontend_references
        )

    @property
    def reference_summary(self) -> str:
        return (
            f"D{len(self.desktop_references)} / "
            f"保留{len(self.retained_frontend_references)}"
        )

    @property
    def search_text(self) -> str:
        return "\n".join(
            (
                self.title,
                self.project,
                self.cwd,
                self.thread_id,
                self.codex_home,
                self.source,
                *self.blockers,
            )
        ).casefold()

    def details_text(self) -> str:
        lines = [
            f"名称：{self.title}",
            f"项目：{self.project}",
            f"状态：{self.lifecycle}；{self.availability}",
            f"来源：{self.source}",
            f"Thread ID：{self.thread_id}",
            f"CODEX_HOME：{self.codex_home}",
            f"工作目录：{self.cwd}",
            f"稳定操作 ID：{self.action_id}",
            "",
            "本次原生删除范围：",
            *(
                f"  - {thread_id}"
                for thread_id in self.affected_thread_ids
            ),
            "",
            f"已发现的 rollout 内容文件（{self.rollout_count}）：",
        ]
        lines.extend(
            (f"  - {path}" for path in self.rollout_paths)
            if self.rollout_paths
            else ("  - 无",)
        )
        lines.extend(
            (
                "",
                "Codex Desktop 引用（原生删除成功后精确清理）：",
            )
        )
        lines.extend(
            (f"  - {reference}" for reference in self.desktop_references)
            if self.desktop_references
            else ("  - 无",)
        )
        lines.extend(("", "Cindy/AionUI 引用（仍按独立动作保留）："))
        lines.extend(
            (
                f"  - {reference}"
                for reference in self.retained_frontend_references
            )
            if self.retained_frontend_references
            else ("  - 无",)
        )
        if self.blockers:
            lines.extend(("", "不可删除原因："))
            lines.extend(f"  - {reason}" for reason in self.blockers)
        return "\n".join(lines)


@dataclass(frozen=True)
class GuiCatalogSnapshot:
    """One immutable inventory/plan pair rendered by the GUI."""

    catalog: SessionCatalog
    plan: ManualDeletePlan
    rows: tuple[GuiConversationRow, ...]
    failure_messages: tuple[str, ...] = ()

    def selected_plan(self, action_ids: Iterable[str]) -> GuiDeletePlan:
        native_plan = self.plan.with_selected_actions(tuple(action_ids))
        return build_gui_delete_plan(native_plan)

    def notice_text(self) -> str:
        lines: list[str] = []
        unmapped = tuple(self.catalog.unmapped_frontend_sessions)
        if unmapped:
            lines.append(f"未映射前端记录（不可删除）：{len(unmapped)} 条")
            lines.extend(
                "  - " + _frontend_reference_text(reference)
                for reference in unmapped
            )
        if self.failure_messages:
            if lines:
                lines.append("")
            lines.append("盘点错误（相关目标已 fail closed）：")
            lines.extend(f"  - {_display(message)}" for message in self.failure_messages)
        if not lines and not self.rows:
            lines.append("当前未发现 Codex 对话记录。")
        return "\n".join(lines)


@dataclass(frozen=True)
class GuiDesktopTarget:
    """One exact Desktop state record approved with its native root."""

    root_action_id: str
    codex_home: Path
    thread_id: str
    snapshot_fingerprint: str
    global_state_reference_count: int

    def approval_payload(self) -> dict[str, Any]:
        return {
            "root_action_id": self.root_action_id,
            "codex_home": canonical_existing_path_key(self.codex_home),
            "thread_id": self.thread_id,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "catalog_record_count": 1,
            "global_state_reference_count": self.global_state_reference_count,
        }


@dataclass(frozen=True)
class GuiDeletePlan:
    """One GUI authorization spanning sequential native and Desktop batches."""

    native_plan: ManualDeletePlan
    desktop_targets: tuple[GuiDesktopTarget, ...]
    plan_fingerprint: str

    @property
    def actions(self) -> tuple[ManualDeleteAction, ...]:
        return self.native_plan.actions


@dataclass(frozen=True)
class GuiDesktopCleanupFailure:
    codex_home: Path
    thread_ids: tuple[str, ...]
    error: str


@dataclass(frozen=True)
class GuiDeleteReport:
    """Final verified outcome of the GUI's sequential cleanup workflow."""

    native_report: CleanupReport
    results: tuple[CleanupResult, ...]
    desktop_results: tuple[DesktopCleanupResult, ...] = ()
    desktop_already_absent: tuple[tuple[str, str], ...] = ()
    desktop_failures: tuple[GuiDesktopCleanupFailure, ...] = ()

    @property
    def planned(self) -> tuple[Any, ...]:
        return tuple(self.native_report.planned)

    @property
    def succeeded(self) -> int:
        return sum(result.succeeded for result in self.results)

    @property
    def failed(self) -> int:
        return sum(not result.succeeded for result in self.results)

    @property
    def ok(self) -> bool:
        return (
            not self.native_report.scan_errors
            and not self.desktop_failures
            and self.failed == 0
        )


def build_gui_snapshot(catalog: SessionCatalog) -> GuiCatalogSnapshot:
    """Convert a complete read-only catalog into GUI rows and delete actions."""

    plan = build_manual_delete_plan(catalog)
    actions = {action.action_id: action for action in plan.actions}
    rows = tuple(
        _gui_row(record, actions.get(str(record.action_id)))
        for record in tuple(catalog.conversations)
    )
    failure_messages = tuple(
        dict.fromkeys(
            (
                *(
                    f"{failure.source}: {failure.message}"
                    for failure in tuple(catalog.failures)
                ),
                *plan.errors,
            )
        )
    )
    return GuiCatalogSnapshot(
        catalog=catalog,
        plan=plan,
        rows=rows,
        failure_messages=failure_messages,
    )


def load_gui_snapshot(
    adapter_builder: Callable[[], Sequence[Any]],
) -> GuiCatalogSnapshot:
    """Discover adapters and build a fresh GUI inventory snapshot."""

    return build_gui_snapshot(build_session_catalog(adapter_builder()))


def build_gui_delete_plan(native_plan: ManualDeletePlan) -> GuiDeletePlan:
    """Bind exact Desktop state cleanup to a selected native delete plan."""

    if (
        not native_plan.selected
        or not native_plan.actions
        or not native_plan.plan_fingerprint
    ):
        raise ManualDeletePlanError(
            "GUI deletion requires a non-empty selected native plan"
        )
    targets: dict[tuple[str, str], GuiDesktopTarget] = {}
    for action in native_plan.actions:
        for reference in action.frontend_sessions:
            if str(getattr(reference, "platform", "")).casefold() != (
                "codex-desktop"
            ):
                continue
            details = getattr(reference, "details", None)
            if not isinstance(details, Mapping):
                raise ManualDeletePlanError(
                    "Codex Desktop reference is missing exact cleanup details"
                )
            host_id = details.get("host_id")
            fingerprint = details.get("snapshot_fingerprint")
            reference_count = details.get("global_state_reference_count")
            thread_id = getattr(reference, "thread_id", None)
            reference_home = Path(
                getattr(reference, "codex_home", action.codex_home)
            ).expanduser().resolve()
            if canonical_existing_path_key(reference_home) != (
                canonical_existing_path_key(action.codex_home)
            ):
                raise ManualDeletePlanError(
                    "Codex Desktop reference escaped the selected CODEX_HOME"
                )
            if host_id != "local":
                raise ManualDeletePlanError(
                    "Only exact local Codex Desktop catalog rows can be cleaned"
                )
            if (
                not isinstance(thread_id, str)
                or thread_id not in action.affected_thread_ids
            ):
                raise ManualDeletePlanError(
                    "Codex Desktop reference is outside the approved cascade"
                )
            if (
                not isinstance(fingerprint, str)
                or not fingerprint.startswith("desktop:v1:")
            ):
                raise ManualDeletePlanError(
                    "Codex Desktop reference has no exact state fingerprint"
                )
            if (
                not isinstance(reference_count, int)
                or isinstance(reference_count, bool)
                or reference_count < 0
            ):
                raise ManualDeletePlanError(
                    "Codex Desktop reference has an invalid reference count"
                )
            target = GuiDesktopTarget(
                root_action_id=action.action_id,
                codex_home=reference_home,
                thread_id=thread_id,
                snapshot_fingerprint=fingerprint,
                global_state_reference_count=reference_count,
            )
            key = (canonical_existing_path_key(reference_home), thread_id)
            if key in targets:
                raise ManualDeletePlanError(
                    "Codex Desktop inventory contains multiple local catalog "
                    f"rows for {thread_id}; exact cleanup is unavailable"
                )
            targets[key] = target

    ordered_targets = tuple(
        sorted(
            targets.values(),
            key=lambda item: (
                canonical_existing_path_key(item.codex_home),
                item.thread_id,
            ),
        )
    )
    fingerprint = _sha256_json(
        {
            "schema_version": 1,
            "native_plan_fingerprint": native_plan.plan_fingerprint,
            "desktop_targets": [
                target.approval_payload() for target in ordered_targets
            ],
        }
    )
    return GuiDeletePlan(
        native_plan=native_plan,
        desktop_targets=ordered_targets,
        plan_fingerprint=f"gui-delete:v1:{fingerprint}",
    )


def execute_gui_delete(
    plan: GuiDeletePlan,
    *,
    catalog_builder: Callable[[], SessionCatalog],
    approved_plan_fingerprint: str,
    clients_closed: bool,
    timeout: float = 30.0,
    app_server_factory: AppServerFactory = CodexAppServer,
    binary_resolver: BinaryResolver = choose_codex_binary,
    client_inspector: ClientInspector | None = None,
    desktop_cleanup_executor: Callable[..., DesktopCleanupResult] = (
        execute_desktop_state_cleanup
    ),
    desktop_state_reader: Callable[..., Any] = read_desktop_state,
    final_verifier: Callable[[Any], VerificationResult] = (
        verify_finding_deleted
    ),
) -> GuiDeleteReport:
    """Delete native records, then exact Desktop state as separate batches."""

    if not clients_closed:
        raise ManualDeletePlanError(
            "GUI deletion requires an explicit clients-closed confirmation"
        )
    if not hmac.compare_digest(
        plan.plan_fingerprint,
        str(approved_plan_fingerprint or ""),
    ):
        raise ManualDeletePlanError(
            "The approved GUI plan fingerprint does not match"
        )
    preflight_catalog = catalog_builder()
    refreshed = build_gui_snapshot(preflight_catalog).selected_plan(
        action.action_id for action in plan.actions
    )
    if not hmac.compare_digest(
        plan.plan_fingerprint,
        refreshed.plan_fingerprint,
    ):
        raise ManualDeletePlanError(
            "The native/Desktop cleanup plan changed after approval; "
            "nothing was deleted"
        )

    def targeted_frontend_guard(action: ManualDeleteAction) -> None:
        # The GUI approval snapshot already contains every frontend reference
        # in the approved cascade.  Keep the per-action callback as a narrow
        # safety gate; native path/row guards remain in the cleaner and no
        # complete catalog is rebuilt inside the action loop.
        live = [
            reference
            for reference in action.frontend_sessions
            if bool(getattr(reference, "is_live", False))
        ]
        if live:
            raise ManualDeletePlanError(
                "目标仍被活跃前端引用，GUI 批次已停止"
            )

    def batch_action_state(_checkpoint: str, _action: Any, result: Any) -> None:
        if result is not None and (
            str(getattr(result, "status", "")) == "unknown"
            or bool(getattr(result, "request_error", None))
        ):
            raise ManualDeletePlanError(
                "GUI 原生删除结果无法确认；已停止剩余批次"
            )

    native_report = execute_manual_delete(
        plan.native_plan,
        catalog_builder=catalog_builder,
        approved_plan_fingerprint=str(
            plan.native_plan.plan_fingerprint or ""
        ),
        clients_closed=True,
        timeout=timeout,
        app_server_factory=app_server_factory,
        binary_resolver=binary_resolver,
        preflight_verified=True,
        targeted_guards_only=True,
        targeted_guard=targeted_frontend_guard,
        action_state_callback=batch_action_state,
    )
    action_by_root = {
        (
            canonical_existing_path_key(action.codex_home),
            action.thread_id,
        ): action
        for action in plan.actions
    }
    eligible_action_ids: set[str] = set()
    native_result_by_action: dict[str, CleanupResult] = {}
    for result in native_report.results:
        key = (
            canonical_existing_path_key(result.finding.codex_home),
            result.finding.thread_id,
        )
        action = action_by_root.get(key)
        if action is None:
            continue
        native_result_by_action[action.action_id] = result
        if result.status == "deleted" or _desktop_only_partial(result):
            eligible_action_ids.add(action.action_id)

    failures: list[GuiDesktopCleanupFailure] = []
    already_absent: list[tuple[str, str]] = []
    targets_by_home: dict[str, list[GuiDesktopTarget]] = {}
    home_paths: dict[str, Path] = {}
    for target in plan.desktop_targets:
        if target.root_action_id not in eligible_action_ids:
            native_result = native_result_by_action.get(target.root_action_id)
            failures.append(
                GuiDesktopCleanupFailure(
                    codex_home=target.codex_home,
                    thread_ids=(target.thread_id,),
                    error=(
                        "Native deletion was not verified far enough to clean "
                        "the approved Desktop reference"
                        + (
                            f" ({native_result.status})"
                            if native_result is not None
                            else ""
                        )
                    ),
                )
            )
            continue
        home_key = canonical_existing_path_key(target.codex_home)
        targets_by_home.setdefault(home_key, []).append(target)
        home_paths[home_key] = target.codex_home

    desktop_results: list[DesktopCleanupResult] = []
    successful_desktop_targets: set[tuple[str, str]] = set()
    for home_key, targets in sorted(targets_by_home.items()):
        home = home_paths[home_key]
        ids = tuple(sorted(target.thread_id for target in targets))
        try:
            current = desktop_state_reader(home, ids)
            present_targets = [
                target
                for target in targets
                if bool(
                    getattr(
                        current.threads.get(target.thread_id),
                        "present",
                        False,
                    )
                )
            ]
            absent_targets = [
                target for target in targets if target not in present_targets
            ]
            already_absent.extend(
                (home_key, target.thread_id) for target in absent_targets
            )
            successful_desktop_targets.update(
                (home_key, target.thread_id) for target in absent_targets
            )
            if not present_targets:
                continue
            approved = {
                target.thread_id: target.snapshot_fingerprint
                for target in present_targets
            }
            result = desktop_cleanup_executor(
                home,
                approved,
                client_inspector=client_inspector,
            )
            desktop_results.append(result)
            successful_desktop_targets.update(
                (home_key, target.thread_id) for target in present_targets
            )
        except Exception as exc:
            failures.append(
                GuiDesktopCleanupFailure(
                    codex_home=home,
                    thread_ids=ids,
                    error=str(exc) or repr(exc),
                )
            )

    targets_by_action: dict[str, set[tuple[str, str]]] = {}
    for target in plan.desktop_targets:
        targets_by_action.setdefault(target.root_action_id, set()).add(
            (
                canonical_existing_path_key(target.codex_home),
                target.thread_id,
            )
        )
    final_results: list[CleanupResult] = []
    for native_result in native_report.results:
        key = (
            canonical_existing_path_key(native_result.finding.codex_home),
            native_result.finding.thread_id,
        )
        action = action_by_root.get(key)
        approved_targets = (
            targets_by_action.get(action.action_id, set())
            if action is not None
            else set()
        )
        should_reverify = (
            native_result.status == "deleted"
            or (
                _desktop_only_partial(native_result)
                and bool(approved_targets)
                and approved_targets.issubset(successful_desktop_targets)
            )
        )
        if not should_reverify:
            final_results.append(native_result)
            continue
        try:
            verification = final_verifier(native_result.finding)
            final_results.append(
                CleanupResult(
                    finding=native_result.finding,
                    status=str(verification.status),
                    error=verification.error,
                    remaining_artifacts=verification.remaining_artifacts,
                    request_error=native_result.request_error,
                    impacted_thread_ids=(
                        verification.checked_thread_ids
                        or native_result.impacted_thread_ids
                    ),
                )
            )
        except Exception as exc:
            final_results.append(
                CleanupResult(
                    finding=native_result.finding,
                    status="unknown",
                    error=f"Final native/Desktop verification failed: {exc}",
                    request_error=native_result.request_error,
                    impacted_thread_ids=native_result.impacted_thread_ids,
                )
            )

    return GuiDeleteReport(
        native_report=native_report,
        results=tuple(final_results),
        desktop_results=tuple(desktop_results),
        desktop_already_absent=tuple(sorted(already_absent)),
        desktop_failures=tuple(failures),
    )


def confirmation_summary(plan: GuiDeletePlan) -> str:
    """Render the exact selected native/Desktop scope without chat bodies."""

    lines = [
        "以下操作会永久删除 Codex 原生 thread、其已批准的级联子 thread，",
        "以及官方 thread/delete 管理的 rollout 和原生元数据。此操作不可撤销。",
        "",
    ]
    total_threads: set[tuple[str, str]] = set()
    total_rollouts: set[str] = set()
    retained_references = 0
    for action in plan.actions:
        root = getattr(action, "root", None)
        summary = getattr(root, "summary", None)
        title = _display(
            getattr(summary, "display_name", None)
            or getattr(summary, "title", None)
            or getattr(summary, "name", None),
            "（未命名）",
        )
        lines.extend(
            (
                f"• {title}",
                f"  根 Thread ID：{_display(action.thread_id)}",
                f"  CODEX_HOME：{_display(action.codex_home)}",
                "  影响范围："
                + ", ".join(_display(value) for value in action.affected_thread_ids),
            )
        )
        total_threads.update(
            (str(action.codex_home), thread_id)
            for thread_id in action.affected_thread_ids
        )
        for record in tuple(getattr(action, "affected_records", ())):
            for rollout in tuple(getattr(record, "rollouts", ())):
                total_rollouts.add(str(rollout.path))
        retained_references += sum(
            str(getattr(reference, "platform", "")).casefold()
            != "codex-desktop"
            for reference in tuple(action.frontend_sessions)
        )
        lines.append("")
    desktop_state_references = sum(
        target.global_state_reference_count
        for target in plan.desktop_targets
    )
    lines.extend(
        (
            f"合计：{len(plan.actions)} 个根对话，"
            f"{len(total_threads)} 个受影响 thread，"
            f"{len(total_rollouts)} 个已发现 rollout 文件。",
            f"随后将精确清理 {len(plan.desktop_targets)} 条 Codex Desktop "
            f"目录记录及 {desktop_state_references} 条结构化 UI 引用；"
            "每个物理存储仍使用独立回滚与验证批次。",
            f"仍将保留 {retained_references} 条 Cindy/AionUI 引用；"
            "它们不是 Codex Desktop 状态。",
            f"计划指纹：{plan.plan_fingerprint or ''}",
        )
    )
    return "\n".join(lines)


def cleanup_report_summary(report: GuiDeleteReport) -> str:
    """Return a compact Chinese result suitable for a modal dialog."""

    labels = {
        "deleted": "已验证删除",
        "not_deleted": "未删除",
        "partial": "部分删除/仍有残留",
        "unknown": "结果未知",
    }
    lines = [
        f"计划 {len(report.planned)} 项；已验证删除 {report.succeeded} 项；"
        f"未完成或未知 {report.failed} 项。",
        "",
    ]
    for result in report.results:
        lines.append(
            f"• {labels.get(result.status, result.status)}："
            f"{_display(result.finding.thread_id)}"
        )
        if result.error:
            lines.append(f"  原因：{_display(result.error)}")
        if result.request_error:
            lines.append(f"  请求信息：{_display(result.request_error)}")
        if result.remaining_artifacts:
            lines.append("  仍存在：")
            lines.extend(
                f"    - {_display(item)}"
                for item in result.remaining_artifacts
            )
    if report.desktop_results or report.desktop_already_absent:
        cleaned_rows = sum(
            result.deleted_catalog_rows for result in report.desktop_results
        )
        cleaned_refs = sum(
            result.removed_global_state_references
            for result in report.desktop_results
        )
        lines.extend(
            (
                "",
                "Codex Desktop 状态："
                f"已删除 {cleaned_rows} 条目录记录和 {cleaned_refs} 条精确 UI 引用；"
                f"另有 {len(report.desktop_already_absent)} 条在原生删除后已不存在。",
            )
        )
    if report.desktop_failures:
        lines.extend(("", "Codex Desktop 清理未完成："))
        lines.extend(
            f"  - {_display(failure.codex_home)} / "
            f"{', '.join(failure.thread_ids)}：{_display(failure.error)}"
            for failure in report.desktop_failures
        )
    return "\n".join(lines).rstrip()


def run_gui(
    args: Any,
    *,
    adapter_builder: Callable[[], Sequence[Any]],
    app_server_factory: AppServerFactory = CodexAppServer,
    binary_resolver: BinaryResolver = choose_codex_binary,
) -> int:
    """Open the Tk GUI and block until the user closes it."""

    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as exc:  # pragma: no cover - runtime dependent
        raise GuiUnavailableError(
            "当前 Python 运行时无法导入 Tk。请改用包含 Tcl/Tk 的 Python 3.10+。"
        ) from exc

    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover - display/runtime dependent
        raise GuiUnavailableError(
            f"无法打开图形窗口：{exc}"
        ) from exc

    _JanitorWindow(
        root,
        tk=tk,
        ttk=ttk,
        messagebox=messagebox,
        adapter_builder=adapter_builder,
        timeout=float(getattr(args, "timeout", 30.0)),
        app_server_factory=app_server_factory,
        binary_resolver=binary_resolver,
    )
    root.mainloop()
    return 0


class _JanitorWindow:
    def __init__(
        self,
        root: Any,
        *,
        tk: Any,
        ttk: Any,
        messagebox: Any,
        adapter_builder: Callable[[], Sequence[Any]],
        timeout: float,
        app_server_factory: AppServerFactory,
        binary_resolver: BinaryResolver,
    ) -> None:
        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.adapter_builder = adapter_builder
        self.timeout = timeout
        self.app_server_factory = app_server_factory
        self.binary_resolver = binary_resolver
        self.snapshot: GuiCatalogSnapshot | None = None
        self.selected_action_ids: set[str] = set()
        self.rows_by_id: dict[str, GuiConversationRow] = {}
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.operation: str | None = None
        self.closed = False

        self.root.title("Local Agent Record Janitor — Codex 对话管理")
        self.root.geometry("1180x760")
        self.root.minsize(880, 600)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._build_widgets()
        self.root.after(80, self._poll_events)
        self.refresh()

    def _build_widgets(self) -> None:
        outer = self.ttk.Frame(self.root, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        outer.columnconfigure(0, weight=1)

        self.ttk.Label(
            outer,
            text="Codex 对话管理",
            font=("TkDefaultFont", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.ttk.Label(
            outer,
            text=(
                "只读取记录元数据，不读取聊天正文。勾选后将永久删除原生 thread、"
                "已批准的级联子 thread、官方管理的内容文件和精确 Codex Desktop "
                "引用；Cindy/AionUI 引用仍按独立动作处理。"
            ),
            wraplength=1120,
        ).grid(row=1, column=0, sticky="ew", pady=(4, 10))

        controls = self.ttk.Frame(outer)
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        controls.columnconfigure(1, weight=1)
        self.ttk.Label(controls, text="搜索：").grid(row=0, column=0, sticky="w")
        self.search_var = self.tk.StringVar(master=self.root)
        search = self.ttk.Entry(controls, textvariable=self.search_var)
        search.grid(row=0, column=1, sticky="ew", padx=(4, 10))
        self.only_available_var = self.tk.BooleanVar(master=self.root, value=False)
        self.ttk.Checkbutton(
            controls,
            text="只看可删除",
            variable=self.only_available_var,
            command=self._render_rows,
        ).grid(row=0, column=2, padx=(0, 10))
        self.refresh_button = self.ttk.Button(
            controls, text="刷新记录", command=self.refresh
        )
        self.refresh_button.grid(row=0, column=3)
        self.search_var.trace_add("write", lambda *_args: self._render_rows())

        paned = self.ttk.Panedwindow(outer, orient=self.tk.VERTICAL)
        paned.grid(row=3, column=0, sticky="nsew")

        table_frame = self.ttk.Frame(paned)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        columns = (
            "mark",
            "title",
            "project",
            "lifecycle",
            "availability",
            "scope",
            "references",
            "thread_id",
            "codex_home",
        )
        self.tree = self.ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=16,
        )
        headings = {
            "mark": "选择",
            "title": "对话名称",
            "project": "项目",
            "lifecycle": "记录状态",
            "availability": "删除状态",
            "scope": "影响 thread",
            "references": "引用(D/保留)",
            "thread_id": "Thread ID",
            "codex_home": "CODEX_HOME",
        }
        widths = {
            "mark": 52,
            "title": 210,
            "project": 150,
            "lifecycle": 80,
            "availability": 90,
            "scope": 88,
            "references": 76,
            "thread_id": 190,
            "codex_home": 260,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=45,
                stretch=column in {"title", "project", "codex_home"},
                anchor="center" if column in {"mark", "scope", "references"} else "w",
            )
        y_scroll = self.ttk.Scrollbar(
            table_frame, orient="vertical", command=self.tree.yview
        )
        x_scroll = self.ttk.Scrollbar(
            table_frame, orient="horizontal", command=self.tree.xview
        )
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("blocked", foreground="#777777")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_details)
        self.tree.bind("<Button-1>", self._tree_click, add=True)
        self.tree.bind("<space>", self._tree_space)
        paned.add(table_frame, weight=3)

        details_frame = self.ttk.LabelFrame(paned, text="所选记录详情", padding=6)
        details_frame.rowconfigure(0, weight=1)
        details_frame.columnconfigure(0, weight=1)
        self.details = self.tk.Text(
            details_frame,
            height=11,
            wrap="word",
            state="disabled",
            borderwidth=0,
            padx=6,
            pady=6,
        )
        details_scroll = self.ttk.Scrollbar(
            details_frame, orient="vertical", command=self.details.yview
        )
        self.details.configure(yscrollcommand=details_scroll.set)
        self.details.grid(row=0, column=0, sticky="nsew")
        details_scroll.grid(row=0, column=1, sticky="ns")
        paned.add(details_frame, weight=2)

        footer = self.ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        self.status_var = self.tk.StringVar(master=self.root, value="准备读取记录…")
        self.ttk.Label(footer, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w"
        )
        self.progress = self.ttk.Progressbar(footer, mode="indeterminate", length=120)
        self.progress.grid(row=0, column=1, padx=(8, 12))
        self.clear_button = self.ttk.Button(
            footer, text="清空勾选", command=self._clear_selection
        )
        self.clear_button.grid(row=0, column=2, padx=(0, 8))
        self.delete_button = self.ttk.Button(
            footer,
            text="永久删除已勾选 (0)",
            command=self._delete_selected,
        )
        self.delete_button.grid(row=0, column=3)
        self._update_buttons()

    def refresh(self) -> None:
        if self.operation is not None:
            return
        # A failed refresh must never leave a stale plan selectable.  Clear
        # the prior snapshot before starting the new read-only inventory.
        self.snapshot = None
        self.rows_by_id.clear()
        self.selected_action_ids.clear()
        self._render_rows()
        self._set_details("")
        self._start_worker("refresh", lambda: load_gui_snapshot(self.adapter_builder))

    def _delete_selected(self) -> None:
        if self.operation is not None or self.snapshot is None:
            return
        if not self.selected_action_ids:
            self.messagebox.showinfo("未选择记录", "请先逐项勾选要永久删除的对话。")
            return
        try:
            selected_plan = self.snapshot.selected_plan(
                tuple(sorted(self.selected_action_ids))
            )
        except Exception as exc:
            self.messagebox.showerror("无法生成删除计划", str(exc), parent=self.root)
            return
        if not _DeleteConfirmationDialog(
            self.root,
            tk=self.tk,
            ttk=self.ttk,
            plan=selected_plan,
        ).confirmed:
            return

        def execute() -> GuiDeleteReport:
            return execute_gui_delete(
                selected_plan,
                catalog_builder=lambda: build_session_catalog(
                    self.adapter_builder()
                ),
                approved_plan_fingerprint=str(selected_plan.plan_fingerprint or ""),
                clients_closed=True,
                timeout=self.timeout,
                app_server_factory=self.app_server_factory,
                binary_resolver=self.binary_resolver,
            )

        self._start_worker("delete", execute)

    def _start_worker(self, operation: str, function: Callable[[], object]) -> None:
        self.operation = operation
        self.status_var.set(
            "正在读取并核验记录…"
            if operation == "refresh"
            else "正在重验证并永久删除；请勿关闭窗口…"
        )
        self.progress.start(12)
        self._update_buttons()

        def work() -> None:
            try:
                result = function()
            except Exception as exc:
                self.events.put((f"{operation}_error", exc))
            else:
                self.events.put((f"{operation}_ok", result))

        self.worker = threading.Thread(
            target=work,
            name=f"local-agent-record-janitor-{operation}",
            daemon=False,
        )
        self.worker.start()

    def _poll_events(self) -> None:
        if self.closed:
            return
        try:
            while True:
                event, payload = self.events.get_nowait()
                self._handle_event(event, payload)
        except queue.Empty:
            pass
        if not self.closed:
            self.root.after(80, self._poll_events)

    def _handle_event(self, event: str, payload: object) -> None:
        self.operation = None
        self.progress.stop()
        if event == "refresh_ok":
            assert isinstance(payload, GuiCatalogSnapshot)
            self.snapshot = payload
            self.rows_by_id = {row.action_id: row for row in payload.rows}
            self.selected_action_ids.clear()
            self._render_rows()
            if payload.failure_messages:
                self.status_var.set(
                    f"已读取 {len(payload.rows)} 条记录；"
                    f"有 {len(payload.failure_messages)} 个盘点错误，相关目标已阻止删除。"
                )
            else:
                available = sum(row.available for row in payload.rows)
                unmapped = len(tuple(payload.catalog.unmapped_frontend_sessions))
                self.status_var.set(
                    f"已读取 {len(payload.rows)} 条对话，其中 {available} 条可删除；"
                    f"另有 {unmapped} 条未映射前端记录。"
                )
            self._set_details(payload.notice_text())
        elif event == "refresh_error":
            self.status_var.set("读取失败；未做任何更改。")
            self.messagebox.showerror("读取记录失败", str(payload), parent=self.root)
        elif event == "delete_ok":
            assert isinstance(payload, GuiDeleteReport)
            summary = cleanup_report_summary(payload)
            if payload.ok:
                self.messagebox.showinfo("删除完成", summary, parent=self.root)
            else:
                self.messagebox.showwarning(
                    "删除未完全完成", summary, parent=self.root
                )
            self.snapshot = None
            self.rows_by_id.clear()
            self.selected_action_ids.clear()
            self._render_rows()
            self.refresh()
            return
        elif event == "delete_error":
            self.messagebox.showerror(
                "删除结果未确认",
                "执行未能正常收口；不会自动重试删除。将只读刷新当前记录。\n\n"
                + str(payload),
                parent=self.root,
            )
            self.snapshot = None
            self.rows_by_id.clear()
            self.selected_action_ids.clear()
            self._render_rows()
            self.refresh()
            return
        self._update_buttons()

    def _render_rows(self) -> None:
        if not hasattr(self, "tree"):
            return
        focused = self.tree.focus()
        yview = self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        needle = self.search_var.get().strip().casefold()
        only_available = bool(self.only_available_var.get())
        rows = tuple(self.rows_by_id.values())
        rows = tuple(
            row
            for row in rows
            if (not only_available or row.available)
            and (not needle or needle in row.search_text)
        )
        for row in sorted(
            rows,
            key=lambda item: (
                not item.available,
                item.title.casefold(),
                item.codex_home.casefold(),
                item.thread_id,
            ),
        ):
            mark = (
                "☑"
                if row.action_id in self.selected_action_ids
                else "☐" if row.available else "—"
            )
            self.tree.insert(
                "",
                "end",
                iid=row.action_id,
                values=(
                    mark,
                    row.title,
                    row.project,
                    row.lifecycle,
                    row.availability,
                    row.affected_count,
                    row.reference_summary,
                    row.thread_id,
                    row.codex_home,
                ),
                tags=() if row.available else ("blocked",),
            )
        if focused and self.tree.exists(focused):
            self.tree.focus(focused)
            self.tree.selection_set(focused)
        if yview and self.tree.get_children():
            self.tree.yview_moveto(yview[0])
        self._update_buttons()

    def _tree_click(self, event: Any) -> str | None:
        row_id = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not row_id:
            return None
        self.tree.focus(row_id)
        self.tree.selection_set(row_id)
        if column == "#1":
            self._toggle(row_id)
            return "break"
        return None

    def _tree_space(self, _event: Any) -> str:
        row_id = self.tree.focus()
        if row_id:
            self._toggle(row_id)
        return "break"

    def _toggle(self, action_id: str) -> None:
        if self.operation is not None:
            return
        row = self.rows_by_id.get(action_id)
        if row is None:
            return
        if not row.available:
            self.status_var.set("该记录当前不可删除；请查看下方阻止原因。")
            self._set_details(row.details_text())
            return
        if action_id in self.selected_action_ids:
            self.selected_action_ids.remove(action_id)
        else:
            self.selected_action_ids.add(action_id)
        if self.tree.exists(action_id):
            values = list(self.tree.item(action_id, "values"))
            values[0] = "☑" if action_id in self.selected_action_ids else "☐"
            self.tree.item(action_id, values=values)
        self._update_buttons()

    def _show_selected_details(self, _event: Any = None) -> None:
        selected = self.tree.selection()
        row = self.rows_by_id.get(selected[0]) if selected else None
        self._set_details(row.details_text() if row is not None else "")

    def _set_details(self, value: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", value)
        self.details.configure(state="disabled")

    def _clear_selection(self) -> None:
        self.selected_action_ids.clear()
        self._render_rows()

    def _update_buttons(self) -> None:
        busy = self.operation is not None
        if busy:
            self.refresh_button.state(["disabled"])
            self.clear_button.state(["disabled"])
            self.delete_button.state(["disabled"])
        else:
            self.refresh_button.state(["!disabled"])
            self.clear_button.state(
                ["!disabled"] if self.selected_action_ids else ["disabled"]
            )
            self.delete_button.state(
                ["!disabled"] if self.selected_action_ids else ["disabled"]
            )
        self.delete_button.configure(
            text=f"永久删除已勾选 ({len(self.selected_action_ids)})"
        )

    def _close(self) -> None:
        if self.operation == "delete":
            self.messagebox.showwarning(
                "删除仍在进行",
                "正在等待删除与验证完成，当前不能关闭窗口。",
                parent=self.root,
            )
            return
        self.closed = True
        self.root.destroy()


class _DeleteConfirmationDialog:
    def __init__(
        self,
        parent: Any,
        *,
        tk: Any,
        ttk: Any,
        plan: GuiDeletePlan,
    ) -> None:
        self.confirmed = False
        dialog = tk.Toplevel(parent)
        self.dialog = dialog
        dialog.title("确认永久删除")
        dialog.geometry("760x600")
        dialog.minsize(620, 500)
        dialog.transient(parent)
        dialog.protocol("WM_DELETE_WINDOW", self._cancel)
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)

        outer = ttk.Frame(dialog, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)
        ttk.Label(
            outer,
            text="请核对完整影响范围",
            font=("TkDefaultFont", 14, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        summary = tk.Text(outer, wrap="word", height=18, padx=7, pady=7)
        summary.insert("1.0", confirmation_summary(plan))
        summary.configure(state="disabled")
        summary.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=summary.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        summary.configure(yscrollcommand=scrollbar.set)

        self.closed_var = tk.BooleanVar(master=dialog, value=False)
        ttk.Checkbutton(
            outer,
            text="我已完全关闭使用这些 CODEX_HOME 的 Codex/ChatGPT Desktop、Cindy 和 AionUI",
            variable=self.closed_var,
            command=self._update_confirm_state,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 6))
        ttk.Label(
            outer,
            text=f"请输入“{GUI_DELETE_CONFIRMATION}”：",
        ).grid(row=3, column=0, columnspan=2, sticky="w")
        self.phrase_var = tk.StringVar(master=dialog)
        entry = ttk.Entry(outer, textvariable=self.phrase_var)
        entry.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        self.phrase_var.trace_add("write", lambda *_args: self._update_confirm_state())

        buttons = ttk.Frame(outer)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="取消", command=self._cancel).grid(
            row=0, column=0, padx=(0, 8)
        )
        self.confirm_button = ttk.Button(
            buttons,
            text="确认永久删除",
            command=self._confirm,
        )
        self.confirm_button.grid(row=0, column=1)
        self.confirm_button.state(["disabled"])
        dialog.bind("<Escape>", lambda _event: self._cancel())
        entry.focus_set()
        dialog.grab_set()
        parent.wait_window(dialog)

    def _update_confirm_state(self) -> None:
        enabled = bool(self.closed_var.get()) and (
            self.phrase_var.get().strip() == GUI_DELETE_CONFIRMATION
        )
        self.confirm_button.state(["!disabled"] if enabled else ["disabled"])

    def _confirm(self) -> None:
        if not bool(self.closed_var.get()):
            return
        if self.phrase_var.get().strip() != GUI_DELETE_CONFIRMATION:
            return
        self.confirmed = True
        self.dialog.destroy()

    def _cancel(self) -> None:
        self.dialog.destroy()


def _gui_row(record: Any, action: ManualDeleteAction | None) -> GuiConversationRow:
    summary = record.summary
    title = _display(
        summary.display_name or summary.title or summary.name,
        "（未命名）",
    )
    cwd = _display(summary.cwd, "—")
    project = _display(summary.project_label or summary.cwd, "—")
    archived = summary.archived
    if archived is True:
        lifecycle = "已归档"
    elif archived is False:
        lifecycle = "活动"
    elif bool(record.indexed):
        lifecycle = "已索引"
    elif bool(record.rollouts):
        lifecycle = "仅内容文件"
    else:
        lifecycle = "仅引用/索引"

    reference_records = (
        tuple(action.frontend_sessions)
        if action is not None
        else tuple(record.frontend_sessions)
    )
    desktop_references = tuple(
        _frontend_reference_text(reference)
        for reference in reference_records
        if str(getattr(reference, "platform", "")).casefold()
        == "codex-desktop"
    )
    retained_frontend_references = tuple(
        _frontend_reference_text(reference)
        for reference in reference_records
        if str(getattr(reference, "platform", "")).casefold()
        != "codex-desktop"
    )
    sources = {"Codex"} if bool(record.artifact_present) else set()
    sources.update(
        _source_label(str(reference.platform))
        for reference in tuple(record.frontend_sessions)
    )
    affected_records = (
        tuple(getattr(action, "affected_records", ()))
        if action is not None
        else (record,)
    )
    rollout_paths = tuple(
        sorted(
            {
                _display(rollout.path)
                for affected in affected_records
                for rollout in tuple(getattr(affected, "rollouts", ()))
            }
        )
    )
    if action is None:
        available = False
        affected_ids = (str(record.thread_id), *tuple(record.descendant_thread_ids))
        blockers = tuple(record.blockers) or ("未能生成稳定删除操作",)
    else:
        available = bool(action.available)
        affected_ids = tuple(action.affected_thread_ids)
        blockers = tuple(action.unavailable_reasons)
    if available:
        availability = "可删除"
    elif bool(record.desktop_state_present) and not bool(record.artifact_present):
        availability = "仅 Desktop 状态"
    else:
        availability = "已阻止"
    return GuiConversationRow(
        action_id=str(record.action_id),
        thread_id=_display(record.thread_id),
        codex_home=_display(record.codex_home),
        title=title,
        project=project,
        cwd=cwd,
        lifecycle=lifecycle,
        source=" + ".join(sorted(sources)) or "前端引用",
        availability=availability,
        available=available,
        affected_thread_ids=tuple(_display(value) for value in affected_ids),
        rollout_paths=rollout_paths,
        desktop_references=desktop_references,
        retained_frontend_references=retained_frontend_references,
        blockers=tuple(_display(value) for value in blockers),
        indexed=bool(record.indexed),
        legacy_indexed=bool(record.legacy_indexed),
        archived=archived,
    )


def _frontend_reference_text(reference: Any) -> str:
    platform = _source_label(str(getattr(reference, "platform", "frontend")))
    session_id = _display(getattr(reference, "platform_session_id", None), "—")
    status = _display(getattr(reference, "status", None), "unknown")
    database = _display(getattr(reference, "database", None), "—")
    live = "live" if bool(getattr(reference, "is_live", False)) else "非 live"
    return f"{platform}: {session_id} [{status}, {live}] @ {database}"


def _source_label(value: str) -> str:
    labels = {
        "aionui": "AionUI",
        "cindy": "Cindy",
        "codex-desktop": "Desktop",
        "native": "Codex",
    }
    return labels.get(value.casefold(), _display(value, "Frontend"))


def _display(value: object | None, fallback: str = "") -> str:
    rendered = safe_single_line(value, max_width=None)
    return rendered or fallback


def _desktop_only_partial(result: CleanupResult) -> bool:
    return (
        result.status == "partial"
        and bool(result.remaining_artifacts)
        and all(
            str(marker).startswith(("desktop-catalog:", "desktop-state:"))
            for marker in result.remaining_artifacts
        )
    )


def _sha256_json(value: object) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


__all__ = [
    "GUI_DELETE_CONFIRMATION",
    "GuiCatalogSnapshot",
    "GuiConversationRow",
    "GuiDeletePlan",
    "GuiDeleteReport",
    "GuiDesktopCleanupFailure",
    "GuiDesktopTarget",
    "GuiUnavailableError",
    "build_gui_delete_plan",
    "build_gui_snapshot",
    "cleanup_report_summary",
    "confirmation_summary",
    "execute_gui_delete",
    "load_gui_snapshot",
    "run_gui",
]
