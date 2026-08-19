from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# ``cleanup_blocked_reason`` is presentation text only.  Every exception that
# deliberately relaxes a blocker must be authorized by one exact, structured
# code set so wording, localization, or punctuation can never change safety.
CASCADE_REQUIRES_EXPLICIT_SCOPE = "cascade_requires_explicit_scope"
IDENTITY_CONFLICT = "identity_conflict"
INTEGRITY_REVIEW_REQUIRED = "integrity_review_required"
LEGACY_INDEX_NOT_THREAD_TARGET = "legacy_index_not_thread_target"
LIVE_FRONTEND_REFERENCE = "live_frontend_reference"
NO_NATIVE_ARTIFACT = "no_native_artifact"
SOURCE_PARENT_UNVERIFIED = "source_parent_unverified"
SPAWN_EDGE_OPEN = "spawn_edge_open"
STANDALONE_RELATION_CLEANUP_UNAVAILABLE = (
    "standalone_relation_cleanup_unavailable"
)
RELATION_EVIDENCE_INCOMPLETE = "relation_evidence_incomplete"
RELATION_ROW_AMBIGUOUS = "relation_row_ambiguous"

MALFORMED_BLOCKER_CODES = "__malformed_cleanup_blocker_codes__"


def cleanup_blocker_codes(details: Mapping[str, Any]) -> frozenset[str]:
    """Return normalized structured blocker codes, failing closed.

    Unknown string codes are retained: callers only waive blockers for exact
    known sets, so a future or misspelled code remains a hard blocker.  A
    malformed container/value is represented by a sentinel for the same
    reason.
    """

    raw = details.get("cleanup_blocker_codes")
    if raw is None:
        return frozenset()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset({MALFORMED_BLOCKER_CODES})
    result: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            result.add(MALFORMED_BLOCKER_CODES)
            continue
        result.add(value.strip())
    return frozenset(result)


def exact_blocker_codes(details: Mapping[str, Any], *expected: str) -> bool:
    return cleanup_blocker_codes(details) == frozenset(expected)


__all__ = [
    "CASCADE_REQUIRES_EXPLICIT_SCOPE",
    "IDENTITY_CONFLICT",
    "INTEGRITY_REVIEW_REQUIRED",
    "LEGACY_INDEX_NOT_THREAD_TARGET",
    "LIVE_FRONTEND_REFERENCE",
    "MALFORMED_BLOCKER_CODES",
    "NO_NATIVE_ARTIFACT",
    "RELATION_EVIDENCE_INCOMPLETE",
    "RELATION_ROW_AMBIGUOUS",
    "SOURCE_PARENT_UNVERIFIED",
    "SPAWN_EDGE_OPEN",
    "STANDALONE_RELATION_CLEANUP_UNAVAILABLE",
    "cleanup_blocker_codes",
    "exact_blocker_codes",
]
