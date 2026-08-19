from __future__ import annotations

import json
import sqlite3
import stat
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_desktop_state import ClientInspector
from .sqlite_identity import (
    quote_identifier,
    row_fingerprint,
    schema_fingerprint,
    table_schema,
)


_TABLE = "thread_spawn_edges"
_ALLOWED_COLUMNS = frozenset(
    {
        "parent_thread_id",
        "child_thread_id",
        "status",
        "created_at",
        "updated_at",
    }
)


class RelationCleanupError(RuntimeError):
    """An exact native relation row could not be safely removed."""


@dataclass(frozen=True)
class RelationCleanupResult:
    database: Path
    removed_relation_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "cleaned",
            "database": str(self.database),
            "removed_relation_count": self.removed_relation_count,
            "temporary_backup_retained": False,
            "verification": {"remaining_approved_relations": 0},
        }


@dataclass(frozen=True)
class _PreparedRelation:
    evidence: Mapping[str, Any]
    columns: tuple[str, ...]
    parent_thread_id: str
    child_thread_id: str
    status: Any


def execute_relation_cleanup(
    codex_home: Path,
    evidence_items: Sequence[Mapping[str, Any]],
    *,
    client_inspector: ClientInspector,
) -> RelationCleanupResult:
    """Delete exact approved spawn-edge rows in one SQLite transaction."""

    evidence = tuple(dict(item) for item in evidence_items)
    if not evidence:
        raise RelationCleanupError("No exact relation evidence was authorized")
    databases = {str(item.get("database") or "") for item in evidence}
    if len(databases) != 1 or "" in databases:
        raise RelationCleanupError(
            "One relation cleanup batch must target one physical database"
        )
    home = codex_home.expanduser().resolve()
    database = Path(next(iter(databases))).expanduser().absolute()
    _validate_database(home, database)
    _require_clients_closed(home, client_inspector)

    backup_directory = Path(
        tempfile.mkdtemp(prefix=".larj-relation-", dir=database.parent)
    )
    backup_path = backup_directory / "database.sqlite"
    try:
        _sqlite_backup(database, backup_path)
        _require_clients_closed(home, client_inspector)
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            integrity = connection.execute("PRAGMA integrity_check(1)").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise RelationCleanupError(
                    "Codex state database failed SQLite integrity_check"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                prepared = tuple(
                    _prepare_relation(connection, item) for item in evidence
                )
                identities = {
                    (
                        item.parent_thread_id,
                        item.child_thread_id,
                        _json_identity(item.status),
                    )
                    for item in prepared
                }
                if len(identities) != len(prepared):
                    raise RelationCleanupError(
                        "The relation batch contains duplicate row mutations"
                    )
                changes_before = connection.total_changes
                for item in prepared:
                    _delete_relation(connection, item)
                changed = connection.total_changes - changes_before
                if changed != len(prepared):
                    raise RelationCleanupError(
                        "Relation transaction affected an unexpected number "
                        f"of rows: expected {len(prepared)}, got {changed}"
                    )
                for item in prepared:
                    if _matching_rows(connection, item):
                        raise RelationCleanupError(
                            "Approved relation row remains after deletion"
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        with closing(sqlite3.connect(database)) as verification:
            verification.row_factory = sqlite3.Row
            for item in prepared:
                if _matching_rows(verification, item):
                    raise RelationCleanupError(
                        "Post-commit verification found an approved relation"
                    )
    except Exception as exc:
        restore_error = _restore_backup(database, backup_path)
        if restore_error is not None:
            raise RelationCleanupError(
                f"Relation cleanup failed ({exc}); rollback also failed "
                f"({restore_error}). Temporary backup: {backup_path}"
            ) from exc
        _discard_backup(backup_path, backup_directory)
        if isinstance(exc, RelationCleanupError):
            raise
        raise RelationCleanupError(str(exc) or repr(exc)) from exc

    _discard_backup(backup_path, backup_directory)
    return RelationCleanupResult(database, len(evidence))


def verify_relation_evidence(
    evidence_items: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    grouped: dict[Path, list[Mapping[str, Any]]] = {}
    for item in evidence_items:
        database = Path(str(item.get("database") or "")).expanduser()
        grouped.setdefault(database, []).append(item)
    markers: list[str] = []
    for database, items in grouped.items():
        _validate_ordinary_file(database)
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            for evidence in items:
                prepared = _prepare_relation(
                    connection,
                    evidence,
                    require_present=False,
                )
                if _matching_rows(connection, prepared):
                    markers.append(
                        "spawn-edge:"
                        f"{database}:{prepared.parent_thread_id}->"
                        f"{prepared.child_thread_id}:"
                        f"{json.dumps(prepared.status, ensure_ascii=False)}"
                    )
    return tuple(sorted(markers))


def _prepare_relation(
    connection: sqlite3.Connection,
    evidence: Mapping[str, Any],
    *,
    require_present: bool = True,
) -> _PreparedRelation:
    if evidence.get("schema_version") != 1 or evidence.get("table") != _TABLE:
        raise RelationCleanupError("Unsupported relation evidence schema")
    schema = table_schema(connection, _TABLE)
    if not schema:
        raise RelationCleanupError("Codex state database has no spawn-edge table")
    columns = tuple(str(item["name"]) for item in schema)
    if (
        schema_fingerprint(schema) != evidence.get("schema_fingerprint")
        or list(columns) != evidence.get("columns")
    ):
        raise RelationCleanupError("Spawn-edge schema changed after authorization")
    if not {"parent_thread_id", "child_thread_id"}.issubset(columns):
        raise RelationCleanupError("Spawn-edge schema is missing identity columns")
    if not set(columns).issubset(_ALLOWED_COLUMNS):
        raise RelationCleanupError("Spawn-edge schema contains an unapproved column")
    trigger = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=? LIMIT 1",
        (_TABLE,),
    ).fetchone()
    if trigger is not None:
        raise RelationCleanupError("Spawn-edge table has an unapproved trigger")
    expected = evidence.get("expected")
    if not isinstance(expected, Mapping):
        raise RelationCleanupError("Relation evidence has no expected identity")
    parent = expected.get("parent_thread_id")
    child = expected.get("child_thread_id")
    status = expected.get("status")
    if not isinstance(parent, str) or not parent or not isinstance(child, str) or not child:
        raise RelationCleanupError("Relation evidence has invalid endpoint IDs")
    if "status" not in columns and status is not None:
        raise RelationCleanupError("Relation status cannot be bound by this schema")
    prepared = _PreparedRelation(evidence, columns, parent, child, status)
    rows = _matching_rows(connection, prepared)
    if require_present:
        if len(rows) != 1:
            raise RelationCleanupError(
                "Approved relation no longer identifies exactly one row"
            )
        if row_fingerprint(rows[0], columns) != evidence.get("row_fingerprint"):
            raise RelationCleanupError("Spawn-edge row changed after authorization")
    elif len(rows) > 1:
        raise RelationCleanupError(
            "Approved relation identity became ambiguous during verification"
        )
    return prepared


def _matching_rows(
    connection: sqlite3.Connection,
    item: _PreparedRelation,
) -> list[sqlite3.Row]:
    projection = ", ".join(quote_identifier(column) for column in item.columns)
    sql = (
        f"SELECT {projection} FROM {quote_identifier(_TABLE)} "
        "WHERE parent_thread_id = ? AND child_thread_id = ?"
    )
    parameters: tuple[Any, ...] = (
        item.parent_thread_id,
        item.child_thread_id,
    )
    if "status" in item.columns:
        sql += " AND status IS ?"
        parameters = (*parameters, item.status)
    return list(connection.execute(sql, parameters).fetchall())


def _delete_relation(connection: sqlite3.Connection, item: _PreparedRelation) -> None:
    sql = (
        f"DELETE FROM {quote_identifier(_TABLE)} "
        "WHERE parent_thread_id = ? AND child_thread_id = ?"
    )
    parameters: tuple[Any, ...] = (
        item.parent_thread_id,
        item.child_thread_id,
    )
    if "status" in item.columns:
        sql += " AND status IS ?"
        parameters = (*parameters, item.status)
    cursor = connection.execute(sql, parameters)
    if cursor.rowcount != 1:
        raise RelationCleanupError(
            f"Expected one relation row deletion, got {cursor.rowcount}"
        )


def _validate_database(home: Path, database: Path) -> None:
    expected = home / "state_5.sqlite"
    try:
        same = database.samefile(expected)
    except OSError:
        same = False
    if not same:
        raise RelationCleanupError(
            "Relation evidence does not target this store's state_5.sqlite"
        )
    _validate_ordinary_file(database)


def _validate_ordinary_file(path: Path) -> None:
    try:
        state = path.lstat()
    except OSError as exc:
        raise RelationCleanupError(f"Could not inspect relation database: {exc}") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise RelationCleanupError("Relation database is not an ordinary file")


def _require_clients_closed(home: Path, inspector: ClientInspector) -> None:
    clients = inspector(home)
    if clients:
        raise RelationCleanupError(
            "Related clients are still running: " + ", ".join(clients)
        )


def _sqlite_backup(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source)) as source_connection,
        closing(sqlite3.connect(destination)) as destination_connection,
    ):
        source_connection.backup(destination_connection)


def _restore_backup(database: Path, backup: Path) -> str | None:
    if not backup.is_file():
        return "temporary backup is missing"
    try:
        with (
            closing(sqlite3.connect(backup)) as source_connection,
            closing(sqlite3.connect(database)) as destination_connection,
        ):
            source_connection.backup(destination_connection)
        return None
    except (OSError, sqlite3.Error) as exc:
        return str(exc) or repr(exc)


def _discard_backup(backup: Path, directory: Path) -> None:
    try:
        backup.unlink(missing_ok=True)
        directory.rmdir()
    except OSError as exc:
        raise RelationCleanupError(
            f"Could not discard successful temporary backup: {exc}"
        ) from exc


def _json_identity(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "RelationCleanupError",
    "RelationCleanupResult",
    "execute_relation_cleanup",
    "verify_relation_evidence",
]
