from __future__ import annotations

import sqlite3
import stat
from abc import ABC, abstractmethod
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models import Finding
from ..sqlite_utils import connect_readonly, table_exists

if TYPE_CHECKING:
    from ..inventory import FrontendSessionRecord


class AdapterScanError(RuntimeError):
    """An adapter could not establish enough evidence for a safe scan."""


@dataclass(frozen=True)
class CodexEvidence:
    """Codex index and cascade evidence for a set of frontend thread IDs."""

    indexed_threads: dict[str, dict[str, Any]]
    descendants_by_parent: dict[str, tuple[str, ...]]
    spawn_edges_available: bool


class FrontendAdapter(ABC):
    name: str

    def __init__(self, *, database: Path, codex_home: Path) -> None:
        self.database = database.expanduser()
        self.codex_home = codex_home.expanduser()
        self._live_thread_ids: set[str] = set()

    @property
    def available(self) -> bool:
        return self.database.is_file() and self.codex_home.is_dir()

    @property
    def live_thread_ids(self) -> frozenset[str]:
        """Clearly Codex-owned thread IDs still referenced by live sessions."""

        return frozenset(self._live_thread_ids)

    def _replace_live_thread_ids(self, thread_ids: set[str]) -> None:
        self._live_thread_ids = set(thread_ids)

    def list_sessions(self) -> list[FrontendSessionRecord]:
        """Return every frontend reference or mapping to a Codex thread.

        This intentionally is not abstract.  Native and third-party adapters
        written against earlier Janitor releases therefore remain usable;
        their Codex artifacts are still inventoried directly from the native
        store.  The method name is retained as a public compatibility surface;
        returned rows are frontend evidence, not another native session.
        Implementations must be read-only and should raise
        :class:`AdapterScanError` when a present database cannot be read
        completely.
        """

        return []

    @abstractmethod
    def scan(self) -> list[Finding]:
        raise NotImplementedError


def require_table_columns(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    required_columns: set[str],
    database: Path,
) -> None:
    """Raise instead of silently treating an incompatible schema as empty."""

    if not table_exists(connection, table_name):
        raise AdapterScanError(
            f"{database} is incompatible: required table {table_name!r} is missing"
        )
    columns = table_columns(
        connection,
        table_name=table_name,
        database=database,
    )
    missing = sorted(required_columns - columns)
    if missing:
        raise AdapterScanError(
            f"{database} is incompatible: table {table_name!r} is missing "
            f"column(s) {', '.join(missing)}"
        )


def table_columns(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    database: Path,
) -> set[str]:
    """Return a validated table-column snapshot."""

    if not table_exists(connection, table_name):
        raise AdapterScanError(
            f"{database} is incompatible: required table {table_name!r} is missing"
        )
    try:
        return {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table_name})")
            if isinstance(row["name"], str)
        }
    except sqlite3.Error as exc:
        raise AdapterScanError(
            f"Could not inspect schema for {table_name!r} in {database}: {exc}"
        ) from exc


def optional_database_file_exists(database: Path) -> bool:
    """Return false only when an optional frontend database is absent.

    ``Path.is_file()`` suppresses some stat errors. Treating an unreadable or
    structurally invalid path as "not installed" would weaken delete guards.
    """

    try:
        status = database.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdapterScanError(
            f"Could not inspect frontend database path {database}: {exc}"
        ) from exc
    if not stat.S_ISREG(status.st_mode):
        raise AdapterScanError(
            f"Frontend database path is not a regular file: {database}"
        )
    return True


def read_codex_evidence(
    codex_home: Path,
    thread_ids: list[str],
) -> CodexEvidence:
    """Read index membership and known direct descendants without writing."""

    ids = sorted({thread_id for thread_id in thread_ids if thread_id})
    state_db = codex_home / "state_5.sqlite"
    if not ids or not state_db.is_file():
        return CodexEvidence({}, {}, False)

    indexed: dict[str, dict[str, Any]] = {}
    descendants: dict[str, list[str]] = defaultdict(list)
    try:
        with closing(connect_readonly(state_db)) as connection:
            require_table_columns(
                connection,
                table_name="threads",
                required_columns={"id", "rollout_path", "archived"},
                database=state_db,
            )
            for chunk in _chunks(ids):
                placeholders = ",".join("?" for _ in chunk)
                for row in connection.execute(
                    f"""
                    SELECT id, rollout_path, archived
                    FROM threads
                    WHERE id IN ({placeholders})
                    """,
                    chunk,
                ):
                    indexed[row["id"]] = dict(row)

            spawn_edges_available = table_exists(connection, "thread_spawn_edges")
            if spawn_edges_available:
                require_table_columns(
                    connection,
                    table_name="thread_spawn_edges",
                    required_columns={"parent_thread_id", "child_thread_id"},
                    database=state_db,
                )
                for chunk in _chunks(ids):
                    placeholders = ",".join("?" for _ in chunk)
                    for row in connection.execute(
                        f"""
                        SELECT parent_thread_id, child_thread_id
                        FROM thread_spawn_edges
                        WHERE parent_thread_id IN ({placeholders})
                        """,
                        chunk,
                    ):
                        parent = row["parent_thread_id"]
                        child = row["child_thread_id"]
                        if isinstance(parent, str) and isinstance(child, str):
                            descendants[parent].append(child)
    except AdapterScanError:
        raise
    except sqlite3.Error as exc:
        raise AdapterScanError(
            f"Could not inspect Codex state database {state_db}: {exc}"
        ) from exc

    normalized_descendants = {
        parent: tuple(sorted(set(children)))
        for parent, children in descendants.items()
    }
    return CodexEvidence(
        indexed_threads=indexed,
        descendants_by_parent=normalized_descendants,
        spawn_edges_available=spawn_edges_available,
    )


def _chunks(values: list[str], size: int = 400) -> list[list[str]]:
    return [values[offset : offset + size] for offset in range(0, len(values), size)]
