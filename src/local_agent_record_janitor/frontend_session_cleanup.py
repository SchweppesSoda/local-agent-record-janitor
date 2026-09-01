"""Verified hard deletion for terminal Cindy frontend task rows."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sqlite_identity import row_fingerprint, schema_fingerprint, table_schema
from .sqlite_utils import connect_readonly


# Cindy uses ``archived`` for a still-retained task. Only the UI tombstone
# state is authorized for physical frontend-session deletion.
_TERMINAL = frozenset({"deleted"})


class FrontendSessionCleanupError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        outcome_known_rolled_back: bool = False,
        outcome_unknown: bool = False,
        mutation_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.outcome_known_rolled_back = outcome_known_rolled_back
        self.outcome_unknown = outcome_unknown
        self.mutation_started = mutation_started


class FrontendSessionGuardError(FrontendSessionCleanupError):
    pass


@dataclass(frozen=True)
class CindySessionDeleteEvidence:
    database: Path
    session_id: str
    expected_status: str
    session_schema_fingerprint: str
    session_row_fingerprint: str
    stable_session_row_fingerprint: str
    expected_sdk_session_id: str | None
    schema_bundle_fingerprint: str
    expected_message_count: int
    message_id_fingerprint: str
    expected_fts_count: int = 0
    expected_embedding_count: int = 0
    expected_vector_count: int = 0
    expected_media_ref_count: int = 0
    expected_skill_source_count: int = 0
    expected_skill_exposure_count: int = 0
    expected_ghost_card_count: int = 0
    table: str = "sessions"

    def __post_init__(self) -> None:
        database = Path(self.database).expanduser().absolute()
        session_id = str(self.session_id).strip()
        status = str(self.expected_status).strip().casefold()
        if not session_id:
            raise ValueError("session_id must not be blank")
        if status not in _TERMINAL:
            raise ValueError("Cindy hard deletion requires status=deleted")
        if self.table != "sessions":
            raise ValueError("only Cindy sessions rows are supported")
        for name in (
            "session_schema_fingerprint",
            "session_row_fingerprint",
            "stable_session_row_fingerprint",
            "schema_bundle_fingerprint",
            "message_id_fingerprint",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        for name in (
            "expected_message_count",
            "expected_fts_count",
            "expected_embedding_count",
            "expected_vector_count",
            "expected_media_ref_count",
            "expected_skill_source_count",
            "expected_skill_exposure_count",
            "expected_ghost_card_count",
        ):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must not be negative")
        object.__setattr__(self, "database", database)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "expected_status", status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "platform": "cindy",
            "operation": "delete_terminal_frontend_session",
            "database": str(self.database),
            "table": self.table,
            "session_id": self.session_id,
            "expected_status": self.expected_status,
            "session_schema_fingerprint": self.session_schema_fingerprint,
            "session_row_fingerprint": self.session_row_fingerprint,
            "stable_session_row_fingerprint": self.stable_session_row_fingerprint,
            "expected_sdk_session_id": self.expected_sdk_session_id,
            "schema_bundle_fingerprint": self.schema_bundle_fingerprint,
            "expected_message_count": self.expected_message_count,
            "message_id_fingerprint": self.message_id_fingerprint,
            "expected_fts_count": self.expected_fts_count,
            "expected_embedding_count": self.expected_embedding_count,
            "expected_vector_count": self.expected_vector_count,
            "expected_media_ref_count": self.expected_media_ref_count,
            "expected_skill_source_count": self.expected_skill_source_count,
            "expected_skill_exposure_count": self.expected_skill_exposure_count,
            "expected_ghost_card_count": self.expected_ghost_card_count,
            "exact": True,
        }


@dataclass(frozen=True)
class FrontendSessionCleanupResult:
    database: Path
    deleted_session_count: int
    deleted_message_count: int
    deleted_fts_count: int
    deleted_embedding_count: int
    deleted_vector_count: int
    deleted_media_ref_count: int
    deleted_skill_row_count: int
    deleted_ids: tuple[str, ...]
    verification_remaining_ids: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return "deleted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "database": str(self.database),
            "deleted_session_count": self.deleted_session_count,
            "deleted_message_count": self.deleted_message_count,
            "deleted_fts_count": self.deleted_fts_count,
            "deleted_embedding_count": self.deleted_embedding_count,
            "deleted_vector_count": self.deleted_vector_count,
            "deleted_media_ref_count": self.deleted_media_ref_count,
            "deleted_skill_row_count": self.deleted_skill_row_count,
            "deleted_ids": list(self.deleted_ids),
            "transaction": {"committed": True, "rollback_performed": False},
            "verification": {
                "remaining_ids": list(self.verification_remaining_ids),
                "verified": not self.verification_remaining_ids,
            },
        }


def build_cindy_session_delete_evidence(
    records: Sequence[Mapping[str, Any]],
    *,
    vector_extension: Path | None = None,
) -> tuple[CindySessionDeleteEvidence, ...]:
    seeds = _normalize_seeds(records)
    database = _one_database_from_seeds(seeds)
    ids = tuple(seed["session_id"] for seed in seeds)
    by_id = {seed["session_id"]: seed for seed in seeds}
    with closing(_connect(database, readonly=True, vector_extension=vector_extension)) as db:
        current = _snapshot(db, ids)
    for item in current:
        seed = by_id[item.session_id]
        if item.expected_status != seed["expected_status"]:
            raise FrontendSessionGuardError("Cindy session status changed before planning")
        if seed.get("session_schema_fingerprint") not in (
            None,
            "",
            item.session_schema_fingerprint,
        ):
            raise FrontendSessionGuardError("Cindy sessions schema changed")
        if seed.get("session_row_fingerprint") not in (
            None,
            "",
            item.session_row_fingerprint,
        ):
            raise FrontendSessionGuardError("Cindy session row changed")
    return current


def guard_cindy_session_rows(
    evidence_items: Sequence[CindySessionDeleteEvidence | Mapping[str, Any]],
    *,
    vector_extension: Path | None = None,
) -> tuple[CindySessionDeleteEvidence, ...]:
    evidence = _normalize_evidence(evidence_items)
    database = _one_database(evidence)
    ids = tuple(item.session_id for item in evidence)
    with closing(_connect(database, readonly=True, vector_extension=vector_extension)) as db:
        current = _snapshot(db, ids)
        _assert_same(evidence, current)
        _guard_non_cascade_references(db, ids)
    return current


def verify_cindy_session_rows(
    evidence_items: Sequence[CindySessionDeleteEvidence | Mapping[str, Any]],
) -> tuple[str, ...]:
    evidence = _normalize_evidence(evidence_items)
    database = _one_database(evidence)
    ids = tuple(item.session_id for item in evidence)
    with closing(connect_readonly(database)) as db:
        return tuple(sorted(_selected_ids(db, "sessions", "id", ids)))


def execute_cindy_session_cleanup(
    evidence_items: Sequence[CindySessionDeleteEvidence | Mapping[str, Any]],
    *,
    vector_extension: Path | None = None,
    owner_client: str = "cindy",
    owner_process_root: Path | None = None,
    client_inspector: Callable[..., Sequence[str]] | None = None,
    phase_callback: Callable[[str], None] | None = None,
) -> FrontendSessionCleanupResult:
    evidence = _normalize_evidence(evidence_items)
    database = _one_database(evidence)
    ids = tuple(item.session_id for item in evidence)
    process_root = (
        Path(owner_process_root).expanduser().absolute()
        if owner_process_root is not None
        else None
    )
    owner_identity = str(owner_client).strip().casefold()
    if owner_identity != "cindy":
        raise FrontendSessionGuardError(
            "Cindy session deletion requires owner_client=cindy"
        )
    guard_cindy_session_rows(evidence, vector_extension=vector_extension)
    # Take one operation/client process snapshot after the complete read-only
    # evidence guard and before creating a rollback copy or opening a write
    # transaction. The batch writer never re-enumerates per selected row.
    _require_client_closed(
        process_root,
        client_inspector,
        owner_client=owner_identity,
    )
    backup_dir, backup = _backup(database)
    db: sqlite3.Connection | None = None
    started = attempted = committed = False
    messages: tuple[str, ...] = ()
    vector_rows: tuple[int, ...] = ()
    counts = dict(sessions=0, messages=0, fts=0, embeddings=0, vectors=0, media=0, skills=0)
    try:
        db = _connect(database, readonly=False, vector_extension=vector_extension)
        db.execute("BEGIN IMMEDIATE")
        _assert_same(evidence, _snapshot(db, ids))
        _guard_non_cascade_references(db, ids)
        _temp_targets(db, ids)
        messages = tuple(
            str(row[0])
            for row in db.execute("SELECT id FROM temp.larj_cindy_messages ORDER BY id")
        )
        vector_rows = tuple(
            int(row[0])
            for row in db.execute(
                """SELECT job.rowid FROM embedding_jobs job
                   JOIN temp.larj_cindy_messages message ON message.id = job.source_id
                   ORDER BY job.rowid"""
            )
        ) if _has(db, "embedding_jobs") else ()
        if phase_callback is not None:
            phase_callback("mutation_started")
        started = True
        counts["fts"] += _delete(
            db, "messages_fts",
            "session_id IN (SELECT id FROM temp.larj_cindy_targets)",
        )
        counts["fts"] += _delete(
            db, "messages_fts_rows",
            "message_id IN (SELECT id FROM temp.larj_cindy_messages)",
        )
        counts["vectors"] += _delete(
            db, "chat_messages_vec_v1",
            """rowid IN (
                 SELECT job.rowid FROM embedding_jobs job
                 JOIN temp.larj_cindy_messages message ON message.id = job.source_id
               )""",
        )
        counts["embeddings"] += _delete(
            db, "embedding_jobs",
            "source_id IN (SELECT id FROM temp.larj_cindy_messages)",
        )
        counts["media"] += _delete(
            db, "media_refs",
            "origin_session_id IN (SELECT id FROM temp.larj_cindy_targets)",
        )
        counts["skills"] += _delete(
            db, "ghost_cards",
            "session_id IN (SELECT id FROM temp.larj_cindy_targets)",
        )
        counts["skills"] += _delete(
            db, "skill_usage_exposures",
            "session_id IN (SELECT id FROM temp.larj_cindy_targets)",
        )
        counts["skills"] += _delete(
            db, "skill_usage_sources",
            "session_id IN (SELECT id FROM temp.larj_cindy_targets)",
        )
        counts["messages"] += _delete(
            db, "messages",
            "session_id IN (SELECT id FROM temp.larj_cindy_targets)",
        )
        counts["sessions"] = _delete(
            db, "sessions",
            "id IN (SELECT id FROM temp.larj_cindy_targets)",
        )
        if counts["sessions"] != len(ids):
            raise FrontendSessionGuardError(
                "Cindy session deletion affected an unexpected row count",
                mutation_started=True,
            )
        attempted = True
        db.commit()
        committed = True
    except Exception as exc:
        rolled_back = False
        if db is not None and not committed:
            try:
                db.rollback()
                rolled_back = True
            except sqlite3.Error:
                pass
        if db is not None:
            db.close()
            db = None
        if not started:
            _discard(backup, backup_dir)
            if isinstance(exc, FrontendSessionCleanupError):
                raise
            raise FrontendSessionCleanupError(str(exc) or repr(exc)) from exc
        if attempted or committed:
            raise FrontendSessionCleanupError(
                f"Cindy session deletion outcome is unknown: {exc}; backup retained at {backup}",
                outcome_unknown=True,
                mutation_started=True,
            ) from exc
        if rolled_back:
            try:
                guard_cindy_session_rows(evidence, vector_extension=vector_extension)
            except Exception:
                rolled_back = False
        if rolled_back:
            _discard(backup, backup_dir)
            raise FrontendSessionCleanupError(
                f"Cindy session deletion rolled back and verified: {exc}",
                outcome_known_rolled_back=True,
                mutation_started=True,
            ) from exc
        raise FrontendSessionCleanupError(
            f"Cindy session deletion outcome is unknown: {exc}; backup retained at {backup}",
            outcome_unknown=True,
            mutation_started=True,
        ) from exc
    finally:
        if db is not None:
            db.close()

    remaining = verify_cindy_session_rows(evidence)
    if remaining:
        raise FrontendSessionCleanupError(
            f"Cindy session rows remain; backup retained at {backup}",
            outcome_unknown=True,
            mutation_started=True,
        )
    with closing(_connect(database, readonly=True, vector_extension=vector_extension)) as check:
        _verify_absent(check, ids, messages, vector_rows)
    if phase_callback is not None:
        try:
            phase_callback("verified")
        except Exception as exc:
            raise FrontendSessionCleanupError(
                f"Cindy deletion verified but journal failed: {exc}; backup retained at {backup}",
                outcome_unknown=True,
                mutation_started=True,
            ) from exc
    _discard(backup, backup_dir)
    return FrontendSessionCleanupResult(
        database=database,
        deleted_session_count=counts["sessions"],
        deleted_message_count=counts["messages"],
        deleted_fts_count=counts["fts"],
        deleted_embedding_count=counts["embeddings"],
        deleted_vector_count=counts["vectors"],
        deleted_media_ref_count=counts["media"],
        deleted_skill_row_count=counts["skills"],
        deleted_ids=ids,
        verification_remaining_ids=remaining,
    )


def _require_client_closed(
    owner_process_root: Path | None,
    client_inspector: Callable[..., Sequence[str]] | None,
    *,
    owner_client: str,
) -> None:
    if client_inspector is None:
        return
    if owner_process_root is None:
        raise FrontendSessionGuardError(
            "Cindy session deletion requires an explicit owner_process_root"
        )
    running = tuple(
        _inspect_owner_process(
            client_inspector,
            owner_process_root,
            owner_client=owner_client,
        )
    )
    if running:
        raise FrontendSessionGuardError(
            "Close the owning Cindy client before deleting frontend sessions: "
            + ", ".join(running)
        )


def _inspect_owner_process(
    client_inspector: Callable[..., Sequence[str]],
    owner_process_root: Path,
    *,
    owner_client: str,
) -> Sequence[str]:
    """Pass both frozen owner identities to inspectors that support them.

    The production inspector is ``running_related_clients`` and accepts the
    keyword. One-argument test/integration inspectors remain usable as a
    narrow compatibility seam; they still receive the exact frozen data root
    and cannot trigger any path-based inference in this writer.
    """

    try:
        parameters = inspect.signature(client_inspector).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    accepts_owner_client = any(
        parameter.name == "owner_client"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if accepts_owner_client:
        return client_inspector(
            owner_process_root,
            owner_client=owner_client,
        )
    return client_inspector(owner_process_root)

def _snapshot(
    db: sqlite3.Connection,
    ids: Sequence[str],
) -> tuple[CindySessionDeleteEvidence, ...]:
    schema = table_schema(db, "sessions")
    columns = tuple(str(item["name"]) for item in schema)
    if not {"id", "status", "agent_kind", "sdk_session_id"}.issubset(columns):
        raise FrontendSessionGuardError("Unsupported Cindy sessions schema")
    rows = _rows(db, "sessions", "id", ids)
    if len(rows) != len(ids):
        raise FrontendSessionGuardError("Approved Cindy session rows are missing")
    database = Path(str(db.execute("PRAGMA database_list").fetchone()[2]))
    schema_hash = schema_fingerprint(schema)
    bundle_hash = _schema_hash(db)
    if not _has(db, "messages"):
        raise FrontendSessionGuardError("Unsupported Cindy schema: messages is missing")
    marks = ",".join("?" for _ in ids)
    message_rows = tuple(
        db.execute(
            f"SELECT id, session_id FROM messages WHERE session_id IN ({marks}) "
            "ORDER BY session_id, id",
            tuple(ids),
        )
    )
    messages_by_session: dict[str, list[str]] = {value: [] for value in ids}
    for message in message_rows:
        messages_by_session.setdefault(str(message["session_id"]), []).append(
            str(message["id"])
        )
    fts_counts = _counts_by_column(db, "messages_fts", "session_id", ids)
    media_counts = _counts_by_column(
        db, "media_refs", "origin_session_id", ids
    )
    skill_source_counts = _counts_by_column(
        db, "skill_usage_sources", "session_id", ids
    )
    skill_exposure_counts = _counts_by_column(
        db, "skill_usage_exposures", "session_id", ids
    )
    ghost_counts = _counts_by_column(db, "ghost_cards", "session_id", ids)
    embedding_counts = _counts_from_sql(
        db,
        """SELECT message.session_id, COUNT(*)
           FROM embedding_jobs job
           JOIN messages message ON message.id = job.source_id
           WHERE message.session_id IN ({marks})
           GROUP BY message.session_id""",
        ids,
        required_tables=("embedding_jobs",),
    )
    if _has(db, "embedding_jobs"):
        unsupported_vector_tables = tuple(
            str(row[0])
            for row in db.execute(
                (
                    "SELECT DISTINCT job.vec_table "
                    "FROM embedding_jobs job "
                    "JOIN messages message ON message.id = job.source_id "
                    "WHERE message.session_id IN ("
                    + marks
                    + ") AND job.vec_table IS NOT NULL "
                    "AND job.vec_table <> '' "
                    "AND job.vec_table <> 'chat_messages_vec_v1'"
                ),
                tuple(ids),
            )
        )
        if unsupported_vector_tables:
            raise FrontendSessionGuardError(
                "Unsupported Cindy vector table(s): "
                + ", ".join(sorted(unsupported_vector_tables))
            )
    vector_counts = _counts_from_sql(
        db,
        """SELECT message.session_id, COUNT(*)
           FROM chat_messages_vec_v1 vector
           JOIN embedding_jobs job ON job.rowid = vector.rowid
           JOIN messages message ON message.id = job.source_id
           WHERE message.session_id IN ({marks})
           GROUP BY message.session_id""",
        ids,
        required_tables=("chat_messages_vec_v1", "embedding_jobs"),
    )
    result: list[CindySessionDeleteEvidence] = []
    stable_columns = tuple(
        column for column in columns if column != "sdk_session_id"
    )
    for row in rows:
        session_id = str(row["id"])
        status = str(row["status"] or "").casefold()
        if status not in _TERMINAL:
            raise FrontendSessionGuardError(
                f"Cindy session {session_id} is no longer terminal"
            )
        message_ids = tuple(messages_by_session.get(session_id, ()))
        result.append(
            CindySessionDeleteEvidence(
                database=database,
                session_id=session_id,
                expected_status=status,
                session_schema_fingerprint=schema_hash,
                session_row_fingerprint=row_fingerprint(row, columns),
                stable_session_row_fingerprint=row_fingerprint(
                    row, stable_columns
                ),
                expected_sdk_session_id=(
                    None
                    if row["sdk_session_id"] is None
                    else str(row["sdk_session_id"])
                ),
                schema_bundle_fingerprint=bundle_hash,
                expected_message_count=len(message_ids),
                message_id_fingerprint=_ids_hash(message_ids),
                expected_fts_count=fts_counts.get(session_id, 0),
                expected_embedding_count=embedding_counts.get(session_id, 0),
                expected_vector_count=vector_counts.get(session_id, 0),
                expected_media_ref_count=media_counts.get(session_id, 0),
                expected_skill_source_count=skill_source_counts.get(session_id, 0),
                expected_skill_exposure_count=skill_exposure_counts.get(
                    session_id, 0
                ),
                expected_ghost_card_count=ghost_counts.get(session_id, 0),
            )
        )
    return tuple(sorted(result, key=lambda item: item.session_id))


def _guard_non_cascade_references(
    db: sqlite3.Connection,
    ids: Sequence[str],
) -> None:
    tables = tuple(
        (str(row["name"]), str(row["sql"] or ""))
        for row in db.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type = 'table' AND sql IS NOT NULL"
        )
    )
    marks = ",".join("?" for _ in ids)
    for table, sql in tables:
        if sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
            continue
        quoted_table = '"' + table.replace('"', '""') + '"'
        for fk in db.execute(f"PRAGMA foreign_key_list({quoted_table})"):
            if str(fk["table"]).casefold() != "sessions":
                continue
            if str(fk["on_delete"]).upper() in {"CASCADE", "SET NULL"}:
                continue
            column = str(fk["from"])
            quoted_column = '"' + column.replace('"', '""') + '"'
            query = f"SELECT COUNT(*) FROM {quoted_table} WHERE {quoted_column} IN ({marks})"
            params: tuple[Any, ...] = tuple(ids)
            if table == "sessions" and column == "parent_session_id":
                query += f" AND id NOT IN ({marks})"
                params = (*ids, *ids)
            count = int(db.execute(query, params).fetchone()[0])
            if count:
                raise FrontendSessionGuardError(
                    f"Deletion would modify {count} non-cascade reference(s) in {table}.{column}"
                )


def _counts_by_column(
    db: sqlite3.Connection,
    table: str,
    column: str,
    ids: Sequence[str],
) -> dict[str, int]:
    if not ids or not _has(db, table):
        return {}
    marks = ",".join("?" for _ in ids)
    quoted_table = '"' + table.replace('"', '""') + '"'
    quoted_column = '"' + column.replace('"', '""') + '"'
    return {
        str(row[0]): int(row[1])
        for row in db.execute(
            f"SELECT {quoted_column}, COUNT(*) FROM {quoted_table} "
            f"WHERE {quoted_column} IN ({marks}) GROUP BY {quoted_column}",
            tuple(ids),
        )
    }


def _counts_from_sql(
    db: sqlite3.Connection,
    sql: str,
    ids: Sequence[str],
    *,
    required_tables: Sequence[str],
) -> dict[str, int]:
    if not ids or any(not _has(db, table) for table in required_tables):
        return {}
    marks = ",".join("?" for _ in ids)
    return {
        str(row[0]): int(row[1])
        for row in db.execute(sql.format(marks=marks), tuple(ids))
    }


def _verify_absent(
    db: sqlite3.Connection,
    ids: Sequence[str],
    messages: Sequence[str],
    vector_rows: Sequence[int],
) -> None:
    residual: dict[str, int] = {}
    checks = (
        ("sessions", "id", ids, None),
        ("messages", "session_id", ids, None),
        ("messages_fts", "session_id", ids, None),
        ("skill_usage_sources", "session_id", ids, None),
        ("skill_usage_exposures", "session_id", ids, None),
        ("ghost_cards", "session_id", ids, None),
        ("media_refs", "origin_session_id", ids, "session"),
        ("messages_fts_rows", "message_id", messages, None),
        ("embedding_jobs", "source_id", messages, "message"),
        ("chat_messages_vec_v1", "rowid", vector_rows, None),
    )
    for table, column, values, qualifier in checks:
        if not values or not _has(db, table):
            continue
        count = _count_values(db, table, column, values)
        if count:
            residual[f"{table}:{qualifier or column}"] = count
    if residual:
        raise FrontendSessionCleanupError(
            "Cindy dependency residuals: " + json.dumps(residual, sort_keys=True),
            outcome_unknown=True,
            mutation_started=True,
        )


def _normalize_seeds(records: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in records:
        database = Path(str(raw.get("database") or "")).expanduser().absolute()
        session_id = str(
            raw.get("session_id") or raw.get("platform_session_id") or ""
        ).strip()
        status = str(
            raw.get("expected_status") or raw.get("status") or ""
        ).casefold()
        if not session_id or status not in _TERMINAL:
            raise FrontendSessionGuardError("Invalid terminal Cindy session seed")
        item = {
            "database": database,
            "session_id": session_id,
            "expected_status": status,
            "session_schema_fingerprint": raw.get("session_schema_fingerprint"),
            "session_row_fingerprint": raw.get("session_row_fingerprint"),
        }
        key = (_path_key(database), session_id)
        if key not in seen:
            seen.add(key)
            result.append(item)
    if not result:
        raise FrontendSessionGuardError("No terminal Cindy sessions selected")
    return tuple(sorted(result, key=lambda item: (_path_key(item["database"]), item["session_id"])))


def _normalize_evidence(
    items: Sequence[CindySessionDeleteEvidence | Mapping[str, Any]],
) -> tuple[CindySessionDeleteEvidence, ...]:
    result: list[CindySessionDeleteEvidence] = []
    for raw in items:
        if isinstance(raw, CindySessionDeleteEvidence):
            item = raw
        elif isinstance(raw, Mapping):
            item = CindySessionDeleteEvidence(
                database=Path(str(raw.get("database") or "")),
                session_id=str(raw.get("session_id") or ""),
                expected_status=str(raw.get("expected_status") or ""),
                session_schema_fingerprint=str(raw.get("session_schema_fingerprint") or ""),
                session_row_fingerprint=str(raw.get("session_row_fingerprint") or ""),
                stable_session_row_fingerprint=str(
                    raw.get("stable_session_row_fingerprint") or ""
                ),
                expected_sdk_session_id=(
                    None
                    if raw.get("expected_sdk_session_id") is None
                    else str(raw.get("expected_sdk_session_id"))
                ),
                schema_bundle_fingerprint=str(raw.get("schema_bundle_fingerprint") or ""),
                expected_message_count=int(raw.get("expected_message_count") or 0),
                message_id_fingerprint=str(raw.get("message_id_fingerprint") or ""),
                expected_fts_count=int(raw.get("expected_fts_count") or 0),
                expected_embedding_count=int(raw.get("expected_embedding_count") or 0),
                expected_vector_count=int(raw.get("expected_vector_count") or 0),
                expected_media_ref_count=int(raw.get("expected_media_ref_count") or 0),
                expected_skill_source_count=int(raw.get("expected_skill_source_count") or 0),
                expected_skill_exposure_count=int(raw.get("expected_skill_exposure_count") or 0),
                expected_ghost_card_count=int(raw.get("expected_ghost_card_count") or 0),
            )
        else:
            raise FrontendSessionGuardError("Invalid Cindy session evidence")
        result.append(item)
    keys = {(_path_key(item.database), item.session_id) for item in result}
    if not result or len(keys) != len(result):
        raise FrontendSessionGuardError("Missing or duplicate Cindy session evidence")
    return tuple(sorted(result, key=lambda item: (_path_key(item.database), item.session_id)))


def _assert_same(
    expected: Sequence[CindySessionDeleteEvidence],
    current: Sequence[CindySessionDeleteEvidence],
) -> None:
    left = {item.session_id: item for item in expected}
    right = {item.session_id: item for item in current}
    if set(left) != set(right):
        raise FrontendSessionGuardError("Approved Cindy session evidence changed")
    for session_id, approved in left.items():
        observed = right[session_id]
        approved_doc = approved.to_dict()
        observed_doc = observed.to_dict()
        approved_sdk = approved_doc.pop("expected_sdk_session_id")
        observed_sdk = observed_doc.pop("expected_sdk_session_id")
        # The preceding, separately authorized reference batch may clear this
        # one field.  The stable row fingerprint still binds every other
        # session column, while a replacement native ID remains forbidden.
        approved_doc.pop("session_row_fingerprint")
        observed_doc.pop("session_row_fingerprint")
        if approved_doc != observed_doc:
            raise FrontendSessionGuardError(
                "Approved Cindy session evidence changed"
            )
        allowed_sdk_values = (
            {None}
            if approved_sdk is None
            else {approved_sdk, None}
        )
        if observed_sdk not in allowed_sdk_values:
            raise FrontendSessionGuardError(
                "Approved Cindy sdk_session_id changed to another record"
            )


def _one_database(items: Sequence[CindySessionDeleteEvidence]) -> Path:
    paths = {_path_key(item.database): item.database for item in items}
    if len(paths) != 1:
        raise FrontendSessionGuardError("One batch must target one Cindy database")
    return next(iter(paths.values()))


def _one_database_from_seeds(items: Sequence[Mapping[str, Any]]) -> Path:
    paths = {_path_key(item["database"]): item["database"] for item in items}
    if len(paths) != 1:
        raise FrontendSessionGuardError("One batch must target one Cindy database")
    return next(iter(paths.values()))


def _temp_targets(db: sqlite3.Connection, ids: Sequence[str]) -> None:
    db.execute("CREATE TEMP TABLE larj_cindy_targets (id TEXT PRIMARY KEY)")
    db.executemany(
        "INSERT INTO temp.larj_cindy_targets (id) VALUES (?)",
        ((value,) for value in ids),
    )
    db.execute("CREATE TEMP TABLE larj_cindy_messages (id TEXT PRIMARY KEY)")
    db.execute(
        """INSERT INTO temp.larj_cindy_messages (id)
           SELECT message.id FROM messages message
           JOIN temp.larj_cindy_targets target ON target.id = message.session_id"""
    )


def _rows(
    db: sqlite3.Connection,
    table: str,
    column: str,
    values: Sequence[str],
) -> tuple[sqlite3.Row, ...]:
    marks = ",".join("?" for _ in values)
    return tuple(
        db.execute(
            f'SELECT * FROM "{table}" WHERE "{column}" IN ({marks}) ORDER BY "{column}"',
            tuple(values),
        )
    )


def _selected_ids(
    db: sqlite3.Connection,
    table: str,
    column: str,
    values: Sequence[str],
) -> tuple[str, ...]:
    marks = ",".join("?" for _ in values)
    return tuple(
        str(row[0])
        for row in db.execute(
            f'SELECT "{column}" FROM "{table}" WHERE "{column}" IN ({marks})',
            tuple(values),
        )
    )


def _count(
    db: sqlite3.Connection,
    table: str,
    predicate: str,
    params: Sequence[Any],
) -> int:
    if not _has(db, table):
        return 0
    return int(
        db.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE {predicate}',
            tuple(params),
        ).fetchone()[0]
    )


def _count_values(
    db: sqlite3.Connection,
    table: str,
    column: str,
    values: Sequence[Any],
) -> int:
    total = 0
    quoted = '"' + column.replace('"', '""') + '"'
    for offset in range(0, len(values), 500):
        chunk = tuple(values[offset : offset + 500])
        marks = ",".join("?" for _ in chunk)
        total += int(
            db.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE {quoted} IN ({marks})',
                chunk,
            ).fetchone()[0]
        )
    return total


def _delete(db: sqlite3.Connection, table: str, predicate: str) -> int:
    if not _has(db, table):
        return 0
    cursor = db.execute(f'DELETE FROM "{table}" WHERE {predicate}')
    return max(0, int(cursor.rowcount))


def _has(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_schema WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone() is not None


def _schema_hash(db: sqlite3.Connection) -> str:
    rows = db.execute(
        """SELECT type,name,tbl_name,COALESCE(sql,'') AS sql
           FROM sqlite_schema
           WHERE name NOT LIKE 'sqlite_stat%'
           ORDER BY type,name,tbl_name,sql"""
    ).fetchall()
    payload = [
        [str(row["type"]), str(row["name"]), str(row["tbl_name"]), str(row["sql"])]
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _ids_hash(values: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _connect(
    database: Path,
    *,
    readonly: bool,
    vector_extension: Path | None,
) -> sqlite3.Connection:
    _validate_database(database)
    if readonly:
        db = connect_readonly(database)
    else:
        db = sqlite3.connect(database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
    if db.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='chat_messages_vec_v1'"
    ).fetchone():
        extension = _vector_extension(vector_extension)
        try:
            db.enable_load_extension(True)
            db.load_extension(str(extension))
        except (AttributeError, OSError, sqlite3.Error) as exc:
            db.close()
            raise FrontendSessionGuardError("Cindy sqlite-vec extension is required") from exc
        finally:
            try:
                db.enable_load_extension(False)
            except (AttributeError, sqlite3.Error):
                pass
    return db


def _vector_extension(explicit: Path | None) -> Path:
    candidates = [Path(explicit)] if explicit is not None else []
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(
            Path(os.environ["LOCALAPPDATA"])
            / "Programs/Cindy/resources/app.asar.unpacked/native/sqlite-vec/win32-x64/vec0.dll"
        )
    for candidate in candidates:
        try:
            state = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(state.st_mode) and not stat.S_ISLNK(state.st_mode):
            return candidate.absolute()
    raise FrontendSessionGuardError("Could not locate Cindy sqlite-vec extension")


def _validate_database(database: Path) -> None:
    try:
        state = database.lstat()
    except OSError as exc:
        raise FrontendSessionGuardError(f"Could not inspect Cindy database: {exc}") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise FrontendSessionGuardError("Cindy database is not an ordinary file")


def _backup(database: Path) -> tuple[Path, Path]:
    directory = Path(tempfile.mkdtemp(prefix=".larj-cindy-session-", dir=database.parent))
    path = directory / "database.sqlite"
    try:
        with closing(connect_readonly(database)) as source, closing(
            sqlite3.connect(path)
        ) as destination:
            source.backup(destination)
        return directory, path
    except Exception:
        try:
            path.unlink(missing_ok=True)
            directory.rmdir()
        except OSError:
            pass
        raise


def _discard(path: Path, directory: Path) -> None:
    path.unlink(missing_ok=True)
    directory.rmdir()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path.expanduser())))


__all__ = [
    "CindySessionDeleteEvidence",
    "FrontendSessionCleanupError",
    "FrontendSessionCleanupResult",
    "build_cindy_session_delete_evidence",
    "execute_cindy_session_cleanup",
    "guard_cindy_session_rows",
    "verify_cindy_session_rows",
]
