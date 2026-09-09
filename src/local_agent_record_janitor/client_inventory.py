"""Client-scoped inventory and project/record selection.

The public functions in this module freeze one selected client's metadata
snapshot and resolve project or record scope without merging storage identities.
No transcript/body is read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .adapters.base import FrontendBatchSnapshot
from .display_metadata import display_title
from .inventory import (
    FrontendSessionRecord,
    InventoryFailure,
    ManagedConversation,
    SessionCatalog,
    build_session_catalog,
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
        return tuple(dict.fromkeys(value for value in values if value))

    def to_dict(self) -> dict[str, Any]:
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
            "lineage_status": "conflict" if "lineage_conflict" in self.blocker_codes else (
                "known" if self.parent_thread_ids else "unknown" if self.is_subagent else "root"
            ),
            "cleanup_eligible": bool(self.action_ids) and not self.blockers and not self.blocker_codes,
            "frontend_reference_ids": list(self.frontend_reference_ids),
            "classification": self.classification.value,
            "capability": self.capability.to_dict(),
            "action_ids": list(self.action_ids),
            "blocker_codes": list(self.blocker_codes),
            "blockers": [dict(blocker) for blocker in self.blockers],
            "project_row_evidence": [
                dict(item) for item in self.project_row_evidence
            ],
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
    errors: tuple[InventoryFailure, ...] = ()
    frontend_snapshots: tuple[FrontendBatchSnapshot, ...] = ()
    project_items: tuple[Any, ...] = ()
    scanned_databases: tuple[Path, ...] = ()
    scanned_resources: tuple[tuple[str, str], ...] = ()

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
            target for target in self.targets if target.capability != self.capability
        )
        if mismatched:
            raise AssertionError(
                "ClientEngineContext targets must use the context capability"
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
    engine_names = requested or inventory.engines
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
        capability = _declared_capability(
            declared,
            selected_client,
            normalized_engine,
            inventory.capabilities,
            catalog=catalog,
            adapters=adapter_list,
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
        )
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
    return tuple(contexts)


build_client_contexts = build_client_engine_contexts


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
    """Ask an adapter for one engine catalog without a second frontend scan."""

    for adapter in adapters:
        if _adapter_client(adapter) != client:
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
            except Exception:
                value = None
            if value is not None:
                return value
        for name in ("engine_catalog", "native_catalog"):
            builder = getattr(adapter, name, None)
            if not callable(builder):
                continue
            try:
                value = builder()
            except TypeError:
                try:
                    value = builder(engine)
                except Exception:
                    value = None
            except Exception:
                value = None
            if value is not None:
                return value
    return None


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
    return len(roots) == 1


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
            return _capability_for(client, engine)
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
    snapshots: list[FrontendBatchSnapshot] = []
    errors: list[InventoryFailure] = []
    project_items: list[Any] = []
    scanned_databases: set[Path] = set()
    scanned_resources: set[tuple[str, str]] = set()
    for adapter in selected_adapters:
        home = _path_or_none(getattr(adapter, "codex_home", None))
        database = _path_or_none(getattr(adapter, "database", None))
        if home is None:
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
            errors.append(
                InventoryFailure(
                    source=f"frontend:{getattr(adapter, 'name', type(adapter).__name__)}",
                    codex_home=home,
                    database=database,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            )
            rows = ()
        project_reader = getattr(adapter, "list_project_items", None)
        if callable(project_reader):
            try:
                project_items.extend(tuple(project_reader()))
                if database is not None and database.is_file():
                    scanned_databases.add(database)
                    scanned_resources.add((canonical_path(database), "projects"))
            except Exception as exc:
                errors.append(
                    InventoryFailure(
                        source=(
                            f"frontend-project:{getattr(adapter, 'name', type(adapter).__name__)}"
                        ),
                        codex_home=home,
                        database=database,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
        if requested_engines:
            rows = tuple(row for row in rows if _session_engine(row) in requested_engines)
        proxies.append(
            _SnapshotAdapter(
                source=adapter,
                rows=rows,
                codex_home=home,
                database=database or home / "state_5.sqlite",
            )
        )

    catalog = build_session_catalog(proxies)
    errors.extend(catalog.errors)
    home_keys = {_path_key(proxy.codex_home) for proxy in proxies}
    all_frontend = tuple(
        sorted(
            (session for proxy in proxies for session in proxy.rows),
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
    unmapped = tuple(
        session for session in catalog.unmapped_frontend_sessions
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
    )
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
        identity = (
            target.record_key.value if target.record_key is not None
            else target.frontend_reference_ids[0]
            if target.frontend_reference_ids
            else target.native_thread_id
        )
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


def _bind_native_targets(
    client: str,
    engine: str,
    inventory_targets: Sequence[ClientTarget],
    frontend_sessions: Sequence[FrontendSessionRecord],
    catalog: object | None,
    capability: EngineCapability,
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

    if not native_targets:
        return _deduplicate_targets(
            _retarget_target(target, capability) for target in inventory_targets
        )

    bound_refs = {
        reference_id
        for target in native_targets
        for reference_id in target.frontend_reference_ids
    }
    bound_native_ids = {
        (target.native_thread_id, target.engine)
        for target in native_targets
        if target.native_thread_id
    }
    residual: list[ClientTarget] = []
    for target in inventory_targets:
        if bound_refs.intersection(target.frontend_reference_ids):
            continue
        if (
            target.record_key is None
            and target.native_thread_id
            and (target.native_thread_id, target.engine) in bound_native_ids
        ):
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
        blocker_codes=capability.blocker_codes,
        blockers=capability.blockers,
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
    wanted_ids = {
        value
        for reference in raw_references
        if (value := _frontend_reference_id(reference)) is not None
    }
    selected: list[FrontendSessionRecord] = []
    for session in frontend_sessions:
        if _session_engine(session) != engine:
            continue
        if (
            session.platform_session_id in wanted_ids
            or session.thread_id == record_id
        ):
            selected.append(session)
    if wanted_ids:
        return _deduplicate_frontend_sessions(selected)
    return _deduplicate_frontend_sessions(
        session
        for session in frontend_sessions
        if _session_engine(session) == engine and session.thread_id == record_id
    )


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
    seen: set[tuple[str, str, str]] = set()
    for session in sessions:
        key = (
            str(session.platform),
            str(session.database).casefold(),
            str(session.platform_session_id),
        )
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
            "delete_project_item",
        }
        and (
            capability.native_delete
            or not action_id.startswith(native_action_prefixes)
        )
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
    if target.project_key is not None and capability.frontend_project_delete:
        action_ids.extend(project_action_ids or ["delete_project_item"])
    return replace(
        target,
        capability=capability,
        action_ids=tuple(dict.fromkeys(action_ids)),
        blocker_codes=capability.blocker_codes,
        blockers=capability.blockers,
    )


def _target_identity(target: ClientTarget) -> str:
    if target.record_key is not None:
        return target.record_key.value
    if target.frontend_reference_ids:
        return (
            f"{target.client}:{target.engine}:frontend:"
            + "|".join(sorted(target.frontend_reference_ids))
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
) -> EngineCapability:
    selected: EngineCapability | None = None
    for key in (f"{client}:{engine}", engine):
        value = declarations.get(key)
        if isinstance(value, EngineCapability):
            selected = value
            break
    if selected is None:
        for adapter in adapters:
            if _adapter_client(adapter) != client:
                continue
            builder = getattr(adapter, "registered_capability", None)
            if not callable(builder):
                continue
            try:
                value = builder(engine)
            except Exception:
                continue
            if isinstance(value, EngineCapability):
                selected = value
                break
    if selected is None:
        selected = fallback.get(engine) or _capability_for(client, engine)
    return _constrain_capability(
        selected,
        client=client,
        engine=engine,
        catalog=catalog,
    )


def _adapter_client(adapter: object) -> str:
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
    classification = classify_record_state(
        native_present=native_present,
        frontend_present=bool(record.frontend_sessions),
        project_present=project is not None,
        relation_broken=False,
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
        blocker_codes=capability.blocker_codes,
        blockers=capability.blockers,
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
        classification=RecordClassification.ORPHAN_FRONTEND,
        capability=capability,
        action_ids=(
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
