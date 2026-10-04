"""Exact current, recovery and terminal-history closure for Herdr schema 3.

Native authority is supplied as rooted session bindings by the coordinator.
The before/after manifest contains hashes and locators, never terminal bodies.
"""
from pathlib import Path
import hashlib
import re

from . import frozen_files as files, herdr_cleanup_json as codec
from .herdr_discovery import bounded_entries, valid_session_name, require_plain_directory
from .record_identity import canonical_path

SCHEMA = "larj.herdr-files.v1"
RECOVERY = re.compile(r"session-([0-9]{39})-([0-9]+)-([0-9]+)\.(json|pending)\Z", re.ASCII)
FIXED = ("session.json", "session.json.tmp", "session-history.json", "session-history.json.tmp")


def sources(root):
    """Record absence and directory membership; retention counts are not bounds."""
    root = Path(root)
    require_plain_directory(root)
    paths, observations = [], []

    def directory(relative, *, sessions=False):
        path = files.checked_path(root, relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            observations.append({"path": relative, "absent": True})
            return ()
        files._plain(info, directory=True)
        entries = bounded_entries(path, maximum=4096)
        observations.append({"path": relative, "identity": files._identity(info),
                             "entries": [p.name for p in entries]})
        for entry in entries:
            if sessions:
                if not valid_session_name(entry.name):
                    codec.fail("session_directory_unverified")
                require_plain_directory(entry)
            else:
                match = RECOVERY.fullmatch(entry.name)
                if match is None or int(match[1]) > 2**128 - 1:
                    codec.fail("recovery_name_unverified")
                files._plain(entry.lstat())
        return entries

    sessions = [("default", ""), *((p.name, "sessions/" + p.name + "/")
                for p in directory("sessions", sessions=True))]
    for name, prefix in sessions:
        for filename in FIXED:
            relative = prefix + filename
            try:
                info = files.checked_path(root, relative).lstat()
            except FileNotFoundError:
                observations.append({"path": relative, "absent": True})
                continue
            files._plain(info)
            paths.append((relative, name, "history" if filename.startswith("session-history") else "snapshot"))
        for dirname in ("session-snapshots", "session-backups"):
            for path in directory(prefix + dirname):
                paths.append((path.relative_to(root).as_posix(), name, "snapshot"))
    if len(paths) > 4096:
        codec.fail("source_budget_exceeded")
    return sorted(paths), observations


def binding_key(item):
    return (item["session"], item["engine"], item["kind"], item["value"])


def validate_bindings(bindings):
    seen = set()
    for item in bindings:
        if (not isinstance(item, dict) or set(item) != {"session", "engine", "kind", "value", "native_root"}
                or item["session"] != "default" and not valid_session_name(item["session"])
                or item["engine"] not in {"codex", "claude", "pi"}
                or item["kind"] not in {"id", "path"}
                or not isinstance(item["value"], str) or not item["value"]
                or canonical_path(item["native_root"]) != item["native_root"]):
            codec.fail("native_binding_invalid")
        key = binding_key(item)
        if key in seen:
            codec.fail("native_binding_ambiguous")
        seen.add(key)
    if not seen:
        codec.fail("native_binding_missing")


def _owned(snapshot, session, bindings):
    keys = {binding_key(item) for item in bindings}
    owned, matched = set(), set()
    for wi, workspace in enumerate(snapshot["workspaces"]):
        for ti, tab in enumerate(workspace["tabs"]):
            for pid, pane in tab["panes"].items():
                handle = pane.get("agent_session")
                if not isinstance(handle, dict):
                    continue
                key = (session, handle.get("agent"), handle.get("kind"), handle.get("value"))
                if key in keys:
                    if handle.get("source") != "herdr:" + handle["agent"]:
                        codec.fail("native_handle_source_unverified")
                    owned.add((wi, ti, int(pid)))
                    matched.add(key)
    return owned, matched


def materialize(root, bindings, *, reader=None):
    """Return a deterministic approval and transient replacement bytes."""
    root, bindings = Path(root), sorted(bindings, key=binding_key)
    validate_bindings(bindings)
    read = reader or files.read_file
    paths, observations = sources(root)
    states, versions, bodies, found, total = [], {}, {}, set(), 0
    history = []
    for relative, session, role in paths:
        evidence, raw = read(root, relative, content=True, limit=16 * 1024 * 1024)
        total += len(raw)
        if total > 128 * 1024 * 1024:
            codec.fail("source_budget_exceeded")
        value = codec.decode(raw)
        if role == "history":
            history.append((relative, session, evidence, raw, value))
            continue
        codec.normalized(value)
        owned, matched = _owned(value, session, bindings)
        found.update(matched)
        after, coordinates = codec.prune_snapshot(value, owned)
        identity = (session, codec.fingerprint(value))
        candidate = (value, after, coordinates)
        # Rust omits unknown properties from its hash. Equal fingerprints must
        # nevertheless identify the same ownership and transformed layout.
        previous = versions.get(identity)
        if previous and (previous[2] != coordinates or codec.fingerprint(previous[1]) != codec.fingerprint(after)):
            codec.fail("history_provenance_ambiguous")
        versions[identity] = candidate
        replacement = codec.encode(after) if owned else raw
        bodies[relative] = replacement
        states.append({"session": session, "role": role, "before": evidence,
            "after_sha256": hashlib.sha256(replacement).hexdigest(), "after_size": len(replacement),
            "owned": [list(p) for p in sorted(owned)]})
    for relative, session, evidence, raw, value in history:
        version = versions.get((session, value.get("layout_fingerprint") if isinstance(value, dict) else None))
        if version is None:
            codec.fail("history_provenance_unverified")
        before, after, coordinates = version
        transformed = codec.prune_history(value, before, after, coordinates)
        replacement = raw if transformed == value else codec.encode(transformed)
        bodies[relative] = replacement
        states.append({"session": session, "role": "history", "before": evidence,
            "after_sha256": hashlib.sha256(replacement).hexdigest(), "after_size": len(replacement)})
    if found != {binding_key(item) for item in bindings}:
        codec.fail("selected_binding_not_observed")
    if sources(root) != (paths, observations):
        codec.fail("source_membership_changed")
    return {"schema_version": SCHEMA, "root": str(root), "bindings": bindings,
            "observations": observations, "files": sorted(states, key=lambda s: s["before"]["path"])}, bodies


def freeze(root, bindings):
    return materialize(root, bindings)[0]


def validate(evidence):
    if (not isinstance(evidence, dict) or evidence.get("schema_version") != SCHEMA
            or not isinstance(evidence.get("files"), list) or not evidence["files"]):
        codec.fail("file_evidence_invalid")
    validate_bindings(evidence["bindings"])
    root = Path(evidence["root"])
    for item in evidence["files"]:
        files.checked_path(root, item["before"]["path"])
    paths, observations = sources(root)
    expected = sorted((s["before"]["path"], s["session"], s["role"]) for s in evidence["files"])
    if paths != expected or observations != evidence["observations"]:
        codec.fail("source_membership_changed")


def remaining(evidence, *, reader=None):
    """Strict after proof, with exact before only counted as a pending change."""
    validate(evidence)
    root, pending = Path(evidence["root"]), 0
    for item in evidence["files"]:
        current, _ = (reader or files.read_file)(root, item["before"]["path"])
        if current["sha256"] == item["after_sha256"] and current["size"] == item["after_size"]:
            continue
        if current != item["before"]:
            codec.fail("file_state_unknown")
        pending += 1
    return pending


def replacements(evidence, *, reader=None):
    current, bodies = materialize(Path(evidence["root"]), evidence["bindings"], reader=reader)
    if current != evidence:
        codec.fail("file_approval_changed")
    return {s["before"]["path"]: bodies[s["before"]["path"]] for s in evidence["files"]
            if s["before"]["sha256"] != s["after_sha256"]}
