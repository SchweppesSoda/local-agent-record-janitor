"""Bounded Orca profile/account evidence; no product initialization or bridge."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Mapping

from .client_contracts import require_local_location
from .path_identity import is_local_absolute_locator
from .record_identity import canonical_path


class OrcaDiscoveryError(ValueError):
    pass


def local_orca_path(locator: str | os.PathLike[str], *, host: str = "local", path_namespace: str = "local") -> Path:
    # Never canonicalize a remote/foreign-OS locator into the current drive.
    require_local_location(host, path_namespace)
    raw = os.fspath(locator)
    if not isinstance(raw, str) or not is_local_absolute_locator(raw) or any(ord(c) < 32 for c in raw):
        raise OrcaDiscoveryError("orca_local_path_unproven")
    return Path(raw)


def _redirected(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def require_plain_directory(path: Path) -> None:
    # Check ancestors before resolve/open can follow a junction or symlink.
    for directory in (*reversed(path.parents), path):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or _redirected(info):
            raise OrcaDiscoveryError("orca_directory_redirected")


def require_plain_file(path: Path) -> os.stat_result:
    require_plain_directory(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or _redirected(info):
        raise OrcaDiscoveryError("orca_metadata_file_redirected")
    return info


def _contained(child: Path, parent: Path) -> bool:
    try:
        return child != parent and child.is_relative_to(parent)
    except (OSError, ValueError):
        return False


def prove_account_home(profile_root: Path, home: Path, *, account_id: str | None = None) -> Path:
    require_plain_directory(profile_root)
    accounts = profile_root / "codex-accounts"
    if account_id is None:
        if home.name != "home" or home.parent.parent != accounts:
            raise OrcaDiscoveryError("orca_account_layout_unproven")
        account_id = home.parent.name
    if not account_id or account_id in {".", ".."} or any(c in account_id for c in "\\/\x00"):
        raise OrcaDiscoveryError("orca_account_identity_invalid")
    expected = accounts / account_id / "home"
    require_plain_directory(expected)
    require_plain_directory(home)
    resolved_accounts, resolved_home = accounts.resolve(strict=True), home.resolve(strict=True)
    if (canonical_path(home) != canonical_path(expected) or not _contained(resolved_home, resolved_accounts)
        or _contained(resolved_home, (Path.home() / ".codex").resolve())
        or canonical_path(home) == canonical_path(Path.home() / ".codex")):
        raise OrcaDiscoveryError("orca_account_containment_unproven")
    marker = home / ".orca-managed-home"
    before = require_plain_file(marker)
    if before.st_size > 1024:
        raise OrcaDiscoveryError("orca_account_marker_invalid")
    with marker.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OrcaDiscoveryError("orca_account_marker_changed")
        raw = stream.read(1025)
    after = require_plain_file(marker)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise OrcaDiscoveryError("orca_account_marker_changed")
    try:
        valid = len(raw) <= 1024 and raw.decode("utf-8").strip() == account_id
    except UnicodeError:
        valid = False
    if not valid:
        raise OrcaDiscoveryError("orca_account_marker_invalid")
    require_plain_directory(home / "sessions")
    return home


def prove_runtime_home(profile_root: Path, locator: str, *, host: str, wsl_distro: str | None) -> Path:
    if host != "local" or wsl_distro is not None:
        raise OrcaDiscoveryError("orca_execution_location_unsupported")
    home = local_orca_path(locator, host=host)
    require_plain_directory(profile_root)
    expected = profile_root / "codex-runtime-home" / "home"
    require_plain_directory(home)
    if canonical_path(home) != canonical_path(expected) or not _contained(home.resolve(strict=True), profile_root.resolve(strict=True)):
        raise OrcaDiscoveryError("orca_runtime_association_unproven")
    require_plain_directory(home / "sessions")
    return home


def default_orca_root(*, appdata: Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("ORCA_USER_DATA_PATH")
    if configured:
        if not configured.strip():
            raise OrcaDiscoveryError("orca_environment_root_invalid")
        return local_orca_path(configured)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "orca"
    if os.name == "nt":
        base = appdata or (local_orca_path(env["APPDATA"]) if env.get("APPDATA") else Path.home() / "AppData" / "Roaming")
    else:
        base = local_orca_path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "orca"


def reverse_account_profile(home: Path) -> Path | None:
    # The exact known layout plus an observed marker limits reverse discovery.
    # A runtime-home basename alone is never an ownership proof.
    if home.name != "home" or home.parent.parent.name != "codex-accounts":
        return None
    marker = home / ".orca-managed-home"
    try:
        marker.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        pass  # A visible but unreadable marker must still produce a guard/error.
    return home.parent.parent.parent
