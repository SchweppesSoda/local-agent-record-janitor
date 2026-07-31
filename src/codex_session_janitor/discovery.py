from __future__ import annotations

import os
import re
import shutil
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
