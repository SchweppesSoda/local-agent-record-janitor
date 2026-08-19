from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .codex_state import parse_thread_source
from .conversation_metadata import read_conversation_summaries
from .models import ConversationSummary, RolloutRecord
from .path_identity import canonical_existing_path_key
from .sqlite_utils import connect_readonly, table_exists


@dataclass(frozen=True)
class FrontendSessionRecord:
    """Immutable, read-only snapshot of one frontend session row."""

    platform: str
    platform_session_id: str
    thread_id: str | None
    database: Path
    codex_home: Path
    backend: str | None = None
    status: str | None = None
    updated_at_ms: int | None = None
    title: str | None = None
    is_live: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    codex_bin_hint: Path | None = None

    def approval_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "platform": self.platform,
            "database": _normalized_path(self.database),
            "platform_session_id": self.platform_session_id,
            "thread_id": self.thread_id,
            "backend": self.backend,
            "status": self.status,
            "updated_at_ms": self.updated_at_ms,
            "is_live": self.is_live,
        }
        if self.platform == "cindy":
            payload.update(
                {
                    "reference_kind": self.details.get("reference_kind", "current"),
                    "boundary_id": self.details.get("boundary_id"),
                    "boundary_created_at_ms": self.details.get(
                        "boundary_created_at_ms"
                    ),
                    "boundary_rewind_at_ms": self.details.get(
                        "boundary_rewind_at_ms"
                    ),
                    "cindy_profile_root": self.details.get("cindy_profile_root"),
                }
            )
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.approval_payload(),
            "title": self.title,
            "codex_home": str(self.codex_home),
            "codex_bin_hint": (
                str(self.codex_bin_hint) if self.codex_bin_hint is not None else None
            ),
            "details": _json_value(dict(self.details)),
        }


@dataclass(frozen=True)
class InventoryFailure:
    """A visible source failure; deletion for its home must fail closed."""

    source: str
    codex_home: Path
    message: str
    database: Path | None = None
    error_type: str = "InventoryError"
    blocks_delete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "codex_home": str(self.codex_home),
            "database": str(self.database) if self.database is not None else None,
            "error_type": self.error_type,
            "message": self.message,
            "blocks_delete": self.blocks_delete,
        }


@dataclass(frozen=True)
class ManagedConversation:
    """One storage-qualified Codex thread and all evidence about it."""

    codex_home: Path
    thread_id: str
    summary: ConversationSummary
    rollouts: tuple[RolloutRecord, ...] = ()
    frontend_sessions: tuple[FrontendSessionRecord, ...] = ()
    descendant_thread_ids: tuple[str, ...] = ()
    thread_index: Mapping[str, Any] | None = None
    indexed: bool = False
    legacy_indexed: bool = False
    artifact_present: bool = False
    deletable: bool = False
    cascade_unknown: bool = False
    blockers: tuple[str, ...] = ()
    codex_bin_hints: tuple[Path, ...] = ()

    @property
    def action_id(self) -> str:
        payload = {
            "schema": "managed-conversation-action-v1",
            "codex_home": _normalized_path(self.codex_home),
            "thread_id": self.thread_id,
        }
        return "record:v1:" + _canonical_sha256(payload)

    @property
    def descendants(self) -> tuple[str, ...]:
        return self.descendant_thread_ids

    @property
    def index(self) -> Mapping[str, Any] | None:
        return self.thread_index

    @property
    def rollout_records(self) -> tuple[RolloutRecord, ...]:
        return self.rollouts

    @property
    def frontend_references(self) -> tuple[FrontendSessionRecord, ...]:
        return self.frontend_sessions

    @property
    def has_codex_artifacts(self) -> bool:
        return self.artifact_present

    @property
    def desktop_state_present(self) -> bool:
        return any(
            reference.platform.casefold() == "codex-desktop"
            for reference in self.frontend_sessions
        )

    @property
    def delete_supported(self) -> bool:
        return self.deletable

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "codex_home": str(self.codex_home),
            "thread_id": self.thread_id,
            "summary": self.summary.to_dict(),
            "rollouts": [record.to_dict() for record in self.rollouts],
            "frontend_sessions": [record.to_dict() for record in self.frontend_sessions],
            "descendant_thread_ids": list(self.descendant_thread_ids),
            "thread_index": _json_value(dict(self.thread_index)) if self.thread_index else None,
            "indexed": self.indexed,
            "legacy_indexed": self.legacy_indexed,
            "artifact_present": self.artifact_present,
            "desktop_state_present": self.desktop_state_present,
            "deletable": self.deletable,
            "cascade_unknown": self.cascade_unknown,
            "blockers": list(self.blockers),
            "codex_bin_hints": [str(path) for path in self.codex_bin_hints],
        }


@dataclass(frozen=True)
class SessionCatalog:
    records: tuple[ManagedConversation, ...] = ()
    unmapped_frontend_sessions: tuple[FrontendSessionRecord, ...] = ()
    errors: tuple[InventoryFailure, ...] = ()

    @property
    def conversations(self) -> tuple[ManagedConversation, ...]:
        return self.records

    @property
    def threads(self) -> tuple[ManagedConversation, ...]:
        """Canonical Codex terminology for the compatibility ``records`` field."""

        return self.records

    @property
    def failures(self) -> tuple[InventoryFailure, ...]:
        return self.errors

    @property
    def unmapped_frontend_references(self) -> tuple[FrontendSessionRecord, ...]:
        """Canonical alias for frontend references without a native thread ID."""

        return self.unmapped_frontend_sessions

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "records": [record.to_dict() for record in self.records],
            "unmapped_frontend_sessions": [
                record.to_dict() for record in self.unmapped_frontend_sessions
            ],
            "errors": [error.to_dict() for error in self.errors],
        }


class InventorySelectionError(ValueError):
    pass


def build_session_catalog(adapters: Iterable[object]) -> SessionCatalog:
    """Build a read-only union of native artifacts and frontend mappings.

    A failure remains visible while other homes and sources continue to be
    inventoried.  Every blocking failure is then applied to all records in
    the affected home so no incomplete snapshot can produce a delete action.
    """

    adapter_list = list(adapters)
    home_paths: dict[str, Path] = {}
    bin_hints: dict[str, set[Path]] = defaultdict(set)
    frontend_by_home: dict[str, list[FrontendSessionRecord]] = defaultdict(list)
    errors: list[InventoryFailure] = []

    for adapter in adapter_list:
        raw_home = getattr(adapter, "codex_home", None)
        if not isinstance(raw_home, Path):
            continue
        home = _absolute_path(raw_home)
        home_key = _normalized_path(home)
        home_paths.setdefault(home_key, home)
        hint = getattr(adapter, "codex_bin_hint", None)
        if isinstance(hint, Path):
            bin_hints[home_key].add(_absolute_path(hint))
        try:
            sessions = list(adapter.list_sessions())
        except Exception as exc:  # each source failure must not hide other homes
            errors.append(
                InventoryFailure(
                    source=f"frontend:{getattr(adapter, 'name', type(adapter).__name__)}",
                    codex_home=home,
                    database=getattr(adapter, "database", None),
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            )
            continue
        for session in sessions:
            if not isinstance(session, FrontendSessionRecord):
                errors.append(
                    InventoryFailure(
                        source=f"frontend:{getattr(adapter, 'name', type(adapter).__name__)}",
                        codex_home=home,
                        database=getattr(adapter, "database", None),
                        error_type="InventoryTypeError",
                        message="list_sessions returned an unsupported record type",
                    )
                )
                continue
            session_home = _absolute_path(session.codex_home)
            session_key = _normalized_path(session_home)
            home_paths.setdefault(session_key, session_home)
            frontend_by_home[session_key].append(
                replace(session, codex_home=session_home, database=_absolute_path(session.database))
            )

    all_records: list[ManagedConversation] = []
    unmapped: list[FrontendSessionRecord] = []
    for home_key, home in sorted(home_paths.items()):
        state_rows, native_edges, state_edge_complete, state_errors = _read_state_partial(home)
        rollouts, rollout_errors = _read_rollouts_partial(home)
        legacy_ids, legacy_names, legacy_errors = _read_legacy_index_partial(home)
        native_errors = state_errors + rollout_errors + legacy_errors
        home_errors = [
            failure
            for failure in errors
            if _normalized_path(failure.codex_home) == home_key
        ] + native_errors
        errors.extend(native_errors)

        rollouts_by_thread: dict[str, list[RolloutRecord]] = defaultdict(list)
        for rollout in rollouts:
            rollouts_by_thread[rollout.thread_id].append(rollout)

        accepted_frontend: list[FrontendSessionRecord] = []
        for session in _deduplicate_frontend(frontend_by_home.get(home_key, [])):
            thread_id = session.thread_id
            if not _valid_thread_id(thread_id):
                # Only an explicitly identified Codex backend can classify an
                # unassigned AionUI row as a Codex record.
                if session.platform != "aionui" or session.backend == "codex":
                    unmapped.append(session)
                continue
            if session.platform == "aionui" and session.backend is None:
                originators = {
                    _normalized_string(record.originator)
                    for record in rollouts_by_thread.get(thread_id, ())
                }
                if "aionui-session" not in originators:
                    continue
            accepted_frontend.append(session)

        frontend_by_thread: dict[str, list[FrontendSessionRecord]] = defaultdict(list)
        for session in accepted_frontend:
            assert session.thread_id is not None
            frontend_by_thread[session.thread_id].append(session)

        graph: dict[str, set[str]] = defaultdict(set)
        for parent, child in native_edges:
            graph[parent].add(child)
        for rollout in rollouts:
            for parent in parse_thread_source(
                rollout.source,
                source_label="session_meta.source",
            ).parent_thread_ids:
                graph[parent].add(rollout.thread_id)

        ids = set(state_rows) | set(rollouts_by_thread) | set(legacy_ids) | set(frontend_by_thread)
        for parent, children in graph.items():
            ids.add(parent)
            ids.update(children)

        try:
            summaries = read_conversation_summaries(
                home,
                ids,
                rollout_records_by_thread=rollouts_by_thread,
                legacy_names=legacy_names,
                strict=True,
            )
        except Exception as exc:
            failure = InventoryFailure(
                source="codex-metadata",
                codex_home=home,
                database=home / "state_5.sqlite",
                error_type=type(exc).__name__,
                message=str(exc),
            )
            errors.append(failure)
            home_errors.append(failure)
            summaries = read_conversation_summaries(
                home,
                ids,
                rollout_records_by_thread=rollouts_by_thread,
                legacy_names=legacy_names,
                strict=False,
            )

        cascade_unknown = not state_edge_complete or bool(rollout_errors)
        blocking_messages = tuple(
            sorted(
                {
                    f"{failure.source}: {failure.message}"
                    for failure in home_errors
                    if failure.blocks_delete
                }
            )
        )
        hints = tuple(sorted(bin_hints.get(home_key, ()), key=_normalized_path))
        for thread_id in sorted(ids):
            records = tuple(
                sorted(
                    rollouts_by_thread.get(thread_id, ()),
                    key=lambda item: (item.archived, _normalized_path(item.path)),
                )
            )
            references = tuple(
                sorted(
                    frontend_by_thread.get(thread_id, ()),
                    key=lambda item: (
                        item.platform,
                        _normalized_path(item.database),
                        item.platform_session_id,
                    ),
                )
            )
            indexed = thread_id in state_rows
            artifact_present = indexed or bool(records)
            descendants = tuple(sorted(_transitive_descendants(graph, thread_id)))
            blockers = list(blocking_messages)
            live_references = [
                reference for reference in references if reference.is_live
            ]
            if live_references:
                platforms = ", ".join(
                    sorted(
                        {
                            reference.platform.casefold()
                            for reference in live_references
                        }
                    )
                )
                blockers.append(
                    "A live frontend reference prevents native-record "
                    f"deletion ({platforms})"
                )
            live_cindy = [
                reference
                for reference in live_references
                if reference.platform.casefold() == "cindy"
            ]
            if live_cindy:
                kinds = {
                    str(reference.details.get("reference_kind", "current"))
                    for reference in live_cindy
                }
                label = (
                    "live historical"
                    if kinds == {"agent_switch"}
                    else "live current or historical"
                )
                blockers.append(
                    f"Cindy retains a {label} native-session reference"
                )
            live_descendant_references = {
                descendant_id: tuple(
                    reference
                    for reference in frontend_by_thread.get(descendant_id, ())
                    if reference.is_live
                )
                for descendant_id in descendants
            }
            live_descendant_references = {
                descendant_id: descendant_references
                for descendant_id, descendant_references
                in live_descendant_references.items()
                if descendant_references
            }
            if live_descendant_references:
                affected = ", ".join(
                    f"{descendant_id} ("
                    + "/".join(
                        sorted(
                            {
                                reference.platform.casefold()
                                for reference in descendant_references
                            }
                        )
                    )
                    + ")"
                    for descendant_id, descendant_references
                    in sorted(live_descendant_references.items())
                )
                blockers.append(
                    "Codex thread/delete would cascade into descendant threads "
                    "retained by live frontend references: " + affected
                )
            live_cindy_descendants = {
                descendant_id
                for descendant_id, descendant_references
                in live_descendant_references.items()
                if any(
                    reference.platform.casefold() == "cindy"
                    for reference in descendant_references
                )
            }
            if live_cindy_descendants:
                blockers.append(
                    "Live Cindy current or historical references retain "
                    "descendant threads: "
                    + ", ".join(sorted(live_cindy_descendants))
                )
            if not artifact_present:
                if any(
                    reference.platform.casefold() == "codex-desktop"
                    for reference in references
                ):
                    blockers.append(
                        "Only Codex Desktop host catalog/UI state remains; "
                        "use the explicit clean remove_desktop_state action"
                    )
                else:
                    blockers.append(
                        "No SQLite thread row or verifiable rollout remains"
                    )
            if cascade_unknown:
                blockers.append("Cascade inventory is incomplete")
            summary = summaries[thread_id]
            desktop_title = next(
                (
                    reference.title
                    for reference in references
                    if reference.platform.casefold() == "codex-desktop"
                    and isinstance(reference.title, str)
                    and reference.title
                ),
                None,
            )
            if desktop_title is not None and summary.display_name is None:
                summary = replace(
                    summary,
                    title=desktop_title,
                    display_name=desktop_title,
                    display_name_source=(
                        "codex-desktop.local_thread_catalog"
                    ),
                    metadata_sources=tuple(
                        dict.fromkeys(
                            (
                                *summary.metadata_sources,
                                "codex-desktop.local_thread_catalog",
                            )
                        )
                    ),
                )
            all_records.append(
                ManagedConversation(
                    codex_home=home,
                    thread_id=thread_id,
                    summary=summary,
                    rollouts=records,
                    frontend_sessions=references,
                    descendant_thread_ids=descendants,
                    thread_index=state_rows.get(thread_id),
                    indexed=indexed,
                    legacy_indexed=thread_id in legacy_ids,
                    artifact_present=artifact_present,
                    deletable=artifact_present and not blockers,
                    cascade_unknown=cascade_unknown,
                    blockers=tuple(blockers),
                    codex_bin_hints=hints,
                )
            )

    records = tuple(
        sorted(all_records, key=lambda item: (_normalized_path(item.codex_home), item.thread_id))
    )
    return SessionCatalog(
        records=records,
        unmapped_frontend_sessions=tuple(
            sorted(
                _deduplicate_frontend(unmapped),
                key=lambda item: (
                    _normalized_path(item.codex_home),
                    item.platform,
                    _normalized_path(item.database),
                    item.platform_session_id,
                ),
            )
        ),
        errors=tuple(
            sorted(
                errors,
                key=lambda item: (
                    _normalized_path(item.codex_home),
                    item.source,
                    item.message,
                ),
            )
        ),
    )


def select_managed_conversations(
    catalog: SessionCatalog,
    selectors: Sequence[str],
) -> tuple[ManagedConversation, ...]:
    """Resolve exact action IDs, thread IDs, or unique prefixes."""

    if not selectors:
        return catalog.records
    selected: list[ManagedConversation] = []
    seen: set[str] = set()
    for raw_selector in selectors:
        selector = raw_selector.strip()
        if not selector:
            raise InventorySelectionError("Conversation selector must not be blank")
        exact = [
            record
            for record in catalog.records
            if record.action_id == selector or record.thread_id == selector
        ]
        matches = exact or [
            record
            for record in catalog.records
            if record.action_id.startswith(selector) or record.thread_id.startswith(selector)
        ]
        if not matches:
            raise InventorySelectionError(f"No conversation matches selector {selector!r}")
        if len(matches) > 1:
            raise InventorySelectionError(
                f"Selector {selector!r} is ambiguous across {len(matches)} conversations"
            )
        record = matches[0]
        if record.action_id not in seen:
            seen.add(record.action_id)
            selected.append(record)
    return tuple(selected)


def _read_state_partial(
    home: Path,
) -> tuple[dict[str, dict[str, Any]], set[tuple[str, str]], bool, list[InventoryFailure]]:
    database = home / "state_5.sqlite"
    try:
        database_status = database.stat()
    except FileNotFoundError:
        return {}, set(), True, []
    except OSError as exc:
        return {}, set(), False, [
            InventoryFailure(
                source="codex-state",
                codex_home=home,
                database=database,
                error_type=type(exc).__name__,
                message=f"Could not inspect {database}: {exc}",
            )
        ]
    if not stat.S_ISREG(database_status.st_mode):
        return {}, set(), False, [
            InventoryFailure(
                source="codex-state",
                codex_home=home,
                database=database,
                error_type="InventoryPathError",
                message=f"Codex state database is not a regular file: {database}",
            )
        ]
    rows: dict[str, dict[str, Any]] = {}
    edges: set[tuple[str, str]] = set()
    try:
        with closing(connect_readonly(database)) as connection:
            if not table_exists(connection, "threads"):
                raise ValueError("required table 'threads' is missing")
            columns = _sqlite_columns(connection, "threads")
            missing = {"id", "rollout_path"} - columns
            if missing:
                raise ValueError(
                    "table 'threads' is missing column(s) " + ", ".join(sorted(missing))
                )
            for row in connection.execute("SELECT * FROM threads ORDER BY id"):
                thread_id = row["id"]
                if not _valid_thread_id(thread_id):
                    raise ValueError("threads.id contains a blank or invalid value")
                if thread_id in rows:
                    raise ValueError(f"threads.id contains duplicate value {thread_id!r}")
                rows[thread_id] = dict(row)
            if not table_exists(connection, "thread_spawn_edges"):
                return rows, edges, False, [
                    InventoryFailure(
                        source="codex-cascade",
                        codex_home=home,
                        database=database,
                        error_type="InventorySchemaError",
                        message=(
                            f"Cascade inventory is incomplete because {database} "
                            "has no thread_spawn_edges table"
                        ),
                    )
                ]
            edge_columns = _sqlite_columns(connection, "thread_spawn_edges")
            missing_edges = {"parent_thread_id", "child_thread_id"} - edge_columns
            if missing_edges:
                raise ValueError(
                    "table 'thread_spawn_edges' is missing column(s) "
                    + ", ".join(sorted(missing_edges))
                )
            for row in connection.execute(
                "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
            ):
                parent = row["parent_thread_id"]
                child = row["child_thread_id"]
                if not _valid_thread_id(parent) or not _valid_thread_id(child):
                    raise ValueError("thread_spawn_edges contains a blank or invalid ID")
                edges.add((parent, child))
    except Exception as exc:
        return rows, edges, False, [
            InventoryFailure(
                source="codex-state",
                codex_home=home,
                database=database,
                error_type=type(exc).__name__,
                message=f"Could not completely read {database}: {exc}",
            )
        ]
    return rows, edges, True, []


def _read_rollouts_partial(
    home: Path,
) -> tuple[list[RolloutRecord], list[InventoryFailure]]:
    records: list[RolloutRecord] = []
    errors: list[InventoryFailure] = []
    for directory_name, archived in (("sessions", False), ("archived_sessions", True)):
        root = home / directory_name
        try:
            root_status = root.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(
                InventoryFailure(
                    source="codex-rollouts",
                    codex_home=home,
                    database=root,
                    error_type=type(exc).__name__,
                    message=f"Could not inspect rollout root {root}: {exc}",
                )
            )
            continue
        if not stat.S_ISDIR(root_status.st_mode):
            errors.append(
                InventoryFailure(
                    source="codex-rollouts",
                    codex_home=home,
                    database=root,
                    error_type="InventoryPathError",
                    message=f"Rollout root is not a directory: {root}",
                )
            )
            continue
        paths: list[Path] = []

        def record_walk_error(exc: OSError) -> None:
            failed_path = Path(exc.filename) if exc.filename else root
            errors.append(
                InventoryFailure(
                    source="codex-rollouts",
                    codex_home=home,
                    database=failed_path,
                    error_type=type(exc).__name__,
                    message=f"Could not completely traverse {root}: {exc}",
                )
            )

        try:
            for directory, directory_names, file_names in os.walk(
                root,
                onerror=record_walk_error,
                followlinks=False,
            ):
                directory_names.sort()
                paths.extend(
                    Path(directory) / file_name
                    for file_name in sorted(file_names)
                    if file_name.endswith(".jsonl")
                )
        except OSError as exc:
            record_walk_error(exc)
        for path in sorted(paths, key=_normalized_path):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    first_line = handle.readline()
                raw = json.loads(first_line)
                payload = raw.get("payload") if isinstance(raw, dict) else None
                if not isinstance(raw, dict) or raw.get("type") != "session_meta":
                    raise ValueError("first record is not session_meta")
                if not isinstance(payload, dict):
                    raise ValueError("session_meta payload is not an object")
                thread_id = payload.get("id") or payload.get("session_id")
                if not _valid_thread_id(thread_id):
                    raise ValueError("session_meta has no valid id/session_id")
                records.append(
                    RolloutRecord(
                        thread_id=thread_id,
                        path=path,
                        originator=_display_string(payload.get("originator")),
                        source=payload.get("source"),
                        cwd=_display_string(payload.get("cwd")),
                        timestamp=_display_string(payload.get("timestamp")),
                        archived=archived,
                    )
                )
            except Exception as exc:
                errors.append(
                    InventoryFailure(
                        source="codex-rollouts",
                        codex_home=home,
                        database=path,
                        error_type=type(exc).__name__,
                        message=f"Could not read rollout identity from {path}: {exc}",
                    )
                )
    return records, errors


def _read_legacy_index_partial(
    home: Path,
) -> tuple[set[str], dict[str, tuple[str, ...]], list[InventoryFailure]]:
    path = home / "session_index.jsonl"
    try:
        path_status = path.stat()
    except FileNotFoundError:
        return set(), {}, []
    except OSError as exc:
        return set(), {}, [
            InventoryFailure(
                source="legacy-index",
                codex_home=home,
                database=path,
                error_type=type(exc).__name__,
                message=f"Could not inspect {path}: {exc}",
            )
        ]
    if not stat.S_ISREG(path_status.st_mode):
        return set(), {}, [
            InventoryFailure(
                source="legacy-index",
                codex_home=home,
                database=path,
                error_type="InventoryPathError",
                message=f"Legacy index is not a regular file: {path}",
            )
        ]
    ids: set[str] = set()
    names: dict[str, set[str]] = defaultdict(set)
    errors: list[InventoryFailure] = []
    try:
        raw_lines = path.read_bytes().splitlines()
    except OSError as exc:
        return ids, {}, [
            InventoryFailure(
                source="legacy-index",
                codex_home=home,
                database=path,
                error_type=type(exc).__name__,
                message=f"Could not completely read {path}: {exc}",
            )
        ]
    for line_number, raw_line in enumerate(raw_lines, 1):
        try:
            decoded = raw_line.decode("utf-8-sig" if line_number == 1 else "utf-8")
            raw = json.loads(decoded)
            if not isinstance(raw, dict):
                raise ValueError("record is not an object")
            thread_id = raw.get("id")
            if not _valid_thread_id(thread_id):
                raise ValueError("record has no valid id")
            ids.add(thread_id)
            name = _display_string(raw.get("thread_name"))
            if name is not None:
                names[thread_id].add(name)
        except Exception as exc:
            errors.append(
                InventoryFailure(
                    source="legacy-index",
                    codex_home=home,
                    database=path,
                    error_type=type(exc).__name__,
                    message=f"Could not parse {path} line {line_number}: {exc}",
                )
            )
    return ids, {key: tuple(sorted(value)) for key, value in names.items()}, errors


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})")
        if isinstance(row["name"], str)
    }


def _transitive_descendants(graph: Mapping[str, set[str]], root: str) -> set[str]:
    seen: set[str] = set()
    pending = list(graph.get(root, ()))
    while pending:
        child = pending.pop()
        if child == root or child in seen:
            continue
        seen.add(child)
        pending.extend(graph.get(child, ()))
    return seen


def _deduplicate_frontend(
    sessions: Iterable[FrontendSessionRecord],
) -> list[FrontendSessionRecord]:
    result: list[FrontendSessionRecord] = []
    seen: set[str] = set()
    for session in sessions:
        key = _canonical_sha256(session.approval_payload())
        if key in seen:
            continue
        seen.add(key)
        result.append(session)
    return result


def _valid_thread_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _display_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalized_string(value: object) -> str | None:
    displayed = _display_string(value)
    return displayed.lower() if displayed is not None else None


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    return Path(os.path.normpath(os.path.abspath(os.fspath(expanded))))


def _normalized_path(path: Path) -> str:
    return canonical_existing_path_key(path.expanduser())


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# Canonical Codex vocabulary.  The original names remain the compatibility
# surface for the package and CLI; these aliases do not alter serialization,
# action identity, or approval fingerprints.
CodexThreadRecord = ManagedConversation
CodexThreadCatalog = SessionCatalog
FrontendReferenceRecord = FrontendSessionRecord


build_codex_thread_catalog = build_session_catalog
select_codex_threads = select_managed_conversations


__all__ = [
    "CodexThreadCatalog",
    "CodexThreadRecord",
    "FrontendSessionRecord",
    "FrontendReferenceRecord",
    "InventoryFailure",
    "InventorySelectionError",
    "ManagedConversation",
    "SessionCatalog",
    "build_codex_thread_catalog",
    "build_session_catalog",
    "select_codex_threads",
    "select_managed_conversations",
]
