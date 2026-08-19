from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from .adapters.base import FrontendAdapter
from .blocker_codes import (
    CASCADE_REQUIRES_EXPLICIT_SCOPE,
    INTEGRITY_REVIEW_REQUIRED,
    LIVE_FRONTEND_REFERENCE,
    STANDALONE_RELATION_CLEANUP_UNAVAILABLE,
    cleanup_blocker_codes,
    exact_blocker_codes,
)
from .codex_app_server import CodexAppServer
from .codex_state import (
    find_thread_rollouts,
    iter_rollouts,
    read_rollouts_at_paths,
    read_spawn_edge_records,
    read_spawn_edges,
    read_spawn_descendants,
    read_thread_index,
    rollout_state_fingerprint,
)
from .codex_desktop_state import (
    DesktopStateError,
    remaining_desktop_state_markers,
)
from .conversation_metadata import (
    read_conversation_summaries,
    read_legacy_thread_names,
)
from .discovery import choose_codex_binary
from .models import Finding, RolloutRecord
from .path_identity import canonical_existing_path_key


class ThreadSelectionError(ValueError):
    """A thread selector matched no finding or more than one thread ID."""

    def __init__(
        self,
        selector: str,
        *,
        kind: str,
        matches: Sequence[str] = (),
        homes: Sequence[str] = (),
    ) -> None:
        self.selector = selector
        self.kind = kind
        self.matches = tuple(matches)
        self.homes = tuple(homes)
        if kind == "not_found":
            message = f"Thread selector {selector!r} did not match any finding"
        elif kind == "ambiguous":
            message = (
                f"Thread selector {selector!r} is ambiguous "
                f"({len(self.matches)} matching cleanup targets)"
            )
        else:
            message = f"Invalid thread selector {selector!r}"
        super().__init__(message)


@dataclass(frozen=True)
class ScanFailure:
    platform: str
    message: str
    error_type: str
    codex_home: Path | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "platform": self.platform,
            "message": self.message,
            "error_type": self.error_type,
            "codex_home": (
                str(self.codex_home) if self.codex_home is not None else None
            ),
        }


@dataclass
class ScanReport:
    findings: list[Finding] = field(default_factory=list)
    errors: list[ScanFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.findings),
            "findings": [finding.to_dict() for finding in self.findings],
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True)
class VerificationResult:
    deleted: bool
    remaining_artifacts: tuple[str, ...] = ()
    error: str | None = None
    status: str | None = None
    checked_thread_ids: tuple[str, ...] = ()
    remaining_thread_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        status = self.status
        if status is None:
            if self.deleted:
                status = "deleted"
            elif self.error:
                status = "unknown"
            else:
                status = "not_deleted"
            object.__setattr__(self, "status", status)
        if status not in {"deleted", "not_deleted", "partial", "unknown"}:
            raise ValueError(f"Unsupported verification status: {status!r}")
        if self.deleted != (status == "deleted"):
            raise ValueError("deleted must agree with verification status")


@dataclass
class CleanupResult:
    finding: Finding
    status: str
    error: str | None = None
    remaining_artifacts: tuple[str, ...] = ()
    request_error: str | None = None
    impacted_thread_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {
            "deleted",
            "not_deleted",
            "partial",
            "unknown",
        }:
            raise ValueError(f"Unsupported cleanup status: {self.status!r}")

    @property
    def succeeded(self) -> bool:
        return self.status == "deleted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.finding.platform,
            "thread_id": self.finding.thread_id,
            "codex_home": str(self.finding.codex_home),
            "status": self.status,
            "error": self.error,
            "request_error": self.request_error,
            "remaining_artifacts": list(self.remaining_artifacts),
            "impacted_thread_ids": list(self.impacted_thread_ids),
        }


@dataclass
class CleanupReport:
    planned: list[Finding]
    results: list[CleanupResult] = field(default_factory=list)
    scan_errors: list[ScanFailure] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(result.succeeded for result in self.results)

    @property
    def failed(self) -> int:
        return sum(not result.succeeded for result in self.results)

    @property
    def ok(self) -> bool:
        return not self.scan_errors and self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "planned": len(self.planned),
            "attempted": len(self.results),
            "succeeded": self.succeeded,
            "failed": self.failed,
            "results": [result.to_dict() for result in self.results],
            "errors": [error.to_dict() for error in self.scan_errors],
        }


class _AppServerContext(Protocol):
    def __enter__(self) -> Any: ...

    def __exit__(self, *exc_info: object) -> object: ...


AppServerFactory = Callable[..., _AppServerContext]
BinaryResolver = Callable[[Path | None], Path | None]
FindingVerifier = Callable[[Finding], VerificationResult]
PreDeleteValidator = Callable[[Finding], None]
ActionStateCallback = Callable[[str, Finding, CleanupResult | None], None]
CleanupTargetKey = tuple[str, str] | str
ApprovedDescendants = Mapping[CleanupTargetKey, Iterable[str]]
ApprovedIntegrityDeletes = Mapping[CleanupTargetKey, Iterable[str]]

_APPROVABLE_INTEGRITY_FINDING_TYPES = frozenset(
    {
        "duplicate_rollout",
        "index_rollout_path_mismatch",
        "residual_spawn_edge",
    }
)


@dataclass(frozen=True)
class ExpectedDeletionScope:
    """The exact native state approved for one delete root."""

    descendant_thread_ids: tuple[str, ...] = ()
    indexed_thread_ids: tuple[str, ...] = ()
    rollout_paths: tuple[str, ...] = ()
    rollout_state_fingerprints: tuple[str, ...] | None = None
    conversation_metadata_fingerprints: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, list[str] | None]:
        return {
            "descendant_thread_ids": list(self.descendant_thread_ids),
            "indexed_thread_ids": list(self.indexed_thread_ids),
            "rollout_paths": list(self.rollout_paths),
            "rollout_state_fingerprints": (
                None
                if self.rollout_state_fingerprints is None
                else list(self.rollout_state_fingerprints)
            ),
            "conversation_metadata_fingerprints": (
                None
                if self.conversation_metadata_fingerprints is None
                else list(self.conversation_metadata_fingerprints)
            ),
        }


ExpectedScopeValue = (
    ExpectedDeletionScope
    | Mapping[str, Iterable[Any] | None]
)
ExpectedDeletionScopes = Mapping[CleanupTargetKey, ExpectedScopeValue]


def finding_key(finding: Finding) -> tuple[str, str]:
    """Return the stable cleanup identity for a finding."""

    home = canonical_existing_path_key(finding.codex_home)
    return home, finding.thread_id


def _normalize_binary_hint(hint: str | os.PathLike[str]) -> str:
    return canonical_existing_path_key(Path(hint).expanduser())


def _finding_binary_hint_candidates(finding: Finding) -> set[str]:
    candidates: set[str] = set()
    if finding.codex_bin_hint is not None:
        candidates.add(_normalize_binary_hint(finding.codex_bin_hint))
    preserved = finding.details.get("codex_bin_hint_candidates")
    values = (
        preserved
        if isinstance(preserved, (list, tuple, set, frozenset))
        else (preserved,)
    )
    candidates.update(
        _normalize_binary_hint(item)
        for item in values
        if (
            isinstance(item, (str, os.PathLike))
            and os.fspath(item)
        )
    )
    return candidates


def deduplicate_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Deduplicate cleanup targets while retaining secondary scan evidence."""

    unique: dict[tuple[str, str], Finding] = {}
    for finding in findings:
        key = finding_key(finding)
        existing = unique.get(key)
        if existing is None:
            details = dict(finding.details)
            hint_candidates = _finding_binary_hint_candidates(finding)
            if hint_candidates:
                details["codex_bin_hint_candidates"] = sorted(
                    hint_candidates
                )
            unique[key] = replace(finding, details=details)
            continue

        additional = {
            "platform": finding.platform,
            "platform_session_id": finding.platform_session_id,
            "reason": finding.reason,
            "details": finding.details,
            "platform_db": str(finding.platform_db),
        }
        existing_details = dict(existing.details)
        hint_candidates = (
            _finding_binary_hint_candidates(existing)
            | _finding_binary_hint_candidates(finding)
        )
        if hint_candidates:
            existing_details["codex_bin_hint_candidates"] = sorted(
                hint_candidates
            )
        prior = existing_details.get("additional_findings")
        additions = list(prior) if isinstance(prior, list) else []
        additions.append(additional)
        existing_details["additional_findings"] = additions

        # Preserve the richest artifact evidence when two adapters describe
        # the same physical Codex thread.
        rollout = existing.rollout or finding.rollout
        codex_bin_hint = existing.codex_bin_hint or finding.codex_bin_hint
        unique[key] = replace(
            existing,
            rollout=rollout,
            codex_indexed=existing.codex_indexed or finding.codex_indexed,
            codex_archived=(
                existing.codex_archived
                if existing.codex_archived is not None
                else finding.codex_archived
            ),
            codex_bin_hint=codex_bin_hint,
            details=existing_details,
        )

    return sorted(
        unique.values(),
        key=lambda finding: (
            finding.platform,
            os.path.normcase(os.fspath(finding.codex_home)),
            finding.thread_id,
        ),
    )


def scan_adapters(
    adapters: Iterable[FrontendAdapter],
    *,
    require_codex_artifacts: bool = True,
) -> ScanReport:
    """Run adapters independently so one broken frontend does not hide others."""

    findings: list[Finding] = []
    errors: list[ScanFailure] = []
    scanned_adapters = list(adapters)
    binary_hints_by_home: dict[str, set[str]] = defaultdict(set)
    for adapter in scanned_adapters:
        raw_codex_home = getattr(adapter, "codex_home", None)
        raw_binary_hint = getattr(adapter, "codex_bin_hint", None)
        if not isinstance(raw_codex_home, (str, os.PathLike)) or not isinstance(
            raw_binary_hint,
            (str, os.PathLike),
        ):
            continue
        if not os.fspath(raw_binary_hint):
            continue
        home = canonical_existing_path_key(raw_codex_home)
        binary_hints_by_home[home].add(
            _normalize_binary_hint(raw_binary_hint)
        )
    for adapter in scanned_adapters:
        platform = getattr(adapter, "name", type(adapter).__name__)
        try:
            adapter_findings = adapter.scan()
        except Exception as exc:
            raw_codex_home = getattr(adapter, "codex_home", None)
            errors.append(
                ScanFailure(
                    platform=str(platform),
                    message=str(exc) or repr(exc),
                    error_type=type(exc).__name__,
                    codex_home=(
                        Path(raw_codex_home)
                        if isinstance(raw_codex_home, (str, os.PathLike))
                        else None
                    ),
                )
            )
            continue
        for finding in adapter_findings:
            if not isinstance(finding, Finding):
                errors.append(
                    ScanFailure(
                        platform=str(platform),
                        message=(
                            "Adapter returned an unsupported finding object: "
                            f"{type(finding).__name__}"
                        ),
                        error_type="TypeError",
                        codex_home=(
                            Path(adapter.codex_home)
                            if isinstance(
                                getattr(adapter, "codex_home", None),
                                (str, os.PathLike),
                            )
                            else None
                        ),
                    )
                )
                continue
            if (
                require_codex_artifacts
                and not finding.has_codex_artifacts
                and not isinstance(
                    finding.details.get("frontend_reference"),
                    Mapping,
                )
            ):
                continue
            findings.append(finding)
    findings, protection_errors = _apply_live_reference_protection(
        findings,
        scanned_adapters,
    )
    errors.extend(protection_errors)
    findings_with_store_hints: list[Finding] = []
    for finding in findings:
        details = dict(finding.details)
        candidates = (
            _finding_binary_hint_candidates(finding)
            | binary_hints_by_home.get(finding_key(finding)[0], set())
        )
        if candidates:
            details["codex_bin_hint_candidates"] = sorted(candidates)
        findings_with_store_hints.append(replace(finding, details=details))
    return ScanReport(
        findings=deduplicate_findings(findings_with_store_hints),
        errors=errors,
    )


def select_findings(
    findings: Iterable[Finding],
    selectors: Iterable[str] | None,
) -> list[Finding]:
    """Select exact thread IDs or unambiguous thread-ID prefixes."""

    candidates = deduplicate_findings(findings)
    requested = [selector.strip() for selector in selectors or () if selector.strip()]
    if not requested:
        return candidates

    selected_keys: set[tuple[str, str]] = set()
    for selector in requested:
        exact = [item for item in candidates if item.thread_id == selector]
        if exact:
            if len(exact) != 1:
                raise ThreadSelectionError(
                    selector,
                    kind="ambiguous",
                    matches=[item.thread_id for item in exact],
                    homes=[str(item.codex_home) for item in exact],
                )
            selected_keys.add(finding_key(exact[0]))
            continue

        prefix_matches = [item for item in candidates if item.thread_id.startswith(selector)]
        if not prefix_matches:
            raise ThreadSelectionError(selector, kind="not_found")
        if len(prefix_matches) != 1:
            raise ThreadSelectionError(
                selector,
                kind="ambiguous",
                matches=[item.thread_id for item in prefix_matches],
                homes=[str(item.codex_home) for item in prefix_matches],
            )
        selected_keys.add(finding_key(prefix_matches[0]))

    return [item for item in candidates if finding_key(item) in selected_keys]


def verify_finding_deleted(finding: Finding) -> VerificationResult:
    """Verify the root conversation and every planned cascade target."""

    remaining: list[str] = []
    checked_thread_ids = tuple(
        sorted(
            {
                finding.thread_id,
                *_detail_thread_ids(finding.details),
            }
        )
    )
    remaining_thread_ids: set[str] = set()
    try:
        known_paths = _detail_paths(finding.details)
        if finding.rollout is not None:
            known_paths.append(finding.rollout.path)
        current_rollouts = read_rollouts_at_paths(
            finding.codex_home,
            known_paths,
            strict=True,
        )
        for current_rollout in current_rollouts:
            if current_rollout.thread_id not in checked_thread_ids:
                continue
            current_path = str(current_rollout.path)
            if current_path not in remaining:
                remaining.append(current_path)
            remaining_thread_ids.add(current_rollout.thread_id)

        indexed = read_thread_index(
            finding.codex_home,
            checked_thread_ids,
            strict=True,
        )
        for thread_id in checked_thread_ids:
            if thread_id in indexed:
                index_marker = _index_artifact_marker(finding, thread_id)
                if index_marker not in remaining:
                    remaining.append(index_marker)
                remaining_thread_ids.add(thread_id)

        current_edges = read_spawn_edges(
            finding.codex_home,
            checked_thread_ids,
            strict=True,
            rollout_records=current_rollouts,
        )
        for parent, child in sorted(current_edges):
            edge_marker = _edge_artifact_marker(parent, child)
            if edge_marker not in remaining:
                remaining.append(edge_marker)
            remaining_thread_ids.update(
                {parent, child} & set(checked_thread_ids)
            )

        # The documented app-server contract removes rollout files and
        # associated native metadata. Codex Desktop also maintains a private
        # host catalog/UI cache outside that contract. Verify it separately so
        # a sidebar ghost cannot be reported as a complete deletion.
        for thread_id in checked_thread_ids:
            desktop_markers = remaining_desktop_state_markers(
                finding.codex_home,
                (thread_id,),
            )
            if desktop_markers:
                remaining_thread_ids.add(thread_id)
                for marker in desktop_markers:
                    if marker not in remaining:
                        remaining.append(marker)

        # Native integrity findings may point at additional on-disk evidence.
        # These paths are verified if supplied, but are never removed directly.
        for raw_path in _detail_paths(finding.details):
            if raw_path.exists():
                rendered = str(raw_path)
                if rendered not in remaining:
                    remaining.append(rendered)
                remaining_thread_ids.add(finding.thread_id)
    except (OSError, RuntimeError, DesktopStateError) as exc:
        return VerificationResult(
            deleted=False,
            remaining_artifacts=tuple(remaining),
            error=f"Could not verify deletion: {exc}",
            status="unknown",
            checked_thread_ids=checked_thread_ids,
            remaining_thread_ids=tuple(sorted(remaining_thread_ids)),
        )

    expected_artifacts = {
        item
        for item in finding.details.get(
            "planned_expected_artifacts", ()
        )
        if isinstance(item, str) and item
    }
    if not remaining:
        status = "deleted"
    elif expected_artifacts and expected_artifacts.issubset(remaining):
        status = "not_deleted"
    elif (
        not expected_artifacts
        and remaining_thread_ids == set(checked_thread_ids)
    ):
        status = "not_deleted"
    else:
        status = "partial"
    return VerificationResult(
        deleted=status == "deleted",
        remaining_artifacts=tuple(remaining),
        status=status,
        checked_thread_ids=checked_thread_ids,
        remaining_thread_ids=tuple(sorted(remaining_thread_ids)),
    )


def clean_findings(
    findings: Iterable[Finding],
    *,
    timeout: float = 30.0,
    app_server_factory: AppServerFactory = CodexAppServer,
    binary_resolver: BinaryResolver = choose_codex_binary,
    verifier: FindingVerifier = verify_finding_deleted,
    verification_attempts: int = 4,
    verification_interval: float = 0.05,
    explicit_selection: bool = False,
    approved_descendants: ApprovedDescendants | None = None,
    expected_scopes: ExpectedDeletionScopes | None = None,
    approved_integrity_deletes: ApprovedIntegrityDeletes | None = None,
    pre_delete_validator: PreDeleteValidator | None = None,
    action_state_callback: ActionStateCallback | None = None,
) -> CleanupReport:
    """Delete findings through Codex app-server and verify every target.

    ``approved_descendants`` is the reviewed transitive cascade scope for each
    root. Keys may be ``finding_key(finding)`` tuples or thread IDs when IDs
    are unique across the supplied findings. Omitting it preserves the legacy
    fail-closed behavior: any descendant blocks deletion.

    ``expected_scopes`` optionally supplies the exact reviewed native state
    (descendant closure, indexed conversation IDs, and normalized active or
    archived rollout paths). Every root must have an entry. After app-server
    startup, all roots are captured and compared before the first delete
    request; any difference blocks the entire Codex-home group.

    ``approved_integrity_deletes`` is a per-target set of explicitly approved
    HIGH-risk native finding types. ``duplicate_rollout``,
    ``index_rollout_path_mismatch`` and ``residual_spawn_edge`` are
    recognized, and an exact ``expected_scopes`` entry for the same target is
    mandatory. This narrow authorization only removes those adapters'
    built-in recoverability/manual-review or standalone-relation soft
    blockers; all identity, ownership, live-reference, schema, quarantine and
    scope blockers remain enforced. It never edits a relationship directly.
    """

    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if verification_attempts < 1:
        raise ValueError("verification_attempts must be at least one")
    if verification_interval < 0:
        raise ValueError("verification_interval must not be negative")

    eligible = [
        finding
        for finding in findings
        if finding.has_codex_artifacts
    ]
    planned = deduplicate_findings(eligible)
    report = CleanupReport(planned=planned)
    groups: dict[str, list[Finding]] = defaultdict(list)
    binary_hints_by_home: dict[str, list[Path]] = defaultdict(list)
    for finding in planned:
        groups[finding_key(finding)[0]].append(finding)
    for finding in eligible:
        binary_hints_by_home[finding_key(finding)[0]].extend(
            Path(candidate)
            for candidate in _finding_binary_hint_candidates(finding)
        )

    for home_key, group in groups.items():
        _clean_group(
            group,
            report=report,
            timeout=timeout,
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
            verifier=verifier,
            verification_attempts=verification_attempts,
            verification_interval=verification_interval,
            explicit_selection=explicit_selection,
            approved_descendants=approved_descendants,
            expected_scopes=expected_scopes,
            approved_integrity_deletes=approved_integrity_deletes,
            pre_delete_validator=pre_delete_validator,
            action_state_callback=action_state_callback,
            selected_binary_hints=binary_hints_by_home.get(
                home_key,
                (),
            ),
        )
    return report


def cleanup_block_reason(
    finding: Finding,
    *,
    explicit_selection: bool = False,
) -> str | None:
    """Return why a finding must not be sent to thread/delete, if unsafe."""

    evidence: list[tuple[str, dict[str, Any]]] = [
        (finding.platform.lower(), finding.details)
    ]
    additional = finding.details.get("additional_findings")
    if isinstance(additional, list):
        for item in additional:
            if not isinstance(item, dict):
                continue
            platform = str(item.get("platform", "")).lower()
            details = item.get("details")
            if isinstance(details, dict):
                evidence.append((platform, details))

    for _platform, details in evidence:
        explicit_reason = details.get("cleanup_blocked_reason")
        if isinstance(explicit_reason, str) and explicit_reason.strip():
            return explicit_reason.strip()
        blocker_codes = cleanup_blocker_codes(details)
        if blocker_codes:
            return (
                "Structured cleanup blocker codes prevent deletion: "
                + ", ".join(sorted(blocker_codes))
            )
        if details.get("needs_quarantine") is True:
            return (
                "The finding requires quarantine/manual review; this release "
                "never moves or removes artifacts directly."
            )
        if (
            details.get("requires_explicit_selection") is True
            and not explicit_selection
        ):
            return (
                "This orphan class requires an explicit --thread-id selection "
                "before cleanup."
            )
        if details.get("cascade_safe") is False or details.get(
            "has_unreviewed_descendants"
        ) is True:
            return (
                "Codex thread/delete would cascade to descendant threads that "
                "have not been approved for deletion."
            )
        descendants = details.get("cascade_descendants")
        if isinstance(descendants, (list, tuple, set)) and descendants:
            return (
                "Codex thread/delete would cascade to descendant threads; "
                "descendant cleanup is report-only in this release."
            )
        if details.get("cleanable") is False:
            return "The adapter explicitly marked this finding read-only/unsafe."
        if details.get("thread_delete_supported") is False:
            return "The adapter reports that Codex thread/delete cannot repair it."
        if (
            details.get("cleanable") is not True
            or details.get("thread_delete_supported") is not True
        ):
            return (
                "Findings are fail-closed unless the adapter "
                "explicitly marks both cleanable and thread_delete_supported."
            )
    return None


def _block_reason_after_cascade_approval(
    finding: Finding,
    *,
    explicit_selection: bool,
) -> str | None:
    """Re-evaluate legacy evidence after removing cascade-only blockers."""

    sanitized_finding = _finding_after_cascade_approval(finding)
    return cleanup_block_reason(
        sanitized_finding,
        explicit_selection=explicit_selection,
    )


def _finding_after_cascade_approval(
    finding: Finding,
) -> Finding:
    details = _without_cascade_only_blockers(finding.details)
    additional = details.get("additional_findings")
    if isinstance(additional, list):
        sanitized_additional: list[object] = []
        for item in additional:
            if not isinstance(item, dict):
                sanitized_additional.append(item)
                continue
            sanitized_item = dict(item)
            item_details = item.get("details")
            if isinstance(item_details, dict):
                sanitized_item["details"] = _without_cascade_only_blockers(
                    item_details
                )
            sanitized_additional.append(sanitized_item)
        details["additional_findings"] = sanitized_additional
    return replace(finding, details=details)


def _without_cascade_only_blockers(
    details: Mapping[str, Any],
) -> dict[str, Any]:
    sanitized = dict(details)
    if not exact_blocker_codes(
        sanitized,
        CASCADE_REQUIRES_EXPLICIT_SCOPE,
    ):
        return sanitized

    sanitized["cleanup_blocked_reason"] = None
    sanitized["cleanup_blocker_codes"] = []
    sanitized["cascade_safe"] = True
    sanitized["has_unreviewed_descendants"] = False
    sanitized["cascade_descendants"] = []
    if sanitized.get("cleanable") is False:
        sanitized["cleanable"] = True
    return sanitized


def _clean_group(
    findings: list[Finding],
    *,
    report: CleanupReport,
    timeout: float,
    app_server_factory: AppServerFactory,
    binary_resolver: BinaryResolver,
    verifier: FindingVerifier,
    verification_attempts: int,
    verification_interval: float,
    explicit_selection: bool,
    approved_descendants: ApprovedDescendants | None,
    expected_scopes: ExpectedDeletionScopes | None,
    approved_integrity_deletes: ApprovedIntegrityDeletes | None,
    pre_delete_validator: PreDeleteValidator | None,
    action_state_callback: ActionStateCallback | None,
    selected_binary_hints: Sequence[Path],
) -> None:
    codex_home = findings[0].codex_home
    normalized_binary_hints = {
        _normalize_binary_hint(hint)
        for hint in selected_binary_hints
    }
    if len(normalized_binary_hints) > 1:
        rendered_hints = ", ".join(sorted(normalized_binary_hints))
        error = (
            "Cleanup was blocked because selected targets in one Codex data "
            "directory provide conflicting Codex executable hints; no "
            f"app-server was started: {rendered_hints}."
        )
        report.results.extend(
            CleanupResult(
                finding=finding,
                status="unknown",
                error=error,
            )
            for finding in findings
        )
        return
    normalized_binary_hint = (
        Path(next(iter(normalized_binary_hints)))
        if normalized_binary_hints
        else None
    )
    thread_id_counts: dict[str, int] = defaultdict(int)
    for finding in report.planned:
        thread_id_counts[finding.thread_id] += 1

    approved_scopes: dict[
        tuple[str, str],
        tuple[set[str], bool],
    ] = {
        finding_key(finding): _resolve_approved_descendants(
            finding,
            approved_descendants,
            thread_id_counts=thread_id_counts,
        )
        for finding in findings
    }
    resolved_expected_scopes = {
        finding_key(finding): _resolve_expected_scope(
            finding,
            expected_scopes,
            thread_id_counts=thread_id_counts,
        )
        for finding in findings
    }
    resolved_integrity_approvals = {
        finding_key(finding): _resolve_integrity_approval(
            finding,
            approved_integrity_deletes,
            thread_id_counts=thread_id_counts,
        )
        for finding in findings
    }
    approval_configuration_issues: dict[tuple[str, str], str] = {}
    for finding in findings:
        key = finding_key(finding)
        expected_scope, expected_present = resolved_expected_scopes[key]
        if not expected_present or expected_scope is None:
            continue
        expected_descendants = set(
            expected_scope.descendant_thread_ids
        )
        approved, approval_present = approved_scopes[key]
        if approved_descendants is None:
            approved_scopes[key] = (expected_descendants, True)
        elif not approval_present:
            approval_configuration_issues[key] = (
                "reviewed descendant scope is missing"
            )
        elif approved != expected_descendants:
            approval_configuration_issues[key] = (
                "approved descendants disagree with the exact expected scope"
            )

    overlaps = _overlapping_affected_scopes(
        findings,
        approved_scopes,
    )
    if overlaps:
        rendered_overlaps = "; ".join(
            f"{left} / {right}: {', '.join(shared)}"
            for left, right, shared in overlaps
        )
        error = (
            "所选根目标互相包含或影响相同的关联任务对话，请只保留上游根目标"
            f"或选择互不重叠的根目标。重叠范围：{rendered_overlaps}"
        )
        report.results.extend(
            CleanupResult(
                finding=finding,
                status="unknown",
                error=error,
                impacted_thread_ids=tuple(
                    sorted(
                        {
                            finding.thread_id,
                            *approved_scopes[finding_key(finding)][0],
                        }
                    )
                ),
            )
            for finding in findings
        )
        return

    supported: list[Finding] = []
    policy_issues: list[tuple[Finding, str]] = []
    for finding in findings:
        key = finding_key(finding)
        policy_finding = finding
        _approved, approval_present = approved_scopes[key]
        _expected, expected_present = resolved_expected_scopes[key]
        if (
            (
                approved_descendants is not None
                or expected_present
            )
            and approval_present
        ):
            policy_finding = _finding_after_cascade_approval(
                policy_finding
            )
        integrity_types, integrity_present = (
            resolved_integrity_approvals[key]
        )
        integrity_configuration_error: str | None = None
        if integrity_present:
            unsupported_types = (
                integrity_types
                - _APPROVABLE_INTEGRITY_FINDING_TYPES
            )
            if unsupported_types:
                integrity_configuration_error = (
                    "HIGH-risk integrity deletion approval contains "
                    "unsupported finding type(s): "
                    + ", ".join(sorted(unsupported_types))
                )
            elif not expected_present:
                integrity_configuration_error = (
                    "HIGH-risk integrity deletion requires an exact "
                    "expected_scopes entry for the same target"
                )
            else:
                policy_finding = _finding_after_integrity_approval(
                    policy_finding,
                    integrity_types,
                )

        blocked_reason = (
            integrity_configuration_error
            or cleanup_block_reason(
                policy_finding,
                explicit_selection=explicit_selection,
            )
        )
        if blocked_reason is not None:
            policy_issues.append((finding, blocked_reason))
        else:
            supported.append(finding)
    if expected_scopes is not None and policy_issues:
        rendered_issues = "; ".join(
            f"{finding.thread_id}: {reason}"
            for finding, reason in policy_issues
        )
        error = (
            "Cleanup was blocked for this exact Codex data-directory plan "
            "because at least one selected target failed authorization or "
            f"static safety policy; no target was deleted: {rendered_issues}."
        )
        report.results.extend(
            CleanupResult(
                finding=finding,
                status="unknown",
                error=error,
                impacted_thread_ids=tuple(
                    sorted(
                        {
                            finding.thread_id,
                            *approved_scopes[finding_key(finding)][0],
                        }
                    )
                ),
            )
            for finding in findings
        )
        return
    report.results.extend(
        CleanupResult(
            finding=finding,
            status="unknown",
            error=reason,
        )
        for finding, reason in policy_issues
    )
    if not supported:
        return

    try:
        descendants_by_thread = read_spawn_descendants(
            codex_home,
            (finding.thread_id for finding in supported),
            strict=True,
        )
    except Exception as exc:
        error = (
            "Could not verify thread/delete cascade safety; all targets in this "
            f"Codex home were left untouched: {exc}"
        )
        report.results.extend(
            CleanupResult(finding=finding, status="unknown", error=error)
            for finding in supported
        )
        return

    cascade_safe: list[tuple[Finding, set[str]]] = []
    current_scopes: dict[tuple[str, str], set[str]] = {}
    scope_issues: list[str] = []
    for finding in supported:
        current_descendants = descendants_by_thread.get(
            finding.thread_id, set()
        )
        current_scopes[finding_key(finding)] = current_descendants
        key = finding_key(finding)
        approved, approval_present = approved_scopes[key]
        _expected, expected_present = resolved_expected_scopes[key]
        if expected_scopes is not None and not expected_present:
            scope_issues.append(
                f"{finding.thread_id}: exact expected scope is missing"
            )
            continue
        configuration_issue = approval_configuration_issues.get(key)
        if configuration_issue is not None:
            scope_issues.append(
                f"{finding.thread_id}: {configuration_issue}"
            )
            continue
        if approved_descendants is not None and not approval_present:
            scope_issues.append(
                f"{finding.thread_id}: reviewed descendant scope is missing"
            )
            continue
        if current_descendants != approved:
            added = sorted(current_descendants - approved)
            removed = sorted(approved - current_descendants)
            changes: list[str] = []
            if added:
                changes.append(f"new descendants: {', '.join(added)}")
            if removed:
                changes.append(
                    f"planned descendants no longer present: {', '.join(removed)}"
                )
            if approved_descendants is None and current_descendants:
                changes = [
                    "descendant deletion was not explicitly approved: "
                    + ", ".join(sorted(current_descendants))
                ]
            scope_issues.append(
                f"{finding.thread_id}: current descendant closure differs "
                f"from the reviewed scope ({'; '.join(changes)})"
            )
            continue
        cascade_safe.append((finding, approved))
    if scope_issues:
        error = (
            "Cleanup was blocked for this Codex data directory because at "
            "least one selected target's associated-task scope changed; no "
            f"supported target was deleted: {'; '.join(scope_issues)}."
        )
        report.results.extend(
            CleanupResult(
                finding=finding,
                status="unknown",
                error=error,
                impacted_thread_ids=tuple(
                    sorted(
                        {
                            finding.thread_id,
                            *approved_scopes[finding_key(finding)][0],
                            *current_scopes.get(finding_key(finding), set()),
                        }
                    )
                ),
            )
            for finding in supported
        )
        return

    binary = binary_resolver(normalized_binary_hint)
    if binary is None:
        error = (
            "No Codex executable was found. Install Codex or provide a valid "
            "frontend-bundled executable."
        )
        report.results.extend(
            CleanupResult(
                finding=finding,
                status="unknown",
                error=error,
                impacted_thread_ids=tuple(
                    sorted({finding.thread_id, *descendants})
                ),
            )
            for finding, descendants in cascade_safe
        )
        return

    try:
        context = app_server_factory(
            codex_home=codex_home,
            codex_binary=binary,
            timeout=timeout,
        )
        with context as server:
            # Build native rollout/source identity once for the whole batch.
            # Individual actions below re-read only their approved paths and
            # targeted database rows.
            post_start_rollouts = tuple(iter_rollouts(codex_home))
            post_start_rollouts_by_thread: dict[
                str,
                list[RolloutRecord],
            ] = defaultdict(list)
            for record in post_start_rollouts:
                post_start_rollouts_by_thread[record.thread_id].append(record)
            post_start_descendants = read_spawn_descendants(
                codex_home,
                (finding.thread_id for finding, _ in cascade_safe),
                strict=True,
                rollout_records=post_start_rollouts,
            )
            captured_scopes: list[
                tuple[Finding, set[str], Finding]
            ] = []
            capture_issues: list[str] = []
            for finding, descendants in cascade_safe:
                expected_scope, expected_present = (
                    resolved_expected_scopes[finding_key(finding)]
                )
                try:
                    verification_finding = _with_verification_scope(
                        finding,
                        descendants,
                        require_indexed_rollout_identity=(
                            expected_scopes is not None
                        ),
                        approved_integrity_types=(
                            resolved_integrity_approvals[
                                finding_key(finding)
                            ][0]
                        ),
                        capture_rollout_state_fingerprints=(
                            expected_scope is not None
                            and expected_scope.rollout_state_fingerprints
                            is not None
                        ),
                        capture_conversation_metadata_fingerprints=(
                            expected_scope is not None
                            and expected_scope.conversation_metadata_fingerprints
                            is not None
                        ),
                        rollout_records_by_thread=(
                            post_start_rollouts_by_thread
                        ),
                        current_descendants=post_start_descendants.get(
                            finding.thread_id,
                            set(),
                        ),
                    )
                except Exception as exc:
                    capture_issues.append(
                        f"{finding.thread_id}: {str(exc) or repr(exc)}"
                    )
                    continue
                if expected_scopes is not None:
                    assert expected_present and expected_scope is not None
                    captured_scope = _captured_deletion_scope(
                        verification_finding,
                        include_rollout_state_fingerprints=(
                            expected_scope.rollout_state_fingerprints
                            is not None
                        ),
                        include_conversation_metadata_fingerprints=(
                            expected_scope.conversation_metadata_fingerprints
                            is not None
                        ),
                    )
                    if captured_scope != expected_scope:
                        capture_issues.append(
                            f"{finding.thread_id}: exact native scope changed "
                            f"({_deletion_scope_difference(expected_scope, captured_scope)})"
                        )
                        continue
                captured_scopes.append(
                    (finding, descendants, verification_finding)
                )

            if capture_issues:
                error = (
                    "Cleanup was blocked because at least one selected "
                    "target's verification scope could not be captured after "
                    "app-server startup; no deletion request was sent: "
                    f"{'; '.join(capture_issues)}."
                )
                report.results.extend(
                    CleanupResult(
                        finding=finding,
                        status="unknown",
                        error=error,
                        impacted_thread_ids=tuple(
                            sorted({finding.thread_id, *descendants})
                        ),
                    )
                    for finding, descendants in cascade_safe
                )
                return

            for capture_index, (
                finding,
                descendants,
                verification_finding,
            ) in enumerate(captured_scopes):
                try:
                    if action_state_callback is not None:
                        action_state_callback(
                            "guard_started",
                            finding,
                            None,
                        )
                    if expected_scopes is not None:
                        expected_scope, expected_present = (
                            resolved_expected_scopes[finding_key(finding)]
                        )
                        assert expected_present and expected_scope is not None
                        immediate_rollouts = read_rollouts_at_paths(
                            finding.codex_home,
                            expected_scope.rollout_paths,
                            strict=True,
                        )
                        immediate_rollouts_by_thread: dict[
                            str,
                            list[RolloutRecord],
                        ] = defaultdict(list)
                        for record in immediate_rollouts:
                            immediate_rollouts_by_thread[
                                record.thread_id
                            ].append(record)
                        immediate_descendants = read_spawn_descendants(
                            finding.codex_home,
                            [finding.thread_id],
                            strict=True,
                            rollout_records=immediate_rollouts,
                        ).get(finding.thread_id, set())
                        immediate_finding = _with_verification_scope(
                            finding,
                            descendants,
                            require_indexed_rollout_identity=True,
                            approved_integrity_types=(
                                resolved_integrity_approvals[
                                    finding_key(finding)
                                ][0]
                            ),
                            capture_rollout_state_fingerprints=(
                                expected_scope.rollout_state_fingerprints
                                is not None
                            ),
                            capture_conversation_metadata_fingerprints=(
                                expected_scope.conversation_metadata_fingerprints
                                is not None
                            ),
                            rollout_records_by_thread=(
                                immediate_rollouts_by_thread
                            ),
                            current_descendants=immediate_descendants,
                        )
                        immediate_scope = _captured_deletion_scope(
                            immediate_finding,
                            include_rollout_state_fingerprints=(
                                expected_scope.rollout_state_fingerprints
                                is not None
                            ),
                            include_conversation_metadata_fingerprints=(
                                expected_scope.conversation_metadata_fingerprints
                                is not None
                            ),
                        )
                        if immediate_scope != expected_scope:
                            raise RuntimeError(
                                "exact native scope changed "
                                f"({_deletion_scope_difference(expected_scope, immediate_scope)})"
                            )
                        if pre_delete_validator is not None:
                            pre_delete_validator(finding)
                    if action_state_callback is not None:
                        # Persist this checkpoint before the irreversible
                        # request.  A persistence failure must prevent the
                        # request from being sent.
                        action_state_callback(
                            "mutation_started",
                            finding,
                            None,
                        )
                except Exception as exc:
                    error = (
                        "Cleanup was blocked by the immediate pre-delete "
                        f"scope check for {finding.thread_id}; no further "
                        f"deletion request was sent: {str(exc) or repr(exc)}."
                    )
                    report.results.extend(
                        CleanupResult(
                            finding=pending_finding,
                            status="unknown",
                            error=error,
                            impacted_thread_ids=tuple(
                                sorted(
                                    {
                                        pending_finding.thread_id,
                                        *pending_descendants,
                                    }
                                )
                            ),
                        )
                        for (
                            pending_finding,
                            pending_descendants,
                            _pending_verification,
                        ) in captured_scopes[capture_index:]
                    )
                    break
                request_error: str | None = None
                try:
                    server.delete_thread(finding.thread_id)
                except Exception as exc:
                    request_error = str(exc) or repr(exc)

                verification = _verify_with_retries(
                    verification_finding,
                    verifier=verifier,
                    attempts=verification_attempts,
                    interval=verification_interval,
                )
                status = verification.status or "unknown"
                if status == "deleted":
                    error = None
                elif status == "not_deleted":
                    error = (
                        verification.error
                        or request_error
                        or "Deletion was not observed; all planned targets remain"
                    )
                elif status == "partial":
                    error = (
                        verification.error
                        or request_error
                        or "Deletion was only partially completed"
                    )
                else:
                    error = (
                        verification.error
                        or request_error
                        or "Deletion could not be verified"
                    )
                cleanup_result = CleanupResult(
                    finding=finding,
                    status=status,
                    error=error,
                    request_error=request_error,
                    remaining_artifacts=verification.remaining_artifacts,
                    impacted_thread_ids=tuple(
                        sorted({finding.thread_id, *descendants})
                    ),
                )
                report.results.append(cleanup_result)
                if action_state_callback is not None:
                    action_state_callback(
                        "verified",
                        finding,
                        cleanup_result,
                    )
    except Exception as exc:
        attempted = {finding_key(item.finding) for item in report.results}
        error = str(exc) or repr(exc)
        report.results.extend(
            CleanupResult(
                finding=finding,
                status="unknown",
                error=error,
                impacted_thread_ids=tuple(
                    sorted({finding.thread_id, *descendants})
                ),
            )
            for finding, descendants in cascade_safe
            if finding_key(finding) not in attempted
        )


def _verify_with_retries(
    finding: Finding,
    *,
    verifier: FindingVerifier,
    attempts: int,
    interval: float,
) -> VerificationResult:
    result = VerificationResult(deleted=False, error="Deletion was not verified")
    for attempt in range(attempts):
        try:
            result = verifier(finding)
        except Exception as exc:
            result = VerificationResult(
                deleted=False,
                error=f"Could not verify deletion: {exc}",
            )
        if result.deleted:
            return result
        if attempt + 1 < attempts and interval:
            time.sleep(interval)
    return result


def _resolve_approved_descendants(
    finding: Finding,
    approved_descendants: ApprovedDescendants | None,
    *,
    thread_id_counts: Mapping[str, int],
) -> tuple[set[str], bool]:
    if approved_descendants is None:
        return set(), True

    key = finding_key(finding)
    raw_descendants: Iterable[str] | None = None
    if key in approved_descendants:
        raw_descendants = approved_descendants[key]
    elif (
        thread_id_counts.get(finding.thread_id) == 1
        and finding.thread_id in approved_descendants
    ):
        raw_descendants = approved_descendants[finding.thread_id]
    if raw_descendants is None:
        return set(), False

    return {
        thread_id
        for thread_id in raw_descendants
        if isinstance(thread_id, str)
        and thread_id
        and thread_id != finding.thread_id
    }, True


def _resolve_integrity_approval(
    finding: Finding,
    approved_integrity_deletes: ApprovedIntegrityDeletes | None,
    *,
    thread_id_counts: Mapping[str, int],
) -> tuple[set[str], bool]:
    if approved_integrity_deletes is None:
        return set(), False

    key = finding_key(finding)
    raw_types: Iterable[str] | str | None = None
    if key in approved_integrity_deletes:
        raw_types = approved_integrity_deletes[key]
    elif (
        thread_id_counts.get(finding.thread_id) == 1
        and finding.thread_id in approved_integrity_deletes
    ):
        raw_types = approved_integrity_deletes[finding.thread_id]
    if raw_types is None:
        return set(), False
    values = (raw_types,) if isinstance(raw_types, str) else raw_types
    return {
        finding_type
        for finding_type in values
        if isinstance(finding_type, str) and finding_type
    }, True


def _finding_after_integrity_approval(
    finding: Finding,
    approved_finding_types: set[str],
) -> Finding:
    details = _without_integrity_soft_blockers(
        finding.details,
        platform=finding.platform,
        approved_finding_types=approved_finding_types,
    )
    additional = details.get("additional_findings")
    if isinstance(additional, list):
        sanitized_additional: list[object] = []
        for item in additional:
            if not isinstance(item, dict):
                sanitized_additional.append(item)
                continue
            sanitized_item = dict(item)
            item_details = item.get("details")
            if isinstance(item_details, dict):
                sanitized_item["details"] = (
                    _without_integrity_soft_blockers(
                        item_details,
                        platform=str(item.get("platform", "")),
                        approved_finding_types=approved_finding_types,
                    )
                )
            sanitized_additional.append(sanitized_item)
        details["additional_findings"] = sanitized_additional
    return replace(finding, details=details)


def _without_integrity_soft_blockers(
    details: Mapping[str, Any],
    *,
    platform: str,
    approved_finding_types: set[str],
) -> dict[str, Any]:
    sanitized = dict(details)
    finding_type = sanitized.get("finding_type")
    if (
        platform.lower() != "native"
        or finding_type not in _APPROVABLE_INTEGRITY_FINDING_TYPES
        or finding_type not in approved_finding_types
        or _has_integrity_hard_signal(sanitized)
    ):
        return sanitized

    expected_code = (
        STANDALONE_RELATION_CLEANUP_UNAVAILABLE
        if finding_type == "residual_spawn_edge"
        else INTEGRITY_REVIEW_REQUIRED
    )
    if not exact_blocker_codes(sanitized, expected_code):
        return sanitized

    sanitized["cleanup_blocked_reason"] = None
    sanitized["cleanup_blocker_codes"] = []
    if sanitized.get("cleanable") is False:
        sanitized["cleanable"] = True
    if sanitized.get("thread_delete_supported") is False:
        sanitized["thread_delete_supported"] = True
    return sanitized


def _has_integrity_hard_signal(
    details: Mapping[str, Any],
) -> bool:
    if any(
        details.get(key) is True
        for key in (
            "needs_quarantine",
            "originator_conflict",
            "source_conflict",
            "identity_conflict",
            "live_reference_guard",
            "live_reference_self",
            "active_reference",
            "active_reference_self",
            "metadata_mismatch",
        )
    ):
        return True
    if details.get("ownership_status") in {
        "conflict",
        "insufficient",
        "unknown",
        "unconfirmed",
    }:
        return True
    if any(
        details.get(key) is False
        for key in (
            "cascade_check_available",
            "associated_scope_available",
            "relationship_scope_available",
            "scope_read_available",
            "spawn_edges_available",
        )
    ):
        return True
    metadata_thread_id = details.get("metadata_thread_id")
    if isinstance(metadata_thread_id, str) and metadata_thread_id:
        return True
    if any(
        isinstance(details.get(key), int)
        and details[key] > 0
        for key in (
            "active_reference_count",
            "frontend_reference_count",
            "frontend_active_reference_count",
            "live_reference_count",
            "live_descendant_reference_count",
        )
    ):
        return True
    live_descendants = details.get("live_descendant_thread_ids")
    return (
        isinstance(
            live_descendants,
            (list, tuple, set, frozenset),
        )
        and bool(live_descendants)
    )


def _resolve_expected_scope(
    finding: Finding,
    expected_scopes: ExpectedDeletionScopes | None,
    *,
    thread_id_counts: Mapping[str, int],
) -> tuple[ExpectedDeletionScope | None, bool]:
    if expected_scopes is None:
        return None, False

    key = finding_key(finding)
    raw_scope: ExpectedScopeValue | None = None
    if key in expected_scopes:
        raw_scope = expected_scopes[key]
    elif (
        thread_id_counts.get(finding.thread_id) == 1
        and finding.thread_id in expected_scopes
    ):
        raw_scope = expected_scopes[finding.thread_id]
    if raw_scope is None:
        return None, False

    if isinstance(raw_scope, ExpectedDeletionScope):
        descendants = raw_scope.descendant_thread_ids
        indexed = raw_scope.indexed_thread_ids
        rollout_paths = raw_scope.rollout_paths
        rollout_state_fingerprints = (
            raw_scope.rollout_state_fingerprints
        )
        conversation_metadata_fingerprints = (
            raw_scope.conversation_metadata_fingerprints
        )
    elif isinstance(raw_scope, Mapping):
        descendants = _scope_field_values(
            raw_scope,
            "descendant_thread_ids",
        )
        indexed = _scope_field_values(
            raw_scope,
            "indexed_thread_ids",
        )
        rollout_paths = _scope_field_values(
            raw_scope,
            "rollout_paths",
        )
        raw_fingerprints = raw_scope.get(
            "rollout_state_fingerprints"
        )
        rollout_state_fingerprints = (
            None
            if raw_fingerprints is None
            else _scope_field_values(
                raw_scope,
                "rollout_state_fingerprints",
            )
        )
        raw_metadata_fingerprints = raw_scope.get(
            "conversation_metadata_fingerprints"
        )
        conversation_metadata_fingerprints = (
            None
            if raw_metadata_fingerprints is None
            else _scope_field_values(
                raw_scope,
                "conversation_metadata_fingerprints",
            )
        )
    else:
        raise TypeError(
            "Expected deletion scopes must be ExpectedDeletionScope "
            "objects or mappings"
        )

    return ExpectedDeletionScope(
        descendant_thread_ids=tuple(
            sorted(
                {
                    item
                    for item in descendants
                    if isinstance(item, str)
                    and item
                    and item != finding.thread_id
                }
            )
        ),
        indexed_thread_ids=tuple(
            sorted(
                {
                    item
                    for item in indexed
                    if isinstance(item, str) and item
                }
            )
        ),
        rollout_paths=tuple(
            sorted(
                {
                    _normalize_scope_path(finding.codex_home, item)
                    for item in rollout_paths
                    if isinstance(item, (str, os.PathLike))
                    and os.fspath(item)
                }
            )
        ),
        rollout_state_fingerprints=(
            None
            if rollout_state_fingerprints is None
            else tuple(
                sorted(
                    {
                        item
                        for item in rollout_state_fingerprints
                        if isinstance(item, str) and item
                    }
                )
            )
        ),
        conversation_metadata_fingerprints=(
            None
            if conversation_metadata_fingerprints is None
            else tuple(
                sorted(
                    {
                        item
                        for item in conversation_metadata_fingerprints
                        if isinstance(item, str) and item
                    }
                )
            )
        ),
    ), True


def _scope_field_values(
    scope: Mapping[str, Iterable[Any] | None],
    field_name: str,
) -> tuple[Any, ...]:
    value = scope.get(field_name, ())
    if value is None:
        raise TypeError(
            f"Expected deletion scope field {field_name!r} must be iterable"
        )
    if isinstance(value, (str, os.PathLike)):
        return (value,)
    try:
        return tuple(value)
    except TypeError as exc:
        raise TypeError(
            f"Expected deletion scope field {field_name!r} must be iterable"
        ) from exc


def _captured_deletion_scope(
    finding: Finding,
    *,
    include_rollout_state_fingerprints: bool = False,
    include_conversation_metadata_fingerprints: bool = False,
) -> ExpectedDeletionScope:
    return ExpectedDeletionScope(
        descendant_thread_ids=tuple(
            finding.details.get(
                "captured_descendant_thread_ids", ()
            )
        ),
        indexed_thread_ids=tuple(
            finding.details.get(
                "captured_indexed_thread_ids", ()
            )
        ),
        rollout_paths=tuple(
            finding.details.get("captured_rollout_paths", ())
        ),
        rollout_state_fingerprints=(
            tuple(
                finding.details.get(
                    "captured_rollout_state_fingerprints",
                    (),
                )
            )
            if include_rollout_state_fingerprints
            else None
        ),
        conversation_metadata_fingerprints=(
            tuple(
                finding.details.get(
                    "captured_conversation_metadata_fingerprints",
                    (),
                )
            )
            if include_conversation_metadata_fingerprints
            else None
        ),
    )


def _deletion_scope_difference(
    expected: ExpectedDeletionScope,
    captured: ExpectedDeletionScope,
) -> str:
    changes: list[str] = []
    for field_name in (
        "descendant_thread_ids",
        "indexed_thread_ids",
        "rollout_paths",
        *(
            ("rollout_state_fingerprints",)
            if expected.rollout_state_fingerprints is not None
            else ()
        ),
        *(
            ("conversation_metadata_fingerprints",)
            if expected.conversation_metadata_fingerprints is not None
            else ()
        ),
    ):
        expected_values = set(getattr(expected, field_name))
        captured_values = set(getattr(captured, field_name))
        added = sorted(captured_values - expected_values)
        removed = sorted(expected_values - captured_values)
        if added:
            changes.append(
                f"{field_name} added: {', '.join(added)}"
            )
        if removed:
            changes.append(
                f"{field_name} removed: {', '.join(removed)}"
            )
    return "; ".join(changes) or "scope values differ"


def _overlapping_affected_scopes(
    findings: Sequence[Finding],
    approved_scopes: Mapping[
        tuple[str, str],
        tuple[set[str], bool],
    ],
) -> list[tuple[str, str, tuple[str, ...]]]:
    affected: list[tuple[str, set[str]]] = []
    for finding in findings:
        descendants, _present = approved_scopes[finding_key(finding)]
        affected.append(
            (
                finding.thread_id,
                {finding.thread_id, *descendants},
            )
        )

    overlaps: list[tuple[str, str, tuple[str, ...]]] = []
    for index, (left_root, left_ids) in enumerate(affected):
        for right_root, right_ids in affected[index + 1 :]:
            shared = tuple(sorted(left_ids & right_ids))
            if shared:
                overlaps.append((left_root, right_root, shared))
    return overlaps


def _with_verification_scope(
    finding: Finding,
    descendants: Iterable[str],
    *,
    require_indexed_rollout_identity: bool = False,
    approved_integrity_types: frozenset[str] | set[str] = frozenset(),
    capture_rollout_state_fingerprints: bool = False,
    capture_conversation_metadata_fingerprints: bool = False,
    rollout_records_by_thread: Mapping[
        str,
        Sequence[RolloutRecord],
    ]
    | None = None,
    current_descendants: set[str] | None = None,
) -> Finding:
    details = dict(finding.details)
    thread_ids = {
        finding.thread_id,
        *descendants,
    }
    details["planned_impact_thread_ids"] = sorted(
        thread_ids - {finding.thread_id}
    )

    supplied_rollout_records = (
        tuple(
            record
            for records in rollout_records_by_thread.values()
            for record in records
        )
        if rollout_records_by_thread is not None
        else None
    )
    if current_descendants is None:
        current_descendants = read_spawn_descendants(
            finding.codex_home,
            [finding.thread_id],
            strict=True,
            rollout_records=supplied_rollout_records,
        ).get(finding.thread_id, set())
    approved_descendants = thread_ids - {finding.thread_id}
    if current_descendants != approved_descendants:
        raise RuntimeError(
            "the current descendant closure changed after app-server startup"
        )

    indexed = read_thread_index(
        finding.codex_home,
        thread_ids,
        strict=True,
    )
    records_by_thread = (
        {
            thread_id: list(
                rollout_records_by_thread.get(thread_id, ())
            )
            for thread_id in thread_ids
        }
        if rollout_records_by_thread is not None
        else {
            thread_id: find_thread_rollouts(finding.codex_home, thread_id)
            for thread_id in thread_ids
        }
    )
    if capture_conversation_metadata_fingerprints:
        summaries = read_conversation_summaries(
            finding.codex_home,
            thread_ids,
            rollout_records_by_thread=records_by_thread,
            legacy_names=read_legacy_thread_names(
                finding.codex_home,
                thread_ids,
            ),
            strict=True,
        )
        details[
            "captured_conversation_metadata_fingerprints"
        ] = sorted(
            f"{thread_id}={summary.metadata_fingerprint}"
            for thread_id, summary in summaries.items()
        )
    parsed_rollout_paths = {
        record.path
        for records in records_by_thread.values()
        for record in records
    }
    if capture_rollout_state_fingerprints:
        details["captured_rollout_state_fingerprints"] = sorted(
            rollout_state_fingerprint(record)
            for records in records_by_thread.values()
            for record in records
        )
    indexed_rollout_paths = {
        path
        for row in indexed.values()
        if (
            path := _thread_index_rollout_path(
                finding.codex_home,
                row.get("rollout_path"),
            )
        )
        is not None
    }
    existing_indexed_rollout_paths = {
        path
        for path in indexed_rollout_paths
        if path.is_file()
    }
    if require_indexed_rollout_identity:
        identity_issues: list[str] = []
        for thread_id, records in records_by_thread.items():
            record_paths = {
                canonical_existing_path_key(record.path)
                for record in records
            }
            is_root = thread_id == finding.thread_id
            if len(records) > 1 and (
                not is_root
                or "duplicate_rollout"
                not in approved_integrity_types
            ):
                identity_issues.append(
                    f"{thread_id} has multiple current rollout files without "
                    "a target-specific duplicate_rollout approval"
                )

            indexed_row = indexed.get(thread_id)
            indexed_path = (
                _thread_index_rollout_path(
                    finding.codex_home,
                    indexed_row.get("rollout_path"),
                )
                if indexed_row is not None
                else None
            )
            indexed_path_key = (
                canonical_existing_path_key(indexed_path)
                if indexed_path is not None
                else None
            )
            if (
                indexed_path is not None
                and indexed_path.is_file()
                and indexed_path_key not in record_paths
            ):
                identity_issues.append(
                    f"{thread_id} existing indexed rollout metadata does "
                    "not confirm that thread identity"
                )
            elif (
                indexed_path is not None
                and record_paths
                and indexed_path_key not in record_paths
                and (
                    not is_root
                    or "index_rollout_path_mismatch"
                    not in approved_integrity_types
                )
            ):
                identity_issues.append(
                    f"{thread_id} has a current index/rollout path mismatch "
                    "without a target-specific "
                    "index_rollout_path_mismatch approval"
                )

            source_parent_values = [
                record.source
                for record in records
            ]
            if indexed_row is not None:
                source_parent_values.append(indexed_row.get("source"))
            source_parents = {
                parent
                for source in source_parent_values
                for parent in _structured_source_parent_ids(source)
            }
            orphan_parent_allowed = False
            orphan_details = _native_finding_details(
                finding,
                "orphaned_subagent_thread",
            ) if is_root else []
            if orphan_details:
                if len(orphan_details) != 1:
                    identity_issues.append(
                        f"{thread_id} has multiple native orphan contracts"
                    )
                else:
                    orphan_issue = _orphaned_root_parent_scope_issue(
                        finding,
                        orphan_details[0],
                        records,
                        indexed_row,
                        source_parents,
                    )
                    if orphan_issue is not None:
                        identity_issues.append(
                            f"{thread_id} approved orphan state changed "
                            f"({orphan_issue})"
                        )
                    else:
                        orphan_parent_allowed = True
            residual_parent_allowed = False
            if (
                is_root
                and "residual_spawn_edge"
                in approved_integrity_types
            ):
                residual_details = _native_finding_details(
                    finding,
                    "residual_spawn_edge",
                )
                if len(residual_details) != 1:
                    identity_issues.append(
                        f"{thread_id} residual_spawn_edge approval does not "
                        "match exactly one native observation"
                    )
                else:
                    residual_issue = _residual_root_scope_issue(
                        finding,
                        residual_details[0],
                        records,
                        indexed_row,
                        source_parents,
                    )
                    if residual_issue is not None:
                        identity_issues.append(
                            f"{thread_id} approved residual relation changed "
                            f"({residual_issue})"
                        )
                    else:
                        residual_parent_allowed = True
            outside_parents = sorted(source_parents - thread_ids)
            if outside_parents:
                orphan_parent_issue = (
                    None
                    if (
                        residual_parent_allowed
                        or orphan_parent_allowed
                    )
                    else "the exception is limited to one validated root parent"
                )
                if orphan_parent_issue is not None:
                    identity_issues.append(
                        f"{thread_id} source names parent(s) outside the "
                        f"approved affected scope ({orphan_parent_issue}): "
                        + ", ".join(outside_parents)
                    )
            if len(source_parents) > 1:
                identity_issues.append(
                    f"{thread_id} rollout files have conflicting structured "
                    "source parents"
                )

        parsed_path_keys = {
            canonical_existing_path_key(path)
            for path in parsed_rollout_paths
        }
        unowned_indexed_paths = sorted(
            (
                path
                for path in existing_indexed_rollout_paths
                if canonical_existing_path_key(path)
                not in parsed_path_keys
            ),
            key=canonical_existing_path_key,
        )
        if unowned_indexed_paths:
            identity_issues.append(
                "existing indexed rollout metadata does not belong to the "
                "selected root or an approved associated task: "
                + ", ".join(str(path) for path in unowned_indexed_paths)
            )
        if identity_issues:
            raise RuntimeError("; ".join(identity_issues))
    current_rollout_paths = (
        parsed_rollout_paths
        | existing_indexed_rollout_paths
    )
    details["captured_descendant_thread_ids"] = sorted(
        current_descendants
    )
    details["captured_indexed_thread_ids"] = sorted(indexed)
    details["captured_rollout_paths"] = sorted(
        {
            _normalize_scope_path(finding.codex_home, path)
            for path in current_rollout_paths
        }
    )

    # Exact planned scope follows the planner's "currently existing content
    # files" meaning. Every indexed path is nevertheless retained for
    # post-delete verification, including paths that are missing now but may
    # appear while deletion is in flight.
    known_paths = (
        set(parsed_rollout_paths) | indexed_rollout_paths
    )
    if finding.rollout is not None:
        known_paths.add(finding.rollout.path)
    known_paths.update(_detail_paths(details))
    details["planned_known_artifact_paths"] = sorted(
        (str(path) for path in known_paths),
        key=os.path.normcase,
    )

    expected_artifacts = {
        str(path)
        for path in known_paths
        if path.exists()
    }
    expected_artifacts.update(
        _index_artifact_marker(finding, thread_id)
        for thread_id in indexed
    )
    expected_artifacts.update(
        _edge_artifact_marker(parent, child)
        for parent, child in read_spawn_edges(
            finding.codex_home,
            thread_ids,
            strict=True,
            rollout_records=(
                record
                for records in records_by_thread.values()
                for record in records
            ),
        )
    )
    details["planned_expected_artifacts"] = sorted(expected_artifacts)
    return replace(finding, details=details)


def _structured_source_parent_ids(value: object) -> set[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return set()
    if not isinstance(value, dict):
        return set()

    candidates: list[object] = [value]
    subagent = value.get("subagent")
    if isinstance(subagent, dict):
        candidates.append(subagent)

    parents: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        spawn = candidate.get("thread_spawn")
        if not isinstance(spawn, dict):
            continue
        parent = spawn.get("parent_thread_id")
        if isinstance(parent, str) and parent:
            parents.add(parent)
    return parents


def _orphaned_root_parent_scope_issue(
    finding: Finding,
    details: Mapping[str, Any],
    records: Sequence[Any],
    indexed_row: Mapping[str, Any] | None,
    source_parents: set[str],
) -> str | None:
    """Validate the narrow missing-parent exception for a native orphan."""

    approved_parent = details.get("parent_thread_id")
    if (
        details.get("finding_type") != "orphaned_subagent_thread"
        or not isinstance(approved_parent, str)
        or not approved_parent
        or source_parents not in (set(), {approved_parent})
        or details.get("parent_indexed") is not False
        or details.get("parent_rollout_present") is not False
    ):
        return "the approved finding is not one exact missing-parent orphan"

    parent_index = read_thread_index(
        finding.codex_home,
        [approved_parent],
        strict=True,
    )
    if approved_parent in parent_index:
        return "the approved missing parent reappeared in the native index"
    if find_thread_rollouts(finding.codex_home, approved_parent):
        return "the approved missing parent reappeared as a valid rollout"

    edge_records = read_spawn_edge_records(
        finding.codex_home,
        [finding.thread_id, approved_parent],
        strict=True,
    )
    expected_edge_present = details.get("spawn_edge_present")
    if not isinstance(expected_edge_present, bool):
        return "the approved observation has no exact incoming-edge state"
    expected_edge = (approved_parent, finding.thread_id)
    incoming = {
        (edge.parent_thread_id, edge.child_thread_id)
        for edge in edge_records
        if edge.child_thread_id == finding.thread_id
    }
    allowed_edges = {expected_edge} if expected_edge_present else set()
    if incoming != allowed_edges:
        return "the current incoming relation differs from the approved observation"

    current_evidence = _current_subagent_evidence(records, indexed_row)
    if expected_edge_present and not source_parents:
        current_evidence.add("thread_spawn_edges")
    approved_evidence = details.get("subagent_evidence")
    if not isinstance(
        approved_evidence,
        (list, tuple, set, frozenset),
    ) or current_evidence != {
        item
        for item in approved_evidence
        if isinstance(item, str) and item
    }:
        return "current source evidence differs from the approved observation"

    evidence_strength = details.get("evidence_strength")
    if expected_edge_present:
        matching = [
            edge
            for edge in edge_records
            if (
                edge.parent_thread_id,
                edge.child_thread_id,
            )
            == expected_edge
        ]
        if (
            len(matching) != 1
            or matching[0].status != details.get("spawn_edge_status")
            or evidence_strength != "spawn_edge"
        ):
            return "the current incoming edge differs from the approved observation"
    elif (
        evidence_strength != "source_consensus"
        or details.get("requires_explicit_selection") is not True
    ):
        return "the edge-free exception lacks approved source consensus"
    return None


def _native_finding_details(
    finding: Finding,
    finding_type: str,
) -> list[Mapping[str, Any]]:
    matches: list[Mapping[str, Any]] = []
    if (
        finding.platform.lower() == "native"
        and finding.details.get("finding_type") == finding_type
    ):
        matches.append(finding.details)
    additional = finding.details.get("additional_findings")
    if isinstance(additional, list):
        for item in additional:
            if (
                isinstance(item, dict)
                and str(item.get("platform", "")).lower() == "native"
                and isinstance(item.get("details"), dict)
                and item["details"].get("finding_type") == finding_type
            ):
                matches.append(item["details"])
    return matches


def _residual_root_scope_issue(
    finding: Finding,
    details: Mapping[str, Any],
    records: Sequence[Any],
    indexed_row: Mapping[str, Any] | None,
    source_parents: set[str],
) -> str | None:
    parent_id = details.get("parent_thread_id")
    if (
        details.get("child_thread_id") != finding.thread_id
        or not isinstance(parent_id, str)
        or not parent_id
        or details.get("source_conflict") is not False
    ):
        return "the approved observation is not one exact safe residual edge"

    approved_source_parents = details.get("source_parent_ids")
    if not isinstance(
        approved_source_parents,
        (list, tuple, set, frozenset),
    ):
        return "the approved observation has no exact source-parent state"
    approved_parent_set = {
        item
        for item in approved_source_parents
        if isinstance(item, str) and item
    }
    if (
        source_parents != approved_parent_set
        or len(source_parents) > 1
        or bool(source_parents - {parent_id})
    ):
        return "current source parents differ from the approved observation"

    approved_evidence = details.get("subagent_evidence")
    if not isinstance(
        approved_evidence,
        (list, tuple, set, frozenset),
    ) or _current_subagent_evidence(records, indexed_row) != {
        item
        for item in approved_evidence
        if isinstance(item, str) and item
    }:
        return "current source evidence differs from the approved observation"

    parent_index = read_thread_index(
        finding.codex_home,
        [parent_id],
        strict=True,
    )
    parent_row = parent_index.get(parent_id)
    current_parent_index_missing = parent_row is None
    current_parent_rollout_present = bool(
        find_thread_rollouts(finding.codex_home, parent_id)
    )
    if parent_row is not None:
        parent_path = _thread_index_rollout_path(
            finding.codex_home,
            parent_row.get("rollout_path"),
        )
        current_parent_rollout_present = (
            current_parent_rollout_present
            or (
                parent_path is not None
                and parent_path.is_file()
            )
        )
    if (
        current_parent_index_missing
        is not details.get("parent_index_missing")
        or current_parent_rollout_present
        is not details.get("parent_rollout_present")
    ):
        return "current parent artifact state differs from the approved observation"

    current_child_index_missing = indexed_row is None
    current_child_rollout_present = bool(records)
    if indexed_row is not None:
        child_path = _thread_index_rollout_path(
            finding.codex_home,
            indexed_row.get("rollout_path"),
        )
        current_child_rollout_present = (
            current_child_rollout_present
            or (
                child_path is not None
                and child_path.is_file()
            )
        )
    if (
        current_child_index_missing
        is not details.get("child_index_missing")
        or current_child_rollout_present
        is not details.get("child_rollout_present")
    ):
        return "current child artifact state differs from the approved observation"

    edge_records = read_spawn_edge_records(
        finding.codex_home,
        [finding.thread_id, parent_id],
        strict=True,
    )
    expected_edge = (parent_id, finding.thread_id)
    incoming = [
        edge
        for edge in edge_records
        if edge.child_thread_id == finding.thread_id
    ]
    if (
        len(incoming) != 1
        or (
            incoming[0].parent_thread_id,
            incoming[0].child_thread_id,
        )
        != expected_edge
        or incoming[0].status != details.get("edge_status")
    ):
        return "current incoming edge differs from the approved observation"
    return None


def _current_subagent_evidence(
    records: Sequence[Any],
    indexed_row: Mapping[str, Any] | None,
) -> set[str]:
    evidence: set[str] = set()
    if indexed_row is not None:
        thread_source = indexed_row.get("thread_source")
        if (
            isinstance(thread_source, str)
            and thread_source.lower() == "subagent"
        ):
            evidence.add("threads.thread_source")
        if _source_declares_subagent(indexed_row.get("source")):
            evidence.add("threads.source")
    if any(_source_declares_subagent(record.source) for record in records):
        evidence.add("session_meta.source")
    return evidence


def _source_declares_subagent(value: object) -> bool:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() == "subagent":
            return True
        try:
            value = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return False
    return isinstance(value, dict) and (
        "subagent" in value or "thread_spawn" in value
    )


def _detail_paths(details: dict[str, Any]) -> list[Path]:
    result: list[Path] = []
    for key in (
        "artifact_path",
        "rollout_path",
        "rollout_paths",
        "indexed_rollout_path",
        "actual_rollout_paths",
        "alternate_rollout_paths",
        "session_index_path",
        "artifact_paths",
        "remaining_paths",
        "planned_known_artifact_paths",
    ):
        value = details.get(key)
        values = value if isinstance(value, (list, tuple)) else (value,)
        for item in values:
            if isinstance(item, (str, os.PathLike)) and os.fspath(item):
                result.append(Path(item))
    return result


def _index_artifact_marker(
    finding: Finding,
    thread_id: str,
) -> str:
    marker = f"index:{finding.codex_home / 'state_5.sqlite'}"
    if thread_id != finding.thread_id:
        marker = f"{marker}:{thread_id}"
    return marker


def _thread_index_rollout_path(
    codex_home: Path,
    raw_path: object,
) -> Path | None:
    if not isinstance(raw_path, (str, os.PathLike)):
        return None
    rendered = os.fspath(raw_path)
    if not rendered:
        return None
    path = Path(rendered).expanduser()
    if not path.is_absolute():
        path = codex_home / path
    return path


def _normalize_scope_path(
    codex_home: Path,
    raw_path: str | os.PathLike[str],
) -> str:
    path = _thread_index_rollout_path(codex_home, raw_path)
    assert path is not None
    return canonical_existing_path_key(path)


def _edge_artifact_marker(parent: str, child: str) -> str:
    return f"spawn-edge:{parent}->{child}"


def _detail_thread_ids(details: Mapping[str, Any]) -> set[str]:
    values: list[object] = []
    for key in (
        "planned_impact_thread_ids",
        "approved_descendant_thread_ids",
    ):
        value = details.get(key)
        if isinstance(value, (list, tuple, set, frozenset)):
            values.extend(value)
    return {
        item
        for item in values
        if isinstance(item, str) and item
    }


def _apply_live_reference_protection(
    findings: list[Finding],
    adapters: Sequence[FrontendAdapter],
) -> tuple[list[Finding], list[ScanFailure]]:
    protected: dict[str, set[str]] = defaultdict(set)
    for adapter in adapters:
        live_ids = getattr(adapter, "live_thread_ids", ())
        if not live_ids:
            continue
        codex_home = getattr(adapter, "codex_home", None)
        if codex_home is None:
            continue
        home = canonical_existing_path_key(codex_home)
        protected[home].update(
            thread_id
            for thread_id in live_ids
            if isinstance(thread_id, str) and thread_id
        )
    if not any(protected.values()):
        return findings, []

    by_home: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_home[finding_key(finding)[0]].append(finding)

    descendants: dict[tuple[str, str], set[str]] = {}
    errors: list[ScanFailure] = []
    for home, home_findings in by_home.items():
        live_ids = protected.get(home, set())
        if not live_ids:
            continue
        try:
            mapping = read_spawn_descendants(
                home_findings[0].codex_home,
                (finding.thread_id for finding in home_findings),
                strict=True,
            )
        except Exception as exc:
            errors.append(
                ScanFailure(
                    platform="live-reference-guard",
                    message=str(exc) or repr(exc),
                    error_type=type(exc).__name__,
                    codex_home=home_findings[0].codex_home,
                )
            )
            continue
        for thread_id, child_ids in mapping.items():
            descendants[(home, thread_id)] = child_ids

    guarded: list[Finding] = []
    for finding in findings:
        home, thread_id = finding_key(finding)
        live_ids = protected.get(home, set())
        live_self = thread_id in live_ids
        live_descendants = sorted(
            descendants.get((home, thread_id), set()) & live_ids
        )
        if not live_self and not live_descendants:
            guarded.append(finding)
            continue

        details = dict(finding.details)
        reasons: list[str] = []
        blocker_codes = set(cleanup_blocker_codes(details))
        existing_reason = details.get("cleanup_blocked_reason")
        if isinstance(existing_reason, str) and existing_reason.strip():
            reasons.append(existing_reason.strip())
        if live_self:
            reasons.append(
                "The thread is still referenced by a live frontend session."
            )
        if live_descendants:
            reasons.append(
                "thread/delete would cascade into a thread still referenced "
                "by a live frontend session."
            )
        details.update(
            {
                "cleanable": False,
                "live_reference_guard": True,
                "live_reference_self": live_self,
                "live_descendant_reference_count": len(live_descendants),
                "live_descendant_thread_ids": live_descendants,
                "cleanup_blocked_reason": " ".join(reasons),
                "cleanup_blocker_codes": sorted(
                    blocker_codes | {LIVE_FRONTEND_REFERENCE}
                ),
            }
        )
        guarded.append(replace(finding, details=details))
    return guarded, errors


__all__ = [
    "ApprovedDescendants",
    "ApprovedIntegrityDeletes",
    "AppServerFactory",
    "BinaryResolver",
    "CleanupTargetKey",
    "ExpectedDeletionScope",
    "ExpectedDeletionScopes",
    "ExpectedScopeValue",
    "CleanupReport",
    "CleanupResult",
    "FindingVerifier",
    "PreDeleteValidator",
    "ScanFailure",
    "ScanReport",
    "ThreadSelectionError",
    "VerificationResult",
    "clean_findings",
    "cleanup_block_reason",
    "deduplicate_findings",
    "finding_key",
    "scan_adapters",
    "select_findings",
    "verify_finding_deleted",
]
