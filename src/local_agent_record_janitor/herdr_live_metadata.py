"""Allowlisted Herdr JSON API metadata at pinned protocol 22.

The public IDs in this API are opaque and do not identify persisted pane IDs,
native stores, operating-system processes, or all writers of an agent record.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any

from .herdr_metadata import HerdrMetadataError, HerdrObservation, _agent_session, _integer, _text, _unique_object

PROTOCOL = 22
MAX_ITEMS = 4096
STATUSES = {"idle", "working", "blocked", "done", "unknown"}


class HerdrLiveMetadataError(ValueError):
    pass


@dataclass(frozen=True)
class HerdrLiveMetadata:
    version: str
    observations: tuple[HerdrObservation, ...]
    pane_states: tuple[tuple[str, str, str, str], ...]
    counts: tuple[tuple[str, int], ...]
    errors: tuple[str, ...] = ()


def decode_response(data: bytes, request_id: str) -> dict[str, Any]:
    def invalid_constant(_: str) -> None:
        raise HerdrLiveMetadataError("live_json_invalid")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise HerdrLiveMetadataError("live_json_invalid") from None
    if not isinstance(value, dict) or value.get("id") != request_id:
        raise HerdrLiveMetadataError("live_response_id_invalid")
    if set(value) == {"id", "error"}:
        raise HerdrLiveMetadataError("live_api_error")
    if set(value) != {"id", "result"} or not isinstance(value["result"], dict):
        raise HerdrLiveMetadataError("live_response_shape_invalid")
    return value["result"]


def _version(value: dict[str, Any], kind: str) -> str:
    if value.get("type") != kind or type(value.get("protocol")) is not int or value["protocol"] != PROTOCOL:
        raise HerdrLiveMetadataError("live_protocol_unsupported")
    try:
        return _text(value.get("version"), maximum=128)
    except HerdrMetadataError:
        raise HerdrLiveMetadataError("live_version_invalid") from None


def parse_pong(result: dict[str, Any]) -> str:
    return _version(result, "pong")


def parse_detached_daemon(result: dict[str, Any]) -> bool | None:
    """Server's startup observation, never proof that clients or writers left."""
    capabilities = result.get("capabilities")
    if capabilities is None:
        return None
    if not isinstance(capabilities, dict):
        raise HerdrLiveMetadataError("live_capabilities_invalid")
    value = capabilities.get("detached_server_daemon")
    if value is not None and type(value) is not bool:
        raise HerdrLiveMetadataError("live_detached_daemon_invalid")
    return value


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS or any(not isinstance(row, dict) for row in value):
        raise HerdrLiveMetadataError("live_collection_invalid")
    return value


def _id(value: Any) -> str:
    return _text(value, maximum=512)


def _status(row: dict[str, Any]) -> str:
    value = row.get("agent_status")
    if not isinstance(value, str) or value not in STATUSES:
        raise HerdrLiveMetadataError("live_agent_status_invalid")
    if type(row.get("focused")) is not bool:
        raise HerdrLiveMetadataError("live_focus_invalid")
    return value


def _index(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        key = _id(row.get(field))
        if key in result:
            raise HerdrLiveMetadataError("live_duplicate_identity")
        result[key] = row
    return result


def _rect(value: Any) -> None:
    if not isinstance(value, dict):
        raise HerdrLiveMetadataError("live_layout_geometry_invalid")
    for field in ("x", "y", "width", "height"):
        _integer(value.get(field), 2**16 - 1)


def parse_live_snapshot(result: dict[str, Any], pong_version: str) -> HerdrLiveMetadata:
    if result.get("type") != "session_snapshot" or not isinstance(result.get("snapshot"), dict):
        raise HerdrLiveMetadataError("live_snapshot_shape_invalid")
    snapshot = result["snapshot"]
    version = _version({**snapshot, "type": "session_snapshot"}, "session_snapshot")
    if version != pong_version:
        raise HerdrLiveMetadataError("live_version_changed")
    observations, states, errors = [], [], []
    # Protocol/shape are checked independently. A matching version string is
    # only compatibility evidence and never establishes a binary revision.
    if not re.fullmatch(r"0\.9\.3(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version):
        errors.append("live_version_unverified")
    try:
        rows = {name: _rows(snapshot.get(name)) for name in ("workspaces", "tabs", "panes", "layouts", "agents")}
        workspaces = _index(rows["workspaces"], "workspace_id")
        tabs = _index(rows["tabs"], "tab_id")
        panes = _index(rows["panes"], "pane_id")
        for workspace_id, workspace in workspaces.items():
            _status(workspace)
            for field in ("number", "pane_count", "tab_count"):
                _integer(workspace.get(field))
            active = _id(workspace.get("active_tab_id"))
            if active not in tabs or tabs[active].get("workspace_id") != workspace_id:
                raise HerdrLiveMetadataError("live_workspace_tab_mismatch")
        for tab_id, tab in tabs.items():
            _status(tab)
            for field in ("number", "pane_count"):
                _integer(tab.get(field))
            if _id(tab.get("workspace_id")) not in workspaces:
                raise HerdrLiveMetadataError("live_tab_workspace_missing")
        for field, index in (("focused_workspace_id", workspaces), ("focused_tab_id", tabs), ("focused_pane_id", panes)):
            if snapshot.get(field) is not None and _id(snapshot[field]) not in index:
                raise HerdrLiveMetadataError("live_focused_identity_missing")
        terminals = set()
        pane_refs = {}
        for pane_id, pane in panes.items():
            workspace_id, tab_id = _id(pane.get("workspace_id")), _id(pane.get("tab_id"))
            terminal_id = _id(pane.get("terminal_id"))
            if (workspace_id not in workspaces or tab_id not in tabs
                    or tabs[tab_id].get("workspace_id") != workspace_id or terminal_id in terminals):
                raise HerdrLiveMetadataError("live_pane_identity_mismatch")
            terminals.add(terminal_id)
            status = _status(pane)
            _integer(pane.get("revision"))
            states.append((workspace_id, tab_id, pane_id, status))
            if pane.get("agent_session") is not None:
                item = _agent_session(pane["agent_session"], f"live/workspaces/{workspace_id}/tabs/{tab_id}/panes/{pane_id}/agent_session")
                observations.append(item)
                pane_refs[pane_id] = (item.source, item.agent, item.kind, item.value)
                if not item.supported:
                    errors.append("live_agent_session_unsupported")
        agents = _index(rows["agents"], "terminal_id")
        agent_panes = set()
        for terminal_id, agent in agents.items():
            pane_id = _id(agent.get("pane_id"))
            pane = panes.get(pane_id)
            if (pane is None or pane.get("terminal_id") != terminal_id or pane_id in agent_panes
                    or agent.get("workspace_id") != pane.get("workspace_id") or agent.get("tab_id") != pane.get("tab_id")):
                raise HerdrLiveMetadataError("live_agent_pane_mismatch")
            agent_panes.add(pane_id)
            _status(agent)
            _integer(agent.get("revision"))
            _integer(agent.get("state_change_seq", 0))
            if agent.get("agent_session") is not None:
                item = _agent_session(agent["agent_session"], f"live/agents/{terminal_id}/agent_session")
                key = (item.source, item.agent, item.kind, item.value)
                if key != pane_refs.get(pane_id):
                    observations.append(item)
                    errors.append("live_agent_reference_conflict")
                if not item.supported:
                    errors.append("live_agent_session_unsupported")
            elif pane_id in pane_refs:
                errors.append("live_agent_reference_conflict")
        # agents contains recognized agent terminals, not ordinary shell panes.
        # agent_session alone can be a persisted fallback, so it is not proof
        # that the pane currently belongs to the agent-terminal subset.
        if not {p for p, row in panes.items() if row.get("agent") is not None} <= agent_panes:
            errors.append("live_agent_coverage_incomplete")
        panes_by_tab = {tab_id: set() for tab_id in tabs}
        tabs_by_workspace = {workspace_id: set() for workspace_id in workspaces}
        for tab_id, tab in tabs.items():
            tabs_by_workspace[tab["workspace_id"]].add(tab_id)
        for pane_id, pane in panes.items():
            panes_by_tab[pane["tab_id"]].add(pane_id)
        for tab_id, tab in tabs.items():
            if tab["pane_count"] != len(panes_by_tab[tab_id]):
                errors.append("live_pane_coverage_incomplete")
        for workspace_id, workspace in workspaces.items():
            workspace_tabs = tabs_by_workspace[workspace_id]
            if workspace["tab_count"] != len(workspace_tabs):
                errors.append("live_tab_coverage_incomplete")
            if workspace["pane_count"] != sum(len(panes_by_tab[t]) for t in workspace_tabs):
                errors.append("live_workspace_pane_coverage_incomplete")
        layout_tabs = set()
        for layout in rows["layouts"]:
            tab_id, workspace_id = _id(layout.get("tab_id")), _id(layout.get("workspace_id"))
            if (tab_id not in tabs or tabs[tab_id].get("workspace_id") != workspace_id or tab_id in layout_tabs
                    or type(layout.get("zoomed")) is not bool):
                raise HerdrLiveMetadataError("live_layout_identity_mismatch")
            layout_tabs.add(tab_id)
            _rect(layout.get("area"))
            layout_panes = _index(_rows(layout.get("panes")), "pane_id")
            for pane_id, pane in layout_panes.items():
                if pane_id not in panes or panes[pane_id].get("tab_id") != tab_id or type(pane.get("focused")) is not bool:
                    raise HerdrLiveMetadataError("live_layout_pane_mismatch")
                _rect(pane.get("rect"))
            if set(layout_panes) != panes_by_tab[tab_id]:
                errors.append("live_layout_pane_coverage_incomplete")
            if _id(layout.get("focused_pane_id")) not in layout_panes:
                raise HerdrLiveMetadataError("live_layout_focus_missing")
            for split in _index(_rows(layout.get("splits")), "id").values():
                ratio = split.get("ratio")
                if (split.get("direction") not in {"right", "down"} or type(ratio) not in {int, float}
                        or not math.isfinite(ratio) or abs(ratio) > 3.4028235e38):
                    raise HerdrLiveMetadataError("live_layout_geometry_invalid")
                _rect(split.get("rect"))
        if layout_tabs != set(tabs):
            errors.append("live_layout_coverage_incomplete")
    except HerdrMetadataError:
        errors.append("live_metadata_field_invalid")
    except HerdrLiveMetadataError as exc:
        errors.append(str(exc))
    return HerdrLiveMetadata(version, tuple(observations), tuple(states),
        tuple((name, len(value)) for name, value in rows.items()) if "rows" in locals() else (),
        tuple(dict.fromkeys(errors)))
