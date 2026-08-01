from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def default_appdata() -> Path:
    configured = os.environ.get("APPDATA")
    if configured:
        return Path(configured)
    return Path.home() / "AppData" / "Roaming"


def default_local_appdata() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    if configured:
        return Path(configured)
    return Path.home() / "AppData" / "Local"


def discover_path_codex() -> Path | None:
    executable = shutil.which("codex")
    return Path(executable) if executable else None


def discover_cindy_codex(cindy_root: Path) -> Path | None:
    base = cindy_root / "codex"
    if not base.is_dir():
        return None
    candidates = list(base.glob("*/codex.exe")) + list(base.glob("*/codex"))
    if not candidates:
        candidates = list(base.rglob("codex.exe")) + list(base.rglob("codex"))
    return _newest_versioned_path(candidates)


def discover_aionui_codex(local_appdata: Path | None = None) -> Path | None:
    root = (local_appdata or default_local_appdata()) / "Programs" / "AionUi" / "resources"
    if not root.is_dir():
        return None
    candidates = list(root.rglob("codex.exe"))
    return _newest_versioned_path(candidates)


def discover_aionui_databases(appdata: Path | None = None) -> tuple[Path, ...]:
    """Return existing current and legacy AionUI databases, newest layout first."""

    roaming = (appdata or default_appdata()).expanduser()
    candidates: list[Path] = []
    for brand in ("AionUi", "AionUI"):
        root = roaming / brand
        candidates.extend(
            (
                root / "aionui" / "aionui.db",
                root / "aionui" / "aionui-backend.db",
                root / "aionui.db",
                root / "aionui-backend.db",
            )
        )
    return _unique_existing_files(candidates)


@dataclass(frozen=True)
class CindyProfile:
    root: Path
    database: Path
    codex_home: Path


def discover_cindy_profiles(appdata: Path | None = None) -> tuple[CindyProfile, ...]:
    """Discover current per-user and known fixed Cindy databases.

    Cindy's Electron ``userData`` directory is the profile root.  Database
    sidecars and backup copies are deliberately excluded: treating them as a
    second live frontend would duplicate references and weaken snapshots.
    """

    roaming = (appdata or default_appdata()).expanduser()
    profiles: list[CindyProfile] = []
    for brand in ("CindyGlobal", "Cindy", "CindyDev", "xdt-maker"):
        root = roaming / brand
        if not _optional_directory_exists(root):
            continue
        databases: list[Path] = []
        for pattern in ("cindy-*.db", "xdt-maker-*.db"):
            try:
                databases.extend(root.glob(pattern))
            except OSError as exc:
                raise RuntimeError(
                    f"Could not enumerate Cindy profile {root}: {exc}"
                ) from exc
        databases = sorted(databases, key=lambda path: path.name.casefold())
        databases.extend(
            (
                root / "cindy-local-v1.db",
                root / "xdt-maker-local-v1.db",
                root / "cindy.db",
                root / "xdt-maker.db",
            )
        )
        discovered_databases = _unique_existing_files(
            [
                database
                for database in databases
                if not _is_database_sidecar_or_backup(database)
            ]
        )
        for database in discovered_databases:
            profiles.append(
                CindyProfile(
                    root=root,
                    database=database,
                    codex_home=root / "codex-home",
                )
            )
        if (
            not discovered_databases
            and _optional_directory_exists(root / "codex-home")
        ):
            # The frontend DB may have been removed while its dedicated Codex
            # storage survived. A missing placeholder DB contributes no
            # frontend rows but keeps that storage visible in the native union.
            profiles.append(
                CindyProfile(
                    root=root,
                    database=root / "cindy-local-v1.db",
                    codex_home=root / "codex-home",
                )
            )
    return tuple(profiles)


def choose_codex_binary(hint: Path | None = None) -> Path | None:
    if hint is None:
        return discover_path_codex()
    try:
        expanded_hint = hint.expanduser()
        return expanded_hint if expanded_hint.is_file() else None
    except (OSError, RuntimeError):
        return None


def _newest_versioned_path(candidates: list[Path]) -> Path | None:
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: (_version_key(str(path)), path.stat().st_mtime))


def _version_key(value: str) -> tuple[int, ...]:
    versions = re.findall(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
    if not versions:
        return (0, 0, 0)
    return tuple(int(part) for part in versions[-1])


def _unique_existing_files(candidates: list[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            candidate_status = candidate.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(
                f"Could not inspect frontend database candidate {candidate}: {exc}"
            ) from exc
        if not stat.S_ISREG(candidate_status.st_mode):
            raise RuntimeError(
                f"Frontend database candidate is not a regular file: {candidate}"
            )
        try:
            canonical = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            canonical = Path(os.path.abspath(os.fspath(candidate)))
        key = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(canonical))))
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return tuple(result)


def _optional_directory_exists(path: Path) -> bool:
    try:
        path_status = path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(f"Could not inspect frontend profile {path}: {exc}") from exc
    if not stat.S_ISDIR(path_status.st_mode):
        raise RuntimeError(f"Frontend profile path is not a directory: {path}")
    return True


def _is_database_sidecar_or_backup(path: Path) -> bool:
    lowered = path.name.casefold()
    return (
        lowered.endswith(("-wal", "-shm", "-journal", ".bak", ".backup"))
        or ".backup." in lowered
        or "-backup." in lowered
        or ".bak." in lowered
        or lowered.startswith("backup-")
    )
