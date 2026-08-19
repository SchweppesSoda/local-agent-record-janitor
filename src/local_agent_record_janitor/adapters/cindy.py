from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ..blocker_codes import (
    CASCADE_REQUIRES_EXPLICIT_SCOPE,
    IDENTITY_CONFLICT,
    LIVE_FRONTEND_REFERENCE,
    SPAWN_EDGE_OPEN,
)
from ..codex_state import iter_rollouts
from ..cindy_references import (
    CindyNativeReference,
    build_cindy_reference_catalog,
)
from ..discovery import discover_cindy_codex
from ..models import Finding, RolloutRecord
from .base import (
    AdapterScanError,
    FrontendAdapter,
    read_codex_evidence,
)


class CindyAdapter(FrontendAdapter):
    name = "cindy"

    def __init__(
        self,
        *,
        database: Path,
        codex_home: Path,
        cindy_root: Path | None = None,
        codex_bin_hint: Path | None = None,
    ) -> None:
        super().__init__(database=database, codex_home=codex_home)
        root = cindy_root or database.parent
        self.cindy_root = root.expanduser()
        self.codex_bin_hint = codex_bin_hint or discover_cindy_codex(root)

    def list_sessions(self) -> list["FrontendSessionRecord"]:
        """Read all Cindy Codex rows, including unassigned sessions."""

        from ..inventory import FrontendSessionRecord

        references = self._codex_references()
        return [
            FrontendSessionRecord(
                platform=self.name,
                platform_session_id=reference.cindy_session_id,
                thread_id=reference.native_session_id,
                database=self.database,
                codex_home=self.codex_home,
                backend="codex",
                status=reference.session_status,
                updated_at_ms=reference.session_updated_at_ms,
                title=_display_string((reference.session_details or {}).get("title")),
                is_live=reference.is_live,
                details={
                    "agent_kind": reference.agent_kind,
                    "source": (reference.session_details or {}).get("source"),
                    "created_at": (reference.session_details or {}).get("created_at"),
                    "parent_session_id": (reference.session_details or {}).get("parent_session_id"),
                    "working_dir": reference.working_dir,
                    "reference_kind": reference.reference_kind,
                    "historical": reference.is_historical,
                    "boundary_id": reference.boundary_id,
                    "boundary_created_at_ms": reference.boundary_created_at_ms,
                    "boundary_rewind_at_ms": reference.boundary_rewind_at_ms,
                    "cindy_profile_root": str(reference.profile_root),
                },
                codex_bin_hint=self.codex_bin_hint,
            )
            for reference in references
        ]

    def _codex_references(self) -> tuple[CindyNativeReference, ...]:
        catalog = build_cindy_reference_catalog(
            self.database,
            profile_root=self.cindy_root,
        )
        if catalog.failures:
            raise AdapterScanError(catalog.failures[0].message)
        return catalog.for_backend("codex")

    def scan(self) -> list[Finding]:
        self._replace_live_thread_ids(set())
        if not self.available:
            return []
        references = self._codex_references()
        live_references = [
            reference
            for reference in references
            if reference.is_live and reference.native_session_id is not None
        ]
        self._replace_live_thread_ids(
            {reference.native_session_id for reference in live_references if reference.native_session_id}
        )
        rows = [
            reference
            for reference in references
            if not reference.is_live and reference.native_session_id is not None
        ]
        if not rows:
            return []

        thread_ids = [reference.native_session_id for reference in rows if reference.native_session_id]
        try:
            rollout_groups = _rollouts_by_thread(self.codex_home, set(thread_ids))
        except OSError as exc:
            raise AdapterScanError(
                f"Could not inspect Codex rollouts in {self.codex_home}: {exc}"
            ) from exc
        evidence = read_codex_evidence(self.codex_home, thread_ids)

        findings: list[Finding] = []
        for reference in rows:
            thread_id = reference.native_session_id
            assert thread_id is not None
            records = rollout_groups.get(thread_id, [])
            rollout = _preferred_rollout(records)
            state_row = evidence.indexed_threads.get(thread_id)
            originators = {
                normalized
                for record in records
                if (normalized := _normalized_string(record.originator)) is not None
            }
            foreign_originators = originators - {"cindy", "xdt-maker"}
            ownership_conflict = bool(foreign_originators)
            live_reference_count = sum(
                item.native_session_id == thread_id for item in live_references
            )
            descendants = evidence.descendants_by_parent.get(thread_id, ())
            cascade_safe = evidence.spawn_edges_available and not descendants
            blocked_reasons = [
                reason
                for reason in (
                    (
                        "Cindy identifies the session as Codex, but the rollout "
                        "has a foreign originator."
                        if ownership_conflict
                        else None
                    ),
                    (
                        "The same Codex thread is still referenced by a live "
                        "Cindy session."
                        if live_reference_count
                        else None
                    ),
                    (
                        "Codex thread/delete would cascade into known descendant "
                        "threads."
                        if descendants
                        else (
                            "Cascade safety could not be verified because "
                            "thread_spawn_edges evidence is unavailable."
                            if not evidence.spawn_edges_available
                            else None
                        )
                    ),
                )
                if reason is not None
            ]
            blocker_codes = [
                code
                for code, blocked in (
                    (IDENTITY_CONFLICT, ownership_conflict),
                    (LIVE_FRONTEND_REFERENCE, bool(live_reference_count)),
                    (CASCADE_REQUIRES_EXPLICIT_SCOPE, bool(descendants)),
                    (SPAWN_EDGE_OPEN, not evidence.spawn_edges_available),
                )
                if blocked
            ]
            cleanable = (
                not ownership_conflict
                and live_reference_count == 0
                and cascade_safe
            )
            frontend_reference = _cindy_reference_evidence(reference)
            findings.append(
                Finding(
                    platform=self.name,
                    platform_session_id=reference.cindy_session_id,
                    thread_id=thread_id,
                    reason="Cindy session is soft-deleted but its Codex thread remains",
                    platform_db=self.database,
                    codex_home=self.codex_home,
                    platform_updated_at_ms=reference.session_updated_at_ms,
                    rollout=rollout,
                    codex_indexed=state_row is not None,
                    codex_archived=bool(state_row["archived"]) if state_row else None,
                    codex_bin_hint=self.codex_bin_hint,
                    details={
                        "frontend_reference": frontend_reference,
                        "frontend_reference_cleanable": (
                            not ownership_conflict
                            and frontend_reference.get("exact") is True
                        ),
                        "session_status": reference.session_status,
                        "source": (reference.session_details or {}).get("source"),
                        "parent_session_id": (reference.session_details or {}).get("parent_session_id"),
                        "reference_kind": reference.reference_kind,
                        "boundary_id": reference.boundary_id,
                        "boundary_created_at_ms": reference.boundary_created_at_ms,
                        "boundary_rewind_at_ms": reference.boundary_rewind_at_ms,
                        "cindy_profile_root": str(reference.profile_root),
                        "rollout_originator": rollout.originator if rollout else None,
                        "rollout_originators": sorted(originators),
                        "ownership_status": (
                            "conflict" if ownership_conflict else "confirmed"
                        ),
                        "ownership_evidence": {
                            "agent_kind": reference.agent_kind,
                            "expected_originator": "cindy",
                            "expected_originators": ["cindy", "xdt-maker"],
                            "observed_originators": sorted(originators),
                            "legacy_missing_originator_accepted": not originators,
                        },
                        "originator_conflict": ownership_conflict,
                        "live_reference_count": live_reference_count,
                        "cleanable": cleanable,
                        "thread_delete_supported": True,
                        "needs_quarantine": (
                            ownership_conflict
                            or not evidence.spawn_edges_available
                        ),
                        "cascade_safe": cascade_safe,
                        "cascade_check_available": evidence.spawn_edges_available,
                        "cascade_descendant_count": len(descendants),
                        "cascade_descendant_thread_ids": list(descendants),
                        "has_unreviewed_descendants": (
                            bool(descendants)
                            or not evidence.spawn_edges_available
                        ),
                        "cleanup_blocked_reason": (
                            " ".join(blocked_reasons) if blocked_reasons else None
                        ),
                        "cleanup_blocker_codes": blocker_codes,
                    },
                )
            )
        return findings


def _rollouts_by_thread(
    codex_home: Path,
    thread_ids: set[str],
) -> dict[str, list[RolloutRecord]]:
    grouped: dict[str, list[RolloutRecord]] = defaultdict(list)
    for record in iter_rollouts(codex_home):
        if record.thread_id in thread_ids:
            grouped[record.thread_id].append(record)
    return dict(grouped)


def _preferred_rollout(records: list[RolloutRecord]) -> RolloutRecord | None:
    if not records:
        return None
    return sorted(
        records,
        key=lambda record: (record.archived, str(record.path).casefold()),
    )[0]


def _normalized_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _display_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _cindy_reference_evidence(
    reference: CindyNativeReference,
) -> dict[str, object]:
    historical = reference.reference_kind == "agent_switch"
    exact = bool(
        reference.session_schema_fingerprint
        and reference.session_row_fingerprint
        and (
            not historical
            or (
                reference.boundary_id
                and reference.message_schema_fingerprint
                and reference.message_row_fingerprint
                and reference.message_content_sha256
            )
        )
    )
    return {
        "schema_version": 1,
        "platform": "cindy",
        "database": str(reference.database.expanduser().absolute()),
        "operation": (
            "remove_agent_switch_from_sdk_session_id"
            if historical
            else "clear_session_sdk_session_id"
        ),
        "table": "messages" if historical else "sessions",
        "locator": {
            "cindy_session_id": reference.cindy_session_id,
            "message_id": reference.boundary_id,
        },
        "expected": {
            "native_session_id": reference.native_session_id,
            "agent_kind": reference.agent_kind,
            "reference_kind": reference.reference_kind,
        },
        "session_schema_fingerprint": (
            reference.session_schema_fingerprint
        ),
        "session_row_fingerprint": reference.session_row_fingerprint,
        "message_schema_fingerprint": (
            reference.message_schema_fingerprint
        ),
        "message_row_fingerprint": reference.message_row_fingerprint,
        "message_content_sha256": reference.message_content_sha256,
        "exact": exact,
    }
