"""Shared operation lifecycle for the thin command facades."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from copy import copy
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .action_registry import action_capability
from .agent_operations import action_binding
from .path_identity import inventory_path_identity_scope
from .operation_store import OperationStore, plan_sha256, strict_json_load, write_new_json
from .operation_guard_sources import (
    PLAN_V1, PLAN_V2, PLAN_V3, guard_sources_for, refresh_guard_sources,
    required_source_errors, retain_guard_sources, validate_guard_sources,
)
from .mutation_guard import (
    frozen_operation_roots, mutation_guard, mutation_roots, scopes_for_actions,
)
from .record_identity import (
    capability_for,
    canonical_path,
    normalize_client,
    normalize_engine,
    project_selector_matches,
)

_BODY_KEYS = frozenset({
    "body", "chat_body", "message_body", "messages", "transcript",
    "prompt", "response", "content",
})


class OperationCoordinatorError(RuntimeError):
    """A scoped operation cannot be planned or safely resumed."""


@dataclass
class _LiveOperation:
    operation_id: str
    document: dict[str, Any]
    context: Any
    candidates: tuple[Any, ...]
    client: str
    adapters: tuple[Any, ...]
    manual_actions: Mapping[str, Any] = field(default_factory=dict)
    manual_catalog: Any | None = field(default=None, repr=False, compare=False)
    manual_plan: Any | None = field(default=None, repr=False, compare=False)
    # Native Cindy Pi/Claude actions are planned in the one top-level
    # operation, but must execute against their engine-specific session
    # context.  Keeping this map on the live operation avoids a second
    # coordinator while preserving the existing batch journal contract.
    action_contexts: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    completed_child_ids: frozenset[str] = field(default_factory=frozenset)
    result: dict[str, Any] | None = None
    clients_closed_ack: bool = False
    terminal_context: Any | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class _ChildStateInspection:
    """Read-only recovery view of every child journal in an operation."""

    completed_child_ids: frozenset[str] = frozenset()
    batches: tuple[dict[str, Any], ...] = ()
    blockers: tuple[dict[str, Any], ...] = ()
    modified: bool = False
    mutation_started: bool = False


class OperationCoordinator:
    """Coordinate one immutable operation and its storage-qualified batches."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self._live: dict[str, _LiveOperation] = {}

    @staticmethod
    def _emit_progress(
        callback: Callable[[Mapping[str, Any]], None] | None,
        stage: str,
        status: str,
        *,
        operation_id: str | None = None,
        counts: Mapping[str, Any] | None = None,
        **metadata: Any,
    ) -> None:
        """Report a safe live phase without making callbacks part of results."""

        if not callable(callback):
            return
        try:
            event: dict[str, Any] = {
                "stage": str(stage),
                "status": str(status),
            }
            if operation_id:
                event["operation_id"] = str(operation_id)
            if isinstance(counts, Mapping):
                event["counts"] = {
                    str(key): value
                    for key, value in counts.items()
                    if isinstance(value, (str, int, float, bool)) or value is None
                }
            for key, value in metadata.items():
                if key.casefold() in _BODY_KEYS:
                    continue
                if isinstance(value, (str, int, float, bool)) or value is None:
                    event[str(key)] = value
            callback(event)
        except Exception:
            # Diagnostics are strictly best effort and cannot alter a safe
            # operation result if the caller's output stream is unavailable.
            return

    @staticmethod
    def _progress_counts(context: Any) -> dict[str, int]:
        plan = getattr(context, "plan", None)
        snapshot = getattr(context, "snapshot", None)
        report = getattr(snapshot, "report", None)

        def count(value: Any) -> int:
            try:
                return len(value or ())
            except TypeError:
                return 0

        return {
            "storage_count": count(getattr(plan, "storages", ())),
            "observation_count": count(getattr(plan, "observations", ())),
            "action_count": count(getattr(plan, "actions", ())),
            "finding_count": count(getattr(report, "findings", ())),
            "error_count": count(getattr(plan, "errors", ())),
        }

    def plan_operation(
        self,
        *,
        scope: Mapping[str, Any] | None = None,
        client: str | None = None,
        projects: Sequence[str] = (),
        all_projects: bool = False,
        record_ids: Sequence[str] = (),
        engines: Sequence[str] = (),
        operation_id: str | None = None,
        plan_path: Path | None = None,
        operation_home: Path | None = None,
        codex_home: Path | None = None,
        orca_roots: Sequence[str | Path] = (),
        herdr_roots: Sequence[str | Path] = (),
        workbuddy_roots: Sequence[str | Path] = (),
        adapters: Iterable[Any] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        normalized_scope = self._scope(
            scope,
            client=client,
            projects=projects,
            all_projects=all_projects,
            record_ids=record_ids,
            engines=engines,
        )
        try:
            self._validate_scope(normalized_scope, require_selection=True)
            client_name = str(normalized_scope["client"])
            source = None if adapters is None else tuple(adapters)
            self._emit_progress(progress_callback, "inventory", "started")
            (
                context,
                active_adapters,
                manual_catalog,
                manual_plan,
                manual_actions,
                action_contexts,
            ) = self._build_context(
                client_name,
                source,
                explicit_frontend_ids=tuple(normalized_scope.get("record_ids", ())),
                engines=tuple(normalized_scope.get("engines", ())),
                explicit_session_ids=tuple(normalized_scope.get("record_ids", ())),
                include_action_contexts=True,
                codex_home=codex_home,
                orca_roots=orca_roots,
                herdr_roots=herdr_roots,
                workbuddy_roots=workbuddy_roots,
            )
            self._emit_progress(
                progress_callback,
                "inventory",
                "completed",
                counts=self._progress_counts(context),
            )
            self._emit_progress(progress_callback, "plan", "started")
            candidates, blockers = self._select_candidates(context, normalized_scope)
            candidates = self._coalesce_manual_candidates(candidates, manual_actions)
            if client_name == "cindy":
                from .cindy_operations import preflight_frontend_actions
                blockers.extend(preflight_frontend_actions(candidates))
            operation = str(operation_id or self._new_operation_id(client_name))
            document = self._make_plan_document(
                operation,
                normalized_scope,
                context,
                candidates,
                blockers,
                plan_path=plan_path,
                operation_home=operation_home,
                codex_home=codex_home,
                active_adapters=active_adapters,
                action_contexts=action_contexts,
                manual_actions=manual_actions,
                manual_catalog=manual_catalog,
            )
            write_new_json(Path(str(document["plan_path"])), document)
            self._emit_progress(
                progress_callback,
                "plan",
                "completed",
                operation_id=operation,
                counts=document.get("counts"),
            )
            self._live[operation] = _LiveOperation(
                operation_id=operation,
                document=document,
                context=context,
                candidates=tuple(candidates),
                client=client_name,
                adapters=tuple(active_adapters),
                manual_actions=manual_actions,
                manual_catalog=manual_catalog,
                manual_plan=manual_plan,
                action_contexts=action_contexts,
            )
            return document
        except Exception as exc:
            self._emit_progress(progress_callback, "plan", "failed")
            return self._error_document(
                "plan", normalized_scope, str(exc) or repr(exc), operation_id=operation_id
            )

    def apply_operation(
        self,
        *,
        scope: Mapping[str, Any] | None = None,
        client: str | None = None,
        projects: Sequence[str] = (),
        all_projects: bool = False,
        record_ids: Sequence[str] = (),
        engines: Sequence[str] = (),
        operation_id: str | None = None,
        plan_path: Path | None = None,
        operation_home: Path | None = None,
        codex_home: Path | None = None,
        orca_roots: Sequence[str | Path] = (),
        workbuddy_roots: Sequence[str | Path] = (),
        plan_sha256: str | None = None,
        clients_closed: bool = False,
        adapters: Iterable[Any] | None = None,
        timeout: float = 30.0,
        app_server_factory: Any = None,
        binary_resolver: Any = None,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        normalized_scope = self._scope(
            scope,
            client=client,
            projects=projects,
            all_projects=all_projects,
            record_ids=record_ids,
            engines=engines,
        )
        if not clients_closed:
            return self._error_document(
                "apply", normalized_scope, "clients_closed_ack_required",
                operation_id=operation_id, blocker_code="clients_closed_ack_required",
            )
        document = None
        child_inspection = None
        live = None
        try:
            document = self._load_plan(
                operation_id,
                plan_path,
                plan_sha256,
                operation_home=operation_home,
                codex_home=codex_home,
            )
            self._validate_apply_scope(document, normalized_scope)
            frozen_workbuddy_roots = self._bound_workbuddy_roots(document, workbuddy_roots)
            child_inspection = self._inspect_child_states(document)
            if child_inspection.blockers:
                self._emit_progress(
                    progress_callback,
                    "apply",
                    "started",
                    operation_id=str(document.get("operation_id") or operation_id or ""),
                )
                result = self._result_document(
                    document,
                    normalized_scope,
                    goal_status="unknown",
                    blockers=child_inspection.blockers,
                    batches=child_inspection.batches,
                    modified=child_inspection.modified,
                    mutation_started=child_inspection.mutation_started,
                )
                self._emit_progress(
                    progress_callback,
                    "apply",
                    "completed",
                    operation_id=str(document.get("operation_id") or operation_id or ""),
                    counts={"batch_count": len(child_inspection.batches)},
                    goal_status="unknown",
                )
                return result
            if document["schema_version"] == PLAN_V3:
                # Even a closed capability can expose a concrete preflight.
                # Refresh process/reference/file evidence on apply; planning
                # observations never stand in for the caller's current ack.
                from .orca_target_safety import recheck_document_targets
                safety_errors = recheck_document_targets(document)
                if safety_errors:
                    return self._result_document(document, normalized_scope, goal_status="blocked",
                        blockers=[self._blocker(code, code, scope="target_safety") for code in safety_errors],
                        batches=child_inspection.batches, modified=child_inspection.modified,
                        mutation_started=child_inspection.mutation_started)
            if self._blocked_without_mutation(document):
                return self._result_document(document, normalized_scope, goal_status="blocked",
                    blockers=document.get("blockers", ()) or [self._blocker("action_unavailable", "The frozen plan has no authorized mutation")],
                    batches=child_inspection.batches, modified=child_inspection.modified,
                    mutation_started=child_inspection.mutation_started)
            operation = str(document["operation_id"])
            client_name = str(document["scope"]["client"])
            codex_home = self._bound_codex_home(document, codex_home)
            frozen_roots = validate_guard_sources(document)
            discovery_roots = (*frozen_roots, *orca_roots) if document["schema_version"] == PLAN_V3 else orca_roots
            live = self._live.get(operation)
            source = (tuple(adapters) if adapters is not None else live.adapters if live is not None
                      else tuple(self._default_adapters(client_name, codex_home=codex_home, orca_roots=discovery_roots,
                                                        workbuddy_roots=frozen_workbuddy_roots)))
            if document["schema_version"] == PLAN_V3:
                source = retain_guard_sources(source, frozen_roots)
            source = self._with_current_guard_sources(source, client_name, codex_home=codex_home, orca_roots=orca_roots)
            source = refresh_guard_sources(retain_guard_sources(source, (*frozen_roots, *orca_roots)))
            source = self._frozen_orca_execution_sources(document, source)
            pending = any(str(batch.get("child_operation_id")) not in child_inspection.completed_child_ids
                          for batch in document.get("child_batches", ()))
            if pending and document["schema_version"] == PLAN_V1 and guard_sources_for(source):
                return self._result_document(document, normalized_scope, goal_status="blocked",
                    blockers=[self._blocker("missing_guard_source_evidence", "Create a new top-level v2 plan that freezes the known Orca protection sources")],
                    batches=child_inspection.batches, modified=child_inspection.modified,
                    mutation_started=child_inspection.mutation_started)
            source_errors = required_source_errors(document, source)
            if pending and source_errors:
                return self._result_document(document, normalized_scope, goal_status="blocked",
                    blockers=[self._blocker("guard_source_incomplete", "; ".join(source_errors))],
                    batches=child_inspection.batches, modified=child_inspection.modified,
                    mutation_started=child_inspection.mutation_started)
            live = self._live.get(operation)
            if live is not None:
                self._retain_live_guards(live, source)
                # A plan followed by apply in one process already owns the
                # immutable snapshot. Rebuilding the catalog here would turn
                # plan + apply + terminal verification into three full passes.
                if (
                    str(live.document.get("plan_sha256"))
                    != str(document.get("plan_sha256"))
                ):
                    raise OperationCoordinatorError(
                        "live operation is bound to a different plan"
                    )
                if live.result is not None:
                    # In particular, an unknown result is a recovery state;
                    # apply must never send another irreversible request.
                    # A known pre-mutation blocker is the one exception: the
                    # child journal is explicitly resumable once the blocker
                    # (for example, an open client) is gone.  Keep terminal
                    # and unknown results immutable, but let that one child
                    # pass through the normal skip/resume loop.
                    if str(live.result.get("goal_status") or "") != "blocked":
                        return dict(live.result)
                    if not any(
                        str(batch.get("status") or "") == "blocked"
                        for batch in child_inspection.batches
                    ):
                        return dict(live.result)
            else:
                self._emit_progress(
                    progress_callback,
                    "inventory",
                    "started",
                    operation_id=operation,
                )
                (
                    context,
                    active_adapters,
                    manual_catalog,
                    manual_plan,
                    manual_actions,
                    action_contexts,
                ) = self._build_context(
                    client_name,
                    source,
                    explicit_frontend_ids=tuple(document.get("scope", {}).get("record_ids", ())),
                    engines=tuple(document.get("scope", {}).get("engines", ())),
                    explicit_session_ids=tuple(document.get("scope", {}).get("record_ids", ())),
                    include_action_contexts=True,
                    codex_home=codex_home,
                )
                self._emit_progress(
                    progress_callback,
                    "inventory",
                    "completed",
                    operation_id=operation,
                    counts=self._progress_counts(context),
                )
                candidates, blockers = self._bind_fresh_candidates(
                    document,
                    context,
                    skip_child_ids=child_inspection.completed_child_ids,
                )
                if blockers:
                    return self._result_document(
                        document, normalized_scope, goal_status="blocked",
                        blockers=blockers, batches=(),
                    )
                live = _LiveOperation(
                    operation_id=operation,
                    document=document,
                    context=context,
                    candidates=tuple(candidates),
                    client=client_name,
                    adapters=tuple(active_adapters),
                    manual_actions=manual_actions,
                    manual_catalog=manual_catalog,
                    manual_plan=manual_plan,
                    action_contexts=action_contexts,
                    completed_child_ids=child_inspection.completed_child_ids,
                )
                self._live[operation] = live
            live.clients_closed_ack = clients_closed is True
            return self._execute_live(
                live,
                timeout=float(timeout),
                app_server_factory=app_server_factory,
                binary_resolver=binary_resolver,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            self._emit_progress(
                progress_callback,
                "apply",
                "failed",
                operation_id=operation_id,
            )
            if document is not None:
                if child_inspection is None:
                    child_inspection = self._inspect_child_states(document)
                result = self._result_document(document, normalized_scope,
                    goal_status="unknown" if child_inspection.blockers or child_inspection.mutation_started else "blocked",
                    blockers=[self._blocker("operation_apply_failed", str(exc) or repr(exc))],
                    batches=child_inspection.batches, modified=child_inspection.modified,
                    mutation_started=child_inspection.mutation_started)
                if live is not None:
                    live.result = result
                return result
            return self._error_document(
                "apply", normalized_scope, str(exc) or repr(exc),
                operation_id=operation_id, blocker_code="operation_apply_failed",
            )

    def run_operation(
        self,
        *,
        scope: Mapping[str, Any] | None = None,
        client: str | None = None,
        projects: Sequence[str] = (),
        all_projects: bool = False,
        record_ids: Sequence[str] = (),
        engines: Sequence[str] = (),
        operation_id: str | None = None,
        plan_path: Path | None = None,
        operation_home: Path | None = None,
        codex_home: Path | None = None,
        orca_roots: Sequence[str | Path] = (),
        herdr_roots: Sequence[str | Path] = (),
        workbuddy_roots: Sequence[str | Path] = (),
        clients_closed: bool = False,
        adapters: Iterable[Any] | None = None,
        timeout: float = 30.0,
        app_server_factory: Any = None,
        binary_resolver: Any = None,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        normalized_scope = self._scope(
            scope,
            client=client,
            projects=projects,
            all_projects=all_projects,
            record_ids=record_ids,
            engines=engines,
        )
        if not clients_closed:
            return self._error_document(
                "run", normalized_scope, "clients_closed_ack_required",
                operation_id=operation_id, blocker_code="clients_closed_ack_required",
            )
        planned = self.plan_operation(
            scope=normalized_scope,
            operation_id=operation_id,
            plan_path=plan_path,
            operation_home=operation_home,
            codex_home=codex_home,
            orca_roots=orca_roots,
            herdr_roots=herdr_roots,
            workbuddy_roots=workbuddy_roots,
            adapters=adapters,
            progress_callback=progress_callback,
        )
        if planned.get("goal_status") != "ready":
            return planned
        live = self._live.get(str(planned["operation_id"]))
        if live is None:
            return self._error_document(
                "run", normalized_scope, "operation_context_unavailable",
                operation_id=operation_id, blocker_code="operation_context_unavailable",
            )
        result = self._execute_live(
            live,
            timeout=float(timeout),
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
            progress_callback=progress_callback,
        )
        return self._finish_native_run(live, result, timeout=float(timeout),
            app_server_factory=app_server_factory, binary_resolver=binary_resolver,
            progress_callback=progress_callback)

    def _finish_native_run(self, initial: _LiveOperation, result: dict[str, Any], **execution: Any) -> dict[str, Any]:
        """Fresh, bounded residual plans; never retry a native deletion request."""
        if initial.client != "native":
            return result
        allowed: set[tuple[str, str]] = set()
        for action in initial.document.get("actions", ()):
            target = action.get("target", {})
            ids = {str(target.get("thread_id", ""))}
            for key in ("affected_thread_ids", "descendant_thread_ids", "indexed_thread_ids"):
                ids.update(action.get("impact", {}).get(key, ()))
            allowed.update((str(target.get("storage_id")), value) for value in ids if value)
        rounds = []
        executed_batches = []
        live = initial
        seen: set[str] = set()
        modified = False
        started = False
        for round_index in range(3):
            modified |= bool(result.get("modified"))
            started |= bool(result.get("mutation_started"))
            executed_batches.extend(result.get("batches", ()))
            rounds.append({"operation_id": live.operation_id, "plan_path": live.document["plan_path"],
                "plan_sha256": live.document["plan_sha256"], "goal_status": result.get("goal_status")})
            if result.get("goal_status") != "completed_with_residuals" or live.terminal_context is None:
                break
            context = live.terminal_context
            candidates = []
            for action in context.plan.actions:
                target = action.target
                if (str(target.storage_id), str(target.thread_id)) not in allowed:
                    continue
                if str(getattr(action.kind, "value", action.kind)) != "remove_desktop_state" or not action.available:
                    continue
                impacted = set(getattr(action.impact, "affected_thread_ids", ()) or (target.thread_id,))
                if any((str(target.storage_id), str(value)) not in allowed for value in impacted):
                    continue
                candidates.append(action)
            signature = json.dumps(sorted(str(action.action_id) for action in candidates))
            if not candidates or signature in seen or round_index == 2:
                break
            seen.add(signature)
            operation = self._new_operation_id(initial.client)
            path = Path(initial.document["plan_path"]).with_name(operation + ".json")
            next_scope = self._scope(None, client=initial.client,
                record_ids=tuple(str(action.target.thread_id) for action in candidates),
                projects=(), all_projects=False, engines=())
            # The terminal context is already a fresh successful scan. Reuse
            # it for the next immutable plan instead of repeating discovery.
            next_adapters = live.adapters
            document = self._make_plan_document(operation, next_scope, context, candidates, (),
                plan_path=path, operation_home=None, codex_home=self._bound_codex_home(initial.document, None),
                active_adapters=next_adapters, action_contexts={})
            write_new_json(path, document)
            live = _LiveOperation(operation_id=operation, document=document, context=context,
                candidates=tuple(candidates), client=initial.client, adapters=next_adapters)
            self._live[operation] = live
            result = self._execute_live(live, **execution)
        # A later plan can cover only the executable subset of the first
        # round's residuals. Its success cannot discharge the original scope.
        if result.get("goal_status") in {"complete", "completed_with_residuals"}:
            try:
                if live.terminal_context is None:
                    raise OperationCoordinatorError("terminal verification context is unavailable")
                remaining = self._residual_action_ids(initial.document, live.terminal_context)
                result = self._result_document(initial.document, initial.document.get("scope", {}),
                    goal_status="completed_with_residuals" if remaining else "complete",
                    blockers=[self._blocker("residual_records", "approved records remain")] if remaining else [],
                    batches=executed_batches, residuals=remaining, modified=modified, mutation_started=started)
            except Exception as exc:
                result = self._result_document(initial.document, initial.document.get("scope", {}),
                    goal_status="unknown", blockers=[self._blocker("terminal_scan_incomplete", str(exc))],
                    batches=executed_batches, modified=modified, mutation_started=started)
        result = {**result, "modified": modified, "mutation_started": started,
            "run_operation_id": initial.operation_id, "rounds": rounds}
        manifest = Path(initial.document["plan_path"]).with_suffix(".run.json")
        write_new_json(manifest, self._metadata(result))
        result["run_receipt_path"] = str(manifest)
        return result

    def status_operation(
        self,
        *,
        operation_id: str | None = None,
        plan_path: Path | None = None,
        operation_home: Path | None = None,
        codex_home: Path | None = None,
        scope: Mapping[str, Any] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        self._emit_progress(
            progress_callback,
            "status",
            "started",
            operation_id=operation_id,
        )
        live = self._live.get(str(operation_id or ""))
        if live is not None:
            result = (
                self._status_for_document(live.document)
                if self._blocked_without_mutation(live.document) else dict(live.result)
                if live.result is not None
                else self._status_for_document(live.document)
            )
            self._emit_progress(
                progress_callback,
                "status",
                "completed",
                operation_id=str(result.get("operation_id") or operation_id or ""),
                counts={"batch_count": len(result.get("batches", ()) or ())},
            )
            return result
        try:
            from .child_recovery import query_child_operation
            child_result = query_child_operation(self, plan_path or _find_operation_plan(
                operation_id, operation_home=operation_home, codex_home=codex_home),
                operation_id=operation_id, codex_home=codex_home, verify=False,
                progress_callback=progress_callback)
            if child_result is not None:
                return child_result
            result = self._status_for_document(
                self._load_plan(
                    operation_id,
                    plan_path,
                    None,
                    operation_home=operation_home,
                    codex_home=codex_home,
                )
            )
            self._emit_progress(
                progress_callback,
                "status",
                "completed",
                operation_id=str(result.get("operation_id") or operation_id or ""),
                counts={"batch_count": len(result.get("batches", ()) or ())},
            )
            return result
        except Exception as exc:
            self._emit_progress(
                progress_callback,
                "status",
                "failed",
                operation_id=operation_id,
            )
            return self._error_document(
                "status", dict(scope or {}), str(exc) or repr(exc),
                operation_id=operation_id, blocker_code="operation_query_unavailable",
            )

    def verify_operation(
        self,
        *,
        operation_id: str | None = None,
        plan_path: Path | None = None,
        operation_home: Path | None = None,
        codex_home: Path | None = None,
        orca_roots: Sequence[str | Path] = (),
        workbuddy_roots: Sequence[str | Path] = (),
        scope: Mapping[str, Any] | None = None,
        adapters: Iterable[Any] | None = None,
        verify_timeout: int = 180,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        document: Mapping[str, Any] | None = None
        computed: Mapping[str, Any] | None = None
        try:
            from .child_recovery import query_child_operation
            child_result = query_child_operation(self, plan_path or _find_operation_plan(
                operation_id, operation_home=operation_home, codex_home=codex_home),
                operation_id=operation_id, codex_home=codex_home, verify=True,
                progress_callback=progress_callback)
            if child_result is not None:
                return child_result
            live = self._live.get(str(operation_id or ""))
            document = live.document if live is not None else self._load_plan(
                operation_id, plan_path, None,
                operation_home=operation_home, codex_home=codex_home,
            )
            self._bound_workbuddy_roots(document, workbuddy_roots)
            if self._blocked_without_mutation(document):
                return self._status_for_document(document)
            with mutation_roots(frozen_operation_roots(document)):
                computed = self._verify_operation_locked(
                    operation_id=operation_id, plan_path=plan_path,
                    operation_home=operation_home, codex_home=codex_home,
                    scope=scope, adapters=adapters, orca_roots=orca_roots, verify_timeout=verify_timeout,
                    workbuddy_roots=workbuddy_roots,
                    progress_callback=progress_callback,
                )
            return dict(computed)
        except Exception as exc:
            if document is not None:
                status = self._status_for_document(document)
                prior = computed or (live.result if live is not None else None) or {}
                result = self._result_document(
                    document, dict(scope or {}), goal_status="unknown",
                    blockers=[self._blocker(getattr(exc, "kind", "operation_verify_failed"), str(exc))],
                    batches=prior.get("batches") or status.get("batches", ()),
                    modified=bool(prior.get("modified")) or bool(status.get("modified")),
                    mutation_started=bool(prior.get("mutation_started")) or bool(status.get("mutation_started")),
                )
                if live is not None:
                    live.result = result
                return result
            return self._error_document(
                "verify", dict(scope or {}), str(exc) or repr(exc),
                operation_id=operation_id,
                blocker_code=getattr(exc, "kind", "operation_verify_failed"),
            )

    def _verify_operation_locked(
        self,
        *,
        operation_id: str | None = None,
        plan_path: Path | None = None,
        operation_home: Path | None = None,
        codex_home: Path | None = None,
        orca_roots: Sequence[str | Path] = (),
        workbuddy_roots: Sequence[str | Path] = (),
        scope: Mapping[str, Any] | None = None,
        adapters: Iterable[Any] | None = None,
        verify_timeout: int = 180,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        del verify_timeout
        self._emit_progress(
            progress_callback,
            "verify",
            "started",
            operation_id=operation_id,
        )

        def finish(result: dict[str, Any]) -> dict[str, Any]:
            residuals = result.get("residuals", ())
            try:
                residual_count = len(residuals or ())
            except TypeError:
                residual_count = 0
            goal_status = str(result.get("goal_status") or "unknown")
            self._emit_progress(
                progress_callback,
                "verify",
                "completed" if goal_status in {
                    "complete", "completed_with_residuals"
                } else "failed",
                operation_id=str(result.get("operation_id") or operation_id or ""),
                counts={"residual_count": residual_count},
                goal_status=goal_status,
            )
            return result

        live = self._live.get(str(operation_id or ""))
        terminal: Any | None = None
        error: str | None = None
        if live is None:
            try:
                document = self._load_plan(
                    operation_id,
                    plan_path,
                    None,
                    operation_home=operation_home,
                    codex_home=codex_home,
                )
                if document["schema_version"] == PLAN_V3:
                    from .orca_target_safety import recheck_document_targets
                    safety_errors = (*self._orca_runtime_recovery_errors(document),
                                     *recheck_document_targets(document, phase="readonly_recovery"))
                    if safety_errors:
                        inspection = self._inspect_child_states(document)
                        return finish(self._result_document(document, dict(scope or {}), goal_status="unknown",
                            blockers=[self._blocker(code, code, scope="target_safety") for code in safety_errors],
                            batches=inspection.batches, modified=inspection.modified,
                            mutation_started=inspection.mutation_started))
                client_name = str(document["scope"]["client"])
                codex_home = self._bound_codex_home(document, codex_home)
                discovery_roots = ((*validate_guard_sources(document), *orca_roots)
                                   if document["schema_version"] == PLAN_V3 else orca_roots)
                source = tuple(adapters) if adapters is not None else tuple(self._default_adapters(
                    client_name, codex_home=codex_home, orca_roots=discovery_roots,
                    workbuddy_roots=self._bound_workbuddy_roots(document, workbuddy_roots)))
                if document["schema_version"] == PLAN_V3:
                    source = retain_guard_sources(source, validate_guard_sources(document))
                source = self._with_current_guard_sources(source, client_name, codex_home=codex_home, orca_roots=orca_roots)
                source = refresh_guard_sources(retain_guard_sources(source, (*validate_guard_sources(document), *orca_roots)))
                self._emit_progress(
                    progress_callback,
                    "inventory",
                    "started",
                    operation_id=str(document.get("operation_id") or operation_id or ""),
                )
                (
                    context,
                    active_adapters,
                    manual_catalog,
                    manual_plan,
                    manual_actions,
                    action_contexts,
                ) = self._build_context(
                    "native" if document.get("schema_version") == PLAN_V3 else client_name,
                    source,
                    inventory_adapters=self._orca_recovery_sources(document, source),
                    explicit_frontend_ids=tuple(document.get("scope", {}).get("record_ids", ())),
                    engines=tuple(document.get("scope", {}).get("engines", ())),
                    explicit_session_ids=tuple(document.get("scope", {}).get("record_ids", ())),
                    include_action_contexts=True,
                    codex_home=codex_home,
                )
                self._emit_progress(
                    progress_callback,
                    "inventory",
                    "completed",
                    operation_id=str(document.get("operation_id") or operation_id or ""),
                    counts=self._progress_counts(context),
                )
                storage_blockers = self._frozen_store_blockers(document)
                if storage_blockers:
                    return finish(self._result_document(
                        document, dict(scope or {}), goal_status="unknown",
                        blockers=storage_blockers, batches=(),
                    ))
                live = _LiveOperation(
                    operation_id=str(document["operation_id"]),
                    document=document,
                    context=context,
                    # Do not bind verification to today's action IDs. The
                    # immutable batch signature is the residual identity.
                    candidates=(),
                    client=client_name,
                    adapters=tuple(active_adapters),
                    manual_actions=manual_actions,
                    manual_catalog=manual_catalog,
                    manual_plan=manual_plan,
                    action_contexts=action_contexts,
                )
                terminal = context
            except Exception:
                # The public wrapper has the approved document and durable
                # child evidence. A failed rebind must retain unknown/started
                # facts rather than become a new, unattempted blocked result.
                raise
        if terminal is None:
            if live.document["schema_version"] == PLAN_V3:
                from .orca_target_safety import recheck_document_targets
                safety_errors = (*self._orca_runtime_recovery_errors(live.document),
                                 *recheck_document_targets(live.document, phase="readonly_recovery"))
                if safety_errors:
                    inspection = self._inspect_child_states(live.document)
                    return finish(self._result_document(live.document, dict(scope or {}), goal_status="unknown",
                        blockers=[self._blocker(code, code, scope="target_safety") for code in safety_errors],
                        batches=inspection.batches, modified=inspection.modified,
                        mutation_started=inspection.mutation_started))
            source = tuple(adapters) if adapters is not None else live.adapters
            if live.document["schema_version"] == PLAN_V3:
                source = retain_guard_sources(source, validate_guard_sources(live.document))
            source = self._with_current_guard_sources(source, live.client, codex_home=self._bound_codex_home(live.document, codex_home), orca_roots=orca_roots)
            source = refresh_guard_sources(retain_guard_sources(source, (*validate_guard_sources(live.document), *orca_roots)))
            self._retain_live_guards(live, source, readonly_recovery=True)
            try:
                self._bound_codex_home(live.document, codex_home)
            except OperationCoordinatorError as exc:
                return finish(self._result_document(live.document, dict(scope or {}), goal_status="blocked",
                    blockers=[self._blocker("frozen_store_mismatch", str(exc))], batches=()))
            terminal, error = self._terminal_context(live, readonly_recovery=True)
        if error is not None:
            inspection = self._inspect_child_states(live.document)
            return finish(self._result_document(
                live.document, dict(scope or {}), goal_status="unknown",
                blockers=[self._blocker("terminal_scan_incomplete", error)], batches=inspection.batches,
                modified=inspection.modified, mutation_started=inspection.mutation_started,
            ))
        source_errors = required_source_errors(live.document, live.adapters)
        if source_errors:
            inspection = self._inspect_child_states(live.document)
            return finish(self._result_document(live.document, dict(scope or {}), goal_status="unknown",
                blockers=[self._blocker("guard_source_incomplete", "; ".join(source_errors))],
                batches=inspection.batches, modified=inspection.modified, mutation_started=inspection.mutation_started))
        if not bool(getattr(getattr(terminal, "plan", None), "scan_complete", True)):
            errors = getattr(getattr(terminal, "plan", None), "errors", ())
            message = "; ".join(str(value) for value in errors)
            if not message:
                message = "terminal verification scan is incomplete"
            return finish(self._result_document(
                live.document,
                live.document.get("scope", {}),
                goal_status="unknown",
                blockers=[self._blocker("terminal_scan_incomplete", message)],
                batches=(),
            ))
        try:
            residuals = self._residual_action_ids(live.document, terminal)
        except Exception as exc:
            return finish(self._result_document(
                live.document,
                live.document.get("scope", {}),
                goal_status="unknown",
                blockers=[self._blocker("terminal_scan_incomplete", str(exc))],
                batches=(),
            ))
        try:
            self._persist_verified_child_journals(live.document, residuals)
        except Exception as exc:
            return finish(self._result_document(
                live.document,
                live.document.get("scope", {}),
                goal_status="unknown",
                blockers=[self._blocker(
                    "verification_journal_failed", str(exc) or repr(exc)
                )],
                batches=(),
                residuals=residuals,
            ))
        result = self._result_document(
            live.document, dict(scope or {}),
            goal_status="completed_with_residuals" if residuals else "complete",
            blockers=[self._blocker("residual_records", "approved records remain")] if residuals else [],
            batches=(), residuals=residuals,
            modified=bool(self._status_for_document(live.document).get("modified")),
            mutation_started=bool(self._status_for_document(live.document).get("mutation_started")),
        )
        return finish(result)

    @staticmethod
    def _frozen_store_blockers(
        document: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Return blockers for stores involved in frozen child batches."""

        involved = {
            str(batch.get("storage_id") or "")
            for batch in document.get("child_batches", ())
            if isinstance(batch, Mapping)
        }
        involved.discard("")
        storages = {
            str(storage.get("storage_id") or ""): storage
            for storage in document.get("storages", ())
            if isinstance(storage, Mapping)
        }
        blockers: list[dict[str, Any]] = []
        for storage_id in sorted(involved):
            storage = storages.get(storage_id)
            if storage is None:
                blockers.append(OperationCoordinator._blocker(
                    "terminal_scan_incomplete",
                    f"approved store {storage_id!r} is missing from the frozen plan",
                    scope=f"storage:{storage_id}",
                ))
                continue
            if str(storage.get("scan_status") or "").casefold() != "ok":
                blockers.append(OperationCoordinator._blocker(
                    "terminal_scan_incomplete",
                    f"approved store {storage_id!r} has non-ok scan status",
                    scope=f"storage:{storage_id}",
                ))
                continue
            raw_path = storage.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                blockers.append(OperationCoordinator._blocker(
                    "terminal_scan_incomplete",
                    f"approved store {storage_id!r} has no trusted path",
                    scope=f"storage:{storage_id}",
                ))
                continue
            try:
                present = Path(raw_path).expanduser().exists()
            except OSError:
                present = False
            if not present:
                blockers.append(OperationCoordinator._blocker(
                    "terminal_scan_incomplete",
                    f"approved store {storage_id!r} is missing",
                    scope=f"storage:{storage_id}",
                ))
        return blockers

    @staticmethod
    def _bound_codex_home(document: Mapping[str, Any], supplied: Path | None) -> Path | None:
        if document.get("scope", {}).get("client") != "native":
            return supplied
        paths = {canonical_path(str(item["path"])) for item in document.get("storages", ()) if item.get("path")}
        if not paths:
            raise OperationCoordinatorError("frozen_store_identity_invalid: native plan has no physical store")
        if len(paths) > 1:
            if supplied is not None:
                raise OperationCoordinatorError("frozen_store_mismatch: a single home cannot override multiple frozen stores")
            return None
        path = Path(next(iter(paths)))
        if supplied is not None and canonical_path(supplied) != canonical_path(path):
            raise OperationCoordinatorError("frozen_store_mismatch: explicit Codex home differs from the approved store")
        return path

    @staticmethod
    def _assert_store_coverage(document: Mapping[str, Any], context: Any) -> None:
        involved = {str(b.get("storage_id")) for b in document.get("child_batches", ())}
        fresh = {str(s.storage_id): s for s in getattr(context.plan, "storages", ())}
        for approved in document.get("storages", ()):
            key = str(approved.get("storage_id"))
            if key not in involved:
                continue
            current = fresh.get(key)
            status = getattr(current, "scan_status", None)
            status = getattr(status, "value", status)
            if (current is None or status != "ok" or
                    canonical_path(current.path) != canonical_path(approved["path"])):
                raise OperationCoordinatorError(f"terminal_scan_incomplete: approved store {key} was not successfully scanned")
        resources = set(getattr(context, "frontend_scan_coverage", ()))
        families = {
            "remove_frontend_reference": ("frontend_database_paths", "sessions"),
            "delete_frontend_session": ("frontend_session_database_paths", "sessions"),
            "delete_project_item": ("frontend_project_database_paths", "projects"),
        }
        for action in document.get("actions", ()):
            contract = families.get(action.get("kind"))
            if contract is None:
                continue
            field, family = contract
            paths = action.get("impact", {}).get(field, ())
            if not paths or any((canonical_path(path), family) not in resources for path in paths):
                raise OperationCoordinatorError("terminal_scan_incomplete: exact frontend database and discovery family were not successfully scanned")

    @staticmethod
    def _action_signature(
        storage_id: object,
        mutation_family: object,
        resource_key: object,
        target: object,
    ) -> tuple[str, str, tuple[str, ...], str] | None:
        target_id = (
            target.get("thread_id")
            if isinstance(target, Mapping)
            else getattr(target, "thread_id", None)
        )
        if not isinstance(target_id, str) or not target_id:
            return None
        if isinstance(resource_key, (list, tuple)):
            normalized_resource = tuple(str(value) for value in resource_key)
        elif resource_key in (None, ""):
            normalized_resource = ()
        else:
            normalized_resource = (str(resource_key),)
        return (
            str(storage_id),
            str(mutation_family),
            normalized_resource,
            target_id,
        )

    @classmethod
    def _frozen_action_signatures(
        cls,
        document: Mapping[str, Any],
    ) -> dict[tuple[str, str, tuple[str, ...], str], tuple[str, ...]]:
        actions = {
            str(action.get("action_id")): action
            for action in document.get("actions", ())
            if isinstance(action, Mapping) and action.get("action_id")
        }
        signatures: dict[tuple[str, str, tuple[str, ...], str], list[str]] = {}
        for batch in document.get("child_batches", ()):
            if not isinstance(batch, Mapping):
                continue
            for raw_id in batch.get("action_ids", ()):
                action_id = str(raw_id)
                action = actions.get(action_id)
                signature = cls._action_signature(
                    batch.get("storage_id"),
                    batch.get("mutation_family"),
                    batch.get("resource_key", ()),
                    action.get("target") if action else None,
                )
                if signature is None:
                    raise OperationCoordinatorError(
                        f"frozen action {action_id!r} has no target signature"
                    )
                signatures.setdefault(signature, []).append(action_id)
        return {key: tuple(value) for key, value in signatures.items()}

    @classmethod
    def _fresh_action_signatures(
        cls,
        context: Any,
    ) -> set[tuple[str, str, tuple[str, ...], str]]:
        from .cleanup_service import partition_actions

        signatures: set[tuple[str, str, tuple[str, ...], str]] = set()
        for batch in partition_actions(getattr(context.plan, "actions", ())):
            for action in batch.actions:
                signature = cls._action_signature(
                    batch.storage_id,
                    batch.mutation_family,
                    batch.resource_key,
                    getattr(action, "target", None),
                )
                if signature is None:
                    raise OperationCoordinatorError(
                        "fresh action has no target signature"
                    )
                signatures.add(signature)
        return signatures

    @classmethod
    def _residual_action_ids(
        cls,
        document: Mapping[str, Any],
        context: Any,
    ) -> list[str]:
        """Map terminal residuals back to the immutable plan action IDs."""

        if document.get("schema_version") == PLAN_V3:
            return cls._orca_native_residual_action_ids(document)

        cls._assert_store_coverage(document, context)
        frozen = cls._frozen_action_signatures(document)
        fresh = cls._fresh_action_signatures(context)
        residuals: list[str] = []
        for signature, action_ids in frozen.items():
            if signature in fresh:
                residuals.extend(action_ids)
        # Action families change after native deletion (e.g. Desktop state).
        # Verify record identity, not disappearance of a former action type.
        present = {(str(item.target.storage_id), str(item.target.thread_id))
                   for item in getattr(context.plan, "conversations", ())}
        present.update((sig[0], sig[3]) for sig in fresh)
        native_actions = [a for a in document.get("actions", ())
                          if a.get("kind") in {"delete_conversation", "remove_desktop_state"}]
        ids_by_store: dict[str, set[str]] = {}
        for action in native_actions:
            target = action.get("target", {})
            ids = {str(target.get("thread_id", ""))} - {""}
            for key in ("affected_thread_ids", "descendant_thread_ids", "indexed_thread_ids"):
                ids.update(str(value) for value in action.get("impact", {}).get(key, ()))
            ids_by_store.setdefault(str(target.get("storage_id")), set()).update(ids)
        # Catalog-free JSON references cannot be rediscovered as conversations.
        # Ask for the exact frozen IDs, once per physical store.
        from .codex_desktop_state import read_desktop_state
        for storage in document.get("storages", ()):
            key = str(storage.get("storage_id"))
            ids = ids_by_store.get(key)
            if ids:
                snapshot = read_desktop_state(Path(storage["path"]), ids)
                present.update((key, record_id) for record_id, state in snapshot.threads.items() if state.present)
        for action in native_actions:
            target = action.get("target", {})
            impact = action.get("impact", {})
            ids = {str(target.get("thread_id", ""))}
            for key in ("affected_thread_ids", "descendant_thread_ids", "indexed_thread_ids"):
                ids.update(str(value) for value in impact.get(key, ()))
            if any((str(target.get("storage_id")), record_id) in present for record_id in ids):
                residuals.append(str(action["action_id"]))
        # A crash can leave only sidebar/mapping keys, without a registration
        # from which normal discovery could construct an action.
        from .native_project_cleanup import remaining_native_project_markers, verify_native_project_recovery
        recovery_by_home: dict[str, list[Mapping[str, Any]]] = {}
        for action in document.get("actions", ()):
            if action.get("kind") == "delete_native_project":
                evidence = action["impact"]["external_action_payload"]["native_project_evidence"]
                recovery_by_home.setdefault(evidence["home"], []).append(evidence)
        for home, items in recovery_by_home.items():
            verify_native_project_recovery(Path(home), items)
        for action in document.get("actions", ()):
            if action.get("kind") != "delete_native_project":
                continue
            evidence = action["impact"]["external_action_payload"]["native_project_evidence"]
            if remaining_native_project_markers(
                Path(evidence["home"]), (evidence["project_id"],),
                required_files=tuple(n for n, h in evidence["file_sha256"].items() if h),
                mapped_ids=tuple(evidence["mapped_ids"]),
            ):
                residuals.append(str(action["action_id"]))
        from .cindy_schedule_cleanup import remaining as remaining_schedule_runs
        schedule_groups: dict[str, list[Mapping[str, Any]]] = {}
        schedule_actions: dict[tuple[str, str], str] = {}
        for action in document.get("actions", ()):
            if action.get("kind") == "delete_schedule_run":
                evidence = action["impact"]["external_action_payload"]["schedule_run_evidence"]
                schedule_groups.setdefault(evidence["database"], []).append(evidence)
                schedule_actions[(evidence["database"], evidence["run_id"])] = action["action_id"]
        for database, evidence in schedule_groups.items():
            for run_id in remaining_schedule_runs(evidence):
                residuals.append(schedule_actions[(database, run_id)])
        from .workbuddy_store import remaining as remaining_workbuddy
        workbuddy_groups, workbuddy_actions = {}, {}
        for action in document.get("actions", ()):
            if action.get("kind") in {"delete_workbuddy_session", "remove_workbuddy_ui_reference"}:
                evidence = action["impact"]["external_action_payload"]["workbuddy_session_evidence"]
                workbuddy_groups.setdefault((evidence["root"], action["kind"]), []).append(evidence)
                workbuddy_actions[(evidence["root"], evidence["session_id"])] = action["action_id"]
        for (root, family), evidence in workbuddy_groups.items():
            terminal_verified = cls._workbuddy_terminal_verified(document, root, family)
            for session_id in remaining_workbuddy(evidence, terminal_verified=terminal_verified):
                residuals.append(workbuddy_actions[(root, session_id)])
        return list(dict.fromkeys(residuals))

    @classmethod
    def _workbuddy_terminal_verified(cls, document: Mapping[str, Any], root: str, family: str = "delete_workbuddy_session") -> bool:
        """Trust only a completed child bound to this exact authorization."""
        storages = {str(item["storage_id"]): str(item["path"]) for item in document.get("storages", ())}
        batches = [batch for batch in document.get("child_batches", ())
                   if batch.get("mutation_family") == family
                   and storages.get(str(batch.get("storage_id"))) == root]
        if len(batches) != 1:
            return False
        batch = batches[0]
        action_ids = list(batch["action_ids"])
        approved = [action for action in document["actions"] if action["action_id"] in action_ids]
        if len(approved) != len(action_ids):
            return False
        projection = {"schema_version": "larj.child-operation-plan.v1",
            "operation_id": batch["child_operation_id"],
            "target": {"codex_home": root, "storage_id": batch["storage_id"]},
            "parent_operation_id": document["operation_id"], "mutation_family": batch["mutation_family"],
            "actions": cls._metadata(approved)}
        store = OperationStore(Path(root), batch["child_operation_id"])
        if not store.directory.exists() or store.lock_exists():
            return False
        result = store.read_result()
        expected_hash = plan_sha256(projection)
        if result is not None:
            return bool(result.get("goal_status") == "complete"
                and result.get("plan_sha256") == expected_hash
                and result.get("mutation_started") is True
                and sorted(result.get("action_ids", ())) == sorted(action_ids)
                and sorted(result.get("verified_action_ids", ())) == sorted(action_ids)
                and result.get("verification", {}).get("all_satisfied") is True)
        # Successful apply has a durable action_verified checkpoint and final
        # batch_finished event even before a result is compacted to a receipt.
        # WorkBuddy emits action_verified only after its strict after proof and
        # temporary rollback cleanup have both succeeded.
        child = store.read_plan()
        state = store.read_state()
        events = store.read_events()
        verified = {event.get("action_id") for event in events if event.get("event") == "action_verified"}
        if events and events[-1].get("event") == "verification_finished":
            verified.update(events[-1].get("verified_action_ids", ()))
        return bool(child.get("plan_sha256") == expected_hash and state is not None
            and state.get("goal_status") == "complete" and state.get("phase") == "finished"
            and state.get("mutation_started") is True and state.get("current_action_state") == "verified"
            and events and events[-1].get("goal_status") == "complete"
            and events[-1].get("event") in {"batch_finished", "verification_finished"}
            and verified == set(action_ids))

    @staticmethod
    def _orca_native_residual_action_ids(document: Mapping[str, Any]) -> list[str]:
        """Read frozen native artifacts without consulting today's abilities."""
        from .cleaner import verify_finding_deleted
        from .models import Finding
        from .orca_target_safety import evidence_for_actions

        residuals = []
        for evidence in evidence_for_actions(document, document.get("actions", ())):
            frozen = evidence["frozen"]
            home = Path(frozen["home"])
            finding = Finding(platform="native", platform_session_id=frozen["record_id"],
                thread_id=frozen["record_id"], reason="frozen read-only recovery",
                platform_db=home / "state_5.sqlite", codex_home=home,
                details={"planned_impact_thread_ids": frozen["affected_thread_ids"],
                         "planned_known_artifact_paths": frozen["rollout_paths"]})
            verification = verify_finding_deleted(finding)
            if verification.status == "unknown":
                raise OperationCoordinatorError(verification.error or "native residual inspection incomplete")
            present = not verification.deleted
            # The API also rewrites the legacy sidebar index. Read only IDs;
            # malformed data is not evidence that the frozen IDs are absent.
            index = home / "session_index.jsonl"
            try:
                handle = index.open("r", encoding="utf-8")
            except FileNotFoundError:
                handle = None
            if handle is not None:
                with handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                            raise OperationCoordinatorError("native session index inspection incomplete")
                        present = present or row["id"] in frozen["affected_thread_ids"]
            if present:
                residuals.append(str(evidence["action_id"]))
        return residuals

    def _persist_verified_child_journals(
        self,
        document: Mapping[str, Any],
        residuals: Sequence[str] = (),
    ) -> None:
        """Persist a terminal read-only verdict for executed child batches.

        Verification is authoritative after an ambiguous mutation.  It may
        therefore move an existing ``unknown`` child to a terminal verdict,
        but it never creates a journal for a child that was never opened.
        """
        storage_by_id = {
            str(storage.get("storage_id")): Path(str(storage.get("path")))
            for storage in document.get("storages", ())
            if isinstance(storage, Mapping)
            and storage.get("storage_id")
            and storage.get("path")
        }
        for raw_batch in document.get("child_batches", ()):
            if not isinstance(raw_batch, Mapping):
                continue
            child_id = str(raw_batch.get("child_operation_id") or "")
            path = storage_by_id.get(str(raw_batch.get("storage_id") or ""))
            if not child_id or path is None:
                continue
            store = OperationStore(path, child_id)
            if not store.directory.exists():
                continue
            existing = store.read_result()
            if existing is not None and str(
                existing.get("goal_status") or ""
            ) in {"complete", "completed_with_residuals"}:
                continue
            if not store.state_path.exists():
                continue
            action_ids = [
                str(value) for value in raw_batch.get("action_ids", ())
            ]
            residual_set = {str(value) for value in residuals}
            child_residuals = [
                action_id for action_id in action_ids
                if action_id in residual_set
            ]
            verified = [
                action_id for action_id in action_ids
                if action_id not in set(child_residuals)
            ]
            goal = (
                "completed_with_residuals"
                if child_residuals
                else "complete"
            )
            with store.mutation_lock():
                current = store.read_result()
                if current is not None and str(
                    current.get("goal_status") or ""
                ) in {"complete", "completed_with_residuals"}:
                    continue
                state = store.read_state()
                if document.get("schema_version") == PLAN_V3 and state is not None and not state.get("mutation_started"):
                    startup_events = [event for event in store.read_events() if event["event"] == "orca_runtime_startup"]
                    if len(startup_events) == 1:
                        state = {**state, "mutation_started": True, "runtime_instance": startup_events[0]["runtime_instance"]}
                if state is None or not (
                    bool(state.get("mutation_started"))
                    or bool(state.get("modified"))
                ):
                    continue
                # WorkBuddy's residual check has proved the complete frozen
                # after-state before reaching this journal update. A started
                # mutation with every approved record removed is modified,
                # even when an interruption prevented an execution result.
                verified_modified = bool(state.get("modified")) or bool(
                    raw_batch.get("mutation_family") in {
                        "delete_workbuddy_session", "remove_workbuddy_ui_reference"
                    }
                    and state.get("mutation_started")
                    and action_ids and not child_residuals
                )
                store.append_event(
                    {
                        "event": "verification_finished",
                        "goal_status": goal,
                        "verified_action_ids": verified,
                        "residual_action_ids": child_residuals,
                    },
                    state_updates={
                        "phase": "finished",
                        "mutation_started": bool(state.get("mutation_started")),
                        "modified": verified_modified,
                        **({"runtime_instance": state["runtime_instance"]} if "runtime_instance" in state else {}),
                        "goal_status": goal,
                        "goal_satisfied": goal == "complete",
                        "current_action_state": "verified",
                    },
                )
                plan = store.read_plan()
                blockers = (
                    [self._blocker(
                        "residual_records",
                        "approved records remain",
                        scope=f"child:{child_id}",
                    )]
                    if child_residuals
                    else []
                )
                result = {
                    "schema_version": "larj.agent-result.v1",
                    "document_type": "operation_result",
                    "command": "agent",
                    "subcommand": "verify",
                    "mode": "agent",
                    "phase": "finished",
                    "operation_id": child_id,
                    "plan_sha256": str(plan.get("plan_sha256") or ""),
                    "goal_status": goal,
                    "goal_satisfied": goal == "complete",
                    "modified": verified_modified,
                    "mutation_started": bool(state.get("mutation_started")),
                    "blockers": blockers,
                    "counts": dict(plan.get("counts") or {
                        "action_count": len(action_ids),
                        "batch_count": 1,
                    }),
                    "action_ids": action_ids,
                    "verified_action_ids": verified,
                    "verification": {
                        "all_satisfied": goal == "complete",
                        "verified_action_ids": verified,
                    },
                    "final_scope_verification": {
                        "all_satisfied": goal == "complete",
                        "scan_complete": True,
                    },
                }
                store.write_result(result)
                store.compact_completed(result)

    plan_delete = plan_operation
    apply_delete = apply_operation
    run_delete = run_operation
    get_operation_status = status_operation
    verify_delete = verify_operation

    def _scope(
        self,
        scope: Mapping[str, Any] | None,
        *,
        client: str | None,
        projects: Sequence[str],
        all_projects: bool,
        record_ids: Sequence[str],
        engines: Sequence[str],
    ) -> dict[str, Any]:
        raw = dict(scope or {})
        selected_client = client or raw.get("client")
        normalized_client = normalize_client(selected_client) if selected_client else None
        return {
            "client": normalized_client,
            "projects": tuple(dict.fromkeys(
                str(value).strip()
                for value in (projects or raw.get("projects", ()))
                if str(value).strip()
            )),
            "all_projects": bool(all_projects or raw.get("all_projects", False)),
            "record_ids": tuple(dict.fromkeys(
                str(value).strip()
                for value in (record_ids or raw.get("record_ids", ()))
                if str(value).strip()
            )),
            "engines": tuple(dict.fromkeys(
                normalize_engine(value)
                for value in (engines or raw.get("engines", ()))
                if str(value).strip()
            )),
        }

    @staticmethod
    def _validate_scope(scope: Mapping[str, Any], *, require_selection: bool) -> None:
        if not scope.get("client"):
            raise OperationCoordinatorError("operation requires one client")
        selected = sum(bool(scope.get(name)) for name in ("projects", "all_projects", "record_ids"))
        if require_selection and selected != 1:
            raise OperationCoordinatorError(
                "operation requires exactly one project, all-projects, or record-id scope"
            )
        if not require_selection and selected > 1:
            raise OperationCoordinatorError("operation scope selectors conflict")

    @inventory_path_identity_scope()
    def _build_context(
        self,
        client: str,
        adapters: tuple[Any, ...] | None,
        *,
        inventory_adapters: tuple[Any, ...] | None = None,
        engines: Sequence[str] = (),
        explicit_session_ids: Sequence[str] = (),
        include_action_contexts: bool = False,
        codex_home: Path | None = None,
        orca_roots: Sequence[str | Path] = (),
        herdr_roots: Sequence[str | Path] = (),
        workbuddy_roots: Sequence[str | Path] = (),
        explicit_frontend_ids: Sequence[str] = (),
    ) -> tuple[Any, ...]:
        from .client_capability_guards import restrict_cleanup_context

        explicit_session_ids = tuple(dict.fromkeys((*explicit_session_ids, *explicit_frontend_ids)))
        explicit_frontend_ids = explicit_session_ids
        source = (tuple(adapters) if adapters is not None else
                  () if client in {"pi", "claude"} else
                  tuple(self._default_adapters(client, codex_home=codex_home, orca_roots=orca_roots,
                                               herdr_roots=herdr_roots, workbuddy_roots=workbuddy_roots)))
        # Recovery inventories only its frozen stores. Ambient adapters still
        # provide protection evidence without becoming new inventory targets.
        candidates = source if inventory_adapters is None else inventory_adapters
        guards = self._with_current_guard_sources(source, client, codex_home=codex_home, orca_roots=orca_roots)
        if client == "orca" and explicit_frontend_ids:
            qualified = self._qualified_orca_context(candidates, guards, explicit_frontend_ids, engines)
            if qualified is not None:
                return qualified if include_action_contexts else qualified[:5]
        result = self._build_context_sources(client, candidates, engines=engines,
            include_action_contexts=include_action_contexts, codex_home=codex_home,
            explicit_session_ids=explicit_session_ids)
        context = restrict_cleanup_context(result[0], guards, self.service.typed_actions)
        active = context.active_adapters
        catalog, manual_plan = result[2:4]
        if catalog is not None:
            catalog = replace(catalog, active_adapters=active)
        if manual_plan is not None:
            manual_plan = replace(manual_plan, active_adapters=active)
        if include_action_contexts:
            bindings = {action_id: restrict_cleanup_context(bound, guards, self.service.typed_actions)
                        for action_id, bound in result[5].items()}
            return (context, active, catalog, manual_plan, result[4], bindings)
        return (context, active, catalog, manual_plan, result[4])

    def _qualified_orca_context(self, candidates, guards, selectors, engines):
        """The first mutation family is an exact selected native-only closure."""
        from .orca_runtime import RUNTIME_ACCEPTED
        if not RUNTIME_ACCEPTED or os.name != "nt" or engines and tuple(engines) != ("codex",):
            return None
        from .client_contracts import describe_adapter
        from .orca_target_safety import plan_target_evidence
        from .orca_authorization import coordinator_scope
        from .adapters import NativeIntegrityAdapter
        from .inventory import build_session_catalog
        from .manual_delete import build_manual_delete_plan
        from .client_capability_guards import restrict_cleanup_context
        selected = tuple(a for a in candidates if describe_adapter(a).client == "orca")
        if not selected:
            return None
        readonly, catalog = self._readonly_client_context(selected, "orca", engines)
        def inventory_only():
            context = restrict_cleanup_context(readonly, guards, self.service.typed_actions)
            return context, context.active_adapters, replace(catalog, active_adapters=context.active_adapters), None, {}, {}
        scope = {"client": "orca", "record_ids": tuple(selectors), "engines": ("codex",)}
        evidence = plan_target_evidence(readonly, scope, guards, (), (), catalog)
        if not evidence or any(not value.get("native_delete") for value in evidence):
            return inventory_only()
        homes = {value["frozen"]["home"]: value["frozen"]["binary"]["path"] for value in evidence}
        if len(homes) != 1:
            return inventory_only()
        native = tuple(NativeIntegrityAdapter(codex_home=Path(home), codex_bin_hint=Path(binary))
                       for home, binary in homes.items())
        with coordinator_scope(evidence):
            catalog = build_session_catalog((*native, *guards))
            manual = build_manual_delete_plan(catalog)
            result = self._native_manual_context(native, catalog, manual)
            context = restrict_cleanup_context(result[0], (*native, *guards), self.service.typed_actions)
        return context, context.active_adapters, replace(result[2], active_adapters=context.active_adapters), \
            replace(result[3], active_adapters=context.active_adapters), result[4], {}

    def _build_context_sources(
        self,
        client: str,
        adapters: tuple[Any, ...] | None,
        *,
        engines: Sequence[str] = (),
        include_action_contexts: bool = False,
        codex_home: Path | None = None,
        explicit_session_ids: Sequence[str] = (),
    ) -> tuple[Any, ...]:
        if client in {"pi", "claude"}:
            args = self._default_catalog_args(client, codex_home=codex_home)
            if client == "pi":
                from .session_catalog_factory import build_pi_catalog

                builder = lambda: build_pi_catalog(args)
            else:
                from .session_catalog_factory import build_claude_catalog

                builder = lambda: build_claude_catalog(args)
            catalog = builder()
            result = (
                self.service.prepare_session_catalog(
                    client,
                    catalog,
                    catalog_builder=builder,
                    target_root=None,
                ),
                (),
                None,
                None,
                {},
                {},
            )
            return result if include_action_contexts else result[:5]
        source = (
            tuple(adapters)
            if adapters is not None
            else tuple(self._default_adapters(client, codex_home=codex_home))
        )
        selected = tuple(adapter for adapter in source if self._adapter_matches(adapter, client))
        if client == "workbuddy":
            from .workbuddy_store import build_context
            context = build_context(selected, self.service, engines=engines, refresh=True)
            result = (context, selected, None, None, {}, {})
            return result if include_action_contexts else result[:5]
        if selected and all(callable(getattr(adapter, "snapshot_references", None))
                            and not callable(getattr(adapter, "scan", None)) for adapter in selected):
            context, catalog = self._readonly_client_context(selected, client, engines)
            result = (context, selected, catalog, None, {}, {})
            return result if include_action_contexts else result[:5]
        if client in {"native", "codex-desktop"}:
            # Native inventory is the authoritative healthy-record snapshot.
            # The regular anomaly scanner is only added for an incomplete or
            # suspicious catalog; otherwise calling both scanners would walk
            # every rollout twice before one plan is written.
            from .inventory import build_session_catalog
            from .manual_delete import build_manual_delete_plan
            from .native_project_cleanup import merge_project_context

            catalog = build_session_catalog(selected)
            manual_plan = build_manual_delete_plan(catalog)
            # Exact IDs can outlive both the native record and Desktop catalog
            # as JSON-only UI references. Keep this discovery explicitly scoped;
            # project/all-projects selection must not absorb arbitrary JSON IDs.
            from .adapters import NativeIntegrityAdapter
            from .codex_desktop_state import read_desktop_state

            catalog_ids = {record.thread_id for record in catalog.records}
            desktop_ids = tuple(
                value for value in explicit_session_ids
                if value not in catalog_ids and self._is_full_thread_id(value)
            )
            scoped = []
            has_desktop_residuals = False
            for adapter in selected:
                if isinstance(adapter, NativeIntegrityAdapter):
                    if adapter.desktop_thread_ids != desktop_ids:
                        adapter = copy(adapter)
                        adapter.desktop_thread_ids = desktop_ids
                    if desktop_ids:
                        desktop = read_desktop_state(adapter.codex_home, desktop_ids)
                        has_desktop_residuals |= any(state.present for state in desktop.threads.values())
                scoped.append(adapter)
            if not has_desktop_residuals and self._native_catalog_is_healthy(catalog, manual_plan):
                result = self._native_manual_context(
                    selected,
                    catalog,
                    manual_plan,
                )
                result = (merge_project_context(result[0], selected, self.service), *result[1:])
                result = (replace(result[0], frontend_scan_coverage=tuple(
                    (canonical_path(path), "sessions") for path in catalog.scanned_session_databases)), *result[1:])
                return (*result, {}) if include_action_contexts else result
            context = self.service.prepare(
                tuple(scoped),
                platforms=("native" if client == "codex-desktop" else client,),
            )
            # The explicit Desktop IDs belong to this scan, not to a new
            # protection source. Retain the original adapters and their exact
            # capability ceilings, so later scans observe each source once.
            context = replace(context, snapshot=replace(context.snapshot, active_adapters=selected))
            result = self._merge_native_manual_records(
                context,
                selected,
                catalog=catalog,
                manual_plan=manual_plan,
            )
            result = (merge_project_context(result[0], selected, self.service), *result[1:])
            result = (replace(result[0], frontend_scan_coverage=tuple(
                (canonical_path(path), "sessions") for path in catalog.scanned_session_databases)), *result[1:])
            return (*result, {}) if include_action_contexts else result
        scan_adapters = selected
        scan_platforms = ("native" if client == "codex-desktop" else client,)
        if client == "cindy" and (not engines or "codex" in engines):
            from .adapters import NativeIntegrityAdapter

            # Audit only the selected client's physical stores. Orphan source
            # parents and residual edges require the native integrity evidence
            # that the frontend scanner cannot supply.
            homes = {adapter.codex_home: adapter for adapter in selected
                     if isinstance(getattr(adapter, "codex_home", None), Path)}
            scan_adapters = (*selected, *(
                NativeIntegrityAdapter(
                    codex_home=home,
                    codex_bin_hint=getattr(adapter, "codex_bin_hint", None),
                )
                for home, adapter in homes.items()
            ))
            scan_platforms = ("cindy", "native")
        context = self.service.prepare(scan_adapters, platforms=scan_platforms)
        if client not in {"cindy", "aionui"}:
            result = (context, selected, None, None, {}, {})
            return result if include_action_contexts else result[:5]
        result = self._merge_client_engine_contexts(
            context,
            selected,
            client=client,
            engines=engines,
            explicit_session_ids=explicit_session_ids,
        )
        if client == "cindy":
            from .cindy_schedule_cleanup import merge_context
            result = (merge_context(result[0], selected, self.service, explicit_session_ids), *result[1:])
        return result if include_action_contexts else result[:5]

    def _readonly_client_context(self, adapters: tuple[Any, ...], client: str, engines: Sequence[str]) -> tuple[Any, Any]:
        """Inventory a pure client without calling legacy scan or fabricating actions."""
        from .cleaner import ScanReport
        from .client_inventory import build_client_engine_contexts, build_client_inventory

        inventory = build_client_inventory(adapters, client=client, engines=engines)
        contexts = build_client_engine_contexts(adapters, client=client, engines=engines, inventory=inventory)
        if contexts:
            inventory = contexts[0].inventory
        context = self.service.prepare_report(ScanReport(), active_adapters=adapters, platforms=(client,))
        from .inventory import SessionCatalog
        catalog = SessionCatalog(records=tuple(record for item in contexts if item.engine == "codex"
                                               for record in item.native_records), active_adapters=adapters)
        return replace(context, client_inventory=inventory), catalog

    def _retain_live_guards(self, live: _LiveOperation, adapters: tuple[Any, ...], *, readonly_recovery: bool = False) -> None:
        """Keep newly supplied protections without rebuilding the approved catalog."""
        from .client_capability_guards import restrict_cleanup_context
        from .client_contracts import describe_adapter

        if adapters == live.adapters and not any(describe_adapter(a).client == "orca" for a in adapters):
            return

        from contextlib import nullcontext
        from .orca_authorization import coordinator_scope
        ticket = (coordinator_scope(live.document["target_safety_evidence"])
                  if not readonly_recovery and live.document.get("schema_version") == PLAN_V3 and live.document.get("actions") else nullcontext())
        with ticket:
            live.context = restrict_cleanup_context(live.context, adapters, self.service.typed_actions)
        live.adapters = live.context.active_adapters
        live.action_contexts = {key: restrict_cleanup_context(bound, adapters, self.service.typed_actions)
                                for key, bound in live.action_contexts.items()}
        if live.manual_catalog is not None:
            live.manual_catalog = replace(live.manual_catalog, active_adapters=live.adapters)
        if live.manual_plan is not None:
            live.manual_plan = replace(live.manual_plan, active_adapters=live.adapters)

    @staticmethod
    def _is_full_thread_id(value: str) -> bool:
        try:
            return str(uuid.UUID(value)) == value
        except (ValueError, AttributeError, TypeError):
            return False

    def _merge_client_engine_contexts(
        self,
        context: Any,
        adapters: tuple[Any, ...],
        *,
        client: str,
        engines: Sequence[str],
        explicit_session_ids: Sequence[str] = (),
    ) -> tuple[Any, tuple[Any, ...], None, None, Mapping[str, Any], Mapping[str, Any]]:
        """Add verified native child contexts to one frontend operation.

        The client inventory owns the single frontend snapshot.  Native
        session catalogs are then projected into the existing CleanupPlan and
        retained in ``action_contexts`` so execution dispatches each child to
        the registered Pi/Claude writer without rebuilding frontend state.
        """

        from .client_inventory import (
            _build_native_catalog,
            build_client_engine_contexts,
            build_client_inventory,
        )

        inventory = build_client_inventory(
            adapters,
            client=client,
            engines=engines,
        )
        context = self._merge_aionui_project_items(
            context,
            inventory,
            client=client,
        )
        context = self._merge_cindy_terminal_sessions(
            context,
            inventory,
            client=client,
            explicit_session_ids=explicit_session_ids,
        )
        # Successful scans remain evidence when their last actionable row is gone.
        from .planning import StorageLocation, ScanStatus, storage_id_for_path
        storages = list(context.plan.storages)
        known = {str(item.storage_id) for item in storages}
        for database in inventory.scanned_databases:
            root = database.parent
            key = storage_id_for_path(root)
            if key not in known:
                storages.append(StorageLocation(storage_id=key, label="Frontend database directory",
                    path=root, scan_status=ScanStatus.OK))
                known.add(key)
        for home in inventory.catalog.scanned_native_homes:
            key = storage_id_for_path(home)
            if key not in known:
                storages.append(StorageLocation(storage_id=key, label="Native record store",
                    path=home, scan_status=ScanStatus.OK))
                known.add(key)
        context = replace(context, plan=replace(context.plan, storages=tuple(storages)),
                          frontend_scan_coverage=inventory.scanned_resources)
        # Cindy's anomaly scan does not propose ordinary Codex conversations.
        # Reuse the same client-qualified inventory that records exposes and
        # retain its manual execution map through apply/recovery. Do not build
        # another catalog or substitute the official native store.
        manual_catalog = manual_plan = None
        manual_actions: Mapping[str, Any] = {}
        action_contexts: dict[str, Any] = {}
        if client == "cindy" and (not engines or "codex" in engines):
            # A partial server cascade can leave children whose parents are
            # gone. Their native orphan proof must reach execution; a synthetic
            # manual finding cannot authorize that missing-parent exception.
            from .adapters import NativeIntegrityAdapter

            record_keys = {
                (canonical_path(record.codex_home), record.thread_id)
                for record in inventory.records
                if record.indexed or record.rollouts
            }
            orphan_homes = {
                canonical_path(record.codex_home)
                for record in inventory.records
                if any(
                    (canonical_path(record.codex_home), parent) not in record_keys
                    for parent in getattr(record.summary, "parent_thread_ids", ())
                )
            }
            native_stores = {
                canonical_path(adapter.codex_home): adapter for adapter in adapters
                if getattr(adapter, "codex_home", None) is not None
                and canonical_path(adapter.codex_home) in orphan_homes
            }
            if native_stores:
                native_adapters = tuple(
                    NativeIntegrityAdapter(
                        codex_home=Path(adapter.codex_home),
                        codex_bin_hint=getattr(adapter, "codex_bin_hint", None),
                    )
                    for adapter in native_stores.values()
                )
                native_context = self.service.prepare(native_adapters, platforms=("native",))
                context = self._merge_cleanup_contexts(context, (native_context,))
                for action in native_context.plan.actions:
                    action_contexts[str(action.action_id)] = native_context
            context, _, manual_catalog, manual_plan, manual_actions = (
                self._merge_native_manual_records(
                    context, adapters, catalog=inventory.catalog
                )
            )
        context = self._merge_cindy_orphan_references(context, inventory, client=client)
        engine_contexts = build_client_engine_contexts(
            adapters,
            client=client,
            engines=engines,
            inventory=inventory,
        )
        native_contexts: list[Any] = []
        for engine_context in engine_contexts:
            engine = normalize_engine(engine_context.engine)
            catalog = engine_context.native_catalog
            if engine not in {"pi", "claude"}:
                continue
            # A proven catalog can expose unavailable candidates as well as
            # writable ones. Exact readonly selections must report blocked,
            # rather than disappear into an empty operation.
            if catalog is None:
                continue
            target_action_ids = {
                str(action_id)
                for target in engine_context.targets
                for action_id in target.action_ids
                if str(action_id)
            }

            def catalog_builder(
                *,
                _engine: str = engine,
                _catalog: Any = catalog,
                _adapters: tuple[Any, ...] = adapters,
            ) -> Any:
                fresh = _build_native_catalog(_adapters, client, _engine)
                if fresh is not None:
                    return fresh
                # The catalog was already proven by the inventory builder. A
                # fallback keeps injected/native test adapters compatible; the
                # real Cindy adapter always reaches the fresh builder above.
                return _catalog

            native_context = self.service.prepare_session_catalog(
                engine,
                catalog,
                catalog_builder=catalog_builder,
                target_root=None,
                active_adapters=adapters,
            )
            # Merge the actual constrained candidate plan, not a full native
            # catalog whose rows could regain another profile's writer.
            native_plan = replace(native_context.plan, actions=tuple(
                action if str(action.action_id) in target_action_ids else replace(
                    action, available=False, unavailable_reason=action.unavailable_reason
                    or "client_capability_limit: Client inventory target has no verified native writer")
                for action in native_context.plan.actions))
            native_context = replace(native_context, plan=native_plan, actions=self.service.typed_actions(native_plan))
            native_actions = tuple(
                action
                for action in getattr(native_context, "actions", ())
                if str(getattr(action, "action_id", "")) in target_action_ids
            )
            native_contexts.append(native_context)
            for action in native_actions:
                action_contexts[str(action.action_id)] = native_context

        if not native_contexts:
            return context, adapters, manual_catalog, manual_plan, manual_actions, action_contexts
        return (
            self._merge_cleanup_contexts(context, tuple(native_contexts)),
            adapters,
            manual_catalog,
            manual_plan,
            manual_actions,
            action_contexts,
        )

    @staticmethod
    def _coalesce_manual_candidates(
        candidates: Sequence[Any], manual_actions: Mapping[str, Any]
    ) -> tuple[Any, ...]:
        """Freeze each selected cascade once, retaining its full reference closure."""
        from .manual_delete import build_manual_delete_closure

        selected = {
            str(action.action_id): manual_actions[str(action.action_id)]
            for action in candidates if str(action.action_id) in manual_actions
        }

        # A selected action can only be covered by a parent in the same
        # physical Codex store whose cascade explicitly names that child's
        # thread.  Index that relation by the storage-qualified descendant
        # key so a large flat selection does not compare every pair.  The
        # path identity is deliberately scoped to this planning phase: it
        # avoids a global security-identity cache while still reusing the
        # proof for each action below.
        home_keys: dict[str, str] = {}
        storage_by_action: dict[str, str] = {}
        affected_by_action: dict[str, set[str]] = {}

        def storage_key(action_id: str, action: Any) -> str:
            raw_home = os.fspath(action.codex_home)
            if raw_home not in home_keys:
                home_keys[raw_home] = canonical_path(action.codex_home)
            storage_by_action[action_id] = home_keys[raw_home]
            return home_keys[raw_home]

        for action_id, action in selected.items():
            storage_key(action_id, action)
            affected_by_action[action_id] = set(action.affected_thread_ids)

        parents_by_descendant: dict[tuple[str, str], list[tuple[str, set[str]]]] = {}
        for parent_id, parent in selected.items():
            key = storage_by_action[parent_id]
            parent_affected = affected_by_action[parent_id]
            for descendant_id in parent.descendants:
                parents_by_descendant.setdefault((key, descendant_id), []).append(
                    (parent_id, parent_affected)
                )

        covered: set[str] = set()
        for action_id, child in selected.items():
            key = (storage_by_action[action_id], child.thread_id)
            child_affected = affected_by_action[action_id]
            if any(
                parent_id != action_id
                and child_affected.issubset(parent_affected)
                for parent_id, parent_affected in parents_by_descendant.get(key, ())
            ):
                covered.add(action_id)
        covered_references: set[str] = set()
        retained_references: set[str] = set()
        for action_id, manual in selected.items():
            destination = covered_references if action_id in covered else retained_references
            destination.update(str(action.action_id) for action in
                               build_manual_delete_closure(manual).frontend_actions)
        omitted = covered | (covered_references - retained_references)
        return tuple(action for action in candidates if str(action.action_id) not in omitted)

    def _merge_cindy_orphan_references(self, context: Any, inventory: Any, *, client: str) -> Any:
        """Plan exact references only after a complete native absence proof."""
        if client != "cindy" or inventory.errors:
            return context
        from .planning import ActionImpact, ActionKind, CandidateAction, RiskLevel, TargetRef, storage_id_for_path

        actions = list(context.plan.actions)
        existing = {
            (a.target.storage_id, a.target.thread_id, canonical_path(path))
            for a in actions if a.kind == ActionKind.REMOVE_FRONTEND_REFERENCE
            for path in a.impact.frontend_database_paths
        }
        for record in inventory.records:
            if (record.indexed or record.legacy_indexed or record.artifact_present or record.rollouts
                    or record.descendant_thread_ids or record.cascade_unknown):
                continue
            grouped: dict[str, list[Any]] = {}
            for session in record.frontend_sessions:
                if session.platform != "cindy" or session.database is None:
                    continue
                grouped.setdefault(canonical_path(session.database), []).append(session)
            storage_id = storage_id_for_path(record.codex_home)
            for database, sessions in grouped.items():
                if (storage_id, record.thread_id, database) in existing:
                    continue
                evidence = tuple(s.details.get("frontend_reference") for s in sessions)
                owners = {str(s.owner_process_root) for s in sessions if s.owner_process_root}
                if (len(owners) != 1 or any(s.owner_client != "cindy" or not s.owner_process_root for s in sessions)
                        or any(not isinstance(e, Mapping) or e.get("exact") is not True
                               or e.get("platform") != "cindy"
                               or canonical_path(str(e.get("database") or "")) != database
                               or e.get("expected", {}).get("agent_kind") != "codex"
                               or e.get("expected", {}).get("native_session_id") != record.thread_id
                               for e in evidence)):
                    continue
                owner = next(iter(owners))
                binding = {"store": canonical_path(record.codex_home), "thread": record.thread_id,
                           "database": database, "owner": owner, "references": self._metadata(evidence)}
                fingerprint = hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                impact = ActionImpact(
                    affected_thread_ids=(record.thread_id,), frontend_reference_count=len(evidence),
                    frontend_residual_count=len(evidence), frontend_references_preserved=False,
                    frontend_database_paths=(database,), frontend_reference_evidence=evidence,
                    owner_client="cindy", owner_process_root=owner, resource_path=database,
                    external_engine="codex", external_action_payload={"cwd": record.summary.cwd},
                )
                actions.append(CandidateAction(
                    action_id="orphan-reference:" + fingerprint, kind=ActionKind.REMOVE_FRONTEND_REFERENCE,
                    target=TargetRef(storage_id, record.thread_id), risk=RiskLevel.REVIEW,
                    available=True, unavailable_reason=None, impact=impact,
                    snapshot_fingerprint=fingerprint, requires_explicit_selection=True,
                    resource_kind="frontend_reference",
                ))
        plan = replace(context.plan, actions=tuple(actions))
        return replace(context, plan=plan, actions=self.service.typed_actions(plan))

    def _merge_cindy_terminal_sessions(
        self,
        context: Any,
        inventory: Any,
        *,
        client: str,
        explicit_session_ids: Sequence[str] = (),
    ) -> Any:
        """Project terminal or explicitly selected Cindy rows into cleanup batches."""

        if client != "cindy":
            return context
        from .frontend_session_cleanup import build_cindy_session_delete_evidence
        from .planning import (
            ActionImpact,
            ActionKind,
            CandidateAction,
            RiskLevel,
            ScanStatus,
            StorageLocation,
            TargetRef,
            storage_id_for_path,
        )

        grouped: dict[tuple[str, str], list[Any]] = {}
        databases: dict[tuple[str, str], tuple[Path, Path]] = {}
        for session in getattr(inventory, "frontend_sessions", ()):
            if str(getattr(session, "platform", "")).casefold() != "cindy":
                continue
            if normalize_engine(getattr(session, "backend", "")) not in {"codex", "pi", "claude"}:
                continue
            status = str(getattr(session, "status", "") or "").casefold()
            details = getattr(session, "details", {})
            explicitly_selected = session.platform_session_id in explicit_session_ids
            if (status != "deleted" and not (explicitly_selected and status in {"active", "archived"})) or not isinstance(
                details, Mapping
            ):
                continue
            if str(details.get("reference_kind") or "current") != "current":
                continue
            database = getattr(session, "database", None)
            reference = details.get("frontend_reference")
            if not isinstance(database, Path) or not isinstance(reference, Mapping):
                continue
            if not bool(reference.get("exact")):
                raise OperationCoordinatorError(
                    "Cindy terminal session evidence is not exact"
                )
            raw_owner_process_root = getattr(
                session,
                "owner_process_root",
                None,
            )
            owner_client = str(
                getattr(session, "owner_client", "") or ""
            ).strip().casefold()
            if owner_client != "cindy":
                raise OperationCoordinatorError(
                    "Cindy terminal session evidence is missing explicit "
                    "owner_client=cindy"
                )
            if raw_owner_process_root is None or not str(raw_owner_process_root).strip():
                raise OperationCoordinatorError(
                    "Cindy terminal session evidence is missing explicit "
                    "owner_process_root"
                )
            database_path = database.expanduser().absolute()
            owner_process_root = Path(
                str(raw_owner_process_root)
            ).expanduser().absolute()
            key = (
                os.path.normcase(os.path.abspath(str(database_path))),
                os.path.normcase(os.path.abspath(str(owner_process_root))),
            )
            databases[key] = (database_path, owner_process_root)
            grouped.setdefault(key, []).append(session)

        if not grouped:
            return context
        existing_ids = {
            str(action.action_id)
            for action in getattr(context.plan, "actions", ())
        }
        actions: list[Any] = []
        storages = list(getattr(context.plan, "storages", ()))
        storage_ids = {str(storage.storage_id) for storage in storages}
        for key in sorted(grouped):
            database, owner_process_root = databases[key]
            seeds = []
            for session in grouped[key]:
                reference = session.details["frontend_reference"]
                seeds.append({
                    "database": str(database),
                    "session_id": session.platform_session_id,
                    "expected_status": session.status,
                    "explicitly_selected": session.platform_session_id in explicit_session_ids,
                    "session_schema_fingerprint": reference.get(
                        "session_schema_fingerprint"
                    ),
                    "session_row_fingerprint": reference.get(
                        "session_row_fingerprint"
                    ),
                })
            try:
                evidence_items = build_cindy_session_delete_evidence(seeds)
            except Exception as exc:
                raise OperationCoordinatorError(
                    "Could not freeze exact Cindy terminal-session evidence: "
                    + (str(exc) or repr(exc))
                ) from exc
            evidence_by_id = {
                item.session_id: item.to_dict() for item in evidence_items
            }
            storage_path = database.parent
            storage_id = storage_id_for_path(storage_path)
            if storage_id not in storage_ids:
                storages.append(
                    StorageLocation(
                        storage_id=storage_id,
                        label="Cindy database directory",
                        path=storage_path,
                        scan_status=ScanStatus.OK,
                    )
                )
                storage_ids.add(storage_id)
            for session in grouped[key]:
                session_id = str(session.platform_session_id)
                evidence = evidence_by_id.get(session_id)
                if evidence is None:
                    raise OperationCoordinatorError(
                        "Cindy terminal-session evidence is incomplete"
                    )
                digest = hashlib.sha256(
                    (
                        os.path.normcase(str(database))
                        + "\0"
                        + os.path.normcase(str(owner_process_root))
                        + "\0"
                        + session_id
                        + "\0"
                        + str(evidence["session_row_fingerprint"])
                    ).encode("utf-8")
                ).hexdigest()[:32]
                action_id = "delete_frontend_session:" + digest
                if action_id in existing_ids:
                    continue
                details = getattr(session, "details", {})
                working_dir = details.get("working_dir")
                payload: dict[str, Any] = {
                    "frontend_session_id": session_id,
                    "status": session.status,
                    "owner_client": owner_client,
                    "owner_process_root": str(owner_process_root),
                }
                if isinstance(working_dir, str) and working_dir.strip():
                    payload["project_path"] = working_dir.strip()
                impact = ActionImpact(
                    frontend_session_database_paths=(str(database),),
                    frontend_session_evidence=(evidence,),
                    owner_client=owner_client,
                    owner_process_root=str(owner_process_root),
                    resource_path=str(database),
                    external_engine=str(session.backend or "codex"),
                    external_storage_root=str(storage_path),
                    external_action_payload=payload,
                )
                snapshot = ":".join(
                    (
                        str(evidence["session_row_fingerprint"]),
                        str(evidence["schema_bundle_fingerprint"]),
                        str(evidence["message_id_fingerprint"]),
                    )
                )
                actions.append(
                    CandidateAction(
                        action_id=action_id,
                        kind=ActionKind.DELETE_FRONTEND_SESSION,
                        target=TargetRef(storage_id, session_id),
                        risk=RiskLevel.HIGH,
                        available=True,
                        unavailable_reason=None,
                        impact=impact,
                        snapshot_fingerprint=snapshot,
                        requires_explicit_selection=True,
                        resource_kind="frontend_session",
                    )
                )
                existing_ids.add(action_id)
        if not actions:
            return context
        merged_plan = replace(
            context.plan,
            actions=tuple((*getattr(context.plan, "actions", ()), *actions)),
            storages=tuple(storages),
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                self._metadata(merged_plan.to_dict()),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        merged_plan = replace(
            merged_plan,
            plan_fingerprint="merged:v1:" + fingerprint,
        )
        return replace(
            context,
            plan=merged_plan,
            actions=self.service.typed_actions(merged_plan),
        )

    def _merge_aionui_project_items(
        self,
        context: Any,
        inventory: Any,
        *,
        client: str,
    ) -> Any:
        """Project exact AionUI orphan rows into normal cleanup batches."""

        if client != "aionui":
            return context
        from .planning import (
            ActionImpact,
            ActionKind,
            CandidateAction,
            RiskLevel,
            ScanStatus,
            StorageLocation,
            TargetRef,
            storage_id_for_path,
        )

        existing_ids = {
            str(action.action_id)
            for action in getattr(context.plan, "actions", ())
        }
        actions: list[Any] = []
        storages = list(getattr(context.plan, "storages", ()))
        storage_ids = {str(storage.storage_id) for storage in storages}
        for target in getattr(inventory, "targets", ()):
            classification = getattr(target.classification, "value", target.classification)
            if str(classification) != "orphan_project":
                continue
            evidence = tuple(
                dict(item)
                for item in getattr(target, "project_row_evidence", ())
                if isinstance(item, Mapping)
            )
            if not evidence:
                continue
            record_key = getattr(target, "record_key", None)
            store = getattr(record_key, "store", None)
            database = getattr(store, "path", None)
            project_id = getattr(record_key, "record_id", None)
            if not isinstance(database, Path) or not isinstance(project_id, str):
                continue
            database = database.expanduser().absolute()
            storage_path = database.parent
            storage_id = storage_id_for_path(storage_path)
            if storage_id not in storage_ids:
                storages.append(
                    StorageLocation(
                        storage_id=storage_id,
                        label="AionUI database directory",
                        path=storage_path,
                        scan_status=ScanStatus.OK,
                    )
                )
                storage_ids.add(storage_id)
            action_id = next(
                (
                    value
                    for value in getattr(target, "action_ids", ())
                    if str(value).startswith("delete_project_item:")
                ),
                None,
            )
            if not isinstance(action_id, str) or action_id in existing_ids:
                continue
            project = getattr(target, "project_key", None)
            payload: dict[str, Any] = {
                "project_id": project_id,
            }
            if project is not None:
                if getattr(project, "kind", "") == "path":
                    payload["project_path"] = project.value
                if getattr(project, "display_name", None):
                    payload["project_label"] = project.display_name
            impact = ActionImpact(
                frontend_project_database_paths=(str(database),),
                frontend_project_evidence=evidence,
                resource_path=str(database),
                external_storage_root=str(storage_path),
                external_action_payload=payload,
            )
            snapshot = ":".join(
                (
                    str(evidence[0].get("schema_fingerprint") or ""),
                    str(evidence[0].get("row_fingerprint") or ""),
                )
            )
            actions.append(
                CandidateAction(
                    action_id=action_id,
                    kind=ActionKind.DELETE_PROJECT_ITEM,
                    target=TargetRef(storage_id, project_id),
                    risk=RiskLevel.REVIEW,
                    available=bool(target.capability.frontend_project_delete),
                    unavailable_reason=(
                        None
                        if target.capability.frontend_project_delete
                        else "Exact AionUI project-row writer is unavailable"
                    ),
                    impact=impact,
                    snapshot_fingerprint=snapshot,
                    requires_explicit_selection=True,
                    resource_kind="project_item",
                )
            )
            existing_ids.add(action_id)
        if not actions:
            return context
        merged_plan = replace(
            context.plan,
            actions=tuple((*getattr(context.plan, "actions", ()), *actions)),
            storages=tuple(storages),
        )
        import hashlib
        import json

        fingerprint = hashlib.sha256(
            json.dumps(
                self._metadata(merged_plan.to_dict()),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        merged_plan = replace(
            merged_plan,
            plan_fingerprint="merged:v1:" + fingerprint,
        )
        return replace(
            context,
            plan=merged_plan,
            actions=self.service.typed_actions(merged_plan),
        )

    def _merge_cleanup_contexts(self, base: Any, extras: Sequence[Any]) -> Any:
        """Merge metadata/actions while retaining one top-level context."""

        if not extras:
            return base
        plans = [getattr(base, "plan")]
        plans.extend(getattr(item, "plan") for item in extras)

        def unique(values: Iterable[Any], key: Any) -> tuple[Any, ...]:
            result: list[Any] = []
            seen: set[Any] = set()
            for value in values:
                identity = key(value)
                if identity in seen:
                    continue
                seen.add(identity)
                result.append(value)
            return tuple(result)

        merged_plan = replace(
            plans[0],
            storages=unique(
                (storage for plan in plans for storage in plan.storages),
                lambda value: str(value.storage_id),
            ),
            conversations=unique(
                (item for plan in plans for item in plan.conversations),
                lambda value: (
                    str(value.target.storage_id),
                    str(value.target.thread_id),
                ),
            ),
            observations=unique(
                (item for plan in plans for item in plan.observations),
                lambda value: str(value.observation_id),
            ),
            actions=unique(
                (item for plan in plans for item in plan.actions),
                lambda value: str(value.action_id),
            ),
            planned_actions=unique(
                (item for plan in plans for item in plan.planned_actions),
                lambda value: str(value.action_id),
            ),
            errors=tuple(dict.fromkeys(
                str(error) for plan in plans for error in plan.errors
            )),
        )
        # CleanupPlan is a frozen value object, so calculate a deterministic
        # composite fingerprint after de-duplication rather than mutating any
        # writer-owned catalog.
        import hashlib
        import json

        fingerprint_payload = OperationCoordinator._metadata(merged_plan.to_dict())
        merged_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        merged_plan = replace(
            merged_plan,
            plan_fingerprint="merged:v1:" + merged_fingerprint,
        )

        snapshots = [getattr(base, "snapshot")]
        snapshots.extend(getattr(item, "snapshot") for item in extras)
        snapshot = snapshots[0]
        snapshot_payload = OperationCoordinator._metadata({
            "platforms": [
                platform for item in snapshots for platform in item.platforms
            ],
            "storages": [
                item.to_dict() for snapshot_item in snapshots
                for item in snapshot_item.storages
            ],
            "records": [
                item.to_dict() for snapshot_item in snapshots
                for item in snapshot_item.records
            ],
            "evidence": [
                item.to_dict() for snapshot_item in snapshots
                for item in snapshot_item.evidence
            ],
        })
        snapshot_id = "snapshot:v1:" + hashlib.sha256(
            json.dumps(
                snapshot_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        merged_snapshot = replace(
            snapshot,
            snapshot_id=snapshot_id,
            platforms=tuple(dict.fromkeys(
                platform for item in snapshots for platform in item.platforms
            )),
            storages=unique(
                (item for snapshot_item in snapshots for item in snapshot_item.storages),
                lambda value: str(value.storage_id),
            ),
            records=unique(
                (item for snapshot_item in snapshots for item in snapshot_item.records),
                lambda value: (str(value.storage_id), str(value.record_id)),
            ),
            evidence=unique(
                (item for snapshot_item in snapshots for item in snapshot_item.evidence),
                lambda value: str(value.evidence_id),
            ),
            scan_complete=all(bool(item.scan_complete) for item in snapshots),
            blocker_codes=tuple(dict.fromkeys(
                code for item in snapshots for code in item.blocker_codes
            )),
        )
        return replace(
            base,
            snapshot=merged_snapshot,
            plan=merged_plan,
            # ``typed_actions`` is the public service hook used by every
            # existing context builder.
            actions=self.service.typed_actions(merged_plan),
        )

    @staticmethod
    def _native_catalog_is_healthy(catalog: Any, manual_plan: Any) -> bool:
        """Whether the manual snapshot proves a normal native-only path."""

        if getattr(catalog, "errors", ()):
            return False
        if getattr(catalog, "unmapped_frontend_sessions", ()):
            return False
        if getattr(manual_plan, "errors", ()):
            return False
        for action in getattr(manual_plan, "actions", ()):
            if not bool(getattr(action, "available", False)):
                return False
            # These approvals identify an integrity anomaly (duplicate or
            # mismatched artifacts). Keep those actions on the established
            # anomaly scanner path rather than silently treating them as
            # ordinary healthy roots.
            if getattr(action, "integrity_approvals", ()):
                return False
        return True

    def _native_manual_context(
        self,
        adapters: tuple[Any, ...],
        catalog: Any,
        manual_plan: Any,
    ) -> tuple[Any, tuple[Any, ...], Any, Any, Mapping[str, Any]]:
        """Build a native context from one catalog snapshot.

        This path deliberately has no anomaly scan. It is used only when the
        catalog proves that all selected native roots are complete, available,
        and free of integrity blockers. Suspicious catalogs stay on the
        existing scanner/merge path so anomaly actions are not dropped.
        """

        from .cleanup_service import CleanupContext, StoreSnapshot
        from .cleaner import ScanReport
        from .core_types import RecordKind, RecordRef, StorageKind, StorageRef
        from .planning import (
            CleanupPlan,
            ConversationCatalogEntry,
            ScanStatus,
            StorageLocation,
            TargetRef,
            storage_id_for_path,
        )

        records = tuple(getattr(catalog, "records", ()))
        storage_hints: dict[str, Path | None] = {}
        storage_paths: dict[str, Path] = {}
        for adapter in adapters:
            home = getattr(adapter, "codex_home", None)
            if home is not None:
                home = Path(home).expanduser().absolute()
                storage_paths[storage_id_for_path(home)] = home
        conversations: list[ConversationCatalogEntry] = []
        snapshot_records: list[RecordRef] = []
        for record in records:
            home = Path(record.codex_home).expanduser().absolute()
            storage_id = storage_id_for_path(home)
            storage_paths.setdefault(storage_id, home)
            hints = tuple(getattr(record, "codex_bin_hints", ()))
            if storage_id not in storage_hints:
                storage_hints[storage_id] = (
                    Path(hints[0]) if hints and hints[0] is not None else None
                )
            target = TargetRef(storage_id, str(record.thread_id))
            conversations.append(
                ConversationCatalogEntry(
                    target=target,
                    summary=record.summary,
                )
            )
            snapshot_records.append(
                RecordRef(
                    storage_id=storage_id,
                    kind=RecordKind.CONVERSATION,
                    record_id=str(record.thread_id),
                )
            )

        storages = tuple(
            StorageLocation(
                storage_id=storage_id,
                label="Codex data directory",
                path=path,
                codex_bin_hint=storage_hints.get(storage_id),
                scan_status=ScanStatus.OK,
            )
            for storage_id, path in sorted(storage_paths.items())
        )
        base_plan = CleanupPlan(
            storages=storages,
            conversations=tuple(
                sorted(
                    conversations,
                    key=lambda item: (
                        item.target.storage_id,
                        item.target.thread_id,
                    ),
                )
            ),
            errors=(),
        )
        report = ScanReport()
        snapshot = StoreSnapshot(
            snapshot_id=self._native_snapshot_id(catalog),
            captured_at=datetime.now(timezone.utc).isoformat(),
            platforms=("native",),
            storages=tuple(
                StorageRef(
                    storage_id=storage_id,
                    kind=StorageKind.CODEX_HOME,
                    path=path,
                    owner="codex",
                )
                for storage_id, path in sorted(storage_paths.items())
            ),
            records=tuple(
                sorted(
                    snapshot_records,
                    key=lambda item: (item.storage_id, item.record_id),
                )
            ),
            evidence=(),
            scan_complete=True,
            blocker_codes=(),
            report=report,
            active_adapters=adapters,
        )
        base_context = CleanupContext(
            snapshot=snapshot,
            plan=base_plan,
            actions=(),
        )
        return self._merge_native_manual_records(
            base_context,
            adapters,
            catalog=catalog,
            manual_plan=manual_plan,
        )

    @staticmethod
    def _native_snapshot_id(catalog: Any) -> str:
        import hashlib
        import json

        payload = (
            catalog.to_dict()
            if callable(getattr(catalog, "to_dict", None))
            else str(catalog)
        )
        encoded = json.dumps(
            OperationCoordinator._metadata(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "snapshot:v1:" + hashlib.sha256(encoded).hexdigest()

    def _merge_native_manual_records(
        self,
        context: Any,
        adapters: tuple[Any, ...],
        *,
        catalog: Any | None = None,
        manual_plan: Any | None = None,
    ) -> tuple[Any, tuple[Any, ...], Any, Any, Mapping[str, Any]]:
        """Add healthy native roots from one manual catalog snapshot.

        The anomaly planner remains authoritative for every action it already
        produced. Manual roots only fill the normal-record gap and are kept in
        a side map so execution can use the existing batch-safe
        ``execute_manual_delete`` path without rebuilding a catalog per root.
        """

        from .inventory import build_session_catalog
        from .manual_delete import (
            build_manual_delete_closure,
            build_manual_delete_plan,
        )
        from .planning import (
            ActionImpact,
            ActionKind,
            CandidateAction,
            RiskLevel,
            ScanStatus,
            StorageLocation,
            storage_id_for_path,
        )

        if catalog is None:
            catalog = build_session_catalog(adapters)
        if manual_plan is None:
            manual_plan = build_manual_delete_plan(catalog)
        existing_actions = tuple(getattr(context.plan, "actions", ()))
        def kind_value(action: Any) -> str:
            raw = getattr(action, "kind", "")
            return str(getattr(raw, "value", raw))

        existing_keys = {
            (
                str(action.target.storage_id),
                str(action.target.thread_id),
                kind_value(action),
            )
            for action in existing_actions
        }
        existing_frontend_keys = {
            (
                str(action.target.storage_id),
                str(action.target.thread_id),
                tuple(
                    str(path)
                    for path in getattr(
                        getattr(action, "impact", None),
                        "frontend_database_paths",
                        (),
                    )
                ),
            )
            for action in existing_actions
            if kind_value(action) == ActionKind.REMOVE_FRONTEND_REFERENCE.value
        }
        existing_native_roots = {
            (str(action.target.storage_id), str(action.target.thread_id))
            for action in existing_actions
            if kind_value(action) == ActionKind.DELETE_CONVERSATION.value
        }
        manual_candidates: list[Any] = []
        manual_actions: dict[str, Any] = {}
        storage_locations = list(getattr(context.plan, "storages", ()))
        storage_ids = {str(item.storage_id) for item in storage_locations}
        if not catalog.errors and not context.plan.errors:
            # Preserve successful empty-store coverage for terminal checks.
            for adapter in adapters:
                home = Path(adapter.codex_home)
                storage_id = storage_id_for_path(home)
                if home.is_dir() and storage_id not in storage_ids:
                    storage_locations.append(StorageLocation(
                        storage_id=storage_id, label="Codex data directory",
                        path=home, codex_bin_hint=getattr(adapter, "codex_bin_hint", None),
                        scan_status=ScanStatus.OK,
                    ))
                    storage_ids.add(storage_id)
        for manual in getattr(manual_plan, "actions", ()):
            storage_id = storage_id_for_path(manual.codex_home)
            if storage_id not in storage_ids:
                storage_locations.append(
                    StorageLocation(
                        storage_id=storage_id,
                        label="Codex data directory",
                        path=Path(manual.codex_home),
                        codex_bin_hint=manual.codex_bin_hint,
                        scan_status=ScanStatus.OK,
                    )
                )
                storage_ids.add(storage_id)
            if not bool(getattr(manual, "available", False)):
                continue
            root_key = (storage_id, str(manual.thread_id))
            action_key = (
                storage_id,
                str(manual.thread_id),
                ActionKind.DELETE_CONVERSATION.value,
            )
            # Preserve anomaly actions exactly; a manual root is only a
            # normal-record supplement when no native delete action exists.
            if root_key in existing_native_roots or action_key in existing_keys:
                continue
            candidate = self._manual_candidate(manual, storage_id)
            manual_candidates.append(candidate)
            manual_actions[str(candidate.action_id)] = manual
            closure = build_manual_delete_closure(manual)
            for frontend_action in closure.frontend_actions:
                frontend_action = replace(
                    frontend_action,
                    impact=replace(
                        frontend_action.impact,
                        external_action_payload=candidate.impact.external_action_payload,
                    ),
                )
                frontend_key = (
                    str(frontend_action.target.storage_id),
                    str(frontend_action.target.thread_id),
                    tuple(
                        str(path)
                        for path in getattr(
                            getattr(frontend_action, "impact", None),
                            "frontend_database_paths",
                            (),
                        )
                    ),
                )
                if frontend_key in existing_frontend_keys:
                    continue
                manual_candidates.append(frontend_action)
                existing_frontend_keys.add(frontend_key)

        # Paired frontend actions need the same project evidence as their
        # native root when selection is by project rather than record ID.
        from .planning import ConversationCatalogEntry, TargetRef

        catalog_summaries = {
            (storage_id_for_path(record.codex_home), str(record.thread_id)): record.summary
            for record in getattr(catalog, "records", ())
        }
        conversations = [
            replace(entry, summary=catalog_summaries.get(
                (str(entry.target.storage_id), str(entry.target.thread_id)), entry.summary))
            for entry in getattr(context.plan, "conversations", ())
        ]
        conversation_keys = {(str(entry.target.storage_id), str(entry.target.thread_id))
                             for entry in conversations}
        for key, summary in catalog_summaries.items():
            if key not in conversation_keys:
                conversations.append(ConversationCatalogEntry(target=TargetRef(*key), summary=summary))
        merged_actions = tuple((*existing_actions, *manual_candidates))
        # ``ManualDeletePlan.errors`` may contain failures from several
        # physical homes. A failure that names one catalog/source must not
        # become a global operation blocker for an otherwise complete store.
        # Keep only unassigned structural errors here; home-scoped failures
        # remain represented by the unavailable actions for that home (and by
        # the existing CleanupService plan when it can attribute them).
        manual_global_errors = self._manual_global_errors(catalog, manual_plan)
        merged_errors = tuple(
            dict.fromkeys(
                (
                    *getattr(context.plan, "errors", ()),
                    *manual_global_errors,
                )
            )
        )
        merged_plan = replace(
            context.plan,
            actions=merged_actions,
            conversations=tuple(conversations),
            storages=tuple(storage_locations),
            errors=merged_errors,
        )
        merged_context = replace(
            context,
            plan=merged_plan,
            actions=self.service.typed_actions(merged_plan),
        )
        return (
            merged_context,
            adapters,
            catalog,
            manual_plan,
            manual_actions,
        )

    @staticmethod
    def _manual_global_errors(catalog: Any, manual_plan: Any) -> tuple[str, ...]:
        """Return only manual-plan errors with no provable store owner.

        Inventory failures carry ``codex_home`` and are intentionally scoped
        to their own catalog. ``build_manual_delete_plan`` renders those
        failures into strings for its compatibility API, so correlate the
        rendered message with the structured catalog failure before deciding
        that an error is operation-wide. Unmatched structural errors remain
        global and fail closed.
        """

        failures = tuple(getattr(catalog, "errors", ()) or ())
        scoped_messages = {
            str(getattr(failure, "message", "")).strip()
            for failure in failures
            if str(getattr(failure, "message", "")).strip()
        }
        scoped_homes = {
            os.path.normcase(os.path.abspath(str(home)))
            for home in (
                getattr(failure, "codex_home", None)
                for failure in failures
            )
            if home is not None
        }
        global_errors: list[str] = []
        for raw_error in tuple(getattr(manual_plan, "errors", ()) or ()):
            error = str(raw_error)
            # ``build_manual_delete_plan`` reserves this prefix for a
            # structured InventoryFailure. Plain errors are catalog-shape
            # failures and remain operation-wide even if their wording
            # happens to overlap a source message.
            if not error.startswith("Blocking inventory failure:"):
                global_errors.append(error)
                continue
            if any(message in error for message in scoped_messages):
                continue
            # Some catalog implementations include only a home in the
            # rendered failure text. Treat that as scoped as well.
            normalized_error = error.casefold()
            if any(
                home and home.casefold() in normalized_error
                for home in scoped_homes
            ):
                continue
            global_errors.append(error)
        return tuple(dict.fromkeys(global_errors))

    @staticmethod
    def _manual_candidate(manual: Any, storage_id: str) -> Any:
        from .planning import ActionImpact, ActionKind, CandidateAction, RiskLevel, TargetRef

        expected = manual.expected_scope
        root = getattr(manual, "root", None)
        summary = getattr(root, "summary", None)
        payload: dict[str, Any] = {}
        approval_payload = getattr(summary, "approval_payload", None)
        if callable(approval_payload):
            raw = approval_payload()
            if isinstance(raw, Mapping):
                payload.update(dict(raw))
        project_label = getattr(summary, "project_label", None)
        if isinstance(project_label, str) and project_label.strip():
            payload["project_label"] = project_label.strip()
        impact = ActionImpact(
            indexed_thread_ids=tuple(expected.indexed_thread_ids),
            index_record_count=len(tuple(expected.indexed_thread_ids)),
            rollout_paths=tuple(expected.rollout_paths),
            rollout_file_count=len(tuple(expected.rollout_paths)),
            descendant_thread_ids=tuple(manual.descendants),
            affected_thread_ids=tuple(manual.affected_thread_ids),
            rollout_state_fingerprints=(
                None
                if expected.rollout_state_fingerprints is None
                else tuple(expected.rollout_state_fingerprints)
            ),
            conversation_metadata_fingerprints=(
                None
                if expected.conversation_metadata_fingerprints is None
                else tuple(expected.conversation_metadata_fingerprints)
            ),
            external_engine="codex",
            external_storage_root=str(manual.codex_home),
            external_action_payload=payload,
        )
        return CandidateAction(
            action_id=str(manual.action_id),
            kind=ActionKind.DELETE_CONVERSATION,
            target=TargetRef(storage_id, str(manual.thread_id)),
            risk=RiskLevel.HIGH,
            available=bool(manual.available),
            unavailable_reason=manual.unavailable_reason,
            impact=impact,
            snapshot_fingerprint=str(manual.snapshot_fingerprint),
            requires_explicit_selection=True,
            resource_kind="conversation",
        )

    @staticmethod
    def _adapter_matches(adapter: Any, client: str) -> bool:
        from .client_contracts import describe_adapter
        name = describe_adapter(adapter).client
        if client == "native":
            return name in {"native", "codex-native", "codex-desktop"}
        return name == client

    @staticmethod
    def _with_current_guard_sources(adapters: Sequence[Any], client: str, *, codex_home: Path | None = None,
                                    orca_roots: Sequence[str | Path] = ()) -> tuple[Any, ...]:
        from .operation_guard_sources import current_guard_sources
        return current_guard_sources(adapters, client=client, codex_home=codex_home, orca_roots=orca_roots)

    @staticmethod
    def _default_adapters(
        client: str,
        *,
        codex_home: Path | None = None,
        orca_roots: Sequence[str | Path] = (),
        herdr_roots: Sequence[str | Path] = (),
        workbuddy_roots: Sequence[str | Path] = (),
    ) -> Sequence[Any]:
        from .adapter_factory import create_default_adapters, discover_orca_guards

        args = OperationCoordinator._default_catalog_args(client, codex_home=codex_home, orca_roots=orca_roots,
                                                          herdr_roots=herdr_roots, workbuddy_roots=workbuddy_roots)
        # Discover known local frontend protections independently of the
        # requested candidate client. The caller filters catalog candidates.
        args.platform = ["all"]
        if client in {"pi", "claude"}:
            return discover_orca_guards(args)
        return create_default_adapters(args)

    @staticmethod
    def _default_catalog_args(
        client: str,
        *,
        codex_home: Path | None = None,
        orca_roots: Sequence[str | Path] = (),
        herdr_roots: Sequence[str | Path] = (),
        workbuddy_roots: Sequence[str | Path] = (),
    ) -> Any:
        return SimpleNamespace(
            client=client,
            orca_root=list(orca_roots),
            herdr_root=list(herdr_roots),
            workbuddy_root=list(workbuddy_roots),
            platform=[client],
            appdata=None,
            codex_home=(Path(codex_home).expanduser() if codex_home else None),
            codex_bin=None,
            aionui_db=None,
            aionui_codex_home=None,
            cindy_root=None,
            cindy_db=None,
            cindy_codex_home=None,
            pi_agent_dir=None,
            pi_session_dir=None,
            claude_config_dir=None,
        )

    def _select_candidates(
        self,
        context: Any,
        scope: Mapping[str, Any],
    ) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        if scope.get("client") == "workbuddy":
            from .workbuddy_store import select_candidates
            return select_candidates(context, scope, self._blocker)
        if getattr(context, "client_inventory", None) is not None:
            inventory = context.client_inventory
            try:
                selected = inventory.select(project_selectors=tuple(scope.get("projects", ())),
                    all_projects=bool(scope.get("all_projects")), record_ids=tuple(scope.get("record_ids", ())),
                    engines=tuple(scope.get("engines", ())))
            except ValueError as exc:
                return (), [self._blocker("client_capability_limit", str(exc), scope="selection")]
            blockers = [self._blocker("client_capability_limit",
                "This metadata client has no registered mutation or complete verification contract",
                scope=f"record:{target.record_id or target.frontend_binding_keys}") for target in selected.targets]
            blockers.extend(self._blocker("inventory_error", error.message) for error in inventory.errors)
            return (), blockers or [self._blocker("client_capability_limit", "This metadata client is inventory-only")]
        def kind_value(action: Any) -> str:
            kind = getattr(action, "kind", "")
            return str(getattr(kind, "value", kind))

        actions = tuple(
            action for action in getattr(context.plan, "actions", ())
            if kind_value(action) != "keep"
        )
        blockers: list[dict[str, Any]] = []
        errors = tuple(getattr(context.plan, "errors", ()))
        if errors:
            blockers.append(
                self._blocker("scan_incomplete", "; ".join(map(str, errors)))
            )
        requested_engines = set(scope.get("engines", ()))
        if requested_engines:
            actions = tuple(
                action for action in actions
                if self._action_engine(context, action) in requested_engines
            )
        if scope.get("record_ids"):
            wanted = set(scope["record_ids"])

            # Build the storage-local selector index once.  Record selection
            # is a read-only operation, so retaining action positions here is
            # enough to preserve the original action order (and also keeps
            # duplicate action objects behaving as before).  Native project
            # IDs deliberately use their exact-only index; every other
            # action participates in the same prefix namespace as its native
            # thread ID and optional frontend aliases.
            from bisect import bisect_left

            project_ids: dict[str, list[int]] = {}
            identifier_actions: dict[str, list[int]] = {}
            for index, action in enumerate(actions):
                thread_id = str(action.target.thread_id)
                if kind_value(action) in {"delete_native_project", "delete_schedule_run"}:
                    project_ids.setdefault(thread_id, []).append(index)
                    continue
                identifiers = {thread_id}
                payload = getattr(
                    getattr(action, "impact", None),
                    "external_action_payload",
                    {},
                )
                if isinstance(payload, Mapping):
                    frontend_id = payload.get("frontend_session_id")
                    if isinstance(frontend_id, str) and frontend_id:
                        identifiers.add(frontend_id)
                        identifiers.add(f"{scope['client']}:{frontend_id}")
                for identifier in identifiers:
                    identifier_actions.setdefault(identifier, []).append(index)

            sorted_identifiers = sorted(identifier_actions)

            def prefix_end(prefix: str) -> str | None:
                """Return the exclusive lexicographic bound for one prefix."""

                for index in range(len(prefix) - 1, -1, -1):
                    codepoint = ord(prefix[index])
                    if codepoint < 0x10FFFF:
                        return prefix[:index] + chr(codepoint + 1)
                return None

            selected_indexes: set[int] = set()
            found: set[str] = set()
            for selector in wanted:
                exact_project = project_ids.get(selector)
                if exact_project:
                    found.add(selector)
                    selected_indexes.update(exact_project)

                start = bisect_left(sorted_identifiers, selector)
                upper = prefix_end(selector)
                end = (
                    len(sorted_identifiers)
                    if upper is None
                    else bisect_left(sorted_identifiers, upper)
                )
                for identifier in sorted_identifiers[start:end]:
                    # The bounds above are exact for Unicode strings; retain
                    # this check as a cheap guard if the identifier format is
                    # extended by a future action family.
                    if not identifier.startswith(selector):
                        continue
                    found.add(selector)
                    selected_indexes.update(identifier_actions[identifier])

            actions = tuple(
                action for index, action in enumerate(actions)
                if index in selected_indexes
            )
            for missing in sorted(wanted - found):
                blockers.append(self._blocker(
                    "record_not_found",
                    f"record selector {missing!r} did not match this client store",
                    scope="selection",
                ))
        elif scope.get("projects"):
            actions, project_blockers = self._select_projects(
                context, actions, tuple(scope["projects"])
            )
            blockers.extend(project_blockers)
        elif scope.get("all_projects"):
            actions, project_blockers = self._select_all_projects(
                context,
                actions,
            )
            blockers.extend(project_blockers)
        unavailable = tuple(
            action for action in actions
            if not bool(getattr(action, "available", False))
            or not action_capability(getattr(action, "kind", "")).implemented
        )
        for action in unavailable:
            blockers.append(self._blocker(
                "action_unavailable",
                str(getattr(action, "unavailable_reason", "unsupported action")),
                scope=f"action:{action.action_id}",
                action_id=str(action.action_id),
            ))
        actions = tuple(action for action in actions if action not in unavailable)
        if not actions and not blockers:
            blockers.append(
                self._blocker("empty_scope", "no records matched the selected scope")
            )
        return actions, blockers

    def _select_projects(
        self,
        context: Any,
        actions: Sequence[Any],
        selectors: Sequence[str],
    ) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        selected: list[Any] = []
        blockers: list[dict[str, Any]] = []
        for selector in selectors:
            matches: list[Any] = []
            identities: set[str] = set()
            for action in actions:
                values = self._project_values(context, action)
                matching = [
                    value for value in values
                    if self._selector_matches(selector, value)
                ]
                if matching:
                    matches.append(action)
                    if str(getattr(action.kind, "value", action.kind)) == "delete_native_project":
                        identities.add(str(action.target.storage_id) + ":" + str(action.target.thread_id))
                        continue
                    identities.update(
                        self._project_identity(value) for value in matching
                    )
            if len(identities) > 1:
                blockers.append(self._blocker(
                    "ambiguous_project",
                    f"project selector {selector!r} matches multiple project paths",
                    scope="selection",
                ))
            elif not matches:
                blockers.append(self._blocker(
                    "project_not_found",
                    f"project selector {selector!r} did not match this client store",
                    scope="selection",
                ))
            else:
                selected.extend(matches)
        # CandidateAction carries mapping-valued impact metadata and is not
        # necessarily hashable. Deduplicate by its immutable action identity,
        # preserving the first snapshot order without a second catalog pass.
        unique: list[Any] = []
        seen: set[str] = set()
        for action in selected:
            action_id = str(getattr(action, "action_id", ""))
            if action_id in seen:
                continue
            seen.add(action_id)
            unique.append(action)
        return tuple(unique), blockers

    def _select_all_projects(
        self,
        context: Any,
        actions: Sequence[Any],
    ) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        """Select only actions with unambiguous project evidence.

        ``--all-projects`` is intentionally narrower than "all actions". A
        native record with no working-directory/project evidence is not
        safely attributable to this selector; callers must name that record
        explicitly by ID. Path evidence is authoritative when both a path and
        a presentation label are present, so a normal ``/repo/project`` plus
        ``project`` pair is not treated as an ambiguity.
        """

        selected: list[Any] = []
        blockers: list[dict[str, Any]] = []
        for action in actions:
            values = self._project_values(context, action)
            if not values:
                continue
            if str(getattr(action.kind, "value", action.kind)) == "delete_native_project":
                selected.append(action)
                continue
            path_values = tuple(
                value
                for value in values
                if "/" in value
                or "\\" in value
                or Path(value).is_absolute()
            )
            authoritative = path_values or values
            identities = {
                self._project_identity(value)
                for value in authoritative
            }
            if len(identities) > 1:
                blockers.append(self._blocker(
                    "ambiguous_project",
                    "all-project scope cannot prove one project for action "
                    f"{action.action_id}",
                    scope=f"action:{action.action_id}",
                    action_id=str(action.action_id),
                ))
                continue
            selected.append(action)

        unique: list[Any] = []
        seen: set[str] = set()
        for action in selected:
            action_id = str(getattr(action, "action_id", ""))
            if action_id in seen:
                continue
            seen.add(action_id)
            unique.append(action)
        return tuple(unique), blockers

    @staticmethod
    def _project_values(context: Any, action: Any) -> tuple[str, ...]:
        values: list[str] = []
        payload = getattr(
            getattr(action, "impact", None), "external_action_payload", None
        )
        if isinstance(payload, Mapping):
            keys = (
                "cwd", "working_directory", "project", "project_path",
                "project_id", "project_paths", "project_label",
            )
            for key in keys:
                raw_value = payload.get(key)
                if isinstance(raw_value, (str, os.PathLike)):
                    if str(raw_value).strip():
                        values.append(str(raw_value).strip())
                elif key == "project_paths" and isinstance(
                    raw_value, (tuple, list, set, frozenset)
                ):
                    values.extend(
                        str(item).strip()
                        for item in raw_value
                        if isinstance(item, (str, os.PathLike))
                        and str(item).strip()
                    )
        for entry in getattr(context.plan, "conversations", ()):
            target = getattr(entry, "target", None)
            if (
                target is None
                or str(getattr(target, "storage_id", ""))
                != str(action.target.storage_id)
                or str(getattr(target, "thread_id", ""))
                != str(action.target.thread_id)
            ):
                continue
            summary = getattr(entry, "summary", None)
            for key in ("cwd", "project_label", "git_origin_url"):
                value = getattr(summary, key, None)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
        wanted = {str(value) for value in getattr(action, "observation_ids", ())}
        for observation in getattr(context.plan, "observations", ()):
            if str(getattr(observation, "observation_id", "")) not in wanted:
                continue
            details = getattr(observation, "details", {})
            if isinstance(details, Mapping):
                keys = (
                    "cwd", "working_directory", "project", "project_path",
                    "project_id", "project_label",
                )
                values.extend(
                    str(details[key]).strip()
                    for key in keys
                    if isinstance(details.get(key), (str, os.PathLike))
                    and str(details[key]).strip()
                )
        values = tuple(dict.fromkeys(values))
        if str(getattr(action.kind, "value", action.kind)) == "delete_native_project":
            return values
        path_values = tuple(
            value for value in values if _looks_like_project_path(value) and "://" not in value
        )
        # A display label is useful for selection, but once a path is present
        # it must not become a second project identity for the same action.
        return path_values or values

    @staticmethod
    def _selector_matches(selector: str, value: str) -> bool:
        return project_selector_matches(selector, value)

    @staticmethod
    def _project_identity(value: str) -> str:
        text = str(value).strip()
        if _looks_like_project_path(text):
            try:
                return canonical_path(text)
            except (OSError, ValueError):
                return text.casefold()
        return text.casefold()

    @staticmethod
    def _action_engine(context: Any, action: Any) -> str:
        external = getattr(
            getattr(action, "impact", None), "external_engine", None
        )
        if external:
            return normalize_engine(external)
        kind = str(
            getattr(
                getattr(action, "kind", None),
                "value",
                getattr(action, "kind", ""),
            )
        )
        if kind in {"delete_pi_session", "delete_claude_session"}:
            return kind.removeprefix("delete_").removesuffix("_session")
        wanted = {str(value) for value in getattr(action, "observation_ids", ())}
        for observation in getattr(context.plan, "observations", ()):
            if str(getattr(observation, "observation_id", "")) in wanted:
                platform = getattr(observation, "platform", "codex")
                return "codex" if platform in {"native", "cindy", "aionui"} else normalize_engine(platform)
        return "codex"

    def _make_plan_document(
        self,
        operation_id: str,
        scope: Mapping[str, Any],
        context: Any,
        candidates: Sequence[Any],
        blockers: Sequence[Mapping[str, Any]],
        *,
        plan_path: Path | None,
        operation_home: Path | None,
        codex_home: Path | None,
        active_adapters: Sequence[Any],
        action_contexts: Mapping[str, Any] | None = None,
        manual_actions: Mapping[str, Any] | None = None,
        manual_catalog: Any = None,
    ) -> dict[str, Any]:
        from .cleanup_service import partition_actions

        batches = partition_actions(candidates)
        chosen_path = self._choose_plan_path(
            operation_id,
            context,
            plan_path,
            active_adapters,
            operation_home=operation_home,
            codex_home=codex_home,
        )
        batch_docs = []
        for index, batch in enumerate(batches):
            child_id = f"{operation_id}-{index + 1}"
            batch_doc: dict[str, Any] = {
                "batch_id": batch.batch_id,
                "child_operation_id": child_id,
                "storage_id": batch.storage_id,
                "mutation_family": batch.mutation_family,
                "resource_key": list(batch.resource_key),
                "action_ids": [str(action.action_id) for action in batch.actions],
                "action_count": len(batch.actions),
            }
            batch_doc.update(self._batch_scope_metadata(context, batch))
            batch_docs.append(batch_doc)
        manual_actions = manual_actions or {}
        action_docs = [
            self._action_document(
                context, action,
                manual_record=getattr(manual_actions.get(action.action_id), "root", None),
                client=str(scope["client"]),
            )
            for action in candidates
        ]
        requested_engines = tuple(scope.get("engines", ()))
        observed_engines = tuple(dict.fromkeys(
            self._action_engine(context, action) for action in candidates
            if self._action_engine(context, action)
        ))
        engines = requested_engines or observed_engines or (
            self._default_engine(str(scope["client"])),
        )
        action_contexts = action_contexts or {}
        capabilities: dict[str, dict[str, Any]] = {}
        bound_engines = {
            normalize_engine(getattr(bound, "session_engine", ""))
            for bound in action_contexts.values()
            if getattr(bound, "session_engine", None)
        }
        for engine in engines:
            normalized_engine = normalize_engine(engine)
            capability = capability_for(str(scope["client"]), normalized_engine)
            if any(
                self._action_engine(context, action) == normalized_engine
                and str(
                    getattr(
                        getattr(action, "kind", None),
                        "value",
                        getattr(action, "kind", ""),
                    )
                ) in {"delete_project_item", "delete_native_project"}
                and bool(getattr(action, "available", False))
                for action in candidates
            ):
                capability = replace(
                    capability,
                    frontend_project_delete=True,
                    reason=(
                        capability.reason
                        or "Exact frontend project-row writer is registered"
                    ),
                )
            if any(
                self._action_engine(context, action) == normalized_engine
                and str(
                    getattr(
                        getattr(action, "kind", None),
                        "value",
                        getattr(action, "kind", ""),
                    )
                ) == "delete_frontend_session"
                and bool(getattr(action, "available", False))
                for action in candidates
            ):
                capability = replace(
                    capability,
                    frontend_session_delete=True,
                    reason=(
                        capability.reason
                        or "Exact frontend session-row writer is registered"
                    ),
                )
            if normalized_engine in bound_engines:
                # A native session context is only put in this set after the
                # client adapter supplied both a catalog and a registered
                # writer.  Reflect that proof in the immutable plan metadata.
                capability = replace(
                    capability,
                    native_delete=True,
                    reason=(
                        capability.reason
                        or "verified native session writer is registered"
                    ),
                )
            from .client_capability_guards import ClientCapabilityLimits
            capability = ClientCapabilityLimits.from_adapters(active_adapters).restrict_summary(capability)
            capabilities[normalized_engine] = capability.to_dict()
        payload: dict[str, Any] = {
            "schema_version": "larj.operation-plan.v1",
            "document_type": "operation_plan",
            "operation_id": operation_id,
            "scope": {
                **dict(scope),
                "projects": list(scope.get("projects", ())),
                "record_ids": list(scope.get("record_ids", ())),
                "engines": list(scope.get("engines", ())),
            },
            "snapshot_id": str(getattr(context.snapshot, "snapshot_id", "")),
            "cleanup_plan_fingerprint": str(
                getattr(context.plan, "plan_fingerprint", "")
            ),
            "plan_path": str(chosen_path),
            "capabilities": capabilities,
            "storages": [
                self._metadata(getattr(storage, "to_dict", lambda: storage)())
                for storage in getattr(context.plan, "storages", ())
            ],
            "actions": action_docs,
            "child_batches": batch_docs,
            "blockers": [dict(blocker) for blocker in blockers],
            "counts": {
                "action_count": len(action_docs),
                "batch_count": len(batch_docs),
                "blocked_count": len(blockers),
            },
            "goal_status": "ready" if candidates and not blockers else "blocked",
        }
        guard_sources = guard_sources_for(active_adapters)
        if guard_sources:
            payload["schema_version"] = PLAN_V2
            payload["guard_sources"] = guard_sources
            errors = required_source_errors(payload, active_adapters)
            if errors and candidates:
                payload["blockers"].append(self._blocker("guard_source_incomplete", "; ".join(errors)))
                payload["counts"]["blocked_count"] = len(payload["blockers"])
                payload["goal_status"] = "blocked"
        from .orca_target_safety import plan_target_evidence
        safety = plan_target_evidence(context, scope, active_adapters, candidates, blockers, manual_catalog)
        if safety:
            payload["schema_version"] = PLAN_V3
            payload["target_safety_evidence"] = safety
            for evidence in safety:
                matched = [action for action in action_docs if action["target"]["thread_id"] == evidence["frozen"]["record_id"]
                           and canonical_path(next(storage["path"] for storage in payload["storages"]
                               if storage["storage_id"] == action["target"]["storage_id"])) == canonical_path(evidence["frozen"]["home"])]
                if len(matched) == 1:
                    evidence["action_id"] = matched[0]["action_id"]
            for evidence in safety:
                for code in evidence["blocker_codes"]:
                    payload["blockers"].append(self._blocker(code, code, scope="target_safety"))
            # No validated API boundary is currently registered. A successful
            # metadata preflight cannot make this a mutation authorization.
            if any(not evidence.get("native_delete") for evidence in safety):
                payload["blockers"].append(self._blocker("orca_api_boundary_not_validated",
                    "Orca native deletion remains closed until this binary/storage/runtime combination is validated"))
            elif candidates:
                for capability in payload["capabilities"].values():
                    capability["native_delete"] = True
            payload["counts"]["blocked_count"] = len(payload["blockers"])
            payload["goal_status"] = "ready" if candidates and not payload["blockers"] else "blocked"
        payload["plan_sha256"] = plan_sha256(payload)
        from .orca_target_safety import validate_document_targets
        validate_document_targets(payload)
        return payload

    def _action_document(
        self, context: Any, action: Any, *, manual_record: Any = None, client: str | None = None,
    ) -> dict[str, Any]:
        raw = getattr(
            action, "to_dict", lambda: {"action_id": str(action.action_id)}
        )()
        result = self._metadata(raw)
        result["binding"] = self._metadata(action_binding(action))
        result["classification"] = self._classification(context, action, manual_record, client)
        return result

    def _batch_scope_metadata(
        self,
        context: Any,
        batch: Any,
    ) -> dict[str, Any]:
        """Return body-free project/engine/location fields for CLI rendering."""

        actions = tuple(getattr(batch, "actions", ()))
        project_values: list[str] = []
        engines: list[str] = []
        for action in actions:
            project_values.extend(self._project_values(context, action))
            engine = self._action_engine(context, action)
            if engine and engine not in engines:
                engines.append(engine)
        project_values = list(dict.fromkeys(project_values))
        metadata: dict[str, Any] = {
            "project": project_values[0] if len(project_values) == 1 else None,
            "project_paths": project_values,
            "engine": engines[0] if len(engines) == 1 else engines,
        }
        try:
            metadata["location"] = str(
                self._storage_path(context, str(batch.storage_id), actions[0])
            ) if actions else None
        except (OSError, TypeError, ValueError, OperationCoordinatorError):
            metadata["location"] = None
        return metadata

    @staticmethod
    def _classification(context: Any, action: Any, manual_record: Any = None, client: str | None = None) -> str:
        from .inventory import ManagedConversation, classify_managed_conversation

        if str(getattr(action.kind, "value", action.kind)) == "delete_schedule_run":
            return "healthy"
        if str(
            getattr(
                getattr(action, "kind", None),
                "value",
                getattr(action, "kind", ""),
            )
        ) in {"delete_project_item", "delete_native_project"}:
            return "orphan_project"
        if str(
            getattr(
                getattr(action, "kind", None),
                "value",
                getattr(action, "kind", ""),
            )
        ) == "delete_frontend_session":
            return "orphan_frontend"
        types: set[str] = set()
        wanted = {str(value) for value in getattr(action, "observation_ids", ())}
        for observation in getattr(context.plan, "observations", ()):
            if str(getattr(observation, "observation_id", "")) in wanted:
                types.add(str(getattr(observation, "finding_type", "")))
        if any("frontend" in value for value in types):
            return "orphan_frontend"
        if any("relation" in value or "spawn_edge" in value for value in types):
            return "broken_relation"
        if any("index" in value for value in types):
            return "stale_index"
        if not wanted and isinstance(manual_record, ManagedConversation):
            return classify_managed_conversation(manual_record, frontend_required=client not in {"native", "codex-desktop"}).value
        if types and types <= {"pi_session", "claude_session"}:
            from .record_identity import classify_record_state
            payload = getattr(getattr(action, "impact", None), "external_action_payload", {}) or {}
            category = payload.get("reference_classification", payload.get("classification", "unreferenced"))
            return classify_record_state(
                native_present=category != "frontend_only",
                frontend_present=category in {"live_current_reference", "live_historical_reference", "deleted_frontend_reference", "frontend_only"},
                frontend_required=client not in {"pi", "claude", "native"},
                corrupt_unreadable=category == "inventory_incomplete",
            ).value
        return "orphan_native"

    @staticmethod
    def _default_engine(client: str) -> str:
        return {"pi": "pi", "claude": "claude", "workbuddy": "workbuddy"}.get(client, "codex")

    @staticmethod
    def _bound_workbuddy_roots(document, requested=()):
        if document.get("scope", {}).get("client") != "workbuddy":
            return tuple(requested)
        roots = tuple(Path(storage["path"]) for storage in document.get("storages", ()))
        if not roots:
            return tuple(requested)
        if requested and {canonical_path(root) for root in requested} != {canonical_path(root) for root in roots}:
            raise OperationCoordinatorError("workbuddy_frozen_store_mismatch")
        return roots

    @staticmethod
    def _choose_plan_path(
        operation_id: str,
        context: Any,
        plan_path: Path | None,
        adapters: Sequence[Any],
        *,
        operation_home: Path | None = None,
        codex_home: Path | None = None,
    ) -> Path:
        if plan_path is not None:
            return Path(plan_path).expanduser().absolute()
        # Top-level plans are user-state artifacts, not records in whichever
        # store happened to sort first. This gives a cross-process caller a
        # deterministic operation-id lookup and keeps plan output independent
        # of the selected project's physical layout. Child journals remain
        # next to their exact storage roots.
        del operation_home, codex_home, context, adapters
        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            state_root = Path(os.environ["LOCALAPPDATA"])
        elif os.environ.get("XDG_STATE_HOME"):
            state_root = Path(os.environ["XDG_STATE_HOME"])
        else:
            state_root = Path.home() / (
                "AppData/Local" if os.name == "nt" else ".local/state"
            )
        return state_root / "local-agent-record-janitor" / "plans" / f"{operation_id}.json"

    def _load_plan(
        self,
        operation_id: str | None,
        plan_path: Path | None,
        authorized_hash: str | None,
        *,
        operation_home: Path | None = None,
        codex_home: Path | None = None,
    ) -> dict[str, Any]:
        if plan_path is None:
            live = self._live.get(str(operation_id or ""))
            if live is not None:
                document = dict(live.document)
            else:
                inferred = _find_operation_plan(
                    operation_id,
                    operation_home=operation_home,
                    codex_home=codex_home,
                )
                if inferred is None:
                    raise OperationCoordinatorError(
                        "operation plan is required (pass plan_path, operation_home, or codex_home)"
                    )
                raw = strict_json_load(inferred)
                if not isinstance(raw, dict):
                    raise OperationCoordinatorError("operation plan is not a JSON object")
                document = raw
        else:
            supplied_path = Path(plan_path).expanduser()
            if supplied_path.is_dir():
                inferred = _find_operation_plan(
                    operation_id,
                    operation_home=supplied_path,
                    codex_home=codex_home,
                )
                if inferred is None:
                    raise OperationCoordinatorError(
                        f"operation plan was not found below {supplied_path}"
                    )
                supplied_path = inferred
            raw = strict_json_load(supplied_path)
            if not isinstance(raw, dict):
                raise OperationCoordinatorError("operation plan is not a JSON object")
            document = raw
        if (
            document.get("schema_version") not in {PLAN_V1, PLAN_V2, PLAN_V3}
            or document.get("document_type") != "operation_plan"
        ):
            raise OperationCoordinatorError("operation plan schema is invalid")
        embedded = str(document.get("plan_sha256") or "")
        if not embedded or plan_sha256(document) != embedded:
            raise OperationCoordinatorError("operation plan integrity validation failed")
        if authorized_hash is not None and str(authorized_hash) != embedded:
            raise OperationCoordinatorError("authorized plan hash does not match")
        if operation_id is not None and str(operation_id) != str(document.get("operation_id")):
            raise OperationCoordinatorError("operation ID does not match plan")
        validate_guard_sources(document)
        from .orca_target_safety import validate_document_targets
        validate_document_targets(document)
        return document

    @staticmethod
    def _validate_apply_scope(
        document: Mapping[str, Any],
        requested: Mapping[str, Any],
    ) -> None:
        stored = document.get("scope")
        if not isinstance(stored, Mapping):
            raise OperationCoordinatorError("operation plan scope is invalid")
        # Apply may be invoked with only an operation ID, so empty/default
        # selectors are deliberately ignored. Every selector that *is*
        # supplied is compared against the immutable plan, not just client;
        # this prevents a valid hash from being replayed against another
        # project, record, or engine by a stale CLI invocation.
        for key in ("client", "projects", "all_projects", "record_ids", "engines"):
            value = requested.get(key)
            if key == "all_projects":
                if value is not True:
                    continue
                stored_value = bool(stored.get(key, False))
                if stored_value is not True:
                    raise OperationCoordinatorError(
                        "requested all-project scope differs from approved plan"
                    )
                continue
            if value in (None, "", (), [], False):
                continue
            if key == "client":
                requested_value = normalize_client(value)
                stored_value = normalize_client(stored.get(key))
            elif key in {"projects", "record_ids", "engines"}:
                requested_value = tuple(dict.fromkeys(str(item) for item in value))
                stored_value = tuple(
                    dict.fromkeys(str(item) for item in (stored.get(key) or ()))
                )
            else:
                requested_value = value
                stored_value = stored.get(key)
            if requested_value != stored_value:
                raise OperationCoordinatorError(
                    f"requested {key} scope differs from approved plan"
                )

    def _bind_fresh_candidates(
        self,
        document: Mapping[str, Any],
        context: Any,
        *,
        skip_child_ids: Iterable[str] = (),
    ) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        frozen = {
            str(item.get("action_id")): item
            for item in document.get("actions", ())
            if isinstance(item, Mapping)
        }
        fresh_actions = tuple(getattr(context.plan, "actions", ()))
        if document.get("scope", {}).get("client") == "workbuddy":
            from .workbuddy_store import freeze_actions
            completed = set(map(str, skip_child_ids))
            pending_ids = {str(action_id) for batch in document.get("child_batches", ())
                           if str(batch.get("child_operation_id")) not in completed for action_id in batch.get("action_ids", ())}
            selected = tuple(action for action in fresh_actions if action.action_id in pending_ids and action.available)
            rebound = {action.action_id: action for action in freeze_actions(selected)} if selected else {}
            fresh_actions = tuple(rebound.get(action.action_id, action) for action in fresh_actions)
        current = {
            str(action.action_id): action
            for action in fresh_actions
        }
        frontend_by_target: dict[tuple[str, str, str], list[Any]] = {}
        for action in current.values():
            kind = str(
                getattr(
                    getattr(action, "kind", None),
                    "value",
                    getattr(action, "kind", ""),
                )
            )
            if kind != "delete_frontend_session":
                continue
            target = getattr(action, "target", None)
            if target is None:
                continue
            target_key = (
                kind,
                str(getattr(target, "storage_id", "")),
                str(getattr(target, "thread_id", "")),
            )
            frontend_by_target.setdefault(target_key, []).append(action)
        completed = {str(child_id) for child_id in skip_child_ids}
        selected: list[Any] = []
        blockers: list[dict[str, Any]] = []
        for batch in document.get("child_batches", ()):
            if not isinstance(batch, Mapping):
                continue
            if str(batch.get("child_operation_id") or "") in completed:
                # A terminal child is already authorized by its durable
                # receipt/event. Its records may intentionally be absent
                # from a fresh catalog, so never try to bind it again.
                continue
            for action_id in batch.get("action_ids", ()):
                key = str(action_id)
                action = current.get(key)
                frozen_action = frozen.get(key)
                rebound = False
                if isinstance(frozen_action, Mapping) and (
                    action is None
                    or self._metadata(action_binding(action))
                    != frozen_action.get("binding")
                ):
                    frozen_target = frozen_action.get("target")
                    if not isinstance(frozen_target, Mapping):
                        frozen_target = frozen_action.get("binding")
                    if not isinstance(frozen_target, Mapping):
                        frozen_target = {}
                    rebound_action = self._rebind_frontend_session_action(
                        frozen_action,
                        frontend_by_target.get(
                            (
                                "delete_frontend_session",
                                str(frozen_target.get("storage_id") or ""),
                                str(frozen_target.get("thread_id") or ""),
                            ),
                            (),
                        ),
                        key,
                    )
                    if rebound_action is not None:
                        action = rebound_action
                        rebound = True
                if action is None or not isinstance(frozen_action, Mapping):
                    blockers.append(self._blocker(
                        "target_state_changed",
                        f"approved action {key} is no longer present",
                        scope=f"action:{key}", action_id=key,
                    ))
                    continue
                if not rebound and self._metadata(action_binding(action)) != frozen_action.get("binding"):
                    blockers.append(self._blocker(
                        "target_state_changed",
                        f"approved action {key} changed after plan",
                        scope=f"action:{key}", action_id=key,
                    ))
                    continue
                if not bool(getattr(action, "available", False)):
                    blockers.append(self._blocker(
                        "action_unavailable",
                        f"approved action {key} is unavailable",
                        scope=f"action:{key}", action_id=key,
                    ))
                    continue
                selected.append(action)
        return tuple(selected), blockers

    @classmethod
    def _rebind_frontend_session_action(
        cls,
        frozen_action: Mapping[str, Any],
        current_actions: Iterable[Any],
        frozen_action_id: str,
    ) -> Any | None:
        """Rebind Cindy session after an authorized SDK reference clear.

        Cindy's generated action ID includes the full session-row
        fingerprint. A preceding remove_frontend_reference child is allowed
        to change only sdk_session_id from its approved value to NULL, which
        necessarily gives the fresh action a different ID and snapshot. Keep
        this exception narrow: the physical target, owner, all stable row
        evidence, and every other action binding field must remain identical.
        """

        if str(frozen_action.get("kind") or "") != "delete_frontend_session":
            return None
        frozen_binding = frozen_action.get("binding")
        if not isinstance(frozen_binding, Mapping):
            return None
        frozen_impact = frozen_binding.get("impact")
        if not isinstance(frozen_impact, Mapping):
            return None
        frozen_target = frozen_action.get("target")
        if not isinstance(frozen_target, Mapping):
            frozen_target = frozen_binding
        frozen_storage = str(
            frozen_target.get("storage_id") or frozen_binding.get("storage_id") or ""
        )
        frozen_thread = str(
            frozen_target.get("thread_id") or frozen_binding.get("thread_id") or ""
        )
        frozen_databases = tuple(
            str(value)
            for value in frozen_impact.get("frontend_session_database_paths", ())
        )
        frozen_owner = str(frozen_impact.get("owner_process_root") or "")
        matches: list[Any] = []
        for candidate in current_actions:
            try:
                current_binding = cls._metadata(action_binding(candidate))
            except Exception:
                continue
            if not isinstance(current_binding, Mapping):
                continue
            if str(current_binding.get("kind") or "") != "delete_frontend_session":
                continue
            if str(current_binding.get("storage_id") or "") != frozen_storage:
                continue
            if str(current_binding.get("thread_id") or "") != frozen_thread:
                continue
            current_impact = current_binding.get("impact")
            if not isinstance(current_impact, Mapping):
                continue
            current_databases = tuple(
                str(value)
                for value in current_impact.get(
                    "frontend_session_database_paths", ()
                )
            )
            if current_databases != frozen_databases:
                continue
            if str(current_impact.get("owner_process_root") or "") != frozen_owner:
                continue
            if cls._frontend_session_transition_allowed(
                frozen_binding,
                current_binding,
            ):
                matches.append(candidate)
        if len(matches) != 1:
            return None
        try:
            frozen_evidence = frozen_impact.get("frontend_session_evidence")
            impact = replace(
                matches[0].impact,
                frontend_session_evidence=tuple(
                    dict(item)
                    for item in frozen_evidence
                    if isinstance(item, Mapping)
                ),
            )
            return replace(
                matches[0],
                action_id=str(frozen_action_id),
                snapshot_fingerprint=str(
                    frozen_binding.get("snapshot_fingerprint") or ""
                ),
                impact=impact,
            )
        except (TypeError, ValueError):
            # Only the repository's immutable CandidateAction is supported;
            # do not mutate an arbitrary adapter object in place.
            return None

    @classmethod
    def _frontend_session_transition_allowed(
        cls,
        frozen_binding: Mapping[str, Any],
        current_binding: Mapping[str, Any],
    ) -> bool:
        """Return whether a fresh Cindy session binding is safe to reuse."""

        for key in (
            "kind",
            "storage_id",
            "thread_id",
            "affected_thread_ids",
            "observation_ids",
            "capability",
        ):
            if cls._metadata(frozen_binding.get(key)) != cls._metadata(
                current_binding.get(key)
            ):
                return False
        frozen_impact = frozen_binding.get("impact")
        current_impact = current_binding.get("impact")
        if not isinstance(frozen_impact, Mapping) or not isinstance(
            current_impact, Mapping
        ):
            return False
        left_impact = dict(frozen_impact)
        right_impact = dict(current_impact)
        frozen_evidence = left_impact.pop("frontend_session_evidence", None)
        current_evidence = right_impact.pop("frontend_session_evidence", None)
        if cls._metadata(left_impact) != cls._metadata(right_impact):
            return False
        if not isinstance(frozen_evidence, Sequence) or isinstance(
            frozen_evidence, (str, bytes, bytearray)
        ) or not isinstance(current_evidence, Sequence) or isinstance(
            current_evidence, (str, bytes, bytearray)
        ):
            return False
        if len(frozen_evidence) != 1 or len(current_evidence) != 1:
            return False
        approved = cls._metadata(frozen_evidence[0])
        observed = cls._metadata(current_evidence[0])
        if not isinstance(approved, Mapping) or not isinstance(observed, Mapping):
            return False
        approved = dict(approved)
        observed = dict(observed)
        sentinel = object()
        approved_sdk = approved.pop("expected_sdk_session_id", sentinel)
        # _metadata intentionally omits None-valued mapping fields. Treat an
        # omitted fresh SDK value as the observed NULL, but require the
        # approved evidence to carry its original non-NULL reference.
        observed_sdk = observed.pop("expected_sdk_session_id", None)
        approved_row = approved.pop("session_row_fingerprint", None)
        observed_row = observed.pop("session_row_fingerprint", None)
        # These optional flags were introduced independently on both branches.
        # Compare their authorization meaning without rewriting frozen evidence.
        for evidence, sdk in ((approved, approved_sdk), (observed, observed_sdk)):
            selected = evidence.pop("explicitly_selected", False)
            legacy = evidence.pop("explicit_unbound_active", False)
            if type(selected) is not bool or type(legacy) is not bool:
                return False
            status = evidence.get("expected_status")
            if legacy and (status != "active" or sdk not in (None, sentinel)):
                return False
            if status != "deleted":
                evidence["explicitly_selected"] = selected or legacy
        if cls._metadata(approved) != cls._metadata(observed):
            return False
        if approved_sdk is sentinel:
            # Serialized metadata omits NULL. Absence permits no SDK transition.
            if observed_sdk is not None or approved_row != observed_row:
                return False
            approved_sdk = None
        allowed_sdk = {None} if approved_sdk is None else {approved_sdk, None}
        if observed_sdk not in allowed_sdk:
            return False
        frozen_snapshot = str(frozen_binding.get("snapshot_fingerprint") or "")
        current_snapshot = str(current_binding.get("snapshot_fingerprint") or "")
        expected_frozen_snapshot = ":".join(
            (
                str(approved_row or ""),
                str(approved.get("schema_bundle_fingerprint") or ""),
                str(approved.get("message_id_fingerprint") or ""),
            )
        )
        expected_current_snapshot = ":".join(
            (
                str(observed_row or ""),
                str(observed.get("schema_bundle_fingerprint") or ""),
                str(observed.get("message_id_fingerprint") or ""),
            )
        )
        return (
            frozen_snapshot == expected_frozen_snapshot
            and current_snapshot == expected_current_snapshot
        )
    @staticmethod
    def _error_details(error: BaseException) -> dict[str, str]:
        """Serialize only type/message metadata for a child failure.

        Exception objects and their arbitrary attributes are deliberately not
        journaled: adapters may attach request bodies, rows, or transcripts to
        them.  The coordinator keeps the original human-readable exception
        text while limiting the durable shape to body-free metadata.
        """

        message = str(error) or repr(error)
        return {
            "type": type(error).__name__,
            "message": message,
        }

    @staticmethod
    def _child_attempted(state: Mapping[str, Any]) -> bool:
        """Return true for any durable indication that mutation was tried."""

        return any(
            bool(state.get(field))
            for field in ("mutation_started", "mutation_attempted", "attempted")
        )

    @classmethod
    def _resumable_blocked_child(cls, state: Mapping[str, Any]) -> bool:
        """Recognize the sole safe apply-resume state.

        A known blocker may be retried only when the child explicitly reached
        ``blocked`` before opening/attempting mutation.  Requiring all four
        fields prevents a forged or partially-written checkpoint from being
        mistaken for a fresh operation.
        """

        return (
            state.get("phase") == "blocked"
            and state.get("goal_status") == "blocked"
            and state.get("current_action_state") == "not_started"
            and not cls._child_attempted(state)
        )

    @classmethod
    def _child_blockers(
        cls,
        value: Mapping[str, Any] | None,
        child_id: str,
    ) -> list[dict[str, Any]]:
        """Extract persisted body-free blockers from a child checkpoint."""

        if not isinstance(value, Mapping):
            return []
        raw_blockers = value.get("blockers")
        result: list[dict[str, Any]] = []
        if isinstance(raw_blockers, Sequence) and not isinstance(
            raw_blockers, (str, bytes, bytearray)
        ):
            for raw in raw_blockers:
                if not isinstance(raw, Mapping):
                    continue
                blocker = cls._metadata(raw)
                if not isinstance(blocker, Mapping):
                    continue
                if not blocker.get("blocker_code"):
                    continue
                item = dict(blocker)
                item.setdefault("scope", f"child:{child_id}")
                result.append(item)
        if result:
            return result
        raw_error = value.get("error")
        if isinstance(raw_error, Mapping):
            message = raw_error.get("message")
        else:
            message = raw_error
        if isinstance(message, str) and message:
            code = (
                "mutation_outcome_unknown"
                if str(value.get("goal_status") or "") == "unknown"
                else "batch_execution_blocked"
            )
            return [cls._blocker(
                code,
                message,
                scope=f"child:{child_id}",
            )]
        return []

    def _inspect_child_states(
        self,
        document: Mapping[str, Any],
    ) -> _ChildStateInspection:
        """Inspect persisted child journals before rebuilding or mutating.

        A child that is already terminal is safe to skip. Any other durable
        mutation marker is a recovery boundary: a new process must not guess
        whether an irreversible request reached the server. Only a fresh
        child, or an existing preflight-only child with no mutation marker,
        may proceed to context construction and execution.
        """

        storage_by_id = {
            str(storage.get("storage_id")): Path(str(storage.get("path")))
            for storage in document.get("storages", ())
            if isinstance(storage, Mapping)
            and storage.get("storage_id")
            and storage.get("path")
        }
        completed: set[str] = set()
        batches: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        modified = False
        mutation_started = False

        for raw in document.get("child_batches", ()):
            if not isinstance(raw, Mapping):
                blockers.append(self._blocker(
                    "recovery_required",
                    "operation child batch is not an object",
                    scope="child-batch",
                ))
                continue
            view = dict(raw)
            child_id = str(raw.get("child_operation_id") or "")
            storage_id = str(raw.get("storage_id") or "")
            storage_path = storage_by_id.get(storage_id)
            if not child_id or storage_path is None:
                view["status"] = "unknown"
                blockers.append(self._blocker(
                    "recovery_required",
                    "operation child batch has no trusted storage binding",
                    scope=f"child:{child_id or '<missing>'}",
                ))
                batches.append(view)
                continue

            store = OperationStore(storage_path, child_id)
            try:
                directory_present = store.directory.exists()
                if not directory_present:
                    view["status"] = "pending"
                    batches.append(view)
                    continue
                if store.lock_exists():
                    view["status"] = "unknown"
                    blockers.append(self._blocker(
                        "recovery_required",
                        "child operation is currently locked by another apply",
                        scope=f"child:{child_id}",
                    ))
                    batches.append(view)
                    continue

                # A compacted terminal receipt is authoritative even though
                # its detailed state/journal has intentionally been removed.
                result = store.read_result()
                state = store.read_state()
                if result is not None:
                    goal = str(result.get("goal_status") or "")
                    modified = modified or bool(result.get("modified"))
                    mutation_started = mutation_started or bool(
                        result.get("mutation_started")
                        or result.get("mutation_attempted")
                        or result.get("attempted")
                    )
                    if goal in {"complete", "completed_with_residuals"}:
                        completed.add(child_id)
                        view["status"] = goal
                        batches.append(view)
                        continue
                    # Older integrations may have persisted a non-terminal
                    # result alongside the checkpoint.  It is resumable only
                    # when the checkpoint proves the same pre-mutation state;
                    # a result's goal alone must never authorize a retry.
                    if (
                        goal == "blocked"
                        and state is not None
                        and self._resumable_blocked_child(state)
                    ):
                        view["status"] = "blocked"
                        persisted = self._child_blockers(state, child_id)
                        if not persisted:
                            persisted = self._child_blockers(result, child_id)
                        if persisted:
                            view["blockers"] = persisted
                        batches.append(view)
                        continue
                    view["status"] = "unknown"
                    persisted = self._child_blockers(result, child_id)
                    if persisted:
                        view["blockers"] = persisted
                        blockers.extend(persisted)
                    blockers.append(self._blocker(
                        "recovery_required",
                        "child operation has a non-terminal persisted result",
                        scope=f"child:{child_id}",
                    ))
                    batches.append(view)
                    continue

                if state is None:
                    # A directory containing any trusted operation artifact
                    # but no state cannot be safely reinitialized in apply.
                    if any(
                        path.exists()
                        for path in (
                            store.plan_path,
                            store.events_path,
                            store.receipt_path,
                        )
                    ):
                        view["status"] = "unknown"
                        blockers.append(self._blocker(
                            "recovery_required",
                            "child operation has durable artifacts but no state",
                            scope=f"child:{child_id}",
                        ))
                    else:
                        view["status"] = "pending"
                    batches.append(view)
                    continue

                if document.get("schema_version") == PLAN_V3:
                    startup_events = [event for event in store.read_events() if event["event"] == "orca_runtime_startup"]
                    if startup_events and not state.get("mutation_started"):
                        mutation_started = True
                        view["status"] = "unknown"
                        blockers.append(self._blocker("recovery_required", "Startup event is ahead of durable state",
                            scope=f"child:{child_id}"))
                        batches.append(view)
                        continue
                goal = str(state.get("goal_status") or "")
                phase = str(state.get("phase") or "")
                started = self._child_attempted(state)
                modified = modified or bool(state.get("modified"))
                mutation_started = mutation_started or started
                persisted_blockers = self._child_blockers(state, child_id)
                if persisted_blockers:
                    view["blockers"] = persisted_blockers
                if phase == "finished" and goal in {
                    "complete",
                    "completed_with_residuals",
                }:
                    events = store.read_events()
                    if (
                        events
                        and events[-1].get("event") == "batch_finished"
                        and str(events[-1].get("goal_status") or "") == goal
                    ):
                        completed.add(child_id)
                        view["status"] = goal
                        batches.append(view)
                        continue
                    view["status"] = "unknown"
                    blockers.extend(persisted_blockers)
                    blockers.append(self._blocker(
                        "recovery_required",
                        "child terminal state lacks a trusted completion event",
                        scope=f"child:{child_id}",
                    ))
                    batches.append(view)
                    continue

                # A known pre-mutation blocker is safe to retry after its
                # external cause is resolved.  This is intentionally stricter
                # than merely checking mutation_started: a guard may have
                # begun or an attempted flag may have been persisted without
                # a mutation marker, and those states still require verify.
                if self._resumable_blocked_child(state):
                    view["status"] = "blocked"
                    batches.append(view)
                    continue

                # The other resumable persisted state is the preflight phase
                # before any irreversible request was marked durable.
                if phase == "preflight" and not started:
                    view["status"] = "pending"
                    batches.append(view)
                    continue

                view["status"] = "unknown"
                blockers.extend(persisted_blockers)
                blockers.append(self._blocker(
                    "recovery_required",
                    "child operation has started or has an untrusted state; "
                    "run verify before attempting another apply",
                    scope=f"child:{child_id}",
                ))
                batches.append(view)
            except Exception as exc:
                view["status"] = "unknown"
                blockers.append(self._blocker(
                    "recovery_required",
                    f"could not trust child operation state: {exc}",
                    scope=f"child:{child_id}",
                ))
                batches.append(view)

        return _ChildStateInspection(
            completed_child_ids=frozenset(completed),
            batches=tuple(batches),
            blockers=tuple(blockers),
            modified=modified,
            mutation_started=mutation_started,
        )

    def _execute_live(
        self,
        live: _LiveOperation,
        *,
        timeout: float,
        app_server_factory: Any,
        binary_resolver: Any,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        entered = False
        try:
            with mutation_roots(frozen_operation_roots(live.document)):
                entered = True
                if live.document.get("schema_version") == PLAN_V3:
                    if app_server_factory is not None or binary_resolver is not None:
                        raise OperationCoordinatorError("Orca frozen runtime does not allow factory/binary overrides")
                    from .orca_authorization import execution_scope
                    with execution_scope(live.document, live.candidates, clients_closed=live.clients_closed_ack):
                        return self._execute_live_locked(live, timeout=timeout,
                            app_server_factory=None, binary_resolver=None, progress_callback=progress_callback)
                return self._execute_live_locked(live, timeout=timeout,
                    app_server_factory=app_server_factory, binary_resolver=binary_resolver, progress_callback=progress_callback)
        except Exception as exc:
            inspection = self._inspect_child_states(live.document)
            prior = live.result or {}
            modified = bool(prior.get("modified")) or inspection.modified
            started = bool(prior.get("mutation_started")) or inspection.mutation_started
            # A lock exit failure can occur after a writer and publication.
            # Preserve those facts; absence of a result after entering the
            # execution scope is never proof of an unchanged outcome.
            unknown = bool(started or modified or inspection.blockers or (entered and not live.result))
            result = self._result_document(
                live.document, live.document.get("scope", {}), goal_status="unknown" if unknown else "blocked",
                blockers=[self._blocker(getattr(exc, "kind", "mutation_root_untrusted"), str(exc))],
                batches=prior.get("batches", inspection.batches),
                modified=modified, mutation_started=started,
            )
            live.result = result
            return result

    def _execute_live_locked(
        self,
        live: _LiveOperation,
        *,
        timeout: float,
        app_server_factory: Any,
        binary_resolver: Any,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        from .cleanup_service import partition_actions
        from .codex_app_server import CodexAppServer
        from .discovery import choose_codex_binary

        self._emit_progress(
            progress_callback,
            "apply",
            "started",
            operation_id=live.operation_id,
            counts={"action_count": len(live.candidates)},
        )
        child_inspection = self._inspect_child_states(live.document)
        if child_inspection.blockers:
            result = self._result_document(
                live.document,
                live.document.get("scope", {}),
                goal_status="unknown",
                blockers=child_inspection.blockers,
                batches=child_inspection.batches,
                modified=child_inspection.modified,
                mutation_started=child_inspection.mutation_started,
            )
            live.result = result
            self._emit_progress(
                progress_callback,
                "apply",
                "completed",
                operation_id=live.operation_id,
                counts={"batch_count": len(child_inspection.batches)},
            )
            return result

        current = self._with_current_guard_sources(live.adapters, live.client,
            codex_home=self._bound_codex_home(live.document, None))
        self._retain_live_guards(live, refresh_guard_sources(retain_guard_sources(
            current, validate_guard_sources(live.document) if live.document.get("schema_version") in {PLAN_V2, PLAN_V3} else ())))
        source_errors = required_source_errors(live.document, live.adapters)
        if source_errors:
            result = self._result_document(live.document, live.document.get("scope", {}), goal_status="blocked",
                blockers=[self._blocker("guard_source_incomplete", "; ".join(source_errors))],
                batches=child_inspection.batches, modified=child_inspection.modified,
                mutation_started=child_inspection.mutation_started)
            live.result = result
            return result

        completed_child_ids = (
            set(child_inspection.completed_child_ids)
            | set(getattr(live, "completed_child_ids", ()))
        )
        batches = partition_actions(live.candidates)
        total_action_count = sum(len(batch.actions) for batch in batches)
        completed_action_count = 0
        completed_action_ids: set[str] = set()
        from .manual_delete import (
            build_manual_delete_closure,
            frontend_actions_after_native_success,
        )

        manual_ids = {
            str(action.action_id) for action in live.candidates
            if str(action.action_id) in getattr(live, "manual_actions", {})
        }
        closures_by_native: dict[str, Any] = {}
        frontend_dependencies: dict[str, str] = {}
        for action_id in manual_ids:
            manual = getattr(live, "manual_actions", {}).get(action_id)
            if manual is None:
                continue
            closure = build_manual_delete_closure(manual)
            closures_by_native[action_id] = closure
            for frontend_action in closure.frontend_actions:
                frontend_dependencies[str(frontend_action.action_id)] = action_id
        released_frontend_ids: set[str] = set()
        batch_results: list[dict[str, Any]] = []
        any_modified = child_inspection.modified
        mutation_started = child_inspection.mutation_started
        stopped_unknown = False
        blocked_batches: list[str] = []
        blocked_batch_blockers: list[dict[str, Any]] = []
        def batch_key(batch: Any) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
            return (
                str(batch.storage_id),
                str(batch.mutation_family),
                tuple(str(value) for value in batch.resource_key),
                tuple(
                    str(getattr(action, "action_id", ""))
                    for action in batch.actions
                ),
            )

        frozen_child_ids = {
            (
                str(raw.get("storage_id") or ""),
                str(raw.get("mutation_family") or ""),
                tuple(str(value) for value in raw.get("resource_key", ())),
                tuple(str(value) for value in raw.get("action_ids", ())),
            ): str(raw.get("child_operation_id") or "")
            for raw in live.document.get("child_batches", ())
            if isinstance(raw, Mapping)
            and str(raw.get("child_operation_id") or "")
        }
        # ``frozen_child_ids`` is built after partitioning so its immutable
        # batch keys can be used to seed progress for already completed
        # recovery children.
        completed_action_count = sum(
            len(batch.actions)
            for batch in batches
            if frozen_child_ids.get(batch_key(batch)) in completed_child_ids
        )
        completed_action_ids.update(
            str(action.action_id)
            for batch in batches
            if frozen_child_ids.get(batch_key(batch)) in completed_child_ids
            for action in batch.actions
        )
        if frozen_child_ids:
            unresolved = [
                self._blocker(
                    "target_state_changed",
                    "fresh batch has no immutable child binding",
                    scope=f"batch:{batch.batch_id}",
                )
                for batch in batches
                if batch_key(batch) not in frozen_child_ids
            ]
            if unresolved:
                result = self._result_document(
                    live.document,
                    live.document.get("scope", {}),
                    goal_status="blocked",
                    blockers=unresolved,
                    batches=child_inspection.batches,
                    modified=child_inspection.modified,
                    mutation_started=child_inspection.mutation_started,
                )
                live.result = result
                self._emit_progress(
                    progress_callback,
                    "apply",
                    "completed",
                    operation_id=live.operation_id,
                    counts={"batch_count": len(child_inspection.batches)},
                )
                return result
        for index, batch in enumerate(batches):
            key = batch_key(batch)
            child_id = frozen_child_ids.get(key) or (
                f"{live.operation_id}-{index + 1}"
            )
            if child_id in completed_child_ids:
                skipped_batch = {
                    "batch_id": batch.batch_id,
                    "child_operation_id": child_id,
                    "storage_id": batch.storage_id,
                    "mutation_family": batch.mutation_family,
                    "status": "complete",
                    "skipped": True,
                    "action_ids": [
                        str(action.action_id) for action in batch.actions
                    ],
                }
                skipped_batch.update(
                    self._batch_scope_metadata(live.context, batch)
                )
                batch_results.append(skipped_batch)
                self._emit_progress(
                    progress_callback,
                    "apply",
                    "batch_skipped",
                    operation_id=live.operation_id,
                    counts={
                        "batch_index": index + 1,
                        "batch_count": len(batches),
                        "action_count": len(batch.actions),
                        "completed_action_count": completed_action_count,
                        "total_action_count": total_action_count,
                    },
                    child_operation_id=child_id,
                    mutation_family=str(batch.mutation_family),
                    storage_id=str(batch.storage_id),
                )
                continue
            self._emit_progress(
                progress_callback,
                "apply",
                "batch_started",
                operation_id=live.operation_id,
                counts={
                    "batch_index": index + 1,
                    "batch_count": len(batches),
                    "action_count": len(batch.actions),
                    "completed_action_count": completed_action_count,
                    "total_action_count": total_action_count,
                },
                child_operation_id=child_id,
                mutation_family=str(batch.mutation_family),
                storage_id=str(batch.storage_id),
            )
            batch_mutation_started = False
            store: OperationStore | None = None
            try:
                storage_path = self._storage_path(
                    live.context, batch.storage_id, batch.actions[0]
                )
                store, state = self._open_batch_store(
                    live, batch, child_id, storage_path, index
                )
                from .mutation_guard import scopes_for_frozen_plan
                batch_scopes = (scopes_for_frozen_plan(store.read_plan()) if live.document.get("schema_version") == PLAN_V3
                                else scopes_for_actions(live.context.plan, batch.actions))
                with mutation_guard(batch_scopes, store=store), store.mutation_lock():
                    def checkpoint(
                        phase: str,
                        action: Any,
                        result: Any | None,
                    ) -> None:
                        nonlocal batch_mutation_started, mutation_started
                        nonlocal completed_action_count
                        raw_status = (
                            getattr(result, "status", "")
                            if result is not None else ""
                        )
                        status = str(
                            getattr(raw_status, "value", raw_status)
                        ).casefold()
                        batch_mutation_started = (
                            batch_mutation_started
                            or phase == "mutation_started"
                        )
                        mutation_started = (
                            mutation_started
                            or batch_mutation_started
                        )
                        current = dict(store.read_state() or state)
                        current.update({
                            "phase": "executing",
                            "mutation_started": bool(current.get("mutation_started")) or phase == "mutation_started",
                            "current_action_ids": [str(action.action_id)],
                            "current_action_state": phase,
                            "modified": bool(current.get("modified")) or status == "deleted",
                        })
                        event_name = {
                            "guard_started": "action_guard_started",
                            "mutation_started": "mutation_started",
                            "verified": "action_verified",
                        }.get(phase)
                        if event_name is None:
                            raise OperationCoordinatorError(
                                f"unsupported action checkpoint: {phase}"
                            )
                        event: dict[str, Any] = {
                            "event": event_name,
                            "action_id": str(action.action_id),
                            "action_state": phase,
                        }
                        if status:
                            event["result_status"] = status
                        store.append_event(event, state_updates=current)
                        action_id = str(action.action_id)
                        if phase == "verified" and action_id not in completed_action_ids:
                            counts = {
                                "completed_action_count": completed_action_count,
                                "total_action_count": total_action_count,
                                "batch_index": index + 1,
                                "batch_count": len(batches),
                            }
                            if status in {"deleted", "cleaned", "repaired"}:
                                completed_action_ids.add(action_id)
                                completed_action_count += 1
                                counts["completed_action_count"] = completed_action_count
                                self._emit_progress(
                                    progress_callback,
                                    "apply",
                                    "action_completed",
                                    operation_id=live.operation_id,
                                    counts=counts,
                                    child_operation_id=child_id,
                                    action_id=action_id,
                                    action_state=phase,
                                    result_status=status,
                                )
                            elif status:
                                self._emit_progress(
                                    progress_callback,
                                    "apply",
                                    "action_checked",
                                    operation_id=live.operation_id,
                                    counts=counts,
                                    child_operation_id=child_id,
                                    action_id=action_id,
                                    action_state=phase,
                                    result_status=status,
                                )

                    def startup_checkpoint(job_name):
                        nonlocal batch_mutation_started, mutation_started
                        from .orca_runtime import runtime_host_identity
                        instance = {"schema_version": "larj.orca-runtime-instance.v1", "job_name": job_name,
                                    **runtime_host_identity()}
                        current = dict(store.read_state() or state)
                        current.update(phase="executing", mutation_started=True,
                            current_action_ids=[str(a.action_id) for a in batch.actions],
                            current_action_state="mutation_started", runtime_instance=instance)
                        store.append_event({"event": "orca_runtime_startup", "mutation_started": True,
                                            "runtime_instance": instance}, state_updates=current)
                        batch_mutation_started = mutation_started = True

                    def startup_boundary_checkpoint(snapshot):
                        store.append_event({"event": "orca_runtime_boundary_observed", "startup_artifacts": snapshot},
                            state_updates={"startup_artifacts": snapshot})

                    if str(batch.mutation_family) == "remove_frontend_reference":
                        required_frontend = {
                            str(action.action_id)
                            for action in batch.actions
                            if str(action.action_id) in frontend_dependencies
                        }
                        unreleased = required_frontend - released_frontend_ids
                        if unreleased:
                            raise OperationCoordinatorError(
                                "paired frontend cleanup is blocked until its "
                                "native deletion is positively verified: "
                                + ", ".join(sorted(unreleased))
                            )
                    if (
                        batch.actions
                        and all(
                            str(action.action_id) in manual_ids
                            for action in batch.actions
                        )
                    ):
                        outcome = self._execute_manual_batch(
                            live,
                            batch.actions,
                            timeout=timeout,
                            app_server_factory=(
                                app_server_factory or CodexAppServer
                            ),
                            binary_resolver=(
                                binary_resolver or choose_codex_binary
                            ),
                            action_state_callback=checkpoint,
                            startup_callback=startup_checkpoint,
                            startup_boundary_callback=startup_boundary_checkpoint,
                        )
                    else:
                        batch_context = self._context_for_batch(live, batch)
                        typed_by_id = {
                            str(action.action_id): action
                            for action in getattr(
                                getattr(batch_context, "plan", None),
                                "actions",
                                (),
                            )
                        }
                        typed = tuple(action if str(getattr(action.kind, "value", action.kind)) in {"delete_workbuddy_session", "remove_workbuddy_ui_reference"}
                                      else typed_by_id.get(str(action.action_id), action) for action in batch.actions)
                        outcome = self.service.execute(
                            batch_context,
                            typed,
                            timeout=timeout,
                            app_server_factory=(
                                app_server_factory or CodexAppServer
                            ),
                            binary_resolver=(
                                binary_resolver or choose_codex_binary
                            ),
                            action_state_callback=checkpoint,
                            session_preflight_verified=True,
                        )
                outcome_doc = self._metadata(outcome)
                statuses = self._outcome_statuses(outcome)
                batch_unknown = "unknown" in statuses
                batch_status = ("unknown" if batch_unknown else
                    "completed_with_residuals" if "partial" in statuses else
                    "blocked" if statuses.intersection({"not_deleted", "failed", "blocked", "unsupported"}) else "complete")
                batch_modified = bool(
                    statuses.intersection({"deleted", "cleaned", "repaired", "partial"})
                    or getattr(outcome, "modified", False)
                )
                if batch_status == "complete":
                    for action in batch.actions:
                        action_id = str(action.action_id)
                        if action_id in completed_action_ids:
                            continue
                        completed_action_ids.add(action_id)
                        completed_action_count += 1
                        self._emit_progress(
                            progress_callback,
                            "apply",
                            "action_completed",
                            operation_id=live.operation_id,
                            counts={
                                "completed_action_count": completed_action_count,
                                "total_action_count": total_action_count,
                                "batch_index": index + 1,
                                "batch_count": len(batches),
                            },
                            child_operation_id=child_id,
                            action_id=action_id,
                            action_state="verified",
                        )
                any_modified = any_modified or batch_modified
                stopped_unknown = stopped_unknown or batch_unknown
                if (
                    batch.actions
                    and all(
                        str(action.action_id) in manual_ids
                        for action in batch.actions
                    )
                ):
                    for native_action in batch.actions:
                        native_id = str(native_action.action_id)
                        closure = closures_by_native.get(native_id)
                        if closure is None:
                            continue
                        status = self._outcome_status_for_action(
                            outcome,
                            native_action,
                        )
                        released_frontend_ids.update(
                            str(action.action_id)
                            for action in frontend_actions_after_native_success(
                                closure,
                                SimpleNamespace(status=status),
                            )
                        )
                batch_result = {
                    "batch_id": batch.batch_id,
                    "child_operation_id": child_id,
                    "storage_id": batch.storage_id,
                    "mutation_family": batch.mutation_family,
                    "status": batch_status,
                    "action_ids": [str(action.action_id) for action in batch.actions],
                    "result": outcome_doc,
                }
                outcome_errors = self._outcome_errors(outcome)
                if outcome_errors:
                    batch_result["blockers"] = [self._blocker(
                        "mutation_outcome_unknown" if batch_unknown else "batch_execution_blocked",
                        message, scope=f"child:{child_id}",
                    ) for message in outcome_errors]
                batch_result.update(self._batch_scope_metadata(live.context, batch))
                batch_results.append(batch_result)
                store.append_event(
                    {
                        "event": "batch_finished",
                        "goal_status": batch_status,
                    },
                    state_updates={
                        "phase": "recovery_required" if batch_unknown else "finished",
                        "goal_status": batch_status,
                        "goal_satisfied": batch_status == "complete",
                        "modified": any_modified,
                        # The child journal records its own irreversible
                        # marker. The operation-level flag may already be
                        # true because an earlier child completed.
                        "mutation_started": batch_mutation_started,
                        "blockers": batch_result.get("blockers", []),
                    },
                )
                self._emit_progress(
                    progress_callback,
                    "apply",
                    "batch_completed",
                    operation_id=live.operation_id,
                    counts={
                        "batch_index": index + 1,
                        "batch_count": len(batches),
                        "action_count": len(batch.actions),
                        "completed_action_count": completed_action_count,
                        "total_action_count": total_action_count,
                    },
                    child_operation_id=child_id,
                    mutation_family=str(batch.mutation_family),
                    storage_id=str(batch.storage_id),
                    batch_status=batch_status,
                )
                if batch_unknown:
                    break
            except Exception as exc:
                # A writer may report an explicit, durably verified rollback
                # after it had opened/started a transaction. That is a known
                # unchanged outcome: retain the mutation_started audit bit,
                # but allow terminal verification and classify this child as
                # blocked/partial. Any unmarked exception remains unknown,
                # even if its message happens to mention a rollback.
                known_rollback = (
                    getattr(exc, "outcome_known_rolled_back", False) is True
                )
                admission_blocked = getattr(exc, "kind", "") in {
                    "store_mutation_outcome_unknown", "mutation_root_locked",
                }
                batch_unknown = (
                    not admission_blocked
                    and
                    not known_rollback
                    and (
                        batch_mutation_started
                        or "unknown" in str(exc).casefold()
                    )
                )
                stopped_unknown = stopped_unknown or batch_unknown
                error_details = self._error_details(exc)
                failure_blocker = self._blocker(
                    getattr(exc, "kind", "batch_execution_blocked")
                    if not batch_unknown
                    else "mutation_outcome_unknown",
                    error_details["message"],
                    scope=f"child:{child_id}",
                )
                failed_batch = {
                    "batch_id": batch.batch_id,
                    "child_operation_id": child_id,
                    "storage_id": batch.storage_id,
                    "mutation_family": batch.mutation_family,
                    "status": "unknown" if batch_unknown else "blocked",
                    "action_ids": [str(action.action_id) for action in batch.actions],
                    "error": error_details["message"],
                    "error_details": error_details,
                    "blockers": [failure_blocker],
                }
                failed_batch.update(self._batch_scope_metadata(live.context, batch))
                if store is not None:
                    try:
                        # Persist both known blockers and ambiguous failures.
                        # Status must not need to replay the journal to recover
                        # the original error, and an unknown child must retain
                        # its recovery boundary rather than being retried.
                        current_state = dict(store.read_state() or state)
                        state_modified = bool(current_state.get("modified"))
                        state_started = bool(
                            current_state.get("mutation_started")
                            or current_state.get("mutation_attempted")
                        )
                        state_attempted = bool(
                            current_state.get("attempted") or batch_mutation_started
                        )
                        safe_pre_mutation_block = (
                            not batch_unknown
                            and not known_rollback
                            and not batch_mutation_started
                            and not state_started
                            and not state_attempted
                        )
                        state_updates = {
                            "phase": "recovery_required" if batch_unknown else "blocked",
                            "goal_status": "unknown" if batch_unknown else "blocked",
                            "goal_satisfied": False,
                            "modified": state_modified,
                            "mutation_started": state_started or batch_mutation_started,
                            "attempted": state_attempted,
                            "error": error_details,
                            "blockers": [failure_blocker],
                        }
                        if safe_pre_mutation_block:
                            # execution.py may checkpoint guard_started before
                            # a writer's process guard rejects the batch. That
                            # marker is not an irreversible-attempt marker;
                            # normalize only this fully proven pre-mutation
                            # known-blocker state so resume remains safe.
                            state_updates["current_action_state"] = "not_started"
                        event = {
                            "event": "batch_finished",
                            "goal_status": "unknown" if batch_unknown else "blocked",
                            "error": error_details,
                            "blockers": [failure_blocker],
                        }
                        if known_rollback:
                            state_updates.update(
                                {
                                    "outcome_known_rolled_back": True,
                                }
                            )
                            event["outcome_known_rolled_back"] = True
                        store.append_event(
                            event,
                            state_updates=state_updates,
                        )
                    except Exception as journal_error:
                        # If the failure itself cannot be journaled, the
                        # operation is no longer trustworthy and must return
                        # to the conservative unknown boundary.
                        batch_unknown = True
                        failed_batch["status"] = "unknown"
                        failed_batch["error"] = (
                            f"{failed_batch['error']}; could not persist "
                            f"failure evidence: {journal_error}"
                        )
                        failed_batch["error_details"] = {
                            **error_details,
                            "persistence_error": str(journal_error) or repr(journal_error),
                        }
                batch_results.append(failed_batch)
                self._emit_progress(
                    progress_callback,
                    "apply",
                    "batch_completed",
                    operation_id=live.operation_id,
                    counts={
                        "batch_index": index + 1,
                        "batch_count": len(batches),
                        "action_count": len(batch.actions),
                    },
                    child_operation_id=child_id,
                    mutation_family=str(batch.mutation_family),
                    storage_id=str(batch.storage_id),
                    batch_status="unknown" if batch_unknown else "blocked",
                )
                if batch_unknown:
                    break
                blocked_batches.append(child_id)
                blocked_batch_blockers.append(failure_blocker)
        # An ambiguous irreversible result is a recovery boundary. Do not
        # trigger even the terminal catalog pass here: explicit ``verify`` is
        # the only recovery operation allowed to rescan after ``unknown``.
        # This also prevents a terminal callback or adapter side effect from
        # being mistaken for progress on a stopped operation.
        if stopped_unknown:
            terminal, terminal_error = None, None
            self._emit_progress(
                progress_callback,
                "verify",
                "skipped",
                operation_id=live.operation_id,
                reason="recovery_required",
            )
        else:
            self._emit_progress(
                progress_callback,
                "verify",
                "started",
                operation_id=live.operation_id,
            )
            self._emit_progress(
                progress_callback,
                "inventory",
                "started",
                operation_id=live.operation_id,
            )
            terminal, terminal_error = self._terminal_context(live)
            self._emit_progress(
                progress_callback,
                "inventory",
                "completed",
                operation_id=live.operation_id,
                counts=self._progress_counts(terminal) if terminal is not None else {},
            )
        live.terminal_context = terminal if terminal_error is None else None
        residuals: list[str] = []
        if terminal_error is None and terminal is not None:
            try:
                residuals = self._residual_action_ids(live.document, terminal)
            except Exception as exc:
                terminal_error = str(exc) or repr(exc)
        paired_frontend_ids = {
            str(action.action_id)
            for action in live.candidates
            if str(action.action_id) in frontend_dependencies
        }
        paired_frontend_evidence = tuple(
            evidence
            for action in live.candidates
            if str(action.action_id) in paired_frontend_ids
            for evidence in getattr(
                getattr(action, "impact", None),
                "frontend_reference_evidence",
                (),
            )
        )
        if terminal_error is None and paired_frontend_evidence:
            try:
                from .frontend_reference_cleanup import (
                    verify_frontend_reference_evidence,
                )

                remaining_frontend = verify_frontend_reference_evidence(
                    paired_frontend_evidence
                )
            except Exception as exc:
                terminal_error = (
                    "Could not verify paired frontend references: "
                    + (str(exc) or repr(exc))
                )
            else:
                if remaining_frontend:
                    residuals.extend(sorted(paired_frontend_ids))
                    residuals = list(dict.fromkeys(residuals))
        blockers: list[dict[str, Any]] = []
        if terminal_error is not None:
            blockers.append(self._blocker("terminal_scan_incomplete", terminal_error))
        if blocked_batch_blockers:
            blockers.extend(blocked_batch_blockers)
        else:
            for child_id in blocked_batches:
                blockers.append(self._blocker(
                    "batch_execution_blocked",
                    "a child batch failed before an irreversible request",
                    scope=f"child:{child_id}",
                ))
        if residuals:
            blockers.append(self._blocker("residual_records", "approved records remain"))
        # Preserve the exact child failure on the immediate result as well as
        # in its durable checkpoint.  This makes a failed apply actionable
        # without opening the child journal and also covers unknown failures.
        for batch_result in batch_results:
            for blocker in batch_result.get("blockers", ()):
                if isinstance(blocker, Mapping) and dict(blocker) not in blockers:
                    blockers.append(dict(blocker))
        if stopped_unknown:
            goal = "unknown"
            blockers.append(self._blocker(
                "mutation_outcome_unknown",
                "an irreversible result was not confirmed",
            ))
        elif terminal_error is not None:
            goal = "unknown"
        elif blocked_batches or any(batch.get("status") == "blocked" for batch in batch_results):
            goal = "blocked"
        elif residuals:
            goal = "completed_with_residuals"
        else:
            goal = "complete"
        result = self._result_document(
            live.document,
            live.document.get("scope", {}),
            goal_status=goal,
            blockers=blockers,
            batches=batch_results,
            residuals=residuals,
            modified=any_modified,
            mutation_started=mutation_started,
        )
        live.result = result
        if stopped_unknown:
            # The operation is waiting for explicit recovery verification;
            # no terminal scan ran in this path.
            pass
        elif terminal_error is not None:
            self._emit_progress(
                progress_callback,
                "verify",
                "failed",
                operation_id=live.operation_id,
                counts={"residual_count": len(residuals)},
            )
        elif terminal is not None:
            self._emit_progress(
                progress_callback,
                "verify",
                "completed",
                operation_id=live.operation_id,
                counts={"residual_count": len(residuals)},
            )
        else:
            self._emit_progress(
                progress_callback,
                "verify",
                "failed",
                operation_id=live.operation_id,
                counts={"residual_count": len(residuals)},
                reason="terminal_context_unavailable",
            )
        self._emit_progress(
            progress_callback,
            "apply",
            "completed",
            operation_id=live.operation_id,
            counts={
                "batch_count": len(batch_results),
                "action_count": sum(
                    len(item.get("action_ids", ())) for item in batch_results
                ),
                "completed_action_count": completed_action_count,
                "total_action_count": total_action_count,
                "residual_count": len(residuals),
            },
            goal_status=goal,
        )
        return result

    @staticmethod
    def _context_for_batch(live: _LiveOperation, batch: Any) -> Any:
        """Route one storage/family batch to its proven native context."""

        contexts: list[Any] = []
        seen: set[int] = set()
        for action in getattr(batch, "actions", ()):
            context = getattr(live, "action_contexts", {}).get(
                str(getattr(action, "action_id", ""))
            )
            if context is None or id(context) in seen:
                continue
            seen.add(id(context))
            contexts.append(context)
        if not contexts:
            return live.context
        if len(contexts) != 1:
            raise OperationCoordinatorError(
                "one child batch maps to more than one engine context"
            )
        return contexts[0]

    def _execute_manual_batch(
        self,
        live: _LiveOperation,
        actions: Sequence[Any],
        *,
        timeout: float,
        app_server_factory: Any,
        binary_resolver: Any,
        action_state_callback: Any,
        startup_callback: Any = None,
        startup_boundary_callback: Any = None,
    ) -> Any:
        """Execute one native store batch from the frozen manual snapshot."""

        from .frontend_reference_cleanup import (
            guard_frontend_reference_closure,
        )
        from .manual_delete import (
            build_manual_delete_closure,
            execute_manual_delete,
        )

        if live.manual_plan is None or live.manual_catalog is None:
            raise OperationCoordinatorError(
                "manual native batch is missing its immutable catalog"
            )
        selected = live.manual_plan.with_selected_actions(
            str(action.action_id) for action in actions
        )
        evidence_by_database: dict[
            str,
            dict[str, list[Mapping[str, Any]]],
        ] = {}
        for action in actions:
            manual = getattr(live, "manual_actions", {}).get(
                str(action.action_id)
            )
            if manual is None:
                continue
            closure = build_manual_delete_closure(manual)
            if not closure.eligible:
                continue
            for frontend_action in closure.frontend_actions:
                if not bool(getattr(frontend_action, "available", False)):
                    raise OperationCoordinatorError(
                        "paired frontend reference action is unavailable"
                    )
                databases = tuple(
                    str(path)
                    for path in getattr(
                        getattr(frontend_action, "impact", None),
                        "frontend_database_paths",
                        (),
                    )
                )
                if len(databases) != 1 or not databases[0]:
                    raise OperationCoordinatorError(
                        "paired frontend reference has no physical database"
                    )
                by_native = evidence_by_database.setdefault(databases[0], {})
                for evidence in getattr(
                    getattr(frontend_action, "impact", None),
                    "frontend_reference_evidence", (),
                ):
                    expected = evidence.get("expected", {})
                    identity_key = (
                        "session_id" if evidence.get("platform") == "aionui"
                        else "native_session_id"
                    )
                    native_id = expected.get(identity_key)
                    if native_id not in manual.affected_thread_ids:
                        raise OperationCoordinatorError(
                            "frontend reference is outside the frozen native cascade"
                        )
                    by_native.setdefault(str(native_id), []).append(evidence)
        # Guard all frozen frontend rows for each physical database as one
        # collection before execute_manual_delete can emit mutation_started.
        for by_native in evidence_by_database.values():
            guard_frontend_reference_closure(by_native)
        kwargs = {}
        from contextlib import nullcontext
        runtime = nullcontext()
        if live.document.get("schema_version") == PLAN_V3:
            from .orca_target_safety import evidence_for_actions, recheck_orca_target, _startup_manifest, recheck_startup_snapshot
            from .orca_runtime import IsolatedCodexRuntime
            from .adapters.orca import OrcaAdapter
            evidence = evidence_for_actions(live.document, actions)
            startup_snapshot = None
            def boundary(phase):
                nonlocal startup_snapshot
                errors = [code for value in evidence for code in recheck_orca_target(value,
                    OrcaAdapter(profile_root=value["frozen"]["profile_root"]), phase=phase)]
                if errors:
                    raise OperationCoordinatorError("; ".join(sorted(set(errors))))
                home = Path(evidence[0]["frozen"]["home"])
                if phase == "post_start":
                    startup_snapshot = _startup_manifest(home, after_start=True)
                    startup_boundary_callback(startup_snapshot)
                elif phase == "readonly_recovery" and startup_snapshot is not None:
                    recheck_startup_snapshot(home, startup_snapshot)
            boundary("before_start")
            binaries = {value["frozen"]["binary"]["path"] for value in evidence}
            if len(binaries) != 1:
                raise OperationCoordinatorError("Orca batch has conflicting pinned binaries")
            runtime = IsolatedCodexRuntime(Path(next(iter(binaries))))
            def factory(*, codex_home, codex_binary, timeout):
                if Path(codex_binary) != runtime.binary or canonical_path(codex_home) != canonical_path(evidence[0]["frozen"]["home"]):
                    raise OperationCoordinatorError("Orca runtime factory scope differs from approval")
                return runtime.server(codex_home=codex_home, timeout=timeout, job_ready_callback=startup_callback)
            app_server_factory = factory
            binary_resolver = lambda _hint: runtime.binary
            kwargs = {"defer_verification_until_close": True,
                "post_start_validator": lambda: boundary("post_start"),
                "post_close_validator": lambda: boundary("readonly_recovery"),
                "targeted_guard": lambda _action: boundary("readonly_recovery")}
        with runtime:
            return execute_manual_delete(
            selected,
            catalog_builder=lambda: live.manual_catalog,
            approved_plan_fingerprint=str(selected.plan_fingerprint or ""),
            clients_closed=True,
            timeout=timeout,
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
            preflight_verified=True,
            targeted_guards_only=True,
            action_state_callback=action_state_callback,
            **kwargs,
            )

    def _open_batch_store(
        self,
        live: _LiveOperation,
        batch: Any,
        child_id: str,
        storage_path: Path,
        index: int,
    ) -> tuple[OperationStore, dict[str, Any]]:
        frozen_actions = {
            str(item.get("action_id")): item
            for item in live.document.get("actions", ())
            if isinstance(item, Mapping)
        }
        child_actions = []
        for action in batch.actions:
            frozen_action = frozen_actions.get(str(action.action_id))
            if not isinstance(frozen_action, Mapping):
                raise OperationCoordinatorError(
                    "batch action is absent from the immutable top-level plan"
                )
            # The child plan is a projection of the immutable top-level
            # authorization. Fresh execution objects may carry a rebinding
            # snapshot, but must never change accept_plan's child hash during
            # cross-process recovery.
            child_actions.append(self._metadata(frozen_action))
        child_plan: dict[str, Any] = {
            "schema_version": "larj.child-operation-plan.v1",
            "operation_id": child_id,
            "target": {
                "codex_home": str(storage_path),
                "storage_id": batch.storage_id,
            },
            "parent_operation_id": live.operation_id,
            "mutation_family": batch.mutation_family,
            "actions": child_actions,
        }
        if live.document.get("schema_version") == PLAN_V3:
            from .orca_target_safety import evidence_for_actions
            child_plan["schema_version"] = "larj.child-operation-plan.v2"
            child_plan["startup_boundary"] = {"schema_version": "larj.orca-startup-boundary.v1",
                "coordination_scope": "root_wide", "home": str(storage_path),
                "target_safety_evidence": evidence_for_actions(live.document, batch.actions)}
        child_plan["plan_sha256"] = plan_sha256(child_plan)
        store = OperationStore(storage_path, child_id)
        new_store = not store.directory.exists()
        store.accept_plan(child_plan)
        # A second process may be resuming an existing preflight child. The
        # child plan is immutable, and its state/journal are durable evidence;
        # never rewrite either file while opening the batch. Recovery states
        # have already been rejected by ``_inspect_child_states``.
        if not new_store:
            state = store.read_state()
            if state is None:
                raise OperationCoordinatorError(
                    "child operation has durable artifacts but no state"
                )
            return store, state
        state = {
            "schema_version": "larj.agent-state.v1",
            "operation_id": child_id,
            "plan_sha256": child_plan["plan_sha256"],
            "phase": "preflight",
            "goal_status": "unknown",
            "goal_satisfied": False,
            "modified": False,
            "mutation_started": False,
            "current_action_ids": [],
            "current_action_state": "not_started",
            "next_event_sequence": 1,
        }
        store.write_state(state)
        store.append_event({
            "event": "plan_accepted",
            "parent_operation_id": live.operation_id,
            "batch_index": index,
        })
        return store, state

    @staticmethod
    def _storage_path(context: Any, storage_id: str, action: Any) -> Path:
        for storage in getattr(context.plan, "storages", ()):
            if str(getattr(storage, "storage_id", "")) == str(storage_id):
                return Path(storage.path)
        root = getattr(
            getattr(action, "impact", None), "external_storage_root", None
        )
        if root:
            return Path(root)
        raise OperationCoordinatorError(
            f"storage {storage_id!r} is not present in the approved plan"
        )

    def _terminal_context(
        self,
        live: _LiveOperation,
        *, readonly_recovery: bool = False,
    ) -> tuple[Any | None, str | None]:
        try:
            self._retain_live_guards(live, self._with_current_guard_sources(live.adapters, live.client,
                codex_home=self._bound_codex_home(live.document, None)), readonly_recovery=readonly_recovery)
            self._retain_live_guards(live, refresh_guard_sources(retain_guard_sources(
                live.adapters, validate_guard_sources(live.document))), readonly_recovery=readonly_recovery)
            for adapter in live.adapters:
                invalidate = getattr(adapter, "invalidate_frontend_snapshot", None)
                if callable(invalidate):
                    invalidate()
            if getattr(live.context, "session_engine", None):
                builder = live.context.session_catalog_builder
                return (
                    self.service.prepare_session_catalog(
                        live.context.session_engine,
                        builder(),
                        catalog_builder=builder,
                        target_root=None,
                        active_adapters=live.adapters,
                    ),
                    None,
                )
            context, _adapters, _catalog, _manual_plan, _manual_actions, _action_contexts = (
                self._build_context(
                    "native" if live.document.get("schema_version") == PLAN_V3 else live.client,
                    live.adapters,
                    inventory_adapters=self._orca_recovery_sources(live.document, live.adapters),
                    explicit_frontend_ids=tuple(live.document.get("scope", {}).get("record_ids", ())),
                    engines=tuple(
                        live.document.get("scope", {}).get("engines", ())
                    ),
                    explicit_session_ids=tuple(live.document.get("scope", {}).get("record_ids", ())),
                    include_action_contexts=True,
                    codex_home=self._bound_codex_home(live.document, None),
                )
            )
            return (
                context,
                None,
            )
        except Exception as exc:
            return None, str(exc) or repr(exc)

    @staticmethod
    def _frozen_orca_execution_sources(document, source):
        if document.get("schema_version") != PLAN_V3:
            return source
        from .adapters.orca import OrcaAdapter
        from .client_contracts import describe_adapter
        binaries = {canonical_path(value["frozen"]["profile_root"]): Path(value["frozen"]["binary"]["path"])
                    for value in document.get("target_safety_evidence", ()) if value["frozen"].get("binary")}
        result = []
        for adapter in source:
            descriptor = describe_adapter(adapter)
            binary = binaries.get(canonical_path(descriptor.profile_root)) if descriptor.profile_root else None
            if descriptor.client == "orca" and binary is not None:
                hint = getattr(adapter, "codex_bin_hint", None)
                if hint is not None and Path(hint).absolute() != binary.absolute():
                    raise OperationCoordinatorError("Orca binary hint differs from frozen approval")
                if hint is None:
                    adapter = OrcaAdapter(profile_root=descriptor.profile_root, codex_bin_hint=binary)
            result.append(adapter)
        return tuple(result)

    @staticmethod
    def _orca_recovery_sources(document, source):
        if document.get("schema_version") != PLAN_V3:
            return source
        from .adapters import NativeIntegrityAdapter
        homes = {canonical_path(value["frozen"]["home"]): value["frozen"]
                 for value in document.get("target_safety_evidence", ()) if value["frozen"].get("binary")}
        return tuple(NativeIntegrityAdapter(codex_home=Path(frozen["home"]),
                                            codex_bin_hint=Path(frozen["binary"]["path"]))
                     for frozen in homes.values())

    @staticmethod
    def _orca_runtime_recovery_errors(document):
        if document.get("schema_version") != PLAN_V3:
            return ()
        from .orca_runtime import runtime_instance_stopped
        storages = {s["storage_id"]: Path(s["path"]) for s in document["storages"]}
        try:
            for batch in document.get("child_batches", ()):
                store = OperationStore(storages[batch["storage_id"]], batch["child_operation_id"])
                if not store.directory.exists():
                    continue
                approved = [action for action in document["actions"] if action["action_id"] in batch["action_ids"]]
                from .orca_target_safety import evidence_for_actions
                projection = {"schema_version": "larj.child-operation-plan.v2", "operation_id": batch["child_operation_id"],
                    "target": {"codex_home": str(storages[batch["storage_id"]]), "storage_id": batch["storage_id"]},
                    "parent_operation_id": document["operation_id"], "mutation_family": batch["mutation_family"],
                    "actions": OperationCoordinator._metadata(approved),
                    "startup_boundary": {"schema_version": "larj.orca-startup-boundary.v1", "coordination_scope": "root_wide",
                        "home": str(storages[batch["storage_id"]]), "target_safety_evidence": evidence_for_actions(document, approved)}}
                state = store.read_state()
                if state is None:
                    result = store.read_result()
                    if (result is not None and result.get("goal_status") in {"complete", "completed_with_residuals"}
                            and result.get("plan_sha256") == plan_sha256(projection)
                            and sorted(result.get("action_ids", ())) == sorted(batch["action_ids"])
                            and result.get("runtime_instance") is not None
                            and runtime_instance_stopped(result["runtime_instance"])):
                        continue
                    return ("orca_runtime_instance_unproven",)
                events = [e for e in store.read_events() if e["event"] == "orca_runtime_startup"]
                if not state.get("mutation_started") and not events:
                    continue
                child = store.read_plan()
                if (child.get("plan_sha256") != plan_sha256(projection)
                        or child.get("schema_version") != "larj.child-operation-plan.v2"
                        or child.get("parent_operation_id") != document["operation_id"]
                        or child.get("actions") != approved
                        or child.get("target", {}).get("codex_home") != str(storages[batch["storage_id"]])):
                    return ("orca_runtime_instance_unproven",)
                from .mutation_guard import scopes_for_frozen_plan
                scopes_for_frozen_plan(child)
                if len(events) != 1:
                    return ("orca_runtime_instance_unproven",)
                instance = events[0].get("runtime_instance")
                if state.get("runtime_instance") is not None and state["runtime_instance"] != instance:
                    return ("orca_runtime_instance_unproven",)
                if not runtime_instance_stopped(instance):
                    return ("orca_runtime_instance_running",)
                snapshots = [e for e in store.read_events() if e["event"] == "orca_runtime_boundary_observed"]
                if len(snapshots) > 1:
                    return ("orca_startup_snapshot_invalid",)
                if snapshots:
                    snapshot = snapshots[0].get("startup_artifacts")
                    if state.get("startup_artifacts") is not None and state["startup_artifacts"] != snapshot:
                        return ("orca_startup_snapshot_invalid",)
                    from .orca_target_safety import recheck_startup_snapshot
                    evidence = child["startup_boundary"]["target_safety_evidence"]
                    schemas = {value["schema_version"] for value in evidence}
                    if len(schemas) != 1:
                        return ("orca_startup_snapshot_invalid",)
                    recheck_startup_snapshot(storages[batch["storage_id"]], snapshot, evidence_schema=next(iter(schemas)))
        except (ValueError, OSError, KeyError, TypeError):
            return ("orca_runtime_instance_unproven",)
        return ()

    @staticmethod
    def _outcome_errors(outcome: Any) -> tuple[str, ...]:
        containers = (
            getattr(outcome, "results", ()),
            getattr(getattr(outcome, "cleanup_report", None), "results", ()),
            getattr(getattr(outcome, "session_cleanup", None), "results", ()),
        )
        return tuple(dict.fromkeys(
            str(error) for results in containers for result in results or ()
            for error in (getattr(result, "error", None), getattr(result, "request_error", None))
            if error
        ))

    @staticmethod
    def _outcome_statuses(outcome: Any) -> set[str]:
        statuses: set[str] = set()
        containers = [
            getattr(getattr(outcome, "cleanup_report", None), "results", ()),
            getattr(getattr(outcome, "session_cleanup", None), "results", ()),
        ]
        if hasattr(outcome, "results"):
            containers.append(getattr(outcome, "results", ()))
        for container in containers:
            for result in container or ():
                raw_status = ("blocked" if getattr(result, "preflight_blocked", False) is True
                              else getattr(result, "status", ""))
                status = str(getattr(raw_status, "value", raw_status))
                if status:
                    statuses.add(status)
        for field_name, fallback_status in (
            ("legacy_repair", "repaired"),
            ("desktop_cleanup", "cleaned"),
            ("frontend_cleanup", "cleaned"),
            ("relation_cleanup", "cleaned"),
        ):
            result = getattr(outcome, field_name, None)
            if result is None:
                continue
            raw_status: Any = None
            to_dict = getattr(result, "to_dict", None)
            if callable(to_dict):
                payload = to_dict()
                if isinstance(payload, Mapping):
                    raw_status = payload.get("status")
            status = str(raw_status or fallback_status)
            if status:
                statuses.add(status)
        return statuses

    @staticmethod
    def _outcome_status_for_action(outcome: Any, action: Any) -> str:
        """Return one native result status without rescanning its catalog."""

        target_id = str(getattr(getattr(action, "target", None), "thread_id", ""))
        containers = (
            getattr(outcome, "results", ()),
            getattr(getattr(outcome, "cleanup_report", None), "results", ()),
            getattr(getattr(outcome, "session_cleanup", None), "results", ()),
        )
        for container in containers:
            for result in container or ():
                finding = getattr(result, "finding", None)
                if str(getattr(finding, "thread_id", "")) != target_id:
                    continue
                raw_status = ("blocked" if getattr(result, "preflight_blocked", False) is True
                              else getattr(result, "status", ""))
                return str(getattr(raw_status, "value", raw_status))
        statuses = OperationCoordinator._outcome_statuses(outcome)
        if statuses == {"deleted"}:
            return "deleted"
        if "unknown" in statuses:
            return "unknown"
        if "partial" in statuses:
            return "partial"
        if "not_deleted" in statuses:
            return "not_deleted"
        return ""

    def _status_for_document(self, document: Mapping[str, Any]) -> dict[str, Any]:
        batches: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        started = False
        modified = False
        storage_by_id = {
            str(storage.get("storage_id")): Path(str(storage.get("path")))
            for storage in document.get("storages", ())
            if isinstance(storage, Mapping) and storage.get("path")
        }

        for raw in document.get("child_batches", ()):
            if not isinstance(raw, Mapping):
                continue
            status = "pending"
            child_id = str(raw.get("child_operation_id") or "")
            path = storage_by_id.get(str(raw.get("storage_id")))
            child_result: Mapping[str, Any] | None = None
            child_state: Mapping[str, Any] | None = None
            child_blockers: list[dict[str, Any]] = []
            if path is None or not child_id:
                status = "unknown"
                child_blockers = [self._blocker(
                    "recovery_required",
                    "operation child batch has no trusted storage binding",
                    scope=f"child:{child_id or '<missing>'}",
                )]
            else:
                try:
                    child_store = OperationStore(path, child_id)
                    child_result = child_store.read_result()
                    child_state = (
                        None
                        if child_result is not None
                        else child_store.read_state()
                    )
                except Exception as exc:
                    status = "unknown"
                    child_blockers = [self._blocker(
                        "recovery_required",
                        f"could not trust child operation state: {exc}",
                        scope=f"child:{child_id}",
                    )]
                if child_result:
                    status = str(
                        child_result.get("goal_status")
                        or child_result.get("status")
                        or "unknown"
                    )
                    started = started or bool(
                        child_result.get("mutation_started")
                        or child_result.get("mutation_attempted")
                        or child_result.get("attempted")
                    )
                    modified = modified or bool(child_result.get("modified"))
                    child_blockers = self._child_blockers(child_result, child_id)
                elif child_state:
                    status = str(
                        child_state.get("goal_status")
                        or child_state.get("phase")
                        or "unknown"
                    )
                    started = started or bool(
                        child_state.get("mutation_started")
                        or child_state.get("mutation_attempted")
                        or child_state.get("attempted")
                    )
                    modified = modified or bool(child_state.get("modified"))
                    child_blockers = self._child_blockers(child_state, child_id)

            source = child_result or child_state
            batch_view = {**dict(raw), "status": status}
            if isinstance(source, Mapping) and source.get("error") is not None:
                batch_view["error"] = self._metadata(source.get("error"))
            if child_blockers:
                batch_view["blockers"] = child_blockers
                for blocker in child_blockers:
                    if blocker not in blockers:
                        blockers.append(blocker)
            batches.append(batch_view)
        statuses = {
            str(batch.get("status") or "")
            for batch in batches
        }
        if "unknown" in statuses:
            goal_status = "unknown"
        elif "blocked" in statuses:
            goal_status = "blocked"
        elif "completed_with_residuals" in statuses:
            goal_status = "completed_with_residuals"
        elif statuses and statuses <= {"complete"}:
            goal_status = "complete"
        elif self._blocked_without_mutation(document):
            goal_status = "blocked"
            blockers = list(document.get("blockers", ()))
        else:
            goal_status = "ready"
        return {
            "schema_version": "larj.operation-result.v1",
            "document_type": "operation_result",
            "operation_id": document.get("operation_id"),
            "plan_sha256": document.get("plan_sha256"),
            "goal_status": goal_status,
            "goal_satisfied": goal_status == "complete",
            "modified": modified,
            "mutation_started": started,
            "scope": document.get("scope", {}),
            "batches": batches,
            "blockers": [self._metadata(value) for value in blockers],
        }

    @staticmethod
    def _blocked_without_mutation(document: Mapping[str, Any]) -> bool:
        return document.get("goal_status") == "blocked" and not document.get("actions") and not document.get("child_batches")

    def _result_document(
        self,
        document: Mapping[str, Any],
        scope: Mapping[str, Any],
        *,
        goal_status: str,
        blockers: Sequence[Mapping[str, Any]],
        batches: Sequence[Mapping[str, Any]],
        residuals: Sequence[str] = (),
        modified: bool = False,
        mutation_started: bool = False,
    ) -> dict[str, Any]:
        if document.get("schema_version") == PLAN_V3 and goal_status == "unknown":
            inspection = self._inspect_child_states(document)
            modified = modified or inspection.modified
            mutation_started = mutation_started or inspection.mutation_started
            batches = batches or inspection.batches
        return {
            "schema_version": "larj.operation-result.v1",
            "document_type": "operation_result",
            "operation_id": document.get("operation_id"),
            "plan_sha256": document.get("plan_sha256"),
            "goal_status": goal_status,
            "goal_satisfied": goal_status == "complete",
            "modified": bool(modified),
            "mutation_started": bool(mutation_started),
            "scope": dict(scope),
            "batches": [dict(value) for value in batches],
            "residual_action_ids": list(residuals),
            "blockers": [dict(value) for value in blockers],
            "counts": {
                "batch_count": len(batches),
                "action_count": sum(
                    len(value.get("action_ids", ())) for value in batches
                ),
                "residual_count": len(residuals),
            },
        }

    def _error_document(
        self,
        subcommand: str,
        scope: Mapping[str, Any],
        message: str,
        *,
        operation_id: str | None,
        blocker_code: str = "operation_backend_failed",
    ) -> dict[str, Any]:
        return {
            "schema_version": "larj.operation-result.v1",
            "document_type": "operation_result",
            "subcommand": subcommand,
            "operation_id": operation_id or "unaccepted",
            "goal_status": "blocked",
            "goal_satisfied": False,
            "modified": False,
            "mutation_started": False,
            "scope": dict(scope),
            "blockers": [self._blocker(blocker_code, message)],
            "counts": {},
        }

    @staticmethod
    def _blocker(
        code: str,
        message: str,
        *,
        scope: str = "operation",
        action_id: str | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "blocker_code": code,
            "scope": scope,
            "severity": "error",
            "retryable": True,
            "message": str(message),
        }
        if action_id:
            value["action_id"] = action_id
        return value

    @staticmethod
    def _metadata(value: Any, *, key: str | None = None, _workbuddy_nulls: bool = False) -> Any:
        if key is not None and key.casefold() in _BODY_KEYS:
            return None
        # Exact WorkBuddy evidence distinguishes a missing row/usage/sidecar
        # from a value. Preserve its explicit nulls through plan persistence;
        # continue applying the ordinary body-key filter throughout the tree.
        _workbuddy_nulls = _workbuddy_nulls or key == "workbuddy_session_evidence"
        if key in {"title", "display_name", "thread_name", "name"}:
            from .display_metadata import display_title
            return display_title(value)
        if key == "desktop_catalog_titles" and isinstance(value, (list, tuple)):
            return [OperationCoordinator._metadata(item, key="title") for item in value]
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for name, raw in value.items():
                cleaned = OperationCoordinator._metadata(raw, key=str(name), _workbuddy_nulls=_workbuddy_nulls)
                if cleaned is not None or (_workbuddy_nulls and raw is None and str(name).casefold() not in _BODY_KEYS):
                    result[str(name)] = cleaned
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            return [OperationCoordinator._metadata(item, _workbuddy_nulls=_workbuddy_nulls) for item in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return OperationCoordinator._metadata(to_dict(), _workbuddy_nulls=_workbuddy_nulls)
        return str(value)

    @staticmethod
    def _new_operation_id(client: str) -> str:
        import datetime

        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        return f"{client}-{stamp}-{uuid.uuid4().hex[:10]}"


def _looks_like_project_path(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if "/" in text or "\\" in text:
        return True
    try:
        return Path(text).expanduser().is_absolute()
    except (OSError, ValueError):
        return False


__all__ = ["OperationCoordinator", "OperationCoordinatorError"]


def _operation_plan_path(root: Path, operation_id: str) -> Path:
    """Map one explicit store/operation root to the canonical plan path."""

    if not operation_id:
        raise OperationCoordinatorError("operation ID is required to locate a plan")
    root = Path(root).expanduser().absolute()
    name = root.name.casefold()
    operation_name = str(operation_id).casefold()
    if name == operation_name:
        return root / "plan.json"
    if name == "operations":
        return root / operation_id / "plan.json"
    if name == ".local-agent-record-janitor":
        return root / "operations" / operation_id / "plan.json"
    return root / ".local-agent-record-janitor" / "operations" / operation_id / "plan.json"


def _find_operation_plan(
    operation_id: str | None,
    *,
    operation_home: Path | None = None,
    codex_home: Path | None = None,
) -> Path | None:
    """Find a plan without scanning catalogs or guessing a record target.

    Recovery callers should pass the exact physical store root whenever one is
    known. The small set of layout variants supports the public CLI's
    ``operation_home`` value (which may be a store, ``operations`` directory,
    or the operation directory itself) while retaining the per-user fallback
    used by read-only plan output.
    """

    if not operation_id:
        return None
    candidates: list[Path] = []
    for root in (operation_home, codex_home):
        if root is None:
            continue
        base = Path(root).expanduser().absolute()
        candidates.append(_operation_plan_path(base, str(operation_id)))
        # A caller may provide a plan path's parent without using the canonical
        # hidden directory name. This is still an exact operation-id lookup,
        # never a recursive search.
        candidates.extend((
            base / str(operation_id) / "plan.json",
            base / "operations" / str(operation_id) / "plan.json",
        ))
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        state_root = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_STATE_HOME"):
        state_root = Path(os.environ["XDG_STATE_HOME"])
    else:
        state_root = Path.home() / (
            "AppData/Local" if os.name == "nt" else ".local/state"
        )
    candidates.append(
        state_root / "local-agent-record-janitor" / "plans"
        / f"{operation_id}.json"
    )
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None
