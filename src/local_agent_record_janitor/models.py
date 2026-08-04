from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RolloutRecord:
    thread_id: str
    path: Path
    originator: str | None
    source: Any
    cwd: str | None
    timestamp: str | None
    archived: bool

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["path"] = str(self.path)
        return result


@dataclass(frozen=True)
class ThreadSourceInfo:
    """Normalized, display-safe facts carried by a Codex thread source.

    Source values have changed shape across Codex releases.  Keeping the
    normalized result immutable makes it suitable both for display metadata
    and for approval snapshots without retaining a mutable source object.
    """

    is_subagent: bool = False
    parent_thread_ids: tuple[str, ...] = ()
    agent_nickname: str | None = None
    agent_role: str | None = None
    agent_path: str | None = None
    metadata_sources: tuple[str, ...] = ()
    metadata_conflicts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_subagent": self.is_subagent,
            "parent_thread_ids": list(self.parent_thread_ids),
            "agent_nickname": self.agent_nickname,
            "agent_role": self.agent_role,
            "agent_path": self.agent_path,
            "metadata_sources": list(self.metadata_sources),
            "metadata_conflicts": list(self.metadata_conflicts),
        }


@dataclass(frozen=True)
class ConversationSummary:
    """Immutable human-readable identity for one Codex conversation."""

    thread_id: str
    name: str | None = None
    title: str | None = None
    display_name: str | None = None
    display_name_source: str | None = None
    cwd: str | None = None
    project_label: str | None = None
    git_origin_url: str | None = None
    is_subagent: bool = False
    agent_nickname: str | None = None
    agent_role: str | None = None
    agent_path: str | None = None
    parent_thread_ids: tuple[str, ...] = ()
    archived: bool | None = None
    indexed: bool = False
    originator: str | None = None
    metadata_sources: tuple[str, ...] = ()
    metadata_conflicts: tuple[str, ...] = ()
    metadata_evidence_fingerprints: tuple[str, ...] = ()

    def approval_payload(self) -> dict[str, Any]:
        """Return the canonical raw metadata used to approve a deletion.

        ``project_label`` is deliberately omitted: it is derived solely from
        ``cwd``.  A change to the directory therefore changes the fingerprint
        without binding approval to presentation logic.
        """

        return {
            "schema_version": 1,
            "thread_id": self.thread_id,
            "name": self.name,
            "title": self.title,
            "display_name": self.display_name,
            "display_name_source": self.display_name_source,
            "cwd": self.cwd,
            "git_origin_url": self.git_origin_url,
            "is_subagent": self.is_subagent,
            "agent_nickname": self.agent_nickname,
            "agent_role": self.agent_role,
            "agent_path": self.agent_path,
            "parent_thread_ids": list(self.parent_thread_ids),
            "archived": self.archived,
            "indexed": self.indexed,
            "originator": self.originator,
            "metadata_sources": list(self.metadata_sources),
            "metadata_conflicts": list(self.metadata_conflicts),
            "metadata_evidence_fingerprints": list(
                self.metadata_evidence_fingerprints
            ),
        }

    @property
    def metadata_fingerprint(self) -> str:
        canonical = json.dumps(
            self.approval_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return f"v1:{hashlib.sha256(canonical).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "name": self.name,
            "title": self.title,
            "display_name": self.display_name,
            "display_name_source": self.display_name_source,
            "cwd": self.cwd,
            "project_label": self.project_label,
            "git_origin_url": self.git_origin_url,
            "is_subagent": self.is_subagent,
            "agent_nickname": self.agent_nickname,
            "agent_role": self.agent_role,
            "agent_path": self.agent_path,
            "parent_thread_ids": list(self.parent_thread_ids),
            "archived": self.archived,
            "indexed": self.indexed,
            "originator": self.originator,
            "metadata_sources": list(self.metadata_sources),
            "metadata_conflicts": list(self.metadata_conflicts),
            "metadata_evidence_fingerprints": list(
                self.metadata_evidence_fingerprints
            ),
            "metadata_fingerprint": self.metadata_fingerprint,
        }


@dataclass
class Finding:
    platform: str
    platform_session_id: str
    thread_id: str
    reason: str
    platform_db: Path
    codex_home: Path
    platform_updated_at_ms: int | None = None
    rollout: RolloutRecord | None = None
    codex_indexed: bool = False
    codex_archived: bool | None = None
    codex_bin_hint: Path | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def has_codex_artifacts(self) -> bool:
        return (
            self.rollout is not None
            or self.codex_indexed
            or self.details.get("diagnostic_artifact_present") is True
        )

    def short_thread_id(self) -> str:
        if len(self.thread_id) <= 12:
            return self.thread_id
        return f"{self.thread_id[:8]}...{self.thread_id[-4:]}"

    def to_dict(self, *, include_paths: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "platform": self.platform,
            "platform_session_id": self.platform_session_id,
            "thread_id": self.thread_id,
            "reason": self.reason,
            "platform_updated_at_ms": self.platform_updated_at_ms,
            "codex_indexed": self.codex_indexed,
            "codex_archived": self.codex_archived,
            "has_codex_artifacts": self.has_codex_artifacts,
            "rollout": self.rollout.to_dict() if self.rollout else None,
            "details": self.details,
        }
        if include_paths:
            data.update(
                {
                    "platform_db": str(self.platform_db),
                    "codex_home": str(self.codex_home),
                    "codex_bin_hint": str(self.codex_bin_hint) if self.codex_bin_hint else None,
                }
            )
        return data
