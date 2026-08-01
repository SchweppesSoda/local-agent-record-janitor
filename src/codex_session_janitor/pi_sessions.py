"""Fail-closed, read-only inventory support for Pi coding-agent sessions.

This module deliberately exposes metadata only.  Pi JSONL message bodies, tool
arguments, tool output, and authentication data are never retained in a record
or included in an error message.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence


_SUPPORTED_SESSION_VERSIONS = frozenset((1, 2, 3))
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


class PiSelectionError(ValueError):
    """Raised when a Pi record selector has no unique match."""


class PiInventoryError(RuntimeError):
    """Internal boundary error that must become a visible catalog failure."""


@dataclass(frozen=True)
class PiPaths:
    """The effective Pi data paths after Pi's documented precedence rules."""

    agent_dir: Path
    session_root: Path
    session_root_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_dir": str(self.agent_dir),
            "session_root": str(self.session_root),
            "session_root_source": self.session_root_source,
        }


@dataclass(frozen=True)
class PiInventoryFailure:
    """A visible failure that prevents an incomplete catalog from deleting data."""

    agent_dir: Path
    session_root: Path | None
    source: str
    error_type: str
    message: str
    path: Path | None = None
    blocks_delete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_dir": str(self.agent_dir),
            "session_root": str(self.session_root) if self.session_root else None,
            "source": self.source,
            "error_type": self.error_type,
            "message": self.message,
            "path": str(self.path) if self.path else None,
            "blocks_delete": self.blocks_delete,
        }


@dataclass(frozen=True)
class PiSessionRecord:
    """Metadata-only, storage-qualified snapshot of one Pi session JSONL file."""

    agent_dir: Path
    session_root: Path
    path: Path
    session_id: str
    # Pi v1 headers omit ``version``.  Keep that fact rather than inventing a
    # field that was not on disk; callers can treat ``None`` as legacy v1.
    version: int | None
    timestamp: str | None
    cwd: str | None
    parent_session: str | None
    session_name: str | None
    provider: str | None
    model: str | None
    used_openai_codex: bool
    active: bool
    child_paths: tuple[Path, ...] = ()
    deletable: bool = False
    blockers: tuple[str, ...] = ()
    stat_dev: int = 0
    stat_ino: int = 0
    stat_mode: int = 0
    stat_size: int = 0
    stat_mtime_ns: int = 0
    stat_ctime_ns: int = 0
    stat_file_attributes: int = 0
    sha256: str = ""

    @property
    def pi_root(self) -> Path:
        """Compatibility name for Pi's agent configuration directory."""

        return self.agent_dir

    @property
    def action_id(self) -> str:
        return "pi-session:v1:" + _canonical_sha256(
            {
                "agent_dir": _normalized_path(self.agent_dir),
                "session_root": _normalized_path(self.session_root),
                "path": _normalized_path(self.path),
                "session_id": self.session_id,
            }
        )

    @property
    def children(self) -> tuple[Path, ...]:
        return self.child_paths

    @property
    def delete_supported(self) -> bool:
        return self.deletable

    def approval_payload(self) -> dict[str, Any]:
        """Exact immutable file identity required before a delete is approved."""

        return {
            "schema_version": 1,
            "kind": "pi_session",
            "action_id": self.action_id,
            "agent_dir": _normalized_path(self.agent_dir),
            "session_root": _normalized_path(self.session_root),
            "session_id": self.session_id,
            "file": {
                "path": _normalized_path(self.path),
                "st_dev": self.stat_dev,
                "st_ino": self.stat_ino,
                "st_mode": self.stat_mode,
                "st_size": self.stat_size,
                "st_mtime_ns": self.stat_mtime_ns,
                "st_ctime_ns": self.stat_ctime_ns,
                "st_file_attributes": self.stat_file_attributes,
                "sha256": self.sha256,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.approval_payload(),
            "version": self.version,
            "timestamp": self.timestamp,
            "cwd": self.cwd,
            "parent_session": self.parent_session,
            "session_name": self.session_name,
            "provider": self.provider,
            "model": self.model,
            "used_openai_codex": self.used_openai_codex,
            "active": self.active,
            "child_paths": [str(path) for path in self.child_paths],
            "deletable": self.deletable,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class PiSessionCatalog:
    agent_dir: Path
    session_root: Path | None
    session_root_source: str | None = None
    records: tuple[PiSessionRecord, ...] = ()
    errors: tuple[PiInventoryFailure, ...] = ()

    @property
    def sessions(self) -> tuple[PiSessionRecord, ...]:
        return self.records

    @property
    def pi_root(self) -> Path:
        return self.agent_dir

    @property
    def failures(self) -> tuple[PiInventoryFailure, ...]:
        return self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "agent_dir": str(self.agent_dir),
            "session_root": str(self.session_root) if self.session_root else None,
            "session_root_source": self.session_root_source,
            "records": [record.to_dict() for record in self.records],
            "errors": [failure.to_dict() for failure in self.errors],
        }


def resolve_pi_paths(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    home: Path | str | None = None,
    agent_dir: Path | str | None = None,
    pi_root: Path | str | None = None,
    session_root: Path | str | None = None,
) -> PiPaths:
    """Resolve Pi paths without ever consulting ``auth.json``.

    Pi itself resolves a supplied session directory relative to its process cwd.
    This matches ``normalizePath`` plus the session manager's startup lookup in
    the current Pi main branch.  ``--session-dir`` is not observable here, so a
    caller-supplied ``session_root`` models that highest-priority override.
    """

    env = os.environ if environ is None else environ
    base = _absolute_path(Path.cwd() if cwd is None else Path(cwd))
    home_path = _absolute_path(Path.home() if home is None else Path(home))
    raw_agent_dir: Path | str
    if agent_dir is not None and pi_root is not None:
        raise PiInventoryError("Specify only one of agent_dir and pi_root")
    if agent_dir is not None:
        raw_agent_dir = agent_dir
    elif pi_root is not None:
        raw_agent_dir = pi_root
    elif env.get("PI_CODING_AGENT_DIR"):
        raw_agent_dir = env["PI_CODING_AGENT_DIR"]
    else:
        raw_agent_dir = home_path / ".pi" / "agent"
    resolved_agent_dir = _resolve_pi_path(raw_agent_dir, base, home_path)
    _require_optional_directory(resolved_agent_dir, "Pi agent directory")

    if session_root is not None:
        return PiPaths(
            agent_dir=resolved_agent_dir,
            session_root=_resolve_pi_path(session_root, base, home_path),
            session_root_source="argument",
        )
    raw_env_root = env.get("PI_CODING_AGENT_SESSION_DIR")
    if raw_env_root:
        return PiPaths(
            agent_dir=resolved_agent_dir,
            session_root=_resolve_pi_path(raw_env_root, base, home_path),
            session_root_source="environment",
        )

    project_directory = base / ".pi"
    _require_optional_directory(project_directory, "Pi project settings directory")
    project_settings = project_directory / "settings.json"
    global_settings = resolved_agent_dir / "settings.json"
    project_value = _load_session_dir_setting(project_settings)
    if project_value is not None:
        return PiPaths(
            agent_dir=resolved_agent_dir,
            session_root=_resolve_pi_path(project_value, base, home_path),
            session_root_source="project_settings",
        )
    global_value = _load_session_dir_setting(global_settings)
    if global_value is not None:
        return PiPaths(
            agent_dir=resolved_agent_dir,
            session_root=_resolve_pi_path(global_value, base, home_path),
            session_root_source="global_settings",
        )
    return PiPaths(
        agent_dir=resolved_agent_dir,
        session_root=resolved_agent_dir / "sessions",
        session_root_source="default",
    )


def build_pi_session_catalog(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    home: Path | str | None = None,
    agent_dir: Path | str | None = None,
    pi_root: Path | str | None = None,
    session_root: Path | str | None = None,
) -> PiSessionCatalog:
    """Inventory Pi JSONL files at most one project directory below the root.

    The bounded compatibility layout accepts both ``sessionDir/*.jsonl`` and
    ``sessions/<encoded-cwd>/*.jsonl``.  We intentionally reject deeper
    layouts rather than silently omitting a session.
    """

    try:
        paths = resolve_pi_paths(
            environ=environ,
            cwd=cwd,
            home=home,
            agent_dir=agent_dir,
            pi_root=pi_root,
            session_root=session_root,
        )
    except Exception as exc:
        fallback_root = agent_dir if agent_dir is not None else pi_root
        fallback_agent_dir = _absolute_path(Path(fallback_root)) if fallback_root else _absolute_path(Path.home() / ".pi" / "agent")
        return PiSessionCatalog(
            agent_dir=fallback_agent_dir,
            session_root=None,
            errors=(
                PiInventoryFailure(
                    agent_dir=fallback_agent_dir,
                    session_root=None,
                    source="pi-paths",
                    error_type=type(exc).__name__,
                    message="Unable to resolve Pi session storage paths",
                ),
            ),
        )

    failures: list[PiInventoryFailure] = []
    try:
        files = _discover_session_files(paths.session_root)
    except Exception as exc:
        failures.append(
            _failure(paths, "pi-sessions", exc, path=paths.session_root)
        )
        files = []

    active_path: Path | None = None
    env = os.environ if environ is None else environ
    raw_active_path = env.get("PI_SESSION_FILE")
    if raw_active_path:
        try:
            active_candidate = Path(raw_active_path).expanduser()
            if not active_candidate.is_absolute():
                raise PiInventoryError("PI_SESSION_FILE is not an absolute path")
            active_path = _absolute_path(active_candidate)
            try:
                active_status = active_path.lstat()
            except FileNotFoundError:
                # Pi can expose a not-yet-flushed session path.  It cannot
                # match an inventory record, but is not evidence of damage.
                pass
            else:
                if _is_link_or_reparse(active_status) or not stat.S_ISREG(active_status.st_mode):
                    raise PiInventoryError("PI_SESSION_FILE is not a regular non-link file")
        except Exception as exc:
            failures.append(_failure(paths, "pi-active-session", exc))

    records: list[PiSessionRecord] = []
    for file_path in files:
        try:
            records.append(_read_session(file_path, paths, active_path))
        except Exception as exc:
            failures.append(_failure(paths, "pi-session-jsonl", exc, path=file_path))

    # UUIDs are selectors in Pi.  A duplicate makes a record unsafe to target.
    by_id: dict[str, list[PiSessionRecord]] = {}
    for record in records:
        by_id.setdefault(record.session_id, []).append(record)
    for duplicate_records in by_id.values():
        if len(duplicate_records) > 1:
            paths_text = ", ".join(_normalized_path(item.path) for item in duplicate_records)
            for item in duplicate_records:
                failures.append(
                    PiInventoryFailure(
                        agent_dir=paths.agent_dir,
                        session_root=paths.session_root,
                        source="pi-session-id",
                        error_type="DuplicateSessionId",
                        message=f"Session id is duplicated across catalog paths: {paths_text}",
                        path=item.path,
                    )
                )

    children_by_parent: dict[str, list[Path]] = {}
    for record in records:
        if record.parent_session:
            parent_key = _normalized_path(_resolve_reference_path(record.parent_session, record.path))
            children_by_parent.setdefault(parent_key, []).append(record.path)

    root_blockers = tuple(
        sorted({f"{failure.source}: {failure.message}" for failure in failures if failure.blocks_delete})
    )
    enriched: list[PiSessionRecord] = []
    duplicate_ids = {key for key, value in by_id.items() if len(value) > 1}
    for record in records:
        blockers = list(root_blockers)
        if record.active:
            blockers.append("PI_SESSION_FILE identifies this as the current active session")
        if record.session_id in duplicate_ids:
            blockers.append("Session id is duplicated")
        child_paths = tuple(sorted(children_by_parent.get(_normalized_path(record.path), ()), key=_normalized_path))
        enriched.append(
            replace(
                record,
                child_paths=child_paths,
                blockers=tuple(sorted(set(blockers))),
                deletable=not blockers,
            )
        )
    return PiSessionCatalog(
        agent_dir=paths.agent_dir,
        session_root=paths.session_root,
        session_root_source=paths.session_root_source,
        records=tuple(sorted(enriched, key=lambda item: _normalized_path(item.path))),
        errors=tuple(sorted(failures, key=lambda item: (item.source, _normalized_path(item.path) if item.path else "", item.message))),
    )


def select_pi_sessions(
    catalog: PiSessionCatalog,
    selectors: Sequence[str],
) -> tuple[PiSessionRecord, ...]:
    """Resolve exact action IDs, paths, UUIDs, or an unambiguous prefix."""

    if not selectors:
        return catalog.records
    selected: list[PiSessionRecord] = []
    selected_ids: set[str] = set()
    for raw_selector in selectors:
        selector = raw_selector.strip()
        if not selector:
            raise PiSelectionError("Pi session selector must not be blank")
        exact = [
            record
            for record in catalog.records
            if selector in (record.action_id, record.session_id, _normalized_path(record.path))
        ]
        matches = exact or [
            record
            for record in catalog.records
            if record.action_id.startswith(selector)
            or record.session_id.startswith(selector)
            or _normalized_path(record.path).startswith(selector)
        ]
        if not matches:
            raise PiSelectionError(f"No Pi session matches selector {selector!r}")
        if len(matches) != 1:
            raise PiSelectionError(f"Pi session selector {selector!r} is ambiguous across {len(matches)} sessions")
        record = matches[0]
        if record.action_id not in selected_ids:
            selected_ids.add(record.action_id)
            selected.append(record)
    return tuple(selected)


def _discover_session_files(root: Path) -> list[Path]:
    try:
        root.lstat()
    except FileNotFoundError:
        # Pi's SessionManager.list/listAll also treats an as-yet uncreated
        # session directory as an empty store.
        return []
    except OSError as exc:
        raise PiInventoryError("Unable to inspect Pi session root") from exc
    _require_directory(root, "Pi session root")
    files: list[Path] = []
    for child in _safe_iterdir(root):
        child_status = _safe_lstat(child)
        if _is_link_or_reparse(child_status):
            raise PiInventoryError("Pi session root contains a symlink or reparse point")
        if stat.S_ISREG(child_status.st_mode):
            if child.suffix.lower() == ".jsonl":
                files.append(_require_contained_file(child, root))
            continue
        if not stat.S_ISDIR(child_status.st_mode):
            if child.suffix.lower() == ".jsonl":
                raise PiInventoryError("Pi JSONL path is not a regular file")
            continue
        for grandchild in _safe_iterdir(child):
            status = _safe_lstat(grandchild)
            if _is_link_or_reparse(status):
                raise PiInventoryError("Pi session layout contains a symlink or reparse point")
            if stat.S_ISDIR(status.st_mode):
                raise PiInventoryError("Pi session layout is deeper than one project directory")
            if grandchild.suffix.lower() == ".jsonl":
                if not stat.S_ISREG(status.st_mode):
                    raise PiInventoryError("Pi JSONL path is not a regular file")
                files.append(_require_contained_file(grandchild, root))
    return sorted(set(files), key=_normalized_path)


def _read_session(path: Path, paths: PiPaths, active_path: Path | None) -> PiSessionRecord:
    before = _safe_lstat(path)
    if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise PiInventoryError("Pi JSONL path is not a regular non-link file")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PiInventoryError("Unable to read Pi JSONL file") from exc
    after = _safe_lstat(path)
    if _stat_identity(before) != _stat_identity(after):
        raise PiInventoryError("Pi JSONL file changed while it was being inventoried")
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PiInventoryError("Pi JSONL file is not UTF-8") from exc

    header: Mapping[str, Any] | None = None
    session_name: str | None = None
    provider: str | None = None
    model: str | None = None
    used_openai_codex = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PiInventoryError(f"Pi JSONL has invalid JSON at line {line_number}") from exc
        if not isinstance(entry, dict):
            raise PiInventoryError(f"Pi JSONL entry at line {line_number} is not an object")
        if header is None:
            header = entry
            continue
        entry_type = entry.get("type")
        if entry_type == "session_info" and isinstance(entry.get("name"), str):
            session_name = entry["name"]
        entry_provider, entry_model = _entry_model(entry)
        if entry_provider is not None:
            provider = entry_provider
        if entry_model is not None:
            model = entry_model
        if entry_provider and entry_provider.lower() == "openai-codex":
            used_openai_codex = True
    if header is None:
        raise PiInventoryError("Pi JSONL file is empty")
    if header.get("type") != "session":
        raise PiInventoryError("Pi JSONL first entry is not a session header")
    raw_version = header.get("version")
    if raw_version is not None and (
        not isinstance(raw_version, int)
        or isinstance(raw_version, bool)
        or raw_version not in _SUPPORTED_SESSION_VERSIONS
    ):
        raise PiInventoryError("Pi session header has an unsupported version")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise PiInventoryError("Pi session header has no valid id")
    header_timestamp = header.get("timestamp")
    if header_timestamp is not None and not isinstance(header_timestamp, str):
        raise PiInventoryError("Pi session header timestamp is invalid")
    header_cwd = header.get("cwd")
    if header_cwd is not None and not isinstance(header_cwd, str):
        raise PiInventoryError("Pi session header cwd is invalid")
    parent_session = header.get("parentSession")
    if parent_session is not None and not isinstance(parent_session, str):
        raise PiInventoryError("Pi session header parentSession is invalid")
    return PiSessionRecord(
        agent_dir=paths.agent_dir,
        session_root=paths.session_root,
        path=path,
        session_id=session_id,
        version=raw_version,
        timestamp=header_timestamp,
        cwd=header_cwd,
        parent_session=parent_session,
        session_name=session_name,
        provider=provider,
        model=model,
        used_openai_codex=used_openai_codex,
        active=active_path is not None and _normalized_path(path) == _normalized_path(active_path),
        stat_dev=before.st_dev,
        stat_ino=before.st_ino,
        stat_mode=before.st_mode,
        stat_size=before.st_size,
        stat_mtime_ns=before.st_mtime_ns,
        stat_ctime_ns=before.st_ctime_ns,
        stat_file_attributes=getattr(before, "st_file_attributes", 0),
        sha256=digest,
    )


def _entry_model(entry: Mapping[str, Any]) -> tuple[str | None, str | None]:
    if entry.get("type") == "model_change":
        provider = entry.get("provider")
        model = entry.get("modelId")
        return (provider if isinstance(provider, str) else None, model if isinstance(model, str) else None)
    if entry.get("type") == "message":
        message = entry.get("message")
        if isinstance(message, Mapping) and message.get("role") == "assistant":
            provider = message.get("provider")
            model = message.get("model")
            return (provider if isinstance(provider, str) else None, model if isinstance(model, str) else None)
    return None, None


def _load_session_dir_setting(path: Path) -> str | None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PiInventoryError("Unable to inspect Pi settings.json") from exc
    if _is_link_or_reparse(status) or not stat.S_ISREG(status.st_mode):
        raise PiInventoryError("Pi settings.json is not a regular non-link file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiInventoryError("Pi settings.json cannot be parsed") from exc
    if not isinstance(value, dict):
        raise PiInventoryError("Pi settings.json root is not an object")
    session_dir = value.get("sessionDir")
    if session_dir is None:
        return None
    if not isinstance(session_dir, str) or not session_dir.strip():
        raise PiInventoryError("Pi settings.json sessionDir is invalid")
    return session_dir


def _resolve_pi_path(value: Path | str, base: Path, home: Path) -> Path:
    raw = str(value)
    if raw == "~":
        raw = str(home)
    elif raw.startswith("~/") or raw.startswith("~\\"):
        raw = str(home / raw[2:])
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    return _absolute_path(candidate)


def _resolve_reference_path(value: str, session_path: Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = session_path.parent / candidate
    return _absolute_path(candidate)


def _require_directory(path: Path, label: str) -> None:
    status = _safe_lstat(path)
    if _is_link_or_reparse(status) or not stat.S_ISDIR(status.st_mode):
        raise PiInventoryError(f"{label} is not a directory or is a link")


def _require_optional_directory(path: Path, label: str) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PiInventoryError(f"Unable to inspect {label}") from exc
    if _is_link_or_reparse(status) or not stat.S_ISDIR(status.st_mode):
        raise PiInventoryError(f"{label} is not a directory or is a link")


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError as exc:
        raise PiInventoryError("Unable to enumerate Pi session directory") from exc


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError as exc:
        raise PiInventoryError("Expected Pi session path no longer exists") from exc
    except OSError as exc:
        raise PiInventoryError("Unable to inspect Pi session path") from exc


def _require_contained_file(path: Path, root: Path) -> Path:
    # Keep the lexical pathname as the record identity.  This must happen
    # before any resolution can follow a link.  The caller already lstat'ed
    # every relevant path and rejected link/reparse entries.
    lexical = _absolute_path(path)
    resolved = lexical.resolve(strict=False)
    root_resolved = _absolute_path(root).resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PiInventoryError("Pi session path escapes the configured session root") from exc
    return lexical


def _is_link_or_reparse(status: os.stat_result) -> bool:
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(attributes & _REPARSE_POINT)


def _stat_identity(status: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
        getattr(status, "st_file_attributes", 0),
    )


def _failure(paths: PiPaths, source: str, exc: Exception, *, path: Path | None = None) -> PiInventoryFailure:
    # Do not serialize exception details: JSON decoder errors can embed user text.
    message = str(exc) if isinstance(exc, PiInventoryError) else "Pi session inventory operation failed"
    return PiInventoryFailure(
        agent_dir=paths.agent_dir,
        session_root=paths.session_root,
        source=source,
        error_type=type(exc).__name__,
        message=message,
        path=path,
    )


def _absolute_path(path: Path) -> Path:
    # ``Path.resolve`` follows symlinks, which is specifically unsafe for a
    # delete approval identity.  Make an absolute, normalized lexical path.
    return Path(os.path.abspath(str(path.expanduser())))


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(_absolute_path(path)))


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
