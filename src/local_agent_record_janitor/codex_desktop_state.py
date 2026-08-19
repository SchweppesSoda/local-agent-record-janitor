from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .codex_state import find_thread_rollouts, read_thread_index
from .path_identity import canonical_existing_path_key
from .sqlite_utils import connect_readonly, table_exists


class DesktopStateError(RuntimeError):
    """Codex Desktop host state could not be inspected or changed safely."""


@dataclass(frozen=True)
class DesktopCatalogRecord:
    host_id: str
    thread_id: str
    title: str | None
    database: Path
    row: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "thread_id": self.thread_id,
            "title": self.title,
            "database": str(self.database),
            "row": _json_value(dict(self.row)),
        }


@dataclass(frozen=True)
class DesktopThreadState:
    thread_id: str
    catalog_records: tuple[DesktopCatalogRecord, ...]
    global_state_references: Mapping[str, tuple[str, ...]]
    catalog_revision: int | None
    state_file_sha256: Mapping[str, str]
    snapshot_fingerprint: str

    @property
    def exact_reference_count(self) -> int:
        return sum(len(items) for items in self.global_state_references.values())

    @property
    def present(self) -> bool:
        return bool(self.catalog_records) or bool(self.exact_reference_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "catalog_records": [item.to_dict() for item in self.catalog_records],
            "global_state_references": {
                path: list(references)
                for path, references in self.global_state_references.items()
            },
            "exact_reference_count": self.exact_reference_count,
            "catalog_revision": self.catalog_revision,
            "state_file_sha256": dict(self.state_file_sha256),
            "snapshot_fingerprint": self.snapshot_fingerprint,
        }


@dataclass(frozen=True)
class DesktopStateSnapshot:
    codex_home: Path
    database: Path | None
    state_paths: tuple[Path, ...]
    threads: Mapping[str, DesktopThreadState]

    def to_dict(self) -> dict[str, Any]:
        return {
            "codex_home": str(self.codex_home),
            "database": str(self.database) if self.database is not None else None,
            "state_paths": [str(path) for path in self.state_paths],
            "threads": {
                thread_id: state.to_dict()
                for thread_id, state in sorted(self.threads.items())
            },
        }


@dataclass(frozen=True)
class DesktopCleanupResult:
    codex_home: Path
    thread_ids: tuple[str, ...]
    deleted_catalog_rows: int
    removed_global_state_references: int
    backup_id: str
    backup_directory: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "cleaned",
            "codex_home": str(self.codex_home),
            "thread_ids": list(self.thread_ids),
            "deleted_catalog_rows": self.deleted_catalog_rows,
            "removed_global_state_references": (
                self.removed_global_state_references
            ),
            "backup_id": self.backup_id,
            "backup_directory": str(self.backup_directory),
            "verification": {
                "remaining_catalog_rows": 0,
                "remaining_exact_global_state_references": 0,
            },
        }


def read_desktop_state(
    codex_home: Path,
    thread_ids: Iterable[str] | None = None,
) -> DesktopStateSnapshot:
    """Read the optional Codex Desktop host catalog and exact UI references.

    These files are host implementation details, not part of the documented
    app-server protocol.  Discovery is therefore schema-probed and optional;
    a present but incompatible catalog fails closed instead of being treated
    as empty.
    """

    home = codex_home.expanduser().resolve()
    database = _discover_catalog_database(home)
    state_paths = _global_state_paths(home)
    requested = {
        value for value in (thread_ids or ()) if isinstance(value, str) and value
    }

    catalog_records: list[DesktopCatalogRecord] = []
    catalog_revision: int | None = None
    if database is not None:
        catalog_records, catalog_revision = _read_catalog_records(
            database,
            requested or None,
        )

    target_ids = requested or {record.thread_id for record in catalog_records}
    state_values: dict[Path, Any] = {}
    state_hashes: dict[str, str] = {}
    refs_by_thread: dict[str, dict[str, tuple[str, ...]]] = {
        thread_id: {} for thread_id in target_ids
    }
    for path in state_paths:
        value = _read_json(path)
        state_values[path] = value
        rendered = str(path)
        state_hashes[rendered] = sha256_file(path)
        for thread_id in target_ids:
            references = tuple(find_state_references(value, {thread_id}))
            if references:
                refs_by_thread[thread_id][rendered] = references

    records_by_thread: dict[str, list[DesktopCatalogRecord]] = {
        thread_id: [] for thread_id in target_ids
    }
    for record in catalog_records:
        records_by_thread.setdefault(record.thread_id, []).append(record)

    states: dict[str, DesktopThreadState] = {}
    for thread_id in sorted(target_ids):
        records = tuple(
            sorted(
                records_by_thread.get(thread_id, ()),
                key=lambda item: (item.host_id, str(item.database)),
            )
        )
        references = refs_by_thread.get(thread_id, {})
        payload = {
            "schema": "codex-desktop-thread-state-v1",
            "codex_home": _normalized_path(home),
            "database": str(database) if database is not None else None,
            "catalog_records": [record.to_dict() for record in records],
            "catalog_revision": catalog_revision,
            "state_file_sha256": state_hashes,
            "global_state_references": {
                path: list(items) for path, items in sorted(references.items())
            },
        }
        states[thread_id] = DesktopThreadState(
            thread_id=thread_id,
            catalog_records=records,
            global_state_references=references,
            catalog_revision=catalog_revision,
            state_file_sha256=state_hashes,
            snapshot_fingerprint="desktop:v1:" + _sha256_json(payload),
        )
    return DesktopStateSnapshot(
        codex_home=home,
        database=database,
        state_paths=state_paths,
        threads=states,
    )


def native_evidence_for_threads(
    codex_home: Path,
    thread_ids: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    ids = tuple(sorted({thread_id for thread_id in thread_ids if thread_id}))
    indexed = read_thread_index(codex_home, ids, strict=True)
    rollout_paths = {
        str(record.path)
        for thread_id in ids
        for record in find_thread_rollouts(codex_home, thread_id)
    }
    return {
        "indexed_thread_ids": tuple(
            thread_id for thread_id in ids if thread_id in indexed
        ),
        "rollout_paths": tuple(sorted(rollout_paths, key=os.path.normcase)),
    }


def remaining_desktop_state_markers(
    codex_home: Path,
    thread_ids: Iterable[str],
) -> tuple[str, ...]:
    snapshot = read_desktop_state(codex_home, thread_ids)
    markers: list[str] = []
    for thread_id, state in sorted(snapshot.threads.items()):
        for record in state.catalog_records:
            markers.append(
                f"desktop-catalog:{record.database}:{record.host_id}:{thread_id}"
            )
        for path, references in sorted(state.global_state_references.items()):
            for reference in references:
                markers.append(f"desktop-state:{path}:{thread_id}:{reference}")
    return tuple(markers)


def execute_desktop_state_cleanup(
    codex_home: Path,
    approved_snapshot_fingerprints: Mapping[str, str],
) -> DesktopCleanupResult:
    """Remove exact Desktop-only thread state after strict revalidation."""

    home = codex_home.expanduser().resolve()
    targets = tuple(sorted(approved_snapshot_fingerprints))
    if not targets:
        raise DesktopStateError("No Codex Desktop thread IDs were selected")

    evidence = native_evidence_for_threads(home, targets)
    if any(evidence.values()):
        raise DesktopStateError(
            "Native thread evidence reappeared; refusing Desktop-only cleanup: "
            + json.dumps(evidence, ensure_ascii=False)
        )

    snapshot = read_desktop_state(home, targets)
    if snapshot.database is None:
        raise DesktopStateError("No compatible Codex Desktop catalog was found")
    for thread_id in targets:
        current = snapshot.threads.get(thread_id)
        if current is None or not current.catalog_records:
            raise DesktopStateError(
                f"The approved local Desktop catalog row disappeared: {thread_id}"
            )
        if any(record.host_id != "local" for record in current.catalog_records):
            raise DesktopStateError(
                f"The target is not exclusively local Desktop state: {thread_id}"
            )
        expected = approved_snapshot_fingerprints.get(thread_id)
        if current.snapshot_fingerprint != expected:
            raise DesktopStateError(
                f"Codex Desktop state changed after approval: {thread_id}"
            )

    clients = running_related_clients(home)
    if clients:
        raise DesktopStateError(
            "Related clients are still running: " + ", ".join(clients)
        )

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "codex_home": str(home),
        "database": str(snapshot.database),
        "database_sha256_before": sha256_file(snapshot.database),
        "targets": {
            thread_id: snapshot.threads[thread_id].to_dict()
            for thread_id in targets
        },
        "state_files_before": {
            str(path): sha256_file(path) for path in snapshot.state_paths
        },
    }
    backup_id, backup_directory = _create_backup(snapshot, manifest)
    deleted_rows = 0
    removed_references = 0
    try:
        deleted_rows = _delete_catalog_rows(snapshot.database, targets)
        for path in snapshot.state_paths:
            expected_hash = manifest["state_files_before"][str(path)]
            if sha256_file(path) != expected_hash:
                raise DesktopStateError(
                    f"Codex Desktop global state changed after backup: {path}"
                )
            value = _read_json(path)
            cleaned, removed = strip_state_references(value, set(targets))
            _atomic_write_json(path, cleaned)
            removed_references += len(removed)

        remaining = read_desktop_state(home, targets)
        leftovers = {
            thread_id: state.to_dict()
            for thread_id, state in remaining.threads.items()
            if state.present
        }
        if leftovers:
            raise DesktopStateError(
                "Post-write verification found remaining Desktop state: "
                + json.dumps(leftovers, ensure_ascii=False)
            )
    except Exception as exc:
        restore_error = _restore_backup(snapshot, backup_directory)
        if restore_error is not None:
            raise DesktopStateError(
                f"Desktop cleanup failed ({exc}); automatic restore also failed "
                f"({restore_error}). Backup: {backup_directory}"
            ) from exc
        if isinstance(exc, DesktopStateError):
            raise DesktopStateError(
                f"{exc}; all changed Desktop files were restored from {backup_id}"
            ) from exc
        raise DesktopStateError(
            f"Desktop cleanup failed ({exc}); all changed Desktop files were "
            f"restored from {backup_id}"
        ) from exc

    return DesktopCleanupResult(
        codex_home=home,
        thread_ids=targets,
        deleted_catalog_rows=deleted_rows,
        removed_global_state_references=removed_references,
        backup_id=backup_id,
        backup_directory=backup_directory,
    )


def find_state_references(
    value: Any,
    targets: set[str],
    path: str = "$",
) -> list[str]:
    """Find structured exact-ID references without matching prompt text."""

    references: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if any(target in str(key) for target in targets):
                references.append(f"key:{child_path}")
                continue
            if isinstance(child, str) and child in targets:
                references.append(f"value:{child_path}")
                continue
            references.extend(find_state_references(child, targets, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            if isinstance(child, str) and child in targets:
                references.append(f"value:{child_path}")
                continue
            references.extend(find_state_references(child, targets, child_path))
    return references


def strip_state_references(
    value: Any,
    targets: set[str],
    path: str = "$",
) -> tuple[Any, list[str]]:
    """Remove only exact scalar/list references and ID-bearing object keys."""

    removed: list[str] = []
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if any(target in str(key) for target in targets):
                removed.append(f"key:{child_path}")
                continue
            if isinstance(child, str) and child in targets:
                removed.append(f"value:{child_path}")
                continue
            cleaned_child, child_removed = strip_state_references(
                child, targets, child_path
            )
            cleaned[key] = cleaned_child
            removed.extend(child_removed)
        return cleaned, removed
    if isinstance(value, list):
        cleaned_list: list[Any] = []
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            if isinstance(child, str) and child in targets:
                removed.append(f"value:{child_path}")
                continue
            cleaned_child, child_removed = strip_state_references(
                child, targets, child_path
            )
            cleaned_list.append(cleaned_child)
            removed.extend(child_removed)
        return cleaned_list, removed
    return value, removed


def running_related_clients(codex_home: Path | None = None) -> tuple[str, ...]:
    if os.name != "nt":
        return ()
    records = _running_related_process_records()
    if codex_home is None:
        return tuple(
            sorted({str(item["name"]) for item in records}, key=str.casefold)
        )
    return _relevant_client_names(codex_home, records)


def _running_related_process_records() -> tuple[dict[str, Any], ...]:
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if powershell is None:
        raise DesktopStateError(
            "Could not verify whether Codex Desktop clients are closed"
        )
    command = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$names=@('codex.exe','chatgpt.exe','aionui.exe','cindy.exe'); "
        "$items=@(Get-CimInstance Win32_Process | "
        "Where-Object { $names -contains $_.Name.ToLowerInvariant() } | "
        "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine); "
        "$items | ConvertTo-Json -Compress -Depth 3"
    )
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise DesktopStateError(
            "Could not verify whether Codex Desktop clients are closed"
        )
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise DesktopStateError(
            "Could not verify whether Codex Desktop clients are closed"
        ) from exc
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise DesktopStateError(
            "Could not verify whether Codex Desktop clients are closed"
        )
    records: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise DesktopStateError(
                "Could not verify whether Codex Desktop clients are closed"
            )
        try:
            process_id = int(item["ProcessId"])
            parent_process_id = int(item["ParentProcessId"])
            name = str(item["Name"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise DesktopStateError(
                "Could not verify whether Codex Desktop clients are closed"
            ) from exc
        if name.casefold() not in {
            "codex.exe",
            "chatgpt.exe",
            "aionui.exe",
            "cindy.exe",
        }:
            raise DesktopStateError(
                "Could not verify whether Codex Desktop clients are closed"
            )
        executable_path = item.get("ExecutablePath")
        command_line = item.get("CommandLine")
        records.append(
            {
                "process_id": process_id,
                "parent_process_id": parent_process_id,
                "name": name,
                "executable_path": (
                    str(executable_path).strip() if executable_path else None
                ),
                "command_line": str(command_line) if command_line else None,
            }
        )
    return tuple(records)


def _relevant_client_names(
    codex_home: Path,
    records: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return client names that might still own the target Codex store.

    A Cindy process family is ignored only when both the family identity and a
    *different existing* ``codex-home`` are proven.  Missing process metadata,
    inaccessible paths, orphaned helpers, and failed identity checks all remain
    blocking.
    """

    home = codex_home.expanduser()
    items = tuple(records)
    try:
        by_pid = {int(item["process_id"]): item for item in items}
    except (KeyError, TypeError, ValueError):
        return tuple(
            sorted({str(item.get("name") or "unknown") for item in items})
        )

    cindy_root_by_pid: dict[int, int] = {}
    for item in items:
        if str(item.get("name") or "").casefold() != "cindy.exe":
            continue
        try:
            process_id = int(item["process_id"])
            parent_id = int(item["parent_process_id"])
        except (KeyError, TypeError, ValueError):
            continue
        root_id = process_id
        seen = {process_id}
        while parent_id in by_pid:
            parent = by_pid[parent_id]
            if str(parent.get("name") or "").casefold() != "cindy.exe":
                break
            if parent_id in seen:
                root_id = -1
                break
            seen.add(parent_id)
            root_id = parent_id
            try:
                parent_id = int(parent["parent_process_id"])
            except (KeyError, TypeError, ValueError):
                root_id = -1
                break
        cindy_root_by_pid[process_id] = root_id

    valid_cindy_families: dict[int, Path] = {}
    for root_id in sorted(set(cindy_root_by_pid.values())):
        if root_id < 0:
            continue
        family = tuple(
            item
            for item in items
            if cindy_root_by_pid.get(_integer_or_minus_one(item.get("process_id")))
            == root_id
        )
        executable_paths = [
            Path(str(item["executable_path"]))
            for item in family
            if item.get("executable_path")
        ]
        if len(executable_paths) != len(family) or not executable_paths:
            continue
        if any(not path.is_absolute() for path in executable_paths):
            continue
        if any(
            _same_existing_path(executable_paths[0], candidate) is not True
            for candidate in executable_paths[1:]
        ):
            continue
        try:
            executable = executable_paths[0].resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not str(executable).casefold().endswith(
            "\\programs\\cindy\\cindy.exe"
        ):
            continue

        user_data_dirs: list[Path] = []
        for item in family:
            command_line = str(item.get("command_line") or "")
            match = re.search(
                r'--user-data-dir=(?:"([^"]+)"|(\S+))',
                command_line,
                flags=re.IGNORECASE,
            )
            if match:
                user_data_dir = Path(match.group(1) or match.group(2))
                if not user_data_dir.is_absolute():
                    user_data_dirs = []
                    break
                user_data_dirs.append(user_data_dir)
        if not user_data_dirs:
            continue
        if any(
            _same_existing_path(user_data_dirs[0], candidate) is not True
            for candidate in user_data_dirs[1:]
        ):
            continue
        valid_cindy_families[root_id] = user_data_dirs[0]

    relevant: set[str] = set()
    for item in items:
        name = str(item.get("name") or "unknown")
        name_key = name.casefold()
        separate_cindy_process = False
        process_id = _integer_or_minus_one(item.get("process_id"))
        parent_id = _integer_or_minus_one(item.get("parent_process_id"))
        if name_key == "cindy.exe":
            root_id = cindy_root_by_pid.get(process_id, -1)
            user_data_dir = valid_cindy_families.get(root_id)
            if user_data_dir is not None:
                separate_cindy_process = (
                    _same_existing_path(user_data_dir / "codex-home", home)
                    is False
                )
        elif name_key == "codex.exe" and item.get("executable_path"):
            root_id = cindy_root_by_pid.get(parent_id, -1)
            user_data_dir = valid_cindy_families.get(root_id)
            if user_data_dir is not None and (
                _same_existing_path(user_data_dir / "codex-home", home) is False
            ):
                executable_path = Path(str(item["executable_path"]))
                separate_cindy_process = (
                    executable_path.is_absolute()
                    and _is_existing_path_below(
                        executable_path,
                        user_data_dir / "codex",
                    )
                )
        if not separate_cindy_process:
            relevant.add(name)
    return tuple(sorted(relevant, key=str.casefold))


def _same_existing_path(first: Path, second: Path) -> bool | None:
    try:
        if not first.exists() or not second.exists():
            return None
        return os.path.samefile(first, second)
    except OSError:
        return None


def _is_existing_path_below(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        return resolved_path.is_relative_to(resolved_root)
    except (OSError, RuntimeError):
        return False


def _integer_or_minus_one(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discover_catalog_database(codex_home: Path) -> Path | None:
    sqlite_root = codex_home / "sqlite"
    try:
        if not sqlite_root.exists():
            return None
        if not sqlite_root.is_dir():
            raise DesktopStateError(
                f"Codex Desktop sqlite path is not a directory: {sqlite_root}"
            )
        candidates = sorted(sqlite_root.glob("*.db"), key=lambda path: path.name)
    except OSError as exc:
        raise DesktopStateError(
            f"Could not inspect Codex Desktop sqlite directory {sqlite_root}: {exc}"
        ) from exc

    matches: list[Path] = []
    failures: list[tuple[Path, str]] = []
    known_without_catalog: list[Path] = []
    known_names = {"codex-dev.db", "codex.db"}
    for path in candidates:
        try:
            with closing(connect_readonly(path)) as connection:
                if table_exists(connection, "local_thread_catalog"):
                    matches.append(path.resolve())
                elif path.name.casefold() in known_names:
                    known_without_catalog.append(path)
        except (OSError, sqlite3.Error) as exc:
            failures.append((path, str(exc)))
    if len(matches) > 1:
        raise DesktopStateError(
            "Multiple Codex Desktop catalogs were found; storage identity is "
            "ambiguous: " + ", ".join(str(path) for path in matches)
        )
    if matches:
        return matches[0]
    known_failures = [
        (path, message)
        for path, message in failures
        if path.name.casefold() in known_names
    ]
    if known_failures:
        raise DesktopStateError(
            "Could not inspect the Codex Desktop catalog candidate: "
            + "; ".join(
                f"{path}: {message}" for path, message in known_failures
            )
        )
    if known_without_catalog:
        raise DesktopStateError(
            "Codex Desktop catalog candidate is present but the required "
            "local_thread_catalog table is missing: "
            + ", ".join(str(path) for path in known_without_catalog)
        )
    return None


def _read_catalog_records(
    database: Path,
    thread_ids: set[str] | None,
) -> tuple[list[DesktopCatalogRecord], int | None]:
    try:
        with closing(connect_readonly(database)) as connection:
            columns = _table_columns(connection, "local_thread_catalog")
            missing = {"host_id", "thread_id"} - columns
            if missing:
                raise DesktopStateError(
                    f"{database} local_thread_catalog is missing column(s): "
                    + ", ".join(sorted(missing))
                )
            title_column = next(
                (
                    name
                    for name in ("display_title", "title", "name")
                    if name in columns
                ),
                None,
            )
            query = "SELECT * FROM local_thread_catalog WHERE host_id = 'local'"
            parameters: tuple[str, ...] = ()
            if thread_ids:
                placeholders = ",".join("?" for _ in thread_ids)
                query += f" AND thread_id IN ({placeholders})"
                parameters = tuple(sorted(thread_ids))
            query += " ORDER BY thread_id"
            rows = connection.execute(query, parameters).fetchall()
            records = [
                DesktopCatalogRecord(
                    host_id=str(row["host_id"]),
                    thread_id=str(row["thread_id"]),
                    title=(
                        str(row[title_column])
                        if title_column is not None
                        and row[title_column] is not None
                        else None
                    ),
                    database=database,
                    row=dict(row),
                )
                for row in rows
                if isinstance(row["thread_id"], str) and row["thread_id"]
            ]
            revision = _read_catalog_revision(connection)
    except DesktopStateError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise DesktopStateError(
            f"Could not read Codex Desktop catalog {database}: {exc}"
        ) from exc
    return records, revision


def _read_catalog_revision(connection: sqlite3.Connection) -> int | None:
    if not table_exists(connection, "local_thread_catalog_metadata"):
        return None
    columns = _table_columns(connection, "local_thread_catalog_metadata")
    if "catalog_revision" not in columns:
        return None
    row = connection.execute(
        "SELECT MAX(catalog_revision) AS revision "
        "FROM local_thread_catalog_metadata"
    ).fetchone()
    if row is None or not isinstance(row["revision"], int):
        return None
    return int(row["revision"])


def _delete_catalog_rows(database: Path, thread_ids: tuple[str, ...]) -> int:
    placeholders = ",".join("?" for _ in thread_ids)
    try:
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("BEGIN IMMEDIATE")
            columns = _table_columns(connection, "local_thread_catalog")
            if not {"host_id", "thread_id"}.issubset(columns):
                raise DesktopStateError(
                    "Codex Desktop catalog schema changed before mutation"
                )
            cursor = connection.execute(
                "DELETE FROM local_thread_catalog "
                f"WHERE host_id = 'local' AND thread_id IN ({placeholders})",
                thread_ids,
            )
            deleted = int(cursor.rowcount)
            if deleted != len(thread_ids):
                raise DesktopStateError(
                    f"Expected {len(thread_ids)} exact local catalog row(s), "
                    f"deleted {deleted}"
                )
            if table_exists(connection, "local_thread_catalog_metadata"):
                metadata_columns = _table_columns(
                    connection, "local_thread_catalog_metadata"
                )
                if "catalog_revision" in metadata_columns:
                    connection.execute(
                        "UPDATE local_thread_catalog_metadata SET "
                        "catalog_revision = catalog_revision + 1"
                    )
            connection.commit()
            return deleted
    except DesktopStateError:
        raise
    except sqlite3.Error as exc:
        raise DesktopStateError(
            f"Could not update Codex Desktop catalog {database}: {exc}"
        ) from exc


def _create_backup(
    snapshot: DesktopStateSnapshot,
    manifest: dict[str, Any],
) -> tuple[str, Path]:
    assert snapshot.database is not None
    backup_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:12]
    )
    directory = (
        snapshot.codex_home
        / ".local-agent-record-janitor"
        / "desktop-state-backups"
        / backup_id
    )
    try:
        directory.mkdir(parents=True, exist_ok=False)
        for state_path in snapshot.state_paths:
            shutil.copy2(state_path, directory / state_path.name)
        with closing(connect_readonly(snapshot.database)) as source:
            with closing(sqlite3.connect(directory / snapshot.database.name)) as target:
                source.backup(target)
        manifest["backup_id"] = backup_id
        manifest["backup_directory"] = str(directory)
        manifest_path = directory / "manifest.json"
        with manifest_path.open("w", encoding="utf-8", newline="") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        raise DesktopStateError(
            f"Could not create verified Codex Desktop backup: {exc}"
        ) from exc
    return backup_id, directory


def _restore_backup(
    snapshot: DesktopStateSnapshot,
    backup_directory: Path,
) -> str | None:
    assert snapshot.database is not None
    try:
        backup_database = backup_directory / snapshot.database.name
        with closing(connect_readonly(backup_database)) as source:
            with closing(sqlite3.connect(snapshot.database)) as destination:
                source.backup(destination)
        for state_path in snapshot.state_paths:
            shutil.copy2(backup_directory / state_path.name, state_path)
    except Exception as exc:
        return str(exc)
    return None


def _global_state_paths(codex_home: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in (
        codex_home / ".codex-global-state.json",
        codex_home / ".codex-global-state.json.bak",
    ):
        try:
            if path.exists():
                if not path.is_file():
                    raise DesktopStateError(
                        f"Codex Desktop state path is not a regular file: {path}"
                    )
                paths.append(path.resolve())
        except OSError as exc:
            raise DesktopStateError(
                f"Could not inspect Codex Desktop state path {path}: {exc}"
            ) from exc
    return tuple(paths)


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopStateError(
            f"Could not read Codex Desktop JSON state {path}: {exc}"
        ) from exc


def _atomic_write_json(path: Path, value: Any) -> None:
    original_mode = path.stat().st_mode
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.janitor-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, original_mode)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table_name})")
        if isinstance(row["name"], str)
    }


def _normalized_path(path: Path) -> str:
    return canonical_existing_path_key(path)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(child) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = [
    "DesktopCatalogRecord",
    "DesktopCleanupResult",
    "DesktopStateError",
    "DesktopStateSnapshot",
    "DesktopThreadState",
    "execute_desktop_state_cleanup",
    "find_state_references",
    "native_evidence_for_threads",
    "read_desktop_state",
    "remaining_desktop_state_markers",
    "running_related_clients",
    "sha256_file",
    "strip_state_references",
]
