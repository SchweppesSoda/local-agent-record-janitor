"""Bounded persisted Herdr locations; never connect to or start a server."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Mapping

from .client_contracts import require_local_location
from .path_identity import is_local_absolute_locator


class HerdrDiscoveryError(ValueError):
    pass


def local_herdr_path(locator: str | os.PathLike[str], *, host: str = "local", path_namespace: str = "local") -> Path:
    require_local_location(host, path_namespace)
    raw = os.fspath(locator)
    if not isinstance(raw, str) or not is_local_absolute_locator(raw) or any(ord(c) < 32 for c in raw):
        raise HerdrDiscoveryError("herdr_local_path_unproven")
    return Path(raw)


def require_plain_directory(path: Path) -> os.stat_result:
    info = None
    for directory in (*reversed(path.parents), path):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise HerdrDiscoveryError("herdr_directory_redirected")
    return info


def require_plain_file(path: Path) -> os.stat_result:
    require_plain_directory(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise HerdrDiscoveryError("herdr_metadata_file_redirected")
    return info


def bounded_entries(directory: Path, *, maximum: int = 256) -> tuple[Path, ...]:
    before = require_plain_directory(directory)
    entries = []
    with os.scandir(directory) as scan:
        for entry in scan:
            if len(entries) >= maximum:
                raise HerdrDiscoveryError("herdr_directory_limit_exceeded")
            entries.append(directory / entry.name)
    after = require_plain_directory(directory)
    if (before.st_dev, before.st_ino, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns):
        raise HerdrDiscoveryError("herdr_directory_changed")
    return tuple(sorted(entries, key=lambda p: p.name))


def valid_session_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) and name not in {".", "..", "default"})


def valid_recovery_name(name: str) -> bool:
    match = re.fullmatch(r"session-([0-9]{39})-([0-9]+)-([0-9]+)\.json", name)
    return bool(match and int(match[1]) <= 2**128 - 1)


def default_herdr_roots(*, appdata: Path | None = None, environ: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    env = os.environ if environ is None else environ
    # Herdr uses environment presence, including XDG on Windows/macOS. An
    # empty or relative value is not a proved local root; never scan cwd.
    if "XDG_CONFIG_HOME" in env:
        base = local_herdr_path(env["XDG_CONFIG_HOME"])
    elif os.name == "nt" and appdata is not None:
        base = local_herdr_path(appdata)
    elif os.name == "nt" and "APPDATA" in env:
        base = local_herdr_path(env["APPDATA"])
    elif os.name == "nt" and "USERPROFILE" in env:
        base = local_herdr_path(env["USERPROFILE"]) / "AppData" / "Roaming"
    elif "HOME" in env:
        base = local_herdr_path(env["HOME"]) / ".config"
    else:
        base = local_herdr_path(tempfile.gettempdir())
    return tuple(base / name for name in ("herdr", "herdr-dev"))
