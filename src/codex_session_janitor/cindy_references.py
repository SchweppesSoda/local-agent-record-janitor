"""Read-only extraction of Cindy references to native agent sessions.

The Cindy database is evidence used to decide whether native data may be
removed.  This module therefore treats an incomplete schema or an ambiguous
``agent_switch`` row as a blocking inventory failure instead of silently
returning fewer references.
"""

from __future__ import annotations

import json
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .sqlite_utils import connect_readonly, table_exists


_BACKENDS = {"cc": "claude", "codex": "codex", "pi": "pi"}


@dataclass(frozen=True)
class CindyNativeReference:
    database: Path
    profile_root: Path
    cindy_session_id: str
    session_status: str | None
    working_dir: str | None
    session_updated_at_ms: int | None
    backend: str
    native_session_id: str | None
    reference_kind: str
    agent_kind: str
    boundary_id: str | None = None
    boundary_created_at_ms: int | None = None
    boundary_rewind_at_ms: int | None = None
    session_details: Mapping[str, Any] | None = None

    @property
    def is_live(self) -> bool:
        return _normalized(self.session_status) != "deleted"

    @property
    def is_historical(self) -> bool:
        return self.reference_kind == "agent_switch"

    def approval_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "database": str(self.database),
            "profile_root": str(self.profile_root),
            "cindy_session_id": self.cindy_session_id,
            "session_status": self.session_status,
            "working_dir": self.working_dir,
            "session_updated_at_ms": self.session_updated_at_ms,
            "backend": self.backend,
            "native_session_id": self.native_session_id,
            "reference_kind": self.reference_kind,
            "agent_kind": self.agent_kind,
            "boundary_id": self.boundary_id,
            "boundary_created_at_ms": self.boundary_created_at_ms,
            "boundary_rewind_at_ms": self.boundary_rewind_at_ms,
            "is_live": self.is_live,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.approval_payload(),
            "session_details": dict(self.session_details or {}),
        }


@dataclass(frozen=True)
class CindyReferenceFailure:
    database: Path
    profile_root: Path
    error_type: str
    message: str
    blocks_delete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database),
            "profile_root": str(self.profile_root),
            "error_type": self.error_type,
            "message": self.message,
            "blocks_delete": self.blocks_delete,
        }


@dataclass(frozen=True)
class CindyReferenceCatalog:
    database: Path
    profile_root: Path
    references: tuple[CindyNativeReference, ...] = ()
    failures: tuple[CindyReferenceFailure, ...] = ()

    @property
    def errors(self) -> tuple[CindyReferenceFailure, ...]:
        return self.failures

    def for_backend(self, backend: str) -> tuple[CindyNativeReference, ...]:
        return tuple(ref for ref in self.references if ref.backend == backend)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "database": str(self.database),
            "profile_root": str(self.profile_root),
            "references": [reference.to_dict() for reference in self.references],
            "failures": [failure.to_dict() for failure in self.failures],
        }


class CindyReferenceError(RuntimeError):
    """The database cannot provide a complete native-reference snapshot."""


def build_cindy_reference_catalog(
    database: Path,
    *,
    profile_root: Path | None = None,
) -> CindyReferenceCatalog:
    """Extract current and all historical native bindings from one Cindy DB.

    A missing optional database is an empty catalog.  Once a database exists,
    both tables and every relevant value must be understandable.
    """

    database = database.expanduser()
    root = (profile_root or database.parent).expanduser()
    try:
        status = database.stat()
    except FileNotFoundError:
        return CindyReferenceCatalog(database=database, profile_root=root)
    except OSError as exc:
        return _failed(database, root, exc, "Could not inspect Cindy database")
    if not stat.S_ISREG(status.st_mode):
        return _failed(
            database,
            root,
            CindyReferenceError("Cindy database path is not a regular file"),
            "Could not inspect Cindy database",
        )

    try:
        with closing(connect_readonly(database)) as connection:
            session_columns = _require_columns(
                connection,
                "sessions",
                {"id", "sdk_session_id", "status", "agent_kind"},
            )
            optional = (
                "working_dir",
                "updated_at",
                "source",
                "created_at",
                "parent_session_id",
                "title",
            )
            projections = [
                column if column in session_columns else f"NULL AS {column}"
                for column in optional
            ]
            session_rows = connection.execute(
                f"""
                SELECT id, sdk_session_id, status, agent_kind,
                       {", ".join(projections)}
                FROM sessions
                ORDER BY id
                """
            ).fetchall()
            if table_exists(connection, "messages"):
                _require_columns(
                    connection,
                    "messages",
                    {"id", "session_id", "role", "content", "created_at", "rewind_at"},
                )
                switch_rows = connection.execute(
                    """
                    SELECT id, session_id, content, created_at, rewind_at
                    FROM messages
                    WHERE role = 'agent_switch'
                    ORDER BY created_at, rowid
                    """
                ).fetchall()
            else:
                # Before engine switching Cindy databases had no messages
                # table.  Current session bindings remain reliable evidence;
                # there cannot be a switch row hidden in an absent table.
                switch_rows = []
    except (sqlite3.Error, CindyReferenceError) as exc:
        return _failed(database, root, exc, "Could not read Cindy native references")

    try:
        sessions: dict[str, sqlite3.Row] = {}
        references: list[CindyNativeReference] = []
        for row in session_rows:
            session_id = _required_string(row["id"], "sessions.id")
            if session_id in sessions:
                raise CindyReferenceError("sessions.id is duplicated")
            sessions[session_id] = row
            kind = _agent_kind(row["agent_kind"], "sessions.agent_kind")
            backend = _BACKENDS.get(kind)
            if backend is None:
                continue
            references.append(
                _reference(
                    database,
                    root,
                    row,
                    backend=backend,
                    agent_kind=kind,
                    native_session_id=_optional_native_id(row["sdk_session_id"], "sessions.sdk_session_id"),
                    reference_kind="current",
                )
            )

        for row in switch_rows:
            session_id = _required_string(row["session_id"], "messages.session_id")
            session = sessions.get(session_id)
            if session is None:
                raise CindyReferenceError(
                    "agent_switch row refers to a missing Cindy session"
                )
            payload = _switch_payload(row["content"])
            kind = _agent_kind(payload.get("fromAgentKind"), "agent_switch.fromAgentKind")
            native_id = _optional_native_id(
                payload.get("fromSdkSessionId"), "agent_switch.fromSdkSessionId"
            )
            if "fromSdkSessionId" not in payload:
                raise CindyReferenceError(
                    "agent_switch content has no fromSdkSessionId field"
                )
            backend = _BACKENDS.get(kind)
            if backend is None or native_id is None:
                continue
            references.append(
                _reference(
                    database,
                    root,
                    session,
                    backend=backend,
                    agent_kind=kind,
                    native_session_id=native_id,
                    reference_kind="agent_switch",
                    boundary_id=_required_string(row["id"], "messages.id"),
                    boundary_created_at_ms=_optional_int(row["created_at"], "messages.created_at"),
                    boundary_rewind_at_ms=_optional_int(row["rewind_at"], "messages.rewind_at"),
                )
            )
    except CindyReferenceError as exc:
        return _failed(database, root, exc, "Could not safely interpret Cindy native references")

    references.sort(
        key=lambda ref: (
            ref.cindy_session_id,
            0 if ref.reference_kind == "current" else 1,
            ref.boundary_created_at_ms or -1,
            ref.boundary_id or "",
            ref.backend,
            ref.native_session_id or "",
        )
    )
    return CindyReferenceCatalog(database, root, tuple(references))


def _reference(
    database: Path,
    root: Path,
    session: sqlite3.Row,
    *,
    backend: str,
    agent_kind: str,
    native_session_id: str | None,
    reference_kind: str,
    boundary_id: str | None = None,
    boundary_created_at_ms: int | None = None,
    boundary_rewind_at_ms: int | None = None,
) -> CindyNativeReference:
    return CindyNativeReference(
        database=database,
        profile_root=root,
        cindy_session_id=_required_string(session["id"], "sessions.id"),
        session_status=_optional_string(session["status"], "sessions.status"),
        working_dir=_optional_string(session["working_dir"], "sessions.working_dir"),
        session_updated_at_ms=_optional_int(session["updated_at"], "sessions.updated_at"),
        backend=backend,
        native_session_id=native_session_id,
        reference_kind=reference_kind,
        agent_kind=agent_kind,
        boundary_id=boundary_id,
        boundary_created_at_ms=boundary_created_at_ms,
        boundary_rewind_at_ms=boundary_rewind_at_ms,
        session_details={
            "source": session["source"],
            "created_at": session["created_at"],
            "parent_session_id": session["parent_session_id"],
            "title": session["title"],
        },
    )


def _switch_payload(value: object) -> Mapping[str, Any]:
    if not isinstance(value, str):
        raise CindyReferenceError("agent_switch content is not JSON text")
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CindyReferenceError("agent_switch content is malformed JSON") from exc
    if not isinstance(payload, Mapping):
        raise CindyReferenceError("agent_switch content is not a JSON object")
    if "fromAgentKind" not in payload:
        raise CindyReferenceError("agent_switch content has no fromAgentKind field")
    return payload


def _require_columns(
    connection: sqlite3.Connection, table: str, required: set[str]
) -> set[str]:
    if not table_exists(connection, table):
        raise CindyReferenceError(f"required table {table!r} is missing")
    columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})")
        if isinstance(row["name"], str)
    }
    missing = sorted(required - columns)
    if missing:
        raise CindyReferenceError(
            f"table {table!r} is missing column(s) {', '.join(missing)}"
        )
    return columns


def _required_string(value: object, label: str) -> str:
    displayed = _optional_string(value, label)
    if displayed is None:
        raise CindyReferenceError(f"{label} contains an invalid value")
    return displayed


def _agent_kind(value: object, label: str) -> str:
    return _required_string(value, label).lower()


def _optional_native_id(value: object, label: str) -> str | None:
    return _optional_string(value, label)


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CindyReferenceError(f"{label} is not text")
    stripped = value.strip()
    return stripped or None


def _optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise CindyReferenceError(f"{label} is not an integer")
    return value


def _normalized(value: str | None) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _failed(
    database: Path,
    root: Path,
    exc: Exception,
    prefix: str,
) -> CindyReferenceCatalog:
    return CindyReferenceCatalog(
        database=database,
        profile_root=root,
        failures=(
            CindyReferenceFailure(
                database=database,
                profile_root=root,
                error_type=type(exc).__name__,
                message=f"{prefix}: {exc}",
            ),
        ),
    )


__all__ = [
    "CindyNativeReference",
    "CindyReferenceCatalog",
    "CindyReferenceError",
    "CindyReferenceFailure",
    "build_cindy_reference_catalog",
]
