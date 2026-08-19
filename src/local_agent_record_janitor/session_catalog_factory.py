from __future__ import annotations

from pathlib import Path
from typing import Any

from .discovery import (
    CindyProfile,
    default_appdata,
    resolve_cindy_profiles,
)
from .path_identity import canonical_existing_path_key


def build_pi_catalog(args: Any, builder: Any | None = None) -> Any:
    """Build one ownership-qualified Pi inventory for either CLI facade."""

    if builder is not None:
        return builder(
            agent_dir=getattr(args, "pi_agent_dir", None),
            session_root=getattr(args, "pi_session_dir", None),
        )

    from .pi_sessions import (
        build_pi_root_qualified_catalog,
        build_pi_session_inventory,
    )

    appdata = (
        getattr(args, "appdata", None) or default_appdata()
    ).expanduser()
    cindy_profiles = resolve_cindy_profiles(
        appdata,
        root=getattr(args, "cindy_root", None),
        database=getattr(args, "cindy_db", None),
        codex_home=getattr(args, "cindy_codex_home", None),
    )
    if (
        getattr(args, "pi_agent_dir", None) is not None
        or getattr(args, "pi_session_dir", None) is not None
    ):
        return build_pi_root_qualified_catalog(
            agent_dir=getattr(args, "pi_agent_dir", None),
            session_root=getattr(args, "pi_session_dir", None),
            cindy_profiles=cindy_profiles,
        )
    return build_pi_session_inventory(
        standalone_options={},
        cindy_profiles=cindy_profiles,
    )


def build_claude_catalog(args: Any, builder: Any | None = None) -> Any:
    """Build one ownership-qualified Claude inventory for either CLI facade."""

    if builder is not None:
        return builder(
            claude_config_dir=getattr(args, "claude_config_dir", None)
        )

    from .cindy_references import (
        CindyReferenceFailure,
        build_cindy_reference_catalog,
    )
    from .claude_sessions import (
        build_claude_multi_root_catalog,
        resolve_claude_paths,
    )

    configured_root = getattr(args, "claude_config_dir", None)
    effective = resolve_claude_paths(config_dir=configured_root)
    roots: list[Path] = [effective.config_dir]
    explicit_root = configured_root is not None
    appdata = (
        getattr(args, "appdata", None) or default_appdata()
    ).expanduser()
    profiles: list[Any] = list(
        resolve_cindy_profiles(
            appdata,
            root=getattr(args, "cindy_root", None),
            database=getattr(args, "cindy_db", None),
            codex_home=getattr(args, "cindy_codex_home", None),
        )
    )
    if all(
        getattr(args, name, None) is None
        for name in ("cindy_root", "cindy_db", "cindy_codex_home")
    ):
        known_profile_roots = {_path_identity(profile.root) for profile in profiles}
        for brand in ("CindyGlobal", "Cindy", "CindyDev", "xdt-maker"):
            root = appdata / brand
            if _path_identity(root) in known_profile_roots:
                continue
            try:
                has_claude_home = (root / "claude-home").is_dir()
            except OSError:
                has_claude_home = False
            if has_claude_home:
                profiles.append(
                    CindyProfile(
                        root=root,
                        database=root / "cindy-local-v1.db",
                        codex_home=root / "codex-home",
                    )
                )

    default_root = Path.home() / ".claude"
    qualified_references: list[dict[str, Any]] = []
    reference_failures: list[Any] = []
    seen_databases: set[str] = set()
    for profile in profiles:
        database_key = _path_identity(profile.database)
        if database_key in seen_databases:
            continue
        seen_databases.add(database_key)
        reference_catalog = build_cindy_reference_catalog(
            profile.database,
            profile_root=profile.root,
        )
        cc_references = tuple(reference_catalog.for_backend("claude"))
        dedicated_root = profile.root / "claude-home"
        try:
            dedicated_exists = dedicated_root.is_dir()
        except OSError as exc:
            dedicated_exists = False
            reference_failures.append(
                CindyReferenceFailure(
                    profile.database,
                    profile.root,
                    type(exc).__name__,
                    "Could not inspect Cindy Claude storage root",
                )
            )

        if dedicated_exists and not explicit_root:
            roots.append(dedicated_root)

        target_root: Path | None
        production_profile = profile.root.name.casefold() in {
            "cindyglobal",
            "cindy",
        }
        if production_profile and effective.config_dir_source in {
            "default",
            "environment",
        }:
            target_root = effective.config_dir
            if cc_references and not explicit_root:
                roots.append(target_root)
        elif production_profile and _path_identity(
            effective.config_dir
        ) == _path_identity(default_root):
            target_root = default_root
        elif production_profile:
            target_root = None
            if cc_references:
                reference_failures.append(
                    CindyReferenceFailure(
                        profile.database,
                        profile.root,
                        "AmbiguousStorageRoot",
                        "Explicit Claude config root cannot be uniquely "
                        "attributed to production Cindy",
                    )
                )
        elif dedicated_exists:
            target_root = dedicated_root
        else:
            target_root = None
            if cc_references:
                reference_failures.append(
                    CindyReferenceFailure(
                        profile.database,
                        profile.root,
                        "AmbiguousStorageRoot",
                        "Cindy Claude references have no unique Claude config root",
                    )
                )

        for failure in reference_catalog.failures:
            if target_root is None:
                reference_failures.append(failure)
            else:
                reference_failures.append(
                    {
                        **failure.to_dict(),
                        "config_dir": str(target_root),
                    }
                )
        if target_root is None:
            continue
        if explicit_root and _path_identity(target_root) != _path_identity(
            effective.config_dir
        ):
            continue
        for reference in cc_references:
            qualified = reference.to_dict()
            qualified["claude_config_dir"] = str(target_root)
            qualified_references.append(qualified)

    unique_roots: list[Path] = []
    seen_roots: set[str] = set()
    for root in roots:
        key = _path_identity(root)
        if key not in seen_roots:
            seen_roots.add(key)
            unique_roots.append(root)
    return build_claude_multi_root_catalog(
        unique_roots,
        frontend_references=qualified_references,
        reference_errors=reference_failures,
    )


def _path_identity(value: Any) -> str:
    return canonical_existing_path_key(Path(value).expanduser())


__all__ = ["build_claude_catalog", "build_pi_catalog"]
