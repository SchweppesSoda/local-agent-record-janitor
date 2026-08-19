from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping


_CONTROL_DIRECTORY = ".local-agent-record-janitor"
_BACKUP_DIRECTORY = "legacy-index-backups"
_LOCK_FILE = "legacy-index.lock"
_INDEX_FILE = "session_index.jsonl"
_MANIFEST_FILE = "manifest.json"
_BACKUP_FILE = "session_index.jsonl.before"
_MANIFEST_SCHEMA_VERSION = 1
_BACKUP_ID_PATTERN = re.compile(
    r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{16}$"
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class LegacyIndexError(RuntimeError):
    """Base class for fail-closed legacy-index operations."""


class LegacyIndexSafetyError(LegacyIndexError):
    """A path or filesystem object was not safe to inspect or replace."""


class LegacyIndexInventoryError(LegacyIndexError):
    """Live Codex state could not be inventoried without ambiguity."""


class LegacyIndexSnapshotMismatch(LegacyIndexError):
    """The approved inventory is no longer current."""


class LegacyIndexLockError(LegacyIndexError):
    """Another Janitor process already owns this Codex-home lock."""


class LegacyIndexManifestError(LegacyIndexError):
    """A backup manifest or its payload failed validation."""


class LegacyIndexOperationError(LegacyIndexError):
    """A write failed after a durable backup may have been prepared.

    ``state`` is deliberately machine-readable.  ``unchanged`` means the
    target index still has its pre-operation hash, ``applied`` means it has
    the requested new hash, and ``indeterminate`` means neither assertion
    could be established safely.
    """

    def __init__(
        self,
        message: str,
        *,
        state: Literal["unchanged", "applied", "indeterminate"],
        backup_id: str | None = None,
        current_sha256: str | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.backup_id = backup_id
        self.current_sha256 = current_sha256


@dataclass(frozen=True)
class FileIdentity:
    """Portable subset of an ``lstat`` result used in reviewed snapshots."""

    device: int
    inode: int
    mode: int
    link_count: int
    size: int
    mtime_ns: int
    ctime_ns: int
    file_attributes: int | None

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        attributes = getattr(value, "st_file_attributes", None)
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            link_count=int(value.st_nlink),
            size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            ctime_ns=int(value.st_ctime_ns),
            file_attributes=(int(attributes) if attributes is not None else None),
        )

    def to_dict(self) -> dict[str, int | None]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyIndexLine:
    """One original index line, including enough data for exact review."""

    line_number: int
    sha256: str
    byte_length: int
    newline: Literal["lf", "crlf", "cr", "none"]
    has_bom: bool
    thread_id: str | None
    thread_name: str | None
    parse_status: Literal[
        "entry", "invalid_utf8", "invalid_json", "not_object", "missing_id"
    ]
    residual: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyIndexInventory:
    """Complete, reviewed inventory for one ``session_index.jsonl`` file."""

    codex_home: Path
    index_path: Path
    index_identity: FileIdentity
    original_sha256: str
    expected_sha256: str
    snapshot_fingerprint: str
    live_evidence_sha256: str
    indexed_thread_count: int
    rollout_count: int
    live_thread_count: int
    line_count: int
    entry_line_count: int
    expected_line_count: int
    malformed_line_count: int
    residual_thread_ids: tuple[str, ...]
    residual_line_count: int
    duplicate_entry_line_count: int
    duplicate_live_thread_ids: tuple[str, ...]
    duplicate_live_line_count: int
    lines: tuple[LegacyIndexLine, ...]

    @property
    def needs_repair(self) -> bool:
        return self.residual_line_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "codex_home": str(self.codex_home),
            "index_path": str(self.index_path),
            "index_identity": self.index_identity.to_dict(),
            "original_sha256": self.original_sha256,
            "expected_sha256": self.expected_sha256,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "live_evidence_sha256": self.live_evidence_sha256,
            "indexed_thread_count": self.indexed_thread_count,
            "rollout_count": self.rollout_count,
            "live_thread_count": self.live_thread_count,
            "line_count": self.line_count,
            "entry_line_count": self.entry_line_count,
            "expected_line_count": self.expected_line_count,
            "malformed_line_count": self.malformed_line_count,
            "residual_thread_ids": list(self.residual_thread_ids),
            "residual_line_count": self.residual_line_count,
            "duplicate_entry_line_count": self.duplicate_entry_line_count,
            "duplicate_live_thread_ids": list(
                self.duplicate_live_thread_ids
            ),
            "duplicate_live_line_count": self.duplicate_live_line_count,
            "needs_repair": self.needs_repair,
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass(frozen=True)
class LegacyIndexRepairResult:
    codex_home: Path
    index_path: Path
    backup_id: str
    backup_path: Path
    manifest_path: Path
    snapshot_fingerprint: str
    original_sha256: str
    new_sha256: str
    removed_thread_ids: tuple[str, ...]
    removed_line_count: int
    temporary_backup_retained: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("codex_home", "index_path", "backup_path", "manifest_path"):
            result[key] = str(result[key])
        result["removed_thread_ids"] = list(self.removed_thread_ids)
        return result


@dataclass(frozen=True)
class LegacyIndexRestoreResult:
    codex_home: Path
    index_path: Path
    source_backup_id: str
    restore_backup_id: str
    restore_backup_path: Path
    previous_sha256: str
    restored_sha256: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("codex_home", "index_path", "restore_backup_path"):
            result[key] = str(result[key])
        return result


@dataclass(frozen=True)
class _RolloutEvidence:
    relative_path: str
    archived: bool
    thread_id: str
    first_line_sha256: str
    identity: FileIdentity

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "archived": self.archived,
            "thread_id": self.thread_id,
            "first_line_sha256": self.first_line_sha256,
            "identity": self.identity.to_dict(),
        }


@dataclass(frozen=True)
class _RawInventory:
    public: LegacyIndexInventory
    original_bytes: bytes
    expected_bytes: bytes


@dataclass(frozen=True)
class _PreparedBackup:
    backup_id: str
    directory: Path
    backup_path: Path
    manifest_path: Path


def inventory_legacy_index(codex_home: Path | str) -> LegacyIndexInventory:
    """Strictly inventory live state and the legacy aggregate index.

    Unlike the normal diagnostic adapter, this function never softens a
    database, traversal, UTF-8, JSON, or ``session_meta`` error.  Such an
    error means absence of a live conversation has not been proved.
    """

    return _inventory_raw(codex_home).public


def repair_legacy_index(
    codex_home: Path | str,
    *,
    approved_snapshot_fingerprint: str,
) -> LegacyIndexRepairResult:
    """Remove exactly the residual lines approved in a fresh inventory.

    A full inventory is recomputed while holding the per-home Janitor lock.
    The target is not replaced unless its fingerprint exactly matches the
    caller's reviewed fingerprint.
    """

    if not isinstance(approved_snapshot_fingerprint, str) or not (
        approved_snapshot_fingerprint
    ):
        raise LegacyIndexSnapshotMismatch(
            "An approved legacy-index snapshot fingerprint is required"
        )

    home = _validated_home(codex_home)
    with _exclusive_home_lock(home):
        inventory = _inventory_raw(home)
        reviewed = inventory.public
        if reviewed.snapshot_fingerprint != approved_snapshot_fingerprint:
            raise LegacyIndexSnapshotMismatch(
                "Legacy-index state changed after review: approved "
                f"{approved_snapshot_fingerprint}, current "
                f"{reviewed.snapshot_fingerprint}"
            )
        if not reviewed.needs_repair:
            raise LegacyIndexInventoryError(
                "The approved legacy index contains no residual lines"
            )

        prepared: _PreparedBackup | None = None
        try:
            prepared = _prepare_backup(
                home,
                original_bytes=inventory.original_bytes,
                desired_bytes=inventory.expected_bytes,
                target_identity=reviewed.index_identity,
                operation="repair",
                approved_snapshot_fingerprint=reviewed.snapshot_fingerprint,
                source_backup_id=None,
                operation_details={
                    "residual_thread_ids": list(reviewed.residual_thread_ids),
                    "removed_lines": [
                        {
                            "line_number": line.line_number,
                            "sha256": line.sha256,
                            "thread_id": line.thread_id,
                            "thread_name": line.thread_name,
                        }
                        for line in reviewed.lines
                        if line.residual
                    ],
                },
            )
            _replace_index(
                home,
                expected_current_identity=reviewed.index_identity,
                expected_current_sha256=reviewed.original_sha256,
                desired_bytes=inventory.expected_bytes,
                desired_sha256=reviewed.expected_sha256,
                original_mode=reviewed.index_identity.mode,
                backup_id=prepared.backup_id,
            )
            _discard_prepared_backup(home, prepared)
        except LegacyIndexOperationError:
            raise
        except Exception as exc:
            state, current_hash = _determine_target_state(
                home / _INDEX_FILE,
                old_sha256=reviewed.original_sha256,
                new_sha256=reviewed.expected_sha256,
            )
            raise LegacyIndexOperationError(
                f"Legacy-index repair failed ({state}): {exc}",
                state=state,
                backup_id=(prepared.backup_id if prepared else None),
                current_sha256=current_hash,
            ) from exc

        return LegacyIndexRepairResult(
            codex_home=home,
            index_path=home / _INDEX_FILE,
            backup_id=prepared.backup_id,
            backup_path=prepared.backup_path,
            manifest_path=prepared.manifest_path,
            snapshot_fingerprint=reviewed.snapshot_fingerprint,
            original_sha256=reviewed.original_sha256,
            new_sha256=reviewed.expected_sha256,
            removed_thread_ids=reviewed.residual_thread_ids,
            removed_line_count=reviewed.residual_line_count,
            temporary_backup_retained=False,
        )


def restore_legacy_index(
    codex_home: Path | str,
    *,
    backup_id: str,
) -> LegacyIndexRestoreResult:
    """Restore one validated backup without overwriting subsequent changes."""

    if not isinstance(backup_id, str) or not _BACKUP_ID_PATTERN.fullmatch(
        backup_id
    ):
        raise LegacyIndexManifestError("Invalid legacy-index backup ID")

    home = _validated_home(codex_home)
    with _exclusive_home_lock(home):
        manifest, backup_bytes, _ = _load_backup(home, backup_id)
        index_path = home / _INDEX_FILE
        current_bytes, current_identity = _read_stable_regular_file(index_path)
        current_sha256 = _sha256(current_bytes)
        required_hash = _required_manifest_string(manifest, "new_sha256")
        if current_sha256 != required_hash:
            raise LegacyIndexSnapshotMismatch(
                "Restore refused because the current legacy index is not the "
                f"version produced by backup {backup_id}: expected "
                f"{required_hash}, current {current_sha256}"
            )

        desired_sha256 = _required_manifest_string(manifest, "original_sha256")
        if _sha256(backup_bytes) != desired_sha256:
            raise LegacyIndexManifestError(
                f"Backup payload hash does not match manifest for {backup_id}"
            )

        prepared: _PreparedBackup | None = None
        try:
            prepared = _prepare_backup(
                home,
                original_bytes=current_bytes,
                desired_bytes=backup_bytes,
                target_identity=current_identity,
                operation="restore",
                approved_snapshot_fingerprint=None,
                source_backup_id=backup_id,
                operation_details={"source_backup_id": backup_id},
            )
            _replace_index(
                home,
                expected_current_identity=current_identity,
                expected_current_sha256=current_sha256,
                desired_bytes=backup_bytes,
                desired_sha256=desired_sha256,
                original_mode=current_identity.mode,
                backup_id=prepared.backup_id,
            )
        except LegacyIndexOperationError:
            raise
        except Exception as exc:
            state, observed_hash = _determine_target_state(
                index_path,
                old_sha256=current_sha256,
                new_sha256=desired_sha256,
            )
            raise LegacyIndexOperationError(
                f"Legacy-index restore failed ({state}): {exc}",
                state=state,
                backup_id=(prepared.backup_id if prepared else None),
                current_sha256=observed_hash,
            ) from exc

        return LegacyIndexRestoreResult(
            codex_home=home,
            index_path=index_path,
            source_backup_id=backup_id,
            restore_backup_id=prepared.backup_id,
            restore_backup_path=prepared.backup_path,
            previous_sha256=current_sha256,
            restored_sha256=desired_sha256,
        )


def _inventory_raw(codex_home: Path | str) -> _RawInventory:
    home = _validated_home(codex_home)
    index_path = home / _INDEX_FILE
    original_bytes, index_identity = _read_stable_regular_file(index_path)

    indexed_ids, sqlite_evidence = _read_indexed_thread_ids(home)
    rollout_records = _read_rollout_inventory(home)
    rollout_ids = {record.thread_id for record in rollout_records}
    live_ids = indexed_ids | rollout_ids

    parsed_lines, raw_lines = _parse_index_lines(original_bytes)
    counts: dict[str, int] = {}
    for parsed in parsed_lines:
        if parsed.thread_id is not None:
            counts[parsed.thread_id] = counts.get(parsed.thread_id, 0) + 1
    residual_ids = tuple(sorted(set(counts) - live_ids))
    residual_set = set(residual_ids)

    lines = tuple(
        LegacyIndexLine(
            line_number=line.line_number,
            sha256=line.sha256,
            byte_length=line.byte_length,
            newline=line.newline,
            has_bom=line.has_bom,
            thread_id=line.thread_id,
            thread_name=line.thread_name,
            parse_status=line.parse_status,
            residual=(line.thread_id in residual_set),
        )
        for line in parsed_lines
    )
    expected_bytes = b"".join(
        raw
        for raw, line in zip(raw_lines, lines)
        if not line.residual
    )
    original_sha256 = _sha256(original_bytes)
    expected_sha256 = _sha256(expected_bytes)

    duplicate_ids = {thread_id for thread_id, count in counts.items() if count > 1}
    duplicate_live_ids = tuple(sorted(duplicate_ids & live_ids))
    duplicate_live_line_count = sum(
        counts[thread_id] for thread_id in duplicate_live_ids
    )
    malformed_count = sum(
        line.parse_status in {"invalid_utf8", "invalid_json", "not_object"}
        for line in lines
    )

    live_evidence_payload = {
        "sqlite": sqlite_evidence,
        "indexed_thread_ids": sorted(indexed_ids),
        "rollouts": [record.to_dict() for record in rollout_records],
    }
    live_evidence_sha256 = _canonical_sha256(live_evidence_payload)
    snapshot_payload = {
        "schema": "legacy-index-inventory-v1",
        "codex_home": _normalized_path(home),
        "index_identity": index_identity.to_dict(),
        "original_sha256": original_sha256,
        "expected_sha256": expected_sha256,
        "live_evidence_sha256": live_evidence_sha256,
        "lines": [line.to_dict() for line in lines],
        "residual_thread_ids": list(residual_ids),
    }
    snapshot_fingerprint = f"v1:{_canonical_sha256(snapshot_payload)}"

    public = LegacyIndexInventory(
        codex_home=home,
        index_path=index_path,
        index_identity=index_identity,
        original_sha256=original_sha256,
        expected_sha256=expected_sha256,
        snapshot_fingerprint=snapshot_fingerprint,
        live_evidence_sha256=live_evidence_sha256,
        indexed_thread_count=len(indexed_ids),
        rollout_count=len(rollout_records),
        live_thread_count=len(live_ids),
        line_count=len(lines),
        entry_line_count=sum(line.thread_id is not None for line in lines),
        expected_line_count=sum(not line.residual for line in lines),
        malformed_line_count=malformed_count,
        residual_thread_ids=residual_ids,
        residual_line_count=sum(line.residual for line in lines),
        duplicate_entry_line_count=sum(count - 1 for count in counts.values()),
        duplicate_live_thread_ids=duplicate_live_ids,
        duplicate_live_line_count=duplicate_live_line_count,
        lines=lines,
    )
    return _RawInventory(
        public=public,
        original_bytes=original_bytes,
        expected_bytes=expected_bytes,
    )


def _validated_home(codex_home: Path | str) -> Path:
    try:
        rendered = os.fspath(codex_home)
    except TypeError as exc:
        raise LegacyIndexSafetyError("Codex home must be a filesystem path") from exc
    if not rendered:
        raise LegacyIndexSafetyError("Codex home must not be empty")
    path = Path(os.path.abspath(os.path.expanduser(rendered)))
    value = _strict_lstat(path, description="Codex home")
    _reject_reparse(path, value)
    if not stat.S_ISDIR(value.st_mode):
        raise LegacyIndexSafetyError(f"Codex home is not a directory: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise LegacyIndexSafetyError(f"Could not resolve Codex home {path}: {exc}") from exc
    return resolved


def _read_indexed_thread_ids(home: Path) -> tuple[set[str], dict[str, Any]]:
    database = home / "state_5.sqlite"
    database_stat = _optional_lstat(database, description="Codex state database")
    if database_stat is None:
        return set(), {"present": False}
    _validate_regular_unique(database, database_stat)
    before_identity = FileIdentity.from_stat(database_stat)
    before_sidecars = _sqlite_sidecar_identities(home)

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{database.resolve(strict=True).as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
        ).fetchone()
        if table is None:
            raise LegacyIndexInventoryError(
                f"{database} is incompatible: required table 'threads' is missing"
            )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(threads)")
            if isinstance(row["name"], str)
        }
        if "id" not in columns:
            raise LegacyIndexInventoryError(
                f"{database} is incompatible: threads.id is missing"
            )
        rows = connection.execute("SELECT id FROM threads ORDER BY id").fetchall()
        connection.execute("COMMIT")
    except LegacyIndexError:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
        raise LegacyIndexInventoryError(
            f"Could not read a consistent threads inventory from {database}: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    ids: set[str] = set()
    for row in rows:
        thread_id = row["id"]
        if not isinstance(thread_id, str) or not thread_id:
            raise LegacyIndexInventoryError(
                f"{database} contains a non-string or empty threads.id"
            )
        ids.add(thread_id)

    after_stat = _strict_lstat(database, description="Codex state database")
    _validate_regular_unique(database, after_stat)
    after_identity = FileIdentity.from_stat(after_stat)
    after_sidecars = _sqlite_sidecar_identities(home)
    if before_identity != after_identity or before_sidecars != after_sidecars:
        raise LegacyIndexInventoryError(
            f"Codex state changed while it was being inventoried: {database}"
        )
    return ids, {
        "present": True,
        "identity": after_identity.to_dict(),
        "sidecars": after_sidecars,
    }


def _sqlite_sidecar_identities(home: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        path = home / f"state_5.sqlite{suffix}"
        value = _optional_lstat(path, description="SQLite sidecar")
        if value is None:
            result[path.name] = None
            continue
        _validate_regular_unique(path, value)
        identity = FileIdentity.from_stat(value).to_dict()
        if suffix == "-shm":
            # Attaching a read-only SQLite connection to a WAL database can
            # update the shared-memory sidecar timestamps on Windows.  That
            # self-induced clock change is not durable database drift.  Keep
            # every identity and size field strict; omit only the volatile
            # SHM timestamps from comparisons and fingerprints.
            identity["mtime_ns"] = None
            identity["ctime_ns"] = None
        result[path.name] = identity
    return result


def _read_rollout_inventory(home: Path) -> tuple[_RolloutEvidence, ...]:
    records: list[_RolloutEvidence] = []
    for directory_name, archived in (
        ("sessions", False),
        ("archived_sessions", True),
    ):
        root = home / directory_name
        root_stat = _optional_lstat(root, description="rollout directory")
        if root_stat is None:
            continue
        _reject_reparse(root, root_stat)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise LegacyIndexSafetyError(
                f"Rollout root is not a directory: {root}"
            )
        _walk_rollout_directory(
            home,
            root,
            archived=archived,
            records=records,
        )
    records.sort(key=lambda item: (item.relative_path, item.thread_id))
    return tuple(records)


def _walk_rollout_directory(
    home: Path,
    directory: Path,
    *,
    archived: bool,
    records: list[_RolloutEvidence],
) -> None:
    before = _strict_lstat(directory, description="rollout directory")
    _reject_reparse(directory, before)
    if not stat.S_ISDIR(before.st_mode):
        raise LegacyIndexSafetyError(f"Not a directory during traversal: {directory}")
    _assert_within(home, directory)
    try:
        with os.scandir(directory) as iterator:
            entries = sorted(list(iterator), key=lambda item: item.name)
    except OSError as exc:
        raise LegacyIndexInventoryError(
            f"Could not traverse rollout directory {directory}: {exc}"
        ) from exc

    for entry in entries:
        path = directory / entry.name
        _assert_within(home, path, resolve=False)
        # Some Windows Python runtimes return a deliberately incomplete
        # DirEntry stat (zero device/inode/link count).  A direct lstat is
        # required for hard-link and identity checks.
        value = _strict_lstat(path, description="rollout path")
        _reject_reparse(path, value)
        if stat.S_ISDIR(value.st_mode):
            _walk_rollout_directory(
                home,
                path,
                archived=archived,
                records=records,
            )
        elif stat.S_ISREG(value.st_mode):
            if value.st_nlink != 1:
                raise LegacyIndexSafetyError(
                    f"Hard-linked rollout path is not allowed: {path}"
                )
            if path.suffix.lower() == ".jsonl":
                first_line, identity = _read_stable_first_line(path)
                thread_id = _parse_rollout_first_line(path, first_line)
                records.append(
                    _RolloutEvidence(
                        relative_path=path.relative_to(home).as_posix(),
                        archived=archived,
                        thread_id=thread_id,
                        first_line_sha256=_sha256(first_line),
                        identity=identity,
                    )
                )
        else:
            raise LegacyIndexSafetyError(
                f"Non-regular rollout-tree entry is not allowed: {path}"
            )

    after = _strict_lstat(directory, description="rollout directory")
    if not _same_directory_state(before, after):
        raise LegacyIndexInventoryError(
            f"Rollout directory changed during traversal: {directory}"
        )


def _parse_rollout_first_line(path: Path, first_line: bytes) -> str:
    line = first_line[:-1] if first_line.endswith(b"\n") else first_line
    if line.endswith(b"\r"):
        line = line[:-1]
    try:
        decoded = line.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise LegacyIndexInventoryError(
            f"Rollout first line is not UTF-8: {path}: {exc}"
        ) from exc
    try:
        raw = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise LegacyIndexInventoryError(
            f"Rollout first line is not JSON: {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or raw.get("type") != "session_meta":
        raise LegacyIndexInventoryError(
            f"Rollout first line is not a session_meta record: {path}"
        )
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise LegacyIndexInventoryError(
            f"Rollout session_meta payload is not an object: {path}"
        )
    thread_id = payload.get("id") or payload.get("session_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise LegacyIndexInventoryError(
            f"Rollout session_meta has no non-empty id/session_id: {path}"
        )
    return thread_id


def _parse_index_lines(
    raw: bytes,
) -> tuple[tuple[LegacyIndexLine, ...], tuple[bytes, ...]]:
    raw_lines = tuple(raw.splitlines(keepends=True))
    # bytes.splitlines() has exact round-trip behavior, including CR, CRLF,
    # BOM and a missing final newline.  Assert this invariant rather than ever
    # risking a silent normalization.
    if b"".join(raw_lines) != raw:
        raise LegacyIndexInventoryError(
            "Could not split session_index.jsonl without changing its bytes"
        )
    parsed: list[LegacyIndexLine] = []
    for index, full_line in enumerate(raw_lines, start=1):
        content, newline = _split_newline(full_line)
        has_bom = index == 1 and content.startswith(b"\xef\xbb\xbf")
        parse_bytes = content[3:] if has_bom else content
        thread_id: str | None = None
        thread_name: str | None = None
        try:
            decoded = parse_bytes.decode("utf-8", errors="strict")
        except UnicodeError:
            parse_status = "invalid_utf8"
        else:
            try:
                value = json.loads(decoded)
            except json.JSONDecodeError:
                parse_status = "invalid_json"
            else:
                if not isinstance(value, dict):
                    parse_status = "not_object"
                else:
                    candidate = value.get("id")
                    if isinstance(candidate, str) and candidate:
                        parse_status = "entry"
                        thread_id = candidate
                        name = value.get("thread_name")
                        thread_name = name if isinstance(name, str) else None
                    else:
                        parse_status = "missing_id"
        parsed.append(
            LegacyIndexLine(
                line_number=index,
                sha256=_sha256(full_line),
                byte_length=len(full_line),
                newline=newline,
                has_bom=has_bom,
                thread_id=thread_id,
                thread_name=thread_name,
                parse_status=parse_status,  # type: ignore[arg-type]
                residual=False,
            )
        )
    return tuple(parsed), raw_lines


def _split_newline(
    line: bytes,
) -> tuple[bytes, Literal["lf", "crlf", "cr", "none"]]:
    if line.endswith(b"\r\n"):
        return line[:-2], "crlf"
    if line.endswith(b"\n"):
        return line[:-1], "lf"
    if line.endswith(b"\r"):
        return line[:-1], "cr"
    return line, "none"


def _prepare_backup(
    home: Path,
    *,
    original_bytes: bytes,
    desired_bytes: bytes,
    target_identity: FileIdentity,
    operation: Literal["repair", "restore"],
    approved_snapshot_fingerprint: str | None,
    source_backup_id: str | None,
    operation_details: Mapping[str, Any],
) -> _PreparedBackup:
    _, backup_root = _ensure_control_directories(home)
    backup_id = _new_backup_id()
    directory = backup_root / backup_id
    _assert_within(backup_root, directory, resolve=False)
    try:
        os.mkdir(directory, 0o700)
    except OSError as exc:
        raise LegacyIndexOperationError(
            f"Could not create backup directory {directory}: {exc}",
            state="unchanged",
            backup_id=backup_id,
        ) from exc
    _validate_private_directory(directory)

    backup_path = directory / _BACKUP_FILE
    manifest_path = directory / _MANIFEST_FILE
    original_sha256 = _sha256(original_bytes)
    desired_sha256 = _sha256(desired_bytes)
    try:
        _write_exclusive_file(backup_path, original_bytes, mode=0o600)
        saved, _ = _read_stable_regular_file(backup_path)
        if _sha256(saved) != original_sha256:
            raise LegacyIndexOperationError(
                f"Durable backup verification failed: {backup_path}",
                state="unchanged",
                backup_id=backup_id,
            )

        storage_identity = FileIdentity.from_stat(
            _strict_lstat(home, description="Codex home")
        )
        manifest_core: dict[str, Any] = {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "state": "prepared",
            "operation": operation,
            "backup_id": backup_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "storage_path": _normalized_path(home),
            "storage_identity": storage_identity.to_dict(),
            "index_relative_path": _INDEX_FILE,
            "backup_file": _BACKUP_FILE,
            "original_sha256": original_sha256,
            "original_size": len(original_bytes),
            "new_sha256": desired_sha256,
            "new_size": len(desired_bytes),
            "target_identity": target_identity.to_dict(),
            "approved_snapshot_fingerprint": approved_snapshot_fingerprint,
            "source_backup_id": source_backup_id,
            "operation_details": dict(operation_details),
        }
        manifest = dict(manifest_core)
        manifest["manifest_core_sha256"] = _canonical_sha256(manifest_core)
        manifest_bytes = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        _write_exclusive_file(manifest_path, manifest_bytes, mode=0o600)
        _fsync_directory(directory)
        _fsync_directory(backup_root)
    except LegacyIndexOperationError:
        raise
    except Exception as exc:
        raise LegacyIndexOperationError(
            f"Could not prepare durable backup {backup_id}: {exc}",
            state="unchanged",
            backup_id=backup_id,
        ) from exc
    return _PreparedBackup(
        backup_id=backup_id,
        directory=directory,
        backup_path=backup_path,
        manifest_path=manifest_path,
    )


def _discard_prepared_backup(home: Path, prepared: _PreparedBackup) -> None:
    """Delete only the exact rollback files created for a verified mutation."""

    backup_root = home / _CONTROL_DIRECTORY / _BACKUP_DIRECTORY
    try:
        if prepared.directory.parent.resolve(strict=True) != backup_root.resolve(
            strict=True
        ):
            raise LegacyIndexOperationError(
                "Temporary backup escaped its expected directory",
                state="replaced",
                backup_id=prepared.backup_id,
            )
        expected = {prepared.backup_path.name, prepared.manifest_path.name}
        actual = {path.name for path in prepared.directory.iterdir()}
        if actual != expected:
            raise LegacyIndexOperationError(
                "Temporary backup directory contains unexpected files",
                state="replaced",
                backup_id=prepared.backup_id,
            )
        for path in (prepared.backup_path, prepared.manifest_path):
            state = _strict_lstat(path, description="temporary rollback file")
            if not stat.S_ISREG(state.st_mode) or stat.S_ISLNK(state.st_mode):
                raise LegacyIndexOperationError(
                    "Temporary rollback path is not an ordinary file",
                    state="replaced",
                    backup_id=prepared.backup_id,
                )
            path.unlink()
        prepared.directory.rmdir()
        _fsync_directory(backup_root)
        try:
            backup_root.rmdir()
            _fsync_directory(backup_root.parent)
        except OSError:
            pass
    except LegacyIndexOperationError:
        raise
    except OSError as exc:
        raise LegacyIndexOperationError(
            f"Could not discard temporary rollback copy: {exc}",
            state="replaced",
            backup_id=prepared.backup_id,
        ) from exc


def _load_backup(
    home: Path,
    backup_id: str,
) -> tuple[dict[str, Any], bytes, Path]:
    control = home / _CONTROL_DIRECTORY
    backup_root = control / _BACKUP_DIRECTORY
    _validate_existing_private_directory(control)
    _validate_existing_private_directory(backup_root)
    directory = backup_root / backup_id
    _assert_within(backup_root, directory, resolve=False)
    _validate_existing_private_directory(directory)

    manifest_path = directory / _MANIFEST_FILE
    manifest_bytes, _ = _read_stable_regular_file(manifest_path)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyIndexManifestError(
            f"Could not parse backup manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise LegacyIndexManifestError(
            f"Backup manifest is not an object: {manifest_path}"
        )
    _validate_manifest(home, backup_id, manifest)

    backup_file = _required_manifest_string(manifest, "backup_file")
    if backup_file != _BACKUP_FILE or Path(backup_file).name != backup_file:
        raise LegacyIndexManifestError("Manifest backup path is not permitted")
    backup_path = directory / backup_file
    _assert_within(directory, backup_path, resolve=False)
    backup_bytes, _ = _read_stable_regular_file(backup_path)
    if len(backup_bytes) != _required_manifest_int(manifest, "original_size"):
        raise LegacyIndexManifestError("Backup payload size does not match manifest")
    if _sha256(backup_bytes) != _required_manifest_string(
        manifest, "original_sha256"
    ):
        raise LegacyIndexManifestError("Backup payload hash does not match manifest")
    return dict(manifest), backup_bytes, backup_path


def _validate_manifest(
    home: Path,
    backup_id: str,
    manifest: Mapping[str, Any],
) -> None:
    if manifest.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
        raise LegacyIndexManifestError("Unsupported backup manifest schema")
    if manifest.get("state") != "prepared":
        raise LegacyIndexManifestError("Backup manifest is not in prepared state")
    if manifest.get("operation") not in {"repair", "restore"}:
        raise LegacyIndexManifestError("Backup manifest operation is invalid")
    if manifest.get("backup_id") != backup_id:
        raise LegacyIndexManifestError("Backup ID does not match manifest")
    if manifest.get("storage_path") != _normalized_path(home):
        raise LegacyIndexManifestError("Backup belongs to a different Codex home")
    if manifest.get("index_relative_path") != _INDEX_FILE:
        raise LegacyIndexManifestError("Backup targets an unexpected path")

    current_storage = FileIdentity.from_stat(
        _strict_lstat(home, description="Codex home")
    ).to_dict()
    stored_storage = manifest.get("storage_identity")
    if not isinstance(stored_storage, dict):
        raise LegacyIndexManifestError("Manifest storage identity is missing")
    # Directory timestamp and size may legitimately change as backups are
    # added. Device/inode bind the manifest to the original storage object.
    for key in ("device", "inode"):
        if stored_storage.get(key) != current_storage.get(key):
            raise LegacyIndexManifestError(
                "Backup storage identity no longer matches Codex home"
            )

    supplied_core_hash = _required_manifest_string(
        manifest, "manifest_core_sha256"
    )
    core = dict(manifest)
    del core["manifest_core_sha256"]
    if _canonical_sha256(core) != supplied_core_hash:
        raise LegacyIndexManifestError("Backup manifest integrity hash is invalid")
    for key in ("original_sha256", "new_sha256"):
        value = _required_manifest_string(manifest, key)
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise LegacyIndexManifestError(f"Manifest {key} is invalid")
    for key in ("original_size", "new_size"):
        if _required_manifest_int(manifest, key) < 0:
            raise LegacyIndexManifestError(f"Manifest {key} is invalid")


def _replace_index(
    home: Path,
    *,
    expected_current_identity: FileIdentity,
    expected_current_sha256: str,
    desired_bytes: bytes,
    desired_sha256: str,
    original_mode: int,
    backup_id: str,
) -> None:
    index_path = home / _INDEX_FILE
    temporary = home / f".{_INDEX_FILE}.janitor-{secrets.token_hex(8)}.tmp"
    _assert_within(home, temporary, resolve=False)
    replaced = False
    try:
        descriptor = _open_exclusive(temporary, mode=stat.S_IMODE(original_mode))
        try:
            _write_all(descriptor, desired_bytes)
            if hasattr(os, "fchmod"):
                with contextlib.suppress(OSError):
                    os.fchmod(descriptor, stat.S_IMODE(original_mode))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        saved, _ = _read_stable_regular_file(temporary)
        if _sha256(saved) != desired_sha256:
            raise LegacyIndexOperationError(
                "Temporary legacy index failed hash verification",
                state="unchanged",
                backup_id=backup_id,
            )

        current_bytes, current_identity = _read_stable_regular_file(index_path)
        if (
            current_identity != expected_current_identity
            or _sha256(current_bytes) != expected_current_sha256
        ):
            raise LegacyIndexSnapshotMismatch(
                "Legacy index changed after backup preparation; replacement refused"
            )
        os.replace(temporary, index_path)
        replaced = True
        _fsync_directory(home)
        final_bytes, _ = _read_stable_regular_file(index_path)
        final_hash = _sha256(final_bytes)
        if final_hash != desired_sha256:
            raise LegacyIndexOperationError(
                "Replaced legacy index does not have the planned hash",
                state="indeterminate",
                backup_id=backup_id,
                current_sha256=final_hash,
            )
    except LegacyIndexOperationError:
        raise
    except Exception as exc:
        state, current_hash = _determine_target_state(
            index_path,
            old_sha256=expected_current_sha256,
            new_sha256=desired_sha256,
        )
        raise LegacyIndexOperationError(
            f"Atomic legacy-index replacement failed ({state}): {exc}",
            state=state,
            backup_id=backup_id,
            current_sha256=current_hash,
        ) from exc
    finally:
        if not replaced:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def _determine_target_state(
    path: Path,
    *,
    old_sha256: str,
    new_sha256: str,
) -> tuple[Literal["unchanged", "applied", "indeterminate"], str | None]:
    try:
        raw, _ = _read_stable_regular_file(path)
    except LegacyIndexError:
        return "indeterminate", None
    current = _sha256(raw)
    if current == old_sha256:
        return "unchanged", current
    if current == new_sha256:
        return "applied", current
    return "indeterminate", current


@contextlib.contextmanager
def _exclusive_home_lock(home: Path) -> Iterator[None]:
    control, _ = _ensure_control_directories(home)
    lock_path = control / _LOCK_FILE
    existing = _optional_lstat(lock_path, description="Janitor lock")
    if existing is not None:
        _validate_regular_unique(lock_path, existing)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise LegacyIndexLockError(f"Could not open Janitor lock {lock_path}: {exc}") from exc
    locked = False
    try:
        value = os.fstat(descriptor)
        _validate_regular_unique(lock_path, value)
        if value.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        _lock_descriptor(descriptor)
        locked = True
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
            sort_keys=True,
        ).encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        _write_all(descriptor, payload + b"\n")
        os.fsync(descriptor)
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                _unlock_descriptor(descriptor)
        os.close(descriptor)


def _lock_descriptor(descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError) as exc:
        raise LegacyIndexLockError(
            "Another Janitor process is using this Codex home"
        ) from exc


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _ensure_control_directories(home: Path) -> tuple[Path, Path]:
    control = home / _CONTROL_DIRECTORY
    backup_root = control / _BACKUP_DIRECTORY
    _ensure_private_directory(home, control)
    _ensure_private_directory(control, backup_root)
    return control, backup_root


def _ensure_private_directory(parent: Path, path: Path) -> None:
    _assert_within(parent, path, resolve=False)
    created = False
    try:
        os.mkdir(path, 0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise LegacyIndexSafetyError(
            f"Could not create private Janitor directory {path}: {exc}"
        ) from exc
    # Validate before chmod: chmod follows symlinks on several platforms and
    # must never be allowed to touch an attacker-selected target.
    value = _strict_lstat(path, description="private Janitor directory")
    _reject_reparse(path, value)
    if not stat.S_ISDIR(value.st_mode):
        raise LegacyIndexSafetyError(f"Private Janitor path is not a directory: {path}")
    if created:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
    _validate_private_directory(path)


def _validate_existing_private_directory(path: Path) -> None:
    value = _strict_lstat(path, description="Janitor backup directory")
    _reject_reparse(path, value)
    if not stat.S_ISDIR(value.st_mode):
        raise LegacyIndexSafetyError(f"Janitor backup path is not a directory: {path}")
    _validate_private_mode(path, value)


def _validate_private_directory(path: Path) -> None:
    value = _strict_lstat(path, description="private Janitor directory")
    _reject_reparse(path, value)
    if not stat.S_ISDIR(value.st_mode):
        raise LegacyIndexSafetyError(f"Private Janitor path is not a directory: {path}")
    _validate_private_mode(path, value)


def _validate_private_mode(path: Path, value: os.stat_result) -> None:
    if os.name != "nt" and stat.S_IMODE(value.st_mode) & 0o077:
        raise LegacyIndexSafetyError(
            f"Janitor backup directory permissions are not private: {path}"
        )


def _read_stable_regular_file(path: Path) -> tuple[bytes, FileIdentity]:
    before = _strict_lstat(path, description="regular file")
    _validate_regular_unique(path, before)
    descriptor = _open_readonly_no_follow(path)
    try:
        opened = os.fstat(descriptor)
        _validate_regular_unique(path, opened)
        if not _same_path_and_open_file(before, opened):
            raise LegacyIndexInventoryError(f"File changed while opening: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if not _same_file_state(opened, after):
            raise LegacyIndexInventoryError(f"File changed while reading: {path}")
        return b"".join(chunks), FileIdentity.from_stat(after)
    except OSError as exc:
        raise LegacyIndexInventoryError(f"Could not read {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _read_stable_first_line(path: Path) -> tuple[bytes, FileIdentity]:
    before = _strict_lstat(path, description="rollout file")
    _validate_regular_unique(path, before)
    descriptor = _open_readonly_no_follow(path)
    try:
        opened = os.fstat(descriptor)
        _validate_regular_unique(path, opened)
        if not _same_path_and_open_file(before, opened):
            raise LegacyIndexInventoryError(f"Rollout changed while opening: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            newline = chunk.find(b"\n")
            if newline >= 0:
                chunks.append(chunk[: newline + 1])
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if not _same_file_state(opened, after):
            raise LegacyIndexInventoryError(f"Rollout changed while reading: {path}")
        first_line = b"".join(chunks)
        if not first_line:
            raise LegacyIndexInventoryError(f"Rollout is empty: {path}")
        return first_line, FileIdentity.from_stat(after)
    except LegacyIndexError:
        raise
    except OSError as exc:
        raise LegacyIndexInventoryError(f"Could not read rollout {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _open_readonly_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise LegacyIndexInventoryError(f"Could not open {path}: {exc}") from exc


def _write_exclusive_file(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor = _open_exclusive(path, mode=mode)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_exclusive(path: Path, *, mode: int) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags, mode)
    except OSError as exc:
        raise LegacyIndexSafetyError(f"Could not exclusively create {path}: {exc}") from exc


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    total = 0
    while total < len(view):
        written = os.write(descriptor, view[total:])
        if written <= 0:
            raise OSError("short write")
        total += written


def _fsync_directory(path: Path) -> None:
    """Flush directory metadata where the host exposes that primitive.

    POSIX provides a direct directory ``fsync``.  On Windows, request a
    directory handle with backup semantics and call ``FlushFileBuffers``.
    Some Windows filesystems reject directory flushing even though all file
    handles were flushed; only those documented unsupported-handle errors
    are tolerated.
    """

    if os.name == "nt":
        _flush_windows_directory(path)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _flush_windows_directory(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    flush = kernel32.FlushFileBuffers
    flush.argtypes = (wintypes.HANDLE,)
    flush.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    generic_read_write = 0xC0000000
    share_read_write_delete = 0x00000007
    open_existing = 3
    backup_semantics = 0x02000000
    handle = create_file(
        os.fspath(path),
        generic_read_write,
        share_read_write_delete,
        None,
        open_existing,
        backup_semantics,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        # Directory write handles are unavailable on some Windows
        # filesystems. File fsync and atomic replacement have still occurred.
        if error in {1, 5, 6, 50}:
            return
        raise OSError(error, f"Could not open directory for flush: {path}")
    try:
        if not flush(handle):
            error = ctypes.get_last_error()
            if error not in {1, 5, 6, 50}:
                raise OSError(error, f"Could not flush directory: {path}")
    finally:
        close(handle)


def _strict_lstat(path: Path, *, description: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise LegacyIndexSafetyError(
            f"Could not lstat {description} {path}: {exc}"
        ) from exc


def _optional_lstat(path: Path, *, description: str) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LegacyIndexSafetyError(
            f"Could not lstat {description} {path}: {exc}"
        ) from exc


def _validate_regular_unique(path: Path, value: os.stat_result) -> None:
    _reject_reparse(path, value)
    if not stat.S_ISREG(value.st_mode):
        raise LegacyIndexSafetyError(f"Path is not a regular file: {path}")
    if value.st_nlink != 1:
        raise LegacyIndexSafetyError(f"Hard-linked file is not allowed: {path}")


def _reject_reparse(path: Path, value: os.stat_result) -> None:
    attributes = getattr(value, "st_file_attributes", 0)
    if stat.S_ISLNK(value.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise LegacyIndexSafetyError(
            f"Symlink, junction, or reparse-point path is not allowed: {path}"
        )


def _assert_within(root: Path, path: Path, *, resolve: bool = True) -> None:
    try:
        root_value = root.resolve(strict=True)
        if resolve:
            path_value = path.resolve(strict=True)
        else:
            path_value = Path(os.path.abspath(path))
        common = os.path.commonpath(
            (_normalized_path(root_value), _normalized_path(path_value))
        )
    except (OSError, ValueError) as exc:
        raise LegacyIndexSafetyError(
            f"Could not prove path is inside storage: {path}: {exc}"
        ) from exc
    if common != _normalized_path(root_value):
        raise LegacyIndexSafetyError(f"Path escapes controlled storage: {path}")


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return FileIdentity.from_stat(left) == FileIdentity.from_stat(right)


def _same_path_and_open_file(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare lstat and fstat without relying on representation-only fields.

    On Windows, CPython builds disagree about ``st_mode``, ``st_ctime_ns``
    precision and ``st_file_attributes`` between a path stat and a handle
    stat.  Device/inode identify the opened object; size/mtime bind the state
    read from it.  Two fstat results still use the stronger full comparison
    in ``_same_file_state``.
    """

    if not stat.S_ISREG(left.st_mode) or not stat.S_ISREG(right.st_mode):
        return False
    if left.st_size != right.st_size or left.st_mtime_ns != right.st_mtime_ns:
        return False
    if left.st_dev and right.st_dev and left.st_dev != right.st_dev:
        return False
    if left.st_ino and right.st_ino and left.st_ino != right.st_ino:
        return False
    return True


def _same_directory_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_mtime_ns,
        left.st_ctime_ns,
        getattr(left, "st_file_attributes", None),
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_mtime_ns,
        right.st_ctime_ns,
        getattr(right, "st_file_attributes", None),
    )


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(payload)


def _new_backup_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{secrets.token_hex(8)}"


def _required_manifest_string(manifest: Mapping[str, Any], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise LegacyIndexManifestError(f"Manifest {key} is missing or invalid")
    return value


def _required_manifest_int(manifest: Mapping[str, Any], key: str) -> int:
    value = manifest.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise LegacyIndexManifestError(f"Manifest {key} is missing or invalid")
    return value


__all__ = [
    "FileIdentity",
    "LegacyIndexError",
    "LegacyIndexInventory",
    "LegacyIndexInventoryError",
    "LegacyIndexLine",
    "LegacyIndexLockError",
    "LegacyIndexManifestError",
    "LegacyIndexOperationError",
    "LegacyIndexRepairResult",
    "LegacyIndexRestoreResult",
    "LegacyIndexSafetyError",
    "LegacyIndexSnapshotMismatch",
    "inventory_legacy_index",
    "repair_legacy_index",
    "restore_legacy_index",
]
