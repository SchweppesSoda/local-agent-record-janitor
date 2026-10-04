"""Metadata contracts shared by client adapters and inventory consumers.

These types describe evidence; they never authorize a mutation. Legacy
approval payloads remain owned by their existing engine and frontend writers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .record_identity import EngineCapability, RecordKey, StoreKey, canonical_path, normalize_client, normalize_engine


class ClientContractError(ValueError):
    """An adapter descriptor is incompatible with the local inventory surface."""


CAPABILITY_FIELDS = (
    "inventory", "native_delete", "frontend_session_delete",
    "frontend_reference_delete", "frontend_project_delete", "remote_delete", "verify",
)


def require_local_location(host: str = "local", path_namespace: str = "local") -> None:
    if host != "local" or path_namespace != "local":
        raise ClientContractError("This inventory surface requires a local host and path namespace")


def restrict_capability(value: EngineCapability, limit: EngineCapability) -> EngineCapability:
    """Intersect support declarations without losing an existing proof blocker."""
    if (value.client, value.engine) != (limit.client, limit.engine):
        raise ClientContractError("Capability limit belongs to another client or engine")
    flags = {field: bool(getattr(value, field) and getattr(limit, field)) for field in CAPABILITY_FIELDS}
    blockers = list(value.blockers)
    blockers.extend(blocker for blocker in limit.blockers if blocker not in blockers)
    changed = any(flags[field] != getattr(value, field) for field in CAPABILITY_FIELDS)
    return replace(value, **flags, blockers=tuple(blockers),
                   reason=limit.reason if changed and limit.reason else value.reason)


def aggregate_capabilities(values: Sequence[EngineCapability]) -> EngineCapability:
    """Summarize available paths; each target retains its own tighter limits."""
    if not values:
        raise ClientContractError("Cannot aggregate an empty capability set")
    first = values[0]
    if any((v.client, v.engine) != (first.client, first.engine) for v in values):
        raise ClientContractError("Capability summary mixes clients or engines")
    if all(value == first for value in values):
        return first
    return replace(first, **{field: any(getattr(v, field) for v in values) for field in CAPABILITY_FIELDS},
                   blockers=tuple(b for b in first.blockers if all(b in v.blockers for v in values)))


@dataclass(frozen=True)
class ClientDescriptor:
    client: str
    profile_root: Path | None = None
    sources: tuple[Path, ...] = ()
    native_stores: tuple[StoreKey, ...] = ()
    owner_process_root: Path | None = None
    inventory_engines: tuple[str, ...] = ()
    capability_limits: tuple[EngineCapability, ...] = ()
    host: str = "local"
    path_namespace: str = "local"
    _source_keys: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Check execution location before interpreting any opaque path as a
        # local Path/StoreKey. Remote adapters require a separate contract.
        require_local_location(self.host, self.path_namespace)
        object.__setattr__(self, "client", normalize_client(self.client))
        object.__setattr__(self, "sources", tuple(Path(p).expanduser() for p in self.sources))
        object.__setattr__(self, "_source_keys", frozenset(canonical_path(p) for p in self.sources))
        if self.profile_root is not None:
            object.__setattr__(self, "profile_root", Path(self.profile_root).expanduser())
        if self.owner_process_root is not None:
            object.__setattr__(self, "owner_process_root", Path(self.owner_process_root).expanduser())
        object.__setattr__(self, "inventory_engines", tuple(dict.fromkeys(normalize_engine(e) for e in self.inventory_engines)))
        if any(value.client != self.client for value in self.capability_limits):
            raise ClientContractError("Descriptor capability belongs to another client")

    def limit_for(self, engine: str) -> EngineCapability:
        engine = normalize_engine(engine)
        return next((v for v in self.capability_limits if v.engine == engine),
                    EngineCapability(self.client, engine, reason="No verified writer is declared for this profile"))

    def matches(self, *, source: Path | None = None, store: StoreKey | None = None) -> bool:
        return bool(
            source is not None and canonical_path(source) in self._source_keys
            or store is not None and any(
                s.backend == store.backend and s.canonical_path == store.canonical_path for s in self.native_stores
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client, "host": self.host, "path_namespace": self.path_namespace,
            "profile_root": str(self.profile_root) if self.profile_root is not None else None,
            "sources": [str(p) for p in self.sources],
            "native_stores": [s.to_dict() for s in self.native_stores],
            "owner_process_root": str(self.owner_process_root) if self.owner_process_root is not None else None,
            "inventory_engines": list(self.inventory_engines),
            "capability_limits": [v.to_dict() for v in self.capability_limits],
        }


class ReferenceKind(str, Enum):
    CURRENT = "current"
    HISTORY = "history"
    RESTORE = "restore"
    DESKTOP_CATALOG = "desktop_host_catalog"
    UNKNOWN = "unknown"


class ReferenceLifecycle(str, Enum):
    LIVE = "live"
    HISTORICAL = "historical"
    DELETED = "deleted"
    RESTORABLE = "restorable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClientReference:
    client: str
    source: Path
    frontend_id: str
    native_id: str | None
    raw_backend: str | None
    engine: str | None
    binding_key: str
    native_record: RecordKey | None = None
    kind: ReferenceKind = ReferenceKind.UNKNOWN
    lifecycle: ReferenceLifecycle = ReferenceLifecycle.UNKNOWN
    source_locator: str | None = None
    evidence_complete: bool | None = None
    host: str = "local"
    path_namespace: str = "local"
    opaque_native_locator: str | None = None

    def __post_init__(self) -> None:
        if self.native_record is not None and (self.host != "local" or self.path_namespace != "local"):
            raise ClientContractError("A remote reference cannot carry a local native identity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client, "source": str(self.source), "frontend_id": self.frontend_id,
            "native_id": self.native_id, "raw_backend": self.raw_backend, "engine": self.engine,
            "binding_key": self.binding_key,
            "native_record": self.native_record.to_dict() if self.native_record else None,
            "kind": self.kind.value, "lifecycle": self.lifecycle.value,
            "source_locator": self.source_locator, "evidence_complete": self.evidence_complete,
            "host": self.host, "path_namespace": self.path_namespace,
            "opaque_native_locator": self.opaque_native_locator,
        }


@dataclass(frozen=True)
class SourceFailure:
    source: str
    message: str
    profile_root: Path | None = None
    database: Path | None = None
    store: StoreKey | None = None
    error_type: str = "InventoryError"
    blocks_delete: bool = True
    blocks_inventory: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "message": self.message, "error_type": self.error_type,
                "profile_root": str(self.profile_root) if self.profile_root else None,
                "database": str(self.database) if self.database else None,
                "store": self.store.to_dict() if self.store else None, "blocks_delete": self.blocks_delete,
                "blocks_inventory": self.blocks_inventory}


@dataclass(frozen=True)
class ReferenceSnapshot:
    descriptor: ClientDescriptor
    references: tuple[ClientReference, ...] = ()
    errors: tuple[SourceFailure, ...] = ()

    def __post_init__(self) -> None:
        if any(r.client != self.descriptor.client or not self.descriptor.matches(source=r.source)
               for r in self.references):
            raise ClientContractError("Reference source is outside its selected client profile")


class NativeCatalog(Protocol):
    @property
    def records(self) -> Sequence[object]: ...

    @property
    def errors(self) -> Sequence[object]: ...


class ClientAdapter(Protocol):
    def describe_client(self) -> ClientDescriptor: ...

    def snapshot_references(self, *, refresh: bool = False) -> ReferenceSnapshot: ...

    def native_catalog_for(self, engine: str) -> NativeCatalog | None: ...


class RelationKind(str, Enum):
    CODEX_PARENT = "codex_parent"
    PI_BRANCH_SOURCE = "pi_branch_source"
    CLAUDE_MANIFEST_MEMBER = "claude_manifest_member"


@dataclass(frozen=True)
class RelationEvidence:
    kind: RelationKind
    record: RecordKey
    related_record: RecordKey | None = None
    related_path: str | None = None
    source: str | None = None
    completeness: str = "unknown"
    deletion_semantics: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "record": self.record.to_dict(),
                "related_record": self.related_record.to_dict() if self.related_record else None,
                "related_path": self.related_path, "source": self.source,
                "completeness": self.completeness, "deletion_semantics": self.deletion_semantics}


def describe_adapter(adapter: object) -> ClientDescriptor:
    """Read the typed descriptor, with a narrow facade for legacy adapters."""
    method = getattr(adapter, "describe_client", None)
    if callable(method):
        value = method()
        if not isinstance(value, ClientDescriptor):
            raise ClientContractError("describe_client returned an unsupported descriptor")
        return value
    from .record_identity import capability_for

    require_local_location(getattr(adapter, "host", "local"), getattr(adapter, "path_namespace", "local"))
    client = normalize_client(getattr(adapter, "client", getattr(adapter, "name", type(adapter).__name__)))
    database = getattr(adapter, "database", None)
    home = getattr(adapter, "codex_home", None)
    engines = tuple(getattr(adapter, "inventory_engines", (getattr(adapter, "backend", "codex") or "codex",)))
    return ClientDescriptor(client, sources=(database,) if isinstance(database, Path) else (),
                            native_stores=(StoreKey("codex", home, kind="codex_home"),) if isinstance(home, Path) else (),
                            inventory_engines=engines,
                            capability_limits=tuple(capability_for(client, e) for e in dict.fromkeys((*engines, "codex", "pi", "claude"))))


def reference_from_session(session: Any, binding_key: str) -> ClientReference:
    """Project existing metadata only; the original approval payload is untouched."""
    details = session.details
    raw_kind = str(details.get("reference_kind", "current"))
    kind = (ReferenceKind.HISTORY if raw_kind == "agent_switch" else
            next((v for v in ReferenceKind if v.value == raw_kind), ReferenceKind.UNKNOWN))
    lifecycle = (
        ReferenceLifecycle.LIVE if session.is_live else
        ReferenceLifecycle.DELETED if session.status == "deleted" else
        ReferenceLifecycle.HISTORICAL if kind == ReferenceKind.HISTORY else
        ReferenceLifecycle.UNKNOWN
    )
    engine = normalize_engine(session.backend) if session.backend else None
    raw_backend = details.get("agent_kind", session.backend)
    native_record = None
    if engine == "codex" and session.thread_id:
        native_record = RecordKey(StoreKey(engine, session.codex_home, kind="codex_home"), session.thread_id, kind="session")
    boundary = details.get("boundary_id")
    return ClientReference(
        normalize_client(session.platform), session.database, session.platform_session_id,
        session.thread_id, raw_backend if isinstance(raw_backend, str) else None, engine, binding_key,
        native_record=native_record, kind=kind, lifecycle=lifecycle,
        source_locator=str(boundary or session.platform_session_id),
        evidence_complete=True,
    )


def frontend_binding_key(session: Any) -> str:
    """The existing v1 binding identity, shared without changing its encoding."""
    return json.dumps(
        (normalize_client(session.platform), canonical_path(session.database),
         session.platform_session_id, normalize_engine(session.backend or "codex"), session.thread_id,
         session.details.get("reference_kind", "current"), session.details.get("boundary_id")),
        separators=(",", ":"),
    )
