from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from abc import ABC, abstractmethod
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from ..models import Finding
from ..record_identity import (
    EngineCapability,
    StoreKey,
    capability_for,
    normalize_client,
    normalize_engine,
)
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

@dataclass(frozen=True)
class FrontendBatchSnapshot:
    """One immutable frontend enumeration reused by all batch guards.

    A guard may still perform a narrow, exact row check immediately before a
    write. It must not enumerate the frontend database again for every native
    action. ``records`` intentionally contains metadata-only adapter rows.
    """

    client: str
    database: Path
    records: tuple["FrontendSessionRecord", ...]
    fingerprint: str

    @property
    def live_native_ids(self) -> frozenset[str]:
        return frozenset(
            record.thread_id
            for record in self.records
            if record.is_live and isinstance(record.thread_id, str)
        )

    @property
    def native_ids(self) -> frozenset[str]:
        return frozenset(
            record.thread_id
            for record in self.records
            if isinstance(record.thread_id, str)
        )

    def references_for(
        self,
        native_ids: Iterable[str],
    ) -> Mapping[str, tuple["FrontendSessionRecord", ...]]:
        wanted = {value for value in native_ids if isinstance(value, str)}
        grouped: dict[str, list["FrontendSessionRecord"]] = {
            value: [] for value in wanted
        }
        for record in self.records:
            if record.thread_id in grouped:
                grouped[record.thread_id].append(record)
        return {
            value: tuple(grouped[value])
            for value in sorted(grouped)
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "database": str(self.database),
            "fingerprint": self.fingerprint,
            "records": [record.to_dict() for record in self.records],
            "live_native_ids": sorted(self.live_native_ids),
        }

class FrontendAdapter(ABC):
    name: str

    def __init__(
        self,
        *,
        database: Path,
        codex_home: Path,
        data_root: Path | None = None,
        owner_process_root: Path | None = None,
    ) -> None:
        self.database = Path(database).expanduser()
        self.codex_home = Path(codex_home).expanduser()
        # Keep storage identity and process ownership as separate frozen
        # values. In particular, never derive the process root from a
        # database parent or from a directory basename at execution time.
        self._data_root = Path(
            data_root if data_root is not None else self.database.parent
        ).expanduser()
        self._owner_process_root = Path(
            owner_process_root
            if owner_process_root is not None
            else self.codex_home
        ).expanduser()
        self._live_thread_ids: set[str] = set()
        self._frontend_snapshot: FrontendBatchSnapshot | None = None

    @property
    def data_root(self) -> Path:
        """Exact frontend data root associated with this adapter."""

        return self._data_root

    @property
    def owner_process_root(self) -> Path:
        """Exact root passed to the owning-client process guard."""

        return self._owner_process_root

    @property
    def client(self) -> str:
        return normalize_client(self.name)

    @property
    def engine(self) -> str:
        return normalize_engine(
            getattr(self, "backend", None)
            or getattr(self, "engine_name", None)
            or "codex"
        )

    @property
    def store_key(self) -> StoreKey:
        return StoreKey(self.engine, self.database, kind="sqlite")

    @property
    def capability(self) -> EngineCapability:
        """Conservative capability for this concrete adapter instance."""

        return capability_for(self.client, self.engine, observed=self.available)

    def invalidate_frontend_snapshot(self) -> None:
        """Drop a batch snapshot after a successful frontend mutation."""

        self._frontend_snapshot = None

    def snapshot_sessions(
        self,
        *,
        refresh: bool = False,
        all_backends: bool = False,
    ) -> FrontendBatchSnapshot:
        """Enumerate frontend metadata once and cache the immutable result.

        Mutation code can use the returned snapshot for all action guards. A
        caller must explicitly request ``refresh`` after a write or external
        state change; action loops never rebuild the catalog implicitly.
        """

        if self._frontend_snapshot is not None and not refresh:
            return self._frontend_snapshot
        if all_backends and getattr(self, "supports_all_backends", False):
            rows = tuple(self.list_sessions(all_backends=True))
        else:
            rows = tuple(self.list_sessions())
        canonical_rows = [
            record.approval_payload()
            for record in sorted(
                rows,
                key=lambda item: (
                    str(item.platform),
                    str(item.database).casefold(),
                    str(item.platform_session_id),
                    str(item.thread_id or ""),
                ),
            )
        ]
        encoded = json.dumps(
            canonical_rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        snapshot = FrontendBatchSnapshot(
            client=self.client,
            database=self.database,
            records=rows,
            fingerprint="v1:" + hashlib.sha256(encoded).hexdigest(),
        )
        self._frontend_snapshot = snapshot
        self._replace_live_thread_ids(set(snapshot.live_native_ids))
        return snapshot

    # Explicit aliases make the batch contract discoverable to callers that
    # use either noun without adding another enumeration path.
    build_frontend_snapshot = snapshot_sessions
    frontend_snapshot = snapshot_sessions

    def batch_frontend_guard(
        self,
        thread_ids: Iterable[str],
        *,
        snapshot: FrontendBatchSnapshot | None = None,
    ) -> frozenset[str]:
        """Return live references among target IDs from one cached snapshot."""

        wanted = {value for value in thread_ids if isinstance(value, str)}
        current = snapshot or self.snapshot_sessions()
        return frozenset(wanted & current.live_native_ids)

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

    def live_thread_ids_for(
        self,
        thread_ids: set[str],
    ) -> frozenset[str]:
        """Re-read only frontend references needed by a mutation guard.

        This deliberately does not call :meth:`scan`: a guard must not rescan
        native rollouts or rebuild the global cleanup plan for every action.
        Known adapters may override this with a narrower indexed query.
        """

        if not thread_ids:
            return frozenset()
        return self.batch_frontend_guard(thread_ids)

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
