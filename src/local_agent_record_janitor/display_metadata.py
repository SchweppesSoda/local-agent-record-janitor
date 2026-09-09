"""Bounded display strings; identity evidence is fingerprinted separately."""
from __future__ import annotations

import re


def display_title(value: object, limit: int = 240) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if any(marker in value.casefold() for marker in (
        "transcript start", "the following is the codex agent history",
        ">>> approval request", "reviewed codex session id:",
    )):
        return "自动审查记录"
    text = re.sub(r"\s+", " ", value).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"
