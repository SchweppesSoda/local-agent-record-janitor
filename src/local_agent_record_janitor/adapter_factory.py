from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapters import AionUIAdapter, CindyAdapter, NativeIntegrityAdapter
from .adapters.base import FrontendAdapter
from .cleanup_service import selected_platforms
from .discovery import (
    default_appdata,
    default_codex_home,
    discover_aionui_databases,
    resolve_cindy_profiles,
)


def create_default_adapters(args: Any) -> list[FrontendAdapter]:
    """Build adapters without coupling either command driver to the other."""

    appdata = (args.appdata or default_appdata()).expanduser()
    native_codex_home = (
        args.codex_home or default_codex_home()
    ).expanduser()
    codex_bin = args.codex_bin.expanduser() if args.codex_bin else None
    selected = selected_platforms(args.platform)
    adapters: list[FrontendAdapter] = []

    if "aionui" in selected:
        aionui_home = args.aionui_codex_home or native_codex_home
        aionui_databases = (
            (args.aionui_db,)
            if args.aionui_db is not None
            else discover_aionui_databases(appdata)
        )
        if not aionui_databases:
            aionui_databases = (
                appdata / "AionUi" / "aionui" / "aionui.db",
            )
        for aionui_db in aionui_databases:
            adapters.append(
                AionUIAdapter(
                    database=Path(aionui_db),
                    codex_home=Path(aionui_home),
                    codex_bin_hint=codex_bin,
                )
            )

    if "cindy" in selected:
        cindy_profiles = resolve_cindy_profiles(
            appdata,
            root=args.cindy_root,
            database=args.cindy_db,
            codex_home=args.cindy_codex_home,
        )
        if not cindy_profiles:
            root = appdata / "CindyGlobal"
            cindy_profiles = resolve_cindy_profiles(
                appdata,
                root=root,
                codex_home=root / "codex-home",
            )
        for profile in cindy_profiles:
            adapters.append(
                CindyAdapter(
                    database=profile.database,
                    codex_home=profile.codex_home,
                    cindy_root=profile.root,
                    codex_bin_hint=codex_bin,
                )
            )

    if "native" in selected:
        adapters.append(
            NativeIntegrityAdapter(
                codex_home=native_codex_home,
                codex_bin_hint=codex_bin,
            )
        )
    return adapters


__all__ = ["create_default_adapters"]
