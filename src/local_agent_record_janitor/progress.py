"""Opt-in, metadata-only progress reporting for long-running operations.

Progress is deliberately kept out of the operation result and plan contracts.
Callers opt in to one JSON object per line on a supplied stderr stream, so a
machine-readable stdout result remains a single compatible JSON document.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, TextIO


_BODY_KEYS = frozenset(
    {
        "body",
        "chat_body",
        "message_body",
        "messages",
        "transcript",
        "prompt",
        "response",
        "content",
    }
)
_MISSING = object()


def _metadata(value: Any, *, key: str | None = None) -> Any:
    """Return a bounded JSON-safe value without conversation content."""

    if key is not None and key.casefold() in _BODY_KEYS:
        return _MISSING
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            cleaned = _metadata(raw_value, key=str(raw_key))
            if cleaned is not _MISSING:
                result[str(raw_key)] = cleaned
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            cleaned
            for raw_value in value
            if (cleaned := _metadata(raw_value)) is not _MISSING
        ]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 240:
            return value[:237] + "..."
        return value
    return str(value)[:240]


class ProgressReporter:
    """Write live progress events as newline-delimited JSON to stderr.

    The callback is intentionally best effort. A closed or otherwise broken
    diagnostic stream must never change the operation's safety result.
    """

    def __init__(
        self,
        stream: TextIO,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stream = stream
        self.clock = clock
        self._lock = Lock()
        self._disabled = False
        try:
            self._started_at = float(clock())
        except Exception:
            # A diagnostic clock is injectable for tests and embedding hosts;
            # a broken one must not prevent the requested operation.
            self._started_at = 0.0
            self._disabled = True

    def __call__(self, event: Mapping[str, Any] | None = None, /, **fields: Any) -> None:
        with self._lock:
            if self._disabled:
                return
            try:
                payload: dict[str, Any] = {}
                if isinstance(event, Mapping):
                    payload.update(event)
                payload.update(fields)
                payload.setdefault("stage", "operation")
                payload.setdefault("status", "running")
                payload["schema_version"] = "larj.progress.v1"
                payload["event"] = "progress"
                payload["elapsed_seconds"] = round(
                    max(0.0, float(self.clock() - self._started_at)),
                    3,
                )
                cleaned = _metadata(payload)
                if not isinstance(cleaned, Mapping):
                    return
                line = json.dumps(
                    dict(cleaned),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                self.stream.write(line + "\n")
                self.stream.flush()
            except Exception:
                self._disabled = True


__all__ = ["ProgressReporter"]
