from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

from ..codex_desktop_state import DesktopStateError, read_desktop_state
from ..blocker_codes import (
    CASCADE_REQUIRES_EXPLICIT_SCOPE,
    IDENTITY_CONFLICT,
    INTEGRITY_REVIEW_REQUIRED,
    LEGACY_INDEX_NOT_THREAD_TARGET,
    NO_NATIVE_ARTIFACT,
    SOURCE_PARENT_UNVERIFIED,
    SPAWN_EDGE_OPEN,
    STANDALONE_RELATION_CLEANUP_UNAVAILABLE,
)
from ..codex_state import _read_rollout_meta
from ..discovery import discover_path_codex
from ..models import Finding, RolloutRecord
from ..path_identity import canonical_existing_path_key
from ..sqlite_utils import connect_readonly, table_exists
from .base import FrontendAdapter


class NativeIntegrityError(RuntimeError):
    """The native Codex state could not be audited reliably."""


class NativeIntegrityAdapter(FrontendAdapter):
    """Find inconsistencies among Codex's native session artifacts.

    The adapter deliberately stops at diagnosis.  In particular, it never
    repairs SQLite rows, rewrites ``session_index.jsonl``, or removes rollout
    files.  Cleanup callers can use the eligibility flags in ``details`` to
    decide whether the official ``thread/delete`` method is applicable.
    """

    name = "native"

    def __init__(
        self,
        *,
        codex_home: Path,
        codex_bin_hint: Path | None = None,
    ) -> None:
        super().__init__(
            database=codex_home.expanduser() / "state_5.sqlite",
            codex_home=codex_home,
        )
        self.codex_bin_hint = codex_bin_hint or discover_path_codex()

    @property
    def available(self) -> bool:
        # A missing or unreadable state database is unavailable evidence, not
        # proof that every rollout is orphaned.
        return self.codex_home.is_dir() and self.database.is_file()

    def scan(self) -> list[Finding]:
        if not self.available:
            return []

        snapshot = self._read_state_snapshot()
        if snapshot is None:
            return []
        threads, edges = snapshot
        rollouts = _scan_all_rollouts(self.codex_home)

        findings: list[Finding] = []
        findings.extend(self._scan_thread_artifacts(threads, rollouts, edges))

        orphan_findings, claimed_edges = self._scan_orphaned_subagents(
            threads, rollouts, edges
        )
        findings.extend(orphan_findings)
        findings.extend(
            self._scan_residual_spawn_edges(
                threads,
                rollouts,
                edges,
                claimed_edges=claimed_edges,
            )
        )

        try:
            findings.extend(self._scan_desktop_state(threads, rollouts))
        except DesktopStateError as exc:
            raise NativeIntegrityError(
                f"Could not read Codex Desktop host state: {exc}"
            ) from exc

        legacy_finding = self._scan_legacy_session_index(threads, rollouts)
        if legacy_finding is not None:
            findings.append(legacy_finding)

        return sorted(
            findings,
            key=lambda finding: (
                str(finding.details.get("finding_type", "")),
                finding.thread_id,
                finding.platform_session_id,
            ),
        )

    def list_sessions(self) -> list[Any]:
        """Expose Codex Desktop catalog rows to the unified read-only catalog."""

        try:
            snapshot = read_desktop_state(self.codex_home)
        except DesktopStateError as exc:
            raise NativeIntegrityError(
                f"Could not read Codex Desktop host state: {exc}"
            ) from exc
        if snapshot.database is None:
            return []

        # Import lazily to keep the adapter/inventory compatibility boundary
        # free of a module-level cycle.
        from ..inventory import FrontendSessionRecord

        sessions: list[FrontendSessionRecord] = []
        for thread_id, state in sorted(snapshot.threads.items()):
            for record in state.catalog_records:
                sessions.append(
                    FrontendSessionRecord(
                        platform="codex-desktop",
                        platform_session_id=(
                            f"{record.host_id}:{record.thread_id}"
                        ),
                        thread_id=thread_id,
                        database=record.database,
                        codex_home=self.codex_home,
                        backend="codex",
                        status="cataloged",
                        title=record.title,
                        # This is a host catalog/cache reference, not an
                        # independent live-session ownership guard.
                        is_live=False,
                        details={
                            "reference_kind": "desktop_host_catalog",
                            "host_id": record.host_id,
                            "snapshot_fingerprint": (
                                state.snapshot_fingerprint
                            ),
                            "global_state_reference_count": (
                                state.exact_reference_count
                            ),
                        },
                        codex_bin_hint=self.codex_bin_hint,
                    )
                )
        return sessions

    def _scan_desktop_state(
        self,
        threads: dict[str, dict[str, Any]],
        rollouts: dict[str, list[RolloutRecord]],
    ) -> list[Finding]:
        snapshot = read_desktop_state(self.codex_home)
        if snapshot.database is None:
            return []

        findings: list[Finding] = []
        native_ids = set(threads) | set(rollouts)
        for thread_id, state in sorted(snapshot.threads.items()):
            if thread_id in native_ids or not state.catalog_records:
                continue
            local_records = tuple(
                record
                for record in state.catalog_records
                if record.host_id == "local"
            )
            if not local_records:
                continue
            titles = sorted(
                {
                    record.title
                    for record in local_records
                    if isinstance(record.title, str) and record.title
                }
            )
            findings.append(
                Finding(
                    platform="codex-desktop",
                    platform_session_id=f"local:{thread_id}",
                    thread_id=thread_id,
                    reason=(
                        "Codex Desktop host catalog remains after the native "
                        "thread row and rollout were deleted"
                    ),
                    platform_db=snapshot.database,
                    codex_home=self.codex_home,
                    rollout=None,
                    codex_indexed=False,
                    codex_archived=None,
                    codex_bin_hint=self.codex_bin_hint,
                    details={
                        "finding_type": "desktop_state_orphan",
                        "desktop_database": str(snapshot.database),
                        "desktop_host_id": "local",
                        "desktop_catalog_record_count": len(local_records),
                        "desktop_catalog_titles": titles,
                        "desktop_global_state_paths": sorted(
                            state.global_state_references
                        ),
                        "desktop_global_state_reference_count": (
                            state.exact_reference_count
                        ),
                        "desktop_state_snapshot_fingerprint": (
                            state.snapshot_fingerprint
                        ),
                        "thread_delete_supported": False,
                        "cleanable": True,
                        "requires_explicit_selection": True,
                        "needs_quarantine": False,
                        "manual_review_required": True,
                        "diagnostic_artifact_present": True,
                        "private_host_schema": True,
                    },
                )
            )
        return findings

    def _read_state_snapshot(
        self,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]] | None:
        try:
            with closing(connect_readonly(self.database)) as connection:
                if not table_exists(connection, "threads"):
                    raise NativeIntegrityError(
                        "Codex state database has no threads table"
                    )
                columns = _table_columns(connection, "threads")
                if not {"id", "rollout_path"}.issubset(columns):
                    raise NativeIntegrityError(
                        "Codex threads table is missing required id/rollout_path columns"
                    )

                optional_columns = (
                    "archived",
                    "created_at",
                    "updated_at",
                    "source",
                    "thread_source",
                    "agent_nickname",
                    "agent_role",
                )
                projections = ["id", "rollout_path"]
                projections.extend(
                    column if column in columns else f"NULL AS {column}"
                    for column in optional_columns
                )
                rows = connection.execute(
                    f"SELECT {', '.join(projections)} FROM threads"
                ).fetchall()
                threads = {
                    row["id"]: dict(row)
                    for row in rows
                    if isinstance(row["id"], str) and row["id"]
                }

                edges: list[dict[str, Any]] = []
                if table_exists(connection, "thread_spawn_edges"):
                    edge_columns = _table_columns(connection, "thread_spawn_edges")
                    required = {"parent_thread_id", "child_thread_id"}
                    if required.issubset(edge_columns):
                        status_projection = (
                            "status" if "status" in edge_columns else "NULL AS status"
                        )
                        edge_rows = connection.execute(
                            """
                            SELECT parent_thread_id, child_thread_id,
                                   %s
                            FROM thread_spawn_edges
                            """
                            % status_projection
                        ).fetchall()
                        edges = [
                            dict(row)
                            for row in edge_rows
                            if isinstance(row["parent_thread_id"], str)
                            and row["parent_thread_id"]
                            and isinstance(row["child_thread_id"], str)
                            and row["child_thread_id"]
                        ]
        except NativeIntegrityError:
            raise
        except (OSError, sqlite3.Error) as exc:
            # A corrupt, locked, or incompatible state database must not make
            # the whole command crash or turn unavailable evidence into
            # hundreds of false positives.  scan_adapters records this as a
            # visible per-adapter failure and continues other scanners.
            raise NativeIntegrityError(
                f"Could not read Codex state database: {exc}"
            ) from exc
        return threads, edges

    def _scan_thread_artifacts(
        self,
        threads: dict[str, dict[str, Any]],
        rollouts: dict[str, list[RolloutRecord]],
        edges: list[dict[str, Any]],
    ) -> list[Finding]:
        findings: list[Finding] = []
        child_edges_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in edges:
            child_edges_by_parent[edge["parent_thread_id"]].append(edge)
        records_by_path = {
            _path_key(record.path): record
            for records in rollouts.values()
            for record in records
        }

        for thread_id, row in threads.items():
            expected_path = _indexed_rollout_path(self.codex_home, row)
            alternatives = rollouts.get(thread_id, [])
            expected_record = (
                records_by_path.get(_path_key(expected_path))
                if expected_path is not None and _is_file(expected_path)
                else None
            )
            if (
                expected_record is not None
                and expected_record.thread_id != thread_id
            ):
                rollout = (
                    _preferred_rollout(alternatives) if alternatives else None
                )
                findings.append(
                    self._finding(
                        thread_id=thread_id,
                        reason=(
                            "Codex thread index rollout metadata belongs to a "
                            "different thread"
                        ),
                        rollout=rollout,
                        codex_indexed=True,
                        codex_archived=_optional_bool(row.get("archived")),
                        details={
                            "finding_type": "index_rollout_metadata_mismatch",
                            "indexed_rollout_path": str(expected_path),
                            "metadata_thread_id": expected_record.thread_id,
                            "alternate_rollout_paths": [
                                str(record.path) for record in alternatives
                            ],
                            "thread_delete_supported": False,
                            "needs_quarantine": False,
                            "cleanable": False,
                            "cleanup_blocked_reason": (
                                "The indexed file belongs to another thread; "
                                "automatic deletion could remove unrelated data."
                            ),
                            "cleanup_blocker_codes": [IDENTITY_CONFLICT],
                            "manual_review_required": True,
                        },
                    )
                )
                continue

            if len(alternatives) > 1:
                findings.append(
                    self._duplicate_rollout_finding(
                        thread_id=thread_id,
                        records=alternatives,
                        row=row,
                        expected_path=expected_path,
                    )
                )
                continue

            if expected_path is not None and _is_file(expected_path):
                continue

            archived = _optional_bool(row.get("archived"))
            if alternatives:
                rollout = _preferred_rollout(alternatives)
                findings.append(
                    self._finding(
                        thread_id=thread_id,
                        reason=(
                            "Codex thread index points to a missing rollout path, "
                            "but the same thread was found elsewhere"
                        ),
                        rollout=rollout,
                        codex_indexed=True,
                        codex_archived=archived,
                        details={
                            "finding_type": "index_rollout_path_mismatch",
                            "indexed_rollout_path": (
                                str(expected_path) if expected_path else None
                            ),
                            "actual_rollout_paths": [
                                str(record.path) for record in alternatives
                            ],
                            # A path mismatch may be recoverable.  Do not turn
                            # an integrity finding into destructive cleanup.
                            "thread_delete_supported": False,
                            "needs_quarantine": False,
                            "cleanable": False,
                            "cleanup_blocked_reason": (
                                "The alternate rollout may be recoverable; "
                                "deletion is not a safe path repair."
                            ),
                            "cleanup_blocker_codes": [INTEGRITY_REVIEW_REQUIRED],
                            "manual_review_required": True,
                        },
                    )
                )
                continue

            descendant_edges = child_edges_by_parent.get(thread_id, [])
            cleanable = not descendant_edges
            findings.append(
                self._finding(
                    thread_id=thread_id,
                    reason="Codex thread index remains but its rollout is missing",
                    rollout=None,
                    codex_indexed=True,
                    codex_archived=archived,
                    details={
                        "finding_type": "index_missing_rollout",
                        "indexed_rollout_path": (
                            str(expected_path) if expected_path else None
                        ),
                        # The thread still has a native index row, so the
                        # official app-server deletion path can be attempted.
                        "thread_delete_supported": True,
                        "needs_quarantine": False,
                        # thread/delete cascades into spawned descendants.  A
                        # parent with any child edge is therefore fail-closed.
                        "cleanable": cleanable,
                        "cleanup_blocked_reason": (
                            None
                            if cleanable
                            else "thread/delete would cascade into spawned descendants."
                        ),
                        "cleanup_blocker_codes": (
                            []
                            if cleanable
                            else [CASCADE_REQUIRES_EXPLICIT_SCOPE]
                        ),
                        "spawn_descendant_edge_count": len(descendant_edges),
                    },
                )
            )

        for thread_id, records in rollouts.items():
            if thread_id in threads:
                continue
            if len(records) > 1:
                findings.append(
                    self._duplicate_rollout_finding(
                        thread_id=thread_id,
                        records=records,
                        row=None,
                        expected_path=None,
                    )
                )
                continue
            rollout = _preferred_rollout(records)
            descendant_edges = child_edges_by_parent.get(thread_id, [])
            cleanable = not descendant_edges
            findings.append(
                self._finding(
                    thread_id=thread_id,
                    reason="Codex rollout exists without a native thread index row",
                    rollout=rollout,
                    codex_indexed=False,
                    codex_archived=rollout.archived,
                    details={
                        "finding_type": "rollout_missing_index",
                        "rollout_paths": [str(record.path) for record in records],
                        # Codex 0.144.6 was verified to resolve and delete a
                        # canonical rollout even when its threads row is gone.
                        "thread_delete_supported": True,
                        "needs_quarantine": False,
                        "cleanable": cleanable,
                        "cleanup_blocked_reason": (
                            None
                            if cleanable
                            else "thread/delete would cascade into spawned descendants."
                        ),
                        "cleanup_blocker_codes": (
                            []
                            if cleanable
                            else [CASCADE_REQUIRES_EXPLICIT_SCOPE]
                        ),
                        "spawn_descendant_edge_count": len(descendant_edges),
                    },
                )
            )

        return findings

    def _duplicate_rollout_finding(
        self,
        *,
        thread_id: str,
        records: list[RolloutRecord],
        row: dict[str, Any] | None,
        expected_path: Path | None,
    ) -> Finding:
        rollout = _preferred_rollout(records)
        return self._finding(
            thread_id=thread_id,
            reason="Multiple Codex rollout files contain the same thread ID",
            rollout=rollout,
            codex_indexed=row is not None,
            codex_archived=_thread_archived(row, rollout),
            details={
                "finding_type": "duplicate_rollout",
                "rollout_paths": [str(record.path) for record in records],
                "rollout_count": len(records),
                "indexed_rollout_path": (
                    str(expected_path) if expected_path is not None else None
                ),
                "indexed_path_exists": (
                    _is_file(expected_path) if expected_path is not None else False
                ),
                "thread_delete_supported": False,
                "needs_quarantine": False,
                "cleanable": False,
                "cleanup_blocked_reason": (
                    "Official deletion has not been verified to remove every "
                    "duplicate rollout; preserve all copies for manual review."
                ),
                "cleanup_blocker_codes": [INTEGRITY_REVIEW_REQUIRED],
                "manual_review_required": True,
            },
        )

    def _scan_orphaned_subagents(
        self,
        threads: dict[str, dict[str, Any]],
        rollouts: dict[str, list[RolloutRecord]],
        edges: list[dict[str, Any]],
    ) -> tuple[list[Finding], set[tuple[str, str]]]:
        edges_by_child = {
            edge["child_thread_id"]: edge
            for edge in edges
        }
        child_edges_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in edges:
            child_edges_by_parent[edge["parent_thread_id"]].append(edge)
        child_ids = set(threads) | set(rollouts)
        findings: list[Finding] = []
        claimed_edges: set[tuple[str, str]] = set()

        for child_id in child_ids:
            row = threads.get(child_id)
            records = rollouts.get(child_id, [])
            is_subagent, source_parents, evidence = _subagent_evidence(row, records)
            edge = edges_by_child.get(child_id)

            # An explicit session source is stronger than a database edge.  If
            # it is absent, thread_source=subagent plus one edge is sufficient.
            parent_id: str | None = None
            if len(source_parents) == 1:
                parent_id = next(iter(source_parents))
            elif not source_parents and is_subagent and edge is not None:
                parent_id = edge["parent_thread_id"]
                evidence.append("thread_spawn_edges")
            else:
                # Conflicting parent IDs are handled as residual edge evidence
                # below; guessing here could delete a live child.
                continue

            if parent_id == child_id:
                continue

            parent_indexed = parent_id in threads
            parent_rollout_present = _rollout_artifact_present(
                parent_id, threads, rollouts, self.codex_home
            )
            matching_edge = (
                edge is not None and edge["parent_thread_id"] == parent_id
            )

            # A subagent is orphaned only when *both* forms of native parent
            # evidence are gone.  An index-only or rollout-only parent is the
            # root inconsistency; reporting every healthy descendant as a
            # separate orphan duplicates the problem and can block the exact
            # parent cascade that Codex itself owns.
            if parent_indexed or parent_rollout_present:
                continue

            if matching_edge:
                claimed_edges.add((parent_id, child_id))

            child_indexed = child_id in threads
            rollout = _preferred_rollout(records) if records else None
            descendant_edges = child_edges_by_parent.get(child_id, [])
            edge_is_open = (
                matching_edge
                and isinstance(edge.get("status"), str)
                and edge["status"].lower() == "open"
            )
            thread_delete_supported = child_indexed or rollout is not None
            source_consensus = _has_source_consensus_without_edge(
                child_id=child_id,
                parent_id=parent_id,
                row=row,
                records=records,
                edge=edge,
                codex_home=self.codex_home,
            )
            source_consensus_cleanup = (
                source_consensus
                and not parent_indexed
                and not parent_rollout_present
                and not descendant_edges
                and thread_delete_supported
            )
            safely_deletable = (
                thread_delete_supported
                and not parent_indexed
                and not parent_rollout_present
                and not edge_is_open
                and not descendant_edges
                and (matching_edge or source_consensus_cleanup)
            )
            blocked_reasons: list[str] = []
            blocker_codes: list[str] = []
            if parent_indexed or parent_rollout_present:
                blocked_reasons.append("The parent still has a native artifact.")
                blocker_codes.append(IDENTITY_CONFLICT)
            if edge_is_open:
                blocked_reasons.append("The spawn edge is still open.")
                blocker_codes.append(SPAWN_EDGE_OPEN)
            if not matching_edge and not source_consensus:
                blocked_reasons.append(
                    "No matching spawn edge remains to corroborate the source metadata."
                )
                blocker_codes.append(SOURCE_PARENT_UNVERIFIED)
            if descendant_edges:
                blocked_reasons.append(
                    "thread/delete would cascade into spawned descendants."
                )
                blocker_codes.append(CASCADE_REQUIRES_EXPLICIT_SCOPE)
            if not thread_delete_supported:
                blocked_reasons.append(
                    "No native thread or rollout artifact can be passed to thread/delete."
                )
                blocker_codes.append(NO_NATIVE_ARTIFACT)
            findings.append(
                self._finding(
                    thread_id=child_id,
                    reason=(
                        "Codex subagent thread is orphaned because its parent "
                        "thread or parent rollout is missing"
                    ),
                    rollout=rollout,
                    codex_indexed=child_indexed,
                    codex_archived=_thread_archived(row, rollout),
                    details={
                        "finding_type": "orphaned_subagent_thread",
                        "parent_thread_id": parent_id,
                        "parent_indexed": parent_indexed,
                        "parent_rollout_present": parent_rollout_present,
                        "subagent_evidence": sorted(set(evidence)),
                        "spawn_edge_present": matching_edge,
                        "spawn_edge_status": edge.get("status") if matching_edge else None,
                        # Automatic deletion is limited to the unambiguous case
                        # where the parent has no remaining native artifact.
                        "thread_delete_supported": thread_delete_supported,
                        "needs_quarantine": False,
                        "cleanable": safely_deletable,
                        "requires_explicit_selection": (
                            safely_deletable and source_consensus_cleanup
                        ),
                        "evidence_strength": (
                            "spawn_edge"
                            if matching_edge
                            else (
                                "source_consensus"
                                if source_consensus
                                else "source_only"
                            )
                        ),
                        "cleanup_blocked_reason": (
                            None if safely_deletable else " ".join(blocked_reasons)
                        ),
                        "cleanup_blocker_codes": (
                            [] if safely_deletable else sorted(set(blocker_codes))
                        ),
                        "spawn_descendant_edge_count": len(descendant_edges),
                        "manual_review_required": not safely_deletable,
                    },
                )
            )

        return findings, claimed_edges

    def _scan_residual_spawn_edges(
        self,
        threads: dict[str, dict[str, Any]],
        rollouts: dict[str, list[RolloutRecord]],
        edges: list[dict[str, Any]],
        *,
        claimed_edges: set[tuple[str, str]],
    ) -> list[Finding]:
        findings: list[Finding] = []
        for edge in edges:
            parent_id = edge["parent_thread_id"]
            child_id = edge["child_thread_id"]
            edge_key = (parent_id, child_id)
            if edge_key in claimed_edges:
                continue

            child_row = threads.get(child_id)
            child_records = rollouts.get(child_id, [])
            _, source_parents, evidence = _subagent_evidence(
                child_row, child_records
            )
            source_conflict = bool(source_parents and parent_id not in source_parents)
            parent_index_missing = parent_id not in threads
            child_index_missing = child_id not in threads
            parent_rollout_present = _rollout_artifact_present(
                parent_id, threads, rollouts, self.codex_home
            )
            child_rollout_present = _rollout_artifact_present(
                child_id, threads, rollouts, self.codex_home
            )
            parent_artifact_missing = (
                parent_index_missing and not parent_rollout_present
            )
            child_artifact_missing = (
                child_index_missing and not child_rollout_present
            )
            if not (
                source_conflict
                or parent_artifact_missing
                or child_artifact_missing
            ):
                continue

            rollout = (
                _preferred_rollout(child_records) if child_records else None
            )
            findings.append(
                self._finding(
                    platform_session_id=f"{parent_id}->{child_id}",
                    thread_id=child_id,
                    reason=(
                        "Codex spawn edge remains after an endpoint disappeared "
                        "or conflicts with the child's session metadata"
                    ),
                    rollout=rollout,
                    codex_indexed=not child_index_missing,
                    codex_archived=_thread_archived(child_row, rollout),
                    details={
                        "finding_type": "residual_spawn_edge",
                        "parent_thread_id": parent_id,
                        "child_thread_id": child_id,
                        "edge_status": edge.get("status"),
                        "parent_index_missing": parent_index_missing,
                        "child_index_missing": child_index_missing,
                        "parent_rollout_present": parent_rollout_present,
                        "child_rollout_present": child_rollout_present,
                        "parent_artifact_missing": parent_artifact_missing,
                        "child_artifact_missing": child_artifact_missing,
                        "source_parent_ids": sorted(source_parents),
                        "source_conflict": source_conflict,
                        "subagent_evidence": sorted(set(evidence)),
                        # There is no supported thread/delete target for a
                        # dangling relationship itself.  Never mutate the DB.
                        "thread_delete_supported": False,
                        "needs_quarantine": False,
                        "cleanable": False,
                        "cleanup_blocked_reason": (
                            "thread/delete does not expose a standalone spawn-edge "
                            "cleanup operation."
                        ),
                        "cleanup_blocker_codes": [
                            STANDALONE_RELATION_CLEANUP_UNAVAILABLE
                        ],
                        "manual_review_required": True,
                        "direct_database_edit_supported": False,
                        "diagnostic_artifact_present": True,
                    },
                )
            )
        return findings

    def _scan_legacy_session_index(
        self,
        threads: dict[str, dict[str, Any]],
        rollouts: dict[str, list[RolloutRecord]],
    ) -> Finding | None:
        index_path = self.codex_home / "session_index.jsonl"
        if not index_path.is_file():
            return None

        entry_ids: list[str] = []
        malformed_lines = 0
        try:
            with index_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_lines += 1
                        continue
                    if not isinstance(raw, dict):
                        malformed_lines += 1
                        continue
                    thread_id = raw.get("id")
                    if isinstance(thread_id, str) and thread_id:
                        entry_ids.append(thread_id)
        except (OSError, UnicodeError):
            return None

        unique_ids = set(entry_ids)
        residual_ids = sorted(
            thread_id
            for thread_id in unique_ids
            if thread_id not in threads and thread_id not in rollouts
        )
        if not residual_ids:
            return None

        return self._finding(
            platform_session_id=index_path.name,
            # This is an aggregate diagnostic, deliberately not a deletable
            # native thread ID.
            thread_id="legacy-session-index",
            reason=(
                "Legacy session_index.jsonl contains entries with no thread "
                "index row or rollout"
            ),
            rollout=None,
            codex_indexed=False,
            codex_archived=None,
            details={
                "finding_type": "legacy_index_only",
                "legacy_index_path": str(index_path),
                "entry_count": len(entry_ids),
                "unique_thread_count": len(unique_ids),
                "residual_thread_count": len(residual_ids),
                "residual_thread_ids_sample": residual_ids[:25],
                "sample_truncated": len(residual_ids) > 25,
                "duplicate_entry_count": len(entry_ids) - len(unique_ids),
                "malformed_line_count": malformed_lines,
                "thread_delete_supported": False,
                "needs_quarantine": False,
                "cleanable": False,
                "cleanup_blocked_reason": (
                    "Legacy index entries are not thread/delete targets."
                ),
                "cleanup_blocker_codes": [LEGACY_INDEX_NOT_THREAD_TARGET],
                "manual_review_required": True,
                "direct_legacy_index_edit_supported": False,
                # logs_2.sqlite is retention/diagnostic data.  A missing live
                # thread is not sufficient evidence that its log rows should
                # be modified, so this adapter intentionally does not touch it.
                "logs_2_direct_cleanup_supported": False,
                "diagnostic_artifact_present": True,
            },
        )

    def _finding(
        self,
        *,
        thread_id: str,
        reason: str,
        rollout: RolloutRecord | None,
        codex_indexed: bool,
        codex_archived: bool | None,
        details: dict[str, Any],
        platform_session_id: str | None = None,
    ) -> Finding:
        return Finding(
            platform=self.name,
            platform_session_id=platform_session_id or thread_id,
            thread_id=thread_id,
            reason=reason,
            platform_db=self.database,
            codex_home=self.codex_home,
            rollout=rollout,
            codex_indexed=codex_indexed,
            codex_archived=codex_archived,
            codex_bin_hint=self.codex_bin_hint,
            details=details,
        )


def _table_columns(
    connection: sqlite3.Connection, table_name: str
) -> set[str]:
    # table_name is selected only from fixed literals in this module.
    return {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table_name})")
        if isinstance(row["name"], str)
    }


def _scan_all_rollouts(
    codex_home: Path,
) -> dict[str, list[RolloutRecord]]:
    records: dict[str, list[RolloutRecord]] = defaultdict(list)
    for directory_name, archived in (
        ("sessions", False),
        ("archived_sessions", True),
    ):
        root = codex_home / directory_name
        if not root.is_dir():
            continue
        try:
            paths = sorted(root.rglob("*.jsonl"), key=lambda path: str(path))
        except OSError:
            continue
        for path in paths:
            record = _read_rollout_meta(path, archived=archived)
            if record is not None:
                records[record.thread_id].append(record)
    return dict(records)


def _indexed_rollout_path(
    codex_home: Path, row: dict[str, Any]
) -> Path | None:
    raw = row.get("rollout_path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = codex_home / path
    return path


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _path_key(path: Path) -> str:
    return canonical_existing_path_key(path)


def _preferred_rollout(records: list[RolloutRecord]) -> RolloutRecord:
    return sorted(
        records,
        key=lambda record: (
            record.archived,
            canonical_existing_path_key(record.path),
        ),
    )[0]


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _thread_archived(
    row: dict[str, Any] | None, rollout: RolloutRecord | None
) -> bool | None:
    if row is not None:
        indexed = _optional_bool(row.get("archived"))
        if indexed is not None:
            return indexed
    return rollout.archived if rollout is not None else None


def _rollout_artifact_present(
    thread_id: str,
    threads: dict[str, dict[str, Any]],
    rollouts: dict[str, list[RolloutRecord]],
    codex_home: Path,
) -> bool:
    if rollouts.get(thread_id):
        return True
    row = threads.get(thread_id)
    if row is None:
        return False
    path = _indexed_rollout_path(codex_home, row)
    return path is not None and _is_file(path)


def _has_source_consensus_without_edge(
    *,
    child_id: str,
    parent_id: str,
    row: dict[str, Any] | None,
    records: list[RolloutRecord],
    edge: dict[str, Any] | None,
    codex_home: Path,
) -> bool:
    """Return whether two independent sources safely identify one parent.

    This is intentionally stricter than the evidence needed to *report* an
    orphan.  It is the exceptional cleanup gate for historical subagents whose
    spawn edge has already disappeared.
    """

    if edge is not None or row is None or len(records) != 1:
        return False
    if row.get("thread_source") != "subagent":
        return False

    indexed_path = _indexed_rollout_path(codex_home, row)
    rollout = records[0]
    if (
        indexed_path is None
        or not _is_file(indexed_path)
        or _path_key(indexed_path) != _path_key(rollout.path)
        or rollout.thread_id != child_id
    ):
        return False

    indexed_is_subagent, indexed_parents = _parse_subagent_source(
        row.get("source")
    )
    rollout_is_subagent, rollout_parents = _parse_subagent_source(
        rollout.source
    )
    expected = {parent_id}
    return (
        indexed_is_subagent
        and rollout_is_subagent
        and indexed_parents == expected
        and rollout_parents == expected
    )


def _subagent_evidence(
    row: dict[str, Any] | None,
    records: list[RolloutRecord],
) -> tuple[bool, set[str], list[str]]:
    is_subagent = False
    parent_ids: set[str] = set()
    evidence: list[str] = []

    if row is not None:
        thread_source = row.get("thread_source")
        if isinstance(thread_source, str) and thread_source.lower() == "subagent":
            is_subagent = True
            evidence.append("threads.thread_source")
        source_is_subagent, source_parents = _parse_subagent_source(
            row.get("source")
        )
        if source_is_subagent:
            is_subagent = True
            evidence.append("threads.source")
        parent_ids.update(source_parents)

    for record in records:
        source_is_subagent, source_parents = _parse_subagent_source(record.source)
        if source_is_subagent:
            is_subagent = True
            evidence.append("session_meta.source")
        parent_ids.update(source_parents)

    return is_subagent, parent_ids, evidence


def _parse_subagent_source(value: object) -> tuple[bool, set[str]]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() == "subagent":
            return True, set()
        try:
            value = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return False, set()

    if not isinstance(value, dict):
        return False, set()

    subagent_value = value.get("subagent")
    if "subagent" in value:
        parents = _parents_from_spawn_value(subagent_value)
        return True, parents

    if "thread_spawn" in value:
        return True, _parents_from_spawn_value(value)

    return False, set()


def _parents_from_spawn_value(value: object) -> set[str]:
    if not isinstance(value, dict):
        return set()
    spawn = value.get("thread_spawn")
    if not isinstance(spawn, dict):
        return set()
    parent_id = spawn.get("parent_thread_id")
    if isinstance(parent_id, str) and parent_id:
        return {parent_id}
    return set()
