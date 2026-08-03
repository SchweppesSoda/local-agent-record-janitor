"""Fail-closed, metadata-only inventory for Claude Code sessions.

Claude Code does not expose a local per-session deletion API.  This module
therefore records an exact, storage-qualified manifest that can be approved by
the separate :mod:`claude_delete` executor.  Transcript bodies are parsed a
line at a time and are never retained in public records or error messages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_AUXILIARY_ROOTS = ("file-history", "tasks", "debug", "session-env", "todos")
_MAX_JSONL_LINE = 16 * 1024 * 1024
_SAFE_TODO_AGENT_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def _auxiliary_target_name_matches(
    root_name: str, name: str, session_id: str
) -> bool:
    """Match only documented or legacy exact session-owned aux targets."""
    if root_name not in _AUXILIARY_ROOTS:
        return False
    if name == session_id:
        return True
    if root_name == "debug" and name == session_id + ".txt":
        return True
    if root_name != "todos":
        return False
    prefix = session_id + "-agent-"
    suffix = ".json"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return False
    token = name[len(prefix):-len(suffix)]
    return _SAFE_TODO_AGENT_TOKEN.fullmatch(token) is not None


def _auxiliary_pattern_requires_file(
    root_name: str, name: str, session_id: str
) -> bool:
    return name != session_id and _auxiliary_target_name_matches(
        root_name, name, session_id
    )


class ClaudeSelectionError(ValueError):
    """Raised when a Claude session selector has no unique match."""


class ClaudeInventoryError(RuntimeError):
    """A storage boundary could not be inventoried safely."""


@dataclass(frozen=True)
class ClaudePaths:
    config_dir: Path
    config_dir_source: str

    def to_dict(self) -> dict[str, Any]:
        return {"config_dir": str(self.config_dir), "config_dir_source": self.config_dir_source}


@dataclass(frozen=True)
class ClaudeInventoryFailure:
    config_dir: Path
    source: str
    error_type: str
    message: str
    path: Path | None = None
    blocks_delete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_dir": str(self.config_dir),
            "source": self.source,
            "error_type": self.error_type,
            "message": self.message,
            "path": str(self.path) if self.path else None,
            "blocks_delete": self.blocks_delete,
        }


@dataclass(frozen=True)
class ClaudeManifestEntry:
    """One immutable file or directory in a session-owned deletion scope."""

    path: Path
    relative_path: str
    node_type: str  # file | directory
    stat_dev: int
    stat_ino: int
    stat_mode: int
    stat_size: int
    stat_mtime_ns: int
    stat_ctime_ns: int
    stat_file_attributes: int
    sha256: str | None = None

    def approval_payload(self) -> dict[str, Any]:
        return {
            "path": _normalized_path(self.path),
            "relative_path": self.relative_path,
            "node_type": self.node_type,
            "stat": {
                "dev": self.stat_dev,
                "ino": self.stat_ino,
                "mode": self.stat_mode,
                "size": self.stat_size,
                "mtime_ns": self.stat_mtime_ns,
                "ctime_ns": self.stat_ctime_ns,
                "file_attributes": self.stat_file_attributes,
            },
            "sha256": self.sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.approval_payload()


@dataclass(frozen=True)
class ClaudeSessionRecord:
    config_dir: Path
    config_dir_source: str
    session_id: str
    transcript_paths: tuple[Path, ...]
    project_paths: tuple[Path, ...]
    manifest: tuple[ClaudeManifestEntry, ...]
    frontend_references: tuple[Any, ...] = ()
    frontend_reference_snapshot: tuple[Mapping[str, Any], ...] = ()
    classification: str = "unreferenced"
    deletable: bool = True
    blockers: tuple[str, ...] = ()
    transcript_line_count: int = 0
    transcript_size: int = 0

    @property
    def action_id(self) -> str:
        return "claude-session:v1:" + _canonical_sha256(
            {"config_dir": _normalized_path(self.config_dir), "session_id": self.session_id}
        )

    @property
    def delete_supported(self) -> bool:
        return self.deletable

    @property
    def references(self) -> tuple[Any, ...]:
        return self.frontend_references

    @property
    def cindy_references(self) -> tuple[Any, ...]:
        return self.frontend_references

    @property
    def reference_classification(self) -> str:
        return self.classification

    def approval_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "claude_session",
            "action_id": self.action_id,
            "config_dir": _normalized_path(self.config_dir),
            "session_id": self.session_id,
            "transcript_paths": [_normalized_path(path) for path in self.transcript_paths],
            "manifest": [entry.approval_payload() for entry in self.manifest],
            "frontend_reference_snapshot": [dict(item) for item in self.frontend_reference_snapshot],
            "classification": self.classification,
            "deletable": self.deletable,
            "blockers": list(self.blockers),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.approval_payload(),
            "config_dir_source": self.config_dir_source,
            "project_paths": [str(path) for path in self.project_paths],
            "transcript_line_count": self.transcript_line_count,
            "transcript_size": self.transcript_size,
        }


@dataclass(frozen=True)
class ClaudeSessionCatalog:
    config_dir: Path
    config_dir_source: str | None = None
    records: tuple[ClaudeSessionRecord, ...] = ()
    errors: tuple[ClaudeInventoryFailure, ...] = ()

    @property
    def sessions(self) -> tuple[ClaudeSessionRecord, ...]:
        return self.records

    @property
    def failures(self) -> tuple[ClaudeInventoryFailure, ...]:
        return self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "config_dir": str(self.config_dir),
            "config_dir_source": self.config_dir_source,
            "records": [record.to_dict() for record in self.records],
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True)
class ClaudeMultiRootCatalog:
    """A non-merging aggregate of independently qualified config roots."""

    catalogs: tuple[ClaudeSessionCatalog, ...] = ()
    root_errors: tuple[ClaudeInventoryFailure, ...] = ()

    @property
    def records(self) -> tuple[ClaudeSessionRecord, ...]:
        return tuple(record for catalog in self.catalogs for record in catalog.records)

    @property
    def sessions(self) -> tuple[ClaudeSessionRecord, ...]:
        return self.records

    @property
    def errors(self) -> tuple[ClaudeInventoryFailure, ...]:
        return (*self.root_errors, *(error for catalog in self.catalogs for error in catalog.errors))

    @property
    def failures(self) -> tuple[ClaudeInventoryFailure, ...]:
        return self.errors

    @property
    def frontend_only_records(self) -> tuple[ClaudeSessionRecord, ...]:
        return tuple(record for record in self.records if record.classification == "frontend_only")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "catalogs": [catalog.to_dict() for catalog in self.catalogs],
            "records": [record.to_dict() for record in self.records],
            "errors": [error.to_dict() for error in self.errors],
        }


def resolve_claude_paths(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | str | None = None,
    config_dir: Path | str | None = None,
    claude_config_dir: Path | str | None = None,
) -> ClaudePaths:
    """Resolve explicit config dir, then ``CLAUDE_CONFIG_DIR``, then ``~/.claude``."""
    if config_dir is not None and claude_config_dir is not None:
        raise ClaudeInventoryError("Specify only one Claude config directory")
    env = os.environ if environ is None else environ
    home_path = _absolute_path(Path.home() if home is None else Path(home))
    if config_dir is not None or claude_config_dir is not None:
        raw = config_dir if config_dir is not None else claude_config_dir
        source = "argument"
    elif env.get("CLAUDE_CONFIG_DIR"):
        raw = env["CLAUDE_CONFIG_DIR"]
        source = "environment"
    else:
        raw = home_path / ".claude"
        source = "default"
    assert raw is not None
    path = _canonical_config_dir(_expand_home(raw, home_path))
    return ClaudePaths(path, source)


def build_claude_session_catalog(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | str | None = None,
    config_dir: Path | str | None = None,
    claude_config_dir: Path | str | None = None,
    frontend_references: Iterable[Any] = (),
    references: Iterable[Any] | None = None,
    reference_errors: Iterable[Any] = (),
    cindy_references: Iterable[Any] | None = None,
    cindy_failures: Iterable[Any] | None = None,
) -> ClaudeSessionCatalog:
    """Build a bounded inventory of ``projects/<project>/<uuid>.jsonl``.

    ``frontend_references`` deliberately accepts duck-typed Cindy reference
    records.  ``references`` is a compatibility alias for callers with a
    backend-neutral reference layer.
    """
    try:
        paths = resolve_claude_paths(
            environ=environ, home=home, config_dir=config_dir,
            claude_config_dir=claude_config_dir,
        )
    except Exception as exc:
        fallback = config_dir if config_dir is not None else claude_config_dir
        root = _absolute_path(Path(fallback)) if fallback is not None else _absolute_path((Path.home() if home is None else Path(home)) / ".claude")
        return ClaudeSessionCatalog(root, errors=(ClaudeInventoryFailure(
            root, "claude-paths", type(exc).__name__, "Unable to resolve Claude config directory",
        ),))

    failures: list[ClaudeInventoryFailure] = []
    transcripts: dict[str, list[tuple[Path, Path, int, int]]] = {}
    try:
        for transcript, project in _discover_transcripts(paths.config_dir):
            try:
                session_id, lines, size = _read_transcript(transcript)
                transcripts.setdefault(session_id, []).append((transcript, project, lines, size))
            except Exception as exc:
                failures.append(_failure(paths.config_dir, "claude-transcript", exc, transcript))
    except Exception as exc:
        failures.append(_failure(paths.config_dir, "claude-projects", exc, paths.config_dir / "projects"))

    supplied_aliases = sum(item is not None for item in (references, cindy_references))
    if supplied_aliases > 1:
        failures.append(ClaudeInventoryFailure(
            paths.config_dir, "claude-references", "AmbiguousReferenceOverlay",
            "Specify only one Claude reference overlay alias",
        ))
    raw_references = cindy_references if cindy_references is not None else (
        frontend_references if references is None else references
    )
    try:
        reference_list = tuple(raw_references)
    except Exception as exc:
        reference_list = ()
        failures.append(_failure(paths.config_dir, "claude-references", exc))
    raw_reference_errors = reference_errors if cindy_failures is None else cindy_failures
    try:
        reference_error_list = tuple(raw_reference_errors)
    except Exception as exc:
        reference_error_list = ()
        failures.append(_failure(paths.config_dir, "claude-references", exc))
    for error in reference_error_list:
        message = _field(error, ("message",))
        failures.append(ClaudeInventoryFailure(
            paths.config_dir, "claude-references", type(error).__name__,
            message if isinstance(message, str) and message else "Frontend reference inventory is incomplete",
        ))

    reference_map: dict[str, list[tuple[Any, Mapping[str, Any], str]]] = {}
    for reference in reference_list:
        try:
            parsed = _parse_reference(reference, paths.config_dir)
            if parsed is not None:
                session_id, snapshot, category = parsed
                reference_map.setdefault(session_id, []).append((reference, snapshot, category))
                if snapshot.get("root_qualified") is not True:
                    failures.append(ClaudeInventoryFailure(
                        paths.config_dir, "claude-references", "AmbiguousStorageRoot",
                        "Claude frontend reference does not qualify its config root",
                    ))
        except Exception as exc:
            failures.append(_failure(paths.config_dir, "claude-references", exc))

    records: list[ClaudeSessionRecord] = []
    for session_id, copies in transcripts.items():
        try:
            manifest = _build_manifest(paths.config_dir, session_id, copies)
            refs = sorted(reference_map.get(session_id, ()), key=lambda item: _canonical_json(item[1]))
            category = _classification(tuple(item[2] for item in refs))
            records.append(ClaudeSessionRecord(
                config_dir=paths.config_dir,
                config_dir_source=paths.config_dir_source,
                session_id=session_id,
                transcript_paths=tuple(sorted((item[0] for item in copies), key=_normalized_path)),
                project_paths=tuple(sorted({item[1] for item in copies}, key=_normalized_path)),
                manifest=manifest,
                frontend_references=tuple(item[0] for item in refs),
                frontend_reference_snapshot=tuple(item[1] for item in refs),
                classification=category,
                transcript_line_count=sum(item[2] for item in copies),
                transcript_size=sum(item[3] for item in copies),
            ))
        except Exception as exc:
            failures.append(_failure(paths.config_dir, "claude-manifest", exc))

    # A DB reference is useful inventory evidence even after its native files
    # have disappeared.  Keep it visible rather than silently dropping it.
    for session_id in sorted(set(reference_map) - set(transcripts)):
        refs = sorted(reference_map[session_id], key=lambda item: _canonical_json(item[1]))
        records.append(ClaudeSessionRecord(
            config_dir=paths.config_dir,
            config_dir_source=paths.config_dir_source,
            session_id=session_id,
            transcript_paths=(),
            project_paths=(),
            manifest=(),
            frontend_references=tuple(item[0] for item in refs),
            frontend_reference_snapshot=tuple(item[1] for item in refs),
            classification="frontend_only",
            deletable=False,
            blockers=("Frontend reference has no local Claude transcript",),
        ))

    blocking = tuple(sorted({f"{item.source}: {item.message}" for item in failures if item.blocks_delete}))
    enriched = tuple(sorted((replace(
        record,
        classification="inventory_incomplete" if blocking else record.classification,
        blockers=tuple(sorted(set((*record.blockers, *blocking)))),
        deletable=not blocking and record.classification in ("unreferenced", "deleted_frontend_reference"),
    ) for record in records), key=lambda item: item.session_id))
    return ClaudeSessionCatalog(
        paths.config_dir, paths.config_dir_source, enriched,
        tuple(sorted(failures, key=lambda item: (item.source, _normalized_path(item.path) if item.path else "", item.message))),
    )


def build_claude_multi_root_catalog(
    config_dirs: Iterable[Path | str],
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | str | None = None,
    frontend_references: Iterable[Any] = (),
    references: Iterable[Any] | None = None,
    reference_errors: Iterable[Any] = (),
    cindy_references: Iterable[Any] | None = None,
    cindy_failures: Iterable[Any] | None = None,
) -> ClaudeMultiRootCatalog:
    """Inventory several roots while preserving root-qualified identities.

    Duplicate lexical roots are rejected instead of scanned twice.  The same
    session UUID in two roots remains two records and two different action IDs.
    """
    if references is not None and cindy_references is not None:
        fallback = _absolute_path(Path.home() if home is None else Path(home))
        return ClaudeMultiRootCatalog(root_errors=(ClaudeInventoryFailure(
            fallback, "claude-multi-root", "AmbiguousReferenceOverlay",
            "Specify only one Claude reference overlay alias",
        ),))
    raw_references = cindy_references if cindy_references is not None else (
        frontend_references if references is None else references
    )
    try:
        refs = tuple(raw_references)
        ref_errors = tuple(reference_errors if cindy_failures is None else cindy_failures)
        roots = tuple(config_dirs)
    except TypeError as exc:
        fallback = _absolute_path(Path.home() if home is None else Path(home))
        error = ClaudeInventoryFailure(
            fallback, "claude-multi-root", type(exc).__name__,
            "Unable to enumerate Claude config roots or references",
        )
        return ClaudeMultiRootCatalog(root_errors=(error,))
    catalogs: list[ClaudeSessionCatalog] = []
    errors: list[ClaudeInventoryFailure] = []
    seen: set[str] = set()
    for raw_root in roots:
        try:
            root = _canonical_config_dir(_expand_home(
                raw_root,
                _absolute_path(Path.home() if home is None else Path(home)),
            ))
            key = _normalized_path(root)
            if key in seen:
                raise ClaudeInventoryError("Claude multi-root inventory contains a duplicate config root")
            seen.add(key)
            catalogs.append(build_claude_session_catalog(
                environ=environ, home=home, config_dir=root,
                frontend_references=refs,
                reference_errors=tuple(
                    error
                    for error in ref_errors
                    if _reference_error_applies(error, root)
                ),
            ))
        except Exception as exc:
            fallback = _absolute_path(Path(raw_root))
            errors.append(_failure(fallback, "claude-multi-root", exc, fallback))
    return ClaudeMultiRootCatalog(
        tuple(sorted(catalogs, key=lambda item: _normalized_path(item.config_dir))),
        tuple(errors),
    )


def _reference_error_applies(error: Any, config: Path) -> bool:
    """Keep qualified frontend failures local; unqualified ones block all roots."""

    root = _field(error, ("config_dir", "claude_config_dir", "native_root", "storage_root"))
    if root is None:
        return True
    try:
        if not isinstance(root, (str, os.PathLike)):
            return True
        physical_root = _canonical_config_dir(
            _absolute_path(Path(root).expanduser())
        )
        return _normalized_path(physical_root) == _normalized_path(config)
    except Exception:
        # A malformed, dangling, or otherwise uncanonicalizable qualifier is
        # ambiguous and therefore applies to every root.  Never discard a
        # blocking failure merely because its alias cannot be resolved.
        return True


def select_claude_sessions(catalog: ClaudeSessionCatalog, selectors: Sequence[str]) -> tuple[ClaudeSessionRecord, ...]:
    """Resolve exact action IDs/session IDs or their unambiguous prefixes."""
    if not selectors:
        return catalog.records
    selected: list[ClaudeSessionRecord] = []
    seen: set[str] = set()
    for raw in selectors:
        if not isinstance(raw, str) or not raw.strip():
            raise ClaudeSelectionError("Claude session selector must not be blank")
        selector = raw.strip()
        if selector.lower() == "all":
            raise ClaudeSelectionError("Claude session selection never accepts all")
        exact = [item for item in catalog.records if selector in (item.action_id, item.session_id)]
        matches = exact or [item for item in catalog.records if item.action_id.startswith(selector) or item.session_id.startswith(selector)]
        if not matches:
            raise ClaudeSelectionError(f"No Claude session matches selector {selector!r}")
        if len(matches) != 1:
            raise ClaudeSelectionError(f"Claude session selector {selector!r} is ambiguous")
        if matches[0].action_id in seen:
            raise ClaudeSelectionError(f"Claude session {matches[0].action_id!r} was selected more than once")
        seen.add(matches[0].action_id)
        selected.append(matches[0])
    return tuple(selected)


def _discover_transcripts(config: Path) -> list[tuple[Path, Path]]:
    projects = config / "projects"
    try:
        status = projects.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ClaudeInventoryError("Unable to inspect Claude projects directory") from exc
    _require_directory_status(status, "Claude projects directory")
    result: list[tuple[Path, Path]] = []
    for project in _safe_iterdir(projects):
        project_status = _safe_lstat(project)
        _require_directory_status(project_status, "Claude project entry")
        _require_contained(project, config)
        for child in _safe_iterdir(project):
            child_status = _safe_lstat(child)
            if _is_link_or_reparse(child_status):
                raise ClaudeInventoryError("Claude project contains a symlink or reparse point")
            if stat.S_ISREG(child_status.st_mode):
                if child.suffix.lower() != ".jsonl":
                    continue
                _validate_session_id(child.stem)
                result.append((_require_contained(child, config), project))
            elif stat.S_ISDIR(child_status.st_mode):
                # Session sidecar directories and project memory are examined
                # only after their exact session ID is known.
                continue
            else:
                raise ClaudeInventoryError("Claude project contains an unknown node type")
    return sorted(result, key=lambda item: _normalized_path(item[0]))


def _read_transcript(path: Path) -> tuple[str, int, int]:
    session_id = _validate_session_id(path.stem)
    before = _safe_lstat(path)
    if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ClaudeInventoryError("Claude transcript is not a regular non-link file")
    digest = hashlib.sha256()
    line_count = 0
    try:
        with path.open("rb") as stream:
            while True:
                # Binary-stream iteration performs an unbounded readline.
                # Read one byte beyond the policy limit so an over-sized line
                # is rejected without materializing the rest of it in memory.
                raw_line = stream.readline(_MAX_JSONL_LINE + 1)
                if not raw_line:
                    break
                if len(raw_line) > _MAX_JSONL_LINE:
                    raise ClaudeInventoryError("Claude transcript contains an over-sized JSONL entry")
                digest.update(raw_line)
                if not raw_line.strip():
                    continue
                line_count += 1
                try:
                    entry = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ClaudeInventoryError(f"Claude transcript has invalid JSON at line {line_count}") from exc
                if not isinstance(entry, Mapping):
                    raise ClaudeInventoryError(f"Claude transcript entry at line {line_count} is not an object")
                embedded = entry.get("sessionId", entry.get("session_id"))
                if embedded is not None and embedded != session_id:
                    raise ClaudeInventoryError("Claude transcript session ID does not match its filename")
    except OSError as exc:
        raise ClaudeInventoryError("Unable to read Claude transcript") from exc
    after = _safe_lstat(path)
    if _stat_identity(before) != _stat_identity(after):
        raise ClaudeInventoryError("Claude transcript changed while it was inventoried")
    if line_count == 0:
        raise ClaudeInventoryError("Claude transcript is empty")
    # Digesting here is intentional even though the manifest re-hashes the file:
    # it guarantees the parse itself covered the exact full byte stream.
    if not digest.hexdigest():  # pragma: no cover - hashlib always has a digest
        raise ClaudeInventoryError("Unable to fingerprint Claude transcript")
    return session_id, line_count, before.st_size


def _build_manifest(config: Path, session_id: str, copies: Sequence[tuple[Path, Path, int, int]]) -> tuple[ClaudeManifestEntry, ...]:
    candidates: list[Path] = []
    for transcript, project, _, _ in copies:
        candidates.append(transcript)
        sidecar = project / session_id
        if _lexists(sidecar):
            candidates.append(sidecar)
    candidates.extend(_discover_auxiliary_targets(config, session_id))
    entries: list[ClaudeManifestEntry] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=_normalized_path):
        for path in _walk_manifest(candidate, config):
            key = _normalized_path(path)
            if key in seen:
                raise ClaudeInventoryError("Claude session manifest contains a duplicate path")
            seen.add(key)
            entries.append(_manifest_entry(path, config))
    return tuple(sorted(entries, key=lambda item: item.relative_path))


def _discover_auxiliary_targets(config: Path, session_id: str) -> list[Path]:
    targets: list[Path] = []
    for root_name in _AUXILIARY_ROOTS:
        root = config / root_name
        try:
            root_status = root.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ClaudeInventoryError(
                "Unable to inspect Claude auxiliary root"
            ) from exc
        _require_directory_status(root_status, "Claude auxiliary root")
        before = _stat_identity(root_status)
        for child in _safe_iterdir(root):
            if not _auxiliary_target_name_matches(
                root_name, child.name, session_id
            ):
                continue
            status = _safe_lstat(child)
            if _is_link_or_reparse(status):
                raise ClaudeInventoryError(
                    "Claude session auxiliary target is a symlink or reparse point"
                )
            if _auxiliary_pattern_requires_file(
                root_name, child.name, session_id
            ) and not stat.S_ISREG(status.st_mode):
                raise ClaudeInventoryError(
                    "Claude session auxiliary file has an unknown node type"
                )
            if not stat.S_ISREG(status.st_mode) and not stat.S_ISDIR(status.st_mode):
                raise ClaudeInventoryError(
                    "Claude session auxiliary target has an unknown node type"
                )
            targets.append(_require_contained(child, config))
        if _stat_identity(_safe_lstat(root)) != before:
            raise ClaudeInventoryError(
                "Claude auxiliary root changed while it was inventoried"
            )
    return targets


def _walk_manifest(root: Path, config: Path) -> list[Path]:
    _require_contained(root, config)
    _reject_reparse_components(root, config)
    status = _safe_lstat(root)
    if _is_link_or_reparse(status):
        raise ClaudeInventoryError("Claude session manifest contains a symlink or reparse point")
    if stat.S_ISREG(status.st_mode):
        return [root]
    if not stat.S_ISDIR(status.st_mode):
        raise ClaudeInventoryError("Claude session manifest contains an unknown node type")
    before = _stat_identity(status)
    result = [root]
    for child in _safe_iterdir(root):
        result.extend(_walk_manifest(child, config))
    if _stat_identity(_safe_lstat(root)) != before:
        raise ClaudeInventoryError("Claude session directory changed while it was inventoried")
    return result


def _manifest_entry(path: Path, config: Path) -> ClaudeManifestEntry:
    before = _safe_lstat(path)
    if _is_link_or_reparse(before):
        raise ClaudeInventoryError("Claude session manifest contains a symlink or reparse point")
    if stat.S_ISDIR(before.st_mode):
        node_type, digest = "directory", None
    elif stat.S_ISREG(before.st_mode):
        node_type, digest = "file", _stream_sha256(path)
    else:
        raise ClaudeInventoryError("Claude session manifest contains an unknown node type")
    after = _safe_lstat(path)
    if _stat_identity(before) != _stat_identity(after):
        raise ClaudeInventoryError("Claude session path changed while it was inventoried")
    return ClaudeManifestEntry(
        _absolute_path(path), _relative(path, config), node_type,
        before.st_dev, before.st_ino, before.st_mode, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
        getattr(before, "st_file_attributes", 0), digest,
    )


def _parse_reference(reference: Any, config: Path) -> tuple[str, Mapping[str, Any], str] | None:
    backend = _field(reference, ("backend", "agent_kind", "kind"))
    if backend is not None and str(backend).lower() not in ("claude", "cc", "claude-code", "claude_code"):
        return None
    root = _field(reference, ("config_dir", "claude_config_dir", "native_root", "storage_root"))
    canonical_reference_root: Path | None = None
    if root is not None:
        if not isinstance(root, (str, os.PathLike)):
            raise ClaudeInventoryError(
                "Claude frontend reference has an invalid config root"
            )
        canonical_reference_root = _canonical_config_dir(
            _absolute_path(Path(root).expanduser())
        )
        if _normalized_path(canonical_reference_root) != _normalized_path(config):
            return None
    session_id = _field(reference, ("native_session_id", "sdk_session_id", "session_id", "native_id"))
    if not isinstance(session_id, str):
        raise ClaudeInventoryError("Claude frontend reference has no valid native session ID")
    session_id = _validate_session_id(session_id)
    deleted = _field(reference, ("deleted", "is_deleted")) is True
    status = _field(reference, ("session_status", "status", "frontend_status"))
    if isinstance(status, str) and status.lower() in ("deleted", "soft_deleted", "removed"):
        deleted = True
    reference_kind = _field(reference, ("reference_kind", "binding_kind", "reference_type")) or "current"
    kind_text = str(reference_kind).lower()
    if deleted:
        category = "deleted"
    elif kind_text in ("current", "active", "current_binding"):
        category = "live_current"
    elif kind_text in ("historical", "agent_switch", "switch", "parked"):
        category = "live_historical"
    else:
        raise ClaudeInventoryError("Claude frontend reference has an unknown reference kind")
    snapshot = {
        "config_dir": _normalized_path(config),
        "reference_config_dir": (
            _normalized_path(canonical_reference_root)
            if canonical_reference_root is not None else None
        ),
        "root_qualified": root is not None,
        "root_ambiguous": root is None,
        "session_id": session_id,
        "category": category,
        "reference_kind": str(reference_kind),
        "session_status": _safe_scalar(status),
        "frontend_session_id": _safe_scalar(_field(reference, ("frontend_session_id", "cindy_session_id", "platform_session_id"))),
        "database": _safe_path(_field(reference, ("database", "platform_db", "frontend_database"))),
        "profile_root": _safe_path(_field(reference, ("profile_root", "frontend_root"))),
        "working_dir": _safe_scalar(_field(reference, ("working_dir", "cwd"))),
        "session_updated_at_ms": _safe_scalar(_field(reference, ("session_updated_at_ms", "updated_at_ms"))),
        "boundary_id": _safe_scalar(_field(reference, ("boundary_id", "switch_boundary_id"))),
        "boundary_created_at_ms": _safe_scalar(_field(reference, ("boundary_created_at_ms", "boundary_time", "timestamp"))),
        "boundary_rewind_at_ms": _safe_scalar(_field(reference, ("boundary_rewind_at_ms", "rewind_at_ms"))),
    }
    return session_id, snapshot, category


def _classification(categories: Sequence[str]) -> str:
    if "live_current" in categories:
        return "live_current_reference"
    if "live_historical" in categories:
        return "live_historical_reference"
    if categories:
        return "deleted_frontend_reference"
    return "unreferenced"


def _field(value: Any, names: Sequence[str]) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _safe_scalar(value: Any) -> str | int | bool | None:
    return value if value is None or isinstance(value, (str, int, bool)) else str(value)


def _safe_path(value: Any) -> str | None:
    return _normalized_path(Path(value)) if isinstance(value, (str, os.PathLike)) else None


def _validate_session_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ClaudeInventoryError("Claude session filename is not a UUID") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise ClaudeInventoryError("Claude session UUID is not canonical")
    return canonical


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise ClaudeInventoryError("Unable to read Claude session file") from exc
    return digest.hexdigest()


def _failure(config: Path, source: str, exc: Exception, path: Path | None = None) -> ClaudeInventoryFailure:
    message = str(exc) if isinstance(exc, ClaudeInventoryError) else "Claude session inventory operation failed"
    return ClaudeInventoryFailure(config, source, type(exc).__name__, message, path)


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError as exc:
        raise ClaudeInventoryError("Unable to enumerate Claude session directory") from exc


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise ClaudeInventoryError("Unable to inspect Claude session path") from exc


def _canonical_config_dir(path: Path) -> Path:
    """Return one physical identity even when a parent is an alias.

    ``Path.resolve(strict=False)`` can silently preserve a dangling alias, so
    missing config roots are handled by resolving their nearest existing
    ancestor strictly and then appending only the known-missing components.
    Existing final symlinks/junctions are safe aliases here because all later
    inventory and deletion operates on the resolved target, never the alias.
    """
    lexical = _absolute_path(path)
    ancestor = lexical
    missing: list[str] = []
    while True:
        try:
            ancestor_status = ancestor.lstat()
            break
        except FileNotFoundError:
            if ancestor.parent == ancestor:
                raise ClaudeInventoryError(
                    "Unable to find an existing ancestor for Claude config root"
                )
            missing.append(ancestor.name)
            ancestor = ancestor.parent
        except OSError as exc:
            raise ClaudeInventoryError(
                "Unable to inspect Claude config root ancestry"
            ) from exc
    try:
        physical_ancestor = ancestor.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ClaudeInventoryError(
            "Unable to canonicalize Claude config root"
        ) from exc
    try:
        physical_status = physical_ancestor.lstat()
    except OSError as exc:
        raise ClaudeInventoryError(
            "Unable to inspect canonical Claude config root"
        ) from exc
    if _is_link_or_reparse(physical_status):
        raise ClaudeInventoryError(
            "Canonical Claude config root remains a symlink or reparse point"
        )
    if missing and not stat.S_ISDIR(physical_status.st_mode):
        raise ClaudeInventoryError(
            "Claude config root has a non-directory existing ancestor"
        )
    physical = physical_ancestor.joinpath(*reversed(missing))
    if not missing and not stat.S_ISDIR(physical_status.st_mode):
        raise ClaudeInventoryError(
            "Claude config root is not a directory"
        )
    # Re-lstat the existing alias after resolution.  This does not make path
    # resolution atomic, but catches ordinary replacement during the lookup.
    if not missing:
        try:
            after = ancestor.lstat()
        except OSError as exc:
            raise ClaudeInventoryError(
                "Claude config root alias changed while canonicalized"
            ) from exc
        if _stat_identity(after) != _stat_identity(ancestor_status):
            raise ClaudeInventoryError(
                "Claude config root alias changed while canonicalized"
            )
    return _absolute_path(physical)


def _require_directory_status(status: os.stat_result, label: str) -> None:
    if _is_link_or_reparse(status) or not stat.S_ISDIR(status.st_mode):
        raise ClaudeInventoryError(f"{label} is not a directory or is a link")


def _require_contained(path: Path, root: Path) -> Path:
    lexical = _absolute_path(path)
    try:
        lexical.relative_to(_absolute_path(root))
        lexical.resolve(strict=False).relative_to(_absolute_path(root).resolve(strict=False))
    except ValueError as exc:
        raise ClaudeInventoryError("Claude session path escapes its config directory") from exc
    return lexical


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ClaudeInventoryError("Unable to inspect optional Claude session path") from exc


def _is_link_or_reparse(status: os.stat_result) -> bool:
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status, "st_file_attributes", 0) & _REPARSE_POINT)


def _reject_reparse_components(path: Path, root: Path) -> None:
    root = _absolute_path(root)
    target = _absolute_path(path)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ClaudeInventoryError("Claude session path escapes its config directory") from exc
    candidate = root
    for part in (None, *relative.parts):
        if part is not None:
            candidate = candidate / part
        status = _safe_lstat(candidate)
        if _is_link_or_reparse(status):
            raise ClaudeInventoryError("Claude session path contains a symlink or reparse point")


def _stat_identity(status: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (status.st_dev, status.st_ino, status.st_mode, status.st_size,
            status.st_mtime_ns, status.st_ctime_ns, getattr(status, "st_file_attributes", 0))


def _expand_home(value: Path | str, home: Path) -> Path:
    raw = os.fspath(value)
    if raw == "~":
        return home
    if raw.startswith("~/") or raw.startswith("~\\"):
        return _absolute_path(home / raw[2:])
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ClaudeInventoryError("Claude config directory must be absolute")
    return _absolute_path(candidate)


def _relative(path: Path, root: Path) -> str:
    try:
        return _absolute_path(path).relative_to(_absolute_path(root)).as_posix()
    except ValueError as exc:
        raise ClaudeInventoryError("Claude session path is outside its config directory") from exc


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "ClaudeInventoryError", "ClaudeInventoryFailure", "ClaudeManifestEntry",
    "ClaudeMultiRootCatalog",
    "ClaudePaths", "ClaudeSelectionError", "ClaudeSessionCatalog",
    "ClaudeSessionRecord", "build_claude_multi_root_catalog", "build_claude_session_catalog",
    "resolve_claude_paths", "select_claude_sessions",
]
