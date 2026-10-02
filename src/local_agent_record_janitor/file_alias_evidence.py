"""Bounded local file identity observations, never deletion authorization."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .client_contracts import require_local_location
from .path_identity import canonical_existing_path_key, is_local_absolute_locator as _local_absolute


@dataclass(frozen=True)
class FileAliasEvidence:
    lexical_path: str
    resolved_path: str | None = None
    readlink: str | None = None
    kind: str = "unknown"
    device_id: int | None = None
    file_id: int | None = None
    nlink: int | None = None
    known_paths: tuple[str, ...] = ()
    known_hardlink_paths: tuple[str, ...] = ()
    hardlink_count_matches: bool | None = None
    errors: tuple[str, ...] = ()

    @property
    def probe_complete(self) -> bool:
        return not self.errors and self.file_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {"lexical_path": self.lexical_path, "resolved_path": self.resolved_path,
                "readlink": self.readlink, "kind": self.kind, "device_id": self.device_id,
                "file_id": self.file_id, "nlink": self.nlink, "known_paths": list(self.known_paths),
                "known_hardlink_paths": list(self.known_hardlink_paths),
                "hardlink_count_matches": self.hardlink_count_matches,
                "probe_complete": self.probe_complete, "errors": list(self.errors),
                "alias_coverage_complete": False}


@dataclass(frozen=True)
class FileAliasSnapshot:
    observed_at: str
    roots: tuple[str, ...]
    entries: tuple[FileAliasEvidence, ...]
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"observed_at": self.observed_at, "roots": list(self.roots),
                "entries": [entry.to_dict() for entry in self.entries], "errors": list(self.errors),
                "probe_complete": not self.errors and all(entry.probe_complete for entry in self.entries),
                "coverage": "supplied_paths_only", "alias_coverage_complete": False,
                "hardlink_scope": "observed_link_count_at_probe_time",
                "symlink_and_copy_coverage": "not_proven"}


def _is_link(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _within(path: Path, roots: frozenset[str]) -> bool:
    text = os.path.normcase(os.fspath(path))
    while text not in roots:
        parent = os.path.dirname(text)
        if parent == text:
            return False
        text = parent
    return True


def probe_file_aliases(
    paths: Iterable[str | os.PathLike[str]], *, roots: Iterable[str | os.PathLike[str]],
    host: str = "local", path_namespace: str = "local",
) -> FileAliasSnapshot:
    """Inspect supplied files without discovery, contents or identity merging.

    Only leaf symlinks whose lexical targets stay within the supplied plain
    directory roots may resolve. Directory links, external targets, missing
    paths and uncertain metadata remain explicit incomplete observations.
    Matching nlink counts only bounds known hardlinks at this observation;
    it proves nothing about undiscovered symlinks, copies or logical stores.
    """
    require_local_location(host, path_namespace)
    raw_roots = tuple(dict.fromkeys(os.fspath(root) for root in roots))
    raw_paths = tuple(dict.fromkeys(os.fspath(path) for path in paths))
    directories: dict[Path, os.stat_result] = {}

    def plain_directory(path: Path) -> None:
        if path in directories:
            return
        if path.parent != path:
            plain_directory(path.parent)
        value = path.lstat()
        if _is_link(value) or not stat.S_ISDIR(value.st_mode):
            raise ValueError(f"unproven_directory:{path}")
        directories[path] = value

    def recheck_directories(paths: Iterable[Path]) -> None:
        checked: set[Path] = set()
        for path in paths:
            while path not in checked:
                checked.add(path)
                before = directories[path]
                after = path.lstat()
                if (_is_link(after) or not stat.S_ISDIR(after.st_mode)
                        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
                    raise ValueError(f"directory_identity_changed:{path}")
                if path.parent == path:
                    break
                path = path.parent

    approved: list[Path] = []
    resolved_roots: list[Path] = []
    failures: list[str] = []
    for raw in raw_roots:
        if not _local_absolute(raw):
            failures.append(f"opaque_root:{raw}")
            continue
        root = Path(os.path.normpath(raw))
        try:
            plain_directory(root)
            resolved = root.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(f"root_probe_failed:{raw}:{exc}")
        else:
            approved.append(root)
            resolved_roots.append(resolved)

    path_keys: dict[str, str] = {}
    approved_keys = frozenset(os.path.normcase(str(root)) for root in approved)
    resolved_keys = frozenset(os.path.normcase(str(root)) for root in resolved_roots)

    def inspect(raw: str) -> FileAliasEvidence:
        entry = FileAliasEvidence(raw)
        if not _local_absolute(raw):
            return replace(entry, errors=("opaque_locator",))
        path = Path(os.path.normpath(raw))
        entry = replace(entry, lexical_path=str(path))
        if not _within(path, approved_keys):
            return replace(entry, errors=("outside_known_roots",))
        current = path
        seen: set[Path] = set()
        try:
            while True:
                if current in seen or len(seen) >= 40:
                    raise ValueError("symlink_cycle_or_limit")
                seen.add(current)
                plain_directory(current.parent)
                value = current.lstat()
                if not _is_link(value):
                    if not stat.S_ISREG(value.st_mode):
                        raise ValueError("not_regular_file")
                    break
                target = os.readlink(current)
                entry = replace(entry, kind="symlink", readlink=entry.readlink or target)
                if target.startswith(("//", "\\\\")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", target):
                    raise ValueError("opaque_link_target")
                if os.path.isabs(target):
                    if not _local_absolute(target):
                        raise ValueError("opaque_link_target")
                    destination = target
                else:
                    # Drive-qualified/foreign absolute spellings must not
                    # become relative local children on another platform.
                    if re.match(r"^[A-Za-z]:", target) or target.startswith("\\"):
                        raise ValueError("opaque_link_target")
                    destination = os.path.join(current.parent, target)
                candidate = Path(os.path.normpath(destination))
                if not _within(candidate, approved_keys):
                    raise ValueError("outside_known_roots")
                current = candidate
            resolved = current.resolve(strict=True)
            if not _within(resolved, resolved_keys):
                raise ValueError("resolution_escaped_roots")
            after = current.lstat()
            if _is_link(after) or (value.st_dev, value.st_ino, value.st_nlink) != (after.st_dev, after.st_ino, after.st_nlink):
                raise ValueError("file_identity_changed")
            if not value.st_ino or value.st_nlink < 1:
                raise ValueError("file_identity_unavailable")
            # Collapse only proven spellings of one directory entry (8.3
            # and extended-prefix aliases), never distinct hardlink names.
            path_keys[entry.lexical_path] = canonical_existing_path_key(resolved)
            recheck_directories(candidate.parent for candidate in seen)
            return replace(entry, kind=entry.kind if entry.kind == "symlink" else "regular_file",
                           resolved_path=str(resolved), device_id=value.st_dev, file_id=value.st_ino, nlink=value.st_nlink)
        except (OSError, RuntimeError, ValueError) as exc:
            return replace(entry, errors=(f"file_probe_failed:{exc}",))

    entries = tuple(inspect(raw) for raw in raw_paths)
    groups: dict[tuple[int, int], list[FileAliasEvidence]] = {}
    for entry in entries:
        if entry.probe_complete:
            groups.setdefault((entry.device_id, entry.file_id), []).append(entry)
    projections: dict[tuple[int, int], tuple[tuple[str, ...], tuple[str, ...], bool | None]] = {}
    for key, known in groups.items():
        hardlink_names = {path_keys[item.lexical_path]: item.resolved_path
                          for item in known if item.kind == "regular_file"}
        nlinks = {item.nlink for item in known}
        # An extended spelling left uncollapsed lacks the proof needed to
        # count it as another directory entry.
        count_proven = not any(value.startswith("\\\\?\\") for value in hardlink_names)
        matches = len(hardlink_names) == known[0].nlink if len(nlinks) == 1 and count_proven else None
        projections[key] = (tuple(item.lexical_path for item in known), tuple(hardlink_names.values()), matches)
    projected: list[FileAliasEvidence] = []
    for entry in entries:
        known_paths, hardlinks, matches = projections.get((entry.device_id, entry.file_id), ((), (), None))
        projected.append(replace(entry, known_paths=known_paths,
                                 known_hardlink_paths=hardlinks, hardlink_count_matches=matches))
    return FileAliasSnapshot(datetime.now(timezone.utc).isoformat(), raw_roots, tuple(projected), tuple(failures))
