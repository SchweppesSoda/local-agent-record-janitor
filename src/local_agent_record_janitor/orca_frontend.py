"""Frozen local structured-session closure, independent of native deletion.

This internal writer must be called under the coordinator's approved native
dependencies, profile root lock, durable checkpoint and product exclusion.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import re
import time

from . import frozen_files, frozen_sqlite, orca_journal_cleanup as journal, orca_profile_cleanup as profile
from .orca_frontend_json import transform as json_transform, mapping
from .orca_journal_cleanup import decode, fail, rows, text
from .orca_metadata import parse_orca_record
from .orca_discovery import prove_account_home
from .record_identity import canonical_path
from .sqlite_utils import connect_readonly

SCHEMA = "larj.orca-frontend.v1"
_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_DB_BACKUP = re.compile(r"profile-state\.db\.backup\.[1-9][0-9]*-" + _UUID4 + r"\.db\Z", re.I)
_JSON_BACKUP = re.compile(r"orca-data\.json\.(?:bak\.[0-4]|sqlite-export\.[1-9][0-9]*\.json)\Z")


def _exists(root, relative):
    path = frozen_files.checked_path(root, relative)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    frozen_files._plain(info, directory=path.is_dir())
    return True


def _names(root, relative=None):
    if relative and not _exists(root, relative):
        return []
    directory = root if relative is None else frozen_files.checked_path(root, relative)
    frozen_files._plain(directory.lstat(), directory=True)
    values = []
    for entry in directory.iterdir():
        values.append(entry.name)
        if len(values) > 20000:
            fail("frontend_path_budget_exceeded")
    return sorted(values)


def sources(root):
    json_files, databases = [], []
    locations = [""]
    for name in _names(root, "profiles"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", name):
            fail("profile_directory_identity_unverified")
        relative = "profiles/" + name
        frozen_files._plain(frozen_files.checked_path(root, relative).lstat(), directory=True)
        locations.append(relative + "/")
    for prefix in locations:
        for name in _names(root, prefix.rstrip("/") or None):
            relative = prefix + name
            if name == "orca-data.json" or _JSON_BACKUP.fullmatch(name):
                json_files.append((relative, "profile_json"))
            elif name == "profile-state.db" or _DB_BACKUP.fullmatch(name):
                if not prefix:
                    fail("root_profile_database_layout_unverified")
                databases.append(relative)
            elif any(name.endswith(suffix) and _DB_BACKUP.fullmatch(name[:-len(suffix)])
                     for suffix in ("-wal", "-shm", "-journal")):
                frozen_files._plain(frozen_files.checked_path(root, relative).lstat())
            elif name.startswith("profile-state-corrupt-"):
                fail("quarantined_profile_restore_unverified")
            elif name.startswith(("orca-data.json.", "profile-state.db.")):
                fail("profile_restore_filename_unverified")
    for name in _names(root, "agent-sessions"):
        if name not in {"agent-sessions.json", "agent-sessions.json.bak"}:
            fail("legacy_records_restore_filename_unverified")
        json_files.append(("agent-sessions/" + name, "legacy_records"))
    return sorted(json_files), sorted(databases)


def _indexes(root):
    result = []
    for relative in ("orca-profile-index.json", "orca-profile-index.json.bak", "orca-profile-index.json.tmp"):
        if not _exists(root, relative):
            result.append({"path": relative, "absent": True})
            continue
        if relative.endswith(".tmp"):
            fail("profile_index_write_incomplete")
        observation, raw = frozen_files.read_file(root, relative, content=True)
        value = mapping(decode(raw))
        if value.get("schemaVersion") != 1 or set(value) != {"schemaVersion", "activeProfileId", "profiles"}:
            fail("profile_index_shape_unverified")
        profiles = value["profiles"]
        if not isinstance(profiles, list) or not profiles:
            fail("profile_index_shape_unverified")
        ids = []
        for entry in profiles:
            entry = mapping(entry)
            identifier = entry.get("id")
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identifier):
                fail("profile_index_identity_unverified")
            ids.append(identifier)
        if len(set(ids)) != len(ids) or value["activeProfileId"] not in ids:
            fail("profile_index_identity_unverified")
        result.append(observation)
    return result


def _record_metadata(root, sid, raw):
    value = mapping(raw)
    record = parse_orca_record(sid, value)
    if (record.host != "local" or record.wsl_distro is not None or record.provider != "codex"
            or record.home_variable != "CODEX_HOME" or record.unsupported_recovery):
        fail("selected_record_recovery_unverified")
    home = prove_account_home(root, Path(record.home_locator))
    workspace = mapping(value.get("location")).get("workspaceId")
    if not text(workspace):
        fail("selected_workspace_identity_unverified")
    return {"session_id": sid, "workspace_id": workspace, "home": str(home),
            "native_ids": sorted({handle.native_id for handle in record.handles})}


def _metadata(root, selected, json_sources):
    records, tabs = [], {}
    def add_tab(tab, sid):
        tabs.setdefault(tab, set()).add(sid)
    with closing(connect_readonly(frozen_sqlite.path_for(root, journal.RELATIVE))) as db:
        db.execute("BEGIN")
        journal.qualify(db)
        for sid, raw in rows(db, "SELECT session_id,record_json FROM agent_session_records"):
            value = decode(raw)
            if sid in selected:
                records.append(_record_metadata(root, sid, value))
            else:
                journal.record_references(value, selected)
        for tab, sid in rows(db, "SELECT tab_id,session_id FROM agent_session_tabs"):
            if sid in selected:
                add_tab(tab, sid)
    for relative, family in json_sources:
        if family != "legacy_records":
            continue
        _, raw = frozen_files.read_file(root, relative, content=True)
        value = mapping(decode(raw))
        # Validate the entire known container, including anti-replay ledger,
        # before treating a missing selected entry as trustworthy absence.
        json_transform(raw, family=family, selected=selected)
        for key in ("records", "unusableRecords"):
            for sid, item in mapping(value[key]).items():
                record = mapping(item).get("raw") if key == "unusableRecords" else item
                if sid in selected:
                    records.append(_record_metadata(root, sid, record))
                else:
                    journal.record_references(record, selected)
        for item in value.get("sessionTabs", ()):
            if item["sessionId"] in selected:
                add_tab(item["tabId"], item["sessionId"])
    # Stable source order is not an ownership rule; independently qualify every
    # historical copy and retain the union of its complete native handle chain.
    unique = {(r["session_id"], r["workspace_id"], r["home"], tuple(r["native_ids"])): r for r in records}
    return [unique[key] for key in sorted(unique)], {key: sorted(tabs[key]) for key in sorted(tabs)}


def _legacy_journals(root, records):
    targets = {}
    for record in records:
        prefix = "agent-session-journal/" + hashlib.sha256(record["workspace_id"].encode()).hexdigest()[:32]
        relative = prefix + "/" + hashlib.sha256(record["session_id"].encode()).hexdigest()[:32]
        targets[relative] = record["session_id"]
    manifests = []
    for relative, sid in sorted(targets.items()):
        if not _exists(root, relative):
            continue
        allowed = {"journal.db", "journal.db-wal", "journal.db-shm", "journal.db-journal", "log.jsonl", "snapshot.json"}
        if set(_names(root, relative)) - allowed:
            fail("per_session_journal_members_unverified")
        if _exists(root, relative + "/journal.db"):
            with closing(connect_readonly(frozen_sqlite.path_for(root, relative + "/journal.db"))) as db:
                db.execute("BEGIN")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                definitions = {
                    "journal_rows": "CREATE TABLE journal_rows(session_id TEXT NOT NULL,epoch TEXT NOT NULL,seq INTEGER NOT NULL,ts INTEGER NOT NULL,row_json TEXT NOT NULL,PRIMARY KEY(session_id,epoch,seq))",
                    "journal_sessions": "CREATE TABLE journal_sessions(session_id TEXT PRIMARY KEY,epoch TEXT NOT NULL,updated_at INTEGER NOT NULL)",
                    "journal_repairs": "CREATE TABLE journal_repairs(session_id TEXT PRIMARY KEY,epoch TEXT NOT NULL,content_from INTEGER NOT NULL,repaired_at INTEGER NOT NULL)",
                }
                expected = {0: set(), 1: {"journal_rows", "journal_sessions"}, 2: set(definitions)}.get(version)
                objects = rows(db, "SELECT type,name,tbl_name,sql FROM sqlite_master")
                tables = {name for kind, name, _, _ in objects if kind == "table"}
                if expected is None or tables != expected:
                    fail("per_session_journal_schema_unverified")
                for kind, name, table, sql in objects:
                    if kind == "index" and name.startswith("sqlite_autoindex_") and table in expected and sql is None:
                        continue
                    if kind != "table" or name not in expected or journal._sql(sql) != journal._sql(definitions[name]):
                        fail("per_session_journal_schema_unverified")
                for table in expected:
                    if db.execute(f"SELECT 1 FROM {table} WHERE session_id IS NOT ? LIMIT 1", (sid,)).fetchone():
                        fail("per_session_journal_owner_unverified")
        manifests.append(frozen_files.freeze_remove(root, relative))
    return manifests


def _hooks(root, native_ids, known_tabs=()):
    # Structured statuses are deliberately never persisted by the pinned host.
    # Qualify the actual terminal status/spool identities so a related TUI copy
    # cannot disappear behind that product-level distinction.
    result, directories = [], ["agent-hooks"]
    namespace = re.compile(r"com\.stablyai\.orca(?:\.dev\.[0-9a-f]{10})?\Z")
    temporary = re.compile(r"\.(endpoint|last-status)-[1-9][0-9]*-" + _UUID4 + r"\.tmp\Z", re.I)
    def inspect_identity(item, key=None):
        item = mapping(item)
        pane = item.get("paneKey", key)
        if not text(pane) or key is not None and item.get("paneKey", key) != key:
            fail("hook_identity_unverified")
        tab = item.get("tabId")
        if tab is not None and not text(tab):
            fail("hook_identity_unverified")
        if tab in known_tabs or pane.split(":", 1)[0] in known_tabs:
            fail("terminal_hook_binding_unverified")
        provider = item.get("providerSession")
        if provider is not None:
            provider = mapping(provider)
            if not text(provider.get("id")):
                fail("hook_identity_unverified")
            if provider["id"] in native_ids:
                fail("terminal_hook_binding_unverified")
        return item
    for directory in directories:
        present = _exists(root, directory)
        members = _names(root, directory)
        result.append({"path": directory, "absent": not present, "members": members})
        if not present:
            continue
        for name in members:
            relative = directory + "/" + name
            if directory == "agent-hooks" and namespace.fullmatch(name):
                frozen_files._plain(frozen_files.checked_path(root, relative).lstat(), directory=True)
                directories.append(relative)
                continue
            if name == "spool":
                spool_members = _names(root, relative)
                result.append({"path": relative, "members": spool_members})
                for filename in spool_members:
                    if not filename.endswith(".jsonl"):
                        fail("hook_spool_schema_unverified")
                    observed, raw = frozen_files.read_file(root, relative + "/" + filename, content=True)
                    if raw and not raw.endswith(b"\n"):
                        fail("hook_spool_incomplete")
                    for line in raw.splitlines():
                        if not line.strip():
                            continue
                        item = inspect_identity(decode(line))
                        if not text(item.get("source")) or not journal.integer(item.get("receivedAt")):
                            fail("hook_spool_schema_unverified")
                        payload = mapping(item.get("payload"))
                        if item["source"] == "codex" and payload.get("session_id") in native_ids:
                            fail("terminal_hook_binding_unverified")
                    result.append(observed)
                continue
            temp = temporary.fullmatch(name)
            is_status = name == "last-status.json" or temp is not None and temp[1] == "last-status"
            is_endpoint = name in {"endpoint.env", "endpoint.cmd"} or temp is not None and temp[1] == "endpoint"
            if not is_status and not is_endpoint:
                fail("hook_namespace_members_unverified")
            observed, raw = frozen_files.read_file(root, relative, content=True)
            if is_status:
                value = mapping(decode(raw))
                if value.get("version") != 2 or set(value) - {"version", "entries", "authorityCommitments"}:
                    fail("hook_status_schema_unverified")
                for key, item in mapping(value.get("entries")).items():
                    inspect_identity(item, key)
                for key, item in mapping(value.get("authorityCommitments", {})).items():
                    inspect_identity(item, key)
            else:
                values = {}
                for line in raw.decode("utf-8").splitlines():
                    line = re.sub(r"^set\s+", "", line.strip(), flags=re.I)
                    if not line:
                        continue
                    key, separator, value = line.partition("=")
                    if not separator or key in values or not value:
                        fail("hook_endpoint_schema_unverified")
                    values[key] = value
                required = {"ORCA_AGENT_HOOK_" + key for key in ("PORT", "TOKEN", "ENV", "VERSION")}
                if (set(values) not in (required, required | {"ORCA_AGENT_HOOK_TRANSPORT"})
                        or values.get("ORCA_AGENT_HOOK_TRANSPORT", "raw-json-v1") != "raw-json-v1"):
                    fail("hook_endpoint_schema_unverified")
            result.append(observed)
    return sorted(result, key=lambda item: item["path"])


def _runtime_metadata(root):
    result = []
    for relative in ("orca-runtime.json", "orcad.lock"):
        if not _exists(root, relative):
            result.append({"path": relative, "absent": True})
            continue
        observed, raw = frozen_files.read_file(root, relative, content=True)
        value = mapping(decode(raw))
        if not journal.integer(value.get("pid"), 1):
            fail("runtime_metadata_unverified")
        if relative == "orca-runtime.json":
            if (set(value) - {"runtimeId", "pid", "transports", "transport", "authToken", "startedAt"}
                    or not text(value.get("runtimeId")) or not journal.integer(value.get("startedAt"), 1)
                    or value.get("authToken") is not None and not text(value["authToken"])):
                fail("runtime_metadata_unverified")
            transports = value.get("transports", [value.get("transport")])
            if not isinstance(transports, list) or not transports:
                fail("runtime_metadata_unverified")
            for transport in transports:
                transport = mapping(transport)
                if (set(transport) != {"kind", "endpoint"} or transport["kind"] not in {"unix", "named-pipe", "websocket"}
                        or not text(transport["endpoint"])):
                    fail("runtime_metadata_unverified")
        elif (set(value) != {"pid", "startedAtMs", "identity", "version", "acquiredAt", "nonce"}
                or any(not text(value[key]) for key in ("identity", "version", "nonce"))
                or not text(value["acquiredAt"])
                or value["startedAtMs"] is not None and not journal.integer(value["startedAtMs"], 1)):
            fail("runtime_metadata_unverified")
        result.append({**observed, "pid": value["pid"]})
    return result


def freeze(root, selected, *, timestamp=None, frozen=None):
    root = Path(root)
    selected = sorted(set(selected))
    if not selected or any(not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", sid) for sid in selected):
        fail("frontend_selection_invalid")
    json_sources, profile_dbs = sources(root)
    records, tabs = _metadata(root, set(selected), json_sources)
    if frozen is None and set(selected) - {record["session_id"] for record in records}:
        fail("selected_record_not_found")
    if frozen is not None:
        records, tabs = frozen["records"], frozen["tab_ids"]
    native_ids = {sid for record in records for sid in record["native_ids"]}
    timestamp = int(time.time() * 1000) if timestamp is None else timestamp
    json_files, json_hashes = [], {}
    for relative, family in json_sources:
        def transform(raw):
            return json_transform(raw, family=family, selected=set(selected), known_tabs=tabs, native_ids=native_ids)
        item = frozen_files.freeze_rewrite(root, relative, transform)
        json_files.append({"family": family, "evidence": item})
        if family == "profile_json":
            json_hashes[item["before"]["sha256"]] = item["after_sha256"]
    evidence = {"schema_version": SCHEMA, "root": str(root), "session_ids": selected,
        "indexes": _indexes(root),
        "records": records, "tab_ids": tabs, "timestamp": timestamp,
        "journal": journal.freeze(root, set(selected)), "json_files": json_files, "json_hashes": json_hashes,
        "profile_databases": [profile.freeze(root, relative, set(selected), tabs, native_ids,
            timestamp=timestamp, json_hashes=json_hashes) for relative in profile_dbs],
        "removals": _legacy_journals(root, records), "hook_observations": _hooks(root, native_ids, tabs),
        "runtime_observations": _runtime_metadata(root)}
    from .adapters.orca import OrcaAdapter
    from .orca_cleanup import covers_error
    errors = OrcaAdapter(profile_root=root).snapshot_references(refresh=True).errors
    evidence["covered_errors"] = [error.to_dict() for error in errors if covers_error(evidence, error)]
    return evidence


def validate(evidence):
    if (not isinstance(evidence, dict) or evidence.get("schema_version") != SCHEMA
            or not evidence.get("session_ids") or not isinstance(evidence.get("records"), list)
            or not isinstance(evidence.get("covered_errors"), list)):
        fail("frontend_evidence_invalid")
    root = Path(evidence["root"])
    frozen_files._plain(root.lstat(), directory=True)
    if Path(evidence["journal"]["root"]) != root:
        fail("frontend_evidence_invalid")
    return root


def _arguments(evidence):
    return {"selected": set(evidence["session_ids"]), "known_tabs": evidence["tab_ids"],
            "native_ids": {sid for record in evidence["records"] for sid in record["native_ids"]}}


def apply(evidence, *, phase_callback, require_closed):
    """Execute one frozen frontend child after its native children completed."""
    root = validate(evidence)
    require_closed()
    current = freeze(root, evidence["session_ids"], timestamp=evidence["timestamp"])
    if current != evidence:
        fail("frontend_evidence_changed")
    arguments = _arguments(evidence)
    def checkpoint(phase):
        require_closed()
        phase_callback(phase)
    # All restoration copies are covered before the authoritative journal is
    # removed. A crash after any checkpoint stays unknown, never retryable.
    for item in evidence["json_files"]:
        change = item["evidence"]
        if change["before"]["sha256"] == change["after_sha256"]:
            continue
        checkpoint("mutation_started")
        frozen_files.apply_rewrite(root, change,
            lambda raw: json_transform(raw, family=item["family"], **arguments))
    for item in evidence["profile_databases"]:
        profile.apply(item, **arguments, timestamp=evidence["timestamp"], json_hashes=evidence["json_hashes"],
                      phase_callback=checkpoint)
    for change in evidence["removals"]:
        checkpoint("mutation_started")
        frozen_files.apply_remove(root, change)
    journal.apply(evidence["journal"], arguments["selected"], phase_callback=checkpoint)
    require_closed()
    if remaining(evidence):
        fail("frontend_targets_remain")
    phase_callback("verified")


def remaining(evidence, *, terminal_verified=False):
    root = validate(evidence)
    json_sources, databases = sources(root)
    expected_json = [(item["evidence"]["before"]["path"], item["family"]) for item in evidence["json_files"]]
    if json_sources != sorted(expected_json) or databases != sorted(item["relative"] for item in evidence["profile_databases"]):
        fail("frontend_restore_sources_changed")
    arguments = _arguments(evidence)
    observed = _hooks(root, arguments["native_ids"], evidence["tab_ids"])
    if _indexes(root) != evidence["indexes"]:
        fail("frontend_profile_indexes_changed")
    if _runtime_metadata(root) != evidence["runtime_observations"]:
        fail("frontend_runtime_metadata_changed")
    if not terminal_verified and observed != evidence["hook_observations"]:
        fail("frontend_hook_sources_changed")
    count = journal.remaining(evidence["journal"], arguments["selected"], terminal_verified=terminal_verified)
    count += sum(profile.remaining(item, **arguments, terminal_verified=terminal_verified)
                 for item in evidence["profile_databases"])
    for item in evidence["json_files"]:
        change = item["evidence"]
        _, raw = frozen_files.read_file(root, change["before"]["path"], content=True)
        residue = json_transform(raw, family=item["family"], **arguments) != raw
        count += residue
        if not residue and not terminal_verified and not frozen_files.satisfied(root, change):
            fail("frontend_json_after_state_unverified")
    count += sum(not frozen_files.satisfied(root, change) for change in evidence["removals"])
    # Reintroduced per-session histories remain observable even when their
    # directory was absent at plan time and therefore had no removal action.
    removed_paths = {item["path"] for item in evidence["removals"]}
    count += sum(item["path"] not in removed_paths for item in _legacy_journals(root, evidence["records"]))
    return count
