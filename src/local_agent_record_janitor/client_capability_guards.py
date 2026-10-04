"""Downward client/profile limits at legacy planning and writer boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from .action_registry import capability_field_for_action
from .client_contracts import ClientDescriptor, ReferenceSnapshot, SourceFailure, describe_adapter, aggregate_capabilities, restrict_capability
from .record_identity import EngineCapability, canonical_path, normalize_engine


CLIENT_CAPABILITY_LIMIT = "client_capability_limit"


@dataclass(frozen=True)
class ClientCapabilityLimits:
    """Explicit maxima, matched to exact metadata sources or native stores.

    This guard never registers a writer or supplies missing runtime evidence.
    A descriptor without a locatable source/store cannot narrow its scope, so
    its restriction applies to the selected operation as a whole.
    """

    descriptors: tuple[ClientDescriptor, ...] = ()
    native_source_failures: tuple[SourceFailure, ...] = ()

    @classmethod
    def from_adapters(cls, adapters: Iterable[object]) -> ClientCapabilityLimits:
        descriptors = []
        failures = []
        adapters = list(adapters)
        # Direct catalog/manual/cleaner callers may know only an exact native
        # home. Its observed account marker is a bounded protection source;
        # never discover arbitrary/default product roots from this low layer.
        from .orca_discovery import reverse_account_profile
        known_profiles = set()
        for adapter in adapters:
            descriptor = describe_adapter(adapter)
            if descriptor.client == "orca" and descriptor.profile_root is not None:
                known_profiles.add(canonical_path(descriptor.profile_root))
        for adapter in tuple(adapters):
            for store in describe_adapter(adapter).native_stores:
                if store.backend != "codex":
                    continue
                profile = reverse_account_profile(store.path)
                if profile is not None and canonical_path(profile) not in known_profiles:
                    from .adapters.orca import OrcaAdapter
                    adapters.append(OrcaAdapter(profile_root=profile))
                    known_profiles.add(canonical_path(profile))
        for adapter in adapters:
            descriptor = describe_adapter(adapter)
            descriptors.append(descriptor)
            reader = getattr(adapter, "snapshot_references", None)
            if callable(reader) and not callable(getattr(adapter, "snapshot_sessions", None)):
                try:
                    snapshot = reader()
                    if not isinstance(snapshot, ReferenceSnapshot) or snapshot.descriptor != descriptor:
                        raise ValueError("Reference snapshot differs from its local descriptor")
                    failures.extend(error for error in snapshot.errors if error.blocks_delete and error.store is not None)
                except Exception:
                    # A failed source can block only the stores it already
                    # locates. Unknown/rootless evidence cannot identify a
                    # different native root merely by a raw session ID.
                    failures.extend(SourceFailure(str(descriptor.sources[0]) if descriptor.sources else "client-references",
                        "reference_snapshot_unavailable", profile_root=descriptor.profile_root, store=store)
                        for store in descriptor.native_stores)
        return cls(tuple(descriptors), tuple(dict.fromkeys(failures)))

    def matching(self, engine: str, *, sources: Sequence[Path | str] = (),
                 native_root: Path | str | None = None, frontend: bool = False) -> tuple[ClientDescriptor, ...]:
        source_keys = {canonical_path(path) for path in sources}
        root_key = canonical_path(native_root) if native_root is not None else None
        target_unscoped = not source_keys and root_key is None
        matched = []
        for descriptor in self.descriptors:
            source_match = bool(source_keys & descriptor._source_keys)
            store_match = root_key is not None and any(
                store.backend == engine and store.canonical_path == root_key for store in descriptor.native_stores)
            unscoped = not descriptor.sources and not descriptor.native_stores
            if source_match or unscoped or target_unscoped or store_match and (not frontend or not source_keys):
                matched.append(descriptor)
        return tuple(matched)

    def reasons(self, engine: str, field: str, *, sources: Sequence[Path | str] = (),
                native_root: Path | str | None = None, target_ids: Sequence[str] = (),
                execution: bool = False) -> tuple[str, ...]:
        from .orca_authorization import permits
        reasons = list(
            f"{CLIENT_CAPABILITY_LIMIT}: {descriptor.client}/{engine} {field} is unavailable"
            + (f" ({limit.reason})" if limit.reason else "")
            for descriptor in self.matching(engine, sources=sources, native_root=native_root,
                                            frontend=field.startswith("frontend_"))
            if not getattr(limit := descriptor.limit_for(engine), field)
            and not (descriptor.client == "orca" and engine == "codex" and field == "native_delete"
                     and permits(native_root, target_ids, execution=execution))
        )
        if native_root is not None and (field == "native_delete" or field == "frontend_project_delete" and not sources):
            root_key = canonical_path(native_root)
            reasons.extend(f"{CLIENT_CAPABILITY_LIMIT}: native source evidence is incomplete ({error.message})"
                for error in self.native_source_failures
                if error.store.backend == engine and error.store.canonical_path == root_key)
        return tuple(dict.fromkeys(reasons))

    def action_reasons(self, plan: Any, action: Any) -> tuple[str, ...]:
        if not self.descriptors:
            return ()
        field = capability_field_for_action(getattr(action, "kind", ""))
        if field is None:
            return ()
        engine, root, sources = action_location(plan, action)
        if str(getattr(getattr(action, "kind", ""), "value", getattr(action, "kind", ""))) == "delete_native_project":
            sources = ()  # Native metadata uses its store's ceiling, not a frontend DB source.
        engines = action_frontend_engines(plan, action) if field.startswith("frontend_") else (engine,)
        if not engines and field == "frontend_project_delete":
            # An orphan frontend project row has no backend references. Its
            # already-proven project writer uses the existing namespace;
            # missing reference evidence cannot invent a Pi/Claude target.
            engines = (engine,)
        if not engines:
            # Legacy exact-row evidence may omit its backend. It can retain
            # the old route only when every possible declared engine permits
            # that family; a Codex default cannot hide a tighter Pi ceiling.
            matching = self.matching(engine, sources=sources, native_root=root, frontend=True)
            engines = tuple(dict.fromkeys(limit.engine for descriptor in matching
                                           for limit in descriptor.capability_limits)) or (engine,)
        return tuple(dict.fromkeys(reason for value in engines
            for reason in self.reasons(value, field, sources=sources, native_root=root,
                target_ids=(str(action.target.thread_id), *getattr(getattr(action, "impact", None), "descendant_thread_ids", ()),
                            *getattr(getattr(action, "impact", None), "affected_thread_ids", ())))))

    def restrict_plan(self, plan: Any) -> Any:
        if not self.descriptors:
            return plan
        changed = {}
        actions = []
        for action in plan.actions:
            reasons = self.action_reasons(plan, action)
            if reasons and action.available:
                reason = "; ".join(dict.fromkeys((*(filter(None, (action.unavailable_reason,))), *reasons)))
                action = replace(action, available=False, unavailable_reason=reason)
                changed[str(action.action_id)] = reason
            actions.append(action)
        if not changed:
            return plan
        fingerprint = hashlib.sha256(json.dumps(
            {"original": plan.plan_fingerprint, "limits": sorted(changed.items())},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return replace(plan, actions=tuple(actions),
                       planned_actions=tuple(a for a in plan.planned_actions if str(a.action_id) not in changed),
                       plan_fingerprint="capability:v1:" + fingerprint)

    def restrict_summary(self, capability: EngineCapability) -> EngineCapability:
        limits = [d.limit_for(capability.engine) for d in self.descriptors if d.client == capability.client]
        return restrict_capability(capability, aggregate_capabilities(limits)) if limits else capability


def action_location(plan: Any, action: Any) -> tuple[str, Path | str | None, tuple[Path | str, ...]]:
    """Read the existing writer's storage/source evidence without new scans."""
    impact = getattr(action, "impact", None)
    raw_engine = getattr(impact, "external_engine", None)
    kind = str(getattr(getattr(action, "kind", ""), "value", getattr(action, "kind", "")))
    engine = normalize_engine(raw_engine or (
        "pi" if kind == "delete_pi_session" else "claude" if kind == "delete_claude_session" else "codex"))
    root = getattr(impact, "external_storage_root", None)
    if root is None:
        root = next((storage.path for storage in plan.storages
                     if str(storage.storage_id) == str(action.target.storage_id)), None)
    fields = ("frontend_database_paths", "frontend_session_database_paths", "frontend_project_database_paths")
    sources = [path for name in fields for path in getattr(impact, name, ())]
    observation_ids = set(getattr(action, "observation_ids", ()))
    sources.extend(path for observation in getattr(plan, "observations", ())
                   if getattr(observation, "observation_id", None) in observation_ids
                   and (path := getattr(observation, "platform_db", None)))
    # Pi/Claude manifests retain exact frontend source evidence in their
    # existing native approval payload; no frontend rescan is needed here.
    payload = getattr(impact, "external_action_payload", None)
    if isinstance(payload, dict):
        if kind in {"delete_schedule_run", "delete_workbuddy_session", "remove_workbuddy_ui_reference"}:
            evidence = payload.get("workbuddy_session_evidence" if kind != "delete_schedule_run"
                                   else "schedule_run_evidence")
            if isinstance(evidence, dict) and evidence.get("database"):
                sources.append(evidence["database"])
        if engine == "pi" and payload.get("session_root"):
            root = payload["session_root"]
        for name in ("frontend_references", "cindy_references"):
            for reference in payload.get(name, ()) or ():
                if isinstance(reference, dict):
                    path = reference.get("database") or reference.get("source_path")
                    if path:
                        sources.append(path)
    return engine, root, tuple(dict.fromkeys(sources))


def action_frontend_engines(plan: Any, action: Any) -> tuple[str, ...]:
    """A grouped exact-row mutation must satisfy every known backend limit."""
    impact = getattr(action, "impact", None)
    values = []
    if getattr(impact, "external_engine", None):
        values.append(normalize_engine(impact.external_engine))
    wanted = set(getattr(action, "observation_ids", ()))
    for observation in getattr(plan, "observations", ()):
        if getattr(observation, "observation_id", None) not in wanted:
            continue
        details = getattr(observation, "details", {})
        backend = details.get("backend")
        if isinstance(backend, str) and backend:
            values.append(normalize_engine(backend))
        raw_kind = details.get("agent_kind")
        if isinstance(raw_kind, str) and raw_kind:
            from .cindy_references import backend_for_agent_kind
            values.append(backend_for_agent_kind(raw_kind))
    for name in ("frontend_reference_evidence", "frontend_session_evidence"):
        for reference in getattr(impact, name, ()):
            expected = reference.get("expected", {})
            backend = reference.get("backend")
            if isinstance(backend, str) and backend:
                values.append(normalize_engine(backend))
            raw_kind = expected.get("agent_kind")
            if isinstance(raw_kind, str) and raw_kind:
                from .cindy_references import backend_for_agent_kind
                values.append(backend_for_agent_kind(raw_kind))
    return tuple(dict.fromkeys(values))


def restrict_cleanup_context(context: Any, adapters: Iterable[object], typed_action_builder: Any) -> Any:
    """Keep one context's candidate limits and execution bindings together."""
    adapters = tuple(adapters)
    plan = ClientCapabilityLimits.from_adapters(adapters).restrict_plan(context.plan)
    active_by_id = {id(adapter): adapter for adapter in (*context.active_adapters, *adapters)}
    active = tuple(active_by_id.values())
    snapshot = context.snapshot if active == context.active_adapters else replace(context.snapshot, active_adapters=active)
    builder = context.adapter_builder
    if builder is not None and active != context.active_adapters:
        def builder_with_guards() -> tuple[object, ...]:
            return tuple({id(adapter): adapter for adapter in (*active, *builder())}.values())
        guarded_builder = builder_with_guards
    else:
        guarded_builder = builder
    if plan is context.plan and snapshot is context.snapshot:
        return context
    return replace(context, plan=plan, snapshot=snapshot, actions=typed_action_builder(plan),
                   adapter_builder=guarded_builder)


def restrict_finding(finding: Any, limits: ClientCapabilityLimits) -> Any:
    reasons = limits.reasons("codex", "native_delete", native_root=finding.codex_home,
                            sources=(finding.platform_db,) if finding.platform_db else ())
    if not reasons:
        return finding
    details = dict(finding.details)
    details.update(cleanable=False, thread_delete_supported=False,
                   cleanup_blocked_reason="; ".join(filter(None, (details.get("cleanup_blocked_reason"), *reasons))),
                   cleanup_blocker_codes=list(dict.fromkeys((*details.get("cleanup_blocker_codes", ()), CLIENT_CAPABILITY_LIMIT))))
    return replace(finding, details=details)
