from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .codex_state import (
    find_thread_rollouts,
    read_spawn_descendants,
    read_thread_index,
    rollout_state_fingerprint,
)
from .blocker_codes import (
    CASCADE_REQUIRES_EXPLICIT_SCOPE,
    INTEGRITY_REVIEW_REQUIRED,
    STANDALONE_RELATION_CLEANUP_UNAVAILABLE,
    cleanup_blocker_codes,
    exact_blocker_codes,
)
from .conversation_metadata import (
    read_conversation_summaries,
    read_legacy_thread_names,
)
from .legacy_index import (
    LegacyIndexInventory,
    inventory_legacy_index,
)
from .models import ConversationSummary, Finding, RolloutRecord
from .path_identity import canonical_existing_path_key


class ActionKind(str, Enum):
    DELETE_CONVERSATION = "delete_conversation"
    REMOVE_BROKEN_RELATION = "remove_broken_relation"
    REPAIR_INDEX_PATH = "repair_index_path"
    QUARANTINE_ARTIFACTS = "quarantine_artifacts"
    REMOVE_FRONTEND_REFERENCE = "remove_frontend_reference"
    REMOVE_DESKTOP_STATE = "remove_desktop_state"
    REPAIR_LEGACY_INDEX = "repair_legacy_index"
    KEEP = "keep"


class RiskLevel(str, Enum):
    LOW = "low"
    REVIEW = "review"
    HIGH = "high"
    BLOCKED = "blocked"


class ScanStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class ResultStatus(str, Enum):
    DELETED = "deleted"
    NOT_DELETED = "not_deleted"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class StorageLocation:
    storage_id: str
    label: str
    path: Path
    codex_bin_hint: Path | None = None
    scan_status: ScanStatus = ScanStatus.OK
    errors: tuple[str, ...] = ()

    @property
    def normalized_path(self) -> str:
        return normalize_storage_path(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage_id": self.storage_id,
            "label": self.label,
            "path": self.normalized_path,
            "codex_bin_hint": (
                str(self.codex_bin_hint) if self.codex_bin_hint is not None else None
            ),
            "scan_status": self.scan_status.value,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class TargetRef:
    storage_id: str
    thread_id: str

    @property
    def full_thread_id(self) -> str:
        return self.thread_id

    def to_dict(self) -> dict[str, str]:
        return {
            "storage_id": self.storage_id,
            "thread_id": self.thread_id,
        }


@dataclass(frozen=True)
class Observation:
    observation_id: str
    target: TargetRef
    platform: str
    platform_session_id: str
    finding_type: str
    reason: str
    details: Mapping[str, Any] = field(default_factory=dict)
    codex_indexed: bool = False
    codex_archived: bool | None = None
    rollout_paths: tuple[str, ...] = ()
    platform_db: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "target": self.target.to_dict(),
            "platform": self.platform,
            "platform_session_id": self.platform_session_id,
            "finding_type": self.finding_type,
            "reason": self.reason,
            "details": _json_value(self.details),
            "codex_indexed": self.codex_indexed,
            "codex_archived": self.codex_archived,
            "rollout_paths": list(self.rollout_paths),
            "platform_db": self.platform_db,
        }


@dataclass(frozen=True)
class ActionImpact:
    index_record_count: int = 0
    rollout_file_count: int = 0
    rollout_paths: tuple[str, ...] = ()
    descendant_thread_ids: tuple[str, ...] = ()
    affected_thread_ids: tuple[str, ...] = ()
    frontend_reference_count: int = 0
    frontend_residual_count: int = 0
    frontend_references_preserved: bool = True
    frontend_database_paths: tuple[str, ...] = ()
    frontend_reference_evidence: tuple[Mapping[str, Any], ...] = ()
    indexed_thread_ids: tuple[str, ...] = ()
    rollout_state_fingerprints: tuple[str, ...] = ()
    conversation_metadata_fingerprints: tuple[str, ...] = ()
    resource_path: str | None = None
    legacy_residual_thread_ids: tuple[str, ...] = ()
    legacy_residual_line_count: int = 0
    legacy_original_sha256: str | None = None
    legacy_expected_sha256: str | None = None
    fingerprint_error: str | None = None
    desktop_catalog_record_count: int = 0
    desktop_global_state_reference_count: int = 0
    desktop_database_paths: tuple[str, ...] = ()
    desktop_global_state_paths: tuple[str, ...] = ()

    @property
    def descendant_count(self) -> int:
        return len(self.descendant_thread_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_record_count": self.index_record_count,
            "indexed_thread_ids": list(self.indexed_thread_ids),
            "rollout_file_count": self.rollout_file_count,
            "rollout_paths": list(self.rollout_paths),
            "rollout_state_fingerprints": list(
                self.rollout_state_fingerprints
            ),
            "conversation_metadata_fingerprints": list(
                self.conversation_metadata_fingerprints
            ),
            "resource_path": self.resource_path,
            "legacy_residual_thread_ids": list(
                self.legacy_residual_thread_ids
            ),
            "legacy_residual_line_count": self.legacy_residual_line_count,
            "legacy_original_sha256": self.legacy_original_sha256,
            "legacy_expected_sha256": self.legacy_expected_sha256,
            "fingerprint_error": self.fingerprint_error,
            "desktop_catalog_record_count": self.desktop_catalog_record_count,
            "desktop_global_state_reference_count": (
                self.desktop_global_state_reference_count
            ),
            "desktop_database_paths": list(self.desktop_database_paths),
            "desktop_global_state_paths": list(self.desktop_global_state_paths),
            "descendant_thread_count": self.descendant_count,
            "descendant_thread_ids": list(self.descendant_thread_ids),
            "affected_thread_ids": list(self.affected_thread_ids),
            "frontend_reference_count": self.frontend_reference_count,
            "frontend_residual_count": self.frontend_residual_count,
            "frontend_references_preserved": self.frontend_references_preserved,
            "frontend_database_paths": list(self.frontend_database_paths),
            "frontend_reference_evidence": [
                _json_value(value)
                for value in self.frontend_reference_evidence
            ],
        }


@dataclass(frozen=True)
class CandidateAction:
    action_id: str
    kind: ActionKind
    target: TargetRef
    risk: RiskLevel
    available: bool
    unavailable_reason: str | None
    impact: ActionImpact
    snapshot_fingerprint: str
    observation_ids: tuple[str, ...] = ()
    requires_explicit_selection: bool = False
    resource_kind: str = "conversation"
    legacy_inventory: LegacyIndexInventory | None = None

    @property
    def action_kind(self) -> ActionKind:
        return self.kind

    @property
    def executable(self) -> bool:
        return self.available and self.kind is not ActionKind.KEEP

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "target": self.target.to_dict(),
            "risk": self.risk.value,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "impact": self.impact.to_dict(),
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "observation_ids": list(self.observation_ids),
            "requires_explicit_selection": self.requires_explicit_selection,
            "resource": (
                {
                    "kind": "legacy_index",
                    "path": self.impact.resource_path,
                    "inventory": (
                        self.legacy_inventory.to_dict()
                        if self.legacy_inventory is not None
                        else None
                    ),
                }
                if self.resource_kind == "legacy_index"
                else {
                    "kind": "conversation",
                    "target": self.target.to_dict(),
                }
            ),
        }


@dataclass(frozen=True)
class PlannedAction:
    action_id: str
    kind: ActionKind
    target: TargetRef
    snapshot_fingerprint: str

    @classmethod
    def from_candidate(cls, action: CandidateAction) -> PlannedAction:
        return cls(
            action_id=action.action_id,
            kind=action.kind,
            target=action.target,
            snapshot_fingerprint=action.snapshot_fingerprint,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "target": self.target.to_dict(),
            "snapshot_fingerprint": self.snapshot_fingerprint,
        }


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    target: TargetRef
    status: ResultStatus
    error: str | None = None
    warnings: tuple[str, ...] = ()
    remaining_artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "target": self.target.to_dict(),
            "status": self.status.value,
            "error": self.error,
            "warnings": list(self.warnings),
            "remaining_artifacts": list(self.remaining_artifacts),
        }


@dataclass(frozen=True)
class ConversationCatalogEntry:
    """One storage-qualified entry in the plan's identity catalog."""

    target: TargetRef
    summary: ConversationSummary

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "summary": self.summary.to_dict(),
        }


@dataclass(frozen=True)
class CleanupPlan:
    storages: tuple[StorageLocation, ...] = ()
    conversations: tuple[ConversationCatalogEntry, ...] = ()
    observations: tuple[Observation, ...] = ()
    actions: tuple[CandidateAction, ...] = ()
    planned_actions: tuple[PlannedAction, ...] = ()
    errors: tuple[str, ...] = ()
    plan_fingerprint: str = ""

    @property
    def candidate_actions(self) -> tuple[CandidateAction, ...]:
        return self.actions

    @property
    def scan_complete(self) -> bool:
        return not self.errors and all(
            storage.scan_status is ScanStatus.OK for storage in self.storages
        )

    def with_selected_actions(self, action_ids: Iterable[str]) -> CleanupPlan:
        requested = tuple(dict.fromkeys(action_ids))
        by_id = {action.action_id: action for action in self.actions}
        missing = [action_id for action_id in requested if action_id not in by_id]
        if missing:
            raise ValueError(f"Unknown action ID(s): {', '.join(missing)}")
        unavailable = [by_id[action_id] for action_id in requested if not by_id[action_id].available]
        if unavailable:
            reasons = "; ".join(
                f"{action.action_id}: {action.unavailable_reason or 'unavailable'}"
                for action in unavailable
            )
            raise ValueError(f"Unavailable action(s): {reasons}")
        return replace(
            self,
            planned_actions=tuple(
                PlannedAction.from_candidate(by_id[action_id])
                for action_id in requested
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        conversation_lookup = {
            (
                entry.target.storage_id,
                entry.target.thread_id,
            ): entry.summary
            for entry in self.conversations
        }
        return {
            "storage_count": len(self.storages),
            "conversation_count": len(self.conversations),
            "observation_count": len(self.observations),
            "action_count": len(self.actions),
            "storages": [storage.to_dict() for storage in self.storages],
            "conversations": [
                conversation.to_dict()
                for conversation in self.conversations
            ],
            "observations": [
                observation.to_dict() for observation in self.observations
            ],
            "actions": [action.to_dict() for action in self.actions],
            "action_conversation_views": [
                {
                    "action_id": action.action_id,
                    "affected_conversations": [
                        {
                            "relationship": (
                                "root"
                                if thread_id == action.target.thread_id
                                else "descendant"
                            ),
                            "target": TargetRef(
                                action.target.storage_id,
                                thread_id,
                            ).to_dict(),
                            "summary": (
                                summary.to_dict()
                                if (
                                    summary := conversation_lookup.get(
                                        (
                                            action.target.storage_id,
                                            thread_id,
                                        )
                                    )
                                )
                                is not None
                                else None
                            ),
                        }
                        for thread_id in action.impact.affected_thread_ids
                    ],
                }
                for action in self.actions
                if action.kind is ActionKind.DELETE_CONVERSATION
            ],
            "planned_actions": [
                action.to_dict() for action in self.planned_actions
            ],
            "errors": list(self.errors),
            "scan_complete": self.scan_complete,
            "plan_fingerprint": self.plan_fingerprint,
        }


RolloutReader = Callable[[Path, str], Sequence[RolloutRecord]]
IndexReader = Callable[..., Mapping[str, Mapping[str, Any]]]
DescendantReader = Callable[..., Mapping[str, set[str]]]


@dataclass
class _StorageEvidence:
    path: Path
    bin_hint: Path | None = None
    normalized_bin_hints: set[str] = field(default_factory=set)
    has_findings: bool = False
    errors: list[str] = field(default_factory=list)
    descendants: dict[str, set[str]] = field(default_factory=dict)
    indexed_ids: set[str] = field(default_factory=set)
    current_indexed_ids: set[str] = field(default_factory=set)
    current_index_rows: dict[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    conversation_summaries: dict[str, ConversationSummary] = field(
        default_factory=dict
    )
    legacy_inventory: LegacyIndexInventory | None = None
    legacy_inventory_error: str | None = None
    indexed_rollout_paths: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    verified_rollout_paths: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    rollout_originators: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    rollout_source_identities: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    current_rollout_records: dict[str, list[RolloutRecord]] = field(
        default_factory=lambda: defaultdict(list)
    )
    rollout_state_fingerprints: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    rollout_fingerprint_errors: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    rollout_paths: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )


def normalize_storage_path(path: str | os.PathLike[str]) -> str:
    """Return the normalized absolute Codex data-directory identity."""

    return canonical_existing_path_key(path)


def storage_id_for_path(path: str | os.PathLike[str]) -> str:
    normalized = normalize_storage_path(path)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"storage-{digest}"


def build_cleanup_plan(
    source: Any,
    *,
    rollout_reader: RolloutReader = find_thread_rollouts,
    index_reader: IndexReader = read_thread_index,
    descendant_reader: DescendantReader = read_spawn_descendants,
) -> CleanupPlan:
    """Build immutable cleanup candidates from a ScanReport or Findings.

    The function deliberately keeps Finding as the adapter evidence format.
    It expands compatibility-era ``additional_findings`` entries into their
    own observations before grouping targets by storage and full thread ID.
    """

    findings, scan_failures = _coerce_source(source)
    storage_evidence: dict[str, _StorageEvidence] = {}
    findings_by_target: dict[TargetRef, list[Finding]] = defaultdict(list)

    for finding in findings:
        normalized_home = normalize_storage_path(finding.codex_home)
        storage_id = storage_id_for_path(normalized_home)
        evidence = storage_evidence.setdefault(
            storage_id,
            _StorageEvidence(path=Path(normalized_home)),
        )
        evidence.has_findings = True
        for bin_hint_candidate in _finding_bin_hint_candidates(finding):
            raw_bin_hint = os.fspath(bin_hint_candidate)
            if raw_bin_hint:
                evidence.normalized_bin_hints.add(
                    normalize_storage_path(raw_bin_hint)
                )
        target = TargetRef(storage_id=storage_id, thread_id=finding.thread_id)
        findings_by_target[target].append(finding)

    for evidence in storage_evidence.values():
        normalized_bin_hints = sorted(evidence.normalized_bin_hints)
        if len(normalized_bin_hints) == 1:
            evidence.bin_hint = Path(normalized_bin_hints[0])
        elif len(normalized_bin_hints) > 1:
            evidence.bin_hint = None
            evidence.errors.append(
                "Conflicting Codex executable hints were reported for this "
                "data directory: "
                + ", ".join(normalized_bin_hints)
            )

    global_errors = _assign_scan_failures(scan_failures, storage_evidence)
    observations = _make_observations(findings_by_target)

    targets_by_storage: dict[str, list[TargetRef]] = defaultdict(list)
    for target in findings_by_target:
        targets_by_storage[target.storage_id].append(target)

    for storage_id, targets in targets_by_storage.items():
        evidence = storage_evidence[storage_id]
        thread_ids = sorted({target.thread_id for target in targets})
        has_legacy_resource = any(
            finding.details.get("finding_type") == "legacy_index_only"
            for target in targets
            for finding in findings_by_target.get(target, ())
        )
        if has_legacy_resource:
            try:
                evidence.legacy_inventory = inventory_legacy_index(
                    evidence.path
                )
            except Exception as exc:
                evidence.legacy_inventory_error = _error_text(exc)
        try:
            descendants = descendant_reader(evidence.path, thread_ids, strict=True)
            evidence.descendants = {
                thread_id: set(descendants.get(thread_id, set()))
                for thread_id in thread_ids
            }
        except Exception as exc:
            evidence.errors.append(
                f"Could not inspect associated task conversations: {_error_text(exc)}"
            )
            evidence.descendants = {thread_id: set() for thread_id in thread_ids}

        affected_ids = set(thread_ids)
        for child_ids in evidence.descendants.values():
            affected_ids.update(child_ids)

        try:
            rows = index_reader(evidence.path, affected_ids, strict=True)
            evidence.indexed_ids.update(
                thread_id for thread_id in affected_ids if thread_id in rows
            )
            evidence.current_indexed_ids.update(
                thread_id for thread_id in affected_ids if thread_id in rows
            )
            for thread_id in sorted(affected_ids):
                row = rows.get(thread_id)
                if not isinstance(row, Mapping):
                    continue
                evidence.current_index_rows[thread_id] = dict(row)
                indexed_path = _indexed_rollout_path(evidence.path, row)
                if indexed_path is None:
                    continue
                try:
                    is_file = indexed_path.is_file()
                except OSError as exc:
                    evidence.errors.append(
                        "Could not inspect the indexed conversation content "
                        f"path for {thread_id}: {_error_text(exc)}"
                    )
                    continue
                if is_file:
                    normalized_path = _normalize_artifact_path(indexed_path)
                    evidence.indexed_rollout_paths[thread_id].add(normalized_path)
                    evidence.rollout_paths[thread_id].add(normalized_path)
        except Exception as exc:
            evidence.errors.append(
                f"Could not inspect conversation list records: {_error_text(exc)}"
            )

        for thread_id in sorted(affected_ids):
            try:
                records = rollout_reader(evidence.path, thread_id)
            except Exception as exc:
                evidence.errors.append(
                    "Could not inspect conversation content files for "
                    f"{thread_id}: {_error_text(exc)}"
                )
                continue
            for record in records:
                record_path = Path(record.path)
                try:
                    is_file = record_path.is_file()
                except OSError as exc:
                    evidence.errors.append(
                        "Could not inspect the conversation content path for "
                        f"{thread_id}: {_error_text(exc)}"
                    )
                    continue
                if not is_file:
                    continue
                normalized_path = _normalize_artifact_path(record.path)
                evidence.rollout_paths[thread_id].add(normalized_path)
                evidence.current_rollout_records[thread_id].append(record)
                try:
                    state_fingerprint = rollout_state_fingerprint(record)
                except Exception as exc:
                    evidence.rollout_fingerprint_errors[thread_id].append(
                        f"{normalized_path}: {_error_text(exc)}"
                    )
                else:
                    evidence.rollout_state_fingerprints[thread_id].add(
                        state_fingerprint
                    )
                if record.thread_id == thread_id:
                    evidence.verified_rollout_paths[thread_id].add(
                        normalized_path
                    )
                    evidence.rollout_originators[thread_id].add(
                        record.originator or "<missing>"
                    )
                    evidence.rollout_source_identities[thread_id].add(
                        json.dumps(
                            _json_value(record.source),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )

        try:
            evidence.conversation_summaries.update(
                read_conversation_summaries(
                    evidence.path,
                    affected_ids,
                    rollout_records_by_thread=(
                        evidence.current_rollout_records
                    ),
                    legacy_names=read_legacy_thread_names(
                        evidence.path,
                        affected_ids,
                    ),
                    strict=True,
                )
            )
        except Exception as exc:
            evidence.errors.append(
                "Could not inspect conversation display metadata: "
                f"{_error_text(exc)}"
            )

        # A Desktop-only ghost no longer has native metadata to name it. Use
        # the exact local host-catalog title strictly as display/approval
        # evidence; it does not make the ID a native thread/delete target.
        for target in targets:
            summary = evidence.conversation_summaries.get(target.thread_id)
            if summary is None or summary.display_name is not None:
                continue
            desktop_title = next(
                (
                    title
                    for finding in findings_by_target.get(target, ())
                    if finding.details.get("finding_type")
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
            evidence.conversation_summaries[target.thread_id] = replace(
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

    # Adapter paths remain in Observation identity even after disappearing,
    # but ActionImpact is an exact approval snapshot of files confirmed to
    # exist while this plan is generated.
    for observation in observations:
        evidence = storage_evidence[observation.target.storage_id]
        for raw_path in observation.rollout_paths:
            path = Path(raw_path)
            try:
                is_file = path.is_file()
            except OSError as exc:
                evidence.errors.append(
                    "Could not inspect an adapter-reported conversation "
                    f"content path: {_error_text(exc)}"
                )
                continue
            if is_file:
                evidence.rollout_paths[observation.target.thread_id].add(
                    _normalize_artifact_path(path)
                )
        if observation.codex_indexed:
            evidence.indexed_ids.add(observation.target.thread_id)

    observations = _enrich_observations_with_index_paths(
        observations,
        storage_evidence,
    )
    observations_by_target: dict[TargetRef, list[Observation]] = defaultdict(list)
    for observation in observations:
        observations_by_target[observation.target].append(observation)

    actions: list[CandidateAction] = []
    unscoped_reason = (
        "Cleanup planning is blocked because one or more scan failures could "
        "not be assigned to a Codex data directory."
        if global_errors
        else None
    )
    for target in sorted(
        findings_by_target,
        key=lambda item: (item.storage_id, item.thread_id),
    ):
        target_observations = observations_by_target[target]
        evidence = storage_evidence[target.storage_id]
        actions.extend(
            _candidate_actions(
                target,
                target_observations,
                evidence,
                observations_by_target=observations_by_target,
                unscoped_reason=unscoped_reason,
            )
        )

    storages = tuple(
        _storage_location(storage_id, evidence)
        for storage_id, evidence in sorted(storage_evidence.items())
    )
    observation_tuple = tuple(observations)
    action_tuple = tuple(
        sorted(
            actions,
            key=lambda action: (
                action.target.storage_id,
                action.target.thread_id,
                action.kind.value,
            ),
        )
    )
    legacy_targets = {
        observation.target
        for observation in observations
        if observation.finding_type == "legacy_index_only"
    }
    conversation_tuple = tuple(
        ConversationCatalogEntry(
            target=TargetRef(
                storage_id=storage_id,
                thread_id=thread_id,
            ),
            summary=summary,
        )
        for storage_id, evidence in sorted(storage_evidence.items())
        for thread_id, summary in sorted(
            evidence.conversation_summaries.items()
        )
        if TargetRef(storage_id, thread_id) not in legacy_targets
    )
    fingerprint = _fingerprint(
        {
            "storages": [storage.to_dict() for storage in storages],
            "conversations": [
                conversation.to_dict()
                for conversation in conversation_tuple
            ],
            "actions": [
                {
                    "action_id": action.action_id,
                    "snapshot_fingerprint": action.snapshot_fingerprint,
                }
                for action in action_tuple
            ],
            "errors": global_errors,
        }
    )
    return CleanupPlan(
        storages=storages,
        conversations=conversation_tuple,
        observations=observation_tuple,
        actions=action_tuple,
        errors=tuple(global_errors),
        plan_fingerprint=fingerprint,
    )


def _finding_bin_hint_candidates(
    finding: Finding,
) -> tuple[str | os.PathLike[str], ...]:
    candidates: list[str | os.PathLike[str]] = []
    if finding.codex_bin_hint is not None:
        candidates.append(finding.codex_bin_hint)
    compatibility_candidates = finding.details.get(
        "codex_bin_hint_candidates"
    )
    if isinstance(
        compatibility_candidates,
        (list, tuple, set, frozenset),
    ):
        candidates.extend(
            candidate
            for candidate in compatibility_candidates
            if isinstance(candidate, (str, os.PathLike))
        )
    return tuple(candidates)


generate_cleanup_plan = build_cleanup_plan


def _coerce_source(source: Any) -> tuple[list[Finding], list[Any]]:
    if isinstance(source, Finding):
        return [source], []
    if hasattr(source, "findings"):
        findings = list(getattr(source, "findings"))
        failures = list(getattr(source, "errors", ()))
    else:
        findings = list(source)
        failures = []
    unsupported = [
        item for item in findings if not isinstance(item, Finding)
    ]
    if unsupported:
        raise TypeError(
            "Cleanup planning accepts Finding objects; got "
            f"{type(unsupported[0]).__name__}"
        )
    return findings, failures


def _assign_scan_failures(
    failures: Sequence[Any],
    storage_evidence: MutableMapping[str, _StorageEvidence],
) -> list[str]:
    unassigned: list[str] = []
    for failure in failures:
        rendered = _scan_failure_text(failure)
        raw_home = (
            failure.get("codex_home")
            if isinstance(failure, Mapping)
            else getattr(failure, "codex_home", None)
        )
        if raw_home:
            storage_id = storage_id_for_path(raw_home)
            normalized_home = normalize_storage_path(raw_home)
            evidence = storage_evidence.setdefault(
                storage_id,
                _StorageEvidence(path=Path(normalized_home)),
            )
            evidence.errors.append(rendered)
            continue
        unassigned.append(rendered)
    return unassigned


def _scan_failure_text(failure: Any) -> str:
    if isinstance(failure, Mapping):
        platform = failure.get("platform", "scanner")
        message = failure.get("message") or failure
    else:
        platform = getattr(failure, "platform", "scanner")
        message = getattr(failure, "message", None) or failure
    return f"{platform}: {message}"


def _make_observations(
    findings_by_target: Mapping[TargetRef, Sequence[Finding]],
) -> list[Observation]:
    observations: list[Observation] = []
    for target, findings in findings_by_target.items():
        for finding in findings:
            entries = [
                {
                    "platform": finding.platform,
                    "platform_session_id": finding.platform_session_id,
                    "reason": finding.reason,
                    "details": finding.details,
                    "platform_db": str(finding.platform_db),
                    "primary": True,
                }
            ]
            additional = finding.details.get("additional_findings")
            if isinstance(additional, list):
                for item in additional:
                    if not isinstance(item, Mapping):
                        continue
                    entries.append(
                        {
                            "platform": str(item.get("platform", finding.platform)),
                            "platform_session_id": str(
                                item.get(
                                    "platform_session_id",
                                    finding.platform_session_id,
                                )
                            ),
                            "reason": str(item.get("reason", finding.reason)),
                            "details": (
                                item.get("details")
                                if isinstance(item.get("details"), Mapping)
                                else {}
                            ),
                            "platform_db": str(
                                item.get(
                                    "platform_db",
                                    finding.platform_db,
                                )
                            ),
                            "primary": False,
                        }
                    )

            for entry in entries:
                details = dict(entry["details"])
                # The wrapper field is aggregation metadata, not evidence owned
                # by the primary observation.
                details.pop("additional_findings", None)
                rollout_paths = set(_detail_rollout_paths(details))
                if entry["primary"] and finding.rollout is not None:
                    rollout_paths.add(
                        _normalize_artifact_path(finding.rollout.path)
                    )
                finding_type = _finding_type(
                    str(entry["platform"]),
                    str(entry["reason"]),
                    details,
                )
                observations.append(
                    Observation(
                        observation_id="",
                        target=target,
                        platform=str(entry["platform"]),
                        platform_session_id=str(entry["platform_session_id"]),
                        finding_type=finding_type,
                        reason=str(entry["reason"]),
                        details=details,
                        codex_indexed=finding.codex_indexed,
                        codex_archived=finding.codex_archived,
                        rollout_paths=tuple(sorted(rollout_paths)),
                        platform_db=str(entry["platform_db"]),
                    )
                )
    return _reidentify_observations(observations)


def _enrich_observations_with_index_paths(
    observations: Sequence[Observation],
    storage_evidence: Mapping[str, _StorageEvidence],
) -> list[Observation]:
    enriched: list[Observation] = []
    for observation in observations:
        evidence = storage_evidence[observation.target.storage_id]
        paths = set(observation.rollout_paths)
        paths.update(
            evidence.indexed_rollout_paths.get(
                observation.target.thread_id,
                set(),
            )
        )
        enriched.append(
            replace(
                observation,
                rollout_paths=tuple(sorted(paths)),
            )
        )
    return _reidentify_observations(enriched)


def _reidentify_observations(
    observations: Sequence[Observation],
) -> list[Observation]:
    identified: list[Observation] = []
    id_counts: Counter[str] = Counter()
    for observation in observations:
        identity = {
            "target": observation.target.to_dict(),
            "platform": observation.platform,
            "platform_session_id": observation.platform_session_id,
            "finding_type": observation.finding_type,
            "reason": observation.reason,
            "details": observation.details,
            "platform_db": observation.platform_db,
            "codex_indexed": observation.codex_indexed,
            "rollout_paths": list(observation.rollout_paths),
        }
        base_id = f"observation-{_fingerprint(identity)[:20]}"
        occurrence = id_counts[base_id]
        id_counts[base_id] += 1
        observation_id = (
            base_id if occurrence == 0 else f"{base_id}-{occurrence + 1}"
        )
        identified.append(
            replace(
                observation,
                observation_id=observation_id,
            )
        )
    return identified


def _finding_type(
    platform: str,
    reason: str,
    details: Mapping[str, Any],
) -> str:
    explicit = details.get("finding_type")
    if isinstance(explicit, str) and explicit:
        return explicit
    lowered = f"{platform} {reason}".lower()
    if platform.lower() in {"aionui", "cindy"} or (
        "frontend" in lowered and ("deleted" in lowered or "gone" in lowered)
    ):
        return "frontend_deleted_reference"
    slug = re.sub(r"[^a-z0-9]+", "_", reason.lower()).strip("_")
    return slug or "unspecified"


def _detail_rollout_paths(details: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for key in (
        "rollout_path",
        "rollout_paths",
        "actual_rollout_paths",
        "alternate_rollout_paths",
        "artifact_path",
        "artifact_paths",
        "remaining_paths",
    ):
        value = details.get(key)
        values = value if isinstance(value, (list, tuple, set)) else (value,)
        for item in values:
            if isinstance(item, (str, os.PathLike)) and os.fspath(item):
                result.add(_normalize_artifact_path(item))
    return result


def _candidate_actions(
    target: TargetRef,
    observations: Sequence[Observation],
    evidence: _StorageEvidence,
    *,
    observations_by_target: Mapping[TargetRef, Sequence[Observation]],
    unscoped_reason: str | None,
) -> list[CandidateAction]:
    kinds = {ActionKind.KEEP}
    for observation in observations:
        kinds.update(_action_kinds_for_observation(observation))
    include_frontend_actions = (
        ActionKind.REMOVE_FRONTEND_REFERENCE in kinds
    )
    kinds.discard(ActionKind.REMOVE_FRONTEND_REFERENCE)
    if (
        any(
            observation.finding_type == "residual_spawn_edge"
            for observation in observations
        )
        and _target_has_exact_native_artifact(target, evidence)
    ):
        kinds.add(ActionKind.DELETE_CONVERSATION)

    descendants = tuple(sorted(evidence.descendants.get(target.thread_id, set())))
    affected_ids = (target.thread_id,) + descendants
    affected_observations = list(observations)
    descendant_observations: dict[str, Sequence[Observation]] = {}
    for descendant_id in descendants:
        descendant_target = TargetRef(
            storage_id=target.storage_id,
            thread_id=descendant_id,
        )
        current = tuple(observations_by_target.get(descendant_target, ()))
        if current:
            descendant_observations[descendant_id] = current
            affected_observations.extend(current)
    descendant_block_reason = _descendant_delete_block_reason(
        descendant_observations
    )
    if descendant_block_reason is None:
        descendant_block_reason = _descendant_evidence_block_reason(
            descendants,
            affected_ids,
            evidence,
        )
    rollout_paths = tuple(
        sorted(
            {
                path
                for thread_id in affected_ids
                for path in evidence.rollout_paths.get(thread_id, set())
            }
        )
    )
    live_reference_count = sum(
        _as_nonnegative_int(obs.details.get("live_reference_count"))
        + int(obs.details.get("live_reference_self") is True)
        + _as_nonnegative_int(obs.details.get("live_descendant_reference_count"))
        for obs in affected_observations
    )
    frontend_residual_count = sum(
        obs.finding_type == "frontend_deleted_reference"
        for obs in affected_observations
    )
    indexed_thread_ids = tuple(
        sorted(
            thread_id
            for thread_id in affected_ids
            if thread_id in evidence.current_indexed_ids
        )
    )
    rollout_state_fingerprints = tuple(
        sorted(
            {
                state_fingerprint
                for thread_id in affected_ids
                for state_fingerprint in evidence.rollout_state_fingerprints.get(
                    thread_id,
                    set(),
                )
            }
        )
    )
    fingerprint_errors = tuple(
        sorted(
            f"{thread_id}: {message}"
            for thread_id in affected_ids
            for message in evidence.rollout_fingerprint_errors.get(
                thread_id,
                (),
            )
        )
    )
    fingerprint_error = (
        "; ".join(fingerprint_errors) if fingerprint_errors else None
    )
    is_legacy_resource = any(
        obs.finding_type == "legacy_index_only"
        for obs in observations
    )
    conversation_metadata_fingerprints = tuple(
        sorted(
            f"{thread_id}={summary.metadata_fingerprint}"
            for thread_id in affected_ids
            if not is_legacy_resource
            if (
                summary := evidence.conversation_summaries.get(thread_id)
            )
            is not None
        )
    )
    impact = ActionImpact(
        index_record_count=len(indexed_thread_ids),
        rollout_file_count=len(rollout_paths),
        rollout_paths=rollout_paths,
        descendant_thread_ids=descendants,
        affected_thread_ids=affected_ids,
        frontend_reference_count=live_reference_count,
        frontend_residual_count=frontend_residual_count,
        indexed_thread_ids=indexed_thread_ids,
        rollout_state_fingerprints=rollout_state_fingerprints,
        conversation_metadata_fingerprints=(
            conversation_metadata_fingerprints
        ),
        fingerprint_error=fingerprint_error,
        desktop_catalog_record_count=sum(
            _as_nonnegative_int(
                obs.details.get("desktop_catalog_record_count")
            )
            for obs in affected_observations
            if obs.finding_type == "desktop_state_orphan"
        ),
        desktop_global_state_reference_count=sum(
            _as_nonnegative_int(
                obs.details.get("desktop_global_state_reference_count")
            )
            for obs in affected_observations
            if obs.finding_type == "desktop_state_orphan"
        ),
        desktop_database_paths=tuple(
            sorted(
                {
                    str(path)
                    for obs in affected_observations
                    if obs.finding_type == "desktop_state_orphan"
                    if (path := obs.details.get("desktop_database"))
                }
            )
        ),
        desktop_global_state_paths=tuple(
            sorted(
                {
                    str(path)
                    for obs in affected_observations
                    if obs.finding_type == "desktop_state_orphan"
                    for path in obs.details.get(
                        "desktop_global_state_paths", ()
                    )
                    if isinstance(path, (str, os.PathLike))
                }
            )
        ),
    )
    if is_legacy_resource:
        inventory = evidence.legacy_inventory
        adapter_path = next(
            (
                obs.details.get("legacy_index_path")
                for obs in observations
                if obs.finding_type == "legacy_index_only"
            ),
            None,
        )
        impact = ActionImpact(
            resource_path=(
                str(inventory.index_path)
                if inventory is not None
                else str(adapter_path) if adapter_path else None
            ),
            legacy_residual_thread_ids=(
                inventory.residual_thread_ids
                if inventory is not None
                else ()
            ),
            legacy_residual_line_count=(
                inventory.residual_line_count
                if inventory is not None
                else 0
            ),
            legacy_original_sha256=(
                inventory.original_sha256
                if inventory is not None
                else None
            ),
            legacy_expected_sha256=(
                inventory.expected_sha256
                if inventory is not None
                else None
            ),
        )
    finding_types = sorted(
        {obs.finding_type for obs in affected_observations}
    )
    snapshot = _fingerprint(
        {
            "storage_path": normalize_storage_path(evidence.path),
            "thread_id": target.thread_id,
            "finding_types": finding_types,
            "indexed_thread_ids": list(indexed_thread_ids),
            "rollout_paths": list(rollout_paths),
            "descendant_thread_ids": descendants,
            "active_reference": bool(live_reference_count),
            "frontend_residual_count": frontend_residual_count,
            "observation_ids": sorted(
                obs.observation_id for obs in affected_observations
            ),
            "current_rollout_evidence": _current_rollout_snapshot(
                affected_ids,
                evidence,
            ),
            "current_indexed_rollout_paths": {
                thread_id: sorted(
                    evidence.indexed_rollout_paths.get(thread_id, set())
                )
                for thread_id in affected_ids
            },
            "current_index_rows": {
                thread_id: _json_value(
                    evidence.current_index_rows.get(thread_id)
                )
                for thread_id in affected_ids
            },
            "current_verified_rollout_paths": {
                thread_id: sorted(
                    evidence.verified_rollout_paths.get(thread_id, set())
                )
                for thread_id in affected_ids
            },
            "rollout_state_fingerprints": list(
                rollout_state_fingerprints
            ),
            "conversation_metadata": {
                thread_id: summary.approval_payload()
                for thread_id in affected_ids
                if not is_legacy_resource
                if (
                    summary := evidence.conversation_summaries.get(
                        thread_id
                    )
                )
                is not None
            },
            "conversation_metadata_fingerprints": list(
                conversation_metadata_fingerprints
            ),
            "fingerprint_error": fingerprint_error,
        }
    )
    if is_legacy_resource and evidence.legacy_inventory is not None:
        snapshot = evidence.legacy_inventory.snapshot_fingerprint
    observation_ids = tuple(
        obs.observation_id for obs in affected_observations
    )
    actions: list[CandidateAction] = []
    for kind in sorted(kinds, key=lambda item: item.value):
        unavailable_reason = _unavailable_reason(
            kind,
            target,
            affected_observations,
            evidence,
            impact,
            descendant_block_reason=descendant_block_reason,
            unscoped_reason=unscoped_reason,
        )
        available = unavailable_reason is None
        risk = _risk_level(
            kind,
            affected_observations,
            impact,
            unavailable_reason=unavailable_reason,
        )
        action_impact = replace(
            impact,
            frontend_references_preserved=(
                kind
                not in {
                    ActionKind.REMOVE_FRONTEND_REFERENCE,
                    ActionKind.REMOVE_DESKTOP_STATE,
                }
            ),
        )
        actions.append(
            CandidateAction(
                action_id=_action_id(target, kind),
                kind=kind,
                target=target,
                risk=risk,
                available=available,
                unavailable_reason=unavailable_reason,
                impact=action_impact,
                snapshot_fingerprint=snapshot,
                observation_ids=observation_ids,
                requires_explicit_selection=(
                    (
                        kind is ActionKind.DELETE_CONVERSATION
                        and (
                            bool(descendants)
                            or bool(
                                set(finding_types)
                                & {
                                    "duplicate_rollout",
                                    "index_rollout_path_mismatch",
                                    "residual_spawn_edge",
                                }
                            )
                        )
                    )
                    or any(
                        obs.details.get("requires_explicit_selection") is True
                        for obs in affected_observations
                    )
                    or kind is ActionKind.REPAIR_LEGACY_INDEX
                    or kind is ActionKind.REMOVE_DESKTOP_STATE
                ),
                legacy_inventory=(
                    evidence.legacy_inventory
                    if is_legacy_resource
                    else None
                ),
                resource_kind=(
                    "legacy_index"
                    if is_legacy_resource
                    else "conversation"
                ),
            )
        )
    if include_frontend_actions:
        actions.extend(
            _frontend_reference_actions(
                target,
                observations,
                impact,
                evidence,
                unscoped_reason=unscoped_reason,
            )
        )
    return actions


def _frontend_reference_actions(
    target: TargetRef,
    observations: Sequence[Observation],
    base_impact: ActionImpact,
    storage_evidence: _StorageEvidence,
    *,
    unscoped_reason: str | None,
) -> list[CandidateAction]:
    groups: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for observation in observations:
        if observation.finding_type != "frontend_deleted_reference":
            continue
        reference = observation.details.get("frontend_reference")
        database = (
            str(reference.get("database") or "")
            if isinstance(reference, Mapping)
            else str(observation.platform_db or "")
        )
        groups[(observation.platform.lower(), database)].append(observation)

    actions: list[CandidateAction] = []
    for (platform, database), group in sorted(groups.items()):
        references = tuple(
            dict(reference)
            for observation in group
            if isinstance(
                (reference := observation.details.get("frontend_reference")),
                Mapping,
            )
        )
        unavailable_reason = unscoped_reason
        if unavailable_reason is None and storage_evidence.errors:
            unavailable_reason = (
                "Cleanup is blocked for this Codex data directory because "
                "current state could not be read completely: "
                + "; ".join(storage_evidence.errors)
            )
        if unavailable_reason is None and platform not in {"aionui", "cindy"}:
            unavailable_reason = (
                "This frontend has no supported exact reference writer."
            )
        if unavailable_reason is None and (
            not database or len(references) != len(group)
        ):
            unavailable_reason = (
                "The frontend reference has no complete physical-row evidence."
            )
        if unavailable_reason is None and any(
            observation.details.get("frontend_reference_cleanable") is not True
            for observation in group
        ):
            unavailable_reason = (
                "The frontend reference identity or ownership is not exact."
            )
        if unavailable_reason is None and any(
            str(reference.get("database") or "") != database
            or str(reference.get("platform") or "").lower() != platform
            for reference in references
        ):
            unavailable_reason = (
                "Frontend evidence does not agree on one physical database."
            )

        observation_ids = tuple(
            observation.observation_id for observation in group
        )
        snapshot = _fingerprint(
            {
                "storage_path": normalize_storage_path(storage_evidence.path),
                "target": target.to_dict(),
                "platform": platform,
                "database": database,
                "observation_ids": list(observation_ids),
                "references": list(references),
            }
        )
        action_impact = replace(
            base_impact,
            frontend_residual_count=len(group),
            frontend_references_preserved=False,
            frontend_database_paths=((database,) if database else ()),
            frontend_reference_evidence=references,
            resource_path=database or None,
        )
        action_id_digest = _fingerprint(
            {
                "target": target.to_dict(),
                "kind": ActionKind.REMOVE_FRONTEND_REFERENCE.value,
                "platform": platform,
                "database": database,
                "observation_ids": list(observation_ids),
            }
        )[:24]
        actions.append(
            CandidateAction(
                action_id=(
                    f"{ActionKind.REMOVE_FRONTEND_REFERENCE.value}-"
                    f"{action_id_digest}"
                ),
                kind=ActionKind.REMOVE_FRONTEND_REFERENCE,
                target=target,
                risk=(
                    RiskLevel.REVIEW
                    if unavailable_reason is None
                    else RiskLevel.BLOCKED
                ),
                available=unavailable_reason is None,
                unavailable_reason=unavailable_reason,
                impact=action_impact,
                snapshot_fingerprint=snapshot,
                observation_ids=observation_ids,
                requires_explicit_selection=True,
                resource_kind="frontend_reference",
            )
        )
    return actions


def _target_has_exact_native_artifact(
    target: TargetRef,
    evidence: _StorageEvidence,
) -> bool:
    return (
        target.thread_id in evidence.current_indexed_ids
        or bool(evidence.verified_rollout_paths.get(target.thread_id, set()))
    )


def _action_kinds_for_observation(
    observation: Observation,
) -> set[ActionKind]:
    finding_type = observation.finding_type
    if finding_type == "frontend_deleted_reference":
        result = {ActionKind.REMOVE_FRONTEND_REFERENCE}
        if observation.codex_indexed or observation.rollout_paths:
            result.add(ActionKind.DELETE_CONVERSATION)
        return result
    if finding_type == "index_missing_rollout":
        return {ActionKind.DELETE_CONVERSATION}
    if finding_type == "rollout_missing_index":
        return {ActionKind.DELETE_CONVERSATION}
    if finding_type == "duplicate_rollout":
        return {
            ActionKind.QUARANTINE_ARTIFACTS,
            ActionKind.DELETE_CONVERSATION,
        }
    if finding_type == "index_rollout_path_mismatch":
        return {
            ActionKind.REPAIR_INDEX_PATH,
            ActionKind.DELETE_CONVERSATION,
        }
    if finding_type == "index_rollout_metadata_mismatch":
        return {ActionKind.QUARANTINE_ARTIFACTS}
    if finding_type == "orphaned_subagent_thread":
        return {ActionKind.DELETE_CONVERSATION}
    if finding_type == "residual_spawn_edge":
        return {ActionKind.REMOVE_BROKEN_RELATION}
    if finding_type == "legacy_index_only":
        return {ActionKind.REPAIR_LEGACY_INDEX}
    if finding_type == "desktop_state_orphan":
        return {ActionKind.REMOVE_DESKTOP_STATE}

    result: set[ActionKind] = set()
    if (
        observation.codex_indexed
        or observation.rollout_paths
    ):
        if observation.details.get("thread_delete_supported") is not False:
            result.add(ActionKind.DELETE_CONVERSATION)
    if observation.platform.lower() != "native":
        result.add(ActionKind.REMOVE_FRONTEND_REFERENCE)
    return result


_UNIMPLEMENTED_REASONS = {
    ActionKind.REMOVE_BROKEN_RELATION: (
        "Removing an invalid conversation relation is not implemented; it "
        "requires a verified database backup, schema check and transaction."
    ),
    ActionKind.REPAIR_INDEX_PATH: (
        "Repairing a conversation list path is not implemented; metadata "
        "identity and a recoverable database backup must be revalidated."
    ),
    ActionKind.QUARANTINE_ARTIFACTS: (
        "Artifact quarantine is not implemented; a recoverable quarantine "
        "manifest and restore workflow are required."
    ),
}


def _unavailable_reason(
    kind: ActionKind,
    target: TargetRef,
    observations: Sequence[Observation],
    evidence: _StorageEvidence,
    impact: ActionImpact,
    *,
    descendant_block_reason: str | None,
    unscoped_reason: str | None,
) -> str | None:
    if kind is ActionKind.KEEP:
        return None
    if unscoped_reason is not None:
        return unscoped_reason
    if evidence.errors:
        return (
            "Cleanup is blocked for this Codex data directory because current "
            f"state could not be read completely: {'; '.join(evidence.errors)}"
        )
    if kind is ActionKind.REPAIR_LEGACY_INDEX:
        if evidence.legacy_inventory_error is not None:
            return (
                "Legacy aggregate index repair is blocked because strict "
                "inventory failed: "
                + evidence.legacy_inventory_error
            )
        inventory = evidence.legacy_inventory
        if inventory is None:
            return "No strict legacy aggregate index inventory is available."
        if not inventory.needs_repair:
            return "The legacy aggregate index no longer has residual entries."
        return None
    if kind is ActionKind.REMOVE_DESKTOP_STATE:
        matching = [
            observation
            for observation in observations
            if observation.finding_type == "desktop_state_orphan"
        ]
        if len(matching) != 1:
            return (
                "Codex Desktop cleanup requires exactly one current orphan "
                "host-state observation per target."
            )
        details = matching[0].details
        if details.get("cleanable") is not True:
            return "The Codex Desktop host-state observation is not cleanable."
        if details.get("desktop_host_id") != "local":
            return "Only host_id='local' Codex Desktop state can be cleaned."
        if not isinstance(
            details.get("desktop_state_snapshot_fingerprint"), str
        ):
            return "No exact Codex Desktop state snapshot is available."
        if _as_nonnegative_int(
            details.get("desktop_catalog_record_count")
        ) != 1:
            return "The target does not map to exactly one local Desktop row."
        return None
    if kind in _UNIMPLEMENTED_REASONS:
        return _UNIMPLEMENTED_REASONS[kind]
    if kind is not ActionKind.DELETE_CONVERSATION:
        return f"The {kind.value} action is not implemented."

    if impact.fingerprint_error is not None:
        return (
            "Deletion is blocked because current conversation content state "
            "could not be fingerprinted exactly: "
            + impact.fingerprint_error
        )
    if descendant_block_reason is not None:
        return descendant_block_reason
    if impact.frontend_reference_count:
        return (
            "The conversation or an associated task conversation is still "
            "referenced by an active frontend session."
        )
    current_identity_blocker = _current_identity_block_reason(
        impact.affected_thread_ids,
        evidence,
    )
    if current_identity_blocker is not None:
        return current_identity_blocker
    source_scope_blocker = _source_parent_scope_block_reason(
        impact.affected_thread_ids,
        observations,
        evidence,
    )
    if source_scope_blocker is not None:
        return source_scope_blocker
    finding_types = {obs.finding_type for obs in observations}
    integrity_types = {
        "duplicate_rollout",
        "index_rollout_path_mismatch",
    }
    integrity_delete = bool(finding_types & integrity_types)
    if "legacy_index_only" in finding_types:
        return (
            "A legacy aggregate index finding is not a real conversation ID "
            "and cannot be sent to thread/delete."
        )
    if "index_rollout_metadata_mismatch" in finding_types:
        return (
            "The indexed content file belongs to a different conversation; "
            "deletion is blocked to protect unrelated data."
        )
    residual_contract_issue = _residual_delete_contract_issue(
        target,
        observations,
        evidence,
    )
    if residual_contract_issue is not None:
        return (
            "Residual relation deletion is blocked because the exact native "
            f"child-target contract is not verified: {residual_contract_issue}"
        )
    capability_blocker: str | None = None
    for observation in observations:
        details = observation.details
        if details.get("originator_conflict") is True or details.get(
            "source_conflict"
        ) is True:
            return (
                "Conversation identity evidence conflicts with another owner "
                "or parent, so deletion is blocked."
            )
        ownership = details.get("ownership_status")
        if ownership in {
            "conflict",
            "insufficient",
            "unknown",
            "unconfirmed",
        }:
            return (
                "Conversation ownership is not confirmed for this Codex data "
                "directory, so deletion is blocked."
            )
        if details.get("needs_quarantine") is True:
            return (
                "The observation requires quarantine or manual identity "
                "review, so conversation deletion is blocked."
            )
        if details.get("cascade_check_available") is False:
            return (
                "Associated task conversation scope could not be verified for "
                "this target."
            )
        explicit_value = details.get("cleanup_blocked_reason")
        explicit = (
            explicit_value.strip()
            if isinstance(explicit_value, str) and explicit_value.strip()
            else None
        )
        blocker_codes = cleanup_blocker_codes(details)
        cascade_only = exact_blocker_codes(
            details,
            CASCADE_REQUIRES_EXPLICIT_SCOPE,
        )
        residual_relation_only = (
            observation.finding_type == "residual_spawn_edge"
            and exact_blocker_codes(
                details,
                STANDALONE_RELATION_CLEANUP_UNAVAILABLE,
            )
            and _target_has_exact_native_artifact(
                observation.target,
                evidence,
            )
        )
        integrity_soft_reason = (
            integrity_delete
            and observation.platform.lower() == "native"
            and observation.finding_type in integrity_types
            and exact_blocker_codes(details, INTEGRITY_REVIEW_REQUIRED)
        )
        if explicit is not None or blocker_codes:
            if cascade_only or residual_relation_only or integrity_soft_reason:
                pass
            else:
                return explicit or (
                    "Structured cleanup blocker codes prevent deletion: "
                    + ", ".join(sorted(blocker_codes))
                )
        capability_exception = (
            cascade_only or residual_relation_only or integrity_soft_reason
        )
        if (
            capability_blocker is None
            and not capability_exception
            and details.get("cleanable") is not True
        ):
            capability_blocker = (
                "The adapter did not explicitly mark this observation "
                "cleanable; deletion is fail-closed."
            )
        if (
            capability_blocker is None
            and not capability_exception
            and details.get("thread_delete_supported") is not True
        ):
            capability_blocker = (
                "The adapter did not explicitly confirm that official "
                "thread/delete can repair this observation."
            )
        if (
            integrity_delete
            and observation.finding_type not in integrity_types
            and details.get("cleanable") is False
            and not cascade_only
        ):
            return (
                "Another observation for this conversation is explicitly "
                "read-only or unsafe, so high-risk deletion is blocked."
            )
        if (
            details.get("thread_delete_supported") is False
            and not (
                integrity_delete
                and observation.finding_type in integrity_types
            )
            and (
                observation.finding_type
                not in {"residual_spawn_edge", "legacy_index_only"}
                or integrity_delete
            )
        ):
            return (
                "The adapter reports that official conversation deletion "
                "cannot safely repair this observation."
            )

    if integrity_delete:
        identity_blocker = _integrity_identity_block_reason(
            observations[0].target,
            observations,
            evidence,
        )
        if identity_blocker is not None:
            return identity_blocker

    if capability_blocker is not None:
        return capability_blocker

    if not (
        target_has_native_artifact := (
            bool(impact.index_record_count) or bool(impact.rollout_file_count)
        )
    ):
        return (
            "No native conversation list record or content file is available "
            "as a verified thread/delete target."
        )
    return None if target_has_native_artifact else "No deletion target is present."


def _descendant_delete_block_reason(
    observations_by_thread: Mapping[str, Sequence[Observation]],
) -> str | None:
    """Return a hard blocker inherited by a cascading parent deletion.

    A child remains an independent planning target.  Its own high-risk
    integrity deletion may be approvable, but a parent action must not silently
    absorb that approval or cascade through conflicting child evidence.
    """

    integrity_types = {
        "duplicate_rollout",
        "index_rollout_path_mismatch",
        "index_rollout_metadata_mismatch",
    }
    for thread_id in sorted(observations_by_thread):
        observations = observations_by_thread[thread_id]
        finding_types = {observation.finding_type for observation in observations}
        if finding_types & integrity_types:
            return (
                "Associated task conversation "
                f"{thread_id} has an integrity or path anomaly that must be "
                "reviewed and approved independently before a parent deletion."
            )
        for observation in observations:
            details = observation.details
            if details.get("originator_conflict") is True or details.get(
                "source_conflict"
            ) is True:
                return (
                    "Associated task conversation "
                    f"{thread_id} has conflicting identity or ownership "
                    "evidence."
                )
            if details.get("ownership_status") in {
                "conflict",
                "insufficient",
                "unknown",
                "unconfirmed",
            }:
                return (
                    "Associated task conversation "
                    f"{thread_id} does not have confirmed ownership."
                )
            if details.get("needs_quarantine") is True:
                return (
                    "Associated task conversation "
                    f"{thread_id} requires quarantine or manual identity "
                    "review."
                )
            if (
                details.get("cascade_check_available") is False
            ):
                return (
                    "Associated task conversation "
                    f"{thread_id} has an incomplete cascade scope."
                )
            explicit = details.get("cleanup_blocked_reason")
            cascade_only = exact_blocker_codes(
                details,
                CASCADE_REQUIRES_EXPLICIT_SCOPE,
            )
            blocker_codes = cleanup_blocker_codes(details)
            if (
                isinstance(explicit, str)
                and explicit.strip()
                and not cascade_only
            ):
                return (
                    "Associated task conversation "
                    f"{thread_id} is blocked: {explicit.strip()}"
                )
            if blocker_codes and not cascade_only:
                return (
                    "Associated task conversation "
                    f"{thread_id} has structured cleanup blockers: "
                    + ", ".join(sorted(blocker_codes))
                )
            if (
                details.get("cleanable") is False
                and not cascade_only
            ) or details.get("thread_delete_supported") is False:
                return (
                    "Associated task conversation "
                    f"{thread_id} is explicitly read-only or is not a safe "
                    "thread/delete target."
                )
    return None


def _descendant_evidence_block_reason(
    descendant_ids: Sequence[str],
    affected_ids: Sequence[str],
    evidence: _StorageEvidence,
) -> str | None:
    affected_set = set(affected_ids)
    for thread_id in descendant_ids:
        records = evidence.current_rollout_records.get(thread_id, ())
        mismatched_paths = sorted(
            {
                _normalize_artifact_path(record.path)
                for record in records
                if record.thread_id != thread_id
            }
        )
        if mismatched_paths:
            return (
                "Associated task conversation "
                f"{thread_id} has current content metadata for another "
                "conversation and must be reviewed independently: "
                + ", ".join(mismatched_paths)
            )

        exact_paths = {
            _normalize_artifact_path(record.path)
            for record in records
            if record.thread_id == thread_id
        }
        if len(exact_paths) > 1:
            return (
                "Associated task conversation "
                f"{thread_id} has multiple current content files and must be "
                "reviewed and approved independently."
            )

        source_parent_ids = {
            parent_id
            for record in records
            if record.thread_id == thread_id
            for parent_id in _structured_source_parent_ids(record.source)
        }
        allowed_parent_ids = affected_set - {thread_id}
        if (
            len(source_parent_ids) > 1
            or bool(source_parent_ids - allowed_parent_ids)
        ):
            return (
                "Associated task conversation "
                f"{thread_id} has source-parent metadata outside or "
                "conflicting with the approved cascade scope and must be "
                "reviewed independently."
            )
    return None


def _structured_source_parent_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return set()
    if not isinstance(value, Mapping):
        return set()
    if "subagent" in value:
        spawn_container = value.get("subagent")
    elif "thread_spawn" in value:
        spawn_container = value
    else:
        return set()
    if not isinstance(spawn_container, Mapping):
        return set()
    spawn = spawn_container.get("thread_spawn")
    if not isinstance(spawn, Mapping):
        return set()
    parent_id = spawn.get("parent_thread_id")
    if isinstance(parent_id, str) and parent_id:
        return {parent_id}
    return set()


def _current_rollout_snapshot(
    thread_ids: Sequence[str],
    evidence: _StorageEvidence,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for requested_id in thread_ids:
        entries = [
            {
                "path": _normalize_artifact_path(record.path),
                "metadata_thread_id": record.thread_id,
                "originator": record.originator,
                "source": _json_value(record.source),
                "cwd": record.cwd,
                "timestamp": record.timestamp,
                "archived": record.archived,
            }
            for record in evidence.current_rollout_records.get(requested_id, ())
        ]
        result[requested_id] = sorted(
            entries,
            key=lambda entry: (
                str(entry["path"]),
                str(entry["metadata_thread_id"]),
                json.dumps(
                    entry,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
    return result


def _residual_delete_contract_issue(
    target: TargetRef,
    observations: Sequence[Observation],
    evidence: _StorageEvidence,
) -> str | None:
    residual_observations = [
        observation
        for observation in observations
        if observation.target == target
        and observation.finding_type == "residual_spawn_edge"
    ]
    if not residual_observations:
        return None
    if len(residual_observations) != 1:
        return "the target does not have exactly one residual observation"

    observation = residual_observations[0]
    details = observation.details
    parent_id = details.get("parent_thread_id")
    if (
        observation.platform.lower() != "native"
        or details.get("child_thread_id") != target.thread_id
        or not isinstance(parent_id, str)
        or not parent_id
        or details.get("source_conflict") is not False
    ):
        return "the observation does not identify one matching native child edge"

    collection_types = (list, tuple, set, frozenset)
    declared_source_parent_ids = details.get("source_parent_ids")
    declared_subagent_evidence = details.get("subagent_evidence")
    if not isinstance(declared_source_parent_ids, collection_types):
        return "source_parent_ids is not an exact collection"
    if not isinstance(declared_subagent_evidence, collection_types):
        return "subagent_evidence is not an exact collection"

    declared_parent_set = {
        item
        for item in declared_source_parent_ids
        if isinstance(item, str) and item
    }
    current_source_parent_values = [
        record.source
        for record in evidence.current_rollout_records.get(
            target.thread_id,
            (),
        )
        if record.thread_id == target.thread_id
    ]
    indexed_row = evidence.current_index_rows.get(target.thread_id)
    if indexed_row is not None:
        current_source_parent_values.append(indexed_row.get("source"))
    current_source_parent_ids = {
        source_parent_id
        for value in current_source_parent_values
        for source_parent_id in _structured_source_parent_ids(value)
    }
    if (
        current_source_parent_ids != declared_parent_set
        or len(current_source_parent_ids) > 1
        or bool(current_source_parent_ids - {parent_id})
    ):
        return "current source parents differ from the matching edge"

    declared_evidence_set = {
        item
        for item in declared_subagent_evidence
        if isinstance(item, str) and item
    }
    if (
        _current_subagent_evidence(target.thread_id, evidence)
        != declared_evidence_set
    ):
        return "current subagent evidence differs from the observation"

    artifact_flag_names = (
        "parent_index_missing",
        "child_index_missing",
        "parent_rollout_present",
        "child_rollout_present",
    )
    if any(type(details.get(name)) is not bool for name in artifact_flag_names):
        return "parent and child artifact state is not fully boolean"
    current_child_index_missing = (
        target.thread_id not in evidence.current_indexed_ids
    )
    current_child_rollout_present = bool(
        evidence.rollout_paths.get(target.thread_id, set())
    )
    if (
        details.get("child_index_missing") is not current_child_index_missing
        or details.get("child_rollout_present")
        is not current_child_rollout_present
    ):
        return "current child artifact state differs from the observation"

    edge_status = details.get("edge_status")
    if not (
        isinstance(edge_status, str)
        and edge_status.lower() == "closed"
        and details.get("thread_delete_supported") is False
        and details.get("cleanable") is False
        and exact_blocker_codes(
            details,
            STANDALONE_RELATION_CLEANUP_UNAVAILABLE,
        )
        and details.get("direct_database_edit_supported") is False
    ):
        return "the edge or relation-only cleanup contract is not exact"
    if not _target_has_exact_native_artifact(target, evidence):
        return "no exact native child artifact is currently available"
    return None


def _current_subagent_evidence(
    thread_id: str,
    evidence: _StorageEvidence,
) -> set[str]:
    result: set[str] = set()
    indexed_row = evidence.current_index_rows.get(thread_id)
    if indexed_row is not None:
        thread_source = indexed_row.get("thread_source")
        if (
            isinstance(thread_source, str)
            and thread_source.lower() == "subagent"
        ):
            result.add("threads.thread_source")
        if _source_declares_subagent(indexed_row.get("source")):
            result.add("threads.source")
    if any(
        record.thread_id == thread_id
        and _source_declares_subagent(record.source)
        for record in evidence.current_rollout_records.get(thread_id, ())
    ):
        result.add("session_meta.source")
    return result


def _source_declares_subagent(value: Any) -> bool:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() == "subagent":
            return True
        try:
            value = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return False
    return isinstance(value, Mapping) and (
        "subagent" in value or "thread_spawn" in value
    )


def _current_identity_block_reason(
    thread_ids: Sequence[str],
    evidence: _StorageEvidence,
) -> str | None:
    for thread_id in thread_ids:
        current_paths = set(
            evidence.rollout_paths.get(thread_id, set())
        )
        verified_paths = set(
            evidence.verified_rollout_paths.get(thread_id, set())
        )
        unverified_paths = sorted(current_paths - verified_paths)
        if unverified_paths:
            return (
                "Deletion is blocked because current content path "
                f"metadata did not confirm conversation {thread_id}: "
                + ", ".join(unverified_paths)
            )
    return None


def _source_parent_scope_block_reason(
    thread_ids: Sequence[str],
    observations: Sequence[Observation],
    evidence: _StorageEvidence,
) -> str | None:
    affected_set = set(thread_ids)
    for thread_id in thread_ids:
        indexed_row = evidence.current_index_rows.get(thread_id)
        source_values = [
            record.source
            for record in evidence.current_rollout_records.get(thread_id, ())
            if record.thread_id == thread_id
        ]
        if indexed_row is not None:
            source_values.append(indexed_row.get("source"))
        source_parent_ids = {
            parent_id
            for source in source_values
            for parent_id in _structured_source_parent_ids(source)
        }
        outside_parent_ids = source_parent_ids - affected_set
        if outside_parent_ids and (
            thread_id != thread_ids[0]
            or not (
                _is_approved_missing_parent_orphan(
                    thread_id,
                    source_parent_ids,
                    observations,
                )
                or _is_approved_residual_source_parent(
                    thread_id,
                    source_parent_ids,
                    observations,
                    evidence,
                )
            )
        ):
            return (
                "Deletion is blocked because conversation "
                f"{thread_id} has source-parent metadata outside or "
                "conflicting with the complete affected scope."
            )
        if len(source_parent_ids) > 1:
            return (
                "Deletion is blocked because conversation "
                f"{thread_id} has conflicting structured source parents."
            )
    return None


def _is_approved_missing_parent_orphan(
    thread_id: str,
    source_parent_ids: set[str],
    observations: Sequence[Observation],
) -> bool:
    orphan_observations = [
        observation
        for observation in observations
        if observation.target.thread_id == thread_id
        and observation.finding_type == "orphaned_subagent_thread"
    ]
    if (
        len(orphan_observations) != 1
        or len(source_parent_ids) != 1
    ):
        return False

    details = orphan_observations[0].details
    approved_parent = details.get("parent_thread_id")
    if (
        not isinstance(approved_parent, str)
        or not approved_parent
        or source_parent_ids != {approved_parent}
        or details.get("parent_indexed") is not False
        or details.get("parent_rollout_present") is not False
        or details.get("source_conflict") is True
        or details.get("cleanable") is not True
        or details.get("thread_delete_supported") is not True
    ):
        return False

    edge_present = details.get("spawn_edge_present")
    evidence_strength = details.get("evidence_strength")
    if edge_present is True:
        edge_status = details.get("spawn_edge_status")
        return (
            isinstance(edge_status, str)
            and edge_status.lower() == "closed"
            and evidence_strength == "spawn_edge"
        )
    return (
        edge_present is False
        and evidence_strength == "source_consensus"
        and details.get("requires_explicit_selection") is True
    )


def _is_approved_residual_source_parent(
    thread_id: str,
    source_parent_ids: set[str],
    observations: Sequence[Observation],
    evidence: _StorageEvidence,
) -> bool:
    residual_observations = [
        observation
        for observation in observations
        if observation.target.thread_id == thread_id
        and observation.finding_type == "residual_spawn_edge"
    ]
    if len(residual_observations) != 1 or len(source_parent_ids) != 1:
        return False

    observation = residual_observations[0]
    return (
        source_parent_ids
        == {
            observation.details.get("parent_thread_id")
        }
        and _residual_delete_contract_issue(
            observation.target,
            observations,
            evidence,
        )
        is None
    )


def _integrity_identity_block_reason(
    target: TargetRef,
    observations: Sequence[Observation],
    evidence: _StorageEvidence,
) -> str | None:
    known_paths = set(evidence.rollout_paths.get(target.thread_id, set()))
    for observation in observations:
        if observation.finding_type in {
            "duplicate_rollout",
            "index_rollout_path_mismatch",
        }:
            known_paths.update(observation.rollout_paths)

    existing_paths: set[str] = set()
    for raw_path in sorted(known_paths):
        path = Path(raw_path)
        try:
            is_file = path.is_file()
        except OSError as exc:
            return (
                "A known conversation content path could not be inspected, "
                f"so its identity scope is not exact: {_error_text(exc)}"
            )
        if is_file:
            existing_paths.add(raw_path)

    verified_paths = set(
        evidence.verified_rollout_paths.get(target.thread_id, set())
    )
    unverified_paths = sorted(existing_paths - verified_paths)
    if unverified_paths:
        return (
            "High-risk deletion is blocked because current content file "
            "metadata could not confirm the target conversation ID for: "
            + ", ".join(unverified_paths)
        )

    verified_existing_paths = existing_paths & verified_paths
    originators = evidence.rollout_originators.get(target.thread_id, set())
    if len(originators) > 1:
        return (
            "High-risk deletion is blocked because current content files "
            "have conflicting originator metadata."
        )
    source_identities = evidence.rollout_source_identities.get(
        target.thread_id,
        set(),
    )
    if len(source_identities) > 1:
        return (
            "High-risk deletion is blocked because current content files "
            "have conflicting source or parent metadata."
        )
    if (
        target.thread_id not in evidence.current_indexed_ids
        and not verified_existing_paths
    ):
        return (
            "High-risk deletion is blocked because no current conversation "
            "list record or metadata-verified content file remains for the "
            "target."
        )
    return None


def _risk_level(
    kind: ActionKind,
    observations: Sequence[Observation],
    impact: ActionImpact,
    *,
    unavailable_reason: str | None,
) -> RiskLevel:
    if kind is ActionKind.KEEP:
        return RiskLevel.LOW
    finding_types = {obs.finding_type for obs in observations}
    if unavailable_reason is not None:
        if _UNIMPLEMENTED_REASONS.get(kind) != unavailable_reason:
            return RiskLevel.BLOCKED
    if kind in {
        ActionKind.REPAIR_INDEX_PATH,
        ActionKind.QUARANTINE_ARTIFACTS,
        ActionKind.REPAIR_LEGACY_INDEX,
        ActionKind.REMOVE_BROKEN_RELATION,
        ActionKind.REMOVE_DESKTOP_STATE,
    }:
        return RiskLevel.HIGH
    if kind is ActionKind.REMOVE_FRONTEND_REFERENCE:
        return RiskLevel.REVIEW
    if kind is ActionKind.DELETE_CONVERSATION:
        if (
            "rollout_missing_index" in finding_types
            or "duplicate_rollout" in finding_types
            or "index_rollout_path_mismatch" in finding_types
            or "residual_spawn_edge" in finding_types
        ):
            return RiskLevel.HIGH
        if impact.descendant_count:
            return RiskLevel.REVIEW
        if impact.rollout_file_count:
            return RiskLevel.REVIEW
        return RiskLevel.LOW
    return RiskLevel.REVIEW


def _storage_location(
    storage_id: str,
    evidence: _StorageEvidence,
) -> StorageLocation:
    errors = tuple(dict.fromkeys(evidence.errors))
    if not errors:
        status = ScanStatus.OK
    else:
        status = ScanStatus.PARTIAL if evidence.has_findings else ScanStatus.FAILED
    return StorageLocation(
        storage_id=storage_id,
        label=_storage_label(evidence.path),
        path=evidence.path,
        codex_bin_hint=evidence.bin_hint,
        scan_status=status,
        errors=errors,
    )


def _storage_label(path: Path) -> str:
    default_home = Path.home() / ".codex"
    if normalize_storage_path(path) == normalize_storage_path(default_home):
        return "Codex 默认数据目录"
    name = path.name or "Codex"
    lowered = name.lower()
    if "cindy" in lowered:
        return "Cindy 专用数据目录"
    if "aion" in lowered:
        return "AionUI 专用数据目录"
    return f"{name} 数据目录"


def _action_id(target: TargetRef, kind: ActionKind) -> str:
    digest = _fingerprint(
        {
            "storage_id": target.storage_id,
            "thread_id": target.thread_id,
            "action_kind": kind.value,
        }
    )[:24]
    return f"{kind.value}-{digest}"


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _normalize_artifact_path(path: str | os.PathLike[str]) -> str:
    return canonical_existing_path_key(path)


def _indexed_rollout_path(
    codex_home: Path,
    row: Mapping[str, Any],
) -> Path | None:
    raw_path = row.get("rollout_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = codex_home / path
    return path


def _as_nonnegative_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def _error_text(exc: BaseException) -> str:
    return str(exc) or repr(exc)


__all__ = [
    "ActionImpact",
    "ActionKind",
    "ActionResult",
    "CandidateAction",
    "CleanupPlan",
    "Observation",
    "PlannedAction",
    "ResultStatus",
    "RiskLevel",
    "ScanStatus",
    "StorageLocation",
    "TargetRef",
    "build_cleanup_plan",
    "generate_cleanup_plan",
    "normalize_storage_path",
    "storage_id_for_path",
]
