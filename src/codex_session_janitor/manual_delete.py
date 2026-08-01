from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cleaner import (
    AppServerFactory,
    BinaryResolver,
    CleanupReport,
    ExpectedDeletionScope,
    FindingVerifier,
    VerificationResult,
    clean_findings,
    finding_key,
    verify_finding_deleted,
)
from .codex_app_server import CodexAppServer
from .codex_state import rollout_state_fingerprint
from .discovery import choose_codex_binary
from .models import ConversationSummary, Finding, RolloutRecord


class ManualDeletePlanError(ValueError):
    """A manual deletion plan is incomplete, stale, or not approved."""


class ManualDeleteSelectionError(ManualDeletePlanError):
    """A selector did not identify exactly one executable root target."""

    def __init__(
        self,
        selector: str,
        *,
        kind: str,
        matches: Sequence[str] = (),
        reason: str | None = None,
    ) -> None:
        self.selector = selector
        self.kind = kind
        self.matches = tuple(matches)
        self.reason = reason
        if kind == "not_found":
            message = f"Manual delete selector {selector!r} matched no action"
        elif kind == "ambiguous":
            message = (
                f"Manual delete selector {selector!r} is ambiguous: "
                + ", ".join(self.matches)
            )
        elif kind == "unavailable":
            message = (
                f"Manual delete target {selector!r} is unavailable: "
                f"{reason or 'the inventory is not complete enough'}"
            )
        else:
            message = reason or f"Invalid manual delete selector {selector!r}"
        super().__init__(message)


@dataclass(frozen=True)
class ManualDeleteAction:
    """One explicitly selectable, storage-qualified permanent deletion root."""

    action_id: str
    codex_home: Path
    thread_id: str
    affected_thread_ids: tuple[str, ...]
    frontend_sessions: tuple[Any, ...]
    expected_scope: ExpectedDeletionScope
    codex_bin_hint: Path | None
    available: bool
    unavailable_reasons: tuple[str, ...]
    snapshot_fingerprint: str
    frontend_snapshot_fingerprint: str
    integrity_approvals: tuple[str, ...]
    root: Any = field(repr=False, compare=False)
    affected_records: tuple[Any, ...] = field(repr=False, compare=False)
    risk: str = "high"

    @property
    def descendants(self) -> tuple[str, ...]:
        return tuple(
            thread_id
            for thread_id in self.affected_thread_ids
            if thread_id != self.thread_id
        )

    @property
    def unavailable_reason(self) -> str | None:
        return "; ".join(self.unavailable_reasons) or None

    def approval_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "action_id": self.action_id,
            "codex_home": _normalize_home(self.codex_home),
            "thread_id": self.thread_id,
            "affected_thread_ids": list(self.affected_thread_ids),
            "frontend_snapshot_fingerprint": (
                self.frontend_snapshot_fingerprint
            ),
            "expected_scope": self.expected_scope.to_dict(),
            "codex_bin_hint": (
                _normalize_path(self.codex_bin_hint)
                if self.codex_bin_hint is not None
                else None
            ),
            "risk": self.risk,
            "integrity_approvals": list(self.integrity_approvals),
            "snapshot_fingerprint": self.snapshot_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.approval_payload(),
            "available": self.available,
            "unavailable_reasons": list(self.unavailable_reasons),
            "frontend_sessions": [
                _frontend_display_payload(item)
                for item in self.frontend_sessions
            ],
        }


@dataclass(frozen=True)
class ManualDeletePlan:
    """An inventory plan, or an approved plan containing explicit roots."""

    actions: tuple[ManualDeleteAction, ...] = ()
    errors: tuple[str, ...] = ()
    selected: bool = False
    plan_fingerprint: str | None = None

    @property
    def executable_actions(self) -> tuple[ManualDeleteAction, ...]:
        return tuple(action for action in self.actions if action.available)

    def with_selected_actions(
        self,
        selectors: Iterable[str],
    ) -> ManualDeletePlan:
        """Return an approved plan containing only explicitly selected roots."""

        raw_selectors = tuple(selectors)
        if not raw_selectors:
            raise ManualDeleteSelectionError(
                "",
                kind="invalid",
                reason="At least one explicit manual delete target is required",
            )

        selected: list[ManualDeleteAction] = []
        selected_ids: set[str] = set()
        for raw_selector in raw_selectors:
            if not isinstance(raw_selector, str) or not raw_selector.strip():
                raise ManualDeleteSelectionError(
                    str(raw_selector),
                    kind="invalid",
                    reason="Manual delete selectors must be non-empty strings",
                )
            selector = raw_selector.strip()
            if selector.lower() == "all":
                raise ManualDeleteSelectionError(
                    selector,
                    kind="invalid",
                    reason="Manual deletion never accepts an all selector",
                )
            action = _select_action(self.actions, selector)
            if not action.available:
                raise ManualDeleteSelectionError(
                    selector,
                    kind="unavailable",
                    reason="; ".join(action.unavailable_reasons),
                )
            if action.action_id in selected_ids:
                raise ManualDeleteSelectionError(
                    selector,
                    kind="invalid",
                    reason=(
                        "Every selected root must be named exactly once; "
                        f"{action.action_id} was selected more than once"
                    ),
                )
            selected.append(action)
            selected_ids.add(action.action_id)

        _reject_overlapping_actions(selected)
        ordered = tuple(sorted(selected, key=lambda item: item.action_id))
        fingerprint = _fingerprint(
            {
                "schema_version": 1,
                "actions": [
                    action.approval_payload()
                    for action in ordered
                ],
            }
        )
        return ManualDeletePlan(
            actions=ordered,
            errors=(),
            selected=True,
            plan_fingerprint=fingerprint,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "plan_fingerprint": self.plan_fingerprint,
            "actions": [action.to_dict() for action in self.actions],
            "errors": list(self.errors),
        }


CatalogBuilder = Callable[[], Any]


def build_manual_delete_plan(catalog: Any) -> ManualDeletePlan:
    """Build HIGH-risk, per-root actions from a read-only session catalog.

    Inventory structures are read through the small adapter helpers below so
    current ``records/conversations`` and ``errors/failures`` aliases remain
    compatible. Missing safety fields never receive permissive defaults.
    """

    records, catalog_structure_errors = _catalog_records(catalog)
    failures, failure_structure_errors = _catalog_failures(catalog)
    errors = [*catalog_structure_errors, *failure_structure_errors]

    by_target: dict[tuple[str, str], Any] = {}
    action_id_targets: dict[str, tuple[str, str]] = {}
    for record in records:
        home, thread_id, identity_error = _record_identity(record)
        if identity_error is not None:
            errors.append(identity_error)
            continue
        assert home is not None and thread_id is not None
        key = (_normalize_home(home), thread_id)
        if key in by_target:
            errors.append(
                "Inventory contains duplicate storage-qualified conversation "
                f"{key[0]} / {thread_id}"
            )
            continue
        by_target[key] = record
        action_id_present, catalog_action_id = _attribute(record, ("action_id",))
        if (
            not action_id_present
            or not isinstance(catalog_action_id, str)
            or not catalog_action_id
        ):
            errors.append(
                "ManagedConversation is missing a valid stable action_id for "
                f"{key[0]} / {thread_id}"
            )
        elif catalog_action_id in action_id_targets:
            errors.append(
                "Inventory contains duplicate stable action_id "
                f"{catalog_action_id!r} for distinct targets"
            )
        else:
            action_id_targets[catalog_action_id] = key

    blocking_failures = list(_blocking_failures(failures, errors))
    if errors:
        blocking_failures.append(
            (None, "catalog structure is incomplete: " + "; ".join(errors))
        )
    actions = tuple(
        _build_action(
            record,
            by_target=by_target,
            blocking_failures=tuple(blocking_failures),
        )
        for _key, record in sorted(by_target.items())
    )
    return ManualDeletePlan(
        actions=actions,
        errors=tuple(
            dict.fromkeys(
                [
                    *errors,
                    *(
                        f"Blocking inventory failure: {message}"
                        for _home, message in blocking_failures
                        if not message.startswith("catalog structure is incomplete:")
                    ),
                ]
            )
        ),
    )


def execute_manual_delete(
    plan: ManualDeletePlan,
    *,
    catalog_builder: CatalogBuilder,
    approved_plan_fingerprint: str,
    clients_closed: bool,
    timeout: float = 30.0,
    app_server_factory: AppServerFactory = CodexAppServer,
    binary_resolver: BinaryResolver = choose_codex_binary,
    verifier: FindingVerifier | None = None,
    verification_attempts: int = 4,
    verification_interval: float = 0.05,
) -> CleanupReport:
    """Revalidate and execute an explicitly approved manual deletion plan.

    ``catalog_builder`` is intentionally required. It is called once before
    app-server startup for the complete approval comparison, then again by
    the cleaner immediately before each deletion request to compare the exact
    frontend-reference snapshot. No Cindy/AionUI database is ever modified.
    """

    if not clients_closed:
        raise ManualDeletePlanError(
            "Manual deletion requires an explicit clients-closed confirmation"
        )
    if not plan.selected or not plan.actions or not plan.plan_fingerprint:
        raise ManualDeletePlanError(
            "Manual deletion requires a non-empty selected plan"
        )
    if not isinstance(approved_plan_fingerprint, str) or not hmac.compare_digest(
        plan.plan_fingerprint,
        approved_plan_fingerprint,
    ):
        raise ManualDeletePlanError(
            "The approved plan fingerprint does not match the selected plan"
        )
    if not callable(catalog_builder):
        raise ManualDeletePlanError("catalog_builder must be callable")

    refreshed = build_manual_delete_plan(catalog_builder()).with_selected_actions(
        action.action_id for action in plan.actions
    )
    if not hmac.compare_digest(
        approved_plan_fingerprint,
        refreshed.plan_fingerprint or "",
    ):
        raise ManualDeletePlanError(
            "The manual deletion plan changed after approval; nothing was deleted"
        )

    findings = [_synthetic_finding(action) for action in refreshed.actions]
    expected_scopes = {
        finding_key(finding): action.expected_scope
        for finding, action in zip(findings, refreshed.actions)
    }
    approved_descendants = {
        finding_key(finding): action.descendants
        for finding, action in zip(findings, refreshed.actions)
    }
    approved_integrity_deletes = {
        finding_key(finding): set(action.integrity_approvals)
        for finding, action in zip(findings, refreshed.actions)
        if action.integrity_approvals
    }
    action_by_key = {
        finding_key(finding): action
        for finding, action in zip(findings, refreshed.actions)
    }

    def validate_frontend_snapshot(finding: Finding) -> None:
        expected = action_by_key[finding_key(finding)]
        current_plan = build_manual_delete_plan(catalog_builder())
        current = current_plan.with_selected_actions(
            (expected.action_id,)
        ).actions[0]
        if not hmac.compare_digest(
            expected.frontend_snapshot_fingerprint,
            current.frontend_snapshot_fingerprint,
        ):
            raise ManualDeletePlanError(
                "frontend reference snapshot changed after approval"
            )
        if not hmac.compare_digest(
            expected.snapshot_fingerprint,
            current.snapshot_fingerprint,
        ):
            raise ManualDeletePlanError(
                "manual deletion inventory snapshot changed after approval"
            )

    def verify_with_complete_inventory(finding: Finding) -> VerificationResult:
        try:
            verification_catalog = catalog_builder()
            verification_failures, structure_errors = _catalog_failures(
                verification_catalog
            )
            blocking = _blocking_failures(
                verification_failures,
                structure_errors,
            )
        except Exception as exc:
            return VerificationResult(
                deleted=False,
                error=f"Could not rebuild inventory after deletion: {exc}",
                status="unknown",
            )
        home = _normalize_home(finding.codex_home)
        relevant = [
            message
            for failure_home, message in blocking
            if failure_home is None or failure_home == home
        ]
        if structure_errors or relevant:
            messages = [*structure_errors, *relevant]
            return VerificationResult(
                deleted=False,
                error=(
                    "Post-delete inventory was incomplete: "
                    + "; ".join(dict.fromkeys(messages))
                ),
                status="unknown",
            )
        return verify_finding_deleted(finding)

    kwargs: dict[str, Any] = {
        "verifier": (
            verifier if verifier is not None else verify_with_complete_inventory
        ),
    }
    return clean_findings(
        findings,
        timeout=timeout,
        app_server_factory=app_server_factory,
        binary_resolver=binary_resolver,
        verification_attempts=verification_attempts,
        verification_interval=verification_interval,
        explicit_selection=True,
        approved_descendants=approved_descendants,
        expected_scopes=expected_scopes,
        approved_integrity_deletes=(
            approved_integrity_deletes or None
        ),
        pre_delete_validator=validate_frontend_snapshot,
        **kwargs,
    )


def _build_action(
    record: Any,
    *,
    by_target: Mapping[tuple[str, str], Any],
    blocking_failures: tuple[tuple[str | None, str], ...],
) -> ManualDeleteAction:
    home, thread_id, identity_error = _record_identity(record)
    assert identity_error is None and home is not None and thread_id is not None
    canonical_home = _canonical_path(home)
    normalized_home = _normalize_home(home)
    reasons: list[str] = []

    descendants, descendants_error = _required_string_tuple(
        record,
        ("descendant_thread_ids", "descendants"),
        "descendant_thread_ids",
    )
    if descendants_error:
        reasons.append(descendants_error)
    descendants = tuple(
        sorted(item for item in descendants if item != thread_id)
    )
    affected_ids = (thread_id, *descendants)
    if len(set(affected_ids)) != len(affected_ids):
        reasons.append("cascade descendants contain duplicate thread IDs")
    affected_ids = tuple(sorted(set(affected_ids)))

    affected_records: list[Any] = []
    for affected_id in affected_ids:
        affected = by_target.get((normalized_home, affected_id))
        if affected is None:
            reasons.append(
                "cascade inventory is incomplete because descendant "
                f"{affected_id} has no ManagedConversation record"
            )
            continue
        affected_records.append(affected)

    cascade_unknown, cascade_error = _required_bool(
        record,
        ("cascade_unknown",),
        "cascade_unknown",
    )
    if cascade_error:
        reasons.append(cascade_error)
    elif cascade_unknown:
        reasons.append("cascade_unknown prevents exact thread/delete approval")

    delete_supported, delete_error = _required_bool(
        record,
        ("delete_supported", "deletable"),
        "delete_supported",
    )
    if delete_error:
        reasons.append(delete_error)
    elif not delete_supported:
        reasons.append("inventory marked thread/delete unavailable")

    blockers, blockers_error = _required_string_tuple(
        record,
        ("blockers",),
        "blockers",
    )
    if blockers_error:
        reasons.append(blockers_error)
    reasons.extend(f"inventory blocker: {item}" for item in blockers)

    root_rollouts, rollout_error = _record_rollouts(record)
    if rollout_error:
        reasons.append(rollout_error)
    indexed, indexed_error = _required_bool(
        record,
        ("indexed", "indexed_thread"),
        "indexed",
    )
    if indexed_error:
        reasons.append(indexed_error)
    legacy_indexed, legacy_error = _required_bool(
        record,
        ("legacy_indexed",),
        "legacy_indexed",
    )
    if legacy_error:
        reasons.append(legacy_error)
    artifact_present, artifact_error = _required_bool(
        record,
        ("artifact_present", "has_codex_artifacts"),
        "artifact_present",
    )
    if artifact_error:
        reasons.append(artifact_error)
    elif artifact_present != bool(indexed or root_rollouts):
        reasons.append(
            "artifact_present disagrees with indexed/rollout evidence"
        )
    if not indexed and not root_rollouts:
        reasons.append(
            "legacy-index-only or frontend-only records cannot prove a "
            "thread/delete target"
        )
    if legacy_indexed and not indexed and not root_rollouts:
        reasons.append("legacy-index-only records are report-only")

    for failure_home, failure_text in blocking_failures:
        if failure_home is None or failure_home == normalized_home:
            reasons.append(f"blocking inventory failure: {failure_text}")

    indexed_ids: list[str] = []
    rollout_paths: list[str] = []
    rollout_fingerprints: list[str] = []
    metadata_fingerprints: list[str] = []
    frontend_sessions: list[Any] = []
    affected_payloads: list[dict[str, Any]] = []
    hint_paths: dict[str, Path] = {}
    root_integrity_approvals: set[str] = set()
    for affected in affected_records:
        affected_home, affected_id, affected_identity_error = _record_identity(
            affected
        )
        if affected_identity_error:
            reasons.append(affected_identity_error)
            continue
        assert affected_home is not None and affected_id is not None
        if _normalize_home(affected_home) != normalized_home:
            reasons.append("cascade target escaped the root Codex data directory")
            continue

        item_indexed, item_indexed_error = _required_bool(
            affected,
            ("indexed", "indexed_thread"),
            "indexed",
        )
        if item_indexed_error:
            reasons.append(f"{affected_id}: {item_indexed_error}")
        elif item_indexed:
            indexed_ids.append(affected_id)
        item_legacy_indexed, item_legacy_error = _required_bool(
            affected,
            ("legacy_indexed",),
            "legacy_indexed",
        )
        if item_legacy_error:
            reasons.append(f"{affected_id}: {item_legacy_error}")

        item_rollouts, item_rollout_error = _record_rollouts(affected)
        if item_rollout_error:
            reasons.append(f"{affected_id}: {item_rollout_error}")
        item_rollout_payloads: list[dict[str, Any]] = []
        if len(item_rollouts) > 1:
            if affected_id == thread_id:
                root_integrity_approvals.add("duplicate_rollout")
            else:
                reasons.append(
                    f"{affected_id}: descendant has multiple rollout files; "
                    "cleaner integrity approval is root-specific"
                )
        for rollout in item_rollouts:
            try:
                state_fingerprint = rollout_state_fingerprint(rollout)
            except Exception as exc:
                reasons.append(
                    f"{affected_id}: rollout fingerprint failed: "
                    f"{str(exc) or repr(exc)}"
                )
                continue
            path = _normalize_path(rollout.path)
            rollout_paths.append(path)
            rollout_fingerprints.append(state_fingerprint)
            item_rollout_payloads.append(
                {
                    "path": path,
                    "state_fingerprint": state_fingerprint,
                }
            )

        item_index, item_index_error = _record_index(affected)
        if item_index_error:
            reasons.append(f"{affected_id}: {item_index_error}")
        elif item_indexed and item_index is None:
            reasons.append(
                f"{affected_id}: indexed is true but thread_index is absent"
            )
        elif not item_indexed and item_index is not None:
            reasons.append(
                f"{affected_id}: indexed is false but thread_index is present"
            )
        item_cascade_unknown, item_cascade_error = _required_bool(
            affected,
            ("cascade_unknown",),
            "cascade_unknown",
        )
        if item_cascade_error:
            reasons.append(f"{affected_id}: {item_cascade_error}")
        elif item_cascade_unknown:
            reasons.append(
                f"{affected_id}: cascade_unknown prevents exact approval"
            )
        indexed_rollout_path = _indexed_rollout_path(
            canonical_home,
            item_index,
        )
        parsed_rollout_keys = {
            _normalize_path(rollout.path) for rollout in item_rollouts
        }
        if (
            indexed_rollout_path is not None
            and parsed_rollout_keys
            and _normalize_path(indexed_rollout_path)
            not in parsed_rollout_keys
        ):
            try:
                indexed_path_exists = _optional_regular_file_exists(
                    indexed_rollout_path
                )
            except ManualDeletePlanError as exc:
                reasons.append(f"{affected_id}: {exc}")
                indexed_path_exists = None
            if indexed_path_exists:
                reasons.append(
                    f"{affected_id}: existing indexed rollout path does not "
                    "have metadata confirming this thread"
                )
            elif indexed_path_exists is False and affected_id == thread_id:
                root_integrity_approvals.add("index_rollout_path_mismatch")
            elif indexed_path_exists is False:
                reasons.append(
                    f"{affected_id}: descendant index/rollout path mismatch "
                    "cannot receive root-specific integrity approval"
                )

        summary, summary_error = _record_summary(affected)
        if summary_error:
            reasons.append(f"{affected_id}: {summary_error}")
            summary_payload: Any = None
        else:
            assert summary is not None
            metadata_fingerprints.append(
                f"{affected_id}={summary.metadata_fingerprint}"
            )
            summary_payload = summary.approval_payload()

        sessions, sessions_error = _record_frontend_sessions(affected)
        if sessions_error:
            reasons.append(f"{affected_id}: {sessions_error}")
        frontend_sessions.extend(sessions)

        hints, hints_error = _record_hints(affected)
        if hints_error:
            reasons.append(f"{affected_id}: {hints_error}")
        hint_paths.update(
            (_normalize_path(item), _canonical_path(item))
            for item in hints
        )
        affected_payloads.append(
            {
                "thread_id": affected_id,
                "indexed": item_indexed,
                "legacy_indexed": item_legacy_indexed,
                "rollouts": item_rollout_payloads,
                "summary": summary_payload,
                "thread_index": _json_value(item_index),
            }
        )

    if len(hint_paths) > 1:
        reasons.append(
            "affected records provide conflicting Codex executable hints"
        )
    codex_bin_hint = next(iter(hint_paths.values())) if hint_paths else None

    frontend_payload = sorted(
        (_frontend_approval_payload(item) for item in frontend_sessions),
        key=_canonical_json,
    )
    frontend_fingerprint = _fingerprint(
        {
            "schema_version": 1,
            "codex_home": normalized_home,
            "thread_ids": list(affected_ids),
            "frontend_sessions": frontend_payload,
        }
    )
    expected_scope = ExpectedDeletionScope(
        descendant_thread_ids=descendants,
        indexed_thread_ids=tuple(sorted(set(indexed_ids))),
        rollout_paths=tuple(sorted(set(rollout_paths))),
        rollout_state_fingerprints=tuple(
            sorted(set(rollout_fingerprints))
        ),
        conversation_metadata_fingerprints=tuple(
            sorted(set(metadata_fingerprints))
        ),
    )
    action_id_present, catalog_action_id = _attribute(record, ("action_id",))
    if (
        action_id_present
        and isinstance(catalog_action_id, str)
        and catalog_action_id
    ):
        action_id = catalog_action_id
    else:
        action_id = _action_id(normalized_home, thread_id)
        reasons.append("ManagedConversation is missing a valid stable action_id")
    snapshot = _fingerprint(
        {
            "schema_version": 1,
            "action_id": action_id,
            "codex_home": normalized_home,
            "thread_id": thread_id,
            "affected_thread_ids": list(affected_ids),
            "affected_records": affected_payloads,
            "expected_scope": expected_scope.to_dict(),
            "frontend_sessions": frontend_payload,
            "codex_bin_hints": sorted(hint_paths),
            "integrity_approvals": sorted(root_integrity_approvals),
        }
    )
    unique_reasons = tuple(dict.fromkeys(reasons))
    return ManualDeleteAction(
        action_id=action_id,
        codex_home=canonical_home,
        thread_id=thread_id,
        affected_thread_ids=affected_ids,
        frontend_sessions=tuple(frontend_sessions),
        expected_scope=expected_scope,
        codex_bin_hint=codex_bin_hint,
        available=not unique_reasons,
        unavailable_reasons=unique_reasons,
        snapshot_fingerprint=snapshot,
        frontend_snapshot_fingerprint=frontend_fingerprint,
        integrity_approvals=tuple(sorted(root_integrity_approvals)),
        root=record,
        affected_records=tuple(affected_records),
    )


def _synthetic_finding(action: ManualDeleteAction) -> Finding:
    root_rollouts, error = _record_rollouts(action.root)
    if error:
        raise ManualDeletePlanError(error)
    first_frontend_db = next(
        (
            _frontend_database(item)
            for item in action.frontend_sessions
            if _frontend_database(item) is not None
        ),
        action.codex_home / "state_5.sqlite",
    )
    return Finding(
        platform="manual",
        platform_session_id=action.action_id,
        thread_id=action.thread_id,
        reason="explicitly selected permanent Codex thread deletion",
        platform_db=Path(first_frontend_db),
        codex_home=action.codex_home,
        rollout=root_rollouts[0] if root_rollouts else None,
        codex_indexed=(
            action.thread_id in action.expected_scope.indexed_thread_ids
        ),
        codex_archived=(
            getattr(_record_summary(action.root)[0], "archived", None)
        ),
        codex_bin_hint=action.codex_bin_hint,
        details={
            "finding_type": "manual_delete",
            "cleanable": True,
            "thread_delete_supported": True,
            "requires_explicit_selection": True,
            "manual_delete": True,
            "approved_action_id": action.action_id,
            "frontend_references_preserved": True,
        },
    )


def _select_action(
    actions: Sequence[ManualDeleteAction],
    selector: str,
) -> ManualDeleteAction:
    exact_action = [item for item in actions if item.action_id == selector]
    if exact_action:
        return exact_action[0]
    exact_thread = [item for item in actions if item.thread_id == selector]
    if len(exact_thread) == 1:
        return exact_thread[0]
    if len(exact_thread) > 1:
        raise ManualDeleteSelectionError(
            selector,
            kind="ambiguous",
            matches=[item.action_id for item in exact_thread],
        )
    matches = [
        item
        for item in actions
        if item.action_id.startswith(selector)
        or item.thread_id.startswith(selector)
    ]
    if not matches:
        raise ManualDeleteSelectionError(selector, kind="not_found")
    if len(matches) > 1:
        raise ManualDeleteSelectionError(
            selector,
            kind="ambiguous",
            matches=[item.action_id for item in matches],
        )
    return matches[0]


def _reject_overlapping_actions(
    actions: Sequence[ManualDeleteAction],
) -> None:
    affected = [
        (item, set(item.affected_thread_ids))
        for item in actions
    ]
    for index, (left, left_ids) in enumerate(affected):
        for right, right_ids in affected[index + 1 :]:
            if _normalize_home(left.codex_home) != _normalize_home(
                right.codex_home
            ):
                continue
            shared = sorted(left_ids & right_ids)
            if shared:
                raise ManualDeletePlanError(
                    "Selected manual deletion roots overlap or contain one "
                    f"another ({left.thread_id} / {right.thread_id}: "
                    f"{', '.join(shared)})"
                )


def _catalog_records(catalog: Any) -> tuple[tuple[Any, ...], list[str]]:
    present, value = _attribute(catalog, ("conversations", "records"))
    if not present:
        return (), ["SessionCatalog is missing conversations/records"]
    return _object_tuple(value, "SessionCatalog conversations/records")


def _catalog_failures(catalog: Any) -> tuple[tuple[Any, ...], list[str]]:
    present, value = _attribute(catalog, ("failures", "errors"))
    if not present:
        return (), ["SessionCatalog is missing failures/errors"]
    return _object_tuple(value, "SessionCatalog failures/errors")


def _object_tuple(value: Any, label: str) -> tuple[tuple[Any, ...], list[str]]:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return (), [f"{label} must be an iterable of records"]
    try:
        return tuple(value), []
    except TypeError:
        return (), [f"{label} must be an iterable of records"]


def _blocking_failures(
    failures: Sequence[Any],
    errors: list[str],
) -> tuple[tuple[str | None, str], ...]:
    result: list[tuple[str | None, str]] = []
    for failure in failures:
        present, blocks_delete = _attribute(failure, ("blocks_delete",))
        if not present or not isinstance(blocks_delete, bool):
            errors.append("InventoryFailure is missing boolean blocks_delete")
            result.append((None, "malformed InventoryFailure"))
            continue
        if not blocks_delete:
            continue
        home_present, home = _attribute(failure, ("codex_home",))
        message_present, message = _attribute(failure, ("message",))
        if not home_present or not message_present or not isinstance(message, str):
            errors.append("InventoryFailure is missing codex_home/message")
            result.append((None, "malformed InventoryFailure"))
            continue
        if home is not None and not isinstance(home, (str, os.PathLike)):
            errors.append("InventoryFailure has an invalid codex_home")
            result.append((None, "malformed InventoryFailure"))
            continue
        normalized_home = _normalize_home(home) if home is not None else None
        result.append((normalized_home, message))
    return tuple(result)


def _record_identity(
    record: Any,
) -> tuple[Path | None, str | None, str | None]:
    home_present, home = _attribute(record, ("codex_home",))
    thread_present, thread_id = _attribute(record, ("thread_id",))
    if not home_present or not isinstance(home, (str, os.PathLike)):
        return None, None, "ManagedConversation is missing a valid codex_home"
    if not thread_present or not isinstance(thread_id, str) or not thread_id:
        return None, None, "ManagedConversation is missing a valid thread_id"
    return Path(home), thread_id, None


def _record_rollouts(record: Any) -> tuple[tuple[RolloutRecord, ...], str | None]:
    present, value = _attribute(record, ("rollouts",))
    if not present:
        return (), "ManagedConversation is missing rollouts"
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return (), "ManagedConversation rollouts must be an iterable"
    try:
        values = tuple(value)
    except TypeError:
        return (), "ManagedConversation rollouts must be an iterable"
    if not all(isinstance(item, RolloutRecord) for item in values):
        return (), "ManagedConversation rollouts contain an invalid record"
    return values, None


def _record_summary(
    record: Any,
) -> tuple[ConversationSummary | None, str | None]:
    present, value = _attribute(record, ("summary",))
    if not present or not isinstance(value, ConversationSummary):
        return None, "ManagedConversation is missing a ConversationSummary"
    return value, None


def _record_index(record: Any) -> tuple[Mapping[str, Any] | None, str | None]:
    present, value = _attribute(record, ("thread_index", "index"))
    if not present:
        return None, "ManagedConversation is missing thread_index/index"
    if value is not None and not isinstance(value, Mapping):
        return None, "ManagedConversation thread_index/index must be a mapping or None"
    return value, None


def _indexed_rollout_path(
    codex_home: Path,
    row: Mapping[str, Any] | None,
) -> Path | None:
    if row is None:
        return None
    value = row.get("rollout_path")
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = codex_home / path
    return path


def _record_frontend_sessions(record: Any) -> tuple[tuple[Any, ...], str | None]:
    present, value = _attribute(record, ("frontend_sessions",))
    if not present:
        return (), "ManagedConversation is missing frontend_sessions"
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return (), "ManagedConversation frontend_sessions must be an iterable"
    try:
        sessions = tuple(value)
    except TypeError:
        return (), "ManagedConversation frontend_sessions must be an iterable"
    try:
        for item in sessions:
            _frontend_approval_payload(item)
    except ManualDeletePlanError as exc:
        return (), str(exc)
    return sessions, None


def _record_hints(record: Any) -> tuple[tuple[Path, ...], str | None]:
    present, value = _attribute(
        record,
        ("codex_bin_hints", "codex_binary_hints"),
    )
    if not present:
        return (), "ManagedConversation is missing codex_bin_hints"
    if value is None:
        return (), None
    if isinstance(value, (str, os.PathLike)):
        value = (value,)
    try:
        values = tuple(value)
    except TypeError:
        return (), "ManagedConversation codex_bin_hints must be iterable"
    if not all(isinstance(item, (str, os.PathLike)) for item in values):
        return (), "ManagedConversation codex_bin_hints contain an invalid path"
    if any(not os.fspath(item) for item in values):
        return (), "ManagedConversation codex_bin_hints contain an empty path"
    return tuple(Path(item) for item in values), None


def _required_bool(
    value: Any,
    aliases: Sequence[str],
    label: str,
) -> tuple[bool, str | None]:
    present, item = _attribute(value, aliases)
    if not present or not isinstance(item, bool):
        return False, f"ManagedConversation is missing boolean {label}"
    return item, None


def _required_string_tuple(
    value: Any,
    aliases: Sequence[str],
    label: str,
) -> tuple[tuple[str, ...], str | None]:
    present, item = _attribute(value, aliases)
    if not present or item is None or isinstance(item, (str, bytes, Mapping)):
        return (), f"ManagedConversation is missing iterable {label}"
    try:
        values = tuple(item)
    except TypeError:
        return (), f"ManagedConversation is missing iterable {label}"
    if not all(isinstance(entry, str) and entry for entry in values):
        return (), f"ManagedConversation {label} contains an invalid value"
    return values, None


def _frontend_approval_payload(record: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    aliases = {
        "platform": ("platform",),
        "database": ("database", "platform_db"),
        "platform_session_id": ("platform_session_id", "session_id"),
        "thread_id": ("thread_id",),
        "backend": ("backend",),
        "status": ("status",),
        "updated_at_ms": ("updated_at_ms", "last_active_at_ms"),
        "is_live": ("is_live",),
    }
    for label, names in aliases.items():
        present, value = _attribute(record, names)
        if not present:
            raise ManualDeletePlanError(
                f"FrontendSessionRecord is missing {label}"
            )
        if label == "database":
            if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
                raise ManualDeletePlanError(
                    "FrontendSessionRecord has invalid database"
                )
            value = _normalize_path(value)
        fields[label] = _json_value(value)
    if not isinstance(fields["platform"], str) or not fields["platform"]:
        raise ManualDeletePlanError("FrontendSessionRecord has invalid platform")
    if not isinstance(fields["database"], str) or not fields["database"]:
        raise ManualDeletePlanError("FrontendSessionRecord has invalid database")
    if (
        not isinstance(fields["platform_session_id"], str)
        or not fields["platform_session_id"]
    ):
        raise ManualDeletePlanError(
            "FrontendSessionRecord has invalid platform_session_id"
        )
    if not isinstance(fields["thread_id"], str) or not fields["thread_id"]:
        raise ManualDeletePlanError("FrontendSessionRecord has invalid thread_id")
    if fields["backend"] is not None and not isinstance(fields["backend"], str):
        raise ManualDeletePlanError("FrontendSessionRecord has invalid backend")
    if fields["status"] is not None and not isinstance(fields["status"], str):
        raise ManualDeletePlanError("FrontendSessionRecord has invalid status")
    if (
        fields["updated_at_ms"] is not None
        and (
            not isinstance(fields["updated_at_ms"], int)
            or isinstance(fields["updated_at_ms"], bool)
        )
    ):
        raise ManualDeletePlanError(
            "FrontendSessionRecord has invalid updated_at_ms"
        )
    if not isinstance(fields["is_live"], bool):
        raise ManualDeletePlanError("FrontendSessionRecord has invalid is_live")
    return {"schema_version": 1, **fields}


def _frontend_display_payload(record: Any) -> dict[str, Any]:
    method = getattr(record, "to_dict", None)
    if callable(method):
        value = method()
        if isinstance(value, Mapping):
            return _json_value(dict(value))
    return _frontend_approval_payload(record)


def _frontend_database(record: Any) -> Path | None:
    present, value = _attribute(record, ("database", "platform_db"))
    if not present or value is None or not isinstance(value, (str, os.PathLike)):
        return None
    return Path(value)


def _attribute(value: Any, aliases: Sequence[str]) -> tuple[bool, Any]:
    if isinstance(value, Mapping):
        for alias in aliases:
            if alias in value:
                return True, value[alias]
        return False, None
    for alias in aliases:
        if hasattr(value, alias):
            return True, getattr(value, alias)
    return False, None


def _action_id(normalized_home: str, thread_id: str) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "schema_version": 1,
                "codex_home": normalized_home,
                "thread_id": thread_id,
            }
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"manual-delete-{digest}"


def _normalize_home(value: str | os.PathLike[str]) -> str:
    path = _canonical_path(value)
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _canonical_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    try:
        path = path.resolve(strict=False)
    except (OSError, RuntimeError):
        path = Path(os.path.abspath(os.fspath(path)))
    return path


def _optional_regular_file_exists(path: Path) -> bool:
    try:
        path_status = path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ManualDeletePlanError(f"could not inspect rollout path {path}: {exc}") from exc
    if not stat.S_ISREG(path_status.st_mode):
        raise ManualDeletePlanError(f"rollout path is not a regular file: {path}")
    return True


def _normalize_path(value: str | os.PathLike[str]) -> str:
    return _normalize_home(value)


def _fingerprint(value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"v1:{digest}"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_json_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


__all__ = [
    "ManualDeleteAction",
    "ManualDeletePlan",
    "ManualDeletePlanError",
    "ManualDeleteSelectionError",
    "build_manual_delete_plan",
    "execute_manual_delete",
]
