"""Client-scoped inventory and project/record selection.

The public functions in this module freeze one selected client's metadata
snapshot and resolve project or record scope without merging storage identities.
No transcript/body is read.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .adapters.base import FrontendBatchSnapshot
from .client_contracts import (
    CAPABILITY_FIELDS, ClientContractError, ClientDescriptor, ClientReference,
    ReferenceSnapshot, RelationEvidence, RelationKind, SourceFailure,
    aggregate_capabilities, describe_adapter, frontend_binding_key,
    reference_from_session, restrict_capability,
)
from .display_metadata import display_title
from .file_alias_evidence import FileAliasSnapshot, probe_file_aliases
from .inventory import (
    FrontendSessionRecord,
    InventoryFailure,
    ManagedConversation,
    SessionCatalog,
    build_session_catalog,
    classify_managed_conversation,
)
from .record_identity import (
    EngineCapability,
    NATIVE_ROOT_UNVERIFIED,
    ProjectKey,
    ProjectSelectionError,
    RecordClassification,
    RecordKey,
    StoreKey,
    canonical_path,
    classify_record_state,
    normalize_client,
    normalize_engine,
    resolve_project_selector,
)


class ClientInventoryError(ValueError):
    """The requested client inventory or selector is not usable."""


@dataclass(frozen=True)
class ClientTarget:
    """One immutable record/reference target exposed to a planner."""

    client: str
    engine: str
    record_key: RecordKey | None
    project_key: ProjectKey | None
    native_thread_id: str | None
    frontend_reference_ids: tuple[str, ...]
    classification: RecordClassification
    capability: EngineCapability
    action_ids: tuple[str, ...] = ()
    blocker_codes: tuple[str, ...] = ()
    blockers: tuple[Mapping[str, Any], ...] = ()
    project_row_evidence: tuple[Mapping[str, Any], ...] = ()
    display_name: str | None = None
    display_name_source: str | None = None
    is_subagent: bool = False
    parent_thread_ids: tuple[str, ...] = ()
    descendant_thread_ids: tuple[str, ...] = ()
    lineage_status: str | None = None
    # Display IDs are not identities: one UI row can retain several bindings.
    frontend_binding_keys: tuple[str, ...] = ()
    references: tuple[ClientReference, ...] = ()
    relations: tuple[RelationEvidence, ...] = ()

    @property
    def record_id(self) -> str | None:
        return self.record_key.record_id if self.record_key else self.native_thread_id

    @property
    def identifiers(self) -> tuple[str, ...]:
        values: list[str] = [value for value in self.action_ids if value not in {
            "delete_native", "delete_pi_session", "delete_claude_session",
            "delete_frontend_session", "delete_frontend_reference", "delete_frontend_project",
        }]
        if self.record_key is not None:
            values.extend((self.record_key.value, self.record_key.record_id))
        if self.native_thread_id:
            values.append(self.native_thread_id)
        values.extend(self.frontend_reference_ids)
        values.extend(self.frontend_binding_keys)
        return tuple(dict.fromkeys(value for value in values if value))

    def to_dict(self) -> dict[str, Any]:
        lineage_status = self.lineage_status or (
            "conflict" if "lineage_conflict" in self.blocker_codes else
            "known" if self.parent_thread_ids else
            "unknown" if self.is_subagent else "root"
        )
        return {
            "client": self.client,
            "engine": self.engine,
            "record_key": self.record_key.to_dict() if self.record_key else None,
            "project_key": self.project_key.to_dict() if self.project_key else None,
            "native_thread_id": self.native_thread_id,
            "record_id": self.record_id,
            "display_name": display_title(self.display_name),
            "display_name_source": self.display_name_source,
            "record_kind": "subagent" if self.is_subagent else "conversation",
            "is_subagent": self.is_subagent,
            "parent_thread_ids": list(self.parent_thread_ids),
            "descendant_thread_ids": list(self.descendant_thread_ids),
            "lineage_status": lineage_status,
            "cleanup_eligible": (
                bool(self.action_ids) and not self.blockers and not self.blocker_codes
                and lineage_status not in {"unknown", "conflict"}
            ),
            "frontend_reference_ids": list(self.frontend_reference_ids),
            "frontend_binding_keys": list(self.frontend_binding_keys),
            "classification": self.classification.value,
            "capability": self.capability.to_dict(),
            "action_ids": list(self.action_ids),
            "blocker_codes": list(self.blocker_codes),
            "blockers": [dict(blocker) for blocker in self.blockers],
            "project_row_evidence": [
                dict(item) for item in self.project_row_evidence
            ],
            "references": [reference.to_dict() for reference in self.references],
            "relations": [relation.to_dict() for relation in self.relations],
        }


@dataclass(frozen=True)
class ClientInventory:
    """A single-client, immutable inventory snapshot."""

    client: str
    engines: tuple[str, ...]
    projects: tuple[ProjectKey, ...]
    records: tuple[ManagedConversation, ...]
    frontend_sessions: tuple[FrontendSessionRecord, ...]
    unmapped_frontend_sessions: tuple[FrontendSessionRecord, ...]
    targets: tuple[ClientTarget, ...]
    capabilities: Mapping[str, EngineCapability]
    errors: tuple[InventoryFailure | SourceFailure, ...] = ()
    frontend_snapshots: tuple[FrontendBatchSnapshot, ...] = ()
    project_items: tuple[Any, ...] = ()
    scanned_databases: tuple[Path, ...] = ()
    scanned_resources: tuple[tuple[str, str], ...] = ()
    descriptors: tuple[ClientDescriptor, ...] = ()
    references: tuple[ClientReference, ...] = ()
    _reference_sources: Mapping[str, tuple[Path, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        sources: dict[str, list[Path]] = {}
        for reference in self.references:
            sources.setdefault(reference.binding_key, []).append(reference.source)
        object.__setattr__(self, "_reference_sources", {key: tuple(dict.fromkeys(paths)) for key, paths in sources.items()})

    @property
    def catalog(self) -> SessionCatalog:
        return SessionCatalog(
            records=self.records,
            unmapped_frontend_sessions=self.unmapped_frontend_sessions,
            errors=self.errors,
        )

    @property
    def actions(self) -> tuple[ClientTarget, ...]:
        return self.targets

    def select(
        self,
        *,
        project_selectors: Sequence[str] = (),
        all_projects: bool = False,
        record_ids: Sequence[str] = (),
        engines: Sequence[str] = (),
    ) -> "ClientSelection":
        return select_client_targets(
            self,
            project_selectors=project_selectors,
            all_projects=all_projects,
            record_ids=record_ids,
            engines=engines,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "client": self.client,
            "engines": list(self.engines),
            "projects": [project.to_dict() for project in self.projects],
            "records": [record.to_dict() for record in self.records],
            "frontend_sessions": [
                session.to_dict() for session in self.frontend_sessions
            ],
            "unmapped_frontend_sessions": [
                session.to_dict() for session in self.unmapped_frontend_sessions
            ],
            "targets": [target.to_dict() for target in self.targets],
            "actions": [target.to_dict() for target in self.actions],
            "capabilities": {
                engine: capability.to_dict()
                for engine, capability in sorted(self.capabilities.items())
            },
            "errors": [error.to_dict() for error in self.errors],
            "frontend_snapshots": [
                snapshot.to_dict() for snapshot in self.frontend_snapshots
            ],
            "project_items": [
                item.to_dict()
                if callable(getattr(item, "to_dict", None))
                else dict(item)
                if isinstance(item, Mapping)
                else str(item)
                for item in self.project_items
            ],
            "clients": [descriptor.to_dict() for descriptor in self.descriptors],
            "references": [reference.to_dict() for reference in self.references],
        }


@dataclass(frozen=True)
class ClientSelection:
    """Resolved scope for one immutable client inventory."""

    inventory: ClientInventory
    project_keys: tuple[ProjectKey, ...]
    targets: tuple[ClientTarget, ...]
    project_selectors: tuple[str, ...] = ()
    all_projects: bool = False
    record_ids: tuple[str, ...] = ()

    @property
    def records(self) -> tuple[ClientTarget, ...]:
        return self.targets

    @property
    def actions(self) -> tuple[ClientTarget, ...]:
        return self.targets

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "client": self.inventory.client,
            "project_selectors": list(self.project_selectors),
            "project_keys": [project.to_dict() for project in self.project_keys],
            "all_projects": self.all_projects,
            "record_ids": list(self.record_ids),
            "targets": [target.to_dict() for target in self.targets],
            "actions": [target.to_dict() for target in self.actions],
            "capabilities": {
                target.engine: target.capability.to_dict()
                for target in self.targets
            },
        }


@dataclass(frozen=True)
class ClientEngineContext:
    """One engine-scoped context that a coordinator can hand to a driver."""

    inventory: ClientInventory
    engine: str
    targets: tuple[ClientTarget, ...]
    frontend_sessions: tuple[FrontendSessionRecord, ...]
    native_catalog: object | None
    capability: EngineCapability

    def __post_init__(self) -> None:
        mismatched = tuple(
            target for target in self.targets
            if (target.capability.client, target.capability.engine) != (self.capability.client, self.capability.engine)
            or any(getattr(target.capability, field) and not getattr(self.capability, field) for field in CAPABILITY_FIELDS)
        )
        if mismatched:
            raise AssertionError(
                "ClientEngineContext summary must include each target's narrower capability"
            )

    @property
    def native_records(self) -> tuple[Any, ...]:
        value = getattr(self.native_catalog, "records", ())
        return tuple(value) if isinstance(value, (tuple, list)) else ()

    @property
    def actions(self) -> tuple[ClientTarget, ...]:
        return self.targets

    def to_dict(self) -> dict[str, Any]:
        catalog = self.native_catalog
        catalog_value = (
            catalog.to_dict()
            if catalog is not None and callable(getattr(catalog, "to_dict", None))
            else None
        )
        return {
            "client": self.inventory.client,
            "engine": self.engine,
            "targets": [target.to_dict() for target in self.targets],
            "actions": [target.to_dict() for target in self.actions],
            "frontend_sessions": [
                session.to_dict() for session in self.frontend_sessions
            ],
            "native_catalog": catalog_value,
            "native_record_count": len(self.native_records),
            "capability": self.capability.to_dict(),
        }


def collect_client_file_aliases(
    contexts: Sequence[ClientEngineContext], targets: Sequence[ClientTarget],
) -> FileAliasSnapshot:
    """Observe only catalog files belonging to selected qualified targets.

    This separate projection is excluded from target identities, inventory
    snapshot hashes and the existing path/manifest approval payloads.
    """
    root_keys: dict[Path, str] = {}

    def root_key(root: Path) -> str:
        if root not in root_keys:
            root_keys[root] = canonical_path(root)
        return root_keys[root]

    def path_key(path: Path | None) -> str | None:
        return os.path.normcase(os.path.abspath(path)) if path is not None else None

    selected = {(target.engine, root_key(target.record_key.store.path),
                 target.record_key.record_id, path_key(target.record_key.path))
                for target in targets if target.record_key is not None}
    roots: list[Path] = []
    paths: list[Path] = []
    for context in contexts:
        for record in context.native_records:
            root = _native_record_root(record, context.engine)
            if root is None:
                continue
            key = (context.engine, root_key(root), _native_record_id(record, context.engine),
                   path_key(_native_record_path(record, context.engine)))
            if key not in selected:
                continue
            roots.append(root)
            if context.engine == "codex":
                paths.extend(rollout.path for rollout in getattr(record, "rollouts", ()))
            elif context.engine == "pi":
                paths.append(record.path)
            elif context.engine == "claude":
                paths.extend(getattr(record, "transcript_paths", ()))
                paths.extend(entry.path for entry in getattr(record, "manifest", ()) if entry.node_type == "file")
    return probe_file_aliases(paths, roots=roots)


def build_client_engine_contexts(
    adapters: Iterable[object],
    *,
    client: str,
    engines: Sequence[str] = (),
    inventory: ClientInventory | None = None,
    native_catalogs: Mapping[str, object] | None = None,
    writer_capabilities: Mapping[str, EngineCapability] | None = None,
) -> tuple[ClientEngineContext, ...]:
    """Build one immutable, engine-scoped context per selected client engine.

    Pi and Claude catalogs may be supplied by the existing native builders or
    obtained from an adapter exposing native_catalog(). AionUI adapters do not
    invent a native root, so their context remains reference-only unless the
    caller supplies an explicitly qualified catalog and writer capability.
    """

    selected_client = normalize_client(client)
    adapter_list = tuple(adapters)
    if inventory is None:
        inventory = build_client_inventory(
            adapter_list, client=selected_client, engines=engines
        )
    requested = _normalize_engines(engines)
    engine_names = requested or tuple(dict.fromkeys((
        *inventory.engines,
        *(engine for descriptor in inventory.descriptors for engine in descriptor.inventory_engines),
        *(store.backend for descriptor in inventory.descriptors for store in descriptor.native_stores),
        *(normalize_engine(engine) for adapter in adapter_list
          if _adapter_client(adapter) == selected_client
          for engine in getattr(adapter, "inventory_engines", ())),
    )))
    catalogs = native_catalogs or {}
    declared = writer_capabilities or {}
    contexts: list[ClientEngineContext] = []
    for engine in engine_names:
        normalized_engine = normalize_engine(engine)
        frontend_sessions = tuple(
            session for session in inventory.frontend_sessions
            if _session_engine(session) == normalized_engine
        )
        catalog = _lookup_native_catalog(
            catalogs,
            selected_client,
            normalized_engine,
        )
        if catalog is None:
            catalog = _build_native_catalog(
                adapter_list,
                selected_client,
                normalized_engine,
            )
        if catalog is None and normalized_engine == "codex":
            catalog = replace(inventory.catalog, records=tuple(
                r for r in inventory.records if _record_engine(r) == "codex"
            ))
        profile_capabilities = _registered_profile_capabilities(adapter_list, selected_client, normalized_engine)
        capability = _declared_capability(
            declared,
            selected_client,
            normalized_engine,
            inventory.capabilities,
            catalog=catalog,
            adapters=adapter_list,
            profile_capabilities=profile_capabilities,
        )
        # Project-row support is proven per inventory target (schema, row
        # fingerprint, and zero references), not by the frontend adapter's
        # broad engine capability. Preserve that proof when an adapter's
        # registered capability is otherwise selected above.
        if any(
            target.engine == normalized_engine
            and target.capability.frontend_project_delete
            for target in inventory.targets
        ):
            capability = replace(
                capability,
                frontend_project_delete=True,
                blockers=tuple(
                    blocker
                    for blocker in capability.blockers
                    if str(blocker.get("blocker_code") or blocker.get("code"))
                    != "frontend_project_delete_unsupported"
                ),
            )
        targets = _bind_native_targets(
            selected_client,
            normalized_engine,
            tuple(
                target
                for target in inventory.targets
                if target.engine == normalized_engine
            ),
            frontend_sessions,
            catalog,
            capability,
            reference_evidence=inventory.references,
        )
        explicit_capability = any(key in declared for key in (f"{selected_client}:{normalized_engine}", normalized_engine))
        targets = tuple(restrict_client_target(target, inventory,
            profile_capabilities=() if explicit_capability else profile_capabilities) for target in targets)
        if targets:
            capability = aggregate_capabilities(tuple(target.capability for target in targets))
        else:
            from .client_capability_guards import ClientCapabilityLimits
            capability = ClientCapabilityLimits(inventory.descriptors).restrict_summary(capability)
        contexts.append(
            ClientEngineContext(
                inventory=inventory,
                engine=normalized_engine,
                targets=targets,
                frontend_sessions=frontend_sessions,
                native_catalog=catalog,
                capability=capability,
            )
        )
    native_errors = tuple(
        error if isinstance(error, SourceFailure) else InventoryFailure(
            source=f"{context.engine}-catalog:{getattr(error, 'source', 'inventory')}",
            codex_home=Path(getattr(error, "session_root", None)
                            or getattr(error, "config_dir", None)
                            or getattr(error, "agent_dir", None)
                            or getattr(error, "codex_home", None)
                            or getattr(error, "profile_root", ".")),
            message=str(getattr(error, "message", "Native inventory failed")),
            database=getattr(error, "database", None),
            error_type=str(getattr(error, "error_type", type(error).__name__)),
            blocks_delete=bool(getattr(error, "blocks_delete", True)),
        )
        for context in contexts
        for error in getattr(context.native_catalog, "errors", ())
    )
    targets = _deduplicate_targets(target for context in contexts for target in context.targets)
    projects = {p.stable_id: p for p in inventory.projects}
    projects.update({t.project_key.stable_id: t.project_key for t in targets if t.project_key})
    native_bindings = {key for target in targets if target.record_key is not None
                       for key in target.frontend_binding_keys}
    inventory = replace(
        inventory, targets=targets, projects=tuple(projects.values()),
        engines=tuple(context.engine for context in contexts),
        unmapped_frontend_sessions=tuple(
            s for s in inventory.unmapped_frontend_sessions
            if _frontend_binding_key(s) not in native_bindings
        ),
        capabilities={c.engine: c.capability for c in contexts},
        errors=tuple(dict.fromkeys((*inventory.errors, *native_errors))),
    )
    inventory = project_client_evidence(inventory, {c.engine: c.native_catalog for c in contexts if c.native_catalog is not None})
    by_key = {_target_identity(t): t for t in inventory.targets}
    return tuple(replace(context, inventory=inventory,
        targets=tuple(by_key[_target_identity(t)] for t in context.targets)) for context in contexts)


build_client_contexts = build_client_engine_contexts


def _target_from_reference(reference: ClientReference) -> ClientTarget:
    engine = reference.engine or ("unsupported:" + reference.raw_backend if reference.raw_backend else "unknown")
    capability = EngineCapability(reference.client, engine, reason="Reference metadata does not prove a native writer or storage root")
    return ClientTarget(
        reference.client, engine, None, None, reference.native_id,
        (f"{reference.client}:{reference.frontend_id}",), RecordClassification.UNVERIFIED, capability,
        frontend_binding_keys=(reference.binding_key,), references=(reference,),
        blocker_codes=("reference_inventory_incomplete",) if reference.evidence_complete is False else (),
    )


def _frontend_read_failure(
    descriptor: ClientDescriptor, home: Path | None, database: Path | None,
    source: str, exc: Exception,
) -> InventoryFailure | SourceFailure:
    if home is not None:
        return InventoryFailure(source=source, codex_home=home, database=database,
                                error_type=type(exc).__name__, message=str(exc))
    return SourceFailure(source=source, database=database, profile_root=descriptor.profile_root,
                         error_type=type(exc).__name__, message=str(exc))


def _target_sources(target: ClientTarget, inventory: ClientInventory) -> tuple[Path, ...]:
    sources = [source for key in target.frontend_binding_keys for source in inventory._reference_sources.get(key, ())]
    sources.extend(Path(str(e["database"])) for e in target.project_row_evidence if e.get("database"))
    return tuple(dict.fromkeys(sources))


def restrict_client_target(
    target: ClientTarget, inventory: ClientInventory,
    *, profile_capabilities: Sequence[tuple[ClientDescriptor, EngineCapability]] = (),
) -> ClientTarget:
    """Apply only the limits of profiles/sources that own this exact target."""
    capability = target.capability
    sources = _target_sources(target, inventory)
    for descriptor in inventory.descriptors:
        if descriptor.matches(store=target.record_key.store if target.record_key else None) or any(
            descriptor.matches(source=source) for source in sources
        ):
            limit = descriptor.limit_for(target.engine)
            # A protection owner may be a different frontend client;
            # only intersect flags for this exact qualified native store.
            if limit.client != capability.client:
                limit = replace(limit, client=capability.client)
            previous = capability
            capability = restrict_capability(capability, limit)
            if any(getattr(previous, name) and not getattr(capability, name)
                   for name in CAPABILITY_FIELDS if name.endswith("delete")):
                capability = replace(capability, blockers=(*capability.blockers, {
                    "blocker_code": "client_capability_limit", "scope": "record",
                    "message": f"{descriptor.client}/{target.engine} limits this store's mutation capability",
                }))
    for descriptor, current in profile_capabilities:
        if descriptor.matches(store=target.record_key.store if target.record_key else None) or any(
            descriptor.matches(source=source) for source in sources
        ):
            # A schema-proven orphan project has its own exact writer proof.
            if target.capability.frontend_project_delete and descriptor.limit_for(target.engine).frontend_project_delete:
                current = replace(current, frontend_project_delete=True)
            capability = restrict_capability(capability, current)
    return _retarget_target(target, capability) if capability != target.capability else target


def _registered_profile_capabilities(
    adapters: Sequence[object], client: str, engine: str,
) -> tuple[tuple[ClientDescriptor, EngineCapability], ...]:
    result = []
    for adapter in adapters:
        if _adapter_client(adapter) != client:
            continue
        method = getattr(adapter, "registered_capability", None)
        if not callable(method):
            continue
        descriptor = describe_adapter(adapter)
        try:
            capability = method(engine)
            if not isinstance(capability, EngineCapability) or (capability.client, capability.engine) != (client, engine):
                raise ClientContractError("registered capability has a different client or engine")
        except Exception as exc:
            capability = EngineCapability(client, engine, verify=False, blockers=({
                "blocker_code": "adapter_capability_failed", "scope": "client_profile",
                "message": f"Capability evidence failed ({type(exc).__name__})",
            },))
        result.append((descriptor, capability))
    return tuple(result)


def relations_for_record(record: object, key: RecordKey) -> tuple[RelationEvidence, ...]:
    """Keep engine relationships distinct; none expands an approved scope."""
    if isinstance(record, ManagedConversation):
        return tuple(RelationEvidence(
            RelationKind.CODEX_PARENT, key,
            related_record=RecordKey(key.store, parent, kind=key.kind),
            source="native_lineage", completeness=record.lineage_status or "unknown",
            deletion_semantics="engine_defined",
        ) for parent in record.summary.parent_thread_ids)
    if key.store.backend == "pi":
        parent = getattr(record, "parent_session", None)
        return (RelationEvidence(RelationKind.PI_BRANCH_SOURCE, key, related_path=parent,
                                 source="session_header.parentSession", completeness="known",
                                 deletion_semantics="independent"),) if parent else ()
    if key.store.backend == "claude":
        return tuple(RelationEvidence(RelationKind.CLAUDE_MANIFEST_MEMBER, key,
            related_path=str(entry.path), source="session_manifest", completeness="known",
            deletion_semantics="manifest_member") for entry in getattr(record, "manifest", ())
            if "subagents" in Path(entry.relative_path).parts)
    return ()


def project_client_evidence(inventory: ClientInventory, catalogs: Mapping[str, object]) -> ClientInventory:
    """Attach display evidence without changing legacy approval payloads."""
    path_keys: dict[Path, str] = {}

    def path_key(path: Path | None) -> str | None:
        if path is None:
            return None
        if path not in path_keys:
            path_keys[path] = canonical_path(path)
        return path_keys[path]

    def key_for_record(key: RecordKey) -> tuple:
        return key.store.backend, path_key(key.store.path), key.record_id, path_key(key.path)

    by_binding: dict[str, list[ClientReference]] = {}
    by_native: dict[tuple, list[ClientReference]] = {}
    for reference in inventory.references:
        by_binding.setdefault(reference.binding_key, []).append(reference)
        if reference.native_record is not None:
            by_native.setdefault(key_for_record(reference.native_record), []).append(reference)
    records: dict[tuple, object] = {}
    for engine, catalog in catalogs.items():
        for record in _native_catalog_records(catalog):
            root = _native_record_root(record, engine)
            if root is not None:
                records[(engine, path_key(root), _native_record_id(record, engine),
                         path_key(_native_record_path(record, engine)))] = record
    targets: list[ClientTarget] = []
    for target in inventory.targets:
        related = [r for binding in target.frontend_binding_keys for r in by_binding.get(binding, ())]
        native_key = key_for_record(target.record_key) if target.record_key else None
        if native_key is not None:
            related.extend(by_native.get(native_key, ()))
        related = list({r.binding_key: r for r in related}.values())
        references = tuple(
            replace(reference, native_record=target.record_key)
            if target.record_key and reference.host == "local" and reference.path_namespace == "local" else reference
            for reference in related
        )
        relation_record = records.get(native_key) if native_key is not None else None
        targets.append(replace(target, references=references or target.references,
            relations=relations_for_record(relation_record, target.record_key)
                      if relation_record is not None and target.record_key is not None else target.relations))
    return replace(inventory, targets=tuple(targets))


def build_native_client_inventory(
    *, client: str, engine: str, catalog: object, adapters: Sequence[object] = (),
    guard_adapters: Sequence[object] = (),
) -> tuple[ClientInventory, tuple[ClientEngineContext, ...]]:
    """Shared projection for standalone Codex/Pi/Claude metadata catalogs."""
    import os
    from .record_identity import capability_for
    capability = capability_for("native", engine)
    targets: list[ClientTarget] = []
    projects: dict[str, ProjectKey] = {}
    native_references: list[ClientReference] = []
    for record in tuple(
        getattr(catalog, "records", getattr(catalog, "sessions", ())) or ()
    ):
        record_id = (
            getattr(record, "thread_id", None)
            if engine == "codex"
            else getattr(record, "session_id", None)
        )
        if not isinstance(record_id, str) or not record_id.strip():
            continue
        if engine == "codex":
            root = Path(getattr(record, "codex_home"))
            path = None
            kind = "codex_home"
            native_present = bool(getattr(record, "artifact_present", False))
            project_value = getattr(
                getattr(record, "summary", None), "cwd", None
            )
        elif engine == "pi":
            root = Path(getattr(record, "session_root"))
            path = Path(getattr(record, "path"))
            kind = "session_root"
            native_present = path.is_file()
            project_value = getattr(record, "cwd", None)
        else:
            root = Path(getattr(record, "config_dir"))
            paths = tuple(getattr(record, "transcript_paths", ()) or ())
            path = Path(paths[0]) if paths else None
            kind = "config_dir"
            native_present = bool(paths) or bool(getattr(record, "manifest", ()))
            project_paths = tuple(getattr(record, "project_paths", ()) or ())
            project_value = project_paths[0] if project_paths else None
        project = None
        if isinstance(project_value, (str, os.PathLike)) and str(project_value).strip():
            project = ProjectKey.from_path(
                client,
                project_value,
                display_name=Path(project_value).name,
            )
            projects.setdefault(project.stable_id, project)
        raw_references = (
            getattr(record, "frontend_sessions", ())
            if engine == "codex"
            else getattr(record, "frontend_references", getattr(record, "cindy_references", ()))
        )
        refs = tuple(
            _frontend_reference_id(reference)
            for reference in tuple(raw_references or ())
        )
        refs = tuple(value for value in refs if value)
        classification = classify_managed_conversation(
            record, project_present=project is not None, frontend_required=False,
        ) if isinstance(record, ManagedConversation) else classify_record_state(
            native_present=native_present,
            frontend_present=bool(refs),
            project_present=project is not None,
            frontend_required=False,
            corrupt_unreadable=getattr(record, "reference_classification", None) == "inventory_incomplete",
        )
        deletable = bool(getattr(record, "deletable", True))
        native_action_id = getattr(record, "action_id", None)
        action_ids = (
            (str(native_action_id), "delete_native")
            if engine == "codex" and native_action_id and deletable
            else (str(native_action_id), "delete_pi_session")
            if engine == "pi" and native_action_id and deletable
            else (str(native_action_id), "delete_claude_session")
            if engine == "claude" and native_action_id and deletable
            else ()
        )
        target = ClientTarget(
            client=client,
            engine=engine,
            record_key=RecordKey(
                StoreKey(engine, root, kind=kind),
                record_id,
                kind="session",
                path=path,
            ),
            project_key=project,
            native_thread_id=record_id,
            frontend_reference_ids=tuple(
                (
                    str(getattr(reference, "platform", ""))
                    + ":"
                    + str(getattr(reference, "platform_session_id", ""))
                )
                if engine == "codex"
                else f"cindy:{value}"
                for reference, value in (
                    zip(tuple(raw_references or ()), refs)
                    if engine == "codex"
                    else ((None, value) for value in refs)
                )
                if value
            ),
            classification=classification,
            capability=capability,
            action_ids=tuple(
                value for value in action_ids
                if value and value != "None"
            ),
            blocker_codes=tuple(getattr(record, "blocker_codes", ()) or ()),
            blockers=tuple({"blocker_code": "record_blocked", "message": str(value)}
                           for value in (getattr(record, "blockers", ()) or ())),
            display_name=getattr(getattr(record, "summary", record), "display_name", None),
            display_name_source=getattr(getattr(record, "summary", record), "display_name_source", None),
            is_subagent=bool(getattr(getattr(record, "summary", record), "is_subagent", False)),
            parent_thread_ids=tuple(getattr(getattr(record, "summary", record), "parent_thread_ids", ()) or ()),
            descendant_thread_ids=tuple(getattr(record, "descendant_thread_ids", ()) or ()),
            lineage_status=getattr(record, "lineage_status", None),
        )
        for reference in tuple(raw_references or ()):
            if isinstance(reference, FrontendSessionRecord):
                native_references.append(reference_from_session(reference, _frontend_binding_key(reference)))
                continue
            def value(name: str, default: Any = None) -> Any:
                return reference.get(name, default) if isinstance(reference, Mapping) else getattr(reference, name, default)
            database = value("database")
            frontend_id = _frontend_reference_id(reference)
            if database is not None and frontend_id:
                from types import SimpleNamespace

                row = SimpleNamespace(platform="cindy", database=Path(database), platform_session_id=frontend_id,
                    thread_id=value("native_session_id", record_id), backend=engine, codex_home=root,
                    status=value("session_status"), is_live=bool(value("is_live", False)),
                    details={"agent_kind": value("agent_kind", engine), "reference_kind": value("reference_kind", "current"),
                             "boundary_id": value("boundary_id")})
                native_references.append(replace(reference_from_session(row, _frontend_binding_key(row)), native_record=target.record_key))
        targets.append(target)
    catalog_records = tuple(
        getattr(catalog, "records", getattr(catalog, "sessions", ())) or ()
    )
    catalog_frontend_sessions = (
        tuple(
            session
            for record in catalog_records
            for session in (
                getattr(record, "frontend_sessions", ())
                if engine == "codex"
                else getattr(
                    record,
                    "frontend_references",
                    getattr(record, "cindy_references", ()),
                )
                or ()
            )
            if isinstance(session, FrontendSessionRecord)
        )
        if engine == "codex"
        else ()
    )
    if engine == "codex":
        frontend_reference_ids = tuple(
            dict.fromkeys(
                f"{session.platform}:{session.platform_session_id}"
                for session in catalog_frontend_sessions
            )
        )
    else:
        frontend_reference_ids = tuple(
            dict.fromkeys(f"cindy:{value}" for value in refs)
        )
    # The native catalog is itself the authoritative metadata snapshot for
    # the standalone/native compatibility clients. Keep those records and
    # exact frontend rows in the shared contract instead of exposing only the
    # projected targets. Pi/Claude catalogs carry Cindy reference objects
    # rather than FrontendSessionRecord instances, so their legacy projection
    # remains target-only until a frontend adapter is explicitly selected.
    inventory = ClientInventory(
        client=client,
        engines=(engine,),
        projects=tuple(projects[key] for key in sorted(projects)),
        records=(catalog_records if engine == "codex" else ()),
        frontend_sessions=catalog_frontend_sessions,
        unmapped_frontend_sessions=(),
        targets=tuple(targets),
        capabilities={engine: capability},
        errors=(tuple(getattr(catalog, "errors", ()) or ())
                if engine == "codex" else ()),
        frontend_snapshots=(),
        descriptors=tuple(describe_adapter(a) for a in {id(a): a for a in (*adapters, *guard_adapters)}.values())
            if engine == "codex" else tuple(ClientDescriptor("native", profile_root=root,
                native_stores=(StoreKey(engine, root, kind="session_root" if engine == "pi" else "config_dir"),),
                inventory_engines=(engine,), capability_limits=(capability,))
                for root in dict.fromkeys(_native_record_root(r, engine) for r in catalog_records)
                if root is not None),
        references=tuple({r.binding_key: r for r in native_references}.values()),
    )
    inventory = replace(inventory, targets=tuple(restrict_client_target(t, inventory) for t in inventory.targets))
    if inventory.targets:
        capability = aggregate_capabilities(tuple(t.capability for t in inventory.targets))
    else:
        from .client_capability_guards import ClientCapabilityLimits
        capability = ClientCapabilityLimits(inventory.descriptors).restrict_summary(capability)
    inventory = replace(inventory, capabilities={engine: capability})
    context = ClientEngineContext(
        inventory=inventory,
        engine=engine,
        targets=inventory.targets,
        frontend_sessions=catalog_frontend_sessions,
        native_catalog=catalog,
        capability=capability,
    )
    inventory = project_client_evidence(inventory, {engine: catalog})
    context = replace(context, inventory=inventory, targets=inventory.targets)
    if client == "native" and engine == "codex":
        from .native_project_cleanup import append_native_inventory

        original_count = len(inventory.targets)
        inventory = append_native_inventory(inventory, tuple({id(a): a for a in (*adapters, *guard_adapters)}.values()))
        additional = inventory.targets[original_count:]
        contexts = [replace(context, inventory=inventory)]
        for supported in (False, True):
            group = tuple(t for t in additional if t.capability.frontend_project_delete == supported)
            if group:
                contexts.append(ClientEngineContext(
                    inventory=inventory, engine=engine, targets=group,
                    frontend_sessions=(), native_catalog=None, capability=group[0].capability,
                ))
        return inventory, tuple(contexts)
    return inventory, (context,)


def _lookup_native_catalog(
    catalogs: Mapping[str, object],
    client: str,
    engine: str,
) -> object | None:
    for key in ((client, engine), f"{client}:{engine}", engine):
        try:
            value = catalogs.get(key)  # type: ignore[arg-type]
        except (AttributeError, TypeError):
            value = None
        if value is not None:
            return value
    return None


def _build_native_catalog(
    adapters: Sequence[object],
    client: str,
    engine: str,
) -> object | None:
    """Collect every selected profile, retaining native catalog failures."""
    selected = tuple(a for a in adapters if _adapter_client(a) == client)
    values: list[Any] = []
    handled: set[int] = set()
    for adapter in selected:
        if id(adapter) in handled:
            continue
        if (callable(getattr(adapter, "snapshot_references", None))
            and not callable(getattr(adapter, "snapshot_sessions", None))) and not any(
            store.backend == engine for store in describe_adapter(adapter).native_stores
        ):
            continue
        group_builder = getattr(adapter, "native_catalog_group", None)
        if callable(group_builder):
            peers = tuple(a for a in selected if type(a) is type(adapter))
            try:
                value = group_builder(engine, peers)
            except Exception as exc:
                raise ClientInventoryError(f"{client}/{engine} native catalog group failed ({type(exc).__name__})") from exc
            if value is not None:
                values.append(value)
                handled.update(id(a) for a in peers)
                continue
        if _adapter_engine(adapter) not in {engine, "codex"}:
            # Multi-backend frontend adapters often advertise their default
            # backend as codex; their explicit engine builder is still valid.
            if not callable(getattr(adapter, "native_catalog_for", None)):
                continue
        builder = getattr(adapter, "native_catalog_for", None)
        if callable(builder):
            try:
                value = builder(engine)
            except Exception as exc:
                raise ClientInventoryError(f"{client}/{engine} native catalog failed ({type(exc).__name__})") from exc
            if value is not None:
                values.append(value)
            continue
        for name in ("engine_catalog", "native_catalog"):
            builder = getattr(adapter, name, None)
            if not callable(builder):
                continue
            try:
                value = builder()
            except TypeError:
                try:
                    value = builder(engine)
                except Exception as exc:
                    raise ClientInventoryError(f"{client}/{engine} native catalog failed ({type(exc).__name__})") from exc
            except Exception as exc:
                raise ClientInventoryError(f"{client}/{engine} native catalog failed ({type(exc).__name__})") from exc
            if value is not None:
                values.append(value)
                break
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    catalogs = tuple(child for value in values for child in getattr(value, "catalogs", (value,)))
    if engine == "pi":
        from .pi_sessions import PiMultiRootCatalog
        return PiMultiRootCatalog(catalogs=catalogs)
    if engine == "claude":
        from .claude_sessions import ClaudeMultiRootCatalog
        return ClaudeMultiRootCatalog(catalogs=catalogs, root_errors=tuple(
            error for value in values for error in getattr(value, "root_errors", ())
        ))
    raise ClientInventoryError(f"No multi-store catalog contract for engine {engine!r}")


def _native_catalog_records(catalog: object | None) -> tuple[Any, ...]:
    if catalog is None:
        return ()
    for name in ("records", "sessions"):
        value = getattr(catalog, name, ())
        if isinstance(value, (tuple, list)):
            return tuple(value)
    return ()


def _native_record_root(record: object, engine: str) -> Path | None:
    names = (
        ("session_root", "agent_dir", "pi_root")
        if engine == "pi"
        else ("config_dir",)
        if engine == "claude"
        else ("codex_home", "home")
    )
    for name in names:
        value = getattr(record, name, None)
        if isinstance(value, Path):
            return value
        if isinstance(value, (str, bytes)) and str(value).strip():
            return Path(value)
    return None


def _catalog_root(catalog: object | None, engine: str) -> Path | None:
    if catalog is None:
        return None
    names = (
        ("session_root", "agent_dir", "pi_root")
        if engine == "pi"
        else ("config_dir",)
        if engine == "claude"
        else ("codex_home",)
    )
    for name in names:
        value = getattr(catalog, name, None)
        if isinstance(value, Path):
            return value
        if isinstance(value, (str, bytes)) and str(value).strip():
            return Path(value)
    return None


def _catalog_has_verified_native_root(
    catalog: object | None,
    engine: str,
) -> bool:
    nested = getattr(catalog, "catalogs", None)
    if nested is not None:
        return bool(nested) and all(_catalog_has_verified_native_root(item, engine) for item in nested)
    records = _native_catalog_records(catalog)
    root = _catalog_root(catalog, engine)
    roots = {
        _path_key(value)
        for value in (
            _native_record_root(record, engine) for record in records
        )
        if value is not None
    }
    if root is not None:
        root_key = _path_key(root)
        if roots and roots != {root_key}:
            return False
        return True
    return bool(roots) and all(_native_record_root(record, engine) is not None for record in records)


def _constrain_capability(
    capability: EngineCapability,
    *,
    client: str,
    engine: str,
    catalog: object | None,
) -> EngineCapability:
    """Never turn an unqualified frontend row into a native writer action."""

    if client in {"aionui", "cindy"} and engine in {"codex", "pi", "claude"}:
        verified = _catalog_has_verified_native_root(catalog, engine)
        if not verified and capability.native_delete:
            return replace(capability, native_delete=False, blockers=(
                *capability.blockers,
                {"blocker_code": NATIVE_ROOT_UNVERIFIED, "scope": "native_root",
                 "message": "No storage-qualified native catalog is available"},
            ))
        if client == "aionui" and engine in {"pi", "claude"} and not verified:
            return _capability_for(client, engine)
    return capability

def build_client_inventory(
    adapters: Iterable[object],
    *,
    client: str,
    engines: Sequence[str] = (),
) -> ClientInventory:
    """Freeze one selected client's frontend and native metadata inventory.

    Adapters that implement FrontendAdapter.snapshot_sessions are enumerated
    once. A cached proxy feeds build_session_catalog, so native catalog
    construction cannot enumerate the frontend database again.
    """

    selected_client = normalize_client(client)
    if not selected_client:
        raise ClientInventoryError("client selector must not be blank")
    requested_engines = _normalize_engines(engines)
    selected_adapters = [
        adapter for adapter in adapters
        if _adapter_client(adapter) == selected_client
    ]
    if not selected_adapters:
        raise ClientInventoryError(
            f"No adapters are registered for client {selected_client!r}"
        )

    proxies: list[_SnapshotAdapter] = []
    frontend_rows: list[FrontendSessionRecord] = []
    descriptors: list[ClientDescriptor] = []
    references: list[ClientReference] = []
    snapshots: list[FrontendBatchSnapshot] = []
    errors: list[InventoryFailure | SourceFailure] = []
    project_items: list[Any] = []
    scanned_databases: set[Path] = set()
    scanned_resources: set[tuple[str, str]] = set()
    for adapter in selected_adapters:
        try:
            descriptor = describe_adapter(adapter)
        except ClientContractError as exc:
            raise ClientInventoryError(str(exc)) from exc
        descriptors.append(descriptor)
        home = _path_or_none(getattr(adapter, "codex_home", None))
        database = _path_or_none(getattr(adapter, "database", None))
        reference_reader = getattr(adapter, "snapshot_references", None)
        if callable(reference_reader) and not callable(getattr(adapter, "snapshot_sessions", None)):
            try:
                snapshot = reference_reader()
                if not isinstance(snapshot, ReferenceSnapshot) or snapshot.descriptor != descriptor:
                    raise ClientInventoryError("reference snapshot differs from its selected client descriptor")
                references.extend(snapshot.references)
                errors.extend(snapshot.errors)
            except Exception as exc:
                errors.append(SourceFailure(source="client-references", message=str(exc),
                    profile_root=descriptor.profile_root, error_type=type(exc).__name__))
            continue
        try:
            snapshot_method = getattr(adapter, "snapshot_sessions", None)
            if callable(snapshot_method):
                snapshot = snapshot_method(
                    all_backends=bool(
                        getattr(adapter, "supports_all_backends", False)
                    )
                )
                if database is not None and canonical_path(snapshot.database) != canonical_path(database):
                    raise ClientInventoryError("frontend snapshot database differs from selected database")
                rows = tuple(snapshot.records)
                snapshots.append(snapshot)
            else:
                rows = tuple(adapter.list_sessions())
            if database is not None and database.is_file():
                scanned_databases.add(database)
                scanned_resources.add((canonical_path(database), "sessions"))
        except Exception as exc:
            errors.append(_frontend_read_failure(descriptor, home, database,
                f"frontend:{getattr(adapter, 'name', type(adapter).__name__)}", exc))
            rows = ()
        project_reader = getattr(adapter, "list_project_items", None)
        if callable(project_reader):
            try:
                project_items.extend(tuple(project_reader()))
                if database is not None and database.is_file():
                    scanned_databases.add(database)
                    scanned_resources.add((canonical_path(database), "projects"))
            except Exception as exc:
                errors.append(_frontend_read_failure(descriptor, home, database,
                    f"frontend-project:{getattr(adapter, 'name', type(adapter).__name__)}", exc))
        if requested_engines:
            rows = tuple(row for row in rows if _session_engine(row) in requested_engines)
        frontend_rows.extend(rows)
        references.extend(reference_from_session(row, _frontend_binding_key(row)) for row in rows)
        if home is not None:
            proxies.append(
                _SnapshotAdapter(
                    source=adapter,
                    rows=tuple(row for row in rows if _session_engine(row) == "codex"),
                    frontend_rows=rows,
                    codex_home=home,
                    database=database or home / "state_5.sqlite",
                )
            )

    catalog = build_session_catalog(proxies)
    errors.extend(catalog.errors)
    home_keys = {_path_key(proxy.codex_home) for proxy in proxies}
    all_frontend = tuple(
        sorted(
            frontend_rows,
            key=_frontend_sort_key,
        )
    )
    all_frontend = tuple(
        session for session in all_frontend
        if not requested_engines or _session_engine(session) in requested_engines
    )
    records = tuple(
        record for record in catalog.records
        if (
            not requested_engines or _record_engine(record) in requested_engines
        )
        and _record_belongs_to_client(record, selected_client, home_keys)
    )
    bound_keys = {_frontend_binding_key(bound) for record in records for bound in record.frontend_sessions}
    unmapped = _deduplicate_frontend_sessions(
        session for session in (*catalog.unmapped_frontend_sessions,
                                *(s for s in all_frontend if _session_engine(s) != "codex"
                                  or _frontend_binding_key(s) not in bound_keys))
        if not requested_engines or _session_engine(session) in requested_engines
    )

    project_map: dict[str, ProjectKey] = {}
    for record in records:
        project = _record_project(selected_client, record)
        if project is not None:
            project_map.setdefault(project.stable_id, project)
    for session in all_frontend:
        project = _session_project(selected_client, session)
        if project is not None:
            project_map.setdefault(project.stable_id, project)
    unique_project_items: list[Any] = []
    seen_project_items: set[tuple[str, str]] = set()
    for item in project_items:
        project_id = _first_object_string(item, "project_id", "id")
        database = _path_or_none(getattr(item, "database", None))
        if not project_id or database is None:
            continue
        item_key = (_path_key(database), project_id)
        if item_key in seen_project_items:
            continue
        seen_project_items.add(item_key)
        unique_project_items.append(item)
        project = _project_item_project(selected_client, item, project_id)
        if project is not None:
            project_map.setdefault(project.stable_id, project)

    targets = [_target_from_record(selected_client, record) for record in records]
    targets.extend(
        _target_from_frontend(selected_client, session) for session in unmapped
    )
    legacy_bindings = {_frontend_binding_key(row) for row in all_frontend}
    targets.extend(_target_from_reference(reference)
                   for reference in references if reference.binding_key not in legacy_bindings
                   and (not requested_engines or reference.engine in requested_engines))
    referenced_project_ids = {
        str(session.platform_session_id)
        for session in all_frontend
        if isinstance(session.platform_session_id, str)
    }
    targets.extend(
        _target_from_project_item(selected_client, item)
        for item in unique_project_items
        if _project_item_is_orphan(item, referenced_project_ids)
    )
    target_engines = {target.engine for target in targets}
    capabilities = {
        engine: _capability_for(selected_client, engine)
        for engine in sorted(target_engines | set(requested_engines))
    }
    for engine in tuple(capabilities):
        if any(
            target.engine == engine
            and target.capability.frontend_project_delete
            for target in targets
        ):
            capabilities[engine] = replace(
                capabilities[engine],
                frontend_project_delete=True,
                reason=(
                    capabilities[engine].reason
                    or "Exact frontend project-row writer is registered"
                ),
            )
    inventory = ClientInventory(
        client=selected_client,
        engines=tuple(sorted(target_engines | set(requested_engines))),
        projects=tuple(project_map[key] for key in sorted(project_map)),
        records=records,
        frontend_sessions=all_frontend,
        unmapped_frontend_sessions=unmapped,
        targets=tuple(targets),
        capabilities=capabilities,
        errors=tuple(errors),
        frontend_snapshots=tuple(snapshots),
        project_items=tuple(unique_project_items),
        scanned_databases=tuple(sorted(scanned_databases, key=str)),
        scanned_resources=tuple(sorted(scanned_resources)),
        descriptors=tuple(descriptors),
        references=tuple(references),
    )
    inventory = replace(inventory, targets=tuple(restrict_client_target(target, inventory) for target in inventory.targets))
    inventory = replace(inventory, capabilities={engine: aggregate_capabilities(tuple(
        target.capability for target in inventory.targets if target.engine == engine))
        if any(target.engine == engine for target in inventory.targets) else capability
        for engine, capability in inventory.capabilities.items()})
    inventory = project_client_evidence(inventory, {"codex": catalog})
    if selected_client == "native" and (not requested_engines or "codex" in requested_engines):
        from .native_project_cleanup import append_native_inventory
        return append_native_inventory(inventory, selected_adapters)
    return inventory


def select_client_targets(
    inventory: ClientInventory,
    *,
    project_selectors: Sequence[str] = (),
    all_projects: bool = False,
    record_ids: Sequence[str] = (),
    engines: Sequence[str] = (),
) -> ClientSelection:
    """Resolve project, all-project, or record-id scope.

    Project scope only selects targets with explicit project evidence. A target
    with no project evidence can therefore be selected only by record ID.
    """

    selectors = tuple(str(item).strip() for item in project_selectors)
    ids = tuple(str(item).strip() for item in record_ids)
    if any(not item for item in (*selectors, *ids)):
        raise ClientInventoryError("project and record selectors must not be blank")
    if all_projects and (selectors or ids):
        raise ClientInventoryError(
            "--all-projects cannot be combined with --project or --record-id"
        )
    if selectors and ids:
        raise ClientInventoryError("--project cannot be combined with --record-id")
    requested_engines = _normalize_engines(engines)
    candidates = tuple(
        target for target in inventory.targets
        if not requested_engines or target.engine in requested_engines
    )

    if all_projects:
        chosen_projects = inventory.projects
        targets = tuple(
            target for target in candidates if target.project_key is not None
        )
        if not targets:
            raise ClientInventoryError(
                "No records with project evidence are available for --all-projects"
            )
    elif selectors:
        resolved: list[ProjectKey] = []
        for selector in selectors:
            try:
                project = resolve_project_selector(
                    inventory.projects, selector, client=inventory.client
                )
            except ProjectSelectionError as exc:
                raise ClientInventoryError(str(exc)) from exc
            if project.stable_id not in {item.stable_id for item in resolved}:
                resolved.append(project)
        chosen_projects = tuple(resolved)
        wanted = {project.stable_id for project in chosen_projects}
        targets = tuple(
            target for target in candidates
            if target.project_key is not None
            and target.project_key.stable_id in wanted
        )
        if not targets:
            raise ClientInventoryError(
                "Selected projects have no records in the chosen engine scope"
            )
    elif ids:
        targets = _select_record_ids(candidates, ids)
        chosen_projects = tuple(
            project for project in inventory.projects
            if any(
                target.project_key is not None
                and target.project_key.stable_id == project.stable_id
                for target in targets
            )
        )
    else:
        raise ClientInventoryError(
            "Provide project_selectors, all_projects, or record_ids"
        )

    return ClientSelection(
        inventory=inventory,
        project_keys=chosen_projects,
        targets=targets,
        project_selectors=selectors,
        all_projects=all_projects,
        record_ids=ids,
    )


def _select_record_ids(
    candidates: Sequence[ClientTarget],
    selectors: Sequence[str],
) -> tuple[ClientTarget, ...]:
    selected: list[ClientTarget] = []
    seen: set[str] = set()
    for selector in selectors:
        exact = [target for target in candidates if selector in target.identifiers]
        matches = exact or [
            target for target in candidates
            if any(value.startswith(selector) for value in target.identifiers)
        ]
        if not matches:
            raise ClientInventoryError(
                f"No record matches selector {selector!r}; "
                "unmapped records require an exact record/reference ID"
            )
        if len(matches) > 1:
            raise ClientInventoryError(
                f"Record selector {selector!r} is ambiguous across "
                f"{len(matches)} storage-qualified targets"
            )
        target = matches[0]
        identity = _target_identity(target)
        if identity not in seen:
            seen.add(identity)
            selected.append(target)
    return tuple(selected)


@dataclass(frozen=True)
class _SnapshotAdapter:
    source: object
    rows: tuple[FrontendSessionRecord, ...]
    codex_home: Path
    database: Path
    frontend_rows: tuple[FrontendSessionRecord, ...] = ()

    @property
    def name(self) -> str:
        return str(getattr(self.source, "name", type(self.source).__name__))

    @property
    def codex_bin_hint(self) -> Path | None:
        hint = getattr(self.source, "codex_bin_hint", None)
        return hint if isinstance(hint, Path) else None

    def list_sessions(self) -> list[FrontendSessionRecord]:
        return list(self.rows)

    def describe_client(self) -> ClientDescriptor:
        return describe_adapter(self.source)


def _bind_native_targets(
    client: str,
    engine: str,
    inventory_targets: Sequence[ClientTarget],
    frontend_sessions: Sequence[FrontendSessionRecord],
    catalog: object | None,
    capability: EngineCapability,
    *, reference_evidence: Sequence[ClientReference] = (),
) -> tuple[ClientTarget, ...]:
    """Replace frontend-only placeholders with storage-qualified native rows."""

    native_targets: list[ClientTarget] = []
    for record in _native_catalog_records(catalog):
        target = _target_from_native_record(
            client,
            engine,
            record,
            frontend_sessions,
            capability,
        )
        if target is not None:
            native_targets.append(target)

    by_record: dict[tuple, list[ClientReference]] = {}
    for reference in reference_evidence:
        if reference.native_record is not None and reference.host == "local" and reference.path_namespace == "local":
            key = (reference.native_record.value,
                   reference.native_record.canonical_path if reference.native_record.store.backend == "pi" else None)
            by_record.setdefault(key, []).append(reference)
    for index, target in enumerate(native_targets):
        if target.record_key is None:
            continue
        key = (target.record_key.value, target.record_key.canonical_path if engine == "pi" else None)
        references = by_record.get(key, ())
        if references:
            incomplete = any(r.evidence_complete is False for r in references)
            bound_capability = restrict_capability(target.capability, EngineCapability(target.capability.client, engine,
                reason="Reference inventory is incomplete", blockers=({
                    "blocker_code": "reference_inventory_incomplete", "scope": "reference",
                    "message": "Native binding has incomplete reference evidence",
                },))) if incomplete else target.capability
            native_targets[index] = replace(target,
                classification=RecordClassification.HEALTHY if target.classification == RecordClassification.ORPHAN_NATIVE else target.classification,
                references=tuple(references),
                frontend_reference_ids=tuple(dict.fromkeys((*target.frontend_reference_ids,
                    *(f"{r.client}:{r.frontend_id}" for r in references)))),
                frontend_binding_keys=tuple(dict.fromkeys((*target.frontend_binding_keys, *(r.binding_key for r in references)))),
                capability=bound_capability, action_ids=() if incomplete else target.action_ids,
                blocker_codes=tuple(dict.fromkeys((*target.blocker_codes, *bound_capability.blocker_codes))),
                blockers=(*target.blockers, *(b for b in bound_capability.blockers if b not in target.blockers)),
            )
    if not native_targets:
        return _deduplicate_targets(
            _retarget_target(target, capability) for target in inventory_targets
        )

    bound_refs = {
        reference_id
        for target in native_targets
        for reference_id in target.frontend_binding_keys
    }
    residual: list[ClientTarget] = []
    for target in inventory_targets:
        if target.frontend_binding_keys and set(target.frontend_binding_keys) <= bound_refs:
            continue
        residual.append(_retarget_target(target, capability))
    return _deduplicate_targets((*native_targets, *residual))


def _target_from_native_record(
    client: str,
    engine: str,
    record: object,
    frontend_sessions: Sequence[FrontendSessionRecord],
    capability: EngineCapability,
) -> ClientTarget | None:
    if engine == "codex" and isinstance(record, ManagedConversation):
        references = _deduplicate_frontend_sessions((
            *record.frontend_sessions,
            *(session for session in frontend_sessions
              if _session_engine(session) == engine
              and session.thread_id == record.thread_id
              and canonical_path(session.codex_home) == canonical_path(record.codex_home)),
        ))
        record = replace(record, frontend_sessions=references)
        return _retarget_target(_target_from_record(client, record), capability)
    record_id = _native_record_id(record, engine)
    if record_id is None:
        return None
    references = _native_record_frontend_sessions(
        record,
        engine,
        record_id,
        frontend_sessions,
    )
    reference_ids = tuple(
        dict.fromkeys(
            f"{session.platform}:{session.platform_session_id}"
            for session in references
        )
    )
    root = _native_record_root(record, engine)
    path = _native_record_path(record, engine)
    native_present = _native_record_present(record, engine)
    record_key = None
    if native_present and root is not None:
        kind = (
            "session_root"
            if engine == "pi"
            else "config_dir"
            if engine == "claude"
            else "directory"
        )
        record_key = RecordKey(
            StoreKey(engine, root, kind=kind),
            record_id,
            kind="session" if engine != "codex" else "record",
            path=path,
        )
    project = _native_record_project(client, record, references)
    classification = classify_record_state(
        native_present=native_present,
        frontend_present=bool(references),
        project_present=project is not None,
        frontend_required=client not in {"native", "pi", "claude"},
        corrupt_unreadable=getattr(record, "reference_classification", None) == "inventory_incomplete",
    )
    actions: list[str] = []
    native_action_id = getattr(record, "action_id", None)
    if record_key is not None and capability.native_delete:
        if isinstance(native_action_id, str) and native_action_id.strip():
            actions.append(native_action_id.strip())
        actions.append(
            "delete_pi_session"
            if engine == "pi"
            else "delete_claude_session"
            if engine == "claude"
            else "delete_native"
        )
    if reference_ids and capability.frontend_reference_delete:
        actions.append("delete_frontend_reference")
    if (
        capability.frontend_session_delete
        and any(not session.is_live for session in references)
    ):
        actions.append("delete_frontend_session")
    if project is not None and capability.frontend_project_delete:
        actions.append("delete_project_item")
    return ClientTarget(
        client=client,
        engine=engine,
        record_key=record_key,
        project_key=project,
        native_thread_id=record_id,
        frontend_reference_ids=reference_ids,
        classification=classification,
        capability=capability,
        action_ids=tuple(dict.fromkeys(actions)),
        blocker_codes=tuple(dict.fromkeys((
            *capability.blocker_codes,
            *(("native_record_blocked",) if not getattr(record, "deletable", True) else ()),
        ))),
        blockers=(*capability.blockers, *(
            {"blocker_code": "native_record_blocked", "message": str(message)}
            for message in getattr(record, "blockers", ())
        )),
        frontend_binding_keys=tuple(_frontend_binding_key(s) for s in references),
    )


def _native_record_id(record: object, engine: str) -> str | None:
    names = ("session_id", "thread_id", "id") if engine != "codex" else (
        "thread_id",
        "session_id",
        "id",
    )
    for name in names:
        value = getattr(record, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _native_record_path(record: object, engine: str) -> Path | None:
    names = (
        ("path",)
        if engine == "pi"
        else ("transcript_paths", "path")
        if engine == "claude"
        else ("path",)
    )
    for name in names:
        value = getattr(record, name, None)
        if isinstance(value, Path):
            return value
        if isinstance(value, (tuple, list)) and value:
            first = value[0]
            if isinstance(first, Path):
                return first
        if isinstance(value, (str, bytes)) and str(value).strip():
            return Path(value)
    return None


def _native_record_present(record: object, engine: str) -> bool:
    if engine == "pi":
        return _native_record_path(record, engine) is not None
    if engine == "claude":
        paths = getattr(record, "transcript_paths", ())
        return bool(paths) or bool(getattr(record, "manifest", ()))
    value = getattr(record, "artifact_present", None)
    return bool(value) if value is not None else _native_record_path(record, engine) is not None


def _native_record_frontend_sessions(
    record: object,
    engine: str,
    record_id: str,
    frontend_sessions: Sequence[FrontendSessionRecord],
) -> tuple[FrontendSessionRecord, ...]:
    raw_references = getattr(record, "cindy_references", ())
    if not raw_references:
        raw_references = getattr(record, "frontend_references", ())
    if not raw_references:
        raw_references = getattr(record, "references", ())
    def matches_reference(session: FrontendSessionRecord, reference: Any) -> bool:
        def field(name: str) -> Any:
            return reference.get(name) if isinstance(reference, Mapping) else getattr(reference, name, None)
        database = field("database")
        return bool(database) and canonical_path(database) == canonical_path(session.database) and (
            _frontend_reference_id(reference) == session.platform_session_id
            and (field("native_session_id") or field("session_id") or record_id) == session.thread_id
            and (field("boundary_id") or None) == (session.details.get("boundary_id") or None)
            and (field("reference_kind") or "current") == (session.details.get("reference_kind") or "current")
        )

    def qualified(session: FrontendSessionRecord) -> bool:
        if raw_references:
            return any(matches_reference(session, reference) for reference in raw_references)
        # Do not guess a Pi/Claude root from an adapter's Codex home.
        root = _native_record_root(record, engine)
        declared = session.details.get("native_storage_root")
        if root is not None and declared:
            return canonical_path(root) == canonical_path(declared)
        return False

    return _deduplicate_frontend_sessions(
        session
        for session in frontend_sessions
        if _session_engine(session) == engine and qualified(session)
        and (bool(raw_references) or session.thread_id == record_id)
    )


def _frontend_binding_key(session: FrontendSessionRecord) -> str:
    return frontend_binding_key(session)


def _frontend_reference_id(reference: object) -> str | None:
    for name in (
        "cindy_session_id",
        "frontend_session_id",
        "platform_session_id",
        "session_id",
        "id",
    ):
        value = getattr(reference, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(reference, Mapping):
            value = reference.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _deduplicate_frontend_sessions(
    sessions: Iterable[FrontendSessionRecord],
) -> tuple[FrontendSessionRecord, ...]:
    result: list[FrontendSessionRecord] = []
    seen: set[str] = set()
    for session in sessions:
        key = _frontend_binding_key(session)
        if key in seen:
            continue
        seen.add(key)
        result.append(session)
    return tuple(result)


def _native_record_project(
    client: str,
    record: object,
    references: Sequence[FrontendSessionRecord],
) -> ProjectKey | None:
    cwd = getattr(record, "cwd", None)
    if isinstance(cwd, str) and cwd.strip():
        return ProjectKey.from_path(
            client,
            cwd,
            display_name=_display_string(getattr(record, "session_name", None)),
        )
    paths = getattr(record, "project_paths", ())
    if isinstance(paths, (tuple, list)):
        for value in sorted(
            (item for item in paths if isinstance(item, (Path, str))),
            key=lambda item: str(item).casefold(),
        ):
            if str(value).strip():
                return ProjectKey.from_path(client, value)
    for session in references:
        project = _session_project(client, session)
        if project is not None:
            return project
    return None


def _display_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _retarget_target(
    target: ClientTarget,
    capability: EngineCapability,
) -> ClientTarget:
    native_action_prefixes = (
        "record:v1:",
        "pi-session:v1:",
        "claude-session:v1:",
    )
    project_action_ids = [
        action_id
        for action_id in target.action_ids
        if action_id.startswith("delete_project_item:")
    ]
    action_ids = [
        action_id
        for action_id in target.action_ids
        if action_id
        not in {
            "delete_native",
            "delete_pi_session",
            "delete_claude_session",
            "delete_frontend_reference",
            "delete_frontend_session",
            "delete_project_item",
        }
        and (
            capability.native_delete
            or not action_id.startswith(native_action_prefixes)
        )
        and (capability.frontend_session_delete or not action_id.startswith("delete_frontend_session:"))
        and (capability.frontend_reference_delete or not action_id.startswith("remove_frontend_reference"))
        and (capability.frontend_project_delete or not action_id.startswith("delete_project_item:"))
    ]
    native_present = target.record_key is not None
    if native_present and capability.native_delete:
        action_ids.append(
            "delete_pi_session"
            if target.engine == "pi"
            else "delete_claude_session"
            if target.engine == "claude"
            else "delete_native"
        )
    if target.frontend_reference_ids and capability.frontend_reference_delete:
        action_ids.append("delete_frontend_reference")
    if capability.frontend_session_delete and "delete_frontend_session" in target.action_ids:
        action_ids.append("delete_frontend_session")
    if target.project_key is not None and capability.frontend_project_delete:
        action_ids.extend(project_action_ids or ["delete_project_item"])
    if not any((capability.native_delete, capability.frontend_session_delete,
                capability.frontend_reference_delete, capability.frontend_project_delete, capability.remote_delete)):
        action_ids = []
    return replace(
        target,
        capability=capability,
        action_ids=tuple(dict.fromkeys(action_ids)),
        blocker_codes=tuple(dict.fromkeys((
            *(code for code in target.blocker_codes if code not in target.capability.blocker_codes),
            *capability.blocker_codes,
        ))),
        blockers=(
            *(blocker for blocker in target.blockers if blocker not in target.capability.blockers),
            *capability.blockers,
        ),
    )


def _target_identity(target: ClientTarget) -> str:
    if target.record_key is not None:
        return target.record_key.value
    if target.frontend_reference_ids:
        return (
            f"{target.client}:{target.engine}:frontend:"
            + "|".join(sorted(target.frontend_binding_keys or target.frontend_reference_ids))
        )
    return f"{target.client}:{target.engine}:record:{target.native_thread_id or ''}"


def _deduplicate_targets(
    targets: Iterable[ClientTarget],
) -> tuple[ClientTarget, ...]:
    result: list[ClientTarget] = []
    indexes: dict[str, int] = {}
    for target in targets:
        key = _target_identity(target)
        index = indexes.get(key)
        if index is None:
            indexes[key] = len(result)
            result.append(target)
            continue
        current = result[index]
        result[index] = replace(
            current,
            frontend_reference_ids=tuple(
                dict.fromkeys(
                    (*current.frontend_reference_ids, *target.frontend_reference_ids)
                )
            ),
            frontend_binding_keys=tuple(dict.fromkeys((*current.frontend_binding_keys, *target.frontend_binding_keys))),
            action_ids=tuple(dict.fromkeys((*current.action_ids, *target.action_ids))),
            blocker_codes=tuple(
                dict.fromkeys((*current.blocker_codes, *target.blocker_codes))
            ),
            blockers=tuple((*current.blockers, *target.blockers)),
        )
    return tuple(result)


def _adapter_engine(adapter: object) -> str:
    raw = getattr(adapter, "engine", None)
    if isinstance(raw, str):
        return normalize_engine(raw)
    raw = getattr(adapter, "backend", None)
    return normalize_engine(raw or "codex")


def _declared_capability(
    declarations: Mapping[str, EngineCapability],
    client: str,
    engine: str,
    fallback: Mapping[str, EngineCapability],
    *,
    catalog: object | None = None,
    adapters: Sequence[object] = (),
    profile_capabilities: Sequence[tuple[ClientDescriptor, EngineCapability]] = (),
) -> EngineCapability:
    selected: EngineCapability | None = None
    for key in (f"{client}:{engine}", engine):
        value = declarations.get(key)
        if isinstance(value, EngineCapability):
            selected = value
            break
    if selected is None:
        registered = profile_capabilities or _registered_profile_capabilities(adapters, client, engine)
        if registered:
            selected = aggregate_capabilities(tuple(value for _, value in registered))
    if selected is None:
        selected = fallback.get(engine) or _capability_for(client, engine)
    return _constrain_capability(
        selected,
        client=client,
        engine=engine,
        catalog=catalog,
    )


def _adapter_client(adapter: object) -> str:
    if callable(getattr(adapter, "describe_client", None)):
        return describe_adapter(adapter).client
    raw = getattr(adapter, "client", None)
    if isinstance(raw, str):
        return normalize_client(raw)
    return normalize_client(getattr(adapter, "name", type(adapter).__name__))


def _session_engine(session: FrontendSessionRecord) -> str:
    return normalize_engine(session.backend or "codex")


def _record_engine(record: ManagedConversation) -> str:
    engines = {
        _session_engine(session)
        for session in record.frontend_sessions
        if session.backend
    }
    return sorted(engines)[0] if len(engines) == 1 else "codex"


def _record_belongs_to_client(
    record: ManagedConversation,
    client: str,
    home_keys: set[str],
) -> bool:
    if client == "native":
        return True
    if any(
        normalize_client(session.platform) == client
        for session in record.frontend_sessions
    ):
        return True
    return _path_key(record.codex_home) in home_keys and client == "cindy"


def _record_project(client: str, record: ManagedConversation) -> ProjectKey | None:
    if record.summary.cwd:
        return ProjectKey.from_path(
            client,
            record.summary.cwd,
            display_name=(
                record.summary.project_label
                or Path(record.summary.cwd).name
            ),
        )
    for session in record.frontend_sessions:
        project = _session_project(client, session)
        if project is not None:
            return project
    return None


def _session_project(
    client: str,
    session: FrontendSessionRecord,
) -> ProjectKey | None:
    details = dict(session.details)
    project_id = _first_string(
        details, "project_id", "projectId", "project_key", "projectKey"
    )
    if project_id:
        return ProjectKey.from_id(
            client,
            project_id,
            display_name=_first_string(details, "project_name", "projectName"),
        )
    path = _first_string(
        details,
        "working_dir",
        "workingDirectory",
        "cwd",
        "project_path",
        "projectPath",
    )
    if path:
        return ProjectKey.from_path(
            client,
            path,
            display_name=(
                _first_string(details, "project_name", "projectName")
                or Path(path).name
            ),
        )
    return None


def _first_object_string(item: object, *names: str) -> str | None:
    for name in names:
        value = getattr(item, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _project_item_project(
    client: str,
    item: object,
    project_id: str | None = None,
) -> ProjectKey | None:
    path = _first_object_string(
        item,
        "working_dir",
        "working_directory",
        "project_path",
    )
    title = _first_object_string(item, "title", "project_name")
    if path:
        return ProjectKey.from_path(
            client,
            path,
            display_name=title or Path(path).name,
        )
    value = project_id or _first_object_string(item, "project_id", "id")
    if value:
        return ProjectKey.from_id(client, value, display_name=title)
    return None


def _project_item_is_orphan(
    item: object,
    referenced_project_ids: set[str],
) -> bool:
    try:
        reference_count = int(getattr(item, "session_reference_count", -1))
    except (TypeError, ValueError):
        return False
    project_id = _first_object_string(item, "project_id", "id")
    return (
        reference_count == 0
        and project_id is not None
        and project_id not in referenced_project_ids
    )


def _project_item_capability(client: str) -> EngineCapability:
    return _project_item_capability_for(client, supported=False)


def _project_item_capability_for(
    client: str,
    *,
    supported: bool,
) -> EngineCapability:
    capability = _capability_for(client, "codex")
    if supported:
        return replace(
            capability,
            frontend_project_delete=True,
            reason=(
                (capability.reason + "; ") if capability.reason else ""
            ) + "Exact AionUI conversations project-row writer is registered",
            blockers=tuple(
                blocker
                for blocker in capability.blockers
                if str(blocker.get("blocker_code") or blocker.get("code"))
                != "frontend_project_delete_unsupported"
            ),
        )
    blocker = {
        "blocker_code": "frontend_project_delete_unsupported",
        "scope": "frontend_project_item",
        "message": "No verified frontend project-item deletion writer is registered",
    }
    return replace(
        capability,
        reason=(
            (capability.reason + "; ") if capability.reason else ""
        ) + "Project item is inventory-only",
        blockers=tuple((*capability.blockers, blocker)),
    )


def _target_from_project_item(
    client: str,
    item: object,
) -> ClientTarget:
    project_id = _first_object_string(item, "project_id", "id")
    if project_id is None:
        raise ClientInventoryError("A project item has no stable project ID")
    database = _path_or_none(getattr(item, "database", None))
    if database is None:
        raise ClientInventoryError("A project item has no physical database")
    project = _project_item_project(client, item, project_id)
    if project is None:
        raise ClientInventoryError("A project item has no project identity")
    evidence = _project_item_evidence(item)
    supported = bool(
        evidence
        and getattr(item, "project_delete_supported", False)
        and int(getattr(item, "session_reference_count", -1)) == 0
    )
    capability = _project_item_capability_for(client, supported=supported)
    action_ids: tuple[str, ...] = ()
    if supported:
        digest = hashlib.sha256(
            (
                f"{_path_key(database)}\0{project_id}\0"
                f"{evidence[0]['schema_fingerprint']}\0"
                f"{evidence[0]['row_fingerprint']}"
            ).encode("utf-8")
        ).hexdigest()[:32]
        action_ids = (f"delete_project_item:{digest}",)
    return ClientTarget(
        client=client,
        engine="codex",
        record_key=RecordKey(
            StoreKey(client, database, kind="sqlite"),
            project_id,
            kind="project",
        ),
        project_key=project,
        native_thread_id=None,
        frontend_reference_ids=(),
        classification=RecordClassification.ORPHAN_PROJECT,
        capability=capability,
        action_ids=action_ids,
        blocker_codes=capability.blocker_codes,
        blockers=capability.blockers,
        project_row_evidence=evidence,
    )


def _project_item_evidence(item: object) -> tuple[Mapping[str, Any], ...]:
    database = _path_or_none(getattr(item, "database", None))
    project_id = _first_object_string(item, "project_id", "id")
    conversation_id = _first_object_string(item, "conversation_id") or project_id
    schema_hash = _first_object_string(item, "schema_fingerprint")
    row_hash = _first_object_string(item, "row_fingerprint")
    if (
        database is None
        or project_id is None
        or conversation_id is None
        or not schema_hash
        or not row_hash
    ):
        return ()
    return ({
        "database": str(database.absolute()),
        "table": "conversations",
        "id": conversation_id,
        "conversation_id": conversation_id,
        "schema_fingerprint": schema_hash,
        "row_fingerprint": row_hash,
        "expected_zero_acp_session_refs": True,
        "expected_acp_session_refs": 0,
    },)


def _target_from_record(client: str, record: ManagedConversation) -> ClientTarget:
    engine = _record_engine(record)
    unverified_aion_native = (
        client == "aionui"
        and engine in {"pi", "claude"}
    )
    native_present = bool(record.artifact_present) and not unverified_aion_native
    record_key = (
        RecordKey(StoreKey(engine, record.codex_home), record.thread_id)
        if native_present
        else None
    )
    project = _record_project(client, record)
    refs = tuple(
        f"{session.platform}:{session.platform_session_id}"
        for session in record.frontend_sessions
    )
    classification = classify_managed_conversation(
        record, project_present=project is not None,
        frontend_required=client not in {"native", "codex-desktop"},
    ) if engine == "codex" else classify_record_state(
        native_present=native_present,
        frontend_present=bool(record.frontend_sessions),
        project_present=project is not None,
        index_stale=(
            record.legacy_indexed
            and not record.indexed
            and not record.artifact_present
        ),
    )
    capability = _capability_for(client, engine)
    actions: list[str] = []
    if native_present and capability.native_delete:
        actions.append("delete_native")
    if refs and capability.frontend_reference_delete:
        actions.append("delete_frontend_reference")
    if (
        capability.frontend_session_delete
        and any(not session.is_live for session in record.frontend_sessions)
    ):
        actions.append("delete_frontend_session")
    if project is not None and capability.frontend_project_delete:
        actions.append("delete_project_item")
    return ClientTarget(
        client=client,
        engine=engine,
        record_key=record_key,
        project_key=project,
        native_thread_id=record.thread_id,
        frontend_reference_ids=refs,
        classification=classification,
        capability=capability,
        action_ids=(
            *((record.action_id,) if native_present else ()),
            *actions,
        ),
        blocker_codes=tuple(dict.fromkeys((*record.blocker_codes, *capability.blocker_codes))),
        blockers=(
            *({"blocker_code": "record_blocked", "message": message} for message in record.blockers),
            *capability.blockers,
        ),
        display_name=record.summary.display_name,
        display_name_source=record.summary.display_name_source,
        is_subagent=record.summary.is_subagent,
        parent_thread_ids=record.summary.parent_thread_ids,
        descendant_thread_ids=record.descendant_thread_ids,
        lineage_status=record.lineage_status,
        frontend_binding_keys=tuple(_frontend_binding_key(s) for s in record.frontend_sessions),
    )


def _target_from_frontend(
    client: str,
    session: FrontendSessionRecord,
) -> ClientTarget:
    engine = _session_engine(session)
    # A frontend row is evidence of a reference, not proof of the native
    # store. Native identity is added only by a storage catalog binding.
    record_key = None
    project = _session_project(client, session)
    capability = _capability_for(client, engine)
    return ClientTarget(
        client=client,
        engine=engine,
        record_key=record_key,
        project_key=project,
        native_thread_id=session.thread_id,
        frontend_reference_ids=(f"{session.platform}:{session.platform_session_id}",),
        frontend_binding_keys=(_frontend_binding_key(session),),
        classification=(RecordClassification.UNVERIFIED
                        if capability.mode in {"inventory_only", "unsupported"}
                        or NATIVE_ROOT_UNVERIFIED in capability.blocker_codes
                        else RecordClassification.ORPHAN_FRONTEND),
        capability=capability,
        action_ids=() if capability.mode in {"inventory_only", "unsupported"} else (
            f"{session.platform}:{session.platform_session_id}",
            *(
                ("delete_frontend_reference",)
                if capability.frontend_reference_delete
                else ()
            ),
            *(
                ("delete_frontend_session",)
                if capability.frontend_session_delete and not session.is_live
                else ()
            ),
        ),
        blocker_codes=capability.blocker_codes,
        blockers=capability.blockers,
    )


def _capability_for(client: str, engine: str) -> EngineCapability:
    from .record_identity import capability_for

    return capability_for(client, engine, observed=True)


def _normalize_engines(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            normalize_engine(value)
            for value in values
            if str(value).strip()
        )
    )


def _path_or_none(value: object) -> Path | None:
    return value if isinstance(value, Path) else None


def _path_key(value: Path) -> str:
    try:
        return str(value.expanduser().resolve(strict=False)).casefold()
    except OSError:
        return str(value.expanduser().absolute()).casefold()


def _first_string(details: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _frontend_sort_key(session: FrontendSessionRecord) -> tuple[str, ...]:
    return (
        str(session.platform),
        str(session.database).casefold(),
        str(session.platform_session_id),
        str(session.thread_id or ""),
    )


__all__ = [
    "ClientInventory",
    "ClientEngineContext",
    "ClientInventoryError",
    "ClientSelection",
    "ClientTarget",
    "build_client_contexts",
    "build_client_engine_contexts",
    "build_client_inventory",
    "select_client_targets",
]
