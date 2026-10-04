"""Exact-schema office conversation transactions.

This is an internal component of a client closure, not a stand-alone deletion
command. Its caller must exclude the application and finish SDK/UI components
before recording a terminal receipt. Evidence contains identities and hashes,
never message, credential, draft or prompt bodies.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import tempfile
import time

from . import frozen_files
from .office_store import SCHEMAS, MAX_ROWS, _identifier, _UUID
from .sqlite_utils import connect_readonly

SCHEMA = "larj.office-database.v1"
RELATIVE = "data/agents.db"
MAX_BYTES = 512 * 1024 * 1024


class OfficeDatabaseError(RuntimeError):
    def __init__(self, code, *, unknown=False):
        super().__init__(code)
        self.kind, self.unknown = code, unknown


def _fail(code):
    raise OfficeDatabaseError("office_" + code)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("metadata_duplicate_key")
            result[key] = value
        return result
    if not isinstance(raw, str) or len(raw) > 16 * 1024 * 1024:
        _fail("metadata_budget_exceeded")
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: _fail("metadata_invalid"))
    except (ValueError, RecursionError) as exc:
        raise OfficeDatabaseError("office_metadata_invalid") from exc


def _rows(db, query, args=()):
    rows = db.execute(query, args).fetchmany(MAX_ROWS + 1)
    if len(rows) > MAX_ROWS:
        _fail("database_budget_exceeded")
    return [dict(row) for row in rows]


def qualify(db, client):
    if client not in SCHEMAS:
        _fail("client_unverified")
    db.row_factory = sqlite3.Row
    actual = [list(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    if actual != SCHEMAS[client]["objects"]:
        _fail("schema_unverified")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        _fail("relations_inconsistent")
    if [tuple(row) for row in db.execute("PRAGMA quick_check")] != [("ok",)]:
        _fail("database_corrupt")
    # FTS has independent content, not an external-content FK. A ghost entry
    # would otherwise survive deletion of the selected chat and still display
    # its text through search after a nominally successful transaction.
    if db.execute("""SELECT 1 FROM messages_fts AS f LEFT JOIN messages AS m ON f.rowid=m.rowid
        WHERE m.rowid IS NULL OR f.chat_id IS NOT m.chat_id OR f.sub_chat_id IS NOT m.sub_chat_id
        OR f.message_id IS NOT m.message_id OR f.role IS NOT m.role
        OR f.searchable_text IS NOT m.searchable_text LIMIT 1""").fetchone() or db.execute(
        "SELECT 1 FROM messages AS m LEFT JOIN messages_fts AS f ON m.rowid=f.rowid WHERE f.rowid IS NULL LIMIT 1").fetchone():
        _fail("search_projection_inconsistent")


def logical_hash(db):
    """Hash typed cells privately, including unselected data and FTS shadows."""
    sha, budget, count = hashlib.sha256(), 0, 0
    def field(value):
        nonlocal budget
        if value is None:
            raw = b"n"
        elif isinstance(value, bytes):
            raw = b"b" + value
        elif isinstance(value, str):
            raw = b"s" + value.encode("utf-8")
        elif isinstance(value, int):
            raw = b"i" + str(value).encode("ascii")
        elif isinstance(value, float):
            raw = b"f" + struct.pack("!d", value)
        else:
            _fail("database_value_unverified")
        budget += len(raw)
        if budget > MAX_BYTES:
            _fail("database_budget_exceeded")
        sha.update(struct.pack("!Q", len(raw))); sha.update(raw)
    tables = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"))
    for name, sql in tables:
        field(name); field(sql)
        # Names come only from the exact qualified sqlite_master registration.
        quoted = '"' + name.replace('"', '""') + '"'
        order = "rowid"
        if re.search(r"\bWITHOUT\s+ROWID\b", sql or "", re.I):
            columns = list(db.execute(f"PRAGMA table_info({quoted})"))
            keys = sorted((r[5], r[1]) for r in columns if r[5])
            order = ",".join('"' + key.replace('"', '""') + '"' for _, key in keys)
            if not order:
                _fail("database_schema_unverified")
        for row in db.execute(f"SELECT * FROM {quoted} ORDER BY {order}"):
            count += 1
            if count > MAX_ROWS * 20:
                _fail("database_budget_exceeded")
            field(len(row))
            for value in row:
                field(value)
        field(None)
    return sha.hexdigest()


def sidebar(raw, ids):
    value = _json(raw)
    if (not isinstance(value, dict) or set(value) != {"schemaVersion", "revision", "preferences", "orders", "pinnedByScope"}
            or value["schemaVersion"] != 2 or type(value["revision"]) is not int or value["revision"] < 0):
        _fail("sidebar_schema_unverified")
    preferences = value["preferences"]
    if not isinstance(preferences, dict) or set(preferences) != {"pinned", "projects", "recent"}:
        _fail("sidebar_schema_unverified")
    for key, mode in preferences.items():
        if mode not in ({"recency", "manual"} if key == "pinned" else {"recency", "name", "created", "manual"}):
            _fail("sidebar_schema_unverified")
    changed = False
    def remove(items, *, entry=False):
        nonlocal changed
        if not isinstance(items, list) or len(items) > MAX_ROWS or any(not isinstance(i, str) for i in items):
            _fail("sidebar_schema_unverified")
        wanted = {"task:" + i for i in ids} if entry else ids
        after = [i for i in items if i not in wanted]
        changed |= after != items
        return after
    orders = value["orders"]
    if not isinstance(orders, dict) or set(orders) != {"recent", "projects"} or not isinstance(orders["projects"], dict):
        _fail("sidebar_schema_unverified")
    orders["recent"] = remove(orders["recent"])
    for key in orders["projects"]:
        orders["projects"][key] = remove(orders["projects"][key])
    if not isinstance(value["pinnedByScope"], dict):
        _fail("sidebar_schema_unverified")
    for scope in value["pinnedByScope"].values():
        if not isinstance(scope, dict) or set(scope) != {"pinnedTaskIds", "order", "entryOrder"}:
            _fail("sidebar_schema_unverified")
        for key in scope:
            scope[key] = remove(scope[key], entry=key == "entryOrder")
    if not changed:
        return raw
    value["revision"] += 1
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def closure(db, client, ids):
    """Resolve typed ownership, never search prompt strings for identifiers."""
    chats = _rows(db, "SELECT id,project_id,worktree_path,source,source_chat_id,wecom_account_id,wecom_user_id,chat_type,ext FROM chats ORDER BY id")
    children = _rows(db, "SELECT id,chat_id,session_id,mode,ext FROM sub_chats ORDER BY id")
    by_chat = {r["id"]: r for r in chats}
    by_child = {r["id"]: r for r in children}
    if not ids or not ids <= by_chat.keys():
        _fail("selected_chat_missing")
    child_ids = {r["id"] for r in children if r["chat_id"] in ids}
    sdk_ids = {r["session_id"] for r in children if r["id"] in child_ids and r["session_id"]}
    sdk_folded = {i.casefold() for i in sdk_ids}
    if any(not _UUID.fullmatch(i) for i in sdk_ids):
        _fail("sdk_identity_unverified")
    for row in children:
        if row["chat_id"] not in by_chat:
            _fail("relations_inconsistent")
        if row["id"] not in child_ids and (row["session_id"] or "").casefold() in sdk_folded:
            _fail("sdk_session_shared")
    for row in chats:
        if row["source_chat_id"] in ids and row["id"] not in ids:
            _fail("chat_has_unselected_fork")
        if row["id"] in ids and (row["source"] or row["wecom_account_id"] or row["wecom_user_id"]
                or row["chat_type"] not in (None, "task", "coding")):
            _fail("chat_channel_scope_unverified")
    chat_fields = {"activeLegokitId", "awarenessDisabled", "sidebarGroupId", "sidebarGroupColor", "taskStatus", "isCronChat"}
    child_fields = {"workspaceModeId", "contextUsageSnapshot", "sessionMemoryPath"}
    if client == "qwenwork":
        chat_fields.add("taskStatusUpdatedAt")
        child_fields |= {"turnInputQueueVersion", "followUpMode", "queuePause", "apiErrorStreak"}
    selected_children = []
    for rows, allowed, selected in ((chats, chat_fields, ids), (children, child_fields, child_ids)):
        for row in rows:
            ext = _json(row["ext"]) if row["ext"] else {}
            if not isinstance(ext, dict):
                _fail("ext_schema_unverified")
            if row["id"] not in selected:
                if ext.get("forkSourceSubChatId") in child_ids or str(ext.get("forkSourceSessionId") or "").casefold() in sdk_folded or ext.get("boundToChatId") in ids:
                    _fail("chat_has_unselected_fork")
                continue
            if set(ext) - allowed or ext.get("isCronChat") not in (None, False):
                _fail("selected_ext_scope_unverified")
            if rows is children:
                streak = ext.get("apiErrorStreak")
                if streak is not None and (not isinstance(streak, dict) or streak.get("sessionId") not in (None, row["session_id"])):
                    _fail("sdk_old_session_unresolved")
                selected_children.append({"id": row["id"], "chat_id": row["chat_id"], "session_id": row["session_id"],
                    "session_memory_path": ext.get("sessionMemoryPath")})
    for table in ("messages", "agent_turn_inputs") if client == "qwenwork" else ("messages",):
        if db.execute(f"SELECT 1 FROM {table} AS m LEFT JOIN sub_chats AS s ON m.sub_chat_id=s.id WHERE s.id IS NULL OR s.chat_id<>m.chat_id LIMIT 1").fetchone():
            _fail("relations_inconsistent")
    for row in _rows(db, "SELECT chat_id,sub_chat_id FROM rc_session_mappings"):
        if row["chat_id"] in ids or row["sub_chat_id"] in child_ids:
            _fail("remote_session_requires_remote_contract")
    for table, chat_key, child_key in (("scheduled_tasks", "source_chat_id", "source_sub_chat_id"),
                                       ("task_run_logs", "chat_id", "sub_chat_id")):
        for row in _rows(db, f"SELECT {chat_key},{child_key} FROM {table}"):
            if row[chat_key] in ids or row[child_key] in child_ids:
                _fail("selected_automation_reference")
    for row in _rows(db, "SELECT source_sub_chat_id,source_session_id,evolution_chat_id FROM skill_evolution_suggestions"):
        if row["source_sub_chat_id"] in child_ids or row["source_session_id"] in sdk_ids or row["evolution_chat_id"] in ids:
            _fail("selected_skill_reference")
    nudge_ids = []
    for row in _rows(db, "SELECT id,sub_chat_id FROM nudge_logs ORDER BY id"):
        key = row["sub_chat_id"]
        owners = {i for i in by_child if i == key or i[:8] == key}
        if owners & child_ids:
            if not owners <= child_ids:
                _fail("nudge_owner_ambiguous")
            nudge_ids.append(row["id"])
    if client == "qwenwork":
        selected_messages = {r["id"] for r in _rows(db, "SELECT id,chat_id FROM messages") if r["chat_id"] in ids}
        typed = {"chat": ids, "sub_chat": child_ids, "message": selected_messages}
        for row in _rows(db, "SELECT entity_type,source_id,target_id FROM data_import_records"):
            if {row["source_id"], row["target_id"]} & typed.get(row["entity_type"], set()):
                _fail("selected_import_reference")
        if db.execute("SELECT 1 FROM acp_idempotency LIMIT 1").fetchone():
            _fail("acp_replay_scope_unverified")
    return {"sub_chats": selected_children, "sdk_ids": sorted(sdk_ids), "nudge_ids": nudge_ids}


def _transform(db, client, ids, timestamp):
    owned = closure(db, client, ids)
    for identifier in owned["nudge_ids"]:
        db.execute("DELETE FROM nudge_logs WHERE id=?", (identifier,))
    if client == "qwenwork":
        row = db.execute("SELECT value FROM app_settings WHERE key='sidebarTaskLayout'").fetchone()
        if row:
            after = sidebar(row[0], ids)
            if after != row[0]:
                db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE key='sidebarTaskLayout'", (after, timestamp))
    for identifier in sorted(ids):
        db.execute("DELETE FROM chats WHERE id=?", (identifier,))
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        _fail("relations_inconsistent")


def _path(root):
    path = frozen_files.checked_path(root, RELATIVE)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        try:
            frozen_files._plain(candidate.lstat())
        except FileNotFoundError:
            if not suffix:
                raise
    return path


def freeze(client, root, chat_ids, *, timestamp=None):
    ids = sorted({_identifier(i) for i in chat_ids})
    path = _path(root)
    identity = list(frozen_files._identity(path.stat()))
    timestamp = int(time.time()) if timestamp is None else timestamp
    with closing(connect_readonly(path)) as source:
        source.execute("BEGIN")
        qualify(source, client)
        owned, before = closure(source, client, set(ids)), logical_hash(source)
        with tempfile.TemporaryDirectory(prefix="larj-office-db-") as directory:
            with closing(sqlite3.connect(Path(directory) / "simulation.sqlite")) as copy:
                source.backup(copy)
                copy.execute("PRAGMA foreign_keys=ON")
                copy.execute("PRAGMA secure_delete=ON")
                qualify(copy, client)
                copy.execute("INSERT INTO messages_fts(messages_fts,rank) VALUES ('integrity-check',1)")
                copy.commit()
                _transform(copy, client, set(ids), timestamp)
                staged = logical_hash(copy)
                copy.commit()
                after = logical_hash(copy)
    if list(frozen_files._identity(_path(root).stat())) != identity:
        _fail("database_replaced")
    return {"schema_version": SCHEMA, "client": client, "root": str(root), "relative": RELATIVE,
        "chat_ids": ids, "identity": identity, "before_sha256": before, "staged_sha256": staged, "after_sha256": after,
        "timestamp": timestamp, **owned}


def _validate(evidence):
    if (evidence.get("schema_version") != SCHEMA or evidence.get("relative") != RELATIVE
            or evidence.get("client") not in SCHEMAS or type(evidence.get("timestamp")) is not int):
        _fail("database_evidence_invalid")
    ids = evidence.get("chat_ids")
    if not isinstance(ids, list) or not ids or ids != sorted({_identifier(i) for i in ids}):
        _fail("database_evidence_invalid")
    for key in ("before_sha256", "staged_sha256", "after_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get(key, ""))):
            _fail("database_evidence_invalid")
    root = Path(evidence["root"])
    path = _path(root)
    if list(frozen_files._identity(path.stat())) != evidence["identity"]:
        _fail("database_replaced")
    return root, path


@contextmanager
def _database_fence(root, path, identity):
    with frozen_files._parent_fence(root, RELATIVE):
        fd = (frozen_files._windows_fd(path) if os.name == "nt" else
              os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)))
        try:
            frozen_files._plain(os.fstat(fd))
            if list(frozen_files._identity(os.fstat(fd))) != identity or list(frozen_files._identity(path.lstat())) != identity:
                _fail("database_replaced")
            yield
            if list(frozen_files._identity(path.lstat())) != identity:
                _fail("database_replaced")
        finally:
            os.close(fd)


def apply(evidence, *, phase_callback):
    root, path = _validate(evidence)
    started = False
    try:
        with _database_fence(root, path, evidence["identity"]), closing(sqlite3.connect(path, timeout=0)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            qualify(db, evidence["client"])
            if logical_hash(db) != evidence["before_sha256"]:
                _fail("database_changed")
            if closure(db, evidence["client"], set(evidence["chat_ids"])) != {
                    k: evidence[k] for k in ("sub_chats", "sdk_ids", "nudge_ids")}:
                _fail("database_closure_changed")
            phase_callback("mutation_started")
            started = True
            _transform(db, evidence["client"], set(evidence["chat_ids"]), evidence["timestamp"])
            # FTS5 flushes pending segment state at commit. Bind both the
            # transactional projection and the independently reopened result.
            if logical_hash(db) != evidence["staged_sha256"]:
                _fail("database_after_state_unverified")
            db.commit()
        if remaining(evidence):
            _fail("database_targets_remain")
    except Exception as exc:
        raise OfficeDatabaseError(getattr(exc, "kind", "office_database_transaction_failed"), unknown=started) from exc


def remaining(evidence, *, terminal_verified=False):
    _, path = _validate(evidence)
    with closing(connect_readonly(path)) as db:
        db.execute("BEGIN")
        qualify(db, evidence["client"])
        current = {row[0] for row in db.execute("SELECT id FROM chats")}
        count = len(current & set(evidence["chat_ids"]))
        if not count and not terminal_verified and logical_hash(db) != evidence["after_sha256"]:
            _fail("database_after_state_unverified")
        return count
