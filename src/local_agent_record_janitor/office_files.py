"""Bounded, product-owned Office SDK and session cache closure."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import stat

from . import frozen_files
from .office_database import OfficeDatabaseError
from .office_store import MAX_ROWS

SCHEMA = "larj.office-files.v1"
_AGENT = re.compile(r"agent-([A-Za-z0-9._-]{1,128})\.(?:jsonl|meta\.json)\Z")


def _fail(code):
    raise OfficeDatabaseError("office_" + code)


def _names(root, relative, *, optional=True):
    directory = frozen_files.checked_path(root, relative)
    try:
        frozen_files._plain(directory.lstat(), directory=True)
    except FileNotFoundError:
        if optional:
            return []
        raise
    result = []
    for item in directory.iterdir():
        if len(result) >= MAX_ROWS:
            _fail("sdk_inventory_budget_exceeded")
        result.append(item.name)
    return sorted(result)


def _exists(root, relative):
    path = frozen_files.checked_path(root, relative)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    frozen_files._plain(info, directory=stat.S_ISDIR(info.st_mode))
    return True


def _buckets(root, relative):
    result = _names(root, relative)
    for name in result:
        path = frozen_files.checked_path(root, relative + "/" + name)
        frozen_files._plain(path.lstat(), directory=True)
    return result


def sanitize(identifier, limit):
    result = re.sub(r"[^a-zA-Z0-9._-]", "_", identifier)[:limit]
    return result if result and result.strip(".") else None


def discover_sdk(root, *, client, children, all_child_ids, agent_owners=None):
    sdk_ids = sorted({row["session_id"] for row in children if row["session_id"]})
    sdk_folded = {identifier.casefold() for identifier in sdk_ids}
    paths, owners, directories = set(), dict(agent_owners or {}), set()
    selected_directory_ids = set()
    def owner_key(identifier):
        return next((key for key in owners if key.casefold() == identifier.casefold()), None)
    def add(relative):
        if _exists(root, relative):
            paths.add(relative)
    # Exact filenames across every bucket also catch copies left after cwd
    # relocation. A project/bucket root is never itself a deletion target.
    projects = _buckets(root, "projects")
    for bucket in projects:
        for sid in sdk_ids:
            add(f"projects/{bucket}/{sid}.jsonl")
            relative = f"projects/{bucket}/{sid}"
            directories.add(relative)
            if _exists(root, relative):
                frozen_files._plain((root / relative).lstat(), directory=True)
                selected_directory_ids.add(tuple(frozen_files._identity((root / relative).lstat())))
                paths.add(relative)
                tree = frozen_files.freeze_remove(root, relative)
                for item in tree["files"]:
                    # Subagents own the same three fixed SDK temp locations.
                    suffix = item["path"][len(relative) + 1:]
                    match = _AGENT.fullmatch(Path(suffix).name)
                    if suffix.startswith("subagents/") and match:
                        agent = match[1]
                        existing = owner_key(agent)
                        if existing is not None and owners[existing].casefold() != sid.casefold():
                            _fail("subagent_owner_ambiguous")
                        owners[existing or agent] = sid
    # A subordinate id is not a top-level UUID. Check all unselected session
    # directories for collisions without reading transcript bodies.
    if owners:
        visited = 0
        for bucket in projects:
            for name in _names(root, "projects/" + bucket):
                relative = f"projects/{bucket}/{name}"
                session_name = name[:-6] if name.endswith(".jsonl") else name
                if owner_key(session_name) is not None and session_name.casefold() not in sdk_folded:
                    _fail("subagent_owner_ambiguous")
                if name.endswith(".jsonl"):
                    continue
                path = frozen_files.checked_path(root, relative)
                info = path.lstat()
                frozen_files._plain(info, directory=stat.S_ISDIR(info.st_mode))
                if tuple(frozen_files._identity(info)) in selected_directory_ids:
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                subagents = relative + "/subagents"
                if not _exists(root, subagents):
                    continue
                pending = [subagents]
                while pending:
                    directory = pending.pop()
                    for entry in _names(root, directory):
                        visited += 1
                        if visited > MAX_ROWS:
                            _fail("sdk_inventory_budget_exceeded")
                        child = directory + "/" + entry
                        info = frozen_files.checked_path(root, child).lstat()
                        frozen_files._plain(info, directory=stat.S_ISDIR(info.st_mode))
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(child)
                        elif (match := _AGENT.fullmatch(entry)) and owner_key(match[1]) is not None:
                            _fail("subagent_owner_ambiguous")
    for sid in sdk_ids:
        add("file-history/" + sid)
    for bucket in _buckets(root, "tmp"):
        for identifier in (*sdk_ids, *sorted(owners)):
            for relative in (f"tmp/{bucket}/{identifier}", f"tmp/{bucket}/logs/session-{identifier}.jsonl",
                             f"tmp/{bucket}/tool-outputs/session-{identifier}", f"tmp/{bucket}/images/{identifier}"):
                add(relative)
    for bucket in _buckets(root, "logs/sessions"):
        for sid in sdk_ids:
            add(f"logs/sessions/{bucket}/{sid}")
    if client == "qwenwork":
        for prefix, limit in (("shell-outputs", 120), ("sandbox-runtime/sessions", 96)):
            selected = {row["id"] for row in children}
            for cid in selected:
                name = sanitize(cid, limit)
                if name is None or any(other not in selected and (sanitize(other, limit) or "").casefold() == name.casefold() for other in all_child_ids):
                    _fail("session_cache_owner_ambiguous")
                add(prefix + "/" + name)
    for child in children:
        if value := child.get("session_memory_path"):
            if not isinstance(value, str) or not Path(value).is_absolute():
                _fail("session_memory_path_unverified")
            target = Path(value)
            if not any(target == root / directory / "session-memory/summary.md" for directory in directories
                       if directory.endswith("/" + str(child["session_id"]))):
                _fail("session_memory_path_unverified")
    if len(paths) > MAX_ROWS:
        _fail("sdk_inventory_budget_exceeded")
    # Case-insensitive volumes can expose the same object through two selected
    # spellings. Freeze it once; never repeat deletion by an alias pathname.
    unique = {}
    for relative in sorted(paths):
        info = frozen_files.checked_path(root, relative).lstat()
        unique.setdefault(tuple(frozen_files._identity(info)), relative)
    return sorted(unique.values()), owners


def discover_vault(root, children):
    scopes = {hashlib.sha256(row["id"].encode()).hexdigest() + ".json" for row in children}
    result = []
    for identity in _names(root, "data/sensitive-vault"):
        # The master key can coexist with account directories. It is shared.
        if identity == "master-key-v1.enc":
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            _fail("vault_identity_unverified")
        base = "data/sensitive-vault/" + identity
        for scope in _names(root, base, optional=False):
            if scope in scopes:
                relative = base + "/" + scope
                frozen_files._plain(frozen_files.checked_path(root, relative).lstat())
                result.append(relative)
    return sorted(result)


def freeze(root, *, client, role, children, all_child_ids, agent_owners=None):
    # Roots are explicit and are never inferred from body text or credentials.
    if role not in {"sdk", "profile"} or client not in {"qwenwork", "qoderwork"}:
        _fail("file_scope_unverified")
    if role == "sdk":
        paths, owners = discover_sdk(root, client=client, children=children, all_child_ids=all_child_ids, agent_owners=agent_owners)
    else:
        paths, owners = (discover_vault(root, children) if client == "qwenwork" else []), {}
    removals = [frozen_files.freeze_remove(root, path) for path in paths]
    if (sum(len(item["files"]) + len(item["directories"]) for item in removals) > MAX_ROWS
            or sum(file["size"] for item in removals for file in item["files"]) > frozen_files.MAX_BYTES):
        _fail("sdk_inventory_budget_exceeded")
    return {"schema_version": SCHEMA, "root": str(root), "client": client, "role": role,
        "children": children, "all_child_ids": sorted(all_child_ids), "agent_owners": owners,
        "removals": removals}


def fresh(evidence):
    if evidence.get("schema_version") != SCHEMA:
        _fail("file_scope_unverified")
    return freeze(Path(evidence["root"]), client=evidence["client"], role=evidence["role"],
                  children=evidence["children"], all_child_ids=evidence["all_child_ids"], agent_owners=evidence["agent_owners"])


def apply(evidence, *, phase_callback):
    if fresh(evidence) != evidence:
        _fail("file_closure_changed")
    if evidence["removals"]:
        phase_callback("mutation_started")
    for item in evidence["removals"]:
        frozen_files.apply_remove(Path(evidence["root"]), item)
    if remaining(evidence):
        _fail("file_targets_remain")


def remaining(evidence):
    # Re-enumeration catches new copies as well as the originally frozen paths.
    value = fresh(evidence)
    return len(value["removals"])
