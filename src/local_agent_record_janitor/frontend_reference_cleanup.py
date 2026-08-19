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
    text_sha256,
)


class FrontendReferenceError(RuntimeError):
    """An exact frontend reference batch could not be safely applied."""


@dataclass(frozen=True)
class FrontendReferenceCleanupResult:
    database: Path
    platform: str
    removed_reference_count: int
    deleted_aionui_rows: int = 0
    cleared_cindy_current_references: int = 0
    cleaned_cindy_historical_references: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "cleaned",
            "database": str(self.database),
            "platform": self.platform,
            "removed_reference_count": self.removed_reference_count,
            "deleted_aionui_rows": self.deleted_aionui_rows,
            "cleared_cindy_current_references": (
                self.cleared_cindy_current_references
            ),
            "cleaned_cindy_historical_references": (
                self.cleaned_cindy_historical_references
            ),
            "temporary_backup_retained": False,
            "verification": {"remaining_approved_references": 0},
        }


@dataclass(frozen=True)
class _PreparedMutation:
    platform: str
    operation: str
    table: str
    locator: Mapping[str, Any]
    original_row: Mapping[str, Any]
    columns: tuple[str, ...]
    replacement_content: str | None = None


def execute_frontend_reference_cleanup(
    codex_home: Path,
    evidence_items: Sequence[Mapping[str, Any]],
    *,
    client_inspector: ClientInspector,
) -> FrontendReferenceCleanupResult:
    """Apply one physical frontend-database batch transactionally."""

    evidence = tuple(dict(item) for item in evidence_items)
    if not evidence:
        raise FrontendReferenceError(
            "No exact frontend reference evidence was authorized"
        )
    databases = {
        str(item.get("database") or "") for item in evidence
    }
    platforms = {str(item.get("platform") or "") for item in evidence}
    if len(databases) != 1 or "" in databases:
        raise FrontendReferenceError(
            "One frontend cleanup batch must target one physical database"
        )
    if len(platforms) != 1 or not platforms <= {"aionui", "cindy"}:
        raise FrontendReferenceError(
            "One frontend cleanup batch must target one supported frontend"
        )
    database = Path(next(iter(databases))).expanduser().absolute()
    platform = next(iter(platforms))
    _validate_database_file(database)
    _require_clients_closed(codex_home, client_inspector)

    backup_directory = Path(
        tempfile.mkdtemp(
            prefix=".larj-frontend-",
            dir=database.parent,
        )
    )
    backup_path = backup_directory / "database.sqlite"
    try:
        _sqlite_backup(database, backup_path)
        _require_clients_closed(codex_home, client_inspector)
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            integrity = connection.execute(
                "PRAGMA integrity_check(1)"
            ).fetchone()
            if integrity is None or integrity[0] != "ok":
                raise FrontendReferenceError(
                    "Frontend database failed SQLite integrity_check"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                prepared = tuple(
                    _prepare_mutation(connection, item)
                    for item in evidence
                )
                _reject_duplicate_mutations(prepared)
                changes_before = connection.total_changes
                for mutation in prepared:
                    _apply_mutation(connection, mutation)
                changed = connection.total_changes - changes_before
                if changed != len(prepared):
                    raise FrontendReferenceError(
                        "Frontend transaction affected an unexpected number "
                        f"of rows: expected {len(prepared)}, got {changed}"
                    )
                for mutation in prepared:
                    _verify_mutation(connection, mutation)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        with closing(sqlite3.connect(database)) as verification_connection:
            verification_connection.row_factory = sqlite3.Row
            for mutation in prepared:
                _verify_mutation(verification_connection, mutation)
    except Exception as exc:
        restore_error = _restore_backup(database, backup_path)
        if restore_error is not None:
            raise FrontendReferenceError(
                f"Frontend cleanup failed ({exc}); rollback also failed "
                f"({restore_error}). Temporary backup: {backup_path}"
            ) from exc
        _discard_backup(backup_path, backup_directory)
        if isinstance(exc, FrontendReferenceError):
            raise
        raise FrontendReferenceError(str(exc) or repr(exc)) from exc

    _discard_backup(backup_path, backup_directory)
    operations = [mutation.operation for mutation in prepared]
    return FrontendReferenceCleanupResult(
        database=database,
        platform=platform,
        removed_reference_count=len(prepared),
        deleted_aionui_rows=operations.count("delete_row"),
        cleared_cindy_current_references=operations.count(
            "clear_session_sdk_session_id"
        ),
        cleaned_cindy_historical_references=operations.count(
            "remove_agent_switch_from_sdk_session_id"
        ),
    )


def verify_frontend_reference_evidence(
    evidence_items: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return stable residual markers for approved frontend references."""

    grouped: dict[Path, list[Mapping[str, Any]]] = {}
    for item in evidence_items:
        database = Path(str(item.get("database") or "")).expanduser()
        grouped.setdefault(database, []).append(item)
    remaining: list[str] = []
    for database, items in grouped.items():
        _validate_database_file(database)
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            for item in items:
                if _reference_is_present(connection, item):
                    locator = item.get("locator")
                    remaining.append(
                        "frontend-reference:"
                        f"{item.get('platform')}:{database}:"
                        f"{json.dumps(locator, sort_keys=True, ensure_ascii=False)}"
                    )
    return tuple(sorted(remaining))


def _prepare_mutation(
    connection: sqlite3.Connection,
    evidence: Mapping[str, Any],
) -> _PreparedMutation:
    if evidence.get("schema_version") != 1:
        raise FrontendReferenceError(
            "Unsupported frontend reference evidence version"
        )
    platform = str(evidence.get("platform") or "")
    operation = str(evidence.get("operation") or "")
    if platform == "aionui" and operation == "delete_row":
        return _prepare_aionui_delete(connection, evidence)
    if platform == "cindy" and operation in {
        "clear_session_sdk_session_id",
        "remove_agent_switch_from_sdk_session_id",
    }:
        return _prepare_cindy_mutation(connection, evidence)
    raise FrontendReferenceError(
        f"Unsupported frontend reference operation: {platform}/{operation}"
    )


def _prepare_aionui_delete(
    connection: sqlite3.Connection,
    evidence: Mapping[str, Any],
) -> _PreparedMutation:
    if evidence.get("table") != "acp_session":
        raise FrontendReferenceError("AionUI evidence targets an unknown table")
    schema, columns = _validate_schema(
        connection,
        "acp_session",
        str(evidence.get("schema_fingerprint") or ""),
        required={
            "conversation_id",
            "session_id",
            "agent_id",
            "agent_source",
            "session_status",
            "last_active_at",
        },
    )
    _ensure_no_triggers(connection, "acp_session")
    locator = _mapping(evidence.get("locator"))
    row = _select_aionui_row(connection, locator, schema)
    if row_fingerprint(row, columns) != evidence.get("row_fingerprint"):
        raise FrontendReferenceError(
            "AionUI acp_session row changed after authorization"
        )
    expected = _mapping(evidence.get("expected"))
    if (
        row["conversation_id"] != expected.get("conversation_id")
        or row["session_id"] != expected.get("session_id")
    ):
        raise FrontendReferenceError(
            "AionUI row identity no longer matches the approved mapping"
        )
    return _PreparedMutation(
        platform="aionui",
        operation="delete_row",
        table="acp_session",
        locator=locator,
        original_row={column: row[column] for column in columns},
        columns=columns,
    )


def _prepare_cindy_mutation(
    connection: sqlite3.Connection,
    evidence: Mapping[str, Any],
) -> _PreparedMutation:
    operation = str(evidence.get("operation") or "")
    _session_schema, session_columns = _validate_schema(
        connection,
        "sessions",
        str(evidence.get("session_schema_fingerprint") or ""),
        required={"id", "sdk_session_id", "status", "agent_kind"},
    )
    _ensure_no_triggers(connection, "sessions")
    locator = _mapping(evidence.get("locator"))
    session_id = locator.get("cindy_session_id")
    if not isinstance(session_id, str) or not session_id:
        raise FrontendReferenceError("Cindy evidence has no session ID")
    session_row = _select_unique(
        connection,
        "sessions",
        "id",
        session_id,
    )
    if (
        row_fingerprint(session_row, session_columns)
        != evidence.get("session_row_fingerprint")
    ):
        raise FrontendReferenceError(
            "Cindy session row changed after authorization"
        )
    expected = _mapping(evidence.get("expected"))
    native_id = expected.get("native_session_id")
    if not isinstance(native_id, str) or not native_id:
        raise FrontendReferenceError("Cindy evidence has no native session ID")

    if operation == "clear_session_sdk_session_id":
        if session_row["sdk_session_id"] != native_id:
            raise FrontendReferenceError(
                "Cindy current sdk_session_id no longer matches"
            )
        return _PreparedMutation(
            platform="cindy",
            operation=operation,
            table="sessions",
            locator=locator,
            original_row={
                column: session_row[column] for column in session_columns
            },
            columns=session_columns,
        )

    _message_schema, message_columns = _validate_schema(
        connection,
        "messages",
        str(evidence.get("message_schema_fingerprint") or ""),
        required={
            "id",
            "session_id",
            "role",
            "content",
            "created_at",
            "rewind_at",
        },
    )
    _ensure_no_triggers(connection, "messages")
    message_id = locator.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise FrontendReferenceError("Cindy history evidence has no message ID")
    message_row = _select_unique(
        connection,
        "messages",
        "id",
        message_id,
    )
    content = message_row["content"]
    if not isinstance(content, str):
        raise FrontendReferenceError("Cindy agent_switch content is not text")
    if (
        row_fingerprint(message_row, message_columns)
        != evidence.get("message_row_fingerprint")
        or text_sha256(content) != evidence.get("message_content_sha256")
    ):
        raise FrontendReferenceError(
            "Cindy agent_switch message changed after authorization"
        )
    replacement = remove_top_level_json_field(
        content,
        "fromSdkSessionId",
        expected_value=native_id,
    )
    return _PreparedMutation(
        platform="cindy",
        operation=operation,
        table="messages",
        locator=locator,
        original_row={column: message_row[column] for column in message_columns},
        columns=message_columns,
        replacement_content=replacement,
    )


def _apply_mutation(
    connection: sqlite3.Connection,
    mutation: _PreparedMutation,
) -> None:
    if mutation.operation == "delete_row":
        where, parameters = _aionui_where(mutation.locator)
        cursor = connection.execute(
            f"DELETE FROM acp_session WHERE {where}",
            parameters,
        )
    elif mutation.operation == "clear_session_sdk_session_id":
        cursor = connection.execute(
            "UPDATE sessions SET sdk_session_id = NULL "
            "WHERE id = ? AND sdk_session_id = ?",
            (
                mutation.locator["cindy_session_id"],
                mutation.original_row["sdk_session_id"],
            ),
        )
    else:
        cursor = connection.execute(
            "UPDATE messages SET content = ? WHERE id = ? AND content = ?",
            (
                mutation.replacement_content,
                mutation.locator["message_id"],
                mutation.original_row["content"],
            ),
        )
    if cursor.rowcount != 1:
        raise FrontendReferenceError(
            f"Exact frontend mutation affected {cursor.rowcount} rows"
        )


def _verify_mutation(
    connection: sqlite3.Connection,
    mutation: _PreparedMutation,
) -> None:
    if mutation.operation == "delete_row":
        where, parameters = _aionui_where(mutation.locator)
        count = connection.execute(
            f"SELECT COUNT(*) FROM acp_session WHERE {where}",
            parameters,
        ).fetchone()[0]
        if count != 0:
            raise FrontendReferenceError("AionUI row remains after deletion")
        return

    table = "sessions" if mutation.table == "sessions" else "messages"
    locator_column = "id"
    locator_value = (
        mutation.locator["cindy_session_id"]
        if table == "sessions"
        else mutation.locator["message_id"]
    )
    row = _select_unique(connection, table, locator_column, locator_value)
    expected = dict(mutation.original_row)
    if mutation.operation == "clear_session_sdk_session_id":
        expected["sdk_session_id"] = None
    else:
        expected["content"] = mutation.replacement_content
    actual = {column: row[column] for column in mutation.columns}
    if actual != expected:
        raise FrontendReferenceError(
            "Frontend write changed fields outside the approved reference"
        )


def _reference_is_present(
    connection: sqlite3.Connection,
    evidence: Mapping[str, Any],
) -> bool:
    platform = str(evidence.get("platform") or "")
    operation = str(evidence.get("operation") or "")
    locator = _mapping(evidence.get("locator"))
    if platform == "aionui" and operation == "delete_row":
        where, parameters = _aionui_where(locator)
        return bool(
            connection.execute(
                f"SELECT 1 FROM acp_session WHERE {where} LIMIT 1",
                parameters,
            ).fetchone()
        )
    expected = _mapping(evidence.get("expected"))
    native_id = expected.get("native_session_id")
    if operation == "clear_session_sdk_session_id":
        row = connection.execute(
            "SELECT sdk_session_id FROM sessions WHERE id = ?",
            (locator.get("cindy_session_id"),),
        ).fetchone()
        return row is not None and row[0] == native_id
    if operation == "remove_agent_switch_from_sdk_session_id":
        row = connection.execute(
            "SELECT content FROM messages WHERE id = ?",
            (locator.get("message_id"),),
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            return False
        try:
            payload = json.loads(row[0])
        except (json.JSONDecodeError, UnicodeError):
            return True
        return (
            isinstance(payload, Mapping)
            and payload.get("fromSdkSessionId") == native_id
        )
    raise FrontendReferenceError("Unsupported frontend verification evidence")


def _validate_schema(
    connection: sqlite3.Connection,
    table: str,
    expected_fingerprint: str,
    *,
    required: set[str],
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    schema = table_schema(connection, table)
    columns = tuple(str(value["name"]) for value in schema)
    if not schema or not required <= set(columns):
        raise FrontendReferenceError(
            f"Frontend table {table!r} is outside the supported schema"
        )
    if schema_fingerprint(schema) != expected_fingerprint:
        raise FrontendReferenceError(
            f"Frontend table {table!r} schema changed after authorization"
        )
    return schema, columns


def _ensure_no_triggers(
    connection: sqlite3.Connection,
    table: str,
) -> None:
    triggers = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
        (table,),
    ).fetchall()
    if triggers:
        raise FrontendReferenceError(
            f"Frontend table {table!r} has unsupported mutation triggers"
        )


def _select_aionui_row(
    connection: sqlite3.Connection,
    locator: Mapping[str, Any],
    schema: Sequence[Mapping[str, Any]],
) -> sqlite3.Row:
    where, parameters = _aionui_where(locator)
    rows = connection.execute(
        f"SELECT * FROM acp_session WHERE {where}",
        parameters,
    ).fetchall()
    if len(rows) != 1:
        raise FrontendReferenceError(
            "AionUI locator does not identify exactly one current row"
        )
    if locator.get("kind") == "primary_key":
        pk_columns = tuple(
            str(value["name"])
            for value in sorted(schema, key=lambda value: int(value["pk"]))
            if int(value["pk"]) > 0
        )
        if tuple(locator.get("columns") or ()) != pk_columns:
            raise FrontendReferenceError(
                "AionUI primary-key locator no longer matches the schema"
            )
    return rows[0]


def _aionui_where(
    locator: Mapping[str, Any],
) -> tuple[str, tuple[Any, ...]]:
    kind = locator.get("kind")
    if kind == "rowid" and isinstance(locator.get("rowid"), int):
        return "rowid = ?", (int(locator["rowid"]),)
    if kind == "primary_key":
        columns = locator.get("columns")
        values = locator.get("values")
        if (
            not isinstance(columns, list)
            or not columns
            or not isinstance(values, list)
            or len(columns) != len(values)
            or any(not isinstance(column, str) or not column for column in columns)
        ):
            raise FrontendReferenceError("Invalid AionUI primary-key locator")
        decoded = tuple(_decode_sqlite_value(value) for value in values)
        return (
            " AND ".join(
                f"{quote_identifier(column)} IS ?" for column in columns
            ),
            decoded,
        )
    raise FrontendReferenceError(
        "AionUI row lacks a stable primary-key or rowid locator"
    )


def _select_unique(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    value: Any,
) -> sqlite3.Row:
    rows = connection.execute(
        f"SELECT * FROM {quote_identifier(table)} "
        f"WHERE {quote_identifier(column)} = ?",
        (value,),
    ).fetchall()
    if len(rows) != 1:
        raise FrontendReferenceError(
            f"{table}.{column} does not identify exactly one row"
        )
    return rows[0]


def _reject_duplicate_mutations(
    mutations: Sequence[_PreparedMutation],
) -> None:
    identities = [
        (mutation.table, json.dumps(mutation.locator, sort_keys=True))
        for mutation in mutations
    ]
    if len(identities) != len(set(identities)):
        raise FrontendReferenceError(
            "The authorized batch contains duplicate physical row mutations"
        )


def remove_top_level_json_field(
    text: str,
    field: str,
    *,
    expected_value: Any,
) -> str:
    """Remove one top-level JSON member while preserving all other bytes."""

    decoder = json.JSONDecoder()
    length = len(text)

    def whitespace(index: int) -> int:
        while index < length and text[index] in " \t\r\n":
            index += 1
        return index

    index = whitespace(0)
    if index >= length or text[index] != "{":
        raise FrontendReferenceError("agent_switch content is not a JSON object")
    index += 1
    members: list[dict[str, Any]] = []
    previous_comma: int | None = None
    while True:
        leading_start = index
        index = whitespace(index)
        if index < length and text[index] == "}":
            object_end = index
            break
        if index >= length or text[index] != '"':
            raise FrontendReferenceError("agent_switch has invalid JSON members")
        key, key_end = decoder.raw_decode(text, index)
        if not isinstance(key, str):
            raise FrontendReferenceError("agent_switch JSON key is not text")
        index = whitespace(key_end)
        if index >= length or text[index] != ":":
            raise FrontendReferenceError("agent_switch JSON member has no colon")
        value_start = whitespace(index + 1)
        try:
            value, value_end = decoder.raw_decode(text, value_start)
        except json.JSONDecodeError as exc:
            raise FrontendReferenceError(
                "agent_switch JSON member value is malformed"
            ) from exc
        index = whitespace(value_end)
        comma = index if index < length and text[index] == "," else None
        members.append(
            {
                "key": key,
                "value": value,
                "leading_start": leading_start,
                "value_end": value_end,
                "comma": comma,
                "previous_comma": previous_comma,
            }
        )
        if comma is None:
            index = whitespace(index)
            if index >= length or text[index] != "}":
                raise FrontendReferenceError(
                    "agent_switch JSON members are not comma-separated"
                )
            object_end = index
            break
        previous_comma = comma
        index = comma + 1

    if whitespace(object_end + 1) != length:
        raise FrontendReferenceError("agent_switch content has trailing data")
    keys = [member["key"] for member in members]
    if len(keys) != len(set(keys)):
        raise FrontendReferenceError(
            "agent_switch content has duplicate JSON keys"
        )
    matches = [member for member in members if member["key"] == field]
    if len(matches) != 1 or matches[0]["value"] != expected_value:
        raise FrontendReferenceError(
            f"agent_switch.{field} no longer matches the approved value"
        )
    target = matches[0]
    if target["comma"] is not None:
        start = int(target["leading_start"])
        end = int(target["comma"]) + 1
    elif target["previous_comma"] is not None:
        start = int(target["previous_comma"])
        end = int(target["value_end"])
    else:
        start = int(target["leading_start"])
        end = int(target["value_end"])
    cleaned = text[:start] + text[end:]
    before = json.loads(text)
    after = json.loads(cleaned)
    expected = dict(before)
    expected.pop(field)
    if after != expected:
        raise FrontendReferenceError(
            "agent_switch cleanup would alter another JSON field"
        )
    return cleaned


def _validate_database_file(database: Path) -> None:
    try:
        state = database.lstat()
    except OSError as exc:
        raise FrontendReferenceError(
            f"Could not inspect frontend database {database}: {exc}"
        ) from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise FrontendReferenceError(
            "Frontend database is not an ordinary file"
        )


def _require_clients_closed(
    codex_home: Path,
    client_inspector: ClientInspector,
) -> None:
    clients = client_inspector(codex_home)
    if clients:
        raise FrontendReferenceError(
            "Related frontend clients are still running: "
            + ", ".join(clients)
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
        raise FrontendReferenceError(
            f"Could not discard successful temporary backup: {exc}"
        ) from exc


def _decode_sqlite_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float)):
        return value
    if (
        isinstance(value, Mapping)
        and value.get("type") == "blob"
        and isinstance(value.get("base64"), str)
    ):
        import base64

        return base64.b64decode(value["base64"], validate=True)
    raise FrontendReferenceError("Unsupported SQLite locator value")


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FrontendReferenceError("Frontend evidence object is malformed")
    return value


__all__ = [
    "FrontendReferenceCleanupResult",
    "FrontendReferenceError",
    "execute_frontend_reference_cleanup",
    "remove_top_level_json_field",
    "verify_frontend_reference_evidence",
]
