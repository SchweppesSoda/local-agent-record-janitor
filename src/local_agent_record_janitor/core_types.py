from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


_CODE_RE = re.compile(r"^(?:[a-z][a-z0-9_]*|__[a-z0-9_]+__)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StorageKind(str, Enum):
    """Physical container kinds understood by the cleanup core."""

    CODEX_HOME = "codex_home"
    SQLITE = "sqlite"
    JSON = "json"
    JSONL = "jsonl"
    MANIFEST = "manifest"
    FILE = "file"


class RecordKind(str, Enum):
    """Logical record kinds contained by a :class:`StorageRef`."""

    CONVERSATION = "conversation"
    RELATION = "relation"
    FRONTEND_REFERENCE = "frontend_reference"
    LEGACY_INDEX = "legacy_index"
    DESKTOP_STATE = "desktop_state"
    PI_SESSION = "pi_session"
    CLAUDE_SESSION = "claude_session"


class MutationKind(str, Enum):
    """Typed mutation vocabulary used internally during the compatibility period."""

    DELETE_CONVERSATION = "delete_conversation"
    REMOVE_BROKEN_RELATION = "remove_broken_relation"
    REMOVE_FRONTEND_REFERENCE = "remove_frontend_reference"
    REMOVE_DESKTOP_STATE = "remove_desktop_state"
    REPAIR_LEGACY_INDEX = "repair_legacy_index"
    DELETE_PI_SESSION = "delete_pi_session"
    DELETE_CLAUDE_SESSION = "delete_claude_session"
    KEEP = "keep"

    # These values remain parseable only while the public 0.1 JSON contract is
    # being migrated.  The planner no longer treats them as executable work.
    REPAIR_INDEX_PATH = "repair_index_path"
    QUARANTINE_ARTIFACTS = "quarantine_artifacts"


class ResultStatus(str, Enum):
    COMPLETE = "complete"
    BLOCKED = "blocked"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


@dataclass(frozen=True, order=True)
class BlockerCode:
    """Machine-decision code; presentation text never substitutes for it."""

    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip()
        if not _CODE_RE.fullmatch(normalized):
            raise ValueError(f"Invalid blocker code: {self.value!r}")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class StorageRef:
    """Stable identity for one physical storage container."""

    storage_id: str
    kind: StorageKind
    path: Path
    owner: str

    def __post_init__(self) -> None:
        storage_id = self.storage_id.strip()
        owner = self.owner.strip()
        if not storage_id:
            raise ValueError("storage_id must not be blank")
        if not owner:
            raise ValueError("storage owner must not be blank")
        path = Path(self.path)
        if not str(path):
            raise ValueError("storage path must not be blank")
        object.__setattr__(self, "storage_id", storage_id)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "path", path)

    def to_dict(self) -> dict[str, str]:
        return {
            "storage_id": self.storage_id,
            "kind": self.kind.value,
            "path": str(self.path),
            "owner": self.owner,
        }


@dataclass(frozen=True)
class RecordRef:
    """Stable logical record identity inside exactly one physical storage."""

    storage_id: str
    kind: RecordKind
    record_id: str
    locator: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        storage_id = self.storage_id.strip()
        record_id = self.record_id.strip()
        if not storage_id or not record_id:
            raise ValueError("record storage_id and record_id must not be blank")
        normalized_locator = tuple(
            (str(key).strip(), str(value)) for key, value in self.locator
        )
        keys = [key for key, _value in normalized_locator]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("record locator keys must be non-blank and unique")
        object.__setattr__(self, "storage_id", storage_id)
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "locator", normalized_locator)

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage_id": self.storage_id,
            "kind": self.kind.value,
            "record_id": self.record_id,
            "locator": {key: value for key, value in self.locator},
        }


@dataclass(frozen=True)
class Evidence:
    """Content-free evidence identity used to justify one planned action."""

    evidence_id: str
    target: RecordRef
    evidence_type: str
    fingerprint: str
    source: str

    def __post_init__(self) -> None:
        for name in ("evidence_id", "evidence_type", "fingerprint", "source"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be blank")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "target": self.target.to_dict(),
            "evidence_type": self.evidence_type,
            "fingerprint": self.fingerprint,
            "source": self.source,
        }


@dataclass(frozen=True)
class GuardToken:
    """Exact precondition binding checked immediately before one mutation."""

    action_id: str
    storage_id: str
    record_id: str
    snapshot_fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "action_id",
            "storage_id",
            "record_id",
            "snapshot_fingerprint",
        ):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be blank")
            object.__setattr__(self, name, value)

    @property
    def digest(self) -> str:
        return _sha256_json(
            {
                "action_id": self.action_id,
                "storage_id": self.storage_id,
                "record_id": self.record_id,
                "snapshot_fingerprint": self.snapshot_fingerprint,
            }
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "storage_id": self.storage_id,
            "record_id": self.record_id,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class Action:
    """One typed, storage-qualified mutation candidate."""

    action_id: str
    kind: MutationKind
    target: RecordRef
    snapshot_fingerprint: str
    evidence_ids: tuple[str, ...] = ()
    available: bool = False
    blocker_codes: tuple[BlockerCode, ...] = ()

    def __post_init__(self) -> None:
        action_id = self.action_id.strip()
        snapshot = self.snapshot_fingerprint.strip()
        if not action_id or not snapshot:
            raise ValueError("action_id and snapshot_fingerprint must not be blank")
        evidence_ids = tuple(dict.fromkeys(value.strip() for value in self.evidence_ids))
        if any(not value for value in evidence_ids):
            raise ValueError("evidence IDs must not be blank")
        blocker_codes = tuple(sorted(set(self.blocker_codes)))
        if self.available and blocker_codes:
            raise ValueError("an available action cannot carry blocker codes")
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "snapshot_fingerprint", snapshot)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "blocker_codes", blocker_codes)

    @property
    def guard_token(self) -> GuardToken:
        return GuardToken(
            action_id=self.action_id,
            storage_id=self.target.storage_id,
            record_id=self.target.record_id,
            snapshot_fingerprint=self.snapshot_fingerprint,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "target": self.target.to_dict(),
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "evidence_ids": list(self.evidence_ids),
            "available": self.available,
            "blocker_codes": [str(code) for code in self.blocker_codes],
            "guard_token": self.guard_token.to_dict(),
        }


@dataclass(frozen=True)
class AuthorizedAction:
    """Action bound to one immutable plan authorization and guard token."""

    action: Action
    plan_sha256: str
    guard: GuardToken

    def __post_init__(self) -> None:
        plan_hash = self.plan_sha256.strip().lower()
        if not _SHA256_RE.fullmatch(plan_hash):
            raise ValueError("plan_sha256 must be a lowercase SHA-256 digest")
        if self.guard != self.action.guard_token:
            raise ValueError("guard token does not match the authorized action")
        if not self.action.available:
            raise ValueError("an unavailable action cannot be authorized")
        object.__setattr__(self, "plan_sha256", plan_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.to_dict(),
            "plan_sha256": self.plan_sha256,
            "guard": self.guard.to_dict(),
        }


@dataclass(frozen=True)
class Result:
    """Typed action result; display messages are deliberately not authoritative."""

    action_id: str
    status: ResultStatus
    modified: bool
    blocker_codes: tuple[BlockerCode, ...] = ()
    counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        action_id = self.action_id.strip()
        if not action_id:
            raise ValueError("result action_id must not be blank")
        normalized_counts = tuple(
            (str(key).strip(), int(value)) for key, value in self.counts
        )
        keys = [key for key, _value in normalized_counts]
        if (
            any(not key for key in keys)
            or len(keys) != len(set(keys))
            or any(value < 0 for _key, value in normalized_counts)
        ):
            raise ValueError("result counts must have unique names and nonnegative values")
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "blocker_codes", tuple(sorted(set(self.blocker_codes))))
        object.__setattr__(self, "counts", normalized_counts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "status": self.status.value,
            "modified": self.modified,
            "blocker_codes": [str(code) for code in self.blocker_codes],
            "counts": {key: value for key, value in self.counts},
        }


def blocker_codes(values: Iterable[str]) -> tuple[BlockerCode, ...]:
    return tuple(sorted({BlockerCode(value) for value in values}))


def _sha256_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "Action",
    "AuthorizedAction",
    "BlockerCode",
    "Evidence",
    "GuardToken",
    "MutationKind",
    "RecordKind",
    "RecordRef",
    "Result",
    "ResultStatus",
    "StorageKind",
    "StorageRef",
    "blocker_codes",
]
