from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_desktop_state import ClientInspector
from .sqlite_utils import connect_readonly, table_exists

from .sqlite_identity import (
    quote_identifier,
    row_fingerprint,
    schema_fingerprint,
    table_schema,
    text_sha256,
)


class FrontendReferenceError(RuntimeError):
    """An exact frontend reference batch could not be safely applied."""

    def __init__(
        self,
        message: str,
        *,
        outcome_known_rolled_back: bool = False,
        outcome_unknown: bool = False,
        mutation_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.outcome_known_rolled_back = bool(outcome_known_rolled_back)
        self.outcome_unknown = bool(outcome_unknown)
        self.mutation_started = bool(mutation_started)


class FrontendReferenceGuardError(FrontendReferenceError):
    """The frozen frontend reference collection no longer matches."""


@dataclass(frozen=True)
class FrontendReferenceGuardResult:
    """Read-only collection guard result for one physical frontend store."""

    database: Path
    platform: str
    checked_reference_count: int
    checked_native_ids: tuple[str, ...]
    collection_query_count: int
    snapshot_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "guarded",
            "database": str(self.database),
            "platform": self.platform,
            "checked_reference_count": self.checked_reference_count,
            "checked_native_ids": list(self.checked_native_ids),
            "collection_query_count": self.collection_query_count,
            "snapshot_fingerprint": self.snapshot_fingerprint,
        }

@dataclass(frozen=True)
class FrontendReferenceCleanupResult:
    database: Path
    platform: str
    removed_reference_count: int
    deleted_aionui_rows: int = 0
    cleared_cindy_current_references: int = 0
    cleaned_cindy_historical_references: int = 0
    reference_results: tuple[Mapping[str, Any], ...] = ()
    transaction_committed: bool = True
    rollback_performed: bool = False
    verification_results: tuple[Mapping[str, Any], ...] = ()

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
            "transaction": {
                "committed": self.transaction_committed,
                "rollback_performed": self.rollback_performed,
            },
            "references": [dict(item) for item in self.reference_results],
            "verification": {
                "remaining_approved_references": 0,
                "per_reference": [
                    dict(item) for item in self.verification_results
                ],
            },
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
    phase_callback: Callable[[str], None] | None = None,
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
    mutation_started = False
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
                prepared = _prepare_mutations(connection, evidence)
                _reject_duplicate_mutations(prepared)
                # All exact-row guards have passed. The first journal mutation
                # marker belongs immediately before the first SQL write, so a
                # guard mismatch never becomes an ``unknown`` operation.
                if phase_callback is not None:
                    phase_callback("mutation_started")
                mutation_started = True
                changes_before = connection.total_changes
                for mutation in prepared:
                    _apply_mutation(connection, mutation)
                changed = connection.total_changes - changes_before
                if changed != len(prepared):
                    raise FrontendReferenceError(
                        "Frontend transaction affected an unexpected number "
                        f"of rows: expected {len(prepared)}, got {changed}"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        with closing(sqlite3.connect(database)) as verification_connection:
            verification_connection.row_factory = sqlite3.Row
            _verify_mutations(verification_connection, prepared)
        if phase_callback is not None:
            phase_callback("verified")
    except Exception as exc:
        try:
            restore_error = _restore_backup(database, backup_path)
        except Exception as restore_exc:
            restore_error = str(restore_exc) or repr(restore_exc)
        if restore_error is not None:
            raise FrontendReferenceError(
                f"Frontend cleanup outcome is unknown: {exc}; rollback also failed "
                f"({restore_error}). Temporary backup: {backup_path}",
                outcome_unknown=True,
                mutation_started=mutation_started,
            ) from exc
        try:
            _discard_backup(backup_path, backup_directory)
        except FrontendReferenceError as discard_exc:
            discard_exc.mutation_started = bool(mutation_started)
            discard_exc.outcome_known_rolled_back = bool(mutation_started)
            raise
        if isinstance(exc, FrontendReferenceError):
            exc.mutation_started = bool(
                getattr(exc, "mutation_started", False) or mutation_started
            )
            if mutation_started and not getattr(exc, "outcome_unknown", False):
                exc.outcome_known_rolled_back = True
            raise
        raise FrontendReferenceError(
            str(exc) or repr(exc),
            outcome_known_rolled_back=mutation_started,
            mutation_started=mutation_started,
        ) from exc

    _discard_backup(backup_path, backup_directory)
    operations = [mutation.operation for mutation in prepared]
    reference_results = tuple(
        {
            "id": _reference_id(mutation),
            "operation": mutation.operation,
            "affected_rows": 1,
            "verified": True,
        }
        for mutation in prepared
    )
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
        reference_results=reference_results,
        verification_results=reference_results,
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


def guard_frontend_reference_closure(
    evidence_by_native: Mapping[str, Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]],
    *,
    query_observer: Callable[[str], None] | None = None,
) -> FrontendReferenceGuardResult:
    """Guard one frozen frontend reference collection without writing.

    ``evidence_by_native`` may be a mapping from native ID to exact evidence,
    or the already-flattened evidence sequence.  The guard performs one set
    query per supported collection (AionUI rows; Cindy sessions and, when
    present, history messages), compares physical row locators/fingerprints,
    and rejects any newly-live reference before a mutation marker can be sent.
    """

    evidence = _flatten_guard_evidence(evidence_by_native)
    if not evidence:
        raise FrontendReferenceGuardError(
            "No exact frontend reference evidence was authorized"
        )
    databases = {
        str(item.get("database") or "") for item in evidence
    }
    platforms = {
        str(item.get("platform") or "").casefold() for item in evidence
    }
    if len(databases) != 1 or "" in databases:
        raise FrontendReferenceGuardError(
            "A frontend closure guard must target one physical database"
        )
    if platforms not in ({"aionui"}, {"cindy"}):
        raise FrontendReferenceGuardError(
            "A frontend closure guard must target one supported platform"
        )
    database = Path(next(iter(databases))).expanduser().absolute()
    platform = next(iter(platforms))
    _validate_database_file(database)
    try:
        with closing(connect_readonly(database)) as connection:
            connection.row_factory = sqlite3.Row
            prepared = _prepare_mutations(connection, evidence)
            if platform == "aionui":
                query_count = _guard_aionui_collection(
                    connection,
                    evidence,
                    query_observer=query_observer,
                )
            else:
                query_count = _guard_cindy_collection(
                    connection,
                    evidence,
                    query_observer=query_observer,
                )
    except FrontendReferenceGuardError:
        raise
    except FrontendReferenceError as exc:
        raise FrontendReferenceGuardError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise FrontendReferenceGuardError(
            f"Could not read the frontend closure store: {exc}"
        ) from exc

    native_ids = tuple(
        sorted(
            {
                str(
                    _mapping(item.get("expected")).get(
                        "session_id"
                        if platform == "aionui"
                        else "native_session_id"
                    )
                )
                for item in evidence
            }
        )
    )
    snapshot_payload = {
        "schema_version": 1,
        "database": str(database),
        "platform": platform,
        "evidence": [dict(item) for item in sorted(evidence, key=lambda item: json.dumps(_guard_json(item), sort_keys=True))],
    }
    encoded = json.dumps(
        _guard_json(snapshot_payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return FrontendReferenceGuardResult(
        database=database,
        platform=platform,
        checked_reference_count=len(prepared),
        checked_native_ids=native_ids,
        collection_query_count=query_count,
        snapshot_fingerprint="v1:" + hashlib.sha256(encoded).hexdigest(),
    )


def _flatten_guard_evidence(
    evidence_by_native: Mapping[str, Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    if isinstance(evidence_by_native, Mapping):
        flattened: list[Mapping[str, Any]] = []
        for native_id, raw_items in evidence_by_native.items():
            if not isinstance(native_id, str) or not native_id:
                raise FrontendReferenceGuardError(
                    "Frontend closure native IDs must be non-empty strings"
                )
            if isinstance(raw_items, Mapping):
                items: Sequence[Mapping[str, Any]] = (raw_items,)
            else:
                try:
                    items = tuple(raw_items)
                except TypeError as exc:
                    raise FrontendReferenceGuardError(
                        "Frontend closure evidence values must be iterable"
                    ) from exc
            for item in items:
                if not isinstance(item, Mapping):
                    raise FrontendReferenceGuardError(
                        "Frontend closure evidence must contain mappings"
                    )
                evidence = dict(item)
                expected = evidence.get("expected")
                if not isinstance(expected, Mapping):
                    raise FrontendReferenceGuardError(
                        "Frontend closure evidence has no expected identity"
                    )
                key = (
                    expected.get("session_id")
                    if str(evidence.get("platform") or "").casefold() == "aionui"
                    else expected.get("native_session_id")
                )
                if key != native_id:
                    raise FrontendReferenceGuardError(
                        "Frontend closure evidence is bound to the wrong native ID"
                    )
                flattened.append(evidence)
        return tuple(flattened)
    try:
        items = tuple(evidence_by_native)
    except TypeError as exc:
        raise FrontendReferenceGuardError(
            "Frontend closure evidence must be iterable"
        ) from exc
    if not all(isinstance(item, Mapping) for item in items):
        raise FrontendReferenceGuardError(
            "Frontend closure evidence must contain mappings"
        )
    return tuple(dict(item) for item in items)


def _guard_aionui_collection(
    connection: sqlite3.Connection,
    evidence: Sequence[Mapping[str, Any]],
    *,
    query_observer: Callable[[str], None] | None,
) -> int:
    schema = table_schema(connection, "acp_session")
    columns = tuple(str(item["name"]) for item in schema)
    if schema_fingerprint(schema) != str(evidence[0].get("schema_fingerprint") or ""):
        raise FrontendReferenceGuardError(
            "AionUI acp_session schema changed after authorization"
        )
    if not table_exists(connection, "conversations"):
        raise FrontendReferenceGuardError(
            "AionUI live-reference collection cannot prove conversations ownership"
        )
    primary_key = tuple(
        str(item["name"])
        for item in sorted(schema, key=lambda value: int(value["pk"]))
        if int(item["pk"]) > 0
    )
    has_rowid = _guard_table_has_rowid(connection, "acp_session")
    if not primary_key and not has_rowid:
        raise FrontendReferenceGuardError(
            "AionUI acp_session has no stable physical row identity"
        )
    native_ids = sorted(
        {
            str(_mapping(item.get("expected")).get("session_id"))
            for item in evidence
        }
    )
    if any(not value or value == "None" for value in native_ids):
        raise FrontendReferenceGuardError(
            "AionUI closure evidence has no native session ID"
        )
    placeholders = ", ".join("?" for _ in native_ids)
    projection = (
        'a.rowid AS "__larj_guard_rowid", a.*' if has_rowid else "a.*"
    )
    if query_observer is not None:
        query_observer("aionui:live-reference-collection")
    rows = connection.execute(
        "SELECT "
        + projection
        + " FROM acp_session AS a"
        + " INNER JOIN conversations AS c ON c.id = a.conversation_id"
        + " WHERE a.session_id IN ("
        + placeholders
        + ")",
        tuple(native_ids),
    ).fetchall()
    approved_tokens = {
        _aionui_guard_token(item, primary_key)
        for item in evidence
    }
    current_tokens = {
        _aionui_row_guard_token(row, columns, primary_key, has_rowid)
        for row in rows
    }
    if current_tokens != approved_tokens:
        raise FrontendReferenceGuardError(
            "AionUI live-reference collection changed after authorization"
        )
    return 1


def _aionui_guard_token(
    evidence: Mapping[str, Any],
    primary_key: tuple[str, ...],
) -> tuple[Any, ...]:
    locator = _mapping(evidence.get("locator"))
    row_fingerprint_value = evidence.get("row_fingerprint")
    if not isinstance(row_fingerprint_value, str) or not row_fingerprint_value:
        raise FrontendReferenceGuardError(
            "AionUI closure evidence has no row fingerprint"
        )
    if locator.get("kind") == "rowid":
        rowid = locator.get("rowid")
        if not isinstance(rowid, int) or isinstance(rowid, bool):
            raise FrontendReferenceGuardError("AionUI closure rowid is invalid")
        return ("rowid", int(rowid), row_fingerprint_value)
    if locator.get("kind") != "primary_key":
        raise FrontendReferenceGuardError(
            "AionUI closure evidence has no supported row locator"
        )
    columns = tuple(locator.get("columns") or ())
    values = locator.get("values")
    if columns != primary_key or not isinstance(values, list) or len(values) != len(columns):
        raise FrontendReferenceGuardError(
            "AionUI closure primary-key locator changed"
        )
    return (
        "primary_key",
        tuple(_decode_sqlite_value(value) for value in values),
        row_fingerprint_value,
    )


def _aionui_row_guard_token(
    row: sqlite3.Row,
    columns: tuple[str, ...],
    primary_key: tuple[str, ...],
    has_rowid: bool,
) -> tuple[Any, ...]:
    fingerprint = row_fingerprint(row, columns)
    if primary_key:
        return ("primary_key", tuple(row[column] for column in primary_key), fingerprint)
    if not has_rowid:
        raise FrontendReferenceGuardError(
            "AionUI closure row has no stable physical identity"
        )
    value = row["__larj_guard_rowid"]
    if not isinstance(value, int):
        raise FrontendReferenceGuardError("AionUI closure rowid is invalid")
    return ("rowid", int(value), fingerprint)


def _guard_cindy_collection(
    connection: sqlite3.Connection,
    evidence: Sequence[Mapping[str, Any]],
    *,
    query_observer: Callable[[str], None] | None,
) -> int:
    session_schema = table_schema(connection, "sessions")
    session_columns = tuple(str(item["name"]) for item in session_schema)
    session_hash = schema_fingerprint(session_schema)
    if session_hash != str(evidence[0].get("session_schema_fingerprint") or ""):
        raise FrontendReferenceGuardError(
            "Cindy sessions schema changed after authorization"
        )
    native_ids = sorted(
        {
            str(_mapping(item.get("expected")).get("native_session_id"))
            for item in evidence
        }
    )
    if any(not value or value == "None" for value in native_ids):
        raise FrontendReferenceGuardError(
            "Cindy closure evidence has no native session ID"
        )
    placeholders = ", ".join("?" for _ in native_ids)
    if query_observer is not None:
        query_observer("cindy:live-current-collection")
    current_rows = connection.execute(
        "SELECT * FROM sessions WHERE sdk_session_id IN ("
        + placeholders
        + ") AND (status IS NULL OR lower(status) <> 'deleted')",
        tuple(native_ids),
    ).fetchall()
    approved_current = {
        (
            str(_mapping(item.get("locator")).get("cindy_session_id")),
            str(item.get("session_row_fingerprint") or ""),
        )
        for item in evidence
        if item.get("operation") == "clear_session_sdk_session_id"
    }
    current_tokens = {
        (str(row["id"]), row_fingerprint(row, session_columns))
        for row in current_rows
    }
    if current_tokens != approved_current:
        raise FrontendReferenceGuardError(
            "Cindy live current-reference collection changed after authorization"
        )

    query_count = 1
    if not table_exists(connection, "messages"):
        if any(
            item.get("operation") == "remove_agent_switch_from_sdk_session_id"
            for item in evidence
        ):
            raise FrontendReferenceGuardError(
                "Cindy historical closure evidence requires a messages table"
            )
        return query_count

    message_schema = table_schema(connection, "messages")
    message_columns = tuple(str(item["name"]) for item in message_schema)
    required = {"id", "session_id", "role", "content", "created_at", "rewind_at"}
    if not required <= set(message_columns):
        raise FrontendReferenceGuardError(
            "Cindy messages schema is outside the supported closure contract"
        )
    approved_message_hashes = {
        str(item.get("message_schema_fingerprint") or "")
        for item in evidence
        if item.get("operation") == "remove_agent_switch_from_sdk_session_id"
    }
    if approved_message_hashes and schema_fingerprint(message_schema) not in approved_message_hashes:
        raise FrontendReferenceGuardError(
            "Cindy messages schema changed after authorization"
        )
    if query_observer is not None:
        query_observer("cindy:live-history-collection")
    message_rows = connection.execute(
        "SELECT m.* FROM messages AS m INNER JOIN sessions AS s ON s.id = m.session_id "
        "WHERE m.role = 'agent_switch' "
        "AND (s.status IS NULL OR lower(s.status) <> 'deleted')"
    ).fetchall()
    approved_history = {
        (
            str(_mapping(item.get("locator")).get("message_id")),
            str(item.get("message_row_fingerprint") or ""),
            str(item.get("message_content_sha256") or ""),
        )
        for item in evidence
        if item.get("operation") == "remove_agent_switch_from_sdk_session_id"
    }
    current_history: set[tuple[str, str, str]] = set()
    wanted = set(native_ids)
    for row in message_rows:
        content = row["content"]
        if not isinstance(content, str):
            raise FrontendReferenceGuardError(
                "Cindy agent_switch content is not text"
            )
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise FrontendReferenceGuardError(
                "Cindy agent_switch content is malformed JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise FrontendReferenceGuardError(
                "Cindy agent_switch content is not a JSON object"
            )
        if payload.get("fromSdkSessionId") in wanted:
            current_history.add(
                (
                    str(row["id"]),
                    row_fingerprint(row, message_columns),
                    text_sha256(content),
                )
            )
    if current_history != approved_history:
        raise FrontendReferenceGuardError(
            "Cindy live historical-reference collection changed after authorization"
        )
    return query_count + 1


def _guard_table_has_rowid(
    connection: sqlite3.Connection,
    table: str,
) -> bool:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or not isinstance(row["sql"], str):
        raise FrontendReferenceGuardError(
            f"Frontend table {table!r} has no stable schema"
        )
    return "WITHOUT ROWID" not in row["sql"].upper()


def _guard_json(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        import base64

        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Mapping):
        return {str(key): _guard_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_guard_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)

def _prepare_mutations(
    connection: sqlite3.Connection,
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[_PreparedMutation, ...]:
    """Guard every approved row with set queries before any write."""

    if not evidence:
        raise FrontendReferenceError("No frontend evidence was authorized")
    platforms = {str(item.get("platform") or "") for item in evidence}
    if len(platforms) != 1:
        raise FrontendReferenceError(
            "A frontend transaction cannot mix frontend platforms"
        )
    platform = next(iter(platforms))
    if platform == "aionui":
        return _prepare_aionui_batch(connection, evidence)
    if platform == "cindy":
        return _prepare_cindy_batch(connection, evidence)
    raise FrontendReferenceError("Unsupported frontend reference platform")


def _prepare_aionui_batch(
    connection: sqlite3.Connection,
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[_PreparedMutation, ...]:
    first = evidence[0]
    schema, columns = _validate_schema(
        connection,
        "acp_session",
        str(first.get("schema_fingerprint") or ""),
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
    schema_hash = schema_fingerprint(schema)
    primary_key = tuple(
        str(value["name"])
        for value in sorted(schema, key=lambda value: int(value["pk"]))
        if int(value["pk"]) > 0
    )
    locators: list[Mapping[str, Any]] = []
    locator_keys: set[str] = set()
    where_parts: list[str] = []
    parameters: list[Any] = []
    for item in evidence:
        if str(item.get("schema_fingerprint") or "") != schema_hash:
            raise FrontendReferenceError(
                "AionUI evidence rows do not share one schema snapshot"
            )
        locator = _mapping(item.get("locator"))
        kind = locator.get("kind")
        if kind == "primary_key":
            if tuple(locator.get("columns") or ()) != primary_key:
                raise FrontendReferenceError(
                    "AionUI primary-key locator no longer matches the schema"
                )
        elif kind == "rowid":
            if not isinstance(locator.get("rowid"), int):
                raise FrontendReferenceError("Invalid AionUI rowid locator")
        else:
            raise FrontendReferenceError(
                "AionUI row lacks a stable primary-key or rowid locator"
            )
        locator_key = json.dumps(dict(locator), sort_keys=True, separators=(",", ":"))
        if locator_key in locator_keys:
            raise FrontendReferenceError(
                "The authorized batch contains duplicate physical row mutations"
            )
        locator_keys.add(locator_key)
        where, values = _aionui_where(locator)
        where_parts.append("(" + where + ")")
        parameters.extend(values)
        locators.append(locator)
    projection = "rowid AS \"__larj_rowid\", *" if any(
        locator.get("kind") == "rowid" for locator in locators
    ) else "*"
    try:
        rows = connection.execute(
            "SELECT " + projection + " FROM acp_session WHERE "
            + " OR ".join(where_parts),
            tuple(parameters),
        ).fetchall()
    except sqlite3.Error as exc:
        raise FrontendReferenceError(
            "AionUI exact row guard query failed"
        ) from exc
    if len(rows) != len(locators):
        raise FrontendReferenceError(
            "AionUI exact row guard found a missing or duplicate row"
        )
    by_rowid = {
        int(row["__larj_rowid"]): row
        for row in rows
        if "__larj_rowid" in row.keys()
    }
    by_primary_key = {
        tuple(row[column] for column in primary_key): row
        for row in rows
    }
    prepared: list[_PreparedMutation] = []
    for item, locator in zip(evidence, locators):
        row = (
            by_rowid.get(int(locator["rowid"]))
            if locator.get("kind") == "rowid"
            else by_primary_key.get(
                tuple(
                    _decode_sqlite_value(value)
                    for value in (locator.get("values") or ())
                )
            )
        )
        if row is None:
            raise FrontendReferenceError(
                "AionUI locator does not identify exactly one current row"
            )
        if row_fingerprint(row, columns) != item.get("row_fingerprint"):
            raise FrontendReferenceError(
                "AionUI acp_session row changed after authorization"
            )
        expected = _mapping(item.get("expected"))
        if (
            row["conversation_id"] != expected.get("conversation_id")
            or row["session_id"] != expected.get("session_id")
        ):
            raise FrontendReferenceError(
                "AionUI row identity no longer matches the approved mapping"
            )
        prepared.append(
            _PreparedMutation(
                platform="aionui",
                operation="delete_row",
                table="acp_session",
                locator=locator,
                original_row={column: row[column] for column in columns},
                columns=columns,
            )
        )
    return tuple(prepared)


def _prepare_cindy_batch(
    connection: sqlite3.Connection,
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[_PreparedMutation, ...]:
    first = evidence[0]
    session_schema, session_columns = _validate_schema(
        connection,
        "sessions",
        str(first.get("session_schema_fingerprint") or ""),
        required={"id", "sdk_session_id", "status", "agent_kind"},
    )
    _ensure_no_triggers(connection, "sessions")
    session_hash = schema_fingerprint(session_schema)
    session_ids: list[str] = []
    for item in evidence:
        value = _mapping(item.get("locator")).get("cindy_session_id")
        if not isinstance(value, str) or not value:
            raise FrontendReferenceError("Cindy evidence has no session ID")
        if str(item.get("session_schema_fingerprint") or "") != session_hash:
            raise FrontendReferenceError(
                "Cindy evidence rows do not share one session schema snapshot"
            )
        session_ids.append(value)
    if len(session_ids) != len(set(session_ids)):
        raise FrontendReferenceError(
            "The authorized batch contains duplicate Cindy session guards"
        )
    session_rows = _select_id_rows(connection, "sessions", session_ids)
    if len(session_rows) != len(session_ids):
        raise FrontendReferenceError(
            "Cindy session guard found a missing or duplicate row"
        )
    sessions = {str(row["id"]): row for row in session_rows}
    message_items = [
        item
        for item in evidence
        if item.get("operation") == "remove_agent_switch_from_sdk_session_id"
    ]
    messages: dict[str, sqlite3.Row] = {}
    message_columns: tuple[str, ...] = ()
    if message_items:
        message_schema, message_columns = _validate_schema(
            connection,
            "messages",
            str(message_items[0].get("message_schema_fingerprint") or ""),
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
        message_hash = schema_fingerprint(message_schema)
        message_ids: list[str] = []
        for item in message_items:
            value = _mapping(item.get("locator")).get("message_id")
            if not isinstance(value, str) or not value:
                raise FrontendReferenceError(
                    "Cindy history evidence has no message ID"
                )
            if str(item.get("message_schema_fingerprint") or "") != message_hash:
                raise FrontendReferenceError(
                    "Cindy history rows do not share one message schema snapshot"
                )
            message_ids.append(value)
        if len(message_ids) != len(set(message_ids)):
            raise FrontendReferenceError(
                "The authorized batch contains duplicate Cindy message guards"
            )
        message_rows = _select_id_rows(connection, "messages", message_ids)
        if len(message_rows) != len(message_ids):
            raise FrontendReferenceError(
                "Cindy message guard found a missing or duplicate row"
            )
        messages = {str(row["id"]): row for row in message_rows}
    prepared: list[_PreparedMutation] = []
    for item in evidence:
        operation = str(item.get("operation") or "")
        locator = _mapping(item.get("locator"))
        session_id = str(locator["cindy_session_id"])
        session_row = sessions[session_id]
        if row_fingerprint(session_row, session_columns) != item.get(
            "session_row_fingerprint"
        ):
            raise FrontendReferenceError(
                "Cindy session row changed after authorization"
            )
        expected = _mapping(item.get("expected"))
        native_id = expected.get("native_session_id")
        if not isinstance(native_id, str) or not native_id:
            raise FrontendReferenceError("Cindy evidence has no native session ID")
        if operation == "clear_session_sdk_session_id":
            if session_row["sdk_session_id"] != native_id:
                raise FrontendReferenceError(
                    "Cindy current sdk_session_id no longer matches"
                )
            prepared.append(
                _PreparedMutation(
                    platform="cindy",
                    operation=operation,
                    table="sessions",
                    locator=locator,
                    original_row={
                        column: session_row[column] for column in session_columns
                    },
                    columns=session_columns,
                )
            )
            continue
        message_id = locator.get("message_id")
        if not isinstance(message_id, str) or message_id not in messages:
            raise FrontendReferenceError(
                "Cindy history message row is missing"
            )
        message_row = messages[message_id]
        content = message_row["content"]
        if not isinstance(content, str):
            raise FrontendReferenceError(
                "Cindy agent_switch content is not text"
            )
        if (
            row_fingerprint(message_row, message_columns)
            != item.get("message_row_fingerprint")
            or text_sha256(content) != item.get("message_content_sha256")
        ):
            raise FrontendReferenceError(
                "Cindy agent_switch message changed after authorization"
            )
        replacement = remove_top_level_json_field(
            content,
            "fromSdkSessionId",
            expected_value=native_id,
        )
        prepared.append(
            _PreparedMutation(
                platform="cindy",
                operation=operation,
                table="messages",
                locator=locator,
                original_row={column: message_row[column] for column in message_columns},
                columns=message_columns,
                replacement_content=replacement,
            )
        )
    return tuple(prepared)


def _select_id_rows(
    connection: sqlite3.Connection,
    table: str,
    values: Sequence[str],
) -> list[sqlite3.Row]:
    placeholders = ", ".join("?" for _ in values)
    try:
        return list(
            connection.execute(
                f"SELECT * FROM {quote_identifier(table)} "
                f"WHERE {quote_identifier('id')} IN ({placeholders})",
                tuple(values),
            ).fetchall()
        )
    except sqlite3.Error as exc:
        raise FrontendReferenceError(
            f"Could not guard exact rows in {table}"
        ) from exc


def _verify_mutations(
    connection: sqlite3.Connection,
    mutations: Sequence[_PreparedMutation],
) -> None:
    if not mutations:
        return
    platforms = {mutation.platform for mutation in mutations}
    if platforms == {"aionui"}:
        where_parts: list[str] = []
        parameters: list[Any] = []
        for mutation in mutations:
            where, values = _aionui_where(mutation.locator)
            where_parts.append("(" + where + ")")
            parameters.extend(values)
        rows = connection.execute(
            "SELECT 1 FROM acp_session WHERE " + " OR ".join(where_parts),
            tuple(parameters),
        ).fetchall()
        if rows:
            raise FrontendReferenceError(
                "AionUI approved rows remain after deletion"
            )
        return
    if platforms != {"cindy"}:
        raise FrontendReferenceError("Unsupported frontend verification platform")
    current = [
        mutation
        for mutation in mutations
        if mutation.operation == "clear_session_sdk_session_id"
    ]
    if current:
        ids = [str(mutation.locator["cindy_session_id"]) for mutation in current]
        rows = _select_id_rows(connection, "sessions", ids)
        by_id = {str(row["id"]): row for row in rows}
        if len(by_id) != len(ids) or any(
            by_id[value]["sdk_session_id"] is not None for value in ids
        ):
            raise FrontendReferenceError(
                "Cindy current references remain after cleanup"
            )
    historical = [
        mutation
        for mutation in mutations
        if mutation.operation == "remove_agent_switch_from_sdk_session_id"
    ]
    if historical:
        ids = [str(mutation.locator["message_id"]) for mutation in historical]
        rows = _select_id_rows(connection, "messages", ids)
        by_id = {str(row["id"]): row for row in rows}
        if len(by_id) != len(ids):
            raise FrontendReferenceError(
                "Cindy historical message row disappeared unexpectedly"
            )
        for mutation in historical:
            actual = by_id[str(mutation.locator["message_id"])]
            expected = dict(mutation.original_row)
            expected["content"] = mutation.replacement_content
            if any(actual[column] != expected[column] for column in mutation.columns):
                raise FrontendReferenceError(
                    "Cindy historical cleanup changed an unapproved field"
                )


def _reference_id(mutation: _PreparedMutation) -> str:
    if mutation.platform == "aionui":
        return "aionui:" + str(mutation.original_row.get("conversation_id"))
    if mutation.operation == "clear_session_sdk_session_id":
        return "cindy:current:" + str(mutation.locator.get("cindy_session_id"))
    return "cindy:history:" + str(mutation.locator.get("message_id"))


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
    "FrontendReferenceGuardError",
    "FrontendReferenceGuardResult",
    "FrontendReferenceError",
    "execute_frontend_reference_cleanup",
    "guard_frontend_reference_closure",
    "remove_top_level_json_field",
    "verify_frontend_reference_evidence",
]
