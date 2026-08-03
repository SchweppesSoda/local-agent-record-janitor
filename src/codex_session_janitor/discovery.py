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
    """Compatibility projection of one Cindy frontend DB namespace.

    This is not a unique Cindy installation, backend store, or runtime.  A
    local namespace and multiple signed-in owner namespaces may share the same
    ``root``, ``codex_home``, and bundled Codex runtime while retaining their
    distinct database paths.
    """

    root: Path
    database: Path
    codex_home: Path


def discover_cindy_profiles(appdata: Path | None = None) -> tuple[CindyProfile, ...]:
    """Discover DB-namespace projections for known Cindy installations.

    Cindy's Electron ``userData`` directory is the profile root.  Database
    sidecars and backup copies are deliberately excluded: treating them as a
    second live frontend would duplicate references and weaken snapshots.
    Multiple returned projections can deliberately share one root, native
    store, and bundled runtime; consumers must union all such namespaces before
    authorizing a native mutation.
    """

    roaming = (appdata or default_appdata()).expanduser()
    profiles: list[CindyProfile] = []
    for brand in ("CindyGlobal", "Cindy", "CindyDev", "xdt-maker"):
        root = roaming / brand
        if not _optional_directory_exists(root):
            continue
        discovered_databases = _discover_cindy_databases(root)
        for database in discovered_databases:
            profiles.append(
                CindyProfile(
                    root=root,
                    database=database,
                    codex_home=root / "codex-home",
                )
            )
        dedicated_store_exists = False
        if not discovered_databases:
            codex_store_exists = _optional_directory_exists(root / "codex-home")
            pi_home_exists = _optional_directory_exists(root / "pi-agent-home")
            pi_store_exists = (
                _optional_directory_exists(root / "pi-agent-home" / "sessions")
                if pi_home_exists
                else False
            )
            dedicated_store_exists = codex_store_exists or pi_store_exists
        if not discovered_databases and dedicated_store_exists:
            # The frontend DB may have been removed while a dedicated native
            # store survived. A missing placeholder DB contributes no
            # frontend rows but keeps that Cindy-owned storage discoverable.
            profiles.append(
                CindyProfile(
                    root=root,
                    database=root / "cindy-local-v1.db",
                    codex_home=root / "codex-home",
                )
            )
    return tuple(profiles)


def resolve_cindy_profiles(
    appdata: Path | None = None,
    *,
    root: Path | None = None,
    database: Path | None = None,
    codex_home: Path | None = None,
) -> tuple[CindyProfile, ...]:
    """Resolve every Cindy DB namespace that can guard one explicit store.

    With no explicit value this preserves global known-brand discovery.  Once
    any value is explicit, the effective Cindy installation root is bounded to
    ``root`` (or ``database.parent`` when only a DB is supplied), but every
    current sibling DB below that root remains a safety input.  An explicitly
    supplied DB is retained even when it lives outside an explicit root; this
    preserves custom mappings without allowing them to hide root siblings.
    """

    if root is None and database is None and codex_home is None:
        return discover_cindy_profiles(appdata)

    roaming = (appdata or default_appdata()).expanduser()
    explicit_database = database.expanduser() if database is not None else None
    effective_root = (
        root.expanduser()
        if root is not None
        else explicit_database.parent
        if explicit_database is not None
        else roaming / "CindyGlobal"
    )
    effective_home = (
        codex_home.expanduser()
        if codex_home is not None
        else effective_root / "codex-home"
    )

    siblings = (
        _discover_cindy_databases(effective_root)
        if _optional_directory_exists(effective_root)
        else ()
    )
    candidates: list[Path] = list(siblings)
    if explicit_database is not None:
        if _is_database_sidecar_or_backup(explicit_database):
            raise RuntimeError(
                f"Explicit Cindy database is a sidecar or backup: {explicit_database}"
            )
        try:
            explicit_status = explicit_database.stat()
        except FileNotFoundError:
            # A missing explicit DB remains a compatibility placeholder for a
            # native store whose frontend namespace has already disappeared.
            pass
        except OSError as exc:
            raise RuntimeError(
                f"Could not inspect explicit Cindy database {explicit_database}: {exc}"
            ) from exc
        else:
            if not stat.S_ISREG(explicit_status.st_mode):
                raise RuntimeError(
                    "Explicit Cindy database is not a regular file: "
                    f"{explicit_database}"
                )
        candidates.append(explicit_database)
    if not candidates:
        candidates.append(effective_root / "cindy-local-v1.db")

    profiles: list[CindyProfile] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda path: str(path).casefold()):
        key = _normalized_path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        profiles.append(
            CindyProfile(
                root=effective_root,
                database=candidate,
                codex_home=effective_home,
            )
        )
    return tuple(profiles)


def _discover_cindy_databases(root: Path) -> tuple[Path, ...]:
    databases: list[Path] = []
    for pattern in ("cindy-*.db", "xdt-maker-*.db"):
        try:
            databases.extend(root.glob(pattern))
        except OSError as exc:
            raise RuntimeError(f"Could not enumerate Cindy profile {root}: {exc}") from exc
    databases = sorted(databases, key=lambda path: path.name.casefold())
    databases.extend(
        (
            root / "cindy-local-v1.db",
            root / "xdt-maker-local-v1.db",
            root / "cindy.db",
            root / "xdt-maker.db",
        )
    )
    return _unique_existing_files(
        [
            candidate
            for candidate in databases
            if not _is_database_sidecar_or_backup(candidate)
        ]
    )


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
        key = _normalized_path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return tuple(result)


def _normalized_path_key(path: Path) -> str:
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError):
        canonical = Path(os.path.abspath(os.fspath(path)))
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.fspath(canonical)))
    )


def _optional_directory_exists(path: Path) -> bool:
    try:
        path_status = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(f"Could not inspect frontend profile {path}: {exc}") from exc
    attributes = getattr(path_status, "st_file_attributes", 0)
    is_reparse = bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )
    if stat.S_ISLNK(path_status.st_mode) or is_reparse:
        raise RuntimeError(
            f"Frontend profile path is a symlink or reparse point: {path}"
        )
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
