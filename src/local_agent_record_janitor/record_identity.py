"""Storage, project and frontend identity helpers.

The cleanup code has several intentionally different native stores (Codex,
Pi and Claude) and several UI namespaces (Cindy, AionUI and Codex Desktop).
This module contains only immutable identity/value helpers used by inventory
and adapters.  It does not decide whether a mutation is safe and it never
opens a database or a transcript.

Keeping this layer small is deliberate: a project label is useful for
selection, but it must never replace a physical store-qualified record ID.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .path_identity import canonical_existing_path_key


class ClientName(str, Enum):
    """Known frontend/client namespaces.

    ``native`` is retained as the public CLI spelling for the official Codex
    namespace.  ``codex-native`` is accepted as the explicit long spelling.
    """

    CODEX_NATIVE = "native"
    CINDY = "cindy"
    AIONUI = "aionui"
    CODEX_DESKTOP = "codex-desktop"
    PI = "pi"
    CLAUDE = "claude"


# A frontend backend name alone does not prove which native store owns a
# record. Keep this safety conclusion structured so callers cannot turn it
# into an executable native action by changing presentation text.
NATIVE_ROOT_UNVERIFIED = "native_root_unverified"


def normalize_client(value: object) -> str:
    """Normalize a client selector without conflating different stores."""

    text = str(value or "").strip().casefold().replace("_", "-")
    aliases = {
        "codex": "native",
        "codex-cli": "native",
        "codex-native": "native",
        "chatgpt": "native",
        "chatgpt-desktop": "native",
        "desktop": "codex-desktop",
        "aion-ui": "aionui",
        "claude-code": "claude",
        "claude_code": "claude",
    }
    return aliases.get(text, text)


def normalize_engine(value: object) -> str:
    """Normalize engine/backend names while preserving unknown names."""

    text = str(value or "").strip().casefold().replace("_", "-")
    aliases = {
        "codex-cli": "codex",
        "codex-native": "codex",
        "claude-code": "claude",
        "cc": "claude",
        "aion-ui": "aionui",
    }
    return aliases.get(text, text)


def canonical_path(path: str | os.PathLike[str] | Path) -> str:
    """Return the repository's conservative canonical path key."""

    return canonical_existing_path_key(Path(path).expanduser())


@dataclass(frozen=True)
class StoreKey:
    """Identity of one physical native/frontend storage container.

    ``path`` is retained as a :class:`Path` for callers that need to open the
    store.  Equality and hashing use the conservative canonical key, so an ID
    in two homes can never be merged merely because the IDs match.
    """

    backend: str
    path: Path
    kind: str = "directory"

    def __post_init__(self) -> None:
        backend = normalize_engine(self.backend)
        kind = str(self.kind or "directory").strip().casefold()
        if not backend:
            raise ValueError("StoreKey.backend must not be blank")
        if not kind:
            raise ValueError("StoreKey.kind must not be blank")
        raw_path = Path(self.path).expanduser()
        if not str(raw_path):
            raise ValueError("StoreKey.path must not be blank")
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "path", raw_path.absolute())

    @property
    def canonical_path(self) -> str:
        return canonical_path(self.path)

    @property
    def value(self) -> str:
        return f"{self.backend}:{self.kind}:{self.canonical_path}"

    def to_dict(self) -> dict[str, str]:
        return {
            "backend": self.backend,
            "kind": self.kind,
            "path": str(self.path),
            "canonical_path": self.canonical_path,
            "value": self.value,
        }


@dataclass(frozen=True)
class RecordKey:
    """Identity of one logical native record inside one :class:`StoreKey`."""

    store: StoreKey
    record_id: str
    kind: str = "record"
    path: Path | None = None

    def __post_init__(self) -> None:
        record_id = str(self.record_id or "").strip()
        kind = str(self.kind or "record").strip().casefold()
        if not record_id:
            raise ValueError("RecordKey.record_id must not be blank")
        if not kind:
            raise ValueError("RecordKey.kind must not be blank")
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "kind", kind)
        if self.path is not None:
            object.__setattr__(self, "path", Path(self.path).expanduser().absolute())

    @property
    def value(self) -> str:
        return f"{self.store.value}:{self.kind}:{self.record_id}"

    @property
    def canonical_path(self) -> str | None:
        return canonical_path(self.path) if self.path is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "store": self.store.to_dict(),
            "record_id": self.record_id,
            "kind": self.kind,
            "path": str(self.path) if self.path is not None else None,
            "canonical_path": self.canonical_path,
            "value": self.value,
        }


@dataclass(frozen=True)
class ProjectKey:
    """Stable project identity used only to select records.

    A path project stores a canonical path in ``value``.  An ID project stores
    the client-owned ID verbatim (after trimming).  Display names are metadata
    and deliberately do not participate in identity or equality.
    """

    client: str
    kind: str
    value: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        client = normalize_client(self.client)
        kind = str(self.kind or "").strip().casefold()
        raw_value = str(self.value or "").strip()
        if not client:
            raise ValueError("ProjectKey.client must not be blank")
        if not kind:
            raise ValueError("ProjectKey.kind must not be blank")
        if not raw_value:
            raise ValueError("ProjectKey.value must not be blank")
        if kind in {"path", "directory", "worktree", "working_dir"}:
            kind = "path"
            raw_value = canonical_path(raw_value)
        elif kind in {"id", "project_id", "remote_id"}:
            kind = "id"
        object.__setattr__(self, "client", client)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", raw_value)
        if self.display_name is not None:
            display = str(self.display_name).strip()
            object.__setattr__(self, "display_name", display or None)

    @classmethod
    def from_path(
        cls,
        client: str,
        path: str | os.PathLike[str] | Path,
        *,
        display_name: str | None = None,
    ) -> "ProjectKey":
        return cls(client, "path", canonical_path(path), display_name)

    @classmethod
    def from_id(
        cls,
        client: str,
        project_id: object,
        *,
        display_name: str | None = None,
    ) -> "ProjectKey":
        return cls(client, "id", str(project_id), display_name)

    @property
    def stable_id(self) -> str:
        return f"{self.client}:{self.kind}:{self.value}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "kind": self.kind,
            "value": self.value,
            "display_name": self.display_name,
            "stable_id": self.stable_id,
        }


@dataclass(frozen=True)
class FrontendRefKey:
    """Identity of one frontend session/reference/project row."""

    client: str
    store: StoreKey
    kind: str
    reference_id: str

    def __post_init__(self) -> None:
        client = normalize_client(self.client)
        kind = str(self.kind or "reference").strip().casefold()
        reference_id = str(self.reference_id or "").strip()
        if not client or not kind or not reference_id:
            raise ValueError("FrontendRefKey fields must not be blank")
        object.__setattr__(self, "client", client)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "reference_id", reference_id)

    @property
    def stable_id(self) -> str:
        return f"{self.client}:{self.store.value}:{self.kind}:{self.reference_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "store": self.store.to_dict(),
            "kind": self.kind,
            "reference_id": self.reference_id,
            "stable_id": self.stable_id,
        }


class ProjectSelectionError(ValueError):
    """Raised when a project selector is missing or ambiguous."""

    def __init__(self, selector: str, *, matches: Sequence[ProjectKey] = ()) -> None:
        self.selector = selector
        self.matches = tuple(matches)
        if not matches:
            message = f"No project matches selector {selector!r}"
        else:
            message = (
                f"Project selector {selector!r} is ambiguous across "
                f"{len(matches)} projects"
            )
        super().__init__(message)


def resolve_project_selector(
    projects: Iterable[ProjectKey],
    selector: str,
    *,
    client: str | None = None,
) -> ProjectKey:
    """Resolve an exact stable ID/path/ID or unique display-name prefix.

    Display names are a convenience only.  If two paths have the same name,
    this function raises instead of silently choosing one.
    """

    raw = str(selector or "").strip()
    if not raw:
        raise ProjectSelectionError(raw)
    normalized_client = normalize_client(client) if client is not None else None
    candidates = tuple(
        project
        for project in projects
        if normalized_client is None or project.client == normalized_client
    )
    exact = tuple(
        project
        for project in candidates
        if raw in {project.stable_id, project.value}
    )
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ProjectSelectionError(raw, matches=exact)
    lowered = raw.casefold()
    matches = tuple(
        project
        for project in candidates
        if (
            project.stable_id.casefold().startswith(lowered)
            or project.value.casefold().startswith(lowered)
            or (
                project.display_name is not None
                and project.display_name.casefold().startswith(lowered)
            )
        )
    )
    if len(matches) != 1:
        raise ProjectSelectionError(raw, matches=matches)
    return matches[0]


class RecordClassification(str, Enum):
    """Common inventory conclusions for native and frontend remnants."""

    HEALTHY = "healthy"
    ORPHAN_NATIVE = "orphan_native"
    ORPHAN_FRONTEND = "orphan_frontend"
    ORPHAN_PROJECT = "orphan_project"
    BROKEN_RELATION = "broken_relation"
    STALE_INDEX = "stale_index"
    PARTIAL_REMOTE = "partial_remote"
    CORRUPT_UNREADABLE = "corrupt_unreadable"
    UNKNOWN_OPERATION = "unknown_operation"


def classify_record_state(
    *,
    native_present: bool,
    frontend_present: bool,
    project_present: bool = True,
    relation_broken: bool = False,
    index_stale: bool = False,
    remote_partial: bool = False,
    corrupt_unreadable: bool = False,
    operation_unknown: bool = False,
) -> RecordClassification:
    """Classify one content-free snapshot using deterministic precedence."""

    if corrupt_unreadable:
        return RecordClassification.CORRUPT_UNREADABLE
    if operation_unknown:
        return RecordClassification.UNKNOWN_OPERATION
    if remote_partial:
        return RecordClassification.PARTIAL_REMOTE
    if relation_broken:
        return RecordClassification.BROKEN_RELATION
    if index_stale:
        return RecordClassification.STALE_INDEX
    if native_present and frontend_present:
        return RecordClassification.HEALTHY
    if native_present:
        return RecordClassification.ORPHAN_NATIVE
    if frontend_present:
        return RecordClassification.ORPHAN_FRONTEND
    if project_present:
        return RecordClassification.ORPHAN_PROJECT
    return RecordClassification.HEALTHY


@dataclass(frozen=True)
class EngineCapability:
    """Static support declaration for one frontend/backend combination."""

    client: str
    engine: str
    inventory: bool = True
    native_delete: bool = False
    frontend_session_delete: bool = False
    frontend_reference_delete: bool = False
    frontend_project_delete: bool = False
    remote_delete: bool = False
    verify: bool = True
    reason: str | None = None
    blockers: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "client", normalize_client(self.client))
        object.__setattr__(self, "engine", normalize_engine(self.engine))
        normalized: list[Mapping[str, Any]] = []
        for blocker in self.blockers:
            if isinstance(blocker, Mapping):
                code = str(
                    blocker.get("blocker_code")
                    or blocker.get("code")
                    or ""
                ).strip()
                if not code:
                    continue
                value = dict(blocker)
                value.setdefault("blocker_code", code)
                normalized.append(value)
            elif isinstance(blocker, str) and blocker.strip():
                normalized.append({"blocker_code": blocker.strip()})
        object.__setattr__(self, "blockers", tuple(normalized))

    @property
    def blocker_codes(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                str(blocker.get("blocker_code") or blocker.get("code"))
                for blocker in self.blockers
                if isinstance(blocker, Mapping)
                and (blocker.get("blocker_code") or blocker.get("code"))
            )
        )

    @property
    def mode(self) -> str:
        # A proof blocker must not be hidden by a broad writer declaration.
        # This is important for an unqualified AionUI native root, where a
        # caller must never obtain a full descriptor.
        if self.blockers:
            return "partial" if any(
                (
                    self.native_delete,
                    self.frontend_session_delete,
                    self.frontend_reference_delete,
                    self.frontend_project_delete,
                    self.remote_delete,
                )
            ) else "inventory_only"
        if (
            self.native_delete
            and self.frontend_session_delete
            and self.frontend_reference_delete
            and self.frontend_project_delete
        ):
            return "full"
        if any(
            (
                self.native_delete,
                self.frontend_session_delete,
                self.frontend_reference_delete,
                self.frontend_project_delete,
                self.remote_delete,
            )
        ):
            return "partial"
        if self.inventory:
            return "inventory_only"
        return "unsupported"

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "engine": self.engine,
            "inventory": self.inventory,
            "native_delete": self.native_delete,
            "frontend_session_delete": self.frontend_session_delete,
            "frontend_reference_delete": self.frontend_reference_delete,
            "frontend_project_delete": self.frontend_project_delete,
            "remote_delete": self.remote_delete,
            "verify": self.verify,
            "mode": self.mode,
            "reason": self.reason,
            "blocker_codes": list(self.blocker_codes),
            "blockers": [dict(blocker) for blocker in self.blockers],
        }


FULL_ENGINE_CAPABILITIES: Mapping[str, EngineCapability] = {
    "codex": EngineCapability(
        "native", "codex", native_delete=True, verify=True,
    ),
    "pi": EngineCapability(
        "native", "pi", native_delete=True, verify=True,
    ),
    "claude": EngineCapability(
        "native", "claude", native_delete=True, verify=True,
    ),
}


def capability_for(
    client: str,
    engine: str,
    *,
    observed: bool = True,
) -> EngineCapability:
    """Return a conservative static capability for a client/backend pair."""

    normalized_client = normalize_client(client)
    normalized_engine = normalize_engine(engine)
    if normalized_client in {"cindy", "aionui"} and normalized_engine in {
        "codex", "pi", "claude",
    }:
        blockers: tuple[Mapping[str, Any], ...] = ()
        reason = (
            "Exact frontend reference cleanup is supported; native and "
            "project-item deletion require a registered writer"
        )
        if normalized_client == "cindy":
            reason = (
                "Native Codex/Pi/Claude deletion, exact Cindy reference "
                "cleanup, and soft-deleted session-row cleanup are supported; "
                "project-item deletion requires a registered writer"
            )
        if normalized_client == "aionui" and normalized_engine in {"pi", "claude"}:
            blockers = (
                {
                    "blocker_code": NATIVE_ROOT_UNVERIFIED,
                    "scope": "native_root",
                    "message": (
                        "AionUI does not uniquely identify the native "
                        f"{normalized_engine} storage root"
                    ),
                },
            )
            reason = (
                "Exact AionUI frontend reference cleanup is supported, but the "
                f"native {normalized_engine} root is unverified"
            )
        return EngineCapability(
            normalized_client,
            normalized_engine,
            inventory=True,
            native_delete=(normalized_client == "cindy"),
            frontend_session_delete=(normalized_client == "cindy"),
            frontend_reference_delete=True,
            frontend_project_delete=False,
            verify=True,
            reason=reason,
            blockers=blockers,
        )
    if normalized_client in {"native", "codex-desktop"}:
        return EngineCapability(
            normalized_client,
            normalized_engine,
            inventory=True,
            native_delete=normalized_engine in {"codex", "pi", "claude"},
            verify=True,
            reason=(None if normalized_engine in {"codex", "pi", "claude"}
                    else "Unknown native engine; inventory only"),
        )
    return EngineCapability(
        normalized_client,
        normalized_engine,
        inventory=observed,
        native_delete=False,
        frontend_session_delete=False,
        frontend_reference_delete=False,
        frontend_project_delete=False,
        verify=observed,
        reason=(
            "Backend is inventoried but has no verified deletion adapter"
            if observed
            else "Backend is not observed"
        ),
    )


__all__ = [
    "ClientName",
    "EngineCapability",
    "FULL_ENGINE_CAPABILITIES",
    "FrontendRefKey",
    "ProjectKey",
    "ProjectSelectionError",
    "RecordClassification",
    "RecordKey",
    "StoreKey",
    "canonical_path",
    "capability_for",
    "classify_record_state",
    "normalize_client",
    "normalize_engine",
    "NATIVE_ROOT_UNVERIFIED",
    "resolve_project_selector",
]
