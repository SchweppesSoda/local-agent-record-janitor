"""Schema 3 pane/history transformations for Herdr d6b40d4.

Ownership is supplied by the coordinator's exact native bindings. This module
never derives a native root from cwd or executes persisted launch arguments.
"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
import struct

from .herdr_metadata import parse_herdr_snapshot, _unique_object, _invalid_constant


class HerdrCleanupError(RuntimeError):
    def __init__(self, code):
        super().__init__("herdr_" + code)
        self.kind = str(self)


def fail(code):
    raise HerdrCleanupError(code)


def decode(raw):
    if len(raw) > 16 * 1024 * 1024:
        fail("json_budget_exceeded")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        todo, count = [(value, 0)], 0
        while todo:
            item, depth = todo.pop()
            count += 1
            if count > 100000 or depth > 64:
                fail("json_budget_exceeded")
            if isinstance(item, dict):
                todo.extend((member, depth + 1) for member in item.values())
            elif isinstance(item, list):
                todo.extend((member, depth + 1) for member in item)
        return value
    except (ValueError, UnicodeError, RecursionError):
        fail("json_invalid")


def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def integer(value, maximum=2**64 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        fail("snapshot_integer_invalid")
    return value


def text(value):
    if not isinstance(value, str) or len(value) > 16384:
        fail("snapshot_text_invalid")
    try:
        value.encode("utf-8")
    except UnicodeError:
        fail("snapshot_text_invalid")
    return value


def f32(value):
    if type(value) not in {int, float} or not math.isfinite(value):
        fail("snapshot_float_invalid")
    try:
        result = struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error):
        fail("snapshot_float_invalid")
    if not math.isfinite(result):
        fail("snapshot_float_invalid")
    return result


def _map(value, transform):
    if not isinstance(value, dict):
        fail("snapshot_map_invalid")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.isascii() or not key.isdecimal():
            fail("snapshot_map_key_invalid")
        normalized = str(integer(int(key), 2**32 - 1))
        if normalized in result:
            fail("snapshot_map_key_duplicate")
        result[normalized] = transform(item)
    return result


def _optional(value, transform):
    return None if value is None else transform(value)


def _layout_projection(node):
    if "Pane" in node:
        return {"Pane": integer(node["Pane"], 2**32 - 1)}
    split = node["Split"]
    return {"Split": {"direction": split["direction"], "ratio": f32(split["ratio"]),
        "first": _layout_projection(split["first"]), "second": _layout_projection(split["second"])}}


def _pane_projection(pane):
    result = {"cwd": text(pane["cwd"])}
    for key in ("label", "agent_name", "managed_agent_kind"):
        if pane.get(key) is not None:
            result[key] = text(pane[key])
    for key, fields in (("agent_session", ("source", "agent", "kind", "value")),
                        ("agent_resume", ("source", "agent", "argv"))):
        if pane.get(key) is not None:
            item = pane[key]
            if not isinstance(item, dict) or set(fields) - item.keys():
                fail("snapshot_agent_shape_invalid")
            if key == "agent_resume" and not isinstance(item["argv"], list):
                fail("snapshot_agent_shape_invalid")
            result[key] = {field: [text(v) for v in item[field]] if field == "argv" and isinstance(item[field], list)
                          else text(item[field]) for field in fields}
    if pane.get("launch_argv") is not None:
        if not isinstance(pane["launch_argv"], list):
            fail("snapshot_agent_shape_invalid")
        result["launch_argv"] = [text(value) for value in pane["launch_argv"]]
    return result


def _tab_projection(tab):
    return {"custom_name": _optional(tab.get("custom_name"), text), "layout": _layout_projection(tab["layout"]),
        "panes": _map(tab["panes"], _pane_projection), "zoomed": tab["zoomed"],
        "focused": _optional(tab.get("focused"), lambda v: integer(v, 2**32 - 1)),
        "root_pane": _optional(tab.get("root_pane"), lambda v: integer(v, 2**32 - 1))}


def normalized(snapshot):
    _observations, errors = parse_herdr_snapshot(snapshot)
    # Resume/argv content is retained on survivors and erased only with its
    # entire owned pane; it is not an independent source of native authority.
    if set(errors) - {"snapshot_argv_restore_not_covered", "snapshot_agent_metadata_not_covered"}:
        fail("snapshot_schema_unverified")
    workspaces = []
    for workspace in snapshot["workspaces"]:
        tabs = [_tab_projection(tab) for tab in workspace["tabs"]]
        numbers = workspace.get("public_tab_numbers", [])
        if not isinstance(numbers, list):
            fail("snapshot_public_numbers_invalid")
        item = {"id": _optional(workspace.get("id"), text), "custom_name": _optional(workspace.get("custom_name"), text),
            "identity_cwd": text(workspace["identity_cwd"]),
            "public_pane_numbers": _map(workspace.get("public_pane_numbers", {}), integer),
            "next_public_pane_number": integer(workspace.get("next_public_pane_number", 0)),
            "public_tab_numbers": [integer(n) for n in numbers],
            "next_public_tab_number": integer(workspace.get("next_public_tab_number", 0)),
            "tabs": tabs, "active_tab": integer(workspace.get("active_tab", 0))}
        if workspace.get("worktree_space") is not None:
            space = workspace["worktree_space"]
            if not isinstance(space, dict) or type(space.get("is_linked_worktree")) is not bool:
                fail("snapshot_worktree_invalid")
            item["worktree_space"] = {key: text(space[key]) for key in ("key", "label", "repo_root", "checkout_path")}
            item["worktree_space"]["is_linked_worktree"] = space["is_linked_worktree"]
        workspaces.append(item)
    collapsed = snapshot.get("collapsed_space_keys", [])
    if not isinstance(collapsed, list):
        fail("snapshot_collapsed_spaces_invalid")
    return {"version": 3, "workspaces": workspaces, "active": _optional(snapshot["active"], integer),
        "selected": integer(snapshot["selected"]), "sidebar_width": _optional(snapshot.get("sidebar_width"), lambda v: integer(v, 65535)),
        "sidebar_section_split": _optional(snapshot.get("sidebar_section_split"), f32),
        "collapsed_space_keys": sorted({text(value) for value in collapsed})}


def _serde_json(value):
    """Sorted serde_json Value projection, including widened finite f32s."""
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + _serde_json(value[key]) for key in sorted(value)) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_serde_json(item) for item in value) + "]"
    if type(value) is float:
        raw = repr(value)
        if value == 0:
            return raw
        if 1e-5 <= abs(value) < 1e16:
            fixed = format(Decimal(raw), "f")
            return fixed if "." in fixed else fixed + ".0"
        if "e" in raw:
            mantissa, exponent = raw.split("e")
            return mantissa.removesuffix(".0") + "e" + format(int(exponent), "+d")
        return raw
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(snapshot):
    return hashlib.sha256(_serde_json(normalized(snapshot)).encode("utf-8")).hexdigest()


def leaves(layout):
    if "Pane" in layout:
        return [layout["Pane"]]
    return leaves(layout["Split"]["first"]) + leaves(layout["Split"]["second"])


def prune_layout(layout, removed):
    if "Pane" in layout:
        return None if layout["Pane"] in removed else deepcopy(layout)
    split = deepcopy(layout["Split"])
    first, second = (prune_layout(split[key], removed) for key in ("first", "second"))
    if first is None or second is None:
        return first if second is None else second
    return {"Split": {**split, "first": first, "second": second}}


def _selection(old, surviving, *, nullable=False):
    if not surviving:
        return None if nullable else 0
    if old is None:
        return None if nullable else 0
    chosen = old if old in surviving else max((i for i in surviving if i <= old), default=surviving[0])
    return surviving.index(chosen)


def prune_snapshot(snapshot, owned):
    """owned is the exact set of (old workspace, old tab, u32 pane) locators."""
    projection = normalized(snapshot)
    if not owned:
        return deepcopy(snapshot), [(wi, [(ti, set(map(int, tab["panes"])))
            for ti, tab in enumerate(workspace["tabs"])]) for wi, workspace in enumerate(snapshot["workspaces"])]
    result, kept_workspaces, coordinates = deepcopy(snapshot), [], []
    for wi, workspace in enumerate(result["workspaces"]):
        if not any(w == wi for w, _t, _p in owned):
            kept_workspaces.append(wi)
            coordinates.append((wi, [(ti, set(map(int, tab["panes"]))) for ti, tab in enumerate(workspace["tabs"])]))
            continue
        original = projection["workspaces"][wi]
        pane_numbers = dict(original["public_pane_numbers"])
        next_pane = max([original["next_public_pane_number"], 1, *(v + 1 for v in pane_numbers.values())])
        for tab in original["tabs"]:
            for identifier in leaves(tab["layout"]):
                if str(identifier) not in pane_numbers:
                    pane_numbers[str(identifier)] = next_pane
                    next_pane += 1
        tab_numbers = original["public_tab_numbers"]
        effective_tabs = [tab_numbers[i] if i < len(tab_numbers) else i + 1 for i in range(len(original["tabs"]))]
        next_tab = max([original["next_public_tab_number"], 1, *(v + 1 for v in tab_numbers), *(v + 1 for v in effective_tabs)])
        integer(next_pane); integer(next_tab)
        kept_tabs, tab_coordinates, changed = [], [], False
        for ti, tab in enumerate(workspace["tabs"]):
            removed = {pid for w, t, pid in owned if w == wi and t == ti}
            if not removed:
                kept_tabs.append(ti)
                tab_coordinates.append((ti, set(map(int, tab["panes"]))))
                continue
            changed = True
            tab["panes"] = {key: pane for key, pane in tab["panes"].items() if int(key) not in removed}
            if not tab["panes"]:
                continue
            tab["layout"] = prune_layout(tab["layout"], removed)
            if tab["layout"] is None:
                fail("off_layout_survivor_unverified")
            surviving = leaves(tab["layout"])
            for key in ("focused", "root_pane"):
                if tab.get(key) not in surviving:
                    tab[key] = surviving[0]
            kept_tabs.append(ti)
            tab_coordinates.append((ti, set(map(int, tab["panes"]))))
        if not kept_tabs:
            continue
        if changed:
            workspace["tabs"] = [workspace["tabs"][ti] for ti in kept_tabs]
            survivors = {str(int(pid)) for tab in workspace["tabs"] for pid in tab["panes"]}
            workspace["public_pane_numbers"] = {key: value for key, value in pane_numbers.items() if key in survivors}
            workspace["next_public_pane_number"] = next_pane
            workspace["public_tab_numbers"] = [effective_tabs[ti] for ti in kept_tabs]
            workspace["next_public_tab_number"] = next_tab
            workspace["active_tab"] = _selection(original["active_tab"], kept_tabs)
        kept_workspaces.append(wi)
        coordinates.append((wi, tab_coordinates))
    result["workspaces"] = [result["workspaces"][wi] for wi in kept_workspaces]
    result["active"] = _selection(snapshot["active"], kept_workspaces, nullable=True)
    result["selected"] = _selection(snapshot["selected"], kept_workspaces)
    normalized(result)
    return result, coordinates


def prune_history(history, before_snapshot, after_snapshot, coordinates):
    if (not isinstance(history, dict) or history.get("version") != 3
            or history.get("layout_fingerprint") != fingerprint(before_snapshot)
            or not isinstance(history.get("workspaces"), list)
            or len(history["workspaces"]) != len(before_snapshot["workspaces"])):
        fail("history_provenance_unverified")
    # Validate every history slot, including removed slots, before mutation.
    for wi, workspace in enumerate(history["workspaces"]):
        if not isinstance(workspace, dict) or not isinstance(workspace.get("tabs"), list):
            fail("history_shape_unverified")
        if len(workspace["tabs"]) != len(before_snapshot["workspaces"][wi]["tabs"]):
            fail("history_provenance_unverified")
        for ti, tab in enumerate(workspace["tabs"]):
            panes = _map(tab.get("panes"), lambda value: value)
            if set(panes) - {str(int(k)) for k in before_snapshot["workspaces"][wi]["tabs"][ti]["panes"]}:
                fail("history_provenance_unverified")
            for pane in panes.values():
                if not isinstance(pane, dict) or not isinstance(pane.get("ansi"), str):
                    fail("history_shape_unverified")
                integer(pane.get("lines"))
    result = deepcopy(history)
    kept = []
    for wi, tabs in coordinates:
        workspace = result["workspaces"][wi]
        survivors = []
        for ti, pane_ids in tabs:
            tab = workspace["tabs"][ti]
            tab["panes"] = {key: value for key, value in tab["panes"].items() if int(key) in pane_ids}
            survivors.append(tab)
        workspace["tabs"] = survivors
        kept.append(workspace)
    result["workspaces"] = kept
    result["layout_fingerprint"] = fingerprint(after_snapshot)
    return result
