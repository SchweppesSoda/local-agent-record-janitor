from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from io import StringIO
from pathlib import Path
from typing import Any, TextIO

from .adapters.base import FrontendAdapter
from .action_registry import ACTION_REGISTRY, action_capability
from .agent_operations import (
    PLAN_SCHEMA,
    action_binding,
    build_agent_plan_document,
    enum_value,
    new_operation_id,
    plan_counts,
    result_document,
    structured_blocker,
    verify_frozen_actions,
    zero_counts,
)
from .cleaner import AppServerFactory, BinaryResolver
from .cleanup_service import CleanupService
from .codex_desktop_state import (
    ClientInspector,
    DesktopStateError,
    running_related_clients,
)
from .discovery import default_codex_home
from .operation_store import (
    OperationLockedError,
    OperationStore,
    OperationStoreError,
    plan_sha256,
    strict_json_load,
    utc_now,
    write_new_json,
)
from .path_identity import canonical_existing_path_key
from .planning import (
    ScanStatus,
    StorageLocation,
    normalize_storage_path,
    storage_id_for_path,
)


EXIT_OK = 0
EXIT_UNKNOWN = 1
EXIT_BLOCKED = 3


def run_agent_command(
    args: argparse.Namespace,
    *,
    supplied_adapters: Iterable[FrontendAdapter] | None,
    stdout: TextIO,
    stderr: TextIO,
    app_server_factory: AppServerFactory,
    binary_resolver: BinaryResolver,
    client_inspector: ClientInspector | None = None,
    cleanup_service: CleanupService | None = None,
) -> int:
    del stderr  # Agent mode writes exactly one JSON/JSONL stream to stdout.
    if cleanup_service is not None:
        service = cleanup_service
        inspect_clients = service.client_inspector
    else:
        inspect_clients = client_inspector or running_related_clients
        service = CleanupService(client_inspector=inspect_clients)
    try:
        if args.agent_command == "doctor":
            return _run_doctor(
                args,
                supplied_adapters,
                stdout,
                client_inspector=inspect_clients,
                cleanup_service=service,
            )
        if args.agent_command == "plan":
            return _run_plan(
                args,
                supplied_adapters,
                stdout,
                cleanup_service=service,
            )
        if args.agent_command == "apply":
            return _run_apply(
                args,
                supplied_adapters,
                stdout,
                app_server_factory=app_server_factory,
                binary_resolver=binary_resolver,
                client_inspector=inspect_clients,
                cleanup_service=service,
            )
        if args.agent_command == "status":
            return _run_status(args, stdout)
        if args.agent_command == "verify":
            return _run_verify(
                args,
                supplied_adapters,
                stdout,
                cleanup_service=service,
            )
        raise OperationStoreError("Unknown agent subcommand")
    except Exception as exc:
        blocker = structured_blocker(
            "agent_internal_error",
            scope="agent_command",
            retryable=False,
            remediation="Inspect the structured error and do not retry mutation blindly.",
            message=str(exc),
        )
        _write_document(
            result_document(
                subcommand=str(getattr(args, "agent_command", "unknown")),
                operation_id="unaccepted",
                plan_sha="",
                goal_status="unknown",
                modified=False,
                mutation_started=False,
                blockers=[blocker],
                phase="failed",
            ),
            stdout,
        )
        return EXIT_UNKNOWN


def _run_doctor(
    args: argparse.Namespace,
    supplied_adapters: Iterable[FrontendAdapter] | None,
    stdout: TextIO,
    *,
    client_inspector: ClientInspector,
    cleanup_service: CleanupService,
) -> int:
    operation_id = "doctor-" + new_operation_id().removeprefix("purge-")
    try:
        context = _scan_context(
            args,
            supplied_adapters,
            cleanup_service=cleanup_service,
        )
        target_home = _target_home(args)
        storage = _target_storage(
            context["plan"], target_home, context["active_adapters"]
        )
        blockers: list[dict[str, Any]] = []
        checks = [
            {
                "check_code": "scan_complete",
                "ok": bool(context["plan"].scan_complete),
            },
            {
                "check_code": "target_store_resolved",
                "ok": storage is not None,
            },
        ]
        if not context["plan"].scan_complete:
            blockers.append(
                structured_blocker(
                    "scan_incomplete",
                    scope="target_store",
                    remediation="Resolve scan errors before creating an apply plan.",
                    message="; ".join(context["plan"].errors),
                )
            )
        if storage is None:
            blockers.append(
                structured_blocker(
                    "target_store_not_found",
                    scope="target_store",
                    retryable=False,
                    remediation="Pass the exact --codex-home for the target store.",
                )
            )
        clients: tuple[str, ...] = ()
        client_error: str | None = None
        try:
            clients = client_inspector(target_home)
        except DesktopStateError as exc:
            client_error = str(exc)
            blockers.append(
                structured_blocker(
                    "client_check_failed",
                    scope="target_store",
                    remediation="Run doctor again with Windows process inspection available.",
                    message=client_error,
                )
            )
        if clients:
            blockers.append(
                structured_blocker(
                    "target_client_running",
                    scope="target_store",
                    remediation="Close the listed target-store clients before apply.",
                    message=", ".join(clients),
                )
            )
        checks.append(
            {
                "check_code": "target_clients_closed",
                "ok": not clients and client_error is None,
                "running_clients": list(clients),
                "error": client_error,
            }
        )
        status = "ready" if not blockers else "unknown"
        document = {
            "schema_version": "larj.agent-doctor.v1",
            "document_type": "agent_doctor_result",
            "command": "agent",
            "subcommand": "doctor",
            "mode": "agent",
            "phase": "completed",
            "operation_id": operation_id,
            "status": status,
            "target": {
                "codex_home": canonical_existing_path_key(target_home),
                "platforms": context["platforms"],
            },
            "checks": checks,
            "blockers": blockers,
        }
        _write_document(document, stdout)
        return EXIT_OK if not blockers else EXIT_UNKNOWN
    except Exception as exc:
        _write_document(
            {
                "schema_version": "larj.agent-doctor.v1",
                "document_type": "agent_doctor_result",
                "command": "agent",
                "subcommand": "doctor",
                "mode": "agent",
                "phase": "failed",
                "operation_id": operation_id,
                "status": "unknown",
                "target": {
                    "codex_home": canonical_existing_path_key(_target_home(args)),
                },
                "checks": [],
                "blockers": [
                    structured_blocker(
                        "environment_check_failed",
                        scope="target_store",
                        remediation="Resolve the reported environment error and retry doctor.",
                        message=str(exc),
                    )
                ],
            },
            stdout,
        )
        return EXIT_UNKNOWN


def _run_plan(
    args: argparse.Namespace,
    supplied_adapters: Iterable[FrontendAdapter] | None,
    stdout: TextIO,
    *,
    cleanup_service: CleanupService,
) -> int:
    operation_id = new_operation_id()
    try:
        context = _scan_context(
            args,
            supplied_adapters,
            cleanup_service=cleanup_service,
        )
        target_home = _target_home(args)
        storage = _target_storage(
            context["plan"], target_home, context["active_adapters"]
        )
        target_actions = [
            action
            for action in context["plan"].actions
            if storage is not None
            and str(action.target.storage_id) == str(storage.storage_id)
        ]
        mutation_kind, selected_actions = _next_frozen_batch(target_actions)
        if not context["plan"].scan_complete:
            mutation_kind, selected_actions = None, []
        document = build_agent_plan_document(
            operation_id=operation_id,
            codex_home=target_home,
            platforms=context["platforms"],
            storage=storage,
            cleanup_plan=context["plan"],
            findings=context["report"].findings,
            selected_actions=selected_actions,
            mutation_kind=mutation_kind,
            scan_options=_scan_options(args, context["platforms"]),
        )
        write_new_json(Path(args.out).expanduser(), document)
        _write_document(
            {
                "schema_version": "larj.agent-plan-summary.v1",
                "document_type": "agent_plan_summary",
                "command": "agent",
                "subcommand": "plan",
                "mode": "agent",
                "phase": "completed",
                "operation": "purge",
                "operation_id": operation_id,
                "plan_sha256": document["plan_sha256"],
                "plan_path": str(Path(args.out).expanduser().resolve()),
                "authorization_required": bool(selected_actions),
                "mutation_kind": mutation_kind,
                "selected_action_ids": [
                    str(action.action_id) for action in selected_actions
                ],
                "blockers": document["authorization"]["blockers"],
                "counts": document["counts"],
            },
            stdout,
        )
        return EXIT_OK
    except OperationStoreError as exc:
        _write_document(
            {
                "schema_version": "larj.agent-plan-summary.v1",
                "document_type": "agent_plan_summary",
                "command": "agent",
                "subcommand": "plan",
                "mode": "agent",
                "phase": "failed",
                "operation": "purge",
                "operation_id": operation_id,
                "plan_sha256": "",
                "authorization_required": False,
                "blockers": [
                    structured_blocker(
                        "plan_output_exists"
                        if Path(args.out).expanduser().exists()
                        else "plan_write_failed",
                        scope="plan_file",
                        retryable=False,
                        remediation="Choose a new --out path; plans are never overwritten.",
                        message=str(exc),
                    )
                ],
                "counts": zero_counts(),
            },
            stdout,
        )
        return EXIT_UNKNOWN


def _run_apply(
    args: argparse.Namespace,
    supplied_adapters: Iterable[FrontendAdapter] | None,
    stdout: TextIO,
    *,
    app_server_factory: AppServerFactory,
    binary_resolver: BinaryResolver,
    client_inspector: ClientInspector,
    cleanup_service: CleanupService,
) -> int:
    plan, error = _load_authorized_plan(
        Path(args.plan).expanduser(),
        str(args.authorized_plan_sha256 or ""),
    )
    if error is not None:
        _write_document(error, stdout)
        return _exit_for_result(error)
    assert plan is not None
    operation_id = str(plan["operation_id"])
    plan_hash = str(plan["plan_sha256"])
    counts = _mapping(plan.get("counts"), zero_counts())
    if not bool(args.clients_closed):
        result = result_document(
            subcommand="apply",
            operation_id=operation_id,
            plan_sha=plan_hash,
            goal_status="blocked",
            modified=False,
            mutation_started=False,
            counts=counts,
            blockers=[
                structured_blocker(
                    "clients_closed_ack_required",
                    scope="target_store",
                    retryable=True,
                    remediation="Close the target clients and pass --clients-closed.",
                )
            ],
            phase="preflight",
        )
        _write_document(result, stdout)
        return EXIT_BLOCKED

    target = _mapping(plan.get("target"), {})
    target_home = Path(str(target.get("codex_home") or ""))
    store = OperationStore(target_home, operation_id)
    try:
        store.accept_plan(plan)
        existing_result = store.read_result()
        if existing_result is not None:
            _write_document(existing_result, stdout)
            return _exit_for_result(existing_result)
        existing_state = store.read_state()
        if existing_state and bool(existing_state.get("mutation_started")):
            result = result_document(
                subcommand="apply",
                operation_id=operation_id,
                plan_sha=plan_hash,
                goal_status="unknown",
                modified=bool(existing_state.get("modified", False)),
                mutation_started=True,
                counts=counts,
                blockers=[
                    structured_blocker(
                        "mutation_outcome_unknown",
                        scope="operation",
                        remediation=(
                            f"Run agent verify --operation-id {operation_id} "
                            f"--codex-home {target_home} before any new plan."
                        ),
                    )
                ],
                phase="recovery_required",
            )
            _write_document(result, stdout)
            return EXIT_UNKNOWN
        if (
            existing_state
            and existing_state.get("phase") == "executing"
            and not store.lock_exists()
        ):
            result = result_document(
                subcommand="apply",
                operation_id=operation_id,
                plan_sha=plan_hash,
                goal_status="blocked",
                modified=False,
                mutation_started=False,
                counts=counts,
                blockers=[
                    structured_blocker(
                        "execution_attempt_aborted",
                        scope="operation",
                        retryable=False,
                        remediation=(
                            "Create a fresh plan; this operation stopped before "
                            "a durable mutation checkpoint and will not be reused."
                        ),
                    )
                ],
                phase="blocked",
            )
            try:
                store.write_result(result)
            except OperationStoreError:
                pass
            _write_document(result, stdout)
            return EXIT_BLOCKED
        with store.mutation_lock():
            return _apply_locked(
                args,
                plan,
                store,
                supplied_adapters,
                stdout,
                app_server_factory=app_server_factory,
                binary_resolver=binary_resolver,
                client_inspector=client_inspector,
                cleanup_service=cleanup_service,
            )
    except OperationLockedError as exc:
        result = result_document(
            subcommand="apply",
            operation_id=operation_id,
            plan_sha=plan_hash,
            goal_status="unknown",
            modified=False,
            mutation_started=bool((store.read_state() or {}).get("mutation_started")),
            counts=counts,
            blockers=[
                structured_blocker(
                    "operation_locked",
                    scope="operation",
                    remediation="Inspect agent status; never delete a stale lock blindly.",
                    message=str(exc),
                )
            ],
            phase="recovery_required",
        )
        _write_document(result, stdout)
        return EXIT_UNKNOWN
    except OperationStoreError as exc:
        mutation_started = _safe_mutation_started(store)
        result = result_document(
            subcommand="apply",
            operation_id=operation_id,
            plan_sha=plan_hash,
            goal_status="unknown" if mutation_started else "blocked",
            modified=False,
            mutation_started=mutation_started,
            counts=counts,
            blockers=[
                structured_blocker(
                    (
                        "operation_persistence_failed"
                        if mutation_started
                        else "operation_plan_conflict"
                    ),
                    scope="operation",
                    retryable=False,
                    remediation=(
                        "Run agent verify; never repeat apply."
                        if mutation_started
                        else "Use the operation ID only with its original authorized plan."
                    ),
                    message=str(exc),
                )
            ],
            phase="recovery_required" if mutation_started else "preflight",
        )
        _write_document(result, stdout)
        return EXIT_UNKNOWN if mutation_started else EXIT_BLOCKED
    except Exception as exc:
        mutation_started = _safe_mutation_started(store)
        result = result_document(
            subcommand="apply",
            operation_id=operation_id,
            plan_sha=plan_hash,
            goal_status="unknown",
            modified=False,
            mutation_started=mutation_started,
            counts=counts,
            blockers=[
                structured_blocker(
                    "apply_failed_unexpectedly",
                    scope="operation",
                    retryable=False,
                    remediation=(
                        "Run agent verify before any further mutation."
                        if mutation_started
                        else "Inspect the error and create a new plan."
                    ),
                    message=str(exc),
                )
            ],
            phase="recovery_required" if mutation_started else "failed",
        )
        _write_document(result, stdout)
        return EXIT_UNKNOWN


def _apply_locked(
    args: argparse.Namespace,
    plan: Mapping[str, Any],
    store: OperationStore,
    supplied_adapters: Iterable[FrontendAdapter] | None,
    stdout: TextIO,
    *,
    app_server_factory: AppServerFactory,
    binary_resolver: BinaryResolver,
    client_inspector: ClientInspector,
    cleanup_service: CleanupService,
) -> int:
    operation_id = str(plan["operation_id"])
    plan_hash = str(plan["plan_sha256"])
    counts = _mapping(plan.get("counts"), zero_counts())
    authorization = _mapping(plan.get("authorization"), {})
    frozen_actions = authorization.get("root_actions")
    if not isinstance(frozen_actions, list):
        raise OperationStoreError("Authorized root action list is invalid")
    state = {
        "schema_version": "larj.agent-state.v1",
        "operation_id": operation_id,
        "plan_sha256": plan_hash,
        "phase": "preflight",
        "goal_status": "unknown",
        "goal_satisfied": False,
        "modified": False,
        "mutation_started": False,
        "completed_action_ids": [],
        "current_action_ids": [],
        "current_action_state": "not_started",
        "next_event_sequence": 1,
        "updated_at": utc_now(),
    }
    store.write_state(state)
    store.append_event({"event": "plan_accepted", "plan_sha256": plan_hash})

    context_args = _args_from_scan_options(plan)
    context = _scan_context(
        context_args,
        supplied_adapters,
        cleanup_service=cleanup_service,
    )
    target_home = Path(str(_mapping(plan.get("target"), {}).get("codex_home") or ""))
    storage = _target_storage(
        context["plan"], target_home, context["active_adapters"]
    )
    preflight_blockers: list[dict[str, Any]] = []
    if storage is None or str(storage.storage_id) != str(
        _mapping(plan.get("target"), {}).get("storage_id")
    ):
        preflight_blockers.append(
            structured_blocker(
                "target_store_mismatch",
                scope="target_store",
                retryable=False,
                remediation="Create a new plan for the currently discovered target store.",
            )
        )
    if not context["plan"].scan_complete:
        preflight_blockers.append(
            structured_blocker(
                "scan_incomplete",
                scope="target_store",
                remediation="Resolve scan errors and create a new plan.",
                message="; ".join(context["plan"].errors),
            )
        )
    if str(context["plan"].plan_fingerprint) != str(
        _mapping(plan.get("scan"), {}).get("cleanup_plan_fingerprint")
    ):
        preflight_blockers.append(
            structured_blocker(
                "plan_changed",
                scope="target_store",
                remediation="Create and authorize a new plan from a fresh scan.",
            )
        )
    fresh_by_id = {
        str(action.action_id): action for action in context["plan"].actions
    }
    selected_actions: list[Any] = []
    for raw in frozen_actions:
        if not isinstance(raw, Mapping):
            preflight_blockers.append(
                structured_blocker(
                    "plan_schema_invalid",
                    scope="plan",
                    retryable=False,
                    remediation="Discard this plan and generate a new one.",
                )
            )
            continue
        action_id = str(raw.get("action_id") or "")
        fresh = fresh_by_id.get(action_id)
        if fresh is None or action_binding(fresh) != dict(raw):
            preflight_blockers.append(
                structured_blocker(
                    "target_state_changed",
                    scope=f"action:{action_id}",
                    remediation="Create and authorize a new plan from current state.",
                    action_id=action_id,
                )
            )
        else:
            selected_actions.append(fresh)
    try:
        running = client_inspector(target_home)
    except DesktopStateError as exc:
        running = ()
        preflight_blockers.append(
            structured_blocker(
                "client_check_failed",
                scope="target_store",
                remediation="Restore reliable process inspection before applying.",
                message=str(exc),
            )
        )
    if running:
        preflight_blockers.append(
            structured_blocker(
                "target_client_running",
                scope="target_store",
                remediation="Close the listed target-store clients and create a fresh plan.",
                message=", ".join(running),
            )
        )
    if preflight_blockers:
        unknown_preflight_codes = {
            "client_check_failed",
            "scan_incomplete",
            "target_store_mismatch",
        }
        blocker_codes = {
            str(blocker.get("blocker_code") or "")
            for blocker in preflight_blockers
        }
        goal = (
            "unknown"
            if blocker_codes & unknown_preflight_codes
            else "blocked"
        )
        result = result_document(
            subcommand="apply",
            operation_id=operation_id,
            plan_sha=plan_hash,
            goal_status=goal,
            modified=False,
            mutation_started=False,
            counts=counts,
            blockers=preflight_blockers,
            phase="preflight",
        )
        state.update(
            {
                "phase": "preflight" if goal == "unknown" else "blocked",
                "goal_status": goal,
            }
        )
        store.write_state(state)
        store.append_event(
            {
                "event": (
                    "preflight_failed" if goal == "unknown" else "preflight_blocked"
                ),
                "goal_status": goal,
                "blockers": preflight_blockers,
            }
        )
        store.write_result(result)
        _write_document(result, stdout)
        return _exit_for_result(result)

    if not frozen_actions:
        blockers = authorization.get("blockers")
        blocker_list = (
            [dict(value) for value in blockers]
            if isinstance(blockers, list)
            else []
        )
        verification: dict[str, Any] | None = None
        final_scope: dict[str, Any] | None = None
        verification_error: str | None = None
        final_scope_error: str | None = None
        verification_attempts = 0
        if blocker_list:
            goal = "blocked"
            result_blockers = blocker_list
        else:
            (
                verification,
                final_scope,
                verification_error,
                final_scope_error,
                verification_attempts,
            ) = _verify_with_retry(
                plan,
                supplied_adapters,
                total_timeout=float(args.verify_timeout),
                retry_pending=False,
                cleanup_service=cleanup_service,
            )
            if (
                verification is not None
                and final_scope is not None
                and verification.get("all_satisfied") is True
                and final_scope.get("all_satisfied") is True
            ):
                goal = "complete"
                result_blockers = []
            elif (
                verification is not None
                and final_scope is not None
                and final_scope.get("scan_complete") is True
            ):
                goal = "blocked"
                result_blockers = [
                    structured_blocker(
                        "target_scope_residuals_remain",
                        scope="operation",
                        remediation=(
                            "Create and authorize a fresh plan for the newly "
                            "observed target-scope problems."
                        ),
                    )
                ]
            else:
                goal = "unknown"
                result_blockers = [
                    structured_blocker(
                        "verification_scan_incomplete",
                        scope="operation",
                        remediation=(
                            "Resolve scan errors and create a fresh plan; no "
                            "mutation was attempted."
                        ),
                        message=verification_error or final_scope_error,
                    )
                ]
        result = result_document(
            subcommand="apply",
            operation_id=operation_id,
            plan_sha=plan_hash,
            goal_status=goal,
            modified=False,
            mutation_started=False,
            counts=counts,
            blockers=result_blockers,
            phase="preflight" if goal == "unknown" else "finished",
            details={
                "verification": verification,
                "final_scope_verification": final_scope,
                "verification_attempts": verification_attempts,
            },
        )
        state.update(
            {
                "phase": "preflight" if goal == "unknown" else "finished",
                "goal_status": goal,
                "goal_satisfied": goal == "complete",
            }
        )
        store.write_state(state)
        store.append_event(
            {"event": "operation_finished", "goal_status": goal}
        )
        store.write_result(result)
        _write_document(result, stdout)
        return _exit_for_result(result)

    selected_action_ids = [
        str(action.action_id) for action in selected_actions
    ]
    state.update(
        {
            "phase": "executing",
            "current_action_ids": selected_action_ids,
            "current_action_state": "guard_pending",
            "action_states": {
                action_id: {
                    "state": "not_started",
                    "updated_at": utc_now(),
                }
                for action_id in selected_action_ids
            },
        }
    )
    store.append_event(
        {
            "event": "execution_started",
            "action_ids": selected_action_ids,
            "mutation_kind": authorization.get("mutation_kind"),
        },
        state_updates=state,
    )
    state.clear()
    state.update(store.read_state() or {})

    def record_action_state(
        checkpoint: str,
        action: Any,
        action_result: Any | None,
    ) -> None:
        action_id = str(action.action_id)
        if action_id not in selected_action_ids:
            raise OperationStoreError(
                "Execution checkpoint is not bound to the authorized plan"
            )
        current = store.read_state() or dict(state)
        action_states = dict(current.get("action_states") or {})
        result_status = (
            str(getattr(action_result, "status", "")) or None
        )
        action_states[action_id] = {
            "state": checkpoint,
            "result_status": result_status,
            "updated_at": utc_now(),
        }
        mutation_started = bool(current.get("mutation_started")) or (
            checkpoint == "mutation_started"
        )
        completed = list(current.get("completed_action_ids") or [])
        if (
            checkpoint == "verified"
            and (action_result is None or result_status == "deleted")
            and action_id not in completed
        ):
            completed.append(action_id)
        current.update(
            {
                "phase": "executing",
                "mutation_started": mutation_started,
                "current_action_ids": [action_id],
                "current_action_state": checkpoint,
                "completed_action_ids": completed,
                "action_states": action_states,
                "modified": bool(current.get("modified")) or bool(completed),
                "updated_at": utc_now(),
            }
        )
        event_name = {
            "guard_started": "action_guard_started",
            "mutation_started": "mutation_started",
            "verified": "action_verified",
        }.get(checkpoint)
        if event_name is None:
            raise OperationStoreError(
                f"Unsupported action checkpoint: {checkpoint}"
            )
        event: dict[str, Any] = {
            "event": event_name,
            "action_id": action_id,
            "action_state": checkpoint,
        }
        if result_status is not None:
            event["result_status"] = result_status
        # The irreversible request is made only after this event and its
        # corresponding state update are durable.
        store.append_event(event, state_updates=current)
        state.clear()
        state.update(store.read_state() or current)

    execution_error: str | None = None
    execution_result: dict[str, Any] | None = None
    try:
        outcome = cleanup_service.execute(
            context["cleanup_context"],
            selected_actions,
            timeout=float(args.timeout),
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
            action_state_callback=record_action_state,
        )
        exit_code = EXIT_OK if outcome.ok else EXIT_UNKNOWN
        execution_result = outcome.audit_payload()
    except Exception as exc:
        exit_code = EXIT_UNKNOWN
        execution_error = str(exc) or repr(exc)
    (
        verification,
        final_scope,
        verification_error,
        final_scope_error,
        verification_attempts,
    ) = _verify_with_retry(
        plan,
        supplied_adapters,
        total_timeout=float(args.verify_timeout),
        retry_pending=True,
        cleanup_service=cleanup_service,
    )

    exact_satisfied = bool(
        verification is not None and verification["all_satisfied"]
    )
    full_scope_satisfied = bool(
        final_scope is not None and final_scope["all_satisfied"]
    )
    mutation_started = bool(state.get("mutation_started", False))
    if exact_satisfied and full_scope_satisfied:
        goal_status = "complete"
        modified = bool(verification["verified_action_ids"])
        blockers: list[dict[str, Any]] = []
    elif (
        exact_satisfied
        and final_scope is not None
        and bool(final_scope.get("scan_complete"))
    ):
        goal_status = "completed_with_residuals"
        modified = bool(verification["verified_action_ids"])
        blockers = [
            structured_blocker(
                "target_scope_residuals_remain",
                scope="operation",
                remediation=(
                    "Inspect remaining target-scope problems and create a new "
                    "immutable plan; do not widen the completed plan."
                ),
            )
        ]
    else:
        goal_status = "unknown"
        modified = bool(
            verification and verification.get("verified_action_ids")
        )
        blockers = [
            structured_blocker(
                "mutation_outcome_unknown",
                scope="operation",
                remediation=(
                    f"Run agent verify --operation-id {operation_id} "
                    f"--codex-home {target_home}; never repeat apply blindly."
                ),
                message=(
                    verification_error
                    or final_scope_error
                    or execution_error
                ),
            )
        ]
    result = result_document(
        subcommand="apply",
        operation_id=operation_id,
        plan_sha=plan_hash,
        goal_status=goal_status,
        modified=modified,
        mutation_started=mutation_started,
        counts=counts,
        blockers=blockers,
        phase="recovery_required" if goal_status == "unknown" else "finished",
        details={
            "execution_exit_code": exit_code,
            "execution_error": execution_error,
            "execution_result": execution_result,
            "verification": verification,
            "final_scope_verification": final_scope,
            "verification_attempts": verification_attempts,
        },
    )
    state.update(
        {
            "phase": "finished" if goal_status != "unknown" else "recovery_required",
            "goal_status": goal_status,
            "goal_satisfied": goal_status == "complete",
            "modified": modified,
            "current_action_state": (
                "verified" if goal_status == "complete" else "outcome_unknown"
            ),
            "completed_action_ids": (
                verification.get("verified_action_ids", [])
                if verification is not None
                else []
            ),
        }
    )
    store.write_state(state)
    store.append_event(
        {
            "event": "operation_finished",
            "goal_status": goal_status,
            "verified_action_ids": state["completed_action_ids"],
            "execution_result": execution_result,
        }
    )
    store.write_result(result)
    _write_document(result, stdout)
    return _exit_for_result(result)


def _run_status(args: argparse.Namespace, stdout: TextIO) -> int:
    try:
        store = OperationStore(_status_home(args), str(args.operation_id))
        if store.lock_exists():
            plan = store.read_plan()
            state = store.read_state() or {}
            document = result_document(
                subcommand="status",
                operation_id=store.operation_id,
                plan_sha=str(plan.get("plan_sha256") or ""),
                goal_status="unknown",
                modified=bool(state.get("modified", False)),
                mutation_started=bool(state.get("mutation_started", False)),
                blockers=[
                    structured_blocker(
                        "operation_locked",
                        scope="operation",
                        remediation="Wait for apply/verify to finish; never delete the lock blindly.",
                    )
                ],
                phase="recovery_required",
            )
            _write_document(document, stdout)
            return EXIT_UNKNOWN
        result = store.read_result()
        if result is not None:
            document = dict(result)
            document["source_subcommand"] = document.get("subcommand")
            document["subcommand"] = "status"
            _write_document(document, stdout)
            return _exit_for_result(document)
        state = store.read_state()
        if state is None:
            raise OperationStoreError("Operation was not found")
        document = result_document(
            subcommand="status",
            operation_id=store.operation_id,
            plan_sha=str(state.get("plan_sha256") or ""),
            goal_status=str(state.get("goal_status") or "unknown"),
            modified=bool(state.get("modified", False)),
            mutation_started=bool(state.get("mutation_started", False)),
            blockers=[
                structured_blocker(
                    "operation_incomplete",
                    scope="operation",
                    remediation="Run agent verify before considering any new mutation.",
                )
            ],
            phase=str(state.get("phase") or "unknown"),
            details={"state": state},
        )
        _write_document(document, stdout)
        return _exit_for_result(document)
    except OperationStoreError as exc:
        document = result_document(
            subcommand="status",
            operation_id=str(args.operation_id),
            plan_sha="",
            goal_status="unknown",
            modified=False,
            mutation_started=False,
            blockers=[
                structured_blocker(
                    "operation_not_found",
                    scope="operation",
                    retryable=False,
                    remediation="Use an operation ID from agent plan/apply.",
                    message=str(exc),
                )
            ],
            phase="failed",
        )
        _write_document(document, stdout)
        return EXIT_UNKNOWN


def _run_verify(
    args: argparse.Namespace,
    supplied_adapters: Iterable[FrontendAdapter] | None,
    stdout: TextIO,
    *,
    cleanup_service: CleanupService,
) -> int:
    store: OperationStore | None = None
    try:
        store = OperationStore(_status_home(args), str(args.operation_id))
        with store.mutation_lock():
            plan = store.read_plan()
            state = store.read_state() or {}
            (
                verification,
                final_scope,
                verification_error,
                final_scope_error,
                verification_attempts,
            ) = _verify_with_retry(
                plan,
                supplied_adapters,
                total_timeout=float(args.verify_timeout),
                retry_pending=bool(state.get("mutation_started", False)),
                cleanup_service=cleanup_service,
            )
            mutation_started = bool(state.get("mutation_started", False))
            if (
                verification is not None
                and final_scope is not None
                and verification["all_satisfied"]
                and final_scope["all_satisfied"]
            ):
                goal_status = "complete"
                modified = mutation_started and bool(verification["verified_action_ids"])
                blockers: list[dict[str, Any]] = []
            elif (
                verification is not None
                and final_scope is not None
                and bool(final_scope.get("scan_complete"))
            ):
                goal_status = (
                    "completed_with_residuals" if mutation_started else "blocked"
                )
                modified = bool(state.get("modified", False))
                blockers = [
                    structured_blocker(
                        "target_scope_residuals_remain",
                        scope="operation",
                        remediation=(
                            "Review exact and target-scope residuals before creating "
                            "a new immutable plan."
                        ),
                    )
                ]
            else:
                goal_status = "unknown"
                modified = bool(state.get("modified", False))
                blockers = [
                    structured_blocker(
                        "verification_scan_incomplete",
                        scope="operation",
                        remediation="Resolve scan errors and run verify again; never repeat apply.",
                        message=verification_error or final_scope_error,
                    )
                ]
            result = result_document(
                subcommand="verify",
                operation_id=store.operation_id,
                plan_sha=str(plan.get("plan_sha256") or ""),
                goal_status=goal_status,
                modified=modified,
                mutation_started=mutation_started,
                counts=_mapping(plan.get("counts"), zero_counts()),
                blockers=blockers,
                phase=(
                    "recovery_required" if goal_status == "unknown" else "finished"
                ),
                details={
                    "verification": verification,
                    "final_scope_verification": final_scope,
                    "verification_attempts": verification_attempts,
                },
            )
            updated = dict(state)
            updated.update(
                {
                    "schema_version": "larj.agent-state.v1",
                    "operation_id": store.operation_id,
                    "plan_sha256": plan.get("plan_sha256"),
                    "phase": (
                        "recovery_required"
                        if goal_status == "unknown"
                        else "finished"
                    ),
                    "goal_status": goal_status,
                    "goal_satisfied": goal_status == "complete",
                    "modified": modified,
                    "mutation_started": mutation_started,
                    "updated_at": utc_now(),
                }
            )
            updated.setdefault("next_event_sequence", 1)
            store.write_state(updated)
            store.append_event(
                {
                    "event": "verification_finished",
                    "goal_status": goal_status,
                    "verified_action_ids": (
                        verification.get("verified_action_ids", [])
                        if verification is not None
                        else []
                    ),
                }
            )
            store.write_result(result)
            _write_document(result, stdout)
            return _exit_for_result(result)
    except Exception as exc:
        state: Mapping[str, Any] = {}
        plan_sha = ""
        if store is not None:
            try:
                state = store.read_state() or {}
            except OperationStoreError:
                pass
            try:
                plan_sha = str(store.read_plan().get("plan_sha256") or "")
            except OperationStoreError:
                pass
        document = result_document(
            subcommand="verify",
            operation_id=str(args.operation_id),
            plan_sha=plan_sha,
            goal_status="unknown",
            modified=bool(state.get("modified", False)),
            mutation_started=bool(state.get("mutation_started", False)),
            blockers=[
                structured_blocker(
                    "verification_failed",
                    scope="operation",
                    remediation="Resolve the read error; do not repeat apply.",
                    message=str(exc),
                )
            ],
            phase="recovery_required",
        )
        _write_document(document, stdout)
        return EXIT_UNKNOWN


def _scan_context(
    args: argparse.Namespace,
    supplied_adapters: Iterable[FrontendAdapter] | None,
    *,
    cleanup_service: CleanupService,
) -> dict[str, Any]:
    from .adapter_factory import create_default_adapters

    platforms = _effective_platforms(getattr(args, "platform", None))
    adapter_builder: Callable[[], Sequence[FrontendAdapter]] | None = None
    if supplied_adapters is not None:
        active_adapters = list(supplied_adapters)
    else:
        guard_args = argparse.Namespace(**vars(args))
        guard_args.platform = ["all"]
        adapter_builder = lambda: create_default_adapters(guard_args)
        active_adapters = list(adapter_builder())
    prepared = cleanup_service.prepare(
        active_adapters,
        platforms=platforms,
        adapter_builder=adapter_builder,
    )
    result = prepared.legacy_dict()
    result["cleanup_context"] = prepared
    return result


def _verify_full_target_scope(
    plan: Mapping[str, Any],
    supplied_adapters: Iterable[FrontendAdapter] | None,
    *,
    cleanup_service: CleanupService,
) -> dict[str, Any]:
    context = _scan_context(
        _args_from_scan_options(plan),
        supplied_adapters,
        cleanup_service=cleanup_service,
    )
    target = _mapping(plan.get("target"), {})
    target_home = Path(str(target.get("codex_home") or ""))
    storage = _target_storage(
        context["plan"], target_home, context["active_adapters"]
    )
    if storage is None or str(storage.storage_id) != str(target.get("storage_id")):
        raise OperationStoreError("Target store identity changed during verification")
    target_key = normalize_storage_path(target_home)
    findings = [
        finding
        for finding in context["report"].findings
        if normalize_storage_path(finding.codex_home) == target_key
    ]
    actions = [
        action
        for action in context["plan"].actions
        if str(action.target.storage_id) == str(storage.storage_id)
    ]
    problem_actions = [
        action for action in actions if enum_value(action.kind) != "keep"
    ]
    counts = plan_counts(
        findings=findings,
        actions=actions,
    )
    return {
        "scan_complete": bool(context["plan"].scan_complete),
        "all_satisfied": (
            bool(context["plan"].scan_complete)
            and not findings
            and not problem_actions
        ),
        "remaining_finding_count": len(findings),
        "remaining_problem_action_count": len(problem_actions),
        "remaining_action_ids": [
            str(action.action_id) for action in problem_actions
        ],
        "counts": counts,
        "scan_errors": [str(value) for value in context["plan"].errors],
    }


def _verify_with_retry(
    plan: Mapping[str, Any],
    supplied_adapters: Iterable[FrontendAdapter] | None,
    *,
    total_timeout: float,
    retry_pending: bool,
    cleanup_service: CleanupService | None = None,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    str | None,
    str | None,
    int,
]:
    """Retry exact artifact checks, then perform one terminal full scan.

    The full target snapshot is deliberately not retried inside ``apply``:
    one apply has one complete preflight snapshot and one complete terminal
    snapshot.  If that terminal read is inconclusive, ``agent verify`` is the
    recovery path and the mutation is never repeated.
    """

    service = cleanup_service or CleanupService()
    deadline = time.monotonic() + max(0.0, total_timeout)
    delay = 0.25
    attempts = 0
    verification: dict[str, Any] | None = None
    final_scope: dict[str, Any] | None = None
    verification_error: str | None = None
    final_scope_error: str | None = None
    while True:
        attempts += 1
        verification = None
        verification_error = None
        try:
            verification = verify_frozen_actions(plan)
        except Exception as exc:
            verification_error = str(exc) or repr(exc)
        exact_satisfied = bool(
            verification is not None and verification.get("all_satisfied")
        )
        if exact_satisfied:
            break
        if not retry_pending or time.monotonic() >= deadline:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        delay = min(delay * 2.0, 5.0)

    try:
        final_scope = _verify_full_target_scope(
            plan,
            supplied_adapters,
            cleanup_service=service,
        )
    except Exception as exc:
        final_scope_error = str(exc) or repr(exc)
    return (
        verification,
        final_scope,
        verification_error,
        final_scope_error,
        attempts,
    )


def _execution_audit_payload(raw: str) -> dict[str, Any] | None:
    """Retain mutation/backup evidence without persisting conversation text."""

    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"structured_output": False}
    if not isinstance(value, Mapping):
        return {"structured_output": False}
    result: dict[str, Any] = {
        key: value[key]
        for key in (
            "command",
            "status",
            "mutation_kind",
            "action_id",
            "selected_action_ids",
            "succeeded",
            "failed",
            "blocked",
            "error",
        )
        if key in value
    }
    for key in ("repair", "result"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            result[key] = dict(nested)
    raw_results = value.get("results")
    if isinstance(raw_results, list):
        result["results"] = [
            {
                key: item[key]
                for key in (
                    "action_id",
                    "status",
                    "request_error",
                    "error",
                    "remaining_artifacts",
                    "impacted_thread_ids",
                )
                if key in item
            }
            for item in raw_results
            if isinstance(item, Mapping)
        ]
    result["structured_output"] = True
    return result


def _next_frozen_batch(actions: Sequence[Any]) -> tuple[str | None, list[Any]]:
    executable = [
        action
        for action in actions
        if bool(getattr(action, "executable", False))
        and action_capability(action.kind).implemented
        and action_capability(action.kind).mutation_family is not None
    ]
    families = tuple(
        dict.fromkeys(
            capability.mutation_family
            for capability in ACTION_REGISTRY.values()
            if capability.implemented and capability.mutation_family is not None
        )
    )
    for family in families:
        matches = [
            action
            for action in executable
            if action_capability(action.kind).mutation_family == family
        ]
        if not matches:
            continue
        if family == "repair_legacy_index":
            matches = [min(matches, key=lambda value: str(value.action_id))]
        return family, sorted(matches, key=lambda value: str(value.action_id))
    return None, []


def _load_authorized_plan(
    path: Path,
    authorized_hash: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not authorized_hash:
        return None, result_document(
            subcommand="apply",
            operation_id="unaccepted",
            plan_sha="",
            goal_status="blocked",
            modified=False,
            mutation_started=False,
            blockers=[
                structured_blocker(
                    "authorization_hash_required",
                    scope="plan",
                    retryable=True,
                    remediation="Pass the exact plan_sha256 as --authorized-plan-sha256.",
                )
            ],
            phase="preflight",
        )
    try:
        value = strict_json_load(path)
    except OperationStoreError as exc:
        return None, result_document(
            subcommand="apply",
            operation_id="unaccepted",
            plan_sha="",
            goal_status="unknown",
            modified=False,
            mutation_started=False,
            blockers=[
                structured_blocker(
                    "plan_parse_failed",
                    scope="plan",
                    retryable=False,
                    remediation="Generate a new plan; do not repair an untrusted plan in place.",
                    message=str(exc),
                )
            ],
            phase="preflight",
        )
    if not isinstance(value, dict) or value.get("schema_version") != PLAN_SCHEMA:
        return None, result_document(
            subcommand="apply",
            operation_id="unaccepted",
            plan_sha="",
            goal_status="unknown",
            modified=False,
            mutation_started=False,
            blockers=[
                structured_blocker(
                    "plan_schema_invalid",
                    scope="plan",
                    retryable=False,
                    remediation="Generate a new plan with this janitor version.",
                )
            ],
            phase="preflight",
        )
    embedded_hash = str(value.get("plan_sha256") or "")
    calculated = plan_sha256(value)
    if embedded_hash != calculated:
        return None, result_document(
            subcommand="apply",
            operation_id="unaccepted",
            plan_sha=embedded_hash,
            goal_status="blocked",
            modified=False,
            mutation_started=False,
            blockers=[
                structured_blocker(
                    "plan_integrity_failed",
                    scope="plan",
                    retryable=False,
                    remediation="Discard the modified plan and generate a new one.",
                )
            ],
            phase="preflight",
        )
    if authorized_hash != embedded_hash:
        return None, result_document(
            subcommand="apply",
            operation_id="unaccepted",
            plan_sha=embedded_hash,
            goal_status="blocked",
            modified=False,
            mutation_started=False,
            blockers=[
                structured_blocker(
                    "authorization_hash_mismatch",
                    scope="plan",
                    retryable=True,
                    remediation="Review the plan and pass its exact plan_sha256.",
                )
            ],
            phase="preflight",
        )
    try:
        if (
            value.get("document_type") != "agent_cleanup_plan"
            or value.get("mode") != "agent"
            or value.get("operation") != "purge"
        ):
            raise ValueError("plan document type, mode, or operation is invalid")
        operation_id = str(value["operation_id"])
        target = _mapping(value["target"], {})
        target_home_text = str(target["codex_home"])
        target_home = Path(target_home_text)
        if not target_home_text or not target_home.is_absolute():
            raise ValueError("target CODEX_HOME must be an absolute path")
        if not target_home.is_dir():
            raise ValueError("target CODEX_HOME is not an existing directory")
        target_storage_id = str(target["storage_id"])
        if target_storage_id != storage_id_for_path(target_home):
            raise ValueError("target storage_id does not match target CODEX_HOME")
        platforms = target.get("platforms")
        if (
            not isinstance(platforms, list)
            or not platforms
            or len(platforms) != len(set(platforms))
            or any(
                not isinstance(platform, str)
                or platform not in {"aionui", "cindy", "native"}
                for platform in platforms
            )
        ):
            raise ValueError("target platforms are invalid")
        storage_document = target.get("storage")
        if storage_document is not None:
            if not isinstance(storage_document, Mapping):
                raise TypeError("target storage is not an object")
            storage_path = Path(str(storage_document.get("path") or ""))
            try:
                same_storage_path = storage_path.is_absolute() and os.path.samefile(
                    storage_path,
                    target_home,
                )
            except OSError:
                same_storage_path = False
            if (
                str(storage_document.get("storage_id") or "")
                != target_storage_id
                or not same_storage_path
            ):
                raise ValueError("embedded target storage identity is invalid")
        scan = value.get("scan")
        if (
            not isinstance(scan, Mapping)
            or type(scan.get("scan_complete")) is not bool
            or not isinstance(scan.get("cleanup_plan_fingerprint"), str)
            or not isinstance(scan.get("errors"), list)
        ):
            raise ValueError("scan summary is invalid")
        authorization = _mapping(value["authorization"], {})
        roots = authorization["root_actions"]
        if not isinstance(roots, list):
            raise TypeError("root_actions")
        root_ids: list[str] = []
        root_kinds: set[str] = set()
        for root in roots:
            if not isinstance(root, Mapping):
                raise TypeError("root action is not an object")
            action_id = str(root.get("action_id") or "")
            kind = str(root.get("kind") or "")
            storage_id = str(root.get("storage_id") or "")
            thread_id = str(root.get("thread_id") or "")
            snapshot = str(root.get("snapshot_fingerprint") or "")
            affected = root.get("affected_thread_ids")
            impact = root.get("impact")
            observations = root.get("observation_ids")
            capability_document = root.get("capability")
            capability = action_capability(kind)
            if (
                not action_id
                or not capability.implemented
                or capability.mutation_family is None
                or storage_id != target_storage_id
                or not thread_id
                or not re.fullmatch(r"(?:v1:)?[0-9a-f]{64}", snapshot)
                or not isinstance(affected, list)
                or any(not isinstance(value, str) or not value for value in affected)
                or thread_id not in affected
                or len(affected) != len(set(affected))
                or not isinstance(impact, Mapping)
                or (
                    list(impact.get("affected_thread_ids") or [thread_id])
                    != affected
                )
                or not isinstance(observations, list)
                or any(
                    not isinstance(value, str) or not value
                    for value in observations
                )
                or len(observations) != len(set(observations))
                or not isinstance(capability_document, Mapping)
                or dict(capability_document) != capability.to_dict()
            ):
                raise ValueError("root action binding is invalid")
            root_ids.append(action_id)
            root_kinds.add(str(capability.mutation_family))
        if len(set(root_ids)) != len(roots):
            raise ValueError("duplicate action IDs")
        if len(root_kinds) > 1 or (
            root_kinds
            and authorization.get("mutation_kind") not in root_kinds
        ):
            raise ValueError("root actions mix mutation families")
        if not root_kinds and authorization.get("mutation_kind") is not None:
            raise ValueError("empty plan has a mutation family")
        if authorization.get("authorization_required") is not bool(roots):
            raise ValueError("authorization_required does not match root actions")
        blockers = authorization.get("blockers")
        if not isinstance(blockers, list) or any(
            not isinstance(blocker, Mapping)
            or not isinstance(blocker.get("blocker_code"), str)
            for blocker in blockers
        ):
            raise ValueError("authorization blockers are invalid")
        counts = value.get("counts")
        if not isinstance(counts, Mapping) or any(
            type(counts.get(name)) is not int or counts[name] < 0
            for name in zero_counts()
        ):
            raise ValueError("plan counts are invalid")
        scan_options = value.get("scan_options")
        if not isinstance(scan_options, Mapping):
            raise ValueError("scan_options is invalid")
        option_platforms = scan_options.get("platforms")
        if option_platforms != platforms:
            raise ValueError("scan_options platforms do not match target platforms")
        option_home = scan_options.get("codex_home")
        if option_home is not None:
            try:
                same_option_home = Path(str(option_home)).is_absolute() and os.path.samefile(
                    Path(str(option_home)),
                    target_home,
                )
            except OSError:
                same_option_home = False
            if not same_option_home:
                raise ValueError("scan_options codex_home changes the target")
        OperationStore(target_home, operation_id)
    except (KeyError, TypeError, ValueError, OperationStoreError) as exc:
        return None, result_document(
            subcommand="apply",
            operation_id="unaccepted",
            plan_sha=embedded_hash,
            goal_status="unknown",
            modified=False,
            mutation_started=False,
            blockers=[
                structured_blocker(
                    "plan_schema_invalid",
                    scope="plan",
                    retryable=False,
                    remediation="Generate a new plan; this plan cannot be trusted.",
                    message=str(exc),
                )
            ],
            phase="preflight",
        )
    return value, None


def _args_from_scan_options(plan: Mapping[str, Any]) -> argparse.Namespace:
    options = _mapping(plan.get("scan_options"), {})
    defaults: dict[str, Any] = {
        "platform": list(options.get("platforms") or ["native"]),
        "appdata": _optional_path(options.get("appdata")),
        "codex_home": _optional_path(options.get("codex_home")),
        "codex_bin": _optional_path(options.get("codex_bin")),
        "aionui_db": _optional_path(options.get("aionui_db")),
        "aionui_codex_home": _optional_path(options.get("aionui_codex_home")),
        "cindy_root": _optional_path(options.get("cindy_root")),
        "cindy_db": _optional_path(options.get("cindy_db")),
        "cindy_codex_home": _optional_path(options.get("cindy_codex_home")),
        "thread_id": [],
        "json": True,
        "limit": 0,
    }
    return argparse.Namespace(**defaults)


def _scan_options(args: argparse.Namespace, platforms: Sequence[str]) -> dict[str, Any]:
    return {
        "platforms": list(platforms),
        **{
            name: str(value) if value is not None else None
            for name in (
                "appdata",
                "codex_home",
                "codex_bin",
                "aionui_db",
                "aionui_codex_home",
                "cindy_root",
                "cindy_db",
                "cindy_codex_home",
            )
            if (value := getattr(args, name, None)) is not None
        },
    }


def _target_storage(
    plan: Any,
    target_home: Path,
    active_adapters: Sequence[FrontendAdapter] = (),
) -> Any | None:
    target_key = normalize_storage_path(target_home)
    matches = [
        storage
        for storage in plan.storages
        if normalize_storage_path(storage.path) == target_key
    ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    adapter_matches = [
        adapter
        for adapter in active_adapters
        if normalize_storage_path(adapter.codex_home) == target_key
    ]
    if not adapter_matches:
        return None
    bin_hints = {
        str(value)
        for adapter in adapter_matches
        if (value := getattr(adapter, "codex_bin_hint", None)) is not None
    }
    if len(bin_hints) > 1:
        return None
    return StorageLocation(
        storage_id=storage_id_for_path(target_home),
        label=f"{target_home.name or 'Codex'} data directory",
        path=target_home,
        codex_bin_hint=(Path(next(iter(bin_hints))) if bin_hints else None),
        scan_status=ScanStatus.OK,
    )


def _target_home(args: argparse.Namespace) -> Path:
    return (getattr(args, "codex_home", None) or default_codex_home()).expanduser().resolve()


def _status_home(args: argparse.Namespace) -> Path:
    return (getattr(args, "codex_home", None) or default_codex_home()).expanduser().resolve()


def _effective_platforms(values: Sequence[str] | None) -> list[str]:
    if not values:
        return ["native"]
    if "all" in values:
        return ["aionui", "cindy", "native"]
    return list(dict.fromkeys(values))


def _optional_path(value: Any) -> Path | None:
    return Path(str(value)) if value else None


def _safe_mutation_started(store: OperationStore) -> bool:
    try:
        state = store.read_state()
        if state is not None:
            return bool(state.get("mutation_started", False))
    except OperationStoreError:
        pass
    try:
        if store.events_path.exists():
            return '"event":"mutation_started"' in store.events_path.read_text(
                encoding="utf-8"
            )
    except (OSError, UnicodeError):
        return True
    return False


def _mapping(value: Any, default: Mapping[str, Any]) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else dict(default)


def _write_document(document: Mapping[str, Any], output: TextIO) -> None:
    json.dump(
        dict(document),
        output,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    output.write("\n")


def _exit_for_result(document: Mapping[str, Any]) -> int:
    status = str(document.get("goal_status") or "unknown")
    if status == "complete":
        return EXIT_OK
    if status in {"blocked", "completed_with_residuals"}:
        return EXIT_BLOCKED
    return EXIT_UNKNOWN


__all__ = ["run_agent_command"]
