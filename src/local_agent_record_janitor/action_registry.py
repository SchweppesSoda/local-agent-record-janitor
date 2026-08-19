from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ActionCapability:
    kind: str
    implemented: bool
    mutation_family: str | None
    requires_clients_closed: bool
    verifies_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "implemented": self.implemented,
            "mutation_family": self.mutation_family,
            "requires_clients_closed": self.requires_clients_closed,
            "verifies_by": self.verifies_by,
        }


ACTION_REGISTRY: Mapping[str, ActionCapability] = {
    "delete_conversation": ActionCapability(
        kind="delete_conversation",
        implemented=True,
        mutation_family="delete_conversation",
        requires_clients_closed=True,
        verifies_by="native_thread_artifacts_absent",
    ),
    "repair_legacy_index": ActionCapability(
        kind="repair_legacy_index",
        implemented=True,
        mutation_family="repair_legacy_index",
        requires_clients_closed=True,
        verifies_by="approved_legacy_lines_absent",
    ),
    "remove_desktop_state": ActionCapability(
        kind="remove_desktop_state",
        implemented=True,
        mutation_family="remove_desktop_state",
        requires_clients_closed=True,
        verifies_by="desktop_state_markers_absent",
    ),
    "remove_broken_relation": ActionCapability(
        kind="remove_broken_relation",
        implemented=False,
        mutation_family=None,
        requires_clients_closed=True,
        verifies_by="not_implemented",
    ),
    "repair_index_path": ActionCapability(
        kind="repair_index_path",
        implemented=False,
        mutation_family=None,
        requires_clients_closed=True,
        verifies_by="not_implemented",
    ),
    "quarantine_artifacts": ActionCapability(
        kind="quarantine_artifacts",
        implemented=False,
        mutation_family=None,
        requires_clients_closed=True,
        verifies_by="not_implemented",
    ),
    "remove_frontend_reference": ActionCapability(
        kind="remove_frontend_reference",
        implemented=False,
        mutation_family=None,
        requires_clients_closed=True,
        verifies_by="not_implemented",
    ),
    "keep": ActionCapability(
        kind="keep",
        implemented=True,
        mutation_family=None,
        requires_clients_closed=False,
        verifies_by="no_mutation",
    ),
}


def action_capability(kind: object) -> ActionCapability:
    value = getattr(kind, "value", kind)
    key = str(value)
    return ACTION_REGISTRY.get(
        key,
        ActionCapability(
            kind=key,
            implemented=False,
            mutation_family=None,
            requires_clients_closed=True,
            verifies_by="unknown_action_kind",
        ),
    )


__all__ = ["ACTION_REGISTRY", "ActionCapability", "action_capability"]
