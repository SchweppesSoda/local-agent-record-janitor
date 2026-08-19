from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cleaner import ScanFailure, ScanReport
from .core_types import (
    BlockerCode,
    Evidence,
    RecordKind,
    RecordRef,
    StorageKind,
    StorageRef,
)
from .models import Finding
from .planning import (
    ActionImpact,
    ActionKind,
    CandidateAction,
    CleanupPlan,
    Observation,
    RiskLevel,
    ScanStatus,
    StorageLocation,
    TargetRef,
    normalize_storage_path,
    storage_id_for_path,
)


SUPPORTED_SESSION_ENGINES = frozenset({"pi", "claude"})


def build_session_cleanup_context(
    engine: str,
    catalog: Any,
    *,
    catalog_builder: Callable[[], Any],
    target_root: Path | None,
    captured_at: datetime,
    typed_action_builder: Callable[[CleanupPlan], tuple[Any, ...]],
) -> Any:
    """Adapt a metadata-only Pi/Claude inventory to the shared core model."""

    normalized_engine = str(engine).strip().lower()
    if normalized_engine not in SUPPORTED_SESSION_ENGINES:
        raise ValueError(f"Unsupported session engine: {engine!r}")
    if not callable(catalog_builder):
        raise TypeError("catalog_builder must be callable")

    native_plan = _native_delete_plan(normalized_engine, catalog)
    requested_root = (
        Path(normalize_storage_path(target_root))
        if target_root is not None
        else None
    )
    catalog_roots = _catalog_roots(normalized_engine, catalog)
    if requested_root is not None:
        catalog_roots = tuple(
            item
            for item in catalog_roots
            if _same_path(item[0], requested_root)
        )
        if not catalog_roots:
            # Preserve the exact user-selected physical identity even when the
            # directory is empty or its catalog failed before producing rows.
            catalog_roots = ((requested_root, None),)

    native_actions = tuple(
        action
        for action in native_plan.actions
        if requested_root is None
        or _same_path(_action_root(normalized_engine, action), requested_root)
    )
    roots_by_key = {
        normalize_storage_path(root): (root, root_catalog)
        for root, root_catalog in catalog_roots
    }
    for action in native_actions:
        root = _action_root(normalized_engine, action)
        roots_by_key.setdefault(normalize_storage_path(root), (root, None))

    failures_by_root: dict[str, tuple[str, ...]] = {}
    storages: list[StorageLocation] = []
    storage_refs: list[StorageRef] = []
    for key, (root, root_catalog) in sorted(roots_by_key.items()):
        messages = _blocking_failure_messages(
            normalized_engine,
            catalog,
            root,
            root_catalog=root_catalog,
        )
        failures_by_root[key] = messages
        storage_id = storage_id_for_path(root)
        status = ScanStatus.PARTIAL if messages else ScanStatus.OK
        storages.append(
            StorageLocation(
                storage_id=storage_id,
                label=(
                    "Pi Agent data directory"
                    if normalized_engine == "pi"
                    else "Claude Code config directory"
                ),
                path=Path(key),
                scan_status=status,
                errors=messages,
            )
        )
        storage_refs.append(
            StorageRef(
                storage_id=storage_id,
                kind=(
                    StorageKind.JSONL
                    if normalized_engine == "pi"
                    else StorageKind.MANIFEST
                ),
                path=Path(key),
                owner=normalized_engine,
            )
        )

    candidates: list[CandidateAction] = []
    observations: list[Observation] = []
    findings: list[Finding] = []
    records: list[RecordRef] = []
    evidence: list[Evidence] = []
    for native in sorted(native_actions, key=lambda item: str(item.action_id)):
        root = _action_root(normalized_engine, native)
        storage_id = storage_id_for_path(root)
        target = TargetRef(storage_id, str(native.session_id))
        observation_id = _stable_id(
            "observation",
            normalized_engine,
            str(native.action_id),
            str(native.snapshot_fingerprint),
        )
        approval_payload = _json_ready(native.approval_payload())
        artifact_paths = _artifact_paths(normalized_engine, approval_payload)
        root_failures = failures_by_root.get(normalize_storage_path(root), ())
        unavailable = tuple(
            str(value)
            for value in getattr(native, "unavailable_reasons", ())
            if str(value)
        )
        if root_failures:
            unavailable = tuple(dict.fromkeys((*unavailable, *root_failures)))
        available = bool(getattr(native, "available", False)) and not root_failures
        impact = ActionImpact(
            affected_thread_ids=(str(native.session_id),),
            resource_path=(artifact_paths[0] if len(artifact_paths) == 1 else None),
            external_engine=normalized_engine,
            external_storage_root=normalize_storage_path(root),
            external_artifact_paths=artifact_paths,
            external_action_payload=approval_payload,
        )
        kind = (
            ActionKind.DELETE_PI_SESSION
            if normalized_engine == "pi"
            else ActionKind.DELETE_CLAUDE_SESSION
        )
        candidate = CandidateAction(
            action_id=str(native.action_id),
            kind=kind,
            target=target,
            risk=RiskLevel.HIGH,
            available=available,
            unavailable_reason=("; ".join(unavailable) if unavailable else None),
            impact=impact,
            snapshot_fingerprint=str(native.snapshot_fingerprint),
            observation_ids=(observation_id,),
            requires_explicit_selection=True,
            resource_kind=f"{normalized_engine}_session",
        )
        candidates.append(candidate)
        classification = str(
            getattr(
                native,
                "reference_classification",
                getattr(native, "classification", "unreferenced"),
            )
        )
        observation = Observation(
            observation_id=observation_id,
            target=target,
            platform=normalized_engine,
            platform_session_id=str(native.session_id),
            finding_type=f"{normalized_engine}_session",
            reason=f"Explicit {normalized_engine} session deletion candidate",
            details={
                "action_id": str(native.action_id),
                "classification": classification,
                "available": available,
                "artifact_count": len(artifact_paths),
            },
            platform_db=normalize_storage_path(root),
        )
        observations.append(observation)
        finding = Finding(
            platform=normalized_engine,
            platform_session_id=str(native.session_id),
            thread_id=str(native.session_id),
            reason=observation.reason,
            platform_db=Path(normalize_storage_path(root)),
            codex_home=Path(normalize_storage_path(root)),
            details={
                "finding_type": observation.finding_type,
                "action_id": str(native.action_id),
                "classification": classification,
                "available": available,
            },
        )
        findings.append(finding)
        record = RecordRef(
            storage_id=storage_id,
            kind=(
                RecordKind.PI_SESSION
                if normalized_engine == "pi"
                else RecordKind.CLAUDE_SESSION
            ),
            record_id=str(native.session_id),
            locator=(("action_id", str(native.action_id)),),
        )
        records.append(record)
        evidence.append(
            Evidence(
                evidence_id=observation_id.replace("observation", "evidence", 1),
                target=record,
                evidence_type=f"{normalized_engine}_session_snapshot",
                fingerprint=str(native.snapshot_fingerprint),
                source=normalized_engine,
            )
        )

    scan_errors = [
        ScanFailure(
            platform=normalized_engine,
            message=message,
            error_type="SessionInventoryFailure",
            codex_home=Path(root_key),
        )
        for root_key, messages in sorted(failures_by_root.items())
        for message in messages
    ]
    report = ScanReport(findings=findings, errors=scan_errors)
    plan_errors = tuple(dict.fromkeys(error.message for error in scan_errors))
    plan = CleanupPlan(
        storages=tuple(storages),
        observations=tuple(observations),
        actions=tuple(candidates),
        errors=plan_errors,
        plan_fingerprint=_plan_fingerprint(
            storages,
            candidates,
            plan_errors,
        ),
    )
    snapshot_payload = {
        "engine": normalized_engine,
        "storages": [item.to_dict() for item in storage_refs],
        "records": [item.to_dict() for item in records],
        "evidence": [item.to_dict() for item in evidence],
        "errors": list(plan_errors),
    }

    # Imported lazily to keep CleanupService's typed orchestration independent
    # from the backend-specific inventory implementations.
    from .cleanup_service import CleanupContext, StoreSnapshot

    snapshot = StoreSnapshot(
        snapshot_id="snapshot:v1:" + _sha256(snapshot_payload),
        captured_at=captured_at.astimezone(timezone.utc).isoformat(),
        platforms=(normalized_engine,),
        storages=tuple(storage_refs),
        records=tuple(records),
        evidence=tuple(evidence),
        scan_complete=not plan_errors,
        blocker_codes=(
            (BlockerCode("scan_incomplete"),) if plan_errors else ()
        ),
        report=report,
        active_adapters=(),
    )
    return CleanupContext(
        snapshot=snapshot,
        plan=plan,
        actions=typed_action_builder(plan),
        adapter_builder=None,
        session_engine=normalized_engine,
        session_catalog_builder=catalog_builder,
        session_native_plan=native_plan,
    )


def select_session_context(
    context: Any,
    *,
    action_ids: Sequence[str] = (),
    session_ids: Sequence[str] = (),
) -> tuple[Any, tuple[dict[str, Any], ...]]:
    """Return an exact selector-scoped context and structured blockers."""

    from .agent_operations import structured_blocker

    raw_action_ids = tuple(str(value).strip() for value in action_ids)
    raw_session_ids = tuple(str(value).strip() for value in session_ids)
    blockers: list[dict[str, Any]] = []
    if raw_action_ids and raw_session_ids:
        blockers.append(
            structured_blocker(
                "conflicting_selectors",
                scope="selection",
                retryable=False,
                remediation="Use either --action-id or --session-id, not both.",
            )
        )
        return _subset_context(context, (), selector_values=()), tuple(blockers)
    selectors = raw_action_ids or raw_session_ids
    selector_kind = "action_id" if raw_action_ids else "session_id"
    if not selectors:
        blockers.append(
            structured_blocker(
                "explicit_session_selection_required",
                scope="selection",
                retryable=True,
                remediation=(
                    "Pass one or more exact --session-id or --action-id values; "
                    "Agent plans never select all Pi/Claude sessions implicitly."
                ),
            )
        )
        return _subset_context(context, (), selector_values=()), tuple(blockers)
    if any(not value or value.casefold() == "all" for value in selectors):
        blockers.append(
            structured_blocker(
                "invalid_session_selector",
                scope="selection",
                retryable=False,
                remediation="Use exact non-empty session IDs or action IDs; 'all' is forbidden.",
            )
        )
        return _subset_context(context, (), selector_values=selectors), tuple(blockers)
    if len(set(selectors)) != len(selectors):
        blockers.append(
            structured_blocker(
                "duplicate_session_selector",
                scope="selection",
                retryable=False,
                remediation="Remove duplicate selector values and generate a new plan.",
            )
        )
        return _subset_context(context, (), selector_values=selectors), tuple(blockers)

    selected: list[Any] = []
    for selector in selectors:
        matches = [
            action
            for action in context.plan.actions
            if (
                str(action.action_id) == selector
                if selector_kind == "action_id"
                else str(action.target.thread_id) == selector
            )
        ]
        if len(matches) != 1:
            blockers.append(
                structured_blocker(
                    "session_selector_not_found" if not matches else "session_selector_ambiguous",
                    scope="selection",
                    retryable=not matches,
                    remediation=(
                        "Use the full storage-qualified action ID from the current inventory."
                    ),
                    message=f"Selector {selector!r} matched {len(matches)} target(s).",
                )
            )
            continue
        selected.append(matches[0])
    selected_ids = {str(action.action_id) for action in selected}
    return (
        _subset_context(context, selected_ids, selector_values=selectors),
        tuple(blockers),
    )


def _subset_context(
    context: Any,
    selected_action_ids: Sequence[str] | set[str],
    *,
    selector_values: Sequence[str],
) -> Any:
    selected_ids = set(selected_action_ids)
    actions = tuple(
        action
        for action in context.plan.actions
        if str(action.action_id) in selected_ids
    )
    observation_ids = {
        str(value)
        for action in actions
        for value in action.observation_ids
    }
    observations = tuple(
        item
        for item in context.plan.observations
        if str(item.observation_id) in observation_ids
    )
    report = ScanReport(
        findings=[
            finding
            for finding in context.report.findings
            if str(finding.details.get("action_id") or "") in selected_ids
        ],
        errors=list(context.report.errors),
    )
    plan = replace(
        context.plan,
        observations=observations,
        actions=actions,
        planned_actions=(),
        plan_fingerprint=_plan_fingerprint(
            context.plan.storages,
            actions,
            context.plan.errors,
            selectors=selector_values,
        ),
    )
    record_keys = {
        (str(action.target.storage_id), str(action.target.thread_id))
        for action in actions
    }
    snapshot = replace(
        context.snapshot,
        snapshot_id="snapshot:v1:" + _sha256(
            {
                "base": context.snapshot.snapshot_id,
                "actions": sorted(selected_ids),
                "selectors": list(selector_values),
            }
        ),
        records=tuple(
            record
            for record in context.snapshot.records
            if (record.storage_id, record.record_id) in record_keys
        ),
        evidence=tuple(
            item
            for item in context.snapshot.evidence
            if (item.target.storage_id, item.target.record_id) in record_keys
        ),
        report=report,
    )
    typed_by_id = {
        str(action.action_id): action for action in context.actions
    }
    return replace(
        context,
        snapshot=snapshot,
        plan=plan,
        actions=tuple(
            typed_by_id[action_id]
            for action_id in sorted(selected_ids)
            if action_id in typed_by_id
        ),
    )


def _native_delete_plan(engine: str, catalog: Any) -> Any:
    if engine == "pi":
        from .pi_delete import build_pi_delete_plan

        return build_pi_delete_plan(catalog)
    from .claude_delete import build_claude_delete_plan

    return build_claude_delete_plan(catalog)


def _catalog_roots(engine: str, catalog: Any) -> tuple[tuple[Path, Any], ...]:
    nested = getattr(catalog, "catalogs", None)
    catalogs = tuple(nested) if nested is not None else (catalog,)
    result: list[tuple[Path, Any]] = []
    for item in catalogs:
        raw = (
            getattr(item, "agent_dir", getattr(item, "pi_root", None))
            if engine == "pi"
            else getattr(item, "config_dir", None)
        )
        if raw is not None:
            result.append((Path(raw), item))
    return tuple(result)


def _action_root(engine: str, action: Any) -> Path:
    return Path(action.pi_root if engine == "pi" else action.config_dir)


def _blocking_failure_messages(
    engine: str,
    catalog: Any,
    root: Path,
    *,
    root_catalog: Any | None,
) -> tuple[str, ...]:
    failures: list[Any] = []
    if root_catalog is not None:
        failures.extend(
            tuple(
                getattr(
                    root_catalog,
                    "errors",
                    getattr(root_catalog, "failures", ()),
                )
            )
        )
    if engine == "claude":
        failures.extend(tuple(getattr(catalog, "root_errors", ())))
    messages: list[str] = []
    for failure in failures:
        if getattr(failure, "blocks_delete", True) is False:
            continue
        qualifier = (
            getattr(failure, "agent_dir", None)
            if engine == "pi"
            else getattr(failure, "config_dir", None)
        )
        if qualifier is not None and not _same_path(Path(qualifier), root):
            continue
        message = str(getattr(failure, "message", failure))
        if message:
            messages.append(message)
    return tuple(dict.fromkeys(messages))


def _artifact_paths(
    engine: str,
    payload: Mapping[str, Any],
) -> tuple[str, ...]:
    if engine == "pi":
        raw = payload.get("path")
        return (str(raw),) if isinstance(raw, str) and raw else ()
    manifest = payload.get("manifest")
    if not isinstance(manifest, list):
        return ()
    return tuple(
        sorted(
            {
                str(item.get("path"))
                for item in manifest
                if isinstance(item, Mapping)
                and isinstance(item.get("path"), str)
                and item.get("path")
            }
        )
    )


def _plan_fingerprint(
    storages: Sequence[StorageLocation],
    actions: Sequence[CandidateAction],
    errors: Sequence[str],
    *,
    selectors: Sequence[str] = (),
) -> str:
    return _sha256(
        {
            "storages": [item.to_dict() for item in storages],
            "actions": [
                {
                    "action_id": action.action_id,
                    "snapshot_fingerprint": action.snapshot_fingerprint,
                }
                for action in actions
            ],
            "errors": list(errors),
            "selectors": list(selectors),
        }
    )


def _stable_id(prefix: str, *values: str) -> str:
    return f"{prefix}:v1:{_sha256(list(values))}"


def _same_path(left: Path, right: Path) -> bool:
    return normalize_storage_path(left) == normalize_storage_path(right)


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_ready(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_json_ready(item) for item in value]
        return sorted(items, key=str) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = [
    "SUPPORTED_SESSION_ENGINES",
    "build_session_cleanup_context",
    "select_session_context",
]
