from __future__ import annotations

from pathlib import Path
import os
from typing import Any

from .adapters import AionUIAdapter, CindyAdapter, NativeIntegrityAdapter, OrcaAdapter
from .orca_discovery import default_orca_root, local_orca_path, reverse_account_profile
from .record_identity import canonical_path
from .cleanup_service import selected_platforms
from .discovery import (
    default_appdata,
    default_codex_home,
    discover_aionui_databases,
    resolve_cindy_profiles,
)


def discover_orca_guards(args: Any) -> tuple[OrcaAdapter, ...]:
    """Only default/env/explicit profiles and an exact native account marker.

    A missing default profile contributes no new recovery requirement. An
    explicit/frozen profile remains required even if it disappears or fails.
    """
    requested = tuple(getattr(args, "orca_root", ()) or ())
    selected = str(getattr(args, "client", "")) == "orca" or "orca" in (getattr(args, "platform", ()) or ())
    roots = [local_orca_path(root) for root in requested]
    if not requested:
        default = default_orca_root(appdata=getattr(args, "appdata", None))
        try:
            default.lstat()
        except FileNotFoundError:
            if selected or os.environ.get("ORCA_USER_DATA_PATH"):
                roots.append(default)
        except OSError:
            roots.append(default)
        else:
            roots.append(default)
    home = getattr(args, "codex_home", None)
    if home is not None:
        profile = reverse_account_profile(Path(home).expanduser().absolute())
        if profile is not None:
            roots.append(profile)
    unique = {canonical_path(root): root for root in roots}
    return tuple(OrcaAdapter(profile_root=root, codex_bin_hint=getattr(args, "codex_bin", None))
                 for root in unique.values())


def create_default_adapters(args: Any) -> list[object]:
    """Build adapters without coupling either command driver to the other."""

    appdata = (args.appdata or default_appdata()).expanduser()
    native_codex_home = (
        args.codex_home or default_codex_home()
    ).expanduser()
    codex_bin = args.codex_bin.expanduser() if args.codex_bin else None
    selected = selected_platforms(args.platform)
    adapters: list[object] = []

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
    # Protection discovery is independent of candidate selection. Typed
    # adapters never become legacy native/home or scan candidates.
    adapters.extend(discover_orca_guards(args))
    return adapters


__all__ = ["create_default_adapters", "discover_orca_guards"]
