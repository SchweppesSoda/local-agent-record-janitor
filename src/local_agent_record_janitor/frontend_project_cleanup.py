"""Batch deletion of orphan AionUI conversation/project rows.

The module deliberately has no dependency on the AionUI adapter.  A caller
must first freeze exact row evidence (database, schema fingerprint, row
fingerprint, and the expected zero-reference assertion) and then pass that
evidence here.  This keeps discovery and mutation separate and makes the
mutation suitable for a single coordinator child batch.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sqlite_identity import (
    quote_identifier,
    row_fingerprint,
    schema_fingerprint,
    table_schema,
)
from .sqlite_utils import connect_readonly


_CONVERSATIONS_TABLE = "conversations"
_ACP_SESSION_TABLE = "acp_session"
_ACP_REFERENCE_ALIAS = "__larj_acp_session_refs"


class FrontendProjectCleanupError(RuntimeError):
    """A project-row batch could not be completed safely.

    ``outcome_known_rolled_back`` is true only after the original rows have
    been checked again.  ``outcome_unknown`` is reserved for a commit/restore
    boundary that cannot be established; callers must never retry such a
    batch automatically.
    """

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


class FrontendProjectGuardError(FrontendProjectCleanupError):
    """The frozen project-row evidence no longer matches the store."""


@dataclass(frozen=True)
class AionUIProjectRowEvidence:
    """Immutable evidence for one orphan ``conversations`` row.

    The row fingerprint covers every column in ``conversations``.  The
    schema fingerprint covers the same table schema.  A project row is
    deletable only when ``expected_zero_acp_session_refs`` is true and the
    current set query proves that assertion for every selected ID.
    """

    database: Path
    conversation_id: str
    schema_fingerprint: str
    row_fingerprint: str
    expected_zero_acp_session_refs: bool = True
    table: str = _CONVERSATIONS_TABLE

    def __post_init__(self) -> None:
        database = Path(self.database).expanduser().absolute()
        conversation_id = str(self.conversation_id).strip()
        schema_hash = str(self.schema_fingerprint).strip()
        row_hash = str(self.row_fingerprint).strip()
        table = str(self.table).strip()
        if not conversation_id:
            raise ValueError("conversation_id must not be blank")
        if not schema_hash:
            raise ValueError("schema_fingerprint must not be blank")
        if not row_hash:
            raise ValueError("row_fingerprint must not be blank")
        if table != _CONVERSATIONS_TABLE:
            raise ValueError(
                f"only {_CONVERSATIONS_TABLE!r} project rows are supported"
            )
        if not isinstance(self.expected_zero_acp_session_refs, bool):
            raise ValueError(
                "expected_zero_acp_session_refs must be a boolean assertion"
            )
        object.__setattr__(self, "database", database)
        object.__setattr__(self, "conversation_id", conversation_id)
        object.__setattr__(self, "schema_fingerprint", schema_hash)
        object.__setattr__(self, "row_fingerprint", row_hash)
        object.__setattr__(self, "table", table)

    @property
    def id(self) -> str:
        """A stable alias used by project-item callers."""

        return self.conversation_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database),
            "table": self.table,
            "id": self.conversation_id,
            "conversation_id": self.conversation_id,
            "schema_fingerprint": self.schema_fingerprint,
            "row_fingerprint": self.row_fingerprint,
            "expected_zero_acp_session_refs": (
                self.expected_zero_acp_session_refs
            ),
            "expected_acp_session_refs": 0,
        }


# These names keep the evidence type discoverable from either the frontend or
# AionUI vocabulary without adding a second representation.
FrontendProjectRowEvidence = AionUIProjectRowEvidence
FrontendProjectDeleteEvidence = AionUIProjectRowEvidence


@dataclass(frozen=True)
class FrontendProjectGuardResult:
    """Result of the one pre-mutation collection guard query."""

    database: Path
    table: str
    checked_row_count: int
    checked_ids: tuple[str, ...]
    collection_query_count: int
    schema_fingerprint: str
    expected_zero_acp_session_refs: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "guarded",
            "database": str(self.database),
            "table": self.table,
            "checked_row_count": self.checked_row_count,
            "checked_ids": list(self.checked_ids),
            "collection_query_count": self.collection_query_count,
            "schema_fingerprint": self.schema_fingerprint,
            "expected_zero_acp_session_refs": (
                self.expected_zero_acp_session_refs
            ),
        }


@dataclass(frozen=True)
class FrontendProjectCleanupResult:
    """Known-success result for one physical AionUI database batch."""

    database: Path
    table: str
    deleted_row_count: int
    affected_rows: int
    deleted_ids: tuple[str, ...]
    guard_query_count: int = 1
    verify_query_count: int = 1
    transaction_count: int = 1
    transaction_committed: bool = True
    rollback_performed: bool = False
    verification_remaining_ids: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return "deleted"

    @property
    def removed_project_count(self) -> int:
        return self.deleted_row_count

    @property
    def removed_conversation_count(self) -> int:
        return self.deleted_row_count

    @property
    def deleted_aionui_rows(self) -> int:
        return self.deleted_row_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "database": str(self.database),
            "table": self.table,
            "deleted_row_count": self.deleted_row_count,
            "removed_project_count": self.removed_project_count,
            "removed_conversation_count": self.removed_conversation_count,
            "deleted_aionui_rows": self.deleted_aionui_rows,
            "affected_rows": self.affected_rows,
            "deleted_ids": list(self.deleted_ids),
            "guard_query_count": self.guard_query_count,
            "verify_query_count": self.verify_query_count,
            "transaction_count": self.transaction_count,
            "transaction": {
                "committed": self.transaction_committed,
                "rollback_performed": self.rollback_performed,
            },
            "verification": {
                "remaining_ids": list(self.verification_remaining_ids),
                "verified": not self.verification_remaining_ids,
            },
        }


AionUIProjectCleanupResult = FrontendProjectCleanupResult


@dataclass(frozen=True)
class FrontendProjectVerificationResult:
    """One set-query verification result."""

    database: Path
    remaining_ids: tuple[str, ...]
    query_count: int = 1

    @property
    def verified(self) -> bool:
        return not self.remaining_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database),
            "remaining_ids": list(self.remaining_ids),
            "query_count": self.query_count,
            "verified": self.verified,
        }


def guard_aionui_project_rows(
    evidence_items: (
        Sequence[AionUIProjectRowEvidence | Mapping[str, Any]]
        | Mapping[str, Any]
    ),
    *,
    query_observer: Callable[[str], None] | None = None,
) -> FrontendProjectGuardResult:
    """Validate all frozen project rows with one collection query."""

    evidence = _normalize_evidence(evidence_items)
    database = _single_database(evidence)
    _validate_database_file(database)
    try:
        with closing(connect_readonly(database)) as connection:
            connection.row_factory = sqlite3.Row
            columns, current_schema_hash = _validate_schema(connection, evidence)
            _assert_schema_hash(evidence, current_schema_hash)
            _guard_rows(
                connection,
                evidence,
                columns,
                query_observer=query_observer,
            )
    except FrontendProjectGuardError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise FrontendProjectGuardError(
            f"Could not guard AionUI project rows in {database}: "
            f"{str(exc) or repr(exc)}"
        ) from exc
    return FrontendProjectGuardResult(
        database=database,
        table=_CONVERSATIONS_TABLE,
        checked_row_count=len(evidence),
        checked_ids=tuple(item.conversation_id for item in evidence),
        collection_query_count=1,
        schema_fingerprint=current_schema_hash,
    )


def verify_aionui_project_rows(
    evidence_items: (
        Sequence[AionUIProjectRowEvidence | Mapping[str, Any]]
        | Mapping[str, Any]
    ),
    *,
    query_observer: Callable[[str], None] | None = None,
) -> FrontendProjectVerificationResult:
    """Verify that every approved project row is absent with one set query."""

    evidence = _normalize_evidence(evidence_items)
    database = _single_database(evidence)
    _validate_database_file(database)
    try:
        with closing(connect_readonly(database)) as connection:
            connection.row_factory = sqlite3.Row
            columns, current_schema_hash = _validate_schema(connection, evidence)
            _assert_schema_hash(evidence, current_schema_hash)
            rows = _select_rows(
                connection,
                evidence,
                columns,
                query_observer=query_observer,
                phase="verify",
            )
            remaining = tuple(
                sorted({str(row["id"]) for row in rows})
            )
    except FrontendProjectCleanupError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise FrontendProjectCleanupError(
            f"Could not verify AionUI project rows in {database}: "
            f"{str(exc) or repr(exc)}"
        ) from exc
    return FrontendProjectVerificationResult(
        database=database,
        remaining_ids=remaining,
        query_count=1,
    )


def execute_aionui_project_cleanup(
    evidence_items: (
        Sequence[AionUIProjectRowEvidence | Mapping[str, Any]]
        | Mapping[str, Any]
    ),
    *,
    phase_callback: Callable[[str], None] | None = None,
    query_observer: Callable[[str], None] | None = None,
) -> FrontendProjectCleanupResult:
    """Delete one frozen set of orphan AionUI project rows.

    The normal path performs one schema/row/reference guard query, one SQL
    transaction containing one set ``DELETE``, and one set verification query.
    Action cardinality affects placeholder count only; it never adds a query
    or transaction.  A temporary SQLite backup is retained whenever the
    commit/restore boundary is ambiguous.
    """

    evidence = _normalize_evidence(evidence_items)
    database = _single_database(evidence)
    _validate_database_file(database)
    backup_directory, backup_path = _create_backup(database)

    connection: sqlite3.Connection | None = None
    transaction_open = False
    mutation_started = False
    commit_attempted = False
    committed = False
    rollback_succeeded = False
    guard_result: FrontendProjectGuardResult | None = None
    affected_rows = 0
    failure: Exception | None = None
    try:
        try:
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            transaction_open = True
            columns, current_schema_hash = _validate_schema(connection, evidence)
            _assert_schema_hash(evidence, current_schema_hash)
            _guard_rows(
                connection,
                evidence,
                columns,
                query_observer=query_observer,
            )
            guard_result = FrontendProjectGuardResult(
                database=database,
                table=_CONVERSATIONS_TABLE,
                checked_row_count=len(evidence),
                checked_ids=tuple(item.conversation_id for item in evidence),
                collection_query_count=1,
                schema_fingerprint=current_schema_hash,
            )

            # The callback is the caller's durable mutation_started marker. It
            # is intentionally called only after all set guards have passed.
            if phase_callback is not None:
                phase_callback("mutation_started")
            mutation_started = True
            placeholders = ", ".join("?" for _ in evidence)
            delete_sql = (
                f"DELETE FROM {quote_identifier(_CONVERSATIONS_TABLE)} "
                f"WHERE {quote_identifier('id')} IN ({placeholders})"
            )
            cursor = connection.execute(
                delete_sql,
                tuple(item.conversation_id for item in evidence),
            )
            affected_rows = int(cursor.rowcount)
            if affected_rows != len(evidence):
                raise FrontendProjectCleanupError(
                    "AionUI project delete affected an unexpected number of "
                    f"rows: expected {len(evidence)}, got {affected_rows}",
                    mutation_started=True,
                )
            commit_attempted = True
            connection.commit()
            transaction_open = False
            committed = True
        except Exception as exc:
            failure = exc
            if transaction_open:
                try:
                    connection.rollback()  # type: ignore[union-attr]
                    rollback_succeeded = True
                except sqlite3.Error:
                    rollback_succeeded = False
                transaction_open = False
        finally:
            if connection is not None:
                connection.close()
    except Exception as exc:
        failure = failure or exc

    if failure is not None:
        if not mutation_started:
            # A guard/preflight failure happened before the irreversible
            # request. The temporary backup is not needed.
            try:
                _discard_backup(backup_path, backup_directory)
            except Exception as cleanup_exc:
                raise FrontendProjectCleanupError(
                    f"AionUI project guard failed and temporary rollback copy "
                    f"cleanup also failed: {cleanup_exc}"
                ) from failure
            if isinstance(failure, FrontendProjectCleanupError):
                raise failure
            raise FrontendProjectCleanupError(
                f"Could not delete AionUI project rows in {database}: "
                f"{str(failure) or repr(failure)}"
            ) from failure

        # A commit exception is intrinsically ambiguous, even if rollback
        # happens to return successfully. Keep the backup for explicit
        # recovery and prohibit an automatic retry.
        if commit_attempted or committed:
            raise FrontendProjectCleanupError(
                f"AionUI project deletion outcome is unknown: "
                f"{str(failure) or repr(failure)}; temporary backup retained "
                f"at {backup_path}",
                outcome_unknown=True,
                mutation_started=True,
            ) from failure

        # SQL failed after the durable marker but before commit. A successful
        # rollback plus an exact evidence check is a known unchanged outcome.
        original_verified = False
        if rollback_succeeded:
            try:
                original_verified = _original_rows_match(evidence)
            except Exception:
                original_verified = False
        if not original_verified:
            try:
                _restore_backup(database, backup_path)
                original_verified = _original_rows_match(evidence)
            except Exception as restore_exc:
                raise FrontendProjectCleanupError(
                    f"AionUI project deletion outcome is unknown: "
                    f"{str(failure) or repr(failure)}; restore failed: "
                    f"{str(restore_exc) or repr(restore_exc)}; temporary "
                    f"backup retained at {backup_path}",
                    outcome_unknown=True,
                    mutation_started=True,
                ) from failure
        if original_verified:
            try:
                _discard_backup(backup_path, backup_directory)
            except Exception as cleanup_exc:
                raise FrontendProjectCleanupError(
                    f"AionUI project rollback was verified, but temporary "
                    f"backup cleanup failed: {cleanup_exc}",
                    outcome_unknown=True,
                    mutation_started=True,
                ) from failure
            raise FrontendProjectCleanupError(
                f"AionUI project deletion was rolled back and verified: "
                f"{str(failure) or repr(failure)}",
                outcome_known_rolled_back=True,
                mutation_started=True,
            ) from failure
        raise FrontendProjectCleanupError(
            f"AionUI project deletion outcome is unknown: "
            f"{str(failure) or repr(failure)}; temporary backup retained at "
            f"{backup_path}",
            outcome_unknown=True,
            mutation_started=True,
        ) from failure

    try:
        verification = verify_aionui_project_rows(
            evidence,
            query_observer=query_observer,
        )
    except Exception as exc:
        # A successful commit followed by an unreadable/ambiguous verification
        # is never retried.  Keep the rollback copy for explicit recovery.
        raise FrontendProjectCleanupError(
            f"AionUI project deletion verification is unknown: "
            f"{str(exc) or repr(exc)}; temporary backup retained at "
            f"{backup_path}",
            outcome_unknown=True,
            mutation_started=True,
        ) from exc
    if not verification.verified:
        raise FrontendProjectCleanupError(
            "AionUI project deletion left approved rows behind: "
            + ", ".join(verification.remaining_ids)
            + f"; temporary backup retained at {backup_path}",
            outcome_unknown=True,
            mutation_started=True,
        )

    if phase_callback is not None:
        try:
            phase_callback("verified")
        except Exception as exc:
            raise FrontendProjectCleanupError(
                f"AionUI project deletion was verified but its completion "
                f"marker failed: {str(exc) or repr(exc)}; temporary backup "
                f"retained at {backup_path}",
                outcome_unknown=True,
                mutation_started=True,
            ) from exc
    try:
        _discard_backup(backup_path, backup_directory)
    except Exception as exc:
        raise FrontendProjectCleanupError(
            f"AionUI project deletion was verified but temporary backup "
            f"cleanup failed: {str(exc) or repr(exc)}",
            outcome_unknown=True,
            mutation_started=True,
        ) from exc
    return FrontendProjectCleanupResult(
        database=database,
        table=_CONVERSATIONS_TABLE,
        deleted_row_count=len(evidence),
        affected_rows=affected_rows,
        deleted_ids=tuple(item.conversation_id for item in evidence),
        guard_query_count=guard_result.collection_query_count if guard_result else 1,
        verify_query_count=verification.query_count,
        transaction_count=1,
        transaction_committed=True,
        rollback_performed=False,
        verification_remaining_ids=verification.remaining_ids,
    )


def execute_frontend_project_cleanup(
    evidence_items: (
        Sequence[AionUIProjectRowEvidence | Mapping[str, Any]]
        | Mapping[str, Any]
    ),
    *,
    phase_callback: Callable[[str], None] | None = None,
    query_observer: Callable[[str], None] | None = None,
) -> FrontendProjectCleanupResult:
    """Generic frontend-named alias for the AionUI project-row writer."""

    return execute_aionui_project_cleanup(
        evidence_items,
        phase_callback=phase_callback,
        query_observer=query_observer,
    )


guard_frontend_project_rows = guard_aionui_project_rows
verify_frontend_project_rows = verify_aionui_project_rows


def _normalize_evidence(
    raw_items: (
        Sequence[AionUIProjectRowEvidence | Mapping[str, Any]]
        | Mapping[str, Any]
    ),
) -> tuple[AionUIProjectRowEvidence, ...]:
    if isinstance(raw_items, AionUIProjectRowEvidence):
        items: Iterable[Any] = (raw_items,)
    elif isinstance(raw_items, Mapping):
        # A single evidence mapping has an evidence marker.  A mapping keyed
        # by IDs is also accepted for coordinator callers that group rows by
        # project; its values must still carry complete evidence.
        if any(
            key in raw_items
            for key in (
                "database",
                "conversation_id",
                "id",
                "row_fingerprint",
            )
        ):
            items = (raw_items,)
        else:
            flattened: list[Any] = []
            for value in raw_items.values():
                if isinstance(value, Mapping):
                    flattened.append(value)
                else:
                    try:
                        flattened.extend(value)
                    except TypeError as exc:
                        raise FrontendProjectCleanupError(
                            "project evidence mapping values must be evidence "
                            "mappings or iterables"
                        ) from exc
            items = flattened
    else:
        try:
            items = tuple(raw_items)
        except TypeError as exc:
            raise FrontendProjectCleanupError(
                "project evidence must be a sequence or mapping"
            ) from exc

    normalized: list[AionUIProjectRowEvidence] = []
    seen: set[tuple[str, str]] = set()
    for raw in items:
        if isinstance(raw, AionUIProjectRowEvidence):
            evidence = raw
        elif isinstance(raw, Mapping):
            evidence = _evidence_from_mapping(raw)
        else:
            raise FrontendProjectCleanupError(
                "project evidence must contain mappings or "
                "AionUIProjectRowEvidence values"
            )
        key = (_path_key(evidence.database), evidence.conversation_id)
        if key in seen:
            raise FrontendProjectCleanupError(
                "project evidence contains duplicate conversation IDs"
            )
        seen.add(key)
        normalized.append(evidence)
    if not normalized:
        raise FrontendProjectCleanupError(
            "No exact AionUI project-row evidence was authorized"
        )
    return tuple(
        sorted(normalized, key=lambda item: (_path_key(item.database), item.id))
    )


def _evidence_from_mapping(raw: Mapping[str, Any]) -> AionUIProjectRowEvidence:
    database = raw.get("database") or raw.get("db") or raw.get("path")
    conversation_id = (
        raw.get("conversation_id")
        or raw.get("row_id")
        or raw.get("id")
        or raw.get("project_id")
    )
    schema_hash = raw.get("schema_fingerprint") or raw.get("schema_hash")
    row_hash = raw.get("row_fingerprint") or raw.get("row_hash")
    if database is None or conversation_id is None:
        raise FrontendProjectCleanupError(
            "project evidence requires database and conversations.id"
        )
    if schema_hash is None or row_hash is None:
        raise FrontendProjectCleanupError(
            "project evidence requires schema_fingerprint and row_fingerprint"
        )
    expected = _expected_zero_refs(raw)
    return AionUIProjectRowEvidence(
        database=Path(str(database)),
        conversation_id=str(conversation_id),
        schema_fingerprint=str(schema_hash),
        row_fingerprint=str(row_hash),
        expected_zero_acp_session_refs=expected,
        table=str(raw.get("table") or _CONVERSATIONS_TABLE),
    )


def _expected_zero_refs(raw: Mapping[str, Any]) -> bool:
    if "expected_zero_acp_session_refs" in raw:
        value = raw["expected_zero_acp_session_refs"]
        if isinstance(value, bool):
            return value
        raise FrontendProjectCleanupError(
            "expected_zero_acp_session_refs must be boolean"
        )
    for key in ("expected_acp_session_refs", "session_reference_count"):
        if key in raw:
            value = raw[key]
            try:
                return int(value) == 0
            except (TypeError, ValueError) as exc:
                raise FrontendProjectCleanupError(
                    f"{key} must be an integer"
                ) from exc
    raise FrontendProjectCleanupError(
        "project evidence must assert expected zero acp_session references"
    )


def _single_database(evidence: Sequence[AionUIProjectRowEvidence]) -> Path:
    paths = {_path_key(item.database): item.database for item in evidence}
    if len(paths) != 1:
        raise FrontendProjectCleanupError(
            "one frontend project batch must target one physical database"
        )
    return next(iter(paths.values()))


def _validate_database_file(database: Path) -> None:
    try:
        state = database.lstat()
    except OSError as exc:
        raise FrontendProjectCleanupError(
            f"Could not inspect frontend project database {database}: {exc}"
        ) from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise FrontendProjectCleanupError(
            "Frontend project database is not an ordinary file"
        )


def _validate_schema(
    connection: sqlite3.Connection,
    evidence: Sequence[AionUIProjectRowEvidence],
) -> tuple[tuple[str, ...], str]:
    conversation_schema = table_schema(connection, _CONVERSATIONS_TABLE)
    if not conversation_schema:
        raise FrontendProjectGuardError(
            f"required table {_CONVERSATIONS_TABLE!r} is missing"
        )
    columns = tuple(str(item["name"]) for item in conversation_schema)
    if "id" not in columns:
        raise FrontendProjectGuardError(
            f"required column 'id' is missing from {_CONVERSATIONS_TABLE!r}"
        )
    acp_schema = table_schema(connection, _ACP_SESSION_TABLE)
    if not acp_schema:
        raise FrontendProjectGuardError(
            f"required table {_ACP_SESSION_TABLE!r} is missing; "
            "zero session references cannot be proved"
        )
    acp_columns = {str(item["name"]) for item in acp_schema}
    if "conversation_id" not in acp_columns:
        raise FrontendProjectGuardError(
            f"required column 'conversation_id' is missing from "
            f"{_ACP_SESSION_TABLE!r}"
        )
    del evidence
    return columns, schema_fingerprint(conversation_schema)


def _assert_schema_hash(
    evidence: Sequence[AionUIProjectRowEvidence],
    current_schema_hash: str,
) -> None:
    expected = {item.schema_fingerprint for item in evidence}
    if expected != {current_schema_hash}:
        raise FrontendProjectGuardError(
            "AionUI conversations schema fingerprint changed after approval"
        )


def _guard_rows(
    connection: sqlite3.Connection,
    evidence: Sequence[AionUIProjectRowEvidence],
    columns: Sequence[str],
    *,
    query_observer: Callable[[str], None] | None,
) -> None:
    rows = _select_rows(
        connection,
        evidence,
        columns,
        query_observer=query_observer,
        phase="guard",
    )
    expected_ids = {item.conversation_id for item in evidence}
    actual_by_id: dict[str, sqlite3.Row] = {}
    for row in rows:
        row_id = str(row["id"])
        if row_id in actual_by_id:
            raise FrontendProjectGuardError(
                f"AionUI conversations.id is not unique for {row_id!r}"
            )
        actual_by_id[row_id] = row
    if set(actual_by_id) != expected_ids or len(rows) != len(evidence):
        missing = sorted(expected_ids - set(actual_by_id))
        raise FrontendProjectGuardError(
            "AionUI project row set changed; missing IDs: "
            + (", ".join(missing) if missing else "none")
        )
    for item in evidence:
        row = actual_by_id[item.conversation_id]
        current_fingerprint = row_fingerprint(row, columns)
        if current_fingerprint != item.row_fingerprint:
            raise FrontendProjectGuardError(
                f"AionUI project row fingerprint changed for "
                f"{item.conversation_id!r}"
            )
        references = int(row[_ACP_REFERENCE_ALIAS] or 0)
        if not item.expected_zero_acp_session_refs or references != 0:
            raise FrontendProjectGuardError(
                f"AionUI project row {item.conversation_id!r} has "
                f"{references} acp_session references; expected zero"
            )


def _select_rows(
    connection: sqlite3.Connection,
    evidence: Sequence[AionUIProjectRowEvidence],
    columns: Sequence[str],
    *,
    query_observer: Callable[[str], None] | None,
    phase: str,
) -> tuple[sqlite3.Row, ...]:
    placeholders = ", ".join("?" for _ in evidence)
    selected_columns = ", ".join(
        f"c.{quote_identifier(column)}" for column in columns
    )
    query = (
        f"SELECT {selected_columns}, "
        f"(SELECT COUNT(*) FROM {quote_identifier(_ACP_SESSION_TABLE)} AS a "
        f"WHERE a.{quote_identifier('conversation_id')} = "
        f"c.{quote_identifier('id')}) AS {quote_identifier(_ACP_REFERENCE_ALIAS)} "
        f"FROM {quote_identifier(_CONVERSATIONS_TABLE)} AS c "
        f"WHERE c.{quote_identifier('id')} IN ({placeholders})"
    )
    if query_observer is not None:
        query_observer(query)
    try:
        return tuple(
            connection.execute(
                query,
                tuple(item.conversation_id for item in evidence),
            ).fetchall()
        )
    except sqlite3.Error as exc:
        raise FrontendProjectGuardError(
            f"Could not execute AionUI project {phase} set query: {exc}"
        ) from exc


def _create_backup(database: Path) -> tuple[Path, Path]:
    try:
        directory = Path(
            tempfile.mkdtemp(prefix=".larj-project-", dir=database.parent)
        )
        backup = directory / "database.sqlite"
        with (
            closing(sqlite3.connect(database)) as source,
            closing(sqlite3.connect(backup)) as destination,
        ):
            source.backup(destination)
            destination.commit()
        return directory, backup
    except (OSError, sqlite3.Error) as exc:
        try:
            if "backup" in locals() and backup.exists():
                backup.unlink()
            if "directory" in locals() and directory.exists():
                directory.rmdir()
        except OSError:
            pass
        raise FrontendProjectCleanupError(
            f"Could not create temporary AionUI project rollback copy: "
            f"{str(exc) or repr(exc)}"
        ) from exc


def _restore_backup(database: Path, backup: Path) -> None:
    if not backup.is_file():
        raise FrontendProjectCleanupError("temporary rollback copy is missing")
    with (
        closing(sqlite3.connect(backup)) as source,
        closing(sqlite3.connect(database)) as destination,
    ):
        source.backup(destination)
        destination.commit()


def _original_rows_match(
    evidence: Sequence[AionUIProjectRowEvidence],
) -> bool:
    database = _single_database(evidence)
    _validate_database_file(database)
    try:
        with closing(connect_readonly(database)) as connection:
            connection.row_factory = sqlite3.Row
            columns, current_schema_hash = _validate_schema(connection, evidence)
            _assert_schema_hash(evidence, current_schema_hash)
            _guard_rows(
                connection,
                evidence,
                columns,
                query_observer=None,
            )
        return True
    except (FrontendProjectCleanupError, OSError, sqlite3.Error, ValueError, TypeError):
        return False


def _discard_backup(backup: Path, directory: Path) -> None:
    try:
        backup.unlink(missing_ok=True)
        directory.rmdir()
    except OSError as exc:
        raise FrontendProjectCleanupError(
            f"Could not discard temporary AionUI project rollback copy: {exc}"
        ) from exc


def _path_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().absolute()))


__all__ = [
    "AionUIProjectCleanupResult",
    "AionUIProjectRowEvidence",
    "FrontendProjectCleanupError",
    "FrontendProjectCleanupResult",
    "FrontendProjectDeleteEvidence",
    "FrontendProjectGuardError",
    "FrontendProjectGuardResult",
    "FrontendProjectRowEvidence",
    "FrontendProjectVerificationResult",
    "execute_aionui_project_cleanup",
    "execute_frontend_project_cleanup",
    "guard_aionui_project_rows",
    "guard_frontend_project_rows",
    "verify_aionui_project_rows",
    "verify_frontend_project_rows",
]
