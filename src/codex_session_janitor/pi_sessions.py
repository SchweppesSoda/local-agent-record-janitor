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

from .cindy_references import (
    CindyNativeReference,
    CindyReferenceFailure,
    build_cindy_reference_catalog,
)


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
    storage_kind: str = "standalone"
    cindy_profile_root: Path | None = None
    cindy_references: tuple[CindyNativeReference, ...] = ()
    reference_classification: str = "unreferenced"
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
                "storage_kind": self.storage_kind,
                "cindy_profile_root": (
                    _normalized_path(self.cindy_profile_root)
                    if self.cindy_profile_root is not None
                    else None
                ),
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
            "storage_kind": self.storage_kind,
            "cindy_profile_root": (
                _normalized_path(self.cindy_profile_root)
                if self.cindy_profile_root is not None
                else None
            ),
            "reference_classification": self.reference_classification,
            "cindy_references": [
                reference.approval_payload() for reference in self.cindy_references
            ],
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
            "reference_classification": self.reference_classification,
            "cindy_references": [
                reference.to_dict() for reference in self.cindy_references
            ],
        }


@dataclass(frozen=True)
class PiSessionCatalog:
    agent_dir: Path
    session_root: Path | None
    session_root_source: str | None = None
    records: tuple[PiSessionRecord, ...] = ()
    errors: tuple[PiInventoryFailure, ...] = ()
    storage_kind: str = "standalone"
    cindy_profile_root: Path | None = None
    frontend_only_references: tuple[CindyNativeReference, ...] = ()

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
            "storage_kind": self.storage_kind,
            "cindy_profile_root": (
                str(self.cindy_profile_root)
                if self.cindy_profile_root is not None
                else None
            ),
            "records": [record.to_dict() for record in self.records],
            "errors": [failure.to_dict() for failure in self.errors],
            "frontend_only_references": [
                reference.to_dict() for reference in self.frontend_only_references
            ],
        }


@dataclass(frozen=True)
class PiMultiRootCatalog:
    """A clean aggregate over independent standalone and Cindy Pi stores."""

    catalogs: tuple[PiSessionCatalog, ...] = ()

    @property
    def records(self) -> tuple[PiSessionRecord, ...]:
        return tuple(
            sorted(
                (record for catalog in self.catalogs for record in catalog.records),
                key=lambda record: (
                    _normalized_path(record.session_root),
                    _normalized_path(record.path),
                ),
            )
        )

    @property
    def sessions(self) -> tuple[PiSessionRecord, ...]:
        return self.records

    @property
    def errors(self) -> tuple[PiInventoryFailure, ...]:
        return tuple(failure for catalog in self.catalogs for failure in catalog.errors)

    @property
    def failures(self) -> tuple[PiInventoryFailure, ...]:
        return self.errors

    @property
    def frontend_only_references(self) -> tuple[CindyNativeReference, ...]:
        return tuple(
            reference
            for catalog in self.catalogs
            for reference in catalog.frontend_only_references
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "catalogs": [catalog.to_dict() for catalog in self.catalogs],
            "records": [record.to_dict() for record in self.records],
            "errors": [failure.to_dict() for failure in self.errors],
            "frontend_only_references": [
                reference.to_dict() for reference in self.frontend_only_references
            ],
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
    storage_kind: str = "standalone",
    cindy_profile_root: Path | str | None = None,
    cindy_references: Sequence[CindyNativeReference] = (),
    cindy_failures: Sequence[CindyReferenceFailure] = (),
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
            storage_kind=storage_kind,
            cindy_profile_root=(
                _absolute_path(Path(cindy_profile_root))
                if cindy_profile_root is not None
                else None
            ),
            frontend_only_references=tuple(
                reference
                for reference in cindy_references
                if reference.backend == "pi" and reference.native_session_id is not None
            ),
        )

    failures: list[PiInventoryFailure] = []
    profile_path = (
        _absolute_path(Path(cindy_profile_root))
        if cindy_profile_root is not None
        else None
    )
    for failure in cindy_failures:
        failures.append(
            PiInventoryFailure(
                agent_dir=paths.agent_dir,
                session_root=paths.session_root,
                source="cindy-reference",
                error_type=failure.error_type,
                message=failure.message,
                path=failure.database,
            )
        )
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

    children_by_parent: dict[str, list[Path]] = {}
    for record in records:
        if record.parent_session:
            parent_key = _normalized_path(_resolve_reference_path(record.parent_session, record.path))
            children_by_parent.setdefault(parent_key, []).append(record.path)

    matched_by_path: dict[str, list[CindyNativeReference]] = {
        _normalized_path(record.path): [] for record in records
    }
    reference_identity_failed = False
    for reference in cindy_references:
        if reference.backend != "pi" or reference.native_session_id is None:
            continue
        for record in records:
            try:
                matches = _pi_reference_matches(reference.native_session_id, record)
            except PiInventoryError:
                reference_identity_failed = True
                break
            if matches:
                matched_by_path[_normalized_path(record.path)].append(reference)
        if reference_identity_failed:
            break
    if reference_identity_failed:
        failures.append(
            PiInventoryFailure(
                agent_dir=paths.agent_dir,
                session_root=paths.session_root,
                source="cindy-reference-path",
                error_type="ReferencePathIdentityError",
                message="Unable to establish physical identity of a Cindy Pi reference path",
                path=paths.session_root,
            )
        )

    root_blockers = tuple(
        sorted({f"{failure.source}: {failure.message}" for failure in failures if failure.blocks_delete})
    )
    enriched: list[PiSessionRecord] = []
    for record in records:
        blockers = list(root_blockers)
        if record.active:
            blockers.append("PI_SESSION_FILE identifies this as the current active session")
        matched_references = tuple(
            sorted(
                matched_by_path.get(_normalized_path(record.path), ()),
                key=lambda reference: (
                    str(reference.database),
                    reference.cindy_session_id,
                    reference.reference_kind,
                    reference.boundary_created_at_ms or -1,
                    reference.boundary_id or "",
                ),
            )
        )
        live_current = any(
            reference.is_live and reference.reference_kind == "current"
            for reference in matched_references
        )
        live_historical = any(
            reference.is_live and reference.reference_kind == "agent_switch"
            for reference in matched_references
        )
        if live_current:
            blockers.append("A live Cindy session currently references this Pi session")
        if live_historical:
            blockers.append("A live Cindy session retains a historical reference to this Pi session")
        classification = (
            "inventory_incomplete"
            if reference_identity_failed
            else "live_current_reference"
            if live_current
            else "live_historical_reference"
            if live_historical
            else "deleted_frontend_reference"
            if matched_references
            else "unreferenced"
        )
        child_paths = tuple(sorted(children_by_parent.get(_normalized_path(record.path), ()), key=_normalized_path))
        enriched.append(
            replace(
                record,
                child_paths=child_paths,
                blockers=tuple(sorted(set(blockers))),
                deletable=not blockers,
                storage_kind=storage_kind,
                cindy_profile_root=profile_path,
                cindy_references=matched_references,
                reference_classification=classification,
            )
        )
    matched_reference_keys = {
        _cindy_reference_key(reference)
        for record in enriched
        for reference in record.cindy_references
    }
    frontend_only = tuple(
        sorted(
            (
                reference
                for reference in cindy_references
                if reference.backend == "pi"
                and reference.native_session_id is not None
                and _cindy_reference_key(reference) not in matched_reference_keys
            ),
            key=lambda reference: (
                str(reference.database),
                reference.cindy_session_id,
                reference.reference_kind,
                reference.boundary_created_at_ms or -1,
                reference.boundary_id or "",
            ),
        )
    )
    return PiSessionCatalog(
        agent_dir=paths.agent_dir,
        session_root=paths.session_root,
        session_root_source=paths.session_root_source,
        records=tuple(sorted(enriched, key=lambda item: _normalized_path(item.path))),
        errors=tuple(sorted(failures, key=lambda item: (item.source, _normalized_path(item.path) if item.path else "", item.message))),
        storage_kind=storage_kind,
        cindy_profile_root=profile_path,
        frontend_only_references=frontend_only,
    )


def build_pi_multi_root_catalog(
    catalogs: Sequence[PiSessionCatalog],
) -> PiMultiRootCatalog:
    """Aggregate already-built roots without merging their identities."""

    return PiMultiRootCatalog(tuple(catalogs))


def build_pi_root_qualified_catalog(
    *,
    cindy_profiles: Sequence[object] = (),
    environ: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    home: Path | str | None = None,
    agent_dir: Path | str | None = None,
    pi_root: Path | str | None = None,
    session_root: Path | str | None = None,
) -> PiSessionCatalog:
    """Build exactly one Pi root while retaining Cindy ownership guards.

    Explicit path options limit enumeration, but they are not evidence that a
    Cindy-owned store became standalone.  The effective session root is first
    resolved with Pi's normal precedence and then compared to every supplied
    Cindy ``<profile>/pi-agent-home/sessions`` root.
    """

    options = {
        "environ": environ,
        "cwd": cwd,
        "home": home,
        "agent_dir": agent_dir,
        "pi_root": pi_root,
        "session_root": session_root,
    }
    try:
        effective = resolve_pi_paths(**options)
    except Exception:
        # Preserve the ordinary structured path failure.  No action from this
        # result can be executable, so qualification cannot weaken safety.
        return build_pi_session_catalog(**options)

    grouped = _group_cindy_profiles(cindy_profiles)
    match, qualification_error = _match_cindy_pi_storage(
        effective.session_root,
        grouped,
    )
    if qualification_error is not None:
        return _qualification_failed_catalog(options)
    if match is not None:
        root, profiles = match
        return _build_cindy_pi_catalog(root, profiles, environ=environ)
    return build_pi_session_catalog(**options)


def build_pi_session_inventory(
    *,
    standalone_options: Mapping[str, Any] | None = None,
    cindy_profiles: Sequence[object] = (),
    include_standalone: bool = True,
) -> PiMultiRootCatalog:
    """Build standalone Pi plus every supplied Cindy profile Pi root.

    ``cindy_profiles`` accepts the public ``discovery.CindyProfile`` shape
    (``root`` and ``database`` attributes), keeping discovery separate from
    this deterministic inventory layer.
    """

    catalogs: list[PiSessionCatalog] = []
    grouped = _group_cindy_profiles(cindy_profiles)
    inventory_environ = dict(standalone_options or {}).get("environ")
    if include_standalone:
        standalone = build_pi_session_catalog(**dict(standalone_options or {}))
        duplicate_match: tuple[Path, list[object]] | None = None
        qualification_error: Exception | None = None
        if standalone.session_root is not None:
            duplicate_match, qualification_error = _match_cindy_pi_storage(
                standalone.session_root,
                grouped,
            )
        if qualification_error is not None:
            standalone = _block_catalog_for_qualification(standalone)
        if duplicate_match is None:
            catalogs.append(standalone)

    for _, (root, profiles) in sorted(grouped.items()):
        catalogs.append(
            _build_cindy_pi_catalog(root, profiles, environ=inventory_environ)
        )
    return build_pi_multi_root_catalog(catalogs)


def _group_cindy_profiles(
    cindy_profiles: Sequence[object],
) -> dict[str, tuple[Path, list[object]]]:
    grouped: dict[str, tuple[Path, list[object]]] = {}
    for profile in cindy_profiles:
        root_value = getattr(profile, "root", None)
        database_value = getattr(profile, "database", None)
        if not isinstance(root_value, Path) or not isinstance(database_value, Path):
            raise PiInventoryError("Cindy profiles must provide Path root and database values")
        root = _absolute_path(root_value)
        key = _normalized_path(root)
        grouped.setdefault(key, (root, []))[1].append(profile)
    return grouped


def _match_cindy_pi_storage(
    session_root: Path,
    grouped: Mapping[str, tuple[Path, list[object]]],
) -> tuple[tuple[Path, list[object]] | None, Exception | None]:
    """Compare storage by canonical path and directory file identity."""

    try:
        target = _physical_storage_identity(session_root)
        for _, (root, profiles) in sorted(grouped.items()):
            expected = _physical_storage_identity(
                root / "pi-agent-home" / "sessions"
            )
            if _same_physical_storage(target, expected):
                return (root, profiles), None
    except (OSError, RuntimeError, PiInventoryError) as exc:
        # If physical ownership cannot be established, treating the target as
        # standalone would permit an alias to bypass Cindy's live guard.
        return None, exc
    return None, None


def _physical_storage_identity(
    path: Path,
) -> tuple[str, tuple[int, int] | None]:
    absolute = _absolute_path(path)
    try:
        canonical = absolute.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PiInventoryError("Unable to canonicalize Pi session storage") from exc
    directory_identity: tuple[int, int] | None = None
    try:
        status = absolute.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PiInventoryError("Unable to inspect physical Pi session storage") from exc
    else:
        if not stat.S_ISDIR(status.st_mode):
            raise PiInventoryError("Pi session storage is not a directory")
        directory_identity = (status.st_dev, status.st_ino)
    return _normalized_path(canonical), directory_identity


def _same_physical_storage(
    left: tuple[str, tuple[int, int] | None],
    right: tuple[str, tuple[int, int] | None],
) -> bool:
    if left[0] == right[0]:
        return True
    left_stat, right_stat = left[1], right[1]
    return (
        left_stat is not None
        and right_stat is not None
        and left_stat == right_stat
        and left_stat != (0, 0)
    )


def _qualification_failed_catalog(options: Mapping[str, Any]) -> PiSessionCatalog:
    return _block_catalog_for_qualification(build_pi_session_catalog(**options))


def _block_catalog_for_qualification(
    catalog: PiSessionCatalog,
) -> PiSessionCatalog:
    failure = PiInventoryFailure(
        agent_dir=catalog.agent_dir,
        session_root=catalog.session_root,
        source="pi-storage-qualification",
        error_type="StorageQualificationError",
        message="Unable to establish physical ownership of Pi session storage",
        path=catalog.session_root,
    )
    blocker = f"{failure.source}: {failure.message}"
    return replace(
        catalog,
        records=tuple(
            replace(
                record,
                blockers=tuple(sorted(set((*record.blockers, blocker)))),
                deletable=False,
                reference_classification="inventory_incomplete",
            )
            for record in catalog.records
        ),
        errors=tuple((*catalog.errors, failure)),
    )


def _build_cindy_pi_catalog(
    root: Path,
    profiles: Sequence[object],
    *,
    environ: Mapping[str, str] | None = None,
) -> PiSessionCatalog:
    references: list[CindyNativeReference] = []
    reference_failures: list[CindyReferenceFailure] = []
    seen_databases: set[str] = set()
    for profile in profiles:
        database = getattr(profile, "database")
        database_key = _normalized_path(database)
        if database_key in seen_databases:
            continue
        seen_databases.add(database_key)
        reference_catalog = build_cindy_reference_catalog(
            database,
            profile_root=root,
        )
        references.extend(reference_catalog.references)
        reference_failures.extend(reference_catalog.failures)
    agent_home = root / "pi-agent-home"
    return build_pi_session_catalog(
        # Explicit storage arguments keep this catalog pinned to Cindy while
        # the inherited environment preserves PI_SESSION_FILE activity and
        # malformed-marker failures.
        environ=environ,
        cwd=root,
        home=root,
        agent_dir=agent_home,
        session_root=agent_home / "sessions",
        storage_kind="cindy",
        cindy_profile_root=root,
        cindy_references=references,
        cindy_failures=reference_failures,
    )


def _pi_reference_matches(native_id: str, record: PiSessionRecord) -> bool:
    if native_id == record.session_id:
        return True
    try:
        candidate = Path(native_id).expanduser()
    except (TypeError, ValueError, OSError):
        return False
    if not candidate.is_absolute():
        return False
    try:
        candidate_identity = _physical_file_identity(candidate)
        record_identity = _physical_file_identity(record.path)
    except (OSError, RuntimeError) as exc:
        raise PiInventoryError(
            "Unable to canonicalize a Cindy Pi reference path"
        ) from exc
    return _same_physical_storage(candidate_identity, record_identity)


def _physical_file_identity(
    path: Path,
) -> tuple[str, tuple[int, int] | None]:
    absolute = _absolute_path(path)
    try:
        canonical = absolute.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PiInventoryError("Unable to canonicalize Pi session file") from exc
    file_identity: tuple[int, int] | None = None
    try:
        status = absolute.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PiInventoryError("Unable to inspect physical Pi session file") from exc
    else:
        if not stat.S_ISREG(status.st_mode):
            raise PiInventoryError("Pi session reference target is not a regular file")
        file_identity = (status.st_dev, status.st_ino)
    return _normalized_path(canonical), file_identity


def _cindy_reference_key(reference: CindyNativeReference) -> tuple[object, ...]:
    return (
        _normalized_path(reference.database),
        reference.cindy_session_id,
        reference.backend,
        reference.native_session_id,
        reference.reference_kind,
        reference.boundary_id,
        reference.boundary_created_at_ms,
        reference.boundary_rewind_at_ms,
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


__all__ = [
    "PiInventoryError",
    "PiInventoryFailure",
    "PiMultiRootCatalog",
    "PiPaths",
    "PiSelectionError",
    "PiSessionCatalog",
    "PiSessionRecord",
    "build_pi_multi_root_catalog",
    "build_pi_root_qualified_catalog",
    "build_pi_session_catalog",
    "build_pi_session_inventory",
    "resolve_pi_paths",
    "select_pi_sessions",
]
