from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from ..codex_state import iter_rollouts
from ..discovery import discover_cindy_codex
from ..models import Finding, RolloutRecord
from ..sqlite_utils import connect_readonly
from .base import (
    AdapterScanError,
    FrontendAdapter,
    optional_database_file_exists,
    read_codex_evidence,
    require_table_columns,
    table_columns,
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
        self.codex_bin_hint = codex_bin_hint or discover_cindy_codex(root)

    def list_sessions(self) -> list["FrontendSessionRecord"]:
        """Read all Cindy Codex rows, including unassigned sessions."""

        from ..inventory import FrontendSessionRecord

        if not optional_database_file_exists(self.database):
            return []
        try:
            with closing(connect_readonly(self.database)) as connection:
                require_table_columns(
                    connection,
                    table_name="sessions",
                    required_columns={
                        "id",
                        "sdk_session_id",
                        "status",
                        "agent_kind",
                    },
                    database=self.database,
                )
                columns = table_columns(
                    connection,
                    table_name="sessions",
                    database=self.database,
                )
                optional = (
                    "source",
                    "created_at",
                    "updated_at",
                    "parent_session_id",
                    "title",
                    "working_dir",
                )
                projections = [
                    column if column in columns else f"NULL AS {column}"
                    for column in optional
                ]
                rows = connection.execute(
                    f"""
                    SELECT id, sdk_session_id, status, agent_kind,
                           {", ".join(projections)}
                    FROM sessions
                    WHERE LOWER(TRIM(agent_kind)) = 'codex'
                    ORDER BY id
                    """
                ).fetchall()
        except AdapterScanError:
            raise
        except sqlite3.Error as exc:
            raise AdapterScanError(
                f"Could not inspect Cindy database {self.database}: {exc}"
            ) from exc

        if any(not isinstance(row["id"], str) or not row["id"].strip() for row in rows):
            raise AdapterScanError(
                f"{self.database} is incompatible: sessions.id contains an invalid value"
            )
        return [
            FrontendSessionRecord(
                platform=self.name,
                platform_session_id=row["id"],
                thread_id=_display_thread_id(row["sdk_session_id"]),
                database=self.database,
                codex_home=self.codex_home,
                backend="codex",
                status=_display_string(row["status"]),
                updated_at_ms=_as_int(row["updated_at"]),
                title=_display_string(row["title"]),
                is_live=_normalized_string(row["status"]) != "deleted",
                details={
                    "agent_kind": row["agent_kind"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                    "parent_session_id": row["parent_session_id"],
                    "working_dir": row["working_dir"],
                },
                codex_bin_hint=self.codex_bin_hint,
            )
            for row in rows
        ]

    def scan(self) -> list[Finding]:
        self._replace_live_thread_ids(set())
        if not self.available:
            return []
        try:
            with closing(connect_readonly(self.database)) as connection:
                require_table_columns(
                    connection,
                    table_name="sessions",
                    required_columns={
                        "id",
                        "sdk_session_id",
                        "status",
                        "source",
                        "created_at",
                        "updated_at",
                        "parent_session_id",
                        "agent_kind",
                    },
                    database=self.database,
                )
                rows = connection.execute(
                    """
                    SELECT
                        id,
                        sdk_session_id,
                        status,
                        source,
                        created_at,
                        updated_at,
                        parent_session_id,
                        (
                            SELECT COUNT(*)
                            FROM sessions live
                            WHERE live.sdk_session_id = deleted.sdk_session_id
                              AND (
                                  live.status IS NULL
                                  OR live.status <> 'deleted'
                              )
                        ) AS live_reference_count
                    FROM sessions deleted
                    WHERE deleted.agent_kind = 'codex'
                      AND deleted.status = 'deleted'
                      AND deleted.sdk_session_id IS NOT NULL
                      AND typeof(deleted.sdk_session_id) = 'text'
                      AND TRIM(deleted.sdk_session_id) <> ''
                    """
                ).fetchall()
                live_rows = connection.execute(
                    """
                    SELECT DISTINCT sdk_session_id
                    FROM sessions
                    WHERE agent_kind = 'codex'
                      AND sdk_session_id IS NOT NULL
                      AND typeof(sdk_session_id) = 'text'
                      AND TRIM(sdk_session_id) <> ''
                      AND (
                          status IS NULL
                          OR status <> 'deleted'
                      )
                    """
                ).fetchall()
        except AdapterScanError:
            raise
        except sqlite3.Error as exc:
            raise AdapterScanError(
                f"Could not inspect Cindy database {self.database}: {exc}"
            ) from exc
        self._replace_live_thread_ids(
            {
                row["sdk_session_id"]
                for row in live_rows
                if isinstance(row["sdk_session_id"], str)
            }
        )
        if not rows:
            return []

        thread_ids = [row["sdk_session_id"] for row in rows]
        try:
            rollout_groups = _rollouts_by_thread(self.codex_home, set(thread_ids))
        except OSError as exc:
            raise AdapterScanError(
                f"Could not inspect Codex rollouts in {self.codex_home}: {exc}"
            ) from exc
        evidence = read_codex_evidence(self.codex_home, thread_ids)

        findings: list[Finding] = []
        for row in rows:
            thread_id = row["sdk_session_id"]
            records = rollout_groups.get(thread_id, [])
            rollout = _preferred_rollout(records)
            state_row = evidence.indexed_threads.get(thread_id)
            if rollout is None and state_row is None:
                # Cindy retains a soft-delete row, but there is no longer
                # anything for Codex thread/delete to repair.
                continue

            originators = {
                normalized
                for record in records
                if (normalized := _normalized_string(record.originator)) is not None
            }
            foreign_originators = originators - {"cindy", "xdt-maker"}
            ownership_conflict = bool(foreign_originators)
            live_reference_count = int(row["live_reference_count"] or 0)
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
            cleanable = (
                not ownership_conflict
                and live_reference_count == 0
                and cascade_safe
            )
            findings.append(
                Finding(
                    platform=self.name,
                    platform_session_id=row["id"],
                    thread_id=thread_id,
                    reason="Cindy session is soft-deleted but its Codex thread remains",
                    platform_db=self.database,
                    codex_home=self.codex_home,
                    platform_updated_at_ms=_as_int(row["updated_at"]),
                    rollout=rollout,
                    codex_indexed=state_row is not None,
                    codex_archived=bool(state_row["archived"]) if state_row else None,
                    codex_bin_hint=self.codex_bin_hint,
                    details={
                        "session_status": row["status"],
                        "source": row["source"],
                        "parent_session_id": row["parent_session_id"],
                        "rollout_originator": rollout.originator if rollout else None,
                        "rollout_originators": sorted(originators),
                        "ownership_status": (
                            "conflict" if ownership_conflict else "confirmed"
                        ),
                        "ownership_evidence": {
                            "agent_kind": "codex",
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


def _display_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _display_thread_id(value: object) -> str | None:
    return _display_string(value)
