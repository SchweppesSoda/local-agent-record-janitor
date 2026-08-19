from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .action_registry import action_capability
from .codex_desktop_state import (
    native_evidence_for_threads,
    remaining_desktop_state_markers,
)
from .codex_state import find_thread_rollouts, read_thread_index
from .legacy_index import inventory_legacy_index
from .operation_store import plan_sha256
from .path_identity import canonical_existing_path_key


PLAN_SCHEMA = "larj.agent-plan.v1"
RESULT_SCHEMA = "larj.agent-result.v1"


def new_operation_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"purge-{stamp}-{uuid.uuid4().hex[:12]}"


def enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def structured_blocker(
    blocker_code: str,
    *,
    scope: str,
    severity: str = "error",
    retryable: bool = True,
    remediation: str,
    message: str | None = None,
    action_id: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "blocker_code": blocker_code,
        "scope": scope,
        "severity": severity,
        "retryable": retryable,
        "remediation": remediation,
    }
    if message:
        result["message"] = message
    if action_id:
        result["action_id"] = action_id
    return result


def action_binding(action: Any) -> dict[str, Any]:
    impact = action.impact.to_dict()
    affected = tuple(
        dict.fromkeys(
            str(value)
            for value in (
                impact.get("affected_thread_ids")
                or [str(action.target.thread_id)]
            )
        )
    )
    if str(action.target.thread_id) not in affected:
        affected = (str(action.target.thread_id), *affected)
    return {
        "action_id": str(action.action_id),
        "kind": enum_value(action.kind),
        "storage_id": str(action.target.storage_id),
        "thread_id": str(action.target.thread_id),
        "snapshot_fingerprint": str(action.snapshot_fingerprint),
        "affected_thread_ids": list(affected),
        "impact": impact,
        "observation_ids": [str(value) for value in action.observation_ids],
        "capability": action_capability(action.kind).to_dict(),
    }


def action_bindings_match(frozen: Mapping[str, Any], fresh: Any) -> bool:
    return action_binding(fresh) == dict(frozen)


def plan_counts(
    *,
    findings: Sequence[Any],
    actions: Sequence[Any],
    root_actions: Sequence[Any] | None = None,
) -> dict[str, int]:
    problem_actions = [
        action for action in actions if enum_value(action.kind) != "keep"
    ]
    issue_groups = {
        (str(action.target.storage_id), str(action.target.thread_id))
        for action in problem_actions
    }
    actions_by_group: dict[tuple[str, str], list[Any]] = {}
    for action in problem_actions:
        key = (str(action.target.storage_id), str(action.target.thread_id))
        actions_by_group.setdefault(key, []).append(action)
    blocked_groups = {
        key
        for key, group_actions in actions_by_group.items()
        if not any(
            bool(getattr(action, "executable", False))
            and action_capability(action.kind).implemented
            and action_capability(action.kind).mutation_family is not None
            for action in group_actions
        )
    }
    if root_actions is not None:
        mutation_roots = list(root_actions)
    else:
        mutation_roots = [
            max(
                group_actions,
                key=lambda action: (
                    bool(getattr(action, "executable", False))
                    and action_capability(action.kind).implemented,
                    _physical_artifact_count(action),
                    len(tuple(getattr(action.impact, "affected_thread_ids", ()))),
                    str(action.action_id),
                ),
            )
            for _key, group_actions in sorted(actions_by_group.items())
        ]
    affected = {
        str(thread_id)
        for action in mutation_roots
        for thread_id in (
            (
                tuple(getattr(action.impact, "legacy_residual_thread_ids", ()))
                if enum_value(action.kind) == "repair_legacy_index"
                else tuple(getattr(action.impact, "affected_thread_ids", ()))
            )
            or (str(action.target.thread_id),)
        )
    }
    artifact_count = sum(_physical_artifact_count(action) for action in mutation_roots)
    legacy_actions = [
        action
        for action in mutation_roots
        if enum_value(action.kind) == "repair_legacy_index"
    ]
    legacy_ids = {
        str(thread_id)
        for action in legacy_actions
        for thread_id in getattr(
            action.impact, "legacy_residual_thread_ids", ()
        )
    }
    return {
        "finding_count": len(findings),
        "issue_group_count": len(issue_groups),
        "root_action_count": len(mutation_roots),
        "affected_thread_count": len(affected),
        "artifact_count": artifact_count,
        "blocked_group_count": len(blocked_groups),
        "legacy_residual_line_count": sum(
            int(getattr(action.impact, "legacy_residual_line_count", 0))
            for action in legacy_actions
        ),
        "legacy_residual_unique_thread_count": len(legacy_ids),
    }


def _physical_artifact_count(action: Any) -> int:
    impact = action.impact
    return (
        int(getattr(impact, "index_record_count", 0))
        + int(getattr(impact, "rollout_file_count", 0))
        + int(getattr(impact, "desktop_catalog_record_count", 0))
        + int(getattr(impact, "desktop_global_state_reference_count", 0))
        + int(getattr(impact, "legacy_residual_line_count", 0))
    )


def zero_counts() -> dict[str, int]:
    return {
        "finding_count": 0,
        "issue_group_count": 0,
        "root_action_count": 0,
        "affected_thread_count": 0,
        "artifact_count": 0,
        "blocked_group_count": 0,
        "legacy_residual_line_count": 0,
        "legacy_residual_unique_thread_count": 0,
    }


def build_agent_plan_document(
    *,
    operation_id: str,
    codex_home: Path,
    platforms: Sequence[str],
    storage: Any | None,
    cleanup_plan: Any,
    findings: Sequence[Any],
    selected_actions: Sequence[Any],
    mutation_kind: str | None,
    scan_options: Mapping[str, Any],
) -> dict[str, Any]:
    target_actions = [
        action
        for action in cleanup_plan.actions
        if storage is None
        or str(action.target.storage_id) == str(storage.storage_id)
    ]
    blockers: list[dict[str, Any]] = []
    if not cleanup_plan.scan_complete:
        blockers.append(
            structured_blocker(
                "scan_incomplete",
                scope="target_store",
                remediation="Fix the reported scan errors and create a new plan.",
                message="; ".join(str(value) for value in cleanup_plan.errors),
            )
        )
    if storage is None:
        blockers.append(
            structured_blocker(
                "target_store_not_found",
                scope="target_store",
                retryable=False,
                remediation="Pass the exact --codex-home for one discovered store.",
            )
        )
    executable_groups = {
        (str(action.target.storage_id), str(action.target.thread_id))
        for action in target_actions
        if enum_value(action.kind) != "keep" and bool(action.executable)
    }
    for action in target_actions:
        action_group = (
            str(action.target.storage_id),
            str(action.target.thread_id),
        )
        if (
            enum_value(action.kind) == "keep"
            or bool(action.executable)
            or action_group in executable_groups
        ):
            continue
        capability = action_capability(action.kind)
        blockers.append(
            structured_blocker(
                (
                    "action_not_implemented"
                    if not capability.implemented
                    else "action_unavailable"
                ),
                scope=f"action:{action.action_id}",
                remediation=(
                    "Use the structured action metadata to resolve the safety "
                    "condition, then create a new plan."
                ),
                message=str(action.unavailable_reason or "Action is unavailable"),
                action_id=str(action.action_id),
            )
        )

    counts = plan_counts(
        findings=findings,
        actions=target_actions,
        root_actions=selected_actions,
    )
    document: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "document_type": "agent_cleanup_plan",
        "mode": "agent",
        "operation": "purge",
        "operation_id": operation_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": {
            "platforms": list(platforms),
            "codex_home": canonical_existing_path_key(codex_home),
            "storage_id": (
                str(storage.storage_id) if storage is not None else None
            ),
            "storage": storage.to_dict() if storage is not None else None,
        },
        "scan": {
            "scan_complete": bool(cleanup_plan.scan_complete),
            "cleanup_plan_fingerprint": str(cleanup_plan.plan_fingerprint),
            "errors": [str(value) for value in cleanup_plan.errors],
        },
        "authorization": {
            "authorization_required": bool(selected_actions),
            "mutation_kind": mutation_kind,
            "root_actions": [action_binding(action) for action in selected_actions],
            "blockers": blockers,
        },
        "counts": counts,
        "scan_options": dict(scan_options),
    }
    document["plan_sha256"] = plan_sha256(document)
    return document


def verify_frozen_actions(plan: Mapping[str, Any]) -> dict[str, Any]:
    target = plan.get("target")
    authorization = plan.get("authorization")
    if not isinstance(target, Mapping) or not isinstance(authorization, Mapping):
        raise ValueError("The operation plan is missing target or authorization data")
    codex_home = Path(str(target.get("codex_home") or ""))
    roots = authorization.get("root_actions")
    if not isinstance(roots, list):
        raise ValueError("The operation plan has no root action list")

    remaining: list[dict[str, Any]] = []
    verified: list[str] = []
    for raw in roots:
        if not isinstance(raw, Mapping):
            raise ValueError("The operation plan contains an invalid root action")
        action_id = str(raw.get("action_id") or "")
        kind = str(raw.get("kind") or "")
        thread_id = str(raw.get("thread_id") or "")
        affected = tuple(
            str(value)
            for value in raw.get("affected_thread_ids", [])
            if isinstance(value, str) and value
        ) or (thread_id,)
        markers: list[str] = []
        if kind == "delete_conversation":
            indexed = read_thread_index(codex_home, affected, strict=True)
            markers.extend(f"thread-index:{value}" for value in sorted(indexed))
            for current_id in affected:
                markers.extend(
                    f"rollout:{record.path}"
                    for record in find_thread_rollouts(codex_home, current_id)
                )
        elif kind == "repair_legacy_index":
            inventory = inventory_legacy_index(codex_home)
            approved_ids = {
                str(value)
                for value in (
                    raw.get("impact", {}).get("legacy_residual_thread_ids", [])
                    if isinstance(raw.get("impact"), Mapping)
                    else []
                )
            }
            for current_id in inventory.residual_thread_ids:
                if current_id in approved_ids:
                    markers.append(f"legacy-index:{current_id}")
        elif kind == "remove_desktop_state":
            markers.extend(remaining_desktop_state_markers(codex_home, affected))
            evidence = native_evidence_for_threads(codex_home, affected)
            markers.extend(
                f"native-index:{value}"
                for value in evidence["indexed_thread_ids"]
            )
            markers.extend(
                f"native-rollout:{value}" for value in evidence["rollout_paths"]
            )
        else:
            markers.append(f"unsupported-action-kind:{kind}")
        if markers:
            remaining.append(
                {
                    "action_id": action_id,
                    "kind": kind,
                    "thread_id": thread_id,
                    "remaining_artifacts": sorted(set(markers)),
                }
            )
        else:
            verified.append(action_id)
    return {
        "verified_action_ids": verified,
        "remaining_actions": remaining,
        "all_satisfied": not remaining,
    }


def result_document(
    *,
    subcommand: str,
    operation_id: str,
    plan_sha: str,
    goal_status: str,
    modified: bool,
    mutation_started: bool,
    counts: Mapping[str, Any] | None = None,
    blockers: Iterable[Mapping[str, Any]] = (),
    phase: str = "finished",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "document_type": "operation_result",
        "command": "agent",
        "subcommand": subcommand,
        "mode": "agent",
        "phase": phase,
        "operation_id": operation_id,
        "plan_sha256": plan_sha,
        "goal_status": goal_status,
        "goal_satisfied": goal_status == "complete",
        "modified": bool(modified),
        "mutation_started": bool(mutation_started),
        "blockers": [dict(value) for value in blockers],
        "counts": dict(counts or zero_counts()),
    }
    if details:
        document.update(dict(details))
    return document


__all__ = [
    "PLAN_SCHEMA",
    "RESULT_SCHEMA",
    "action_binding",
    "action_bindings_match",
    "build_agent_plan_document",
    "enum_value",
    "new_operation_id",
    "plan_counts",
    "result_document",
    "structured_blocker",
    "verify_frozen_actions",
    "zero_counts",
]
