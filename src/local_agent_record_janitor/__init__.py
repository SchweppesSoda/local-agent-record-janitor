"""Audit and safely clean storage-qualified local agent records.

Codex records are threads; Pi Agent and Claude Code records are sessions.
Cindy and AionUI rows are frontend references to native records; exact stale
references can be cleaned as separately authorized mutations.
"""

from .models import ConversationSummary, Finding, RolloutRecord, ThreadSourceInfo

__all__ = [
    "ConversationSummary",
    "Finding",
    "RolloutRecord",
    "ThreadSourceInfo",
]
__version__ = "0.2.0"
