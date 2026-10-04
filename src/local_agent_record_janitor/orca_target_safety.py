"""Target-scoped Orca evidence for one validated Windows managed account home.

This module never launches a product or initializes a store. Its frozen evidence
is consumed by the coordinator's exact action ticket; static capabilities and
legacy writers do not inherit that qualification.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import re
import uuid
from contextlib import closing
from datetime import datetime
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .client_contracts import describe_adapter
from .file_alias_evidence import probe_file_aliases
from .orca_discovery import local_orca_path, prove_account_home, require_plain_directory, require_plain_file
from .orca_metadata import OrcaLease, read_orca_journal
from .record_identity import canonical_path

LEGACY_EVIDENCE_SCHEMA = "larj.orca-target-safety.v1"
EVIDENCE_SCHEMA = "larj.orca-target-safety.v2"
FRONTEND_EVIDENCE_SCHEMA = "larj.orca-target-safety.v3"
_DATABASES = ("state_5.sqlite", "logs_2.sqlite", "memories_1.sqlite", "queue_1.sqlite", "goals_1.sqlite")
_FAMILY = tuple(name + suffix for name in _DATABASES for suffix in ("", "-wal", "-shm", "-journal")) + ("session_index.jsonl",)
_SIDECARS = frozenset(name for name in _FAMILY if name.endswith(("-wal", "-shm", "-journal")))
_STARTUP_PATHS = ("installation_id", "skills", "tmp", ".tmp", "thread-writer-locks")


def _rollout_boundary(home: Path, rollouts: Iterable[str], affected: Iterable[str]) -> dict:
    """Match the pinned native filename parser without reading transcripts.

    Native deletion can remove compressed siblings and discover compressed-only
    copies. This write combination therefore requires their bounded absence in
    both native roots; an unproved subtree is never treated as empty.
    """
    identities = {uuid.UUID(value) for value in affected}
    pattern = re.compile(r"rollout-([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2})-([0-9a-fA-F-]{36})(?:_([0-9a-fA-F-]{36}))?\.jsonl")
    for raw in rollouts:
        match = pattern.fullmatch(Path(raw).name)
        try:
            if match is None:
                raise ValueError
            datetime.strptime(match[1], "%Y-%m-%dT%H-%M-%S")
            if uuid.UUID(match[2]) not in identities:
                raise ValueError
            if match[3] is not None:
                uuid.UUID(match[3])
        except ValueError as exc:
            raise ValueError("orca_rollout_filename_unverified") from exc
    visited = 0
    for root in (home / "sessions", home / "archived_sessions"):
        try:
            root.lstat()
        except FileNotFoundError:
            continue
        pending = [root]
        while pending:
            directory = pending.pop()
            require_plain_directory(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 200000:
                        raise ValueError("orca_rollout_scope_budget_exceeded")
                    path = Path(entry.path)
                    info = path.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        require_plain_directory(path)
                        pending.append(path)
                    else:
                        require_plain_file(path)
                        if entry.name.casefold().endswith(".jsonl.zst"):
                            raise ValueError("orca_compressed_rollout_scope_unverified")
    return {"filename_policy": "timestamp_and_owned_uuid", "compressed_rollouts_absent": True}


def _schema_metadata(path: Path) -> dict:
    from .orca_codex_schema import SCHEMA_METADATA
    info = require_plain_file(path)
    if info.st_nlink != 1:
        raise ValueError("orca_sqlite_schema_unverified")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        schema = list(connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
        migrations = [list(row) for row in connection.execute(
            "SELECT version,description,success,hex(checksum) FROM _sqlx_migrations ORDER BY version")]
    value = {"schema_sha256": hashlib.sha256(json.dumps(schema, separators=(",", ":"), sort_keys=True).encode()).hexdigest(),
             "migrations": migrations}
    if value != SCHEMA_METADATA[path.name]:
        raise ValueError("orca_sqlite_schema_unverified")
    return value


def _startup_manifest(home: Path, *, after_start: bool, evidence_schema: str = EVIDENCE_SCHEMA) -> list[dict]:
    from .orca_codex_schema import STARTUP_FILES, STARTUP_DIRECTORIES
    legacy = evidence_schema == LEGACY_EVIDENCE_SCHEMA
    fingerprints = {} if legacy else json.loads(Path(__file__).with_name("orca_startup_0160.json").read_text(encoding="utf-8"))["files"]
    result = []
    for name in _STARTUP_PATHS + (() if legacy else (".sqlite-maintenance.lock",)):
        root = home / name
        try:
            root.lstat()
        except FileNotFoundError:
            result.append({"path": str(root), "absent": True})
            continue
        if not after_start and legacy:
            raise ValueError("orca_existing_startup_artifact_unverified")
        pending = [root]
        while pending:
            path = pending.pop()
            info = path.lstat()
            relative = path.relative_to(home).as_posix()
            bucket = re.fullmatch(r"tmp/arg0/codex-arg0[A-Za-z0-9]{6}", relative)
            ephemeral_leaf = re.fullmatch(r"tmp/arg0/codex-arg0[A-Za-z0-9]{6}/(?:\.lock|apply_patch\.bat|applypatch\.bat)", relative)
            if not after_start and (bucket or ephemeral_leaf):
                raise ValueError("orca_existing_startup_artifact_unverified")
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("orca_startup_artifact_linked")
            if stat.S_ISDIR(info.st_mode):
                if relative not in STARTUP_DIRECTORIES and not bucket:
                    raise ValueError("orca_startup_artifact_unapproved")
                require_plain_directory(path)
                pending.extend(path.iterdir())
                result.append({"path": str(path), "kind": "directory", "device_id": info.st_dev, "file_id": info.st_ino})
            else:
                maintenance_lock = not legacy and relative == ".sqlite-maintenance.lock"
                if relative not in STARTUP_FILES and not ephemeral_leaf and not maintenance_lock:
                    raise ValueError("orca_startup_artifact_unapproved")
                require_plain_file(path)
                if info.st_nlink != 1:
                    raise ValueError("orca_startup_artifact_linked")
                entry = {"path": str(path), "kind": "file", "device_id": info.st_dev, "file_id": info.st_ino}
                if not legacy:
                    if ephemeral_leaf and path.name == ".lock" or maintenance_lock:
                        # Native workers may hold exclusive byte locks. Only
                        # these exact empty coordination leaves are stat-only;
                        # other stable files and shim scripts require hashes.
                        if info.st_size != 0:
                            raise ValueError("orca_startup_content_unverified")
                        entry.update(size=0)
                    else:
                        digest = _identity(path, digest=True)["sha256"]
                        entry.update(size=info.st_size, sha256=digest)
                    if relative in fingerprints:
                        if {key: entry[key] for key in ("size", "sha256")} != fingerprints[relative]:
                            raise ValueError("orca_startup_content_unverified")
                    elif relative == "installation_id":
                        if info.st_size > 128:
                            raise ValueError("orca_installation_id_unverified")
                        try:
                            data = path.read_bytes()
                            uuid.UUID(data.decode("ascii").strip())
                            if hashlib.sha256(data).hexdigest() != digest:
                                raise ValueError
                        except (ValueError, UnicodeError):
                            raise ValueError("orca_installation_id_unverified") from None
                    elif relative == "thread-writer-locks/.coordination.lock" and info.st_size != 0:
                        raise ValueError("orca_startup_content_unverified")
                result.append(entry)
            if len(result) + len(pending) > 4096:
                raise ValueError("orca_startup_artifact_budget_exceeded")
        # Disabled remote discovery must not create sync artifacts at all.
        if name == ".tmp" and any(Path(value["path"]).name == "plugins.sync.lock" or
                                  Path(value["path"]).name.startswith("git-") for value in result):
            raise ValueError("orca_unapproved_plugin_sync_artifact")
    return sorted(result, key=lambda value: value["path"])


def recheck_startup_snapshot(home: Path, snapshot, *, evidence_schema: str = EVIDENCE_SCHEMA) -> None:
    """Keep observed stable startup objects; arg0 buckets may disappear."""
    home = local_orca_path(home)
    current = {value["path"]: value for value in _startup_manifest(home, after_start=True, evidence_schema=evidence_schema)}
    for value in snapshot:
        if value.get("absent"):
            continue
        path = local_orca_path(value["path"])
        if not _lexically_inside(path, home):
            raise ValueError("orca_startup_snapshot_invalid")
        observed = current.get(value["path"])
        relative = path.relative_to(home).as_posix()
        if observed is None and relative.startswith("tmp/arg0/codex-arg0"):
            continue
        if observed != value:
            raise ValueError("orca_startup_artifact_identity_changed")


def _windows_process_snapshot() -> tuple[dict[str, Any], ...]:
    """Toolhelp parent/name and process birth metadata only, without WMI."""
    if os.name != "nt":
        raise ValueError("orca_process_probe_unavailable")
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(kernel, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        getattr(kernel, name).restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise ValueError("orca_process_probe_failed")
    values = []
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise ValueError("orca_process_probe_failed")
        while True:
            stamp = None
            handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
            if handle:
                try:
                    times = tuple(wintypes.FILETIME() for _ in range(4))
                    if kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                        birth = times[0]
                        stamp = ((birth.dwHighDateTime << 32) | birth.dwLowDateTime) // 10000 - 11644473600000
                finally:
                    kernel.CloseHandle(handle)
            values.append({"process_id": int(entry.th32ProcessID), "parent_process_id": int(entry.th32ParentProcessID),
                           "name": entry.szExeFile, "start_time_ms": stamp})
            if len(values) > 65536:
                raise ValueError("orca_process_probe_invalid")
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise ValueError("orca_process_probe_failed")
                return tuple(values)
    finally:
        kernel.CloseHandle(snapshot)


def inspect_orca_target_processes(leases: Iterable[OrcaLease], *,
                                  records: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Reject known main processes, exact lease owners and known descendants.

    A released lease is not an acknowledgement. PID absence is one observation,
    not proof about every unknown or previously reparented writer.
    """
    leases = tuple(leases)
    errors: list[str] = []
    blocked: set[int] = set()
    by_pid: dict[int, Mapping[str, Any]] = {}
    try:
        items = tuple(records) if records is not None else _windows_process_snapshot()
        for item in items:
            if (not isinstance(item, Mapping) or type(item.get("process_id")) is not int
                    or item["process_id"] < 0 or type(item.get("parent_process_id")) is not int
                    or item["parent_process_id"] < 0 or not isinstance(item.get("name"), str)
                    or not item["name"] or item["process_id"] in by_pid):
                raise ValueError("orca_process_probe_invalid")
            by_pid[item["process_id"]] = item
        blocked.update(pid for pid, item in by_pid.items() if item["name"].casefold() == "orca.exe")
        owner_ids = {lease.owner_pid for lease in leases if lease.owner_pid is not None}
        seeds = blocked | owner_ids
        for lease in leases:
            if (lease.claim_status != "released" or lease.unreconciled or lease.runtime_kind != "native"
                    or lease.handoff_stage is not None):
                errors.append("orca_lease_not_released")
            if lease.owner_pid is None:
                continue
            if lease.owner_host != "local":
                errors.append("orca_lease_owner_host_unverified")
            current = by_pid.get(lease.owner_pid)
            if current is not None:
                # PID reuse never proves a formerly-owned child has exited.
                blocked.add(lease.owner_pid)
                stamp = current.get("start_time_ms")
                if type(stamp) is not int or lease.owner_start_time_ms is None:
                    errors.append("orca_lease_owner_identity_incomplete")
                elif stamp != lease.owner_start_time_ms:
                    errors.append("orca_lease_owner_identity_changed")
        # Also find children of an absent owner: direct parent IDs survive the
        # owner's exit on Windows. Do not limit this to branded process names.
        for _ in range(len(by_pid) + 1):
            children = {pid for pid, item in by_pid.items()
                        if item["parent_process_id"] in seeds and pid not in seeds}
            if not children:
                break
            blocked.update(children)
            seeds.update(children)
        probe_complete = True
    except (OSError, ValueError, TypeError):
        errors.append("orca_process_probe_failed")
        probe_complete = False
    if os.name != "nt":
        errors.append("orca_target_platform_unsupported")
    return {"probe_complete": probe_complete, "clients_closed": False if blocked else
            True if probe_complete and not errors else None,
            "scope": "known_windows_orca_main_lease_owner_and_descendants",
            "unknown_writers": "not_proven", "requires_clients_closed_ack": True,
            "blocking_process_ids": sorted(blocked), "errors": sorted(set(errors))}


def _identity(path: Path, *, digest: bool = False) -> dict[str, Any]:
    before = require_plain_file(path)
    result = {"path": str(path), "device_id": before.st_dev, "file_id": before.st_ino,
              "nlink": before.st_nlink}
    if not before.st_ino or before.st_nlink != 1:
        raise ValueError("orca_file_alias_unproven")
    if digest:
        fingerprint = hashlib.sha256()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("orca_file_identity_changed")
            while block := stream.read(1024 * 1024):
                fingerprint.update(block)
        after = require_plain_file(path)
        if (before.st_dev, before.st_ino, before.st_nlink, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_nlink, after.st_size, after.st_mtime_ns):
            raise ValueError("orca_file_identity_changed")
        result["sha256"] = fingerprint.hexdigest()
    return result


def _optional_identity(path: Path, *, digest: bool = False) -> dict[str, Any]:
    require_plain_directory(path.parent)
    parent = path.parent.lstat()
    try:
        path.lstat()
    except FileNotFoundError:
        require_plain_directory(path.parent)
        current = path.parent.lstat()
        if (parent.st_dev, parent.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("orca_directory_identity_changed")
        return {"path": str(path), "absent": True}
    # Any later disappearance is a failed probe, never initial absence.
    return _identity(path, digest=digest)


def _lexically_inside(path: Path, root: Path) -> bool:
    # WindowsPath comparison folds case even inside a case-sensitive folder.
    # Catalog spellings must exactly retain the approved directory entries.
    return len(path.parts) > len(root.parts) and path.parts[:len(root.parts)] == root.parts


def _system_configuration_root() -> Path:
    """Use the Windows known folder, never a caller's PROGRAMDATA override."""
    if os.name != "nt":
        raise ValueError("orca_system_configuration_unverified")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x23, None, 0, buffer) != 0 or not buffer.value:
        raise ValueError("orca_system_configuration_unverified")
    return local_orca_path(buffer.value) / "OpenAI" / "Codex"


def _absent_configuration(path: Path) -> dict[str, Any]:
    """Freeze absence and every existing plain ancestor without reading it."""
    directories = []
    missing = []
    for directory in (*reversed(path.parents), path.parent):
        # parents already includes the immediate parent.
        if directories and directories[-1]["path"] == str(directory):
            continue
        try:
            info = directory.lstat()
        except FileNotFoundError:
            missing.append(str(directory))
            continue
        if missing or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("orca_system_configuration_unverified")
        directories.append({"path": str(directory), "device_id": info.st_dev, "file_id": info.st_ino})
    try:
        path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "absent": True, "directories": directories,
                "missing_directories": list(dict.fromkeys(missing))}
    raise ValueError("orca_system_configuration_unverified")


def _startup_control(home: Path) -> dict[str, Any]:
    """Require the fixed native binary's metadata backfill to be complete."""
    with closing(sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
        rows = list(connection.execute("SELECT id,status,last_watermark,last_success_at,updated_at FROM backfill_state"))
        migration_counts = [connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("rollout_migration_state", "rollout_migration_skipped_rollouts")]
    if (len(rows) != 1 or rows[0][0] != 1 or rows[0][1] != "complete"
            or rows[0][2] is not None and not isinstance(rows[0][2], str)
            or any(type(value) is not int or value <= 0 for value in rows[0][3:])
            or any(migration_counts)):
        raise ValueError("orca_startup_backfill_unverified")
    return {"backfill_state": list(rows[0]), "rollout_migration_counts": migration_counts}


def freeze_orca_target(adapter: Any, home: Path, record_id: str, *,
                       affected_ids: Iterable[str], rollout_paths: Iterable[str | Path],
                       binary: Path | None, process_records: Iterable[Mapping[str, Any]] | None = None,
                       after_start: bool = False, readonly_recovery: bool = False,
                       evidence_schema: str = EVIDENCE_SCHEMA, frontend_evidence=None) -> dict[str, Any]:
    """Collect body-free boundaries only from the already selected scope."""
    home = local_orca_path(home)
    root = adapter.profile_root
    affected = tuple(sorted(set((record_id, *affected_ids))))
    rollouts = tuple(sorted(set(str(local_orca_path(path)) for path in rollout_paths)))
    blockers: list[str] = []
    legacy = evidence_schema == LEGACY_EVIDENCE_SCHEMA
    frozen: dict[str, Any] = {"profile_root": str(root), "home": str(home), "record_id": record_id,
                              "affected_thread_ids": list(affected), "rollout_paths": list(rollouts)}
    try:
        if evidence_schema == FRONTEND_EVIDENCE_SCHEMA:
            from . import orca_frontend
            if frontend_evidence is None or canonical_path(frontend_evidence["root"]) != canonical_path(root):
                raise ValueError("orca_frontend_evidence_invalid")
            if not readonly_recovery and orca_frontend.freeze(root, frontend_evidence["session_ids"],
                    timestamp=frontend_evidence["timestamp"]) != frontend_evidence:
                raise ValueError("orca_frontend_evidence_changed")
            frozen["frontend"] = frontend_evidence
        elif frontend_evidence is not None:
            raise ValueError("orca_frontend_evidence_requires_new_schema")
        if os.name != "nt":
            blockers.append("orca_target_platform_unsupported")
        prove_account_home(root, home)
        frozen["directories"] = [{"path": str(path), "device_id": (info := path.lstat()).st_dev,
                                   "file_id": info.st_ino} for path in (root, home, home / "sessions")]
        frozen["marker"] = _identity(home / ".orca-managed-home", digest=True)
        snapshot = adapter.snapshot_references(refresh=True)
        from .orca_cleanup import covers_error, covers_reference
        relevant_errors = [error.message for error in snapshot.errors if error.blocks_delete and
                           (error.store is None or error.store.backend == "codex" and
                            error.store.canonical_path == canonical_path(home))
                           and not (frontend_evidence is not None and covers_error(frontend_evidence, error))]
        blockers.extend(relevant_errors)
        frozen["sources"] = [_optional_identity(path) for path in snapshot.descriptor.sources]
        references = [reference.to_dict() for reference in snapshot.references
                      if reference.native_record is not None and
                      reference.native_record.store.canonical_path == canonical_path(home) and
                      reference.native_record.record_id in affected]
        frozen["references"] = references
        uncovered = [reference for reference in snapshot.references if reference.native_record is not None
            and reference.native_record.store.canonical_path == canonical_path(home)
            and reference.native_record.record_id in affected
            and not (frontend_evidence is not None and covers_reference(frontend_evidence, reference))]
        if uncovered:
            blockers.append("orca_target_retains_reference")
        if not rollouts:
            blockers.append("orca_target_rollout_scope_incomplete")
        rows, errors = read_orca_journal(root / "agent-session-journal.db", root)
        blockers.extend(error.message for error in errors if error.blocks_delete)
        related = tuple(row for row in rows if row.provider == "codex" and row.host == "local" and
                        row.wsl_distro is None and canonical_path(row.home_locator) == canonical_path(home))
        frozen["leases"] = [{"session_id": row.session_id, **row.lease.to_dict()} for row in related]
        runtime = inspect_orca_target_processes((row.lease for row in related), records=process_records)
        frozen["sqlite_schemas"] = {}
        for name in _DATABASES:
            if (home / name).exists():
                frozen["sqlite_schemas"][name] = _schema_metadata(home / name)
            elif name == "state_5.sqlite":
                blockers.append("orca_sqlite_schema_unverified")
        if not readonly_recovery:
            frozen["startup_control"] = _startup_control(home)
        if not legacy:
            # Native startup prunes expired logs across all threads. Requiring
            # an empty existing table avoids granting unrelated log deletion
            # and does not depend on wall-clock or timestamp assumptions.
            frozen["logs_startup_policy"] = "empty_before_start"
            if not after_start and (home / "logs_2.sqlite").exists():
                with closing(sqlite3.connect((home / "logs_2.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
                    if connection.execute("SELECT 1 FROM logs LIMIT 1").fetchone() is not None:
                        raise ValueError("orca_startup_logs_cleanup_unverified")
            frozen["memory_consolidation_policy"] = "no_selected_phase2_inputs"
            if (home / "memories_1.sqlite").exists():
                with closing(sqlite3.connect((home / "memories_1.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
                    for offset in range(0, len(affected), 250):
                        chunk = affected[offset:offset + 250]
                        query = "SELECT selected_for_phase2 FROM stage1_outputs WHERE thread_id IN (" + ",".join("?" for _ in chunk) + ")"
                        if any(type(row[0]) is not int or row[0] != 0 for row in connection.execute(query, chunk)):
                            raise ValueError("orca_global_memory_job_scope_unverified")
        paths = [home / name for name in _FAMILY]
        for rollout in rollouts:
            path = Path(rollout)
            if not _lexically_inside(path, home / "sessions") and not _lexically_inside(path, home / "archived_sessions"):
                raise ValueError("orca_rollout_outside_target_home")
            paths.append(path)
        if not legacy:
            frozen["rollout_boundary"] = _rollout_boundary(home, rollouts, affected)
            # This candidate can touch these optional stores during startup or
            # native deletion. Their write schemas are not registered here.
            frozen["unregistered_database_absence"] = [_absent_configuration(home / (name + suffix))
                for name in ("thread_history_1.sqlite", "memories_v2_1.sqlite", "agent_message_board_1.sqlite")
                for suffix in ("", "-wal", "-shm", "-journal")]
            frozen["index_replacement_absence"] = _absent_configuration(home / "session_index.jsonl.tmp")
        aliases = probe_file_aliases(paths, roots=(home,), omit_initially_missing=True)
        entries = [entry.to_dict() for entry in aliases.entries]
        if aliases.errors or any(not entry.probe_complete or entry.kind != "regular_file" or entry.nlink != 1
                                 for entry in aliases.entries):
            blockers.append("orca_file_alias_unproven")
        # Absent family leaves are frozen too; new links cannot hide behind
        # omit_initially_missing. Metadata contents/mtime are not authority.
        frozen["files"] = [_optional_identity(path) for path in paths]
        frozen["known_aliases"] = [{key: entry[key] for key in
                                   ("lexical_path", "kind", "device_id", "file_id", "nlink", "known_paths")}
                                  for entry in entries]
        frozen["configuration"] = [_optional_identity(home / "config.toml", digest=True)]
        if legacy:
            if not frozen["configuration"][0].get("absent"):
                blockers.append("orca_storage_configuration_unverified")
            frozen["credential_absence"] = [_absent_configuration(home / name) for name in ("auth.json", "credentials.json")]
        else:
            from .orca_configuration import validate_configuration
            validate_configuration(home / "config.toml", frozen["configuration"][0])
            # The runtime's ephemeral authentication cannot load these opaque
            # files. Freeze only identity/hash, never parse or publish secrets.
            frozen["credentials"] = [_optional_identity(home / name, digest=True)
                                     for name in ("auth.json", ".credentials.json", "credentials.json")]
            frozen["environment_configuration_absence"] = _absent_configuration(home / "environments.toml")
        frozen["forbidden_startup_absence"] = [_absent_configuration(home / name)
            for name in ("plugins.sync.lock", ".tmp/plugins.sync.lock")]
        frozen["startup_artifacts"] = _startup_manifest(home, after_start=after_start, evidence_schema=evidence_schema)
        system_root = _system_configuration_root()
        frozen["system_configuration"] = [_absent_configuration(system_root / name)
                                           for name in ("config.toml", "requirements.toml")]
        # Recovery never launches a binary. A software/policy update cannot
        # prevent read-only recovery of an earlier approved mutation.
        from .orca_runtime import invocation_policy, PINNED_BINARY_SHA256, RUNTIME_ACCEPTED
        frozen["invocation_policy"] = None if readonly_recovery else invocation_policy()
        if readonly_recovery:
            frozen["binary"] = None
        elif binary is None or binary.suffix.casefold() != ".exe":
            blockers.append("orca_binary_evidence_unavailable")
            frozen["binary"] = None
        else:
            frozen["binary"] = _identity(local_orca_path(binary), digest=True)
            if frozen["binary"].get("sha256") != PINNED_BINARY_SHA256:
                blockers.append("orca_binary_unregistered")
            if not legacy:
                from .orca_configuration import package_manifest_paths
                frozen["package_context"] = [_absent_configuration(path) for path in package_manifest_paths(local_orca_path(binary))]
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        if str(exc).startswith("orca_"):
            blockers.append(str(exc))
        blockers.append("orca_target_boundary_unproven")
        runtime = {"probe_complete": False, "clients_closed": None,
                   "requires_clients_closed_ack": True, "errors": ["orca_target_boundary_unproven"]}
    from .orca_runtime import RUNTIME_ACCEPTED
    return {"schema_version": evidence_schema, "frozen": frozen, "runtime_observation": runtime,
            "preflight_complete": not blockers, "blocker_codes": sorted(set(blockers)),
            "native_delete": RUNTIME_ACCEPTED and not blockers,
            "api_boundary": "validated_fixed_runtime" if RUNTIME_ACCEPTED else "not_validated"}


def recheck_orca_target(evidence: Mapping[str, Any], adapter: Any, *,
                        process_records: Iterable[Mapping[str, Any]] | None = None,
                        phase: str = "before_start") -> tuple[str, ...]:
    """Recheck a frozen investigation; this never authorizes a mutation.

    before_start requires exact family identity including absence. readonly
    recovery permits SQLite's own sidecar lifecycle and the validated API's
    index replacement/approved rollout absence, but never a linked or redirected
    survivor. Current software write qualification is irrelevant to recovery.
    """
    schema = evidence.get("schema_version")
    if schema not in {EVIDENCE_SCHEMA, LEGACY_EVIDENCE_SCHEMA, FRONTEND_EVIDENCE_SCHEMA} or phase not in {"before_start", "post_start", "readonly_recovery"}:
        return ("orca_target_evidence_invalid",)
    if schema == LEGACY_EVIDENCE_SCHEMA and phase != "readonly_recovery":
        return ("orca_runtime_policy_replan_required",)
    frozen = evidence.get("frozen")
    if not isinstance(frozen, Mapping):
        return ("orca_target_evidence_invalid",)
    try:
        binary = frozen.get("binary")
        current = freeze_orca_target(adapter, Path(frozen["home"]), frozen["record_id"],
                                    affected_ids=frozen["affected_thread_ids"], rollout_paths=frozen["rollout_paths"],
                                    binary=Path(binary["path"]) if binary else None, process_records=process_records,
                                    after_start=phase != "before_start", readonly_recovery=phase == "readonly_recovery",
                                    evidence_schema=schema, frontend_evidence=frozen.get("frontend"))
        old, new = dict(frozen), dict(current["frozen"])
        if phase == "readonly_recovery":
            for data in (old, new):
                data.pop("invocation_policy", None)
                data.pop("binary", None)
                data.pop("startup_control", None)
                data.pop("package_context", None)
                if schema == FRONTEND_EVIDENCE_SCHEMA:
                    # Each frontend family has its own exact read-only
                    # residual verifier and immutable child receipt.
                    data.pop("references", None)
                    data.pop("sources", None)
        # The lease is checked for current closed/released state, so an owner
        # disappearing or a claim becoming released is not identity drift.
        old.pop("leases", None)
        new.pop("leases", None)
        if phase != "before_start":
            # Runtime creation is permitted only for absent approved startup
            # objects. Every pre-existing stable object retains identity/hash.
            recheck_startup_snapshot(Path(frozen["home"]), frozen.get("startup_artifacts", ()), evidence_schema=schema)
            created_aux = {Path(entry["path"]).name for entry in old.get("files", ())
                           if entry.get("absent") and Path(entry["path"]).name in _DATABASES[1:]}
            for data in (old, new):
                data.pop("startup_artifacts", None)
                # The fixed runtime creates its accepted auxiliary databases;
                # every existing schema was checked above, never guessed.
                data.pop("sqlite_schemas", None)
                data["files"] = [entry for entry in data.get("files", ())
                                 if Path(entry["path"]).name not in _SIDECARS
                                 and Path(entry["path"]).name not in created_aux
                                 and entry["path"] not in frozen["rollout_paths"]
                                 and not (phase == "readonly_recovery" and Path(entry["path"]).name == "session_index.jsonl")]
                data["sources"] = [entry for entry in data.get("sources", ())
                                   if not entry["path"].endswith(("-wal", "-shm", "-journal"))]
                data["known_aliases"] = [entry for entry in data.get("known_aliases", ())
                                        if Path(entry["lexical_path"]).name not in _SIDECARS
                                        and Path(entry["lexical_path"]).name not in created_aux
                                        and entry["lexical_path"] not in frozen["rollout_paths"]
                                        and not (phase == "readonly_recovery" and Path(entry["lexical_path"]).name == "session_index.jsonl")]
            # Missing approved rollouts are permitted recovery observations.
            # Every survivor must retain its identity and plain/nlink boundary;
            # residual verification separately determines target absence.
            current_files = {entry["path"]: entry for entry in current["frozen"].get("files", ())}
            for entry in frozen.get("files", ()):
                if entry["path"] in frozen["rollout_paths"]:
                    observed = current_files.get(entry["path"])
                    if observed is None or observed.get("absent") and phase == "post_start" or not observed.get("absent") and observed != entry:
                        return tuple(sorted(set((*current["blocker_codes"], "orca_target_boundary_changed"))))
        codes = list(current["blocker_codes"])
        if phase != "post_start" and current["runtime_observation"]["clients_closed"] is not True:
            codes.extend(current["runtime_observation"]["errors"] or ["orca_target_process_running"])
        if old != new:
            codes.append("orca_target_boundary_changed")
        return tuple(sorted(set(codes)))
    except (KeyError, TypeError, ValueError, OSError):
        return ("orca_target_evidence_invalid",)


def plan_target_evidence(context: Any, scope: Mapping[str, Any], adapters: Iterable[Any],
                         candidates: Iterable[Any], blockers: Iterable[Mapping[str, Any]],
                         manual_catalog: Any = None) -> list[dict[str, Any]]:
    """Freeze selected unavailable targets without inventing writer actions."""
    adapters = tuple(adapters)
    owners = {store.canonical_path: adapter for adapter in adapters
              if (descriptor := describe_adapter(adapter)).client == "orca"
              for store in descriptor.native_stores if store.backend == "codex" and
              store.path.name == "home" and store.path.parent.parent == descriptor.profile_root / "codex-accounts"}
    if not owners:
        return []
    requests: list[tuple[Path, str, tuple[str, ...], tuple[Path, ...], Path | None]] = []
    inventory = getattr(context, "client_inventory", None)
    if inventory is not None:
        try:
            selected = inventory.select(project_selectors=tuple(scope.get("projects", ())),
                                        all_projects=bool(scope.get("all_projects")),
                                        record_ids=tuple(scope.get("record_ids", ())), engines=tuple(scope.get("engines", ())))
        except ValueError:
            return []  # An ambiguous selector never freezes guessed targets.
        for target in selected.targets:
            if target.engine != "codex" or target.record_key is None:
                continue
            home = target.record_key.store.path
            affected = (target.record_key.record_id, *target.descendant_thread_ids)
            records = tuple(record for record in getattr(manual_catalog, "records", ()) if canonical_path(record.codex_home) == canonical_path(home)
                            and record.thread_id in affected)
            paths = tuple(rollout.path for record in records for rollout in record.rollouts)
            hints = tuple(dict.fromkeys(hint for record in records for hint in record.codex_bin_hints))
            requests.append((home, target.record_key.record_id, affected, paths, hints[0] if len(hints) == 1 else None))
    else:
        selected_ids = {str(action.action_id) for action in candidates}
        selected_ids.update(str(blocker["action_id"]) for blocker in blockers if blocker.get("action_id"))
        storages = {str(storage.storage_id): storage for storage in context.plan.storages}
        for action in context.plan.actions:
            if str(action.action_id) not in selected_ids or str(getattr(action.kind, "value", action.kind)) != "delete_conversation":
                continue
            storage = storages.get(str(action.target.storage_id))
            if storage is None:
                continue
            home, root_id = storage.path, str(action.target.thread_id)
            affected = tuple(set((root_id, *action.impact.descendant_thread_ids, *action.impact.affected_thread_ids)))
            requests.append((home, root_id, affected, tuple(Path(path) for path in action.impact.rollout_paths), storage.codex_bin_hint))
    result = []
    seen = set()
    candidate_storages = {str(storage.storage_id): canonical_path(storage.path) for storage in context.plan.storages}
    for home, root_id, affected, paths, binary in requests:
        key = canonical_path(home)
        if key not in owners or (key, root_id) in seen:
            continue
        seen.add((key, root_id))
        action = next((a for a in candidates if a.target.thread_id == root_id
            and candidate_storages.get(str(a.target.storage_id)) == key), None)
        approved = (action.impact.external_action_payload or {}).get("orca_target_evidence") if action else None
        result.append(approved if approved is not None else freeze_orca_target(owners[key], home, root_id,
            affected_ids=affected, rollout_paths=paths, binary=binary))
    return result


def validate_document_targets(document: Mapping[str, Any]) -> None:
    """Read versions explicitly; old plans never receive new authorization."""
    from .operation_guard_sources import PLAN_V3, validate_guard_sources

    values = document.get("target_safety_evidence")
    if document.get("schema_version") != PLAN_V3:
        if values is not None:
            raise ValueError("target_safety_evidence_requires_operation_plan_v3")
        return
    if not isinstance(values, list) or not values:
        raise ValueError("orca_target_evidence_invalid")
    profiles = {canonical_path(root) for root in validate_guard_sources(document)}
    seen = set()
    for value in values:
        if (not isinstance(value, Mapping) or value.get("schema_version") not in {EVIDENCE_SCHEMA, LEGACY_EVIDENCE_SCHEMA, FRONTEND_EVIDENCE_SCHEMA}
                or not isinstance(value.get("native_delete"), bool)
                or value.get("api_boundary") not in {"not_validated", "validated_fixed_runtime"}
                or not isinstance(value.get("frozen"), Mapping)):
            raise ValueError("orca_target_evidence_invalid")
        frozen = value["frozen"]
        root, home = (local_orca_path(frozen.get(key, "")) for key in ("profile_root", "home"))
        record_id, affected, rollouts = (frozen.get(key) for key in ("record_id", "affected_thread_ids", "rollout_paths"))
        if (canonical_path(root) not in profiles or home.name != "home" or home.parent.parent != root / "codex-accounts"
                or not isinstance(record_id, str) or not record_id or not isinstance(affected, list)
                or record_id not in affected or any(not isinstance(item, str) or not item for item in affected)
                or not isinstance(rollouts, list)):
            raise ValueError("orca_target_evidence_invalid")
        for rollout in rollouts:
            path = local_orca_path(rollout)
            if not _lexically_inside(path, home / "sessions") and not _lexically_inside(path, home / "archived_sessions"):
                raise ValueError("orca_target_evidence_invalid")
        key = (canonical_path(home), record_id)
        if key in seen:
            raise ValueError("orca_target_evidence_duplicate")
        seen.add(key)
    if document.get("actions"):
        evidence_for_actions(document, document["actions"])


def evidence_for_actions(document, actions):
    """Match the approved complete closure, never an enlarged/subset ticket."""
    result = []
    frozen_actions = {value["action_id"]: value for value in document.get("actions", ())}
    for action in actions:
        kind = action.get("kind") if isinstance(action, Mapping) else str(getattr(action.kind, "value", action.kind))
        if kind == "delete_orca_frontend":
            from .orca_cleanup import validate_action
            validate_action(document, action)
            continue
        if isinstance(action, Mapping):
            action_id = action.get("action_id")
            target, impact = action.get("target", {}), action.get("impact", {})
            root_id = target.get("thread_id")
            storage_id, kind = target.get("storage_id"), action.get("kind")
            ids = (root_id, *impact.get("affected_thread_ids", ()), *impact.get("descendant_thread_ids", ()))
            paths = impact.get("rollout_paths", ())
        else:
            action_id, root_id = str(action.action_id), str(action.target.thread_id)
            storage_id, kind = str(action.target.storage_id), str(getattr(action.kind, "value", action.kind))
            ids = (root_id, *action.impact.affected_thread_ids, *action.impact.descendant_thread_ids)
            paths = action.impact.rollout_paths
        approved = frozen_actions.get(action_id)
        values = [value for value in document.get("target_safety_evidence", ())
                  if value.get("action_id") == action_id]
        if len(values) != 1 or approved is None:
            raise ValueError("orca_action_proof_missing")
        value = values[0]
        frozen = value["frozen"]
        storage = next((s for s in document.get("storages", ())
                        if s["storage_id"] == approved["target"]["storage_id"]), None)
        if (storage is None or storage_id != approved["target"]["storage_id"]
                or kind != approved.get("kind") or kind != "delete_conversation"
                or canonical_path(storage["path"]) != canonical_path(frozen["home"])
                or root_id != frozen["record_id"] or sorted(set(ids)) != frozen["affected_thread_ids"]
                or sorted(set(str(p) for p in paths)) != frozen["rollout_paths"]):
            raise ValueError("orca_action_proof_scope_changed")
        result.append(value)
    return result


def recheck_document_targets(document: Mapping[str, Any], *, phase: str = "before_start") -> tuple[str, ...]:
    """Fresh bounded apply preflight using frozen source and target locators."""
    from .adapters.orca import OrcaAdapter

    validate_document_targets(document)
    errors: list[str] = []
    adapters: dict[str, Any] = {}
    for evidence in document.get("target_safety_evidence", ()):
        root = evidence["frozen"]["profile_root"]
        key = canonical_path(root)
        adapter = adapters.setdefault(key, OrcaAdapter(profile_root=root))
        selected_phase = phase
        if phase == "before_start" and evidence.get("schema_version") == FRONTEND_EVIDENCE_SCHEMA:
            from .operation_coordinator import OperationCoordinator
            if OperationCoordinator._native_action_terminal_verified(document, evidence["action_id"]):
                selected_phase = "readonly_recovery"
        errors.extend(recheck_orca_target(evidence, adapter, phase=selected_phase))
    return tuple(sorted(set(errors)))
