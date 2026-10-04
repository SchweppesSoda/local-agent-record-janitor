"""Frozen Paseo registry, atomic-write copies and schedule run references."""
import copy
import hashlib
import json
from pathlib import Path
import re
import stat
import math

from . import frozen_files
from . import paseo_store

SCHEMA = "larj.paseo-files.v1"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
ATOMIC = re.compile(r"\.([^/]+\.json)\.([0-9]+)\.([0-9]+)\.([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})\.tmp\Z")


def fail(code):
    raise paseo_store.PaseoInventoryError("paseo_" + code)


def decode(raw):
    try:
        def integer(value):
            if value == "-0":
                fail("json_value_unverified")
            return int(value)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=paseo_store._object_pairs,
                           parse_constant=paseo_store._invalid_constant, parse_int=integer)
        count = 0
        def visit(item, depth=0):
            nonlocal count
            count += 1
            if (depth > 64 or count > 200000
                    or isinstance(item, float) and (not math.isfinite(item) or item == 0 and math.copysign(1, item) < 0)
                    or type(item) is int and abs(item) > 9007199254740991):
                fail("json_value_unverified")
            if isinstance(item, dict):
                for child in item.values():
                    visit(child, depth + 1)
            elif isinstance(item, list):
                for child in item:
                    visit(child, depth + 1)
        visit(value)
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise paseo_store.PaseoInventoryError("paseo_json_unverified") from exc


def encode(value):
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _object(value, fields, required=()):
    if not isinstance(value, dict) or set(value) - set(fields) or not set(required) <= value.keys():
        fail("schema_unverified")


def _text(value, *, nullable=False):
    if not (nullable and value is None) and not isinstance(value, str):
        fail("schema_unverified")


def _nonempty(value):
    _text(value)
    if not value.strip():
        fail("schema_unverified")


def _guid(value):
    if not isinstance(value, str) or not GUID.fullmatch(value):
        fail("schema_unverified")


def sources(root):
    root = Path(root)
    paths, directories = [], []
    for legacy in ("agent-timelines",):
        path = frozen_files.checked_path(root, legacy)
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            fail("legacy_timeline_schema_unqualified")

    def visit(relative, kind, depth):
        path = frozen_files.checked_path(root, relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            directories.append({"path": relative, "absent": True})
            return
        frozen_files._plain(info, directory=True)
        names = sorted(item.name for item in path.iterdir())
        if len(names) + len(paths) + len(directories) > 20000:
            fail("file_closure_budget_exceeded")
        directories.append({"path": relative, "identity": frozen_files._identity(info), "entries": names})
        for name in names:
            child_relative = relative + "/" + name
            child = frozen_files.checked_path(root, child_relative)
            child_info = child.lstat()
            if stat.S_ISDIR(child_info.st_mode):
                if kind != "agent" or depth:
                    fail("file_layout_unverified")
                visit(child_relative, kind, depth + 1)
                continue
            frozen_files._plain(child_info)
            atomic = ATOMIC.fullmatch(name)
            if not name.endswith(".json") and not atomic:
                fail("file_layout_unverified")
            if atomic:
                import uuid
                try:
                    uuid.UUID(atomic[4])
                except ValueError:
                    fail("atomic_file_identity_unverified")
            paths.append({"path": child_relative, "kind": kind, "atomic_base": atomic[1] if atomic else None})
        if frozen_files._identity(path.lstat()) != frozen_files._identity(info) or sorted(i.name for i in path.iterdir()) != names:
            fail("file_closure_changed")
    visit("agents", "agent", 0)
    visit("schedules", "schedule", 1)
    return paths, directories


def rows(root):
    paths, before = sources(root)
    result, total = [], 0
    for entry in paths:
        proof, raw = frozen_files.read_file(root, entry["path"], content=True, limit=4 * 1024 * 1024)
        total += len(raw)
        if total > 64 * 1024 * 1024:
            fail("file_closure_budget_exceeded")
        value = decode(raw)
        row = _agent(value) if entry["kind"] == "agent" else _schedule(value)
        if not ID.fullmatch(row["id"]) or entry["atomic_base"] is not None and entry["atomic_base"] != row["id"] + ".json":
            fail("file_identity_unverified")
        result.append({**entry, "before": proof, "value": value, "row": row, "raw": raw})
    if sources(root) != (paths, before):
        fail("file_closure_changed")
    return result, before


def _agent(value):
    # Inventory accepts opaque provider state; mutation additionally qualifies
    # every known restoration-bearing container against the pinned schema.
    row = paseo_store._project(value)
    for key in ("workspaceId", "lastActivityAt"):
        if key in value:
            _text(value[key])
    for key in ("lastUserMessageAt", "title", "lastModeId", "lastError", "attentionTimestamp", "archivedAt"):
        if key in value:
            _text(value[key], nullable=True)
    for key in ("requiresAttention", "internal"):
        if key in value and type(value[key]) is not bool:
            fail("agent_schema_unverified")
    if value.get("attentionReason") not in (None, "finished", "error", "permission"):
        fail("agent_schema_unverified")
    if any(not isinstance(v, str) for v in value.get("labels", {}).values()):
        fail("agent_schema_unverified")
    if "owner" in value:
        _object(value["owner"], {"kind", "daemonId", "executionId"}, {"kind", "daemonId", "executionId"})
    persistence = value.get("persistence")
    if persistence is not None:
        _object(persistence, {"provider", "sessionId", "nativeHandle", "metadata"}, {"provider", "sessionId"})
        if "metadata" in persistence and not isinstance(persistence["metadata"], dict):
            fail("agent_schema_unverified")
    if "runtimeInfo" in value:
        runtime = value["runtimeInfo"]
        _object(runtime, {"provider", "sessionId", "model", "thinkingOptionId", "modeId", "extra"}, {"provider", "sessionId"})
        for key in ("model", "thinkingOptionId", "modeId"):
            if key in runtime:
                _text(runtime[key], nullable=True)
        if "extra" in runtime and not isinstance(runtime["extra"], dict):
            fail("agent_schema_unverified")
    config = value.get("config")
    if config is not None:
        _object(config, {"modeId", "model", "thinkingOptionId", "featureValues", "providerOptions", "toolPolicy", "systemPrompt", "mcpServers"})
        for key in ("modeId", "model", "thinkingOptionId", "systemPrompt"):
            if key in config:
                _text(config[key], nullable=True)
        for key in ("featureValues", "providerOptions", "mcpServers"):
            if config.get(key) is not None and not isinstance(config[key], dict):
                fail("agent_schema_unverified")
        policy = config.get("toolPolicy")
        if policy is not None:
            _object(policy, {"preapproved"}, {"preapproved"})
            if not isinstance(policy["preapproved"], list):
                fail("agent_schema_unverified")
            for tool in policy["preapproved"]:
                _object(tool, {"kind", "server", "tool"}, {"kind", "server", "tool"})
                if tool["kind"] != "mcp":
                    fail("agent_schema_unverified")
                _text(tool["server"])
                _text(tool["tool"])
    if "features" in value:
        if not isinstance(value["features"], list):
            fail("agent_schema_unverified")
        for feature in value["features"]:
            _object(feature, {"type", "id", "label", "description", "tooltip", "icon", "value", "desktopTrigger", "options"}, {"type", "id", "label", "value"})
            for key in ("id", "label", "description", "tooltip", "icon"):
                if key in feature:
                    _text(feature[key])
            if feature["type"] == "toggle":
                if type(feature["value"]) is not bool or {"desktopTrigger", "options"} & feature.keys():
                    fail("agent_schema_unverified")
            elif feature["type"] == "select":
                _text(feature["value"], nullable=True)
                if "desktopTrigger" in feature and feature["desktopTrigger"] not in ("icon", "label"):
                    fail("agent_schema_unverified")
                if not isinstance(feature.get("options"), list):
                    fail("agent_schema_unverified")
                for option in feature["options"]:
                    _object(option, {"id", "label", "description", "isDefault", "metadata"}, {"id", "label"})
                    for key in ("id", "label", "description"):
                        if key in option:
                            _text(option[key])
                    if "isDefault" in option and type(option["isDefault"]) is not bool:
                        fail("agent_schema_unverified")
                    if "metadata" in option and not isinstance(option["metadata"], dict):
                        fail("agent_schema_unverified")
            else:
                fail("agent_schema_unverified")
    return row


def _schedule(value):
    fields = {"id", "name", "prompt", "cadence", "target", "status", "createdAt", "updatedAt", "nextRunAt",
              "lastRunAt", "pausedAt", "expiresAt", "maxRuns", "runs"}
    _object(value, fields, fields)
    for key in ("id", "prompt", "createdAt", "updatedAt"):
        _text(value[key])
    if not value["prompt"] or value["status"] not in {"active", "paused", "completed"}:
        fail("schedule_schema_unverified")
    for key in ("name", "nextRunAt", "lastRunAt", "pausedAt", "expiresAt"):
        _text(value[key], nullable=True)
    if value["maxRuns"] is not None and (type(value["maxRuns"]) is not int or value["maxRuns"] < 1):
        fail("schedule_schema_unverified")
    cadence = value["cadence"]
    _object(cadence, {"type", "everyMs", "expression", "timezone"}, {"type"})
    if cadence["type"] == "every":
        if set(cadence) != {"type", "everyMs"} or type(cadence["everyMs"]) is not int or cadence["everyMs"] < 1:
            fail("schedule_schema_unverified")
    elif cadence["type"] == "cron":
        _object(cadence, {"type", "expression", "timezone"}, {"type", "expression"})
        if not isinstance(cadence["expression"], str) or not cadence["expression"].strip():
            fail("schedule_schema_unverified")
        if "timezone" in cadence:
            _nonempty(cadence["timezone"])
    else:
        fail("schedule_schema_unverified")
    target = value["target"]
    _object(target, {"type", "agentId", "config"}, {"type"})
    if target["type"] == "agent":
        if set(target) != {"type", "agentId"} or not isinstance(target["agentId"], str) or not ID.fullmatch(target["agentId"]):
            fail("schedule_schema_unverified")
        _guid(target["agentId"])
    elif target["type"] == "new-agent":
        _object(target, {"type", "config"}, {"type", "config"})
        _object(target["config"], {"provider", "cwd", "modeId", "model", "thinkingOptionId", "archiveOnFinish",
            "isolation", "title", "providerOptions", "featureValues", "systemPrompt", "mcpServers"}, {"provider", "cwd"})
        config = target["config"]
        _text(config["provider"])
        _nonempty(config["cwd"])
        for key in ("modeId", "model", "thinkingOptionId", "title"):
            if key in config and not (key == "title" and config[key] is None):
                _nonempty(config[key])
        if "archiveOnFinish" in config and type(config["archiveOnFinish"]) is not bool:
            fail("schedule_schema_unverified")
        if "isolation" in config and config["isolation"] not in ("local", "worktree"):
            fail("schedule_schema_unverified")
        if "systemPrompt" in config:
            _text(config["systemPrompt"])
        for key in ("providerOptions", "featureValues", "mcpServers"):
            if key in config and not isinstance(config[key], dict):
                fail("schedule_schema_unverified")
    else:
        fail("schedule_schema_unverified")
    if not isinstance(value["runs"], list) or len(value["runs"]) > 20000:
        fail("schedule_schema_unverified")
    seen = set()
    for run in value["runs"]:
        fields = {"id", "scheduledFor", "startedAt", "endedAt", "status", "agentId", "workspaceId", "output", "error"}
        _object(run, fields, fields - {"workspaceId"})
        for key in ("id", "scheduledFor", "startedAt"):
            _text(run[key])
        if run["id"] in seen or run["status"] not in {"running", "succeeded", "failed"}:
            fail("schedule_schema_unverified")
        seen.add(run["id"])
        for key in ("endedAt", "agentId", "output", "error", "workspaceId"):
            _text(run.get(key), nullable=True)
        if run["agentId"] is not None:
            _guid(run["agentId"])
    return {"id": value["id"]}


def transform(entry, ids):
    value = entry["value"]
    if entry["kind"] == "agent":
        return None if value["id"] in ids else entry["raw"]
    owned = value["target"]["type"] == "agent" and value["target"]["agentId"] in ids
    if any(run["status"] == "running" and (owned or run["agentId"] in ids) for run in value["runs"]):
        fail("schedule_run_not_finished")
    if owned:
        if any(run["agentId"] is not None and run["agentId"] not in ids for run in value["runs"]):
            fail("schedule_has_unselected_runs")
        return None
    after = copy.deepcopy(value)
    changed = False
    for run in after["runs"]:
        if run["agentId"] in ids:
            run.update(agentId=None, output=None, error=None)
            changed = True
    return encode(after) if changed else entry["raw"]


def freeze(root, ids):
    ids = sorted(set(ids))
    entries, directories = rows(root)
    if not ids or not set(ids) <= {e["row"]["id"] for e in entries if e["kind"] == "agent"}:
        fail("selected_agent_not_found")
    if any(e["kind"] == "agent" and e["row"]["id"] not in ids and e["row"]["parent_agent_id"] in ids for e in entries):
        fail("unselected_child_agent_remains")
    files = []
    for entry in entries:
        body = transform(entry, set(ids))
        files.append({"before": entry["before"], "kind": entry["kind"], "id": entry["row"]["id"],
            "after_sha256": hashlib.sha256(body).hexdigest() if body is not None else None,
            "after_size": len(body) if body is not None else None})
    return {"schema_version": SCHEMA, "root": str(root), "agent_ids": ids, "files": files, "directories": directories}


def validate(evidence):
    if (evidence.get("schema_version") != SCHEMA or not evidence.get("agent_ids")
            or evidence["agent_ids"] != sorted(set(evidence["agent_ids"]))):
        fail("file_evidence_invalid")
    root = Path(evidence["root"])
    for item in evidence["files"]:
        frozen_files.checked_path(root, item["before"]["path"])
    return root


def remaining(evidence):
    root = validate(evidence)
    paths, directories = sources(root)
    expected_paths = {item["before"]["path"]: item for item in evidence["files"]}
    current_paths = {item["path"] for item in paths}
    if current_paths - expected_paths.keys():
        fail("file_closure_changed")
    missing = expected_paths.keys() - current_paths
    if any(expected_paths[path]["after_sha256"] is not None for path in missing):
        fail("unselected_file_missing")
    expected_dirs = copy.deepcopy(evidence["directories"])
    for directory in expected_dirs:
        if not directory.get("absent"):
            directory["entries"] = [name for name in directory["entries"] if directory["path"] + "/" + name not in missing]
    if directories != expected_dirs:
        fail("file_closure_changed")
    count = 0
    for relative in current_paths:
        item = expected_paths[relative]
        current, _ = frozen_files.read_file(root, relative)
        if current == item["before"]:
            count += item["after_sha256"] != item["before"]["sha256"]
        elif (item["after_sha256"] == item["before"]["sha256"] or item["after_sha256"] is None
              or (current["sha256"], current["size"]) != (item["after_sha256"], item["after_size"])):
            fail("file_state_unknown")
    return count


def apply(evidence, *, phase_callback):
    root = validate(evidence)
    remaining(evidence)
    current, _ = rows(root)
    expected = {item["before"]["path"]: item for item in evidence["files"]}
    changes = []
    for entry in current:
        item = expected[entry["path"]]
        if entry["before"] != item["before"]:
            if (item["after_sha256"] is not None and
                    (entry["before"]["sha256"], entry["before"]["size"]) == (item["after_sha256"], item["after_size"])):
                continue
            fail("file_state_unknown")
        body = transform(entry, set(evidence["agent_ids"]))
        if (hashlib.sha256(body).hexdigest() if body is not None else None) != item["after_sha256"]:
            fail("file_transform_changed")
        if body is None or item["after_sha256"] != item["before"]["sha256"]:
            changes.append((entry, item, body))
    remaining(evidence)
    if changes:
        phase_callback("mutation_started")
    for entry, item, body in changes:
        if body is None:
            frozen_files._delete_frozen(root, item["before"])
        else:
            change = {"kind": "rewrite", "before": item["before"], "after_sha256": item["after_sha256"], "after_size": item["after_size"]}
            frozen_files.apply_rewrite(root, change, lambda _raw, data=body: data)
    if remaining(evidence):
        fail("files_after_unverified")
