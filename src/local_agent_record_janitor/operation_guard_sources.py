"""Frozen local protection locators for top-level operation plan v2.

These sources are not mutation storages. Legacy child/agent v1 approval
payloads remain unchanged and never acquire new runtime authorization.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os
from pathlib import Path
from typing import Any
from types import SimpleNamespace

from .client_contracts import describe_adapter, require_local_location
from .orca_discovery import local_orca_path, reverse_account_profile
from .record_identity import canonical_path

PLAN_V1 = "larj.operation-plan.v1"
PLAN_V2 = "larj.operation-plan.v2"
PLAN_V3 = "larj.operation-plan.v3"


def guard_sources_for(adapters: Iterable[object]) -> list[dict[str, str]]:
    roots = {}
    for adapter in adapters:
        descriptor = describe_adapter(adapter)
        if descriptor.client != "orca":
            continue
        require_local_location(descriptor.host, descriptor.path_namespace)
        if descriptor.profile_root is None:
            raise ValueError("guard_source_root_unavailable")
        root = local_orca_path(descriptor.profile_root, host=descriptor.host, path_namespace=descriptor.path_namespace)
        # Freeze all known profiles, including ones not associated with the
        # current native targets. Association can change before fresh apply.
        roots[canonical_path(root)] = {"client": "orca", "profile_root": canonical_path(root),
                                       "host": "local", "path_namespace": "local"}
    return [roots[key] for key in sorted(roots)]


def validate_guard_sources(document: Mapping[str, Any]) -> tuple[Path, ...]:
    if document.get("schema_version") == PLAN_V1:
        if "guard_sources" in document:
            raise ValueError("guard_sources_require_operation_plan_v2")
        return ()
    if document.get("schema_version") not in {PLAN_V2, PLAN_V3}:
        raise ValueError("operation_plan_schema_invalid")
    values = document.get("guard_sources")
    if not isinstance(values, list) or not values:
        raise ValueError("guard_source_evidence_invalid")
    roots = []
    seen = set()
    for value in values:
        if (not isinstance(value, dict) or set(value) != {"client", "profile_root", "host", "path_namespace"}
                or value["client"] != "orca" or not isinstance(value["profile_root"], str)):
            raise ValueError("guard_source_evidence_invalid")
        # Host/namespace and OS-qualified locator validation precede Path.
        root = local_orca_path(value["profile_root"], host=value["host"], path_namespace=value["path_namespace"])
        key = str(root.absolute()).casefold() if os.name == "nt" else str(root.absolute())
        if key in seen:
            raise ValueError("guard_source_evidence_duplicate")
        seen.add(key)
        roots.append(root)
    return tuple(roots)


def retain_guard_sources(adapters: Iterable[object], roots: Iterable[str | Path]) -> tuple[object, ...]:
    """Union required sources; callers cannot replace frozen roots with new ones."""
    from .adapters.orca import OrcaAdapter

    result = list(adapters)
    present = {canonical_path(d.profile_root) for adapter in result
               if (d := describe_adapter(adapter)).client == "orca" and d.profile_root is not None}
    for raw in roots:
        root = local_orca_path(raw)
        key = canonical_path(root)
        if key not in present:
            result.append(OrcaAdapter(profile_root=root))
            present.add(key)
    return tuple(result)


def current_guard_sources(adapters: Iterable[object] = (), *, client: str = "native",
                          codex_home: Path | None = None, orca_roots: Iterable[str | Path] = (),
                          appdata: Path | None = None) -> tuple[object, ...]:
    """Bounded current/default, explicit and existing protection-source union."""
    from .adapter_factory import discover_orca_guards

    adapters = tuple(adapters)
    # A bound multi-provider client may delete a shared native record. Keep
    # known frontend owners even when candidate discovery selected only it.
    from .herdr_bound_adapter import HerdrBoundAdapter
    from .paseo_bound_adapter import PaseoBoundAdapter
    if any(isinstance(adapter, (HerdrBoundAdapter, PaseoBoundAdapter)) for adapter in adapters):
        from .adapter_factory import discover_shared_codex_guards
        present = {(describe_adapter(a).client, canonical_path(getattr(a, "database", None)))
                   for a in adapters if getattr(a, "database", None) is not None}
        adapters += tuple(a for a in discover_shared_codex_guards(appdata=appdata)
                          if (describe_adapter(a).client, canonical_path(a.database)) not in present)
    requested = tuple(orca_roots)
    # A supplied explicit profile is the selection. Still observe an existing
    # default protection profile, but do not invent a missing default as a
    # second required selected profile.
    discovery_client = "native" if any(describe_adapter(a).client == "orca" for a in adapters) else client
    args = SimpleNamespace(client=discovery_client, platform=[discovery_client], codex_home=codex_home,
                           orca_root=(), appdata=appdata, codex_bin=None)
    discovered = discover_orca_guards(args)
    if requested:
        args.orca_root = requested
        discovered += discover_orca_guards(args)
    roots = [adapter.profile_root for adapter in discovered]
    # Supplied native adapters can identify an account profile even when the
    # selected target is an unrelated runtime home. Keep that known source in
    # the active/frozen union; a later association must survive fresh apply.
    for adapter in adapters:
        descriptor = describe_adapter(adapter)
        require_local_location(descriptor.host, descriptor.path_namespace)
        for store in descriptor.native_stores:
            if store.backend == "codex":
                profile = reverse_account_profile(local_orca_path(store.path))
                if profile is not None:
                    roots.append(profile)
    return retain_guard_sources(adapters, roots)


def refresh_guard_sources(adapters: Iterable[object]) -> tuple[object, ...]:
    """Refresh known product metadata, without adding native catalog targets."""
    adapters = tuple(adapters)
    for adapter in adapters:
        from .herdr_bound_adapter import HerdrBoundAdapter
        from .paseo_bound_adapter import PaseoBoundAdapter
        client = describe_adapter(adapter).client
        if client in {"cindy", "aionui"}:
            invalidate = getattr(adapter, "invalidate_frontend_snapshot", None)
            if callable(invalidate):
                invalidate()
        if client == "orca" or isinstance(adapter, (HerdrBoundAdapter, PaseoBoundAdapter)):
            adapter.snapshot_references(refresh=True)
    return adapters


def required_source_errors(document: Mapping[str, Any], adapters: Iterable[object]) -> tuple[str, ...]:
    """Frozen/current required metadata cannot fail into an empty guard set."""
    if document.get("schema_version") not in {PLAN_V2, PLAN_V3}:
        return ()
    roots = {canonical_path(root) for root in validate_guard_sources(document)}
    known = [(describe_adapter(adapter), adapter) for adapter in adapters]
    roots.update(canonical_path(descriptor.profile_root) for descriptor, _ in known
                 if descriptor.client == "orca" and descriptor.profile_root is not None)
    storages = {str(s.get("storage_id")): s.get("path") for s in document.get("storages", ())}
    targets = set()
    for action in document.get("actions", ()):
        impact = action.get("impact", {})
        root = impact.get("external_storage_root") or storages.get(str(action.get("target", {}).get("storage_id")))
        if root:
            targets.add((impact.get("external_engine") or "codex", canonical_path(root)))
    errors = []
    from .orca_cleanup import frontend_evidence, covers_error
    frontends = frontend_evidence(document)
    for descriptor, adapter in known:
        if descriptor.client != "orca" or descriptor.profile_root is None or canonical_path(descriptor.profile_root) not in roots:
            continue
        roots.discard(canonical_path(descriptor.profile_root))
        snapshot = adapter.snapshot_references()
        closure = frontends.get(canonical_path(descriptor.profile_root))
        errors.extend(error.message for error in snapshot.errors if error.blocks_delete
                      and (error.store is None or (error.store.backend, error.store.canonical_path) in targets)
                      and not (closure is not None and covers_error(closure, error)))
    if roots:
        errors.append("frozen_guard_source_unavailable")
    return tuple(dict.fromkeys(errors))
