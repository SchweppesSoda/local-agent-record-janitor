"""Cindy frontend preflight shared by scoped operation planning.

One collection guard per database validates the frozen native/reference closure.
It does not open app-servers, scan another catalog or mutate a live store.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .frontend_reference_cleanup import guard_frontend_reference_closure
from .record_identity import canonical_path


def preflight_frontend_actions(actions: Sequence[Any]) -> list[dict[str, Any]]:
    databases: dict[str, dict[str, Mapping[str, Any]]] = {}
    action_ids: dict[str, list[str]] = {}
    for action in actions:
        for evidence in getattr(action.impact, "frontend_reference_evidence", ()):
            if evidence.get("platform") != "cindy":
                continue
            database = canonical_path(str(evidence.get("database") or ""))
            # Identical descendant reference actions may occur in two selected
            # root closures. Preserve each exact row once without widening it.
            key = json.dumps(evidence, sort_keys=True)
            databases.setdefault(database, {})[key] = evidence
            action_ids.setdefault(database, []).append(str(action.action_id))
    blockers = []
    for database, evidence in databases.items():
        try:
            guard_frontend_reference_closure(tuple(evidence.values()))
        except Exception as exc:
            blockers.append({
                "blocker_code": "frontend_preflight_blocked",
                "scope": "frontend:" + database,
                "severity": "error",
                "retryable": True,
                "action_ids": sorted(set(action_ids[database])),
                "message": str(exc),
            })
    return blockers
