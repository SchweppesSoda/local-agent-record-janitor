from __future__ import annotations

from pathlib import Path
import os
from typing import Any

from .adapters import AionUIAdapter, CindyAdapter, NativeIntegrityAdapter, OrcaAdapter, HerdrAdapter, WorkBuddyAdapter, OfficeAdapter
from .herdr_discovery import default_herdr_locators, local_herdr_path
from .orca_discovery import OrcaDiscoveryError, default_orca_root, local_orca_path, reverse_account_profile
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
        try:
            default = default_orca_root(appdata=getattr(args, "appdata", None))
        except OrcaDiscoveryError:
            if selected or os.environ.get("ORCA_USER_DATA_PATH"):
                raise
            # An invalid optional default cannot locate a protection source.
            # Continue checking exact native account markers below.
        else:
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


def discover_herdr_adapters(args: Any) -> tuple[HerdrAdapter, ...]:
    """Bounded profiles; socket queries require selected Herdr + inspection."""
    requested = tuple(getattr(args, "herdr_root", ()) or ())
    selected = str(getattr(args, "client", "")) == "herdr" or "herdr" in (getattr(args, "platform", ()) or ())
    roots = [os.fspath(root) for root in requested]
    for root in roots:
        local_herdr_path(root)
    if not requested:
        try:
            defaults = default_herdr_locators(appdata=getattr(args, "appdata", None))
        except ValueError:
            # A rootless product with no proven native association cannot
            # globally disable an unrelated client's native store.
            return (HerdrAdapter(profile_root=None, discovery_error="herdr_environment_root_unproven"),) if selected else ()
        for root in defaults:
            try:
                local_herdr_path(root).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                roots.append(root)
            else:
                roots.append(root)
        if selected and not roots:
            roots.append(defaults[0])
    # The same filesystem root may have distinct named-pipe spellings. Keep
    # each explicitly known spelling for the opt-in observation.
    unique = dict.fromkeys(roots)
    inspect_live = selected and bool(getattr(args, "inspect_clients", False))
    return tuple(HerdrAdapter(profile_root=root, inspect_live=inspect_live) for root in unique)


def create_default_adapters(args: Any) -> list[object]:
    """Build adapters without coupling either command driver to the other."""

    appdata = (args.appdata or default_appdata()).expanduser()
    native_codex_home = (
        args.codex_home or default_codex_home()
    ).expanduser()
    codex_bin = args.codex_bin.expanduser() if args.codex_bin else None
    selected = selected_platforms(args.platform)
    adapters: list[object] = []
    from .office_store import PROFILES, default_profile_roots
    for client in PROFILES:
        requested = tuple(getattr(args, client + "_root", ()) or ())
        if client in selected or str(getattr(args, "client", "")) == client or requested:
            roots = requested or default_profile_roots(client, appdata=getattr(args, "appdata", None))
            unique = {os.path.normcase(os.path.normpath(os.fspath(root))): root for root in roots}
            adapters.extend(OfficeAdapter(client=client, profile_root=root,
                sdk_root=getattr(args, client + "_sdk_root", None)) for root in unique.values())

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
    adapters.extend(discover_herdr_adapters(args))
    # WorkBuddy has no proven shared Codex/Claude store. Its default profile
    # is a candidate only when selected, never an unrelated global guard.
    workbuddy_roots = tuple(getattr(args, "workbuddy_root", ()) or ())
    if "workbuddy" in selected or str(getattr(args, "client", "")) == "workbuddy" or workbuddy_roots:
        from .workbuddy_store import local_root
        roots = workbuddy_roots or (os.environ.get("WORKBUDDY_CONFIG_DIR") or Path.home() / ".workbuddy",)
        unique = {canonical_path(local_root(root)): local_root(root) for root in roots}
        adapters.extend(WorkBuddyAdapter(profile_root=root) for root in unique.values())
    return adapters


__all__ = ["create_default_adapters", "discover_orca_guards", "discover_herdr_adapters"]
