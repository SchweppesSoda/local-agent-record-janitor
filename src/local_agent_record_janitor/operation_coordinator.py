"""Shared operation lifecycle for the thin command facades."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .action_registry import action_capability
from .agent_operations import action_binding
from .operation_store import OperationStore, plan_sha256, strict_json_load, write_new_json
from .record_identity import (
    capability_for,
    canonical_path,
    normalize_client,
    normalize_engine,
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
        adapters: Iterable[Any] | None = None,
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
                engines=tuple(normalized_scope.get("engines", ())),
                include_action_contexts=True,
                codex_home=codex_home,
            )
            candidates, blockers = self._select_candidates(context, normalized_scope)
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
            )
            write_new_json(Path(str(document["plan_path"])), document)
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
        plan_sha256: str | None = None,
        clients_closed: bool = False,
        adapters: Iterable[Any] | None = None,
        timeout: float = 30.0,
        app_server_factory: Any = None,
        binary_resolver: Any = None,
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
        try:
            document = self._load_plan(
                operation_id,
                plan_path,
                plan_sha256,
                operation_home=operation_home,
                codex_home=codex_home,
            )
            self._validate_apply_scope(document, normalized_scope)
            child_inspection = self._inspect_child_states(document)
            if child_inspection.blockers:
                return self._result_document(
                    document,
                    normalized_scope,
                    goal_status="unknown",
                    blockers=child_inspection.blockers,
                    batches=child_inspection.batches,
                    modified=child_inspection.modified,
                    mutation_started=child_inspection.mutation_started,
                )
            operation = str(document["operation_id"])
            client_name = str(document["scope"]["client"])
            live = self._live.get(operation)
            if live is not None:
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
                    return dict(live.result)
            else:
                source = None if adapters is None else tuple(adapters)
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
                    engines=tuple(normalized_scope.get("engines", ())),
                    include_action_contexts=True,
                    codex_home=codex_home,
                )
                candidates, blockers = self._bind_fresh_candidates(document, context)
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
            return self._execute_live(
                live,
                timeout=float(timeout),
                app_server_factory=app_server_factory,
                binary_resolver=binary_resolver,
            )
        except Exception as exc:
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
        clients_closed: bool = False,
        adapters: Iterable[Any] | None = None,
        timeout: float = 30.0,
        app_server_factory: Any = None,
        binary_resolver: Any = None,
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
            adapters=adapters,
        )
        if planned.get("goal_status") != "ready":
            return planned
        live = self._live.get(str(planned["operation_id"]))
        if live is None:
            return self._error_document(
                "run", normalized_scope, "operation_context_unavailable",
                operation_id=operation_id, blocker_code="operation_context_unavailable",
            )
        return self._execute_live(
            live,
            timeout=float(timeout),
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
        )

    def status_operation(
        self,
        *,
        operation_id: str | None = None,
        plan_path: Path | None = None,
        operation_home: Path | None = None,
        codex_home: Path | None = None,
        scope: Mapping[str, Any] | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        live = self._live.get(str(operation_id or ""))
        if live is not None:
            return dict(live.result) if live.result is not None else self._status_for_document(live.document)
        try:
            return self._status_for_document(
                self._load_plan(
                    operation_id,
                    plan_path,
                    None,
                    operation_home=operation_home,
                    codex_home=codex_home,
                )
            )
        except Exception as exc:
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
        scope: Mapping[str, Any] | None = None,
        adapters: Iterable[Any] | None = None,
        verify_timeout: int = 180,
        **_unused: Any,
    ) -> dict[str, Any]:
        del verify_timeout
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
                client_name = str(document["scope"]["client"])
                source = None if adapters is None else tuple(adapters)
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
                    engines=tuple(document.get("scope", {}).get("engines", ())),
                    include_action_contexts=True,
                    codex_home=codex_home,
                )
                storage_blockers = self._frozen_store_blockers(document)
                if storage_blockers:
                    return self._result_document(
                        document, dict(scope or {}), goal_status="unknown",
                        blockers=storage_blockers, batches=(),
                    )
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
            except Exception as exc:
                return self._error_document(
                    "verify", dict(scope or {}), str(exc) or repr(exc),
                    operation_id=operation_id, blocker_code="operation_verify_failed",
                )
        if terminal is None:
            terminal, error = self._terminal_context(live)
        if error is not None:
            return self._result_document(
                live.document, dict(scope or {}), goal_status="unknown",
                blockers=[self._blocker("terminal_scan_incomplete", error)], batches=(),
            )
        if not bool(getattr(getattr(terminal, "plan", None), "scan_complete", True)):
            errors = getattr(getattr(terminal, "plan", None), "errors", ())
            message = "; ".join(str(value) for value in errors)
            if not message:
                message = "terminal verification scan is incomplete"
            return self._result_document(
                live.document,
                live.document.get("scope", {}),
                goal_status="unknown",
                blockers=[self._blocker("terminal_scan_incomplete", message)],
                batches=(),
            )
        try:
            residuals = self._residual_action_ids(live.document, terminal)
        except Exception as exc:
            return self._result_document(
                live.document,
                live.document.get("scope", {}),
                goal_status="unknown",
                blockers=[self._blocker("terminal_scan_incomplete", str(exc))],
                batches=(),
            )
        try:
            self._persist_verified_child_journals(live.document, residuals)
        except Exception as exc:
            return self._result_document(
                live.document,
                live.document.get("scope", {}),
                goal_status="unknown",
                blockers=[self._blocker(
                    "verification_journal_failed", str(exc) or repr(exc)
                )],
                batches=(),
                residuals=residuals,
            )
        return self._result_document(
            live.document, dict(scope or {}),
            goal_status="completed_with_residuals" if residuals else "complete",
            blockers=[self._blocker("residual_records", "approved records remain")] if residuals else [],
            batches=(), residuals=residuals,
        )

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

        frozen = cls._frozen_action_signatures(document)
        fresh = cls._fresh_action_signatures(context)
        residuals: list[str] = []
        for signature, action_ids in frozen.items():
            if signature in fresh:
                residuals.extend(action_ids)
        return list(dict.fromkeys(residuals))

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
                if state is None or not (
                    bool(state.get("mutation_started"))
                    or bool(state.get("modified"))
                ):
                    continue
                store.append_event(
                    {
                        "event": "verification_finished",
                        "goal_status": goal,
                        "verified_action_ids": verified,
                        "residual_action_ids": child_residuals,
                    },
                    state_updates={
                        "phase": "finished",
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
                    "modified": bool(state.get("modified")),
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

    def _build_context(
        self,
        client: str,
        adapters: tuple[Any, ...] | None,
        *,
        engines: Sequence[str] = (),
        include_action_contexts: bool = False,
        codex_home: Path | None = None,
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
        if client in {"native", "codex-desktop"}:
            # Native inventory is the authoritative healthy-record snapshot.
            # The regular anomaly scanner is only added for an incomplete or
            # suspicious catalog; otherwise calling both scanners would walk
            # every rollout twice before one plan is written.
            from .inventory import build_session_catalog
            from .manual_delete import build_manual_delete_plan

            catalog = build_session_catalog(selected)
            manual_plan = build_manual_delete_plan(catalog)
            if self._native_catalog_is_healthy(catalog, manual_plan):
                result = self._native_manual_context(
                    selected,
                    catalog,
                    manual_plan,
                )
                return (*result, {}) if include_action_contexts else result
            context = self.service.prepare(
                selected,
                platforms=("native" if client == "codex-desktop" else client,),
            )
            result = self._merge_native_manual_records(
                context,
                selected,
                catalog=catalog,
                manual_plan=manual_plan,
            )
            return (*result, {}) if include_action_contexts else result
        context = self.service.prepare(
            selected,
            platforms=("native" if client == "codex-desktop" else client,),
        )
        if client not in {"cindy", "aionui"}:
            result = (context, selected, None, None, {}, {})
            return result if include_action_contexts else result[:5]
        result = self._merge_client_engine_contexts(
            context,
            selected,
            client=client,
            engines=engines,
        )
        return result if include_action_contexts else result[:5]

    def _merge_client_engine_contexts(
        self,
        context: Any,
        adapters: tuple[Any, ...],
        *,
        client: str,
        engines: Sequence[str],
    ) -> tuple[Any, tuple[Any, ...], None, None, Mapping[str, Any], Mapping[str, Any]]:
        """Add verified native child contexts to one frontend operation.

        The client inventory owns the single frontend snapshot.  Native
        session catalogs are then projected into the existing CleanupPlan and
        retained in ``action_contexts`` so execution dispatches each child to
        the registered Pi/Claude writer without rebuilding frontend state.
        """

        from .client_inventory import (
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
        )
        engine_contexts = build_client_engine_contexts(
            adapters,
            client=client,
            engines=engines,
            inventory=inventory,
        )
        action_contexts: dict[str, Any] = {}
        native_contexts: list[Any] = []
        for engine_context in engine_contexts:
            engine = normalize_engine(engine_context.engine)
            catalog = engine_context.native_catalog
            capability = engine_context.capability
            if engine not in {"pi", "claude"}:
                continue
            # AionUI's generic codex_home is not a Pi/Claude proof.  Its
            # ClientEngineContext consequently has native_delete=False and
            # cannot enter this branch.
            if catalog is None or not capability.native_delete:
                continue
            target_action_ids = {
                str(action_id)
                for target in engine_context.targets
                for action_id in target.action_ids
                if str(action_id)
            }
            if not target_action_ids:
                continue

            def catalog_builder(
                *,
                _engine: str = engine,
                _catalog: Any = catalog,
                _adapters: tuple[Any, ...] = adapters,
            ) -> Any:
                for adapter in _adapters:
                    builder = getattr(adapter, "native_catalog_for", None)
                    if callable(builder):
                        fresh = builder(_engine)
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
            )
            native_actions = tuple(
                action
                for action in getattr(native_context, "actions", ())
                if str(getattr(action, "action_id", "")) in target_action_ids
            )
            if not native_actions:
                continue
            native_contexts.append(native_context)
            for action in native_actions:
                action_contexts[str(action.action_id)] = native_context

        if not native_contexts:
            return context, adapters, None, None, {}, action_contexts
        return (
            self._merge_cleanup_contexts(context, tuple(native_contexts)),
            adapters,
            None,
            None,
            {},
            action_contexts,
        )

    def _merge_cindy_terminal_sessions(
        self,
        context: Any,
        inventory: Any,
        *,
        client: str,
    ) -> Any:
        """Project exact soft-deleted Cindy rows into cleanup batches."""

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

        grouped: dict[str, list[Any]] = {}
        databases: dict[str, Path] = {}
        for session in getattr(inventory, "frontend_sessions", ()):
            if str(getattr(session, "platform", "")).casefold() != "cindy":
                continue
            status = str(getattr(session, "status", "") or "").casefold()
            details = getattr(session, "details", {})
            if status != "deleted" or not isinstance(
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
            key = os.path.normcase(os.path.abspath(str(database)))
            databases[key] = database.expanduser().absolute()
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
            database = databases[key]
            seeds = []
            for session in grouped[key]:
                reference = session.details["frontend_reference"]
                seeds.append({
                    "database": str(database),
                    "session_id": session.platform_session_id,
                    "expected_status": session.status,
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
                }
                if isinstance(working_dir, str) and working_dir.strip():
                    payload["project_path"] = working_dir.strip()
                impact = ActionImpact(
                    frontend_session_database_paths=(str(database),),
                    frontend_session_evidence=(evidence,),
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
        name = str(getattr(adapter, "name", "")).casefold().replace("_", "-")
        if client == "native":
            return name in {"native", "codex-native", "codex-desktop"}
        return name == client

    @staticmethod
    def _default_adapters(
        client: str,
        *,
        codex_home: Path | None = None,
    ) -> Sequence[Any]:
        from .adapter_factory import create_default_adapters

        return create_default_adapters(
            OperationCoordinator._default_catalog_args(
                client,
                codex_home=codex_home,
            )
        )

    @staticmethod
    def _default_catalog_args(
        client: str,
        *,
        codex_home: Path | None = None,
    ) -> Any:
        return SimpleNamespace(
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

            def matches_record(action: Any, selector: str) -> bool:
                identifiers = {str(action.target.thread_id)}
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
                return selector in identifiers or any(
                    value.startswith(selector) for value in identifiers
                )

            found = {
                selector
                for selector in wanted
                if any(matches_record(action, selector) for action in actions)
            }
            actions = tuple(
                action for action in actions
                if any(matches_record(action, selector) for selector in wanted)
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
                "project_id", "project_paths",
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
        path_values = tuple(
            value for value in values if _looks_like_project_path(value)
        )
        # A display label is useful for selection, but once a path is present
        # it must not become a second project identity for the same action.
        return path_values or values

    @staticmethod
    def _selector_matches(selector: str, value: str) -> bool:
        raw, candidate = str(selector).strip(), str(value).strip()
        if not raw or not candidate:
            return False
        if raw.casefold() == candidate.casefold():
            return True
        if (
            candidate.casefold().startswith(raw.casefold())
            and "/" not in raw
            and "\\" not in raw
        ):
            return True
        try:
            return Path(candidate).name.casefold() == raw.casefold()
        except (OSError, ValueError):
            return False

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
                return normalize_engine(getattr(observation, "platform", "codex"))
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
        action_docs = [self._action_document(context, action) for action in candidates]
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
                ) == "delete_project_item"
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
        payload["plan_sha256"] = plan_sha256(payload)
        return payload

    def _action_document(self, context: Any, action: Any) -> dict[str, Any]:
        raw = getattr(
            action, "to_dict", lambda: {"action_id": str(action.action_id)}
        )()
        result = self._metadata(raw)
        result["binding"] = self._metadata(action_binding(action))
        result["classification"] = self._classification(context, action)
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
    def _classification(context: Any, action: Any) -> str:
        if str(
            getattr(
                getattr(action, "kind", None),
                "value",
                getattr(action, "kind", ""),
            )
        ) == "delete_project_item":
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
        return "orphan_native"

    @staticmethod
    def _default_engine(client: str) -> str:
        return {"pi": "pi", "claude": "claude"}.get(client, "codex")

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
            document.get("schema_version") != "larj.operation-plan.v1"
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
    ) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
        frozen = {
            str(item.get("action_id")): item
            for item in document.get("actions", ())
            if isinstance(item, Mapping)
        }
        current = {
            str(action.action_id): action
            for action in getattr(context.plan, "actions", ())
        }
        selected: list[Any] = []
        blockers: list[dict[str, Any]] = []
        for batch in document.get("child_batches", ()):
            if not isinstance(batch, Mapping):
                continue
            for action_id in batch.get("action_ids", ()):
                key = str(action_id)
                action = current.get(key)
                frozen_action = frozen.get(key)
                if action is None or not isinstance(frozen_action, Mapping):
                    blockers.append(self._blocker(
                        "target_state_changed",
                        f"approved action {key} is no longer present",
                        scope=f"action:{key}", action_id=key,
                    ))
                    continue
                if self._metadata(action_binding(action)) != frozen_action.get("binding"):
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
                    )
                    if goal in {"complete", "completed_with_residuals"}:
                        completed.add(child_id)
                        view["status"] = goal
                        batches.append(view)
                        continue
                    view["status"] = "unknown"
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

                goal = str(state.get("goal_status") or "")
                phase = str(state.get("phase") or "")
                started = bool(state.get("mutation_started"))
                modified = modified or bool(state.get("modified"))
                mutation_started = mutation_started or started
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
                    blockers.append(self._blocker(
                        "recovery_required",
                        "child terminal state lacks a trusted completion event",
                        scope=f"child:{child_id}",
                    ))
                    batches.append(view)
                    continue

                # The only resumable persisted state is the preflight phase
                # before any irreversible request was marked durable.
                if phase == "preflight" and not started:
                    view["status"] = "pending"
                    batches.append(view)
                    continue

                view["status"] = "unknown"
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
    ) -> dict[str, Any]:
        from .cleanup_service import partition_actions
        from .codex_app_server import CodexAppServer
        from .discovery import choose_codex_binary

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
            return result

        completed_child_ids = (
            set(child_inspection.completed_child_ids)
            | set(getattr(live, "completed_child_ids", ()))
        )
        batches = partition_actions(live.candidates)
        from .manual_delete import (
            build_manual_delete_closure,
            frontend_actions_after_native_success,
        )

        manual_ids = {
            str(action_id)
            for action_id in getattr(live, "manual_actions", {})
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
        for index, batch in enumerate(batches):
            child_id = f"{live.operation_id}-{index + 1}"
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
                continue
            batch_mutation_started = False
            store: OperationStore | None = None
            try:
                storage_path = self._storage_path(
                    live.context, batch.storage_id, batch.actions[0]
                )
                store, state = self._open_batch_store(
                    live, batch, child_id, storage_path, index
                )
                with store.mutation_lock():
                    def checkpoint(
                        phase: str,
                        action: Any,
                        result: Any | None,
                    ) -> None:
                        nonlocal batch_mutation_started, mutation_started
                        status = (
                            str(getattr(result, "status", ""))
                            if result is not None else ""
                        )
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
                        typed = tuple(
                            typed_by_id[str(action.action_id)]
                            for action in batch.actions
                        )
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
                batch_modified = bool(
                    statuses.intersection({"deleted", "cleaned", "repaired"})
                    or getattr(outcome, "modified", False)
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
                    "status": "unknown" if batch_unknown else "complete",
                    "action_ids": [str(action.action_id) for action in batch.actions],
                    "result": outcome_doc,
                }
                batch_result.update(self._batch_scope_metadata(live.context, batch))
                batch_results.append(batch_result)
                store.append_event(
                    {
                        "event": "batch_finished",
                        "goal_status": "unknown" if batch_unknown else "complete",
                    },
                    state_updates={
                        "phase": "recovery_required" if batch_unknown else "finished",
                        "goal_status": "unknown" if batch_unknown else "complete",
                        "goal_satisfied": not batch_unknown,
                        "modified": any_modified,
                        # The child journal records its own irreversible
                        # marker. The operation-level flag may already be
                        # true because an earlier child completed.
                        "mutation_started": batch_mutation_started,
                    },
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
                batch_unknown = (
                    not known_rollback
                    and (
                        batch_mutation_started
                        or "unknown" in str(exc).casefold()
                    )
                )
                stopped_unknown = stopped_unknown or batch_unknown
                failed_batch = {
                    "batch_id": batch.batch_id,
                    "child_operation_id": child_id,
                    "storage_id": batch.storage_id,
                    "mutation_family": batch.mutation_family,
                    "status": "unknown" if batch_unknown else "blocked",
                    "action_ids": [str(action.action_id) for action in batch.actions],
                    "error": str(exc) or repr(exc),
                }
                failed_batch.update(self._batch_scope_metadata(live.context, batch))
                if not batch_unknown and store is not None:
                    try:
                        # Persist the explicit known-unchanged outcome so a
                        # later status read does not mistake a guarded child
                        # for an ambiguous native mutation. A child that has
                        # started a transaction remains non-resumable; a
                        # fresh plan is required to retry it.
                        state_updates = {
                            "phase": "blocked",
                            "goal_status": "blocked",
                            "goal_satisfied": False,
                            "modified": False,
                            "mutation_started": batch_mutation_started,
                        }
                        event = {
                            "event": "batch_finished",
                            "goal_status": "blocked",
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
                        # If the known rollback itself cannot be journaled,
                        # the operation is no longer trustworthy and must
                        # return to the conservative unknown boundary.
                        batch_unknown = True
                        failed_batch["status"] = "unknown"
                        failed_batch["error"] = (
                            f"{failed_batch['error']}; could not persist "
                            f"known rollback: {journal_error}"
                        )
                batch_results.append(failed_batch)
                if batch_unknown:
                    break
                blocked_batches.append(child_id)
        # An ambiguous irreversible result is a recovery boundary. Do not
        # trigger even the terminal catalog pass here: explicit ``verify`` is
        # the only recovery operation allowed to rescan after ``unknown``.
        # This also prevents a terminal callback or adapter side effect from
        # being mistaken for progress on a stopped operation.
        if stopped_unknown:
            terminal, terminal_error = None, None
        else:
            terminal, terminal_error = self._terminal_context(live)
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
        for child_id in blocked_batches:
            blockers.append(self._blocker(
                "batch_execution_blocked",
                "a child batch failed before an irreversible request",
                scope=f"child:{child_id}",
            ))
        if residuals:
            blockers.append(self._blocker("residual_records", "approved records remain"))
        if stopped_unknown:
            goal = "unknown"
            blockers.append(self._blocker(
                "mutation_outcome_unknown",
                "an irreversible result was not confirmed",
            ))
        elif terminal_error is not None:
            goal = "unknown"
        elif blocked_batches:
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
                by_native.setdefault(str(manual.thread_id), []).extend(
                    getattr(
                        getattr(frontend_action, "impact", None),
                        "frontend_reference_evidence",
                        (),
                    )
                )
        # Guard all frozen frontend rows for each physical database as one
        # collection before execute_manual_delete can emit mutation_started.
        for by_native in evidence_by_database.values():
            guard_frontend_reference_closure(by_native)
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
        )

    def _open_batch_store(
        self,
        live: _LiveOperation,
        batch: Any,
        child_id: str,
        storage_path: Path,
        index: int,
    ) -> tuple[OperationStore, dict[str, Any]]:
        child_actions = [
            self._action_document(live.context, action)
            for action in batch.actions
        ]
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
        child_plan["plan_sha256"] = plan_sha256(child_plan)
        store = OperationStore(storage_path, child_id)
        store.accept_plan(child_plan)
        # A second process may be resuming an existing preflight child. The
        # child plan is immutable, and its state/journal are durable evidence;
        # never rewrite either file while opening the batch. Recovery states
        # have already been rejected by ``_inspect_child_states``.
        if store.state_path.exists() or store.events_path.exists() or store.receipt_path.exists():
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
    ) -> tuple[Any | None, str | None]:
        try:
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
                    ),
                    None,
                )
            context, _adapters, _catalog, _manual_plan, _manual_actions, _action_contexts = (
                self._build_context(
                    live.client,
                    live.adapters,
                    engines=tuple(
                        live.document.get("scope", {}).get("engines", ())
                    ),
                    include_action_contexts=True,
                )
            )
            return (
                context,
                None,
            )
        except Exception as exc:
            return None, str(exc) or repr(exc)

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
                raw_status = getattr(result, "status", "")
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
            getattr(getattr(outcome, "cleanup_report", None), "results", ()),
            getattr(getattr(outcome, "session_cleanup", None), "results", ()),
        )
        for container in containers:
            for result in container or ():
                finding = getattr(result, "finding", None)
                if str(getattr(finding, "thread_id", "")) != target_id:
                    continue
                raw_status = getattr(result, "status", "")
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
            if path is not None and child_id:
                try:
                    child_store = OperationStore(path, child_id)
                    child_result = child_store.read_result()
                    child_state = (
                        None
                        if child_result is not None
                        else child_store.read_state()
                    )
                except Exception:
                    child_result = None
                    child_state = None
                    status = "unknown"
                if child_result:
                    status = str(
                        child_result.get("goal_status")
                        or child_result.get("status")
                        or "unknown"
                    )
                    started = started or bool(child_result.get("mutation_started"))
                    modified = modified or bool(child_result.get("modified"))
                elif child_state:
                    status = str(
                        child_state.get("goal_status")
                        or child_state.get("phase")
                        or "unknown"
                    )
                    started = started or bool(child_state.get("mutation_started"))
                    modified = modified or bool(child_state.get("modified"))
            batches.append({**dict(raw), "status": status})
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
            "blockers": [],
        }

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
    def _metadata(value: Any, *, key: str | None = None) -> Any:
        if key is not None and key.casefold() in _BODY_KEYS:
            return None
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for name, raw in value.items():
                cleaned = OperationCoordinator._metadata(raw, key=str(name))
                if cleaned is not None:
                    result[str(name)] = cleaned
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            return [OperationCoordinator._metadata(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return OperationCoordinator._metadata(to_dict())
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
