from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
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
        if stripped.lower() == "subagent":
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
    state_db = codex_home / "state_5.sqlite"
    if state_db.is_file():
        try:
            with closing(connect_readonly(state_db)) as connection:
                if table_exists(connection, "thread_spawn_edges"):
                    columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(thread_spawn_edges)"
                        )
                        if isinstance(row["name"], str)
                    }
                    required_columns = {
                        "parent_thread_id",
                        "child_thread_id",
                    }
                    missing_columns = sorted(required_columns - columns)
                    if missing_columns and strict:
                        raise CodexStateReadError(
                            f"{state_db} is incompatible: table "
                            "'thread_spawn_edges' is missing column(s) "
                            f"{', '.join(missing_columns)}"
                        )
                    if not missing_columns:
                        rows = connection.execute(
                            """
                            SELECT parent_thread_id, child_thread_id
                            FROM thread_spawn_edges
                            """
                        ).fetchall()
                        for row in rows:
                            parent = row["parent_thread_id"]
                            child = row["child_thread_id"]
                            if (
                                isinstance(parent, str)
                                and parent
                                and isinstance(child, str)
                                and child
                            ):
                                graph.setdefault(parent, set()).add(child)
        except sqlite3.Error as exc:
            if strict:
                raise CodexStateReadError(
                    f"Could not inspect spawn edges in {state_db}: {exc}"
                ) from exc

    # Some Codex versions retain the relationship only in session metadata.
    records = (
        iter_rollouts(codex_home)
        if rollout_records is None
        else rollout_records
    )
    for record in records:
        for parent in _source_parent_ids(record.source):
            graph.setdefault(parent, set()).add(record.thread_id)

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

    edges: set[tuple[str, str]] = set()
    state_db = codex_home / "state_5.sqlite"
    if state_db.is_file():
        try:
            with closing(connect_readonly(state_db)) as connection:
                if table_exists(connection, "thread_spawn_edges"):
                    columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(thread_spawn_edges)"
                        )
                        if isinstance(row["name"], str)
                    }
                    required = {"parent_thread_id", "child_thread_id"}
                    missing = sorted(required - columns)
                    if missing and strict:
                        raise CodexStateReadError(
                            f"{state_db} is incompatible: table "
                            "'thread_spawn_edges' is missing column(s) "
                            f"{', '.join(missing)}"
                        )
                    if not missing:
                        for row in connection.execute(
                            """
                            SELECT parent_thread_id, child_thread_id
                            FROM thread_spawn_edges
                            """
                        ):
                            parent = row["parent_thread_id"]
                            child = row["child_thread_id"]
                            if (
                                isinstance(parent, str)
                                and parent
                                and isinstance(child, str)
                                and child
                                and (
                                    parent in target_ids
                                    or child in target_ids
                                )
                            ):
                                edges.add((parent, child))
        except sqlite3.Error as exc:
            if strict:
                raise CodexStateReadError(
                    f"Could not inspect spawn edges in {state_db}: {exc}"
                ) from exc

    records = (
        iter_rollouts(codex_home)
        if rollout_records is None
        else rollout_records
    )
    for record in records:
        for parent in _source_parent_ids(record.source):
            if parent in target_ids or record.thread_id in target_ids:
                edges.add((parent, record.thread_id))
    return edges


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
