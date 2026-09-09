from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .models import RolloutRecord, ThreadSourceInfo
from .path_identity import canonical_existing_path_key
from .sqlite_utils import connect_readonly, table_exists


class CodexStateReadError(RuntimeError):
    """A Codex state database could not be inspected reliably."""


@dataclass(frozen=True)
class SpawnEdgeRecord:
    """One native database spawn relation."""

    parent_thread_id: str
    child_thread_id: str
    status: str | None


def parse_thread_source(
    value: object,
    *,
    source_label: str = "source",
) -> ThreadSourceInfo:
    """Normalize historical ``source.subagent.thread_spawn`` shapes."""

    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in {"subagent", "guardian", "guardian_review"}:
            return ThreadSourceInfo(
                is_subagent=True,
                metadata_sources=(source_label,),
            )
        try:
            value = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return ThreadSourceInfo()

    if not isinstance(value, dict):
        return ThreadSourceInfo()

    is_subagent = False
    containers: list[dict[str, Any]] = []
    if "subagent" in value:
        is_subagent = True
        subagent = value.get("subagent")
        if isinstance(subagent, dict):
            containers.append(subagent)
    if "thread_spawn" in value:
        is_subagent = True
        containers.append(value)
    if not is_subagent:
        return ThreadSourceInfo()

    parents: set[str] = set()
    nicknames: list[str] = []
    roles: list[str] = []
    paths: list[str] = []
    for container in containers:
        spawn = container.get("thread_spawn")
        field_containers = [container]
        if isinstance(spawn, dict):
            field_containers.append(spawn)
            parent = _source_text(spawn.get("parent_thread_id"))
            if parent is not None:
                parents.add(parent)
        for field_container in field_containers:
            _append_source_text(
                nicknames, field_container.get("agent_nickname")
            )
            _append_source_text(roles, field_container.get("agent_role"))
            _append_source_text(paths, field_container.get("agent_path"))

    nickname, nickname_conflict = _one_source_value(nicknames)
    role, role_conflict = _one_source_value(roles)
    path, path_conflict = _one_source_value(paths)
    conflicts = []
    if nickname_conflict:
        conflicts.append(
            f"{source_label}.agent_nickname has conflicting values: "
            f"{_source_values_json(nicknames)}"
        )
    if role_conflict:
        conflicts.append(
            f"{source_label}.agent_role has conflicting values: "
            f"{_source_values_json(roles)}"
        )
    if path_conflict:
        conflicts.append(
            f"{source_label}.agent_path has conflicting values: "
            f"{_source_values_json(paths)}"
        )
    return ThreadSourceInfo(
        is_subagent=True,
        parent_thread_ids=tuple(sorted(parents)),
        agent_nickname=nickname,
        agent_role=role,
        agent_path=path,
        metadata_sources=(source_label,),
        metadata_conflicts=tuple(conflicts),
    )


def parse_thread_lineage(
    source: object, *, parent_thread_id: object = None,
    thread_source: object = None, source_label: str = "session_meta",
) -> ThreadSourceInfo:
    """Merge independent, structured lineage evidence without reading messages."""
    info = parse_thread_source(source, source_label=f"{source_label}.source")
    marker = parse_thread_source(thread_source, source_label=f"{source_label}.thread_source")
    parent = _source_text(parent_thread_id)
    parents = set(info.parent_thread_ids) | set(marker.parent_thread_ids)
    conflicts = set(info.metadata_conflicts) | set(marker.metadata_conflicts)
    sources = set(info.metadata_sources) | set(marker.metadata_sources)
    if (info.is_subagent or parent) and thread_source not in (None, "") and not marker.is_subagent:
        conflicts.add("thread_source conflicts with subagent source metadata")
    if parent:
        if parents and parents != {parent}:
            conflicts.add("parent_thread_ids have conflicting source values")
        parents.add(parent)
        sources.add(f"{source_label}.parent_thread_id")
    if len(parents) > 1:
        conflicts.add("parent_thread_ids have conflicting source values")
    return replace(
        info, is_subagent=info.is_subagent or marker.is_subagent or bool(parent),
        parent_thread_ids=tuple(sorted(parents)),
        metadata_sources=tuple(sorted(sources)),
        metadata_conflicts=tuple(sorted(conflicts)),
    )


def rollout_lineage(record: RolloutRecord) -> ThreadSourceInfo:
    return parse_thread_lineage(
        record.source, parent_thread_id=getattr(record, "parent_thread_id", None),
        thread_source=getattr(record, "thread_source", None),
    )


def row_lineage(row: Any) -> ThreadSourceInfo:
    row = row or {}
    return parse_thread_lineage(row.get("source"), parent_thread_id=row.get("parent_thread_id"),
        thread_source=row.get("thread_source"), source_label="threads")


def source_lineage_evidence(row: Any, records: Iterable[RolloutRecord]) -> tuple[bool, set[str], list[str]]:
    infos = [row_lineage(row), *(rollout_lineage(record) for record in records)]
    return (any(info.is_subagent for info in infos),
        {parent for info in infos for parent in info.parent_thread_ids},
        sorted({source for info in infos for source in info.metadata_sources}))


def indexed_rollout_parent_confirmed(
    row: Any, records: Iterable[RolloutRecord], parent_id: str, codex_home: Path,
) -> bool:
    """Exact explicit-selection evidence for newer guardian metadata.

    The native index must independently identify a subagent and its sole
    rollout. The rollout's structured top-level parent is authoritative when
    the index does not duplicate it. Conflicting or nested-only evidence does
    not qualify. Callers must additionally prove the parent absent and freeze
    the complete descendant scope.
    """
    records = tuple(records)
    if not row or len(records) != 1 or not parent_id:
        return False
    record = records[0]
    if getattr(record, "parent_thread_id", None) != parent_id:
        return False
    indexed = row_lineage(row)
    rollout = rollout_lineage(record)
    role = parse_thread_source(row.get("source")).is_subagent or parse_thread_source(row.get("thread_source")).is_subagent
    if (not role or indexed.metadata_conflicts or rollout.metadata_conflicts
            or set(indexed.parent_thread_ids) - {parent_id}
            or set(rollout.parent_thread_ids) != {parent_id}):
        return False
    raw_path = row.get("rollout_path")
    if not isinstance(raw_path, str) or not raw_path:
        return False
    path = Path(raw_path)
    if not path.is_absolute():
        path = codex_home / path
    return (row.get("id", record.thread_id) == record.thread_id and path.is_file()
            and canonical_existing_path_key(path) == canonical_existing_path_key(record.path))


def read_native_lineage(
    codex_home: Path, *, rollout_records: Iterable[RolloutRecord] | None = None,
    strict: bool = True,
) -> dict[str, ThreadSourceInfo]:
    """One lineage contract for inventory, planning and targeted verification."""
    evidence: dict[str, list[ThreadSourceInfo]] = {}
    database = codex_home / "state_5.sqlite"
    if database.is_file():
        try:
            with closing(connect_readonly(database)) as connection:
                if table_exists(connection, "threads"):
                    columns = {r["name"] for r in connection.execute("PRAGMA table_info(threads)")}
                    fields = [name for name in ("id", "source", "thread_source", "parent_thread_id") if name in columns]
                    if "id" not in fields:
                        raise CodexStateReadError("threads is missing id")
                    for raw in connection.execute("SELECT " + ", ".join(fields) + " FROM threads"):
                        row = dict(raw)
                        evidence.setdefault(row["id"], []).append(parse_thread_lineage(
                            row.get("source"), parent_thread_id=row.get("parent_thread_id"),
                            thread_source=row.get("thread_source"), source_label="threads",
                        ))
                if table_exists(connection, "thread_spawn_edges"):
                    columns = {r["name"] for r in connection.execute("PRAGMA table_info(thread_spawn_edges)")}
                    missing = {"parent_thread_id", "child_thread_id"} - columns
                    if missing:
                        if strict:
                            raise CodexStateReadError("thread_spawn_edges is missing " + ", ".join(sorted(missing)))
                        continue_edges = False
                    else:
                        continue_edges = True
                    for row in (connection.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges") if continue_edges else ()):
                        if _source_text(row["parent_thread_id"]) and _source_text(row["child_thread_id"]):
                            evidence.setdefault(row["child_thread_id"], []).append(ThreadSourceInfo(
                                is_subagent=True, parent_thread_ids=(row["parent_thread_id"],),
                                metadata_sources=("thread_spawn_edges",),
                            ))
        except (sqlite3.Error, OSError) as exc:
            if strict:
                raise CodexStateReadError(f"Could not inspect native lineage in {database}: {exc}") from exc
    records = iter_rollouts(codex_home) if rollout_records is None else rollout_records
    for record in records:
        evidence.setdefault(record.thread_id, []).append(rollout_lineage(record))
    result: dict[str, ThreadSourceInfo] = {}
    for child, infos in evidence.items():
        parents = {parent for info in infos for parent in info.parent_thread_ids}
        conflicts = {value for info in infos for value in info.metadata_conflicts}
        if len(parents) > 1:
            conflicts.add("parent_thread_ids have conflicting source values")
        if child in parents:
            conflicts.add("thread cannot be its own parent")
        result[child] = ThreadSourceInfo(
            is_subagent=any(info.is_subagent for info in infos),
            parent_thread_ids=tuple(sorted(parents)),
            metadata_sources=tuple(sorted({s for info in infos for s in info.metadata_sources})),
            metadata_conflicts=tuple(sorted(conflicts)),
        )
    # A cycle cannot be safely interpreted as a deletion tree.
    for child in result:
        pending = list(result[child].parent_thread_ids)
        seen: set[str] = set()
        while pending:
            parent = pending.pop()
            if parent == child:
                info = result[child]
                result[child] = replace(info, metadata_conflicts=tuple(sorted(set(info.metadata_conflicts) | {"cyclic parent relationship"})))
                break
            if parent not in seen:
                seen.add(parent)
                if parent in result:
                    pending.extend(result[parent].parent_thread_ids)
    return result


def scan_rollouts(codex_home: Path) -> dict[str, RolloutRecord]:
    """Read only the session_meta line from each active or archived rollout."""
    records: dict[str, RolloutRecord] = {}
    for record in iter_rollouts(codex_home):
        records[record.thread_id] = record
    return records


def iter_rollouts(codex_home: Path) -> Iterable[RolloutRecord]:
    """Yield every valid rollout, including duplicate IDs in different paths."""

    for directory, archived in (("sessions", False), ("archived_sessions", True)):
        root = codex_home / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            record = _read_rollout_meta(path, archived=archived)
            if record is not None:
                yield record


def find_thread_rollouts(
    codex_home: Path,
    thread_id: str,
) -> list[RolloutRecord]:
    """Return all active and archived rollout paths for one thread ID."""

    return [
        record for record in iter_rollouts(codex_home) if record.thread_id == thread_id
    ]


def read_rollouts_at_paths(
    codex_home: Path,
    paths: Iterable[Path | str],
    *,
    strict: bool = True,
) -> tuple[RolloutRecord, ...]:
    """Read only approved rollout paths within the two native roots."""

    home = codex_home.expanduser().resolve()
    roots = (
        ((home / "sessions").resolve(), False),
        ((home / "archived_sessions").resolve(), True),
    )
    records: list[RolloutRecord] = []
    for raw_path in dict.fromkeys(Path(value) for value in paths):
        path = raw_path.expanduser()
        if not path.is_absolute():
            path = home / path
        try:
            original_state = path.lstat()
            resolved = path.resolve(strict=True)
            path_state = resolved.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            if strict:
                raise CodexStateReadError(
                    f"Could not inspect approved rollout path {path}: {exc}"
                ) from exc
            continue
        matched: tuple[Path, bool] | None = None
        for root, archived in roots:
            try:
                common = os.path.commonpath((os.fspath(root), os.fspath(resolved)))
            except ValueError:
                continue
            if os.path.normcase(common) == os.path.normcase(os.fspath(root)):
                matched = (root, archived)
                break
        if matched is None:
            if strict:
                raise CodexStateReadError(
                    f"Approved rollout path escapes the native store: {path}"
                )
            continue
        if (
            stat.S_ISLNK(original_state.st_mode)
            or not stat.S_ISREG(path_state.st_mode)
        ):
            if strict:
                raise CodexStateReadError(
                    f"Approved rollout path is not an ordinary file: {path}"
                )
            continue
        record = _read_rollout_meta(resolved, archived=matched[1])
        if record is not None:
            records.append(record)
    return tuple(records)


def rollout_state_fingerprint(record: RolloutRecord) -> str:
    """Return a canonical fingerprint of rollout identity and file state.

    The file ``stat`` call is intentionally not softened.  A caller relying
    on this fingerprint for an exact deletion scope must fail closed if the
    current file state cannot be inspected.
    """

    stat_result = record.path.stat()
    payload = {
        "archived": record.archived,
        "cwd": record.cwd,
        "metadata_thread_id": record.thread_id,
        "originator": record.originator,
        "path": canonical_existing_path_key(record.path),
        "source": record.source,
        "parent_thread_id": record.parent_thread_id,
        "thread_source": record.thread_source,
        "st_dev": stat_result.st_dev,
        "st_ino": stat_result.st_ino,
        "st_mtime_ns": stat_result.st_mtime_ns,
        "st_size": stat_result.st_size,
        "timestamp": record.timestamp,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"v1:{hashlib.sha256(canonical).hexdigest()}"


def read_spawn_descendants(
    codex_home: Path,
    thread_ids: Iterable[str],
    *,
    strict: bool = True,
    rollout_records: Iterable[RolloutRecord] | None = None,
) -> dict[str, set[str]]:
    """Read native spawn relationships and return transitive descendants."""

    roots = sorted(set(thread_ids))
    if not roots:
        return {}

    graph: dict[str, set[str]] = {}
    lineage = read_native_lineage(codex_home, rollout_records=rollout_records, strict=strict)
    for child, info in lineage.items():
        for parent in info.parent_thread_ids:
            graph.setdefault(parent, set()).add(child)

    descendants: dict[str, set[str]] = {}
    for root in roots:
        seen: set[str] = set()
        pending = list(graph.get(root, ()))
        while pending:
            child = pending.pop()
            if child == root or child in seen:
                continue
            seen.add(child)
            pending.extend(graph.get(child, ()))
        descendants[root] = seen
    return descendants


def read_spawn_edges(
    codex_home: Path,
    thread_ids: Iterable[str],
    *,
    strict: bool = True,
    rollout_records: Iterable[RolloutRecord] | None = None,
) -> set[tuple[str, str]]:
    """Return direct spawn edges touching any requested conversation."""

    target_ids = {
        thread_id
        for thread_id in thread_ids
        if isinstance(thread_id, str) and thread_id
    }
    if not target_ids:
        return set()

    lineage = read_native_lineage(codex_home, rollout_records=rollout_records, strict=strict)
    return {
        (parent, child) for child, info in lineage.items()
        for parent in info.parent_thread_ids
        if parent in target_ids or child in target_ids
    }


def read_spawn_edge_records(
    codex_home: Path,
    thread_ids: Iterable[str],
    *,
    strict: bool = True,
) -> tuple[SpawnEdgeRecord, ...]:
    """Return native database spawn rows touching requested conversations."""

    target_ids = {
        thread_id
        for thread_id in thread_ids
        if isinstance(thread_id, str) and thread_id
    }
    if not target_ids:
        return ()

    state_db = codex_home / "state_5.sqlite"
    if not state_db.is_file():
        return ()
    try:
        with closing(connect_readonly(state_db)) as connection:
            if not table_exists(connection, "thread_spawn_edges"):
                return ()
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(thread_spawn_edges)"
                )
                if isinstance(row["name"], str)
            }
            required = {"parent_thread_id", "child_thread_id"}
            missing = sorted(required - columns)
            if missing:
                if strict:
                    raise CodexStateReadError(
                        f"{state_db} is incompatible: table "
                        "'thread_spawn_edges' is missing column(s) "
                        f"{', '.join(missing)}"
                    )
                return ()
            status_projection = (
                "status" if "status" in columns else "NULL AS status"
            )
            rows = connection.execute(
                """
                SELECT parent_thread_id, child_thread_id, %s
                FROM thread_spawn_edges
                """
                % status_projection
            ).fetchall()
    except sqlite3.Error as exc:
        if strict:
            raise CodexStateReadError(
                f"Could not inspect spawn edges in {state_db}: {exc}"
            ) from exc
        return ()

    return tuple(
        SpawnEdgeRecord(
            parent_thread_id=row["parent_thread_id"],
            child_thread_id=row["child_thread_id"],
            status=(
                row["status"]
                if isinstance(row["status"], str)
                else None
            ),
        )
        for row in rows
        if (
            isinstance(row["parent_thread_id"], str)
            and row["parent_thread_id"]
            and isinstance(row["child_thread_id"], str)
            and row["child_thread_id"]
            and (
                row["parent_thread_id"] in target_ids
                or row["child_thread_id"] in target_ids
            )
        )
    )


def _read_rollout_meta(path: Path, *, archived: bool) -> RolloutRecord | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
        raw = json.loads(first_line)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if raw.get("type") != "session_meta":
        return None
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return None
    thread_id = payload.get("id") or payload.get("session_id")
    if not isinstance(thread_id, str) or not thread_id:
        return None
    return RolloutRecord(
        thread_id=thread_id,
        path=path,
        originator=_optional_string(payload.get("originator")),
        source=payload.get("source"),
        cwd=_optional_string(payload.get("cwd")),
        timestamp=_optional_string(payload.get("timestamp")),
        archived=archived,
        parent_thread_id=_optional_string(payload.get("parent_thread_id")),
        thread_source=_optional_string(payload.get("thread_source")),
    )


def read_thread_index(
    codex_home: Path,
    thread_ids: Iterable[str],
    *,
    strict: bool = False,
) -> dict[str, dict[str, Any]]:
    ids = sorted(set(thread_ids))
    state_db = codex_home / "state_5.sqlite"
    if not ids or not state_db.is_file():
        return {}
    try:
        with closing(connect_readonly(state_db)) as connection:
            if not table_exists(connection, "threads"):
                if strict:
                    raise CodexStateReadError(
                        f"{state_db} is incompatible: required table "
                        "'threads' is missing"
                    )
                return {}
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(threads)")
                if isinstance(row["name"], str)
            }
            required = {"id", "rollout_path"}
            missing = sorted(required - columns)
            if missing:
                if strict:
                    raise CodexStateReadError(
                        f"{state_db} is incompatible: table 'threads' is "
                        f"missing column(s) {', '.join(missing)}"
                    )
                return {}
            placeholders = ",".join("?" for _ in ids)
            projections = ["id", "rollout_path"]
            projections.extend(
                column if column in columns else f"NULL AS {column}"
                for column in (
                    "archived",
                    "created_at",
                    "updated_at",
                    "source",
                    "thread_source",
                    "parent_thread_id",
                )
            )
            rows = connection.execute(
                f"""
                SELECT {", ".join(projections)}
                FROM threads
                WHERE id IN ({placeholders})
                """,
                ids,
            ).fetchall()
    except sqlite3.Error as exc:
        # A corrupt, locked, or incompatible state database should not make a
        # read-only scan crash.  Callers treat it as unavailable evidence.
        if strict:
            raise CodexStateReadError(
                f"Could not inspect {state_db}: {exc}"
            ) from exc
        return {}
    return {row["id"]: dict(row) for row in rows}


_THREAD_METADATA_COLUMNS = (
    "parent_thread_id",
    "rollout_path",
    "archived",
    "source",
    "thread_source",
    "name",
    "title",
    "cwd",
    "git_origin_url",
    "agent_nickname",
    "agent_role",
    "agent_path",
    "originator",
)


def read_thread_metadata(
    codex_home: Path,
    thread_ids: Iterable[str],
    *,
    strict: bool = True,
) -> dict[str, dict[str, Any]]:
    """Read optional identity columns for only the requested thread IDs.

    Unlike :func:`read_thread_index`, absent optional columns are not
    projected as ``NULL``.  Callers can therefore distinguish an older schema
    that lacks a column from a current schema in which the stored value is
    actually null.  Unknown columns are never interpolated into SQL.
    """

    ids = sorted(
        {
            thread_id
            for thread_id in thread_ids
            if isinstance(thread_id, str) and thread_id
        }
    )
    state_db = codex_home / "state_5.sqlite"
    if not ids or not state_db.is_file():
        return {}
    try:
        with closing(connect_readonly(state_db)) as connection:
            if not table_exists(connection, "threads"):
                if strict:
                    raise CodexStateReadError(
                        f"{state_db} is incompatible: required table "
                        "'threads' is missing"
                    )
                return {}
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(threads)")
                if isinstance(row["name"], str)
            }
            if "id" not in columns:
                if strict:
                    raise CodexStateReadError(
                        f"{state_db} is incompatible: table 'threads' is "
                        "missing column(s) id"
                    )
                return {}

            projections = ["id"]
            projections.extend(
                column
                for column in _THREAD_METADATA_COLUMNS
                if column in columns
            )
            rows: list[sqlite3.Row] = []
            # Stay below conservative SQLite host-parameter limits while
            # preserving one read-only connection and one schema snapshot.
            for offset in range(0, len(ids), 500):
                batch = ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows.extend(
                    connection.execute(
                        f"""
                        SELECT {", ".join(projections)}
                        FROM threads
                        WHERE id IN ({placeholders})
                        """,
                        batch,
                    ).fetchall()
                )
    except sqlite3.Error as exc:
        if strict:
            raise CodexStateReadError(
                f"Could not inspect {state_db}: {exc}"
            ) from exc
        return {}
    return {
        row["id"]: dict(row)
        for row in rows
        if isinstance(row["id"], str) and row["id"]
    }


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _source_parent_ids(value: object) -> set[str]:
    return set(parse_thread_source(value).parent_thread_ids)


def _source_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _append_source_text(values: list[str], value: object) -> None:
    text = _source_text(value)
    if text is not None:
        values.append(text)


def _one_source_value(values: Iterable[str]) -> tuple[str | None, bool]:
    distinct = set(values)
    if not distinct:
        return None, False
    if len(distinct) != 1:
        return None, True
    return next(iter(distinct)), False


def _source_values_json(values: Iterable[str]) -> str:
    return json.dumps(
        sorted(set(values)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
