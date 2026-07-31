from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

from ..codex_state import iter_rollouts
from ..discovery import discover_aionui_codex
from ..models import Finding, RolloutRecord
from ..sqlite_utils import connect_readonly, table_exists
from .base import (
    AdapterScanError,
    FrontendAdapter,
    read_codex_evidence,
    require_table_columns,
)


class AionUIAdapter(FrontendAdapter):
    name = "aionui"

    def __init__(
        self,
        *,
        database: Path,
        codex_home: Path,
        codex_bin_hint: Path | None = None,
    ) -> None:
        super().__init__(database=database, codex_home=codex_home)
        self.codex_bin_hint = codex_bin_hint or discover_aionui_codex()

    def scan(self) -> list[Finding]:
        self._replace_live_thread_ids(set())
        if not self.available:
            return []
        try:
            with closing(connect_readonly(self.database)) as connection:
                require_table_columns(
                    connection,
                    table_name="acp_session",
                    required_columns={
                        "conversation_id",
                        "session_id",
                        "agent_id",
                        "agent_source",
                        "session_status",
                        "last_active_at",
                    },
                    database=self.database,
                )
                require_table_columns(
                    connection,
                    table_name="conversations",
                    required_columns={"id"},
                    database=self.database,
                )
                has_metadata = table_exists(connection, "agent_metadata")
                if has_metadata:
                    require_table_columns(
                        connection,
                        table_name="agent_metadata",
                        required_columns={"agent_id", "backend"},
                        database=self.database,
                    )
                metadata_join = (
                    "LEFT JOIN agent_metadata m ON m.agent_id = a.agent_id"
                    if has_metadata
                    else ""
                )
                backend_column = "m.backend" if has_metadata else "NULL"
                rows = connection.execute(
                    f"""
                    SELECT
                        a.conversation_id,
                        a.session_id,
                        a.agent_id,
                        a.agent_source,
                        a.session_status,
                        a.last_active_at,
                        {backend_column} AS backend,
                        (
                            SELECT COUNT(*)
                            FROM acp_session live
                            JOIN conversations live_conversation
                              ON live_conversation.id = live.conversation_id
                            WHERE live.session_id = a.session_id
                        ) AS live_reference_count
                    FROM acp_session a
                    LEFT JOIN conversations c ON c.id = a.conversation_id
                    {metadata_join}
                    WHERE c.id IS NULL
                      AND a.session_id IS NOT NULL
                      AND typeof(a.session_id) = 'text'
                      AND TRIM(a.session_id) <> ''
                    """
                ).fetchall()
                live_rows = connection.execute(
                    f"""
                    SELECT DISTINCT
                        a.session_id,
                        {backend_column} AS backend
                    FROM acp_session a
                    JOIN conversations c ON c.id = a.conversation_id
                    {metadata_join}
                    WHERE a.session_id IS NOT NULL
                      AND typeof(a.session_id) = 'text'
                      AND TRIM(a.session_id) <> ''
                    """
                ).fetchall()
        except AdapterScanError:
            raise
        except sqlite3.Error as exc:
            # Opening can succeed for a corrupt file and fail on the first
            # schema query, so this must remain visible in ScanReport.errors.
            raise AdapterScanError(
                f"Could not inspect AionUI database {self.database}: {exc}"
            ) from exc
        if not rows and not live_rows:
            return []

        thread_ids = [row["session_id"] for row in rows]
        rollout_scan_ids = set(thread_ids)
        rollout_scan_ids.update(row["session_id"] for row in live_rows)
        try:
            rollout_groups = _rollouts_by_thread(self.codex_home, rollout_scan_ids)
        except OSError as exc:
            raise AdapterScanError(
                f"Could not inspect Codex rollouts in {self.codex_home}: {exc}"
            ) from exc

        live_thread_ids: set[str] = set()
        for row in live_rows:
            thread_id = row["session_id"]
            backend = _normalized_string(row["backend"])
            records = rollout_groups.get(thread_id, [])
            originators = {
                normalized
                for record in records
                if (normalized := _normalized_string(record.originator)) is not None
            }
            if backend == "codex" or (
                backend is None
                and "aionui-session" in originators
                and not (originators - {"aionui-session"})
            ):
                live_thread_ids.add(thread_id)
        self._replace_live_thread_ids(live_thread_ids)

        if not rows:
            return []
        evidence = read_codex_evidence(self.codex_home, thread_ids)

        findings: list[Finding] = []
        for row in rows:
            thread_id = row["session_id"]
            records = rollout_groups.get(thread_id, [])
            rollout = _preferred_rollout(records)
            state_row = evidence.indexed_threads.get(thread_id)
            if rollout is None and state_row is None:
                # The stale frontend mapping has already been resolved on the
                # Codex side. Re-reporting it would create a permanent false
                # cleanup candidate.
                continue

            backend = _normalized_string(row["backend"])
            originators = {
                normalized
                for record in records
                if (normalized := _normalized_string(record.originator)) is not None
            }
            ownership, ownership_reason = _aionui_ownership(
                backend=backend,
                originators=originators,
            )
            if ownership == "not_codex":
                continue

            live_reference_count = int(row["live_reference_count"] or 0)
            descendants = evidence.descendants_by_parent.get(thread_id, ())
            cascade_safe = evidence.spawn_edges_available and not descendants
            blocked_reasons = [
                reason
                for reason in (
                    ownership_reason,
                    (
                        "The same Codex thread is still referenced by a live "
                        "AionUI conversation."
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
            cleanable = (
                ownership == "confirmed"
                and live_reference_count == 0
                and cascade_safe
            )
            findings.append(
                Finding(
                    platform=self.name,
                    platform_session_id=row["conversation_id"],
                    thread_id=thread_id,
                    reason="AionUI conversation row is gone but its ACP mapping remains",
                    platform_db=self.database,
                    codex_home=self.codex_home,
                    platform_updated_at_ms=_as_int(row["last_active_at"]),
                    rollout=rollout,
                    codex_indexed=state_row is not None,
                    codex_archived=bool(state_row["archived"]) if state_row else None,
                    codex_bin_hint=self.codex_bin_hint,
                    details={
                        "agent_source": row["agent_source"],
                        "session_status": row["session_status"],
                        "backend": row["backend"],
                        "rollout_originator": (
                            rollout.originator if rollout is not None else None
                        ),
                        "rollout_originators": sorted(originators),
                        "ownership_status": ownership,
                        "ownership_evidence": _ownership_evidence(
                            backend=backend,
                            originators=originators,
                        ),
                        "originator_conflict": ownership == "conflict",
                        "live_reference_count": live_reference_count,
                        "cleanable": cleanable,
                        "thread_delete_supported": True,
                        "needs_quarantine": (
                            ownership != "confirmed"
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
                    },
                )
            )
        return findings


def _as_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


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


def _aionui_ownership(
    *,
    backend: str | None,
    originators: set[str],
) -> tuple[str, str | None]:
    expected = "aionui-session"
    foreign_originators = originators - {expected}

    if backend == "codex":
        if foreign_originators:
            return (
                "conflict",
                "AionUI identifies the backend as Codex, but the rollout has "
                "a foreign originator.",
            )
        return "confirmed", None

    if backend is None:
        if foreign_originators:
            return (
                "conflict",
                "AionUI backend ownership is unknown and the rollout has a "
                "foreign originator.",
            )
        if expected in originators:
            return "confirmed", None
        return (
            "insufficient",
            "AionUI backend ownership is unknown and no AionUI rollout "
            "originator confirms this thread.",
        )

    if expected in originators:
        return (
            "conflict",
            "AionUI backend metadata and Codex rollout ownership evidence "
            "conflict.",
        )
    return "not_codex", None


def _ownership_evidence(
    *,
    backend: str | None,
    originators: set[str],
) -> dict[str, Any]:
    return {
        "backend": backend,
        "expected_originator": "aionui-session",
        "observed_originators": sorted(originators),
    }
