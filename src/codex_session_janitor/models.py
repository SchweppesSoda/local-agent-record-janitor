from __future__ import annotations

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
