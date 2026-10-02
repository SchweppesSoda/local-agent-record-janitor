"""Allowlisted observations from Herdr persisted snapshot v3 (d6b40d4).

No history/terminal body source, argv execution, socket, restore or native root
inference. Diagnostics are fixed codes, never parser excerpts or raw JSON.
"""

from __future__ import annotations

import json
import math
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .herdr_discovery import HerdrDiscoveryError, require_plain_file
from .path_identity import is_local_absolute_locator

MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 4096


class HerdrMetadataError(ValueError):
    pass


@dataclass(frozen=True)
class HerdrObservation:
    locator: str
    source: str
    agent: str
    kind: str
    value: str
    supported: bool


def _text(value: Any, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or any(unicodedata.category(c) == "Cc" for c in value):
        raise HerdrMetadataError("snapshot_text_invalid")
    try:
        bounded = len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        bounded = False
    if not bounded:
        raise HerdrMetadataError("snapshot_text_invalid")
    return value


def _integer(value: Any, maximum: int = 2**64 - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise HerdrMetadataError("snapshot_integer_invalid")
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise HerdrMetadataError("snapshot_collection_invalid")
    return value


def _layout(value: Any, *, depth: int = 0) -> set[int]:
    if depth > 32 or not isinstance(value, dict) or len(value) != 1:
        raise HerdrMetadataError("snapshot_layout_invalid")
    if "Pane" in value:
        return {_integer(value["Pane"], 2**32 - 1)}
    split = value.get("Split")
    if not isinstance(split, dict) or set(split) != {"direction", "ratio", "first", "second"}:
        raise HerdrMetadataError("snapshot_layout_invalid")
    ratio = split["ratio"]
    if (not isinstance(split["direction"], str) or split["direction"] not in {"Horizontal", "Vertical"}
            or type(ratio) not in {int, float} or abs(ratio) > 3.4028235e38 or not math.isfinite(ratio)):
        raise HerdrMetadataError("snapshot_layout_invalid")
    first, second = _layout(split["first"], depth=depth + 1), _layout(split["second"], depth=depth + 1)
    if first & second or len(first | second) > MAX_ITEMS:
        raise HerdrMetadataError("snapshot_layout_invalid")
    return first | second


def _agent_session(value: Any, locator: str) -> HerdrObservation:
    if not isinstance(value, dict):
        raise HerdrMetadataError("snapshot_agent_session_invalid")
    source, agent = _text(value.get("source"), maximum=512), _text(value.get("agent"), maximum=512)
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in {"id", "path"}:
        raise HerdrMetadataError("snapshot_agent_session_kind_unsupported")
    raw = _text(value.get("value"), maximum=512 if kind == "id" else 4096)
    supported = source == "herdr:" + agent and agent in {"codex", "claude", "pi"}
    supported = supported and (kind == "id" or agent == "pi" and is_local_absolute_locator(raw))
    return HerdrObservation(locator, source, agent, kind, raw, supported)


def parse_herdr_snapshot(value: Any) -> tuple[tuple[HerdrObservation, ...], tuple[str, ...]]:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 3:
        return (), ("snapshot_version_unsupported",)
    observations, errors = [], []
    try:
        workspaces = _list(value.get("workspaces"))
        _integer(value.get("selected"))
        if "active" not in value:
            raise HerdrMetadataError("snapshot_active_missing")
        if value["active"] is not None:
            _integer(value["active"])
    except HerdrMetadataError as exc:
        return (), (str(exc),)
    for wi, workspace in enumerate(workspaces):
        try:
            if not isinstance(workspace, dict):
                raise HerdrMetadataError("snapshot_workspace_invalid")
            _text(workspace.get("identity_cwd"))  # Project metadata, never a native root.
            tabs = _list(workspace.get("tabs"))
            _integer(workspace.get("active_tab", 0))
        except HerdrMetadataError as exc:
            errors.append(str(exc))
            continue
        for ti, tab in enumerate(tabs):
            try:
                if not isinstance(tab, dict) or type(tab.get("zoomed")) is not bool:
                    raise HerdrMetadataError("snapshot_tab_invalid")
                pane_map = tab.get("panes")
                if not isinstance(pane_map, dict) or len(pane_map) > MAX_ITEMS:
                    raise HerdrMetadataError("snapshot_panes_invalid")
                pane_ids = set()
                for key in pane_map:
                    if not isinstance(key, str) or not key.isascii() or not key.isdecimal():
                        raise HerdrMetadataError("snapshot_panes_invalid")
                    pane_id = _integer(int(key), 2**32 - 1)
                    if pane_id in pane_ids:
                        raise HerdrMetadataError("snapshot_panes_invalid")
                    pane_ids.add(pane_id)
                if not _layout(tab.get("layout")) <= pane_ids:
                    raise HerdrMetadataError("snapshot_layout_pane_missing")
                for field in ("focused", "root_pane"):
                    if tab.get(field) is not None:
                        _integer(tab[field], 2**32 - 1)
            except (HerdrMetadataError, ValueError) as exc:
                errors.append(str(exc) if isinstance(exc, HerdrMetadataError) else "snapshot_panes_invalid")
                continue
            for pane_id, pane in pane_map.items():
                try:
                    if not isinstance(pane, dict):
                        raise HerdrMetadataError("snapshot_pane_invalid")
                    _text(pane.get("cwd"))
                    locator = f"workspaces/{wi}/tabs/{ti}/panes/{pane_id}/agent_session"
                    if pane.get("agent_session") is not None:
                        observation = _agent_session(pane["agent_session"], locator)
                        observations.append(observation)
                        if not observation.supported:
                            errors.append("snapshot_agent_session_unsupported")
                    # These can carry independent restore semantics. Do not
                    # infer a native ID from argv; preserve incomplete coverage.
                    if pane.get("agent_resume") is not None or pane.get("launch_argv") is not None:
                        errors.append("snapshot_argv_restore_not_covered")
                    if pane.get("agent_session") is None and (pane.get("agent_name") is not None or pane.get("managed_agent_kind") is not None):
                        errors.append("snapshot_agent_metadata_not_covered")
                except HerdrMetadataError as exc:
                    errors.append(str(exc))
    return tuple(observations), tuple(dict.fromkeys(errors))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise HerdrMetadataError("snapshot_duplicate_field")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise HerdrMetadataError("snapshot_json_invalid")


def read_herdr_snapshot(path: Path) -> tuple[tuple[HerdrObservation, ...], tuple[str, ...]]:
    try:
        before = require_plain_file(path)
        if before.st_size > MAX_SNAPSHOT_BYTES:
            raise HerdrMetadataError("snapshot_size_limit_exceeded")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise HerdrMetadataError("snapshot_file_changed")
            data = stream.read(MAX_SNAPSHOT_BYTES + 1)
        after = require_plain_file(path)
        if len(data) > MAX_SNAPSHOT_BYTES or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise HerdrMetadataError("snapshot_file_changed")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        return parse_herdr_snapshot(value)
    except (HerdrMetadataError, HerdrDiscoveryError) as exc:
        return (), (str(exc),)
    except (OSError, UnicodeError, ValueError, RecursionError, OverflowError):
        return (), ("snapshot_unavailable_or_invalid",)
