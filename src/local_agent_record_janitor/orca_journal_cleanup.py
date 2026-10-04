"""Qualified Orca journal component; the coordinator owns the full closure."""
from __future__ import annotations

import hashlib
import json
import re

from . import frozen_sqlite
from .orca_metadata import parse_orca_record

RELATIVE = "agent-session-journal.db"
MAX_ITEMS = 20000
DDL = """
CREATE TABLE journal_rows(session_id TEXT NOT NULL,epoch TEXT NOT NULL,seq INTEGER NOT NULL,ts INTEGER NOT NULL,row_json TEXT NOT NULL,PRIMARY KEY(session_id,epoch,seq));
CREATE TABLE journal_sessions(session_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,epoch TEXT NOT NULL);
CREATE TABLE journal_repairs(session_id TEXT PRIMARY KEY,epoch TEXT NOT NULL,content_from INTEGER NOT NULL,repaired_at INTEGER NOT NULL);
CREATE TABLE journal_imports(session_id TEXT PRIMARY KEY,epoch TEXT NOT NULL,tip INTEGER NOT NULL);
CREATE TABLE journal_set_aside(session_id TEXT PRIMARY KEY,epoch TEXT NOT NULL,tip INTEGER NOT NULL);
CREATE TABLE agent_session_records(session_id TEXT PRIMARY KEY,record_json TEXT NOT NULL);
CREATE TABLE agent_session_operations(operation_key TEXT PRIMARY KEY,row_json TEXT NOT NULL);
CREATE TABLE agent_session_retired_claim_keys(key_id TEXT PRIMARY KEY,retired_at INTEGER NOT NULL);
CREATE TABLE agent_session_tabs(tab_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,position INTEGER NOT NULL);
CREATE TABLE agent_session_store_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""
SESSION_TABLES = ("journal_rows", "journal_sessions", "journal_repairs", "journal_imports",
                  "journal_set_aside", "agent_session_records", "agent_session_tabs")


class OrcaFrontendError(RuntimeError):
    def __init__(self, code):
        super().__init__("orca_" + code)
        self.kind = str(self)


def fail(code):
    raise OrcaFrontendError(code)


def text(value, *, empty=False):
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= 4096


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 9007199254740991


def decode(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > 16 * 1024 * 1024:
        fail("frontend_json_budget_exceeded")
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                fail("frontend_json_duplicate_key")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: fail("frontend_json_invalid"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise OrcaFrontendError("frontend_json_invalid") from exc
    count, pending = 0, [(value, 0)]
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 100000 or depth > 64:
            fail("frontend_json_budget_exceeded")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def rows(db, query, args=()):
    result = db.execute(query, args).fetchmany(MAX_ITEMS + 1)
    if len(result) > MAX_ITEMS:
        fail("frontend_row_budget_exceeded")
    return result


def _sql(value):
    return re.sub(r"\s+", "", value).replace(";", "").casefold().replace("ifnotexists", "")


def qualify(db):
    if db.execute("PRAGMA user_version").fetchone()[0] != 4 or db.execute("PRAGMA application_id").fetchone()[0] != 0:
        fail("journal_writer_schema_unverified")
    expected = {_sql(statement) for statement in DDL.split(";") if statement.strip()}
    actual = set()
    for kind, name, sql in db.execute("SELECT type,name,sql FROM sqlite_master"):
        if kind == "index" and sql is None and name.startswith("sqlite_autoindex_"):
            continue
        if kind != "table" or sql is None:
            fail("journal_writer_schema_unverified")
        actual.add(_sql(sql))
    if actual != expected:
        fail("journal_writer_schema_unverified")


def command(value, selected, *, record=False):
    fields = {"command", "state", "replacementSessionId", "error", "failure"}
    if record:
        fields |= {"runtimeFence", "operationId", "callerKey", "phase"}
    if (not isinstance(value, dict) or set(value) - fields or value.get("command") not in {"clear", "compact"}
            or value.get("state") not in {"completed", "unknown"}
            or "replacementSessionId" in value and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", str(value["replacementSessionId"]))
            or "error" in value and not text(value["error"], empty=True)):
        fail("conversation_command_shape_unverified")
    if record and (value.get("phase") not in {"prepared", "committed"}
            or not text(value.get("operationId")) or not text(value.get("callerKey"))
            or "runtimeFence" in value and not integer(value["runtimeFence"], 1)):
        fail("conversation_command_shape_unverified")
    if "failure" in value:
        fail("conversation_failure_recovery_unverified")
    if value.get("replacementSessionId") in selected:
        return {key: item for key, item in value.items() if key != "replacementSessionId"}
    return value


def record_references(value, selected):
    if not isinstance(value, dict):
        fail("frontend_record_unverified")
    if "conversationCommand" not in value:
        return value
    after = command(value["conversationCommand"], selected, record=True)
    return value if after == value["conversationCommand"] else {**value, "conversationCommand": after}


def _launch(value):
    if (not isinstance(value, dict) or set(value) - {"outcome", "worktreeId", "warning", "receipt", "prompt"}
            or not text(value.get("worktreeId"), empty=True)
            or "warning" in value and not text(value["warning"], empty=True)):
        fail("launch_result_unverified")
    outcome, receipt = value.get("outcome"), value.get("receipt")
    if not isinstance(outcome, dict) or not text(outcome.get("handle")):
        fail("launch_result_unverified")
    if outcome.get("kind") == "structured":
        if (set(outcome) - {"kind", "sessionId", "handle", "tabId"} or not text(outcome.get("sessionId"))
                or "tabId" in outcome and not text(outcome["tabId"], empty=True)):
            fail("launch_result_unverified")
    elif outcome.get("kind") != "terminal" or set(outcome) - {"kind", "handle", "paneKey"}:
        fail("launch_result_unverified")
    elif "paneKey" in outcome and not text(outcome["paneKey"], empty=True):
        fail("launch_result_unverified")
    if (not isinstance(receipt, dict) or set(receipt) != {"mode", "preferred", "reason", "detail"}
            or receipt["mode"] not in {"structured", "terminal"} or receipt["preferred"] not in {"structured", "terminal"}
            or not text(receipt["reason"], empty=True) or not text(receipt["detail"], empty=True)):
        fail("launch_result_unverified")
    if "prompt" in value:
        prompt = value["prompt"]
        if (not isinstance(prompt, dict) or prompt.get("delivery") not in {"submit", "draft"}
                or prompt.get("outcome") not in {"journaled", "handed-to-terminal", "not-delivered"}
                or set(prompt) != ({"delivery", "outcome", "messageId"} if prompt.get("outcome") == "journaled" else {"delivery", "outcome"})
                or prompt.get("outcome") == "journaled" and not text(prompt.get("messageId"), empty=True)):
            fail("launch_result_unverified")
    return outcome.get("sessionId") if outcome["kind"] == "structured" else None


def operation(key, value, selected):
    required = {"callerKey", "operationId", "fingerprint", "operationTimestamp", "recordedAt", "expiresAt", "outcome"}
    if (not isinstance(value, dict) or set(value) != required
            or any(not text(value[k]) for k in ("callerKey", "operationId", "fingerprint"))
            or any(not integer(value[k]) for k in ("operationTimestamp", "recordedAt", "expiresAt"))
            or re.fullmatch(r"[0-9]{13}-[0-9a-f]{32}", value["operationId"]) is None
            or "\0" in value["callerKey"] or "\0" in value["operationId"]
            or key != value["callerKey"] + "\0" + value["operationId"]):
        fail("operation_ledger_shape_unverified")
    outcome = value["outcome"]
    if not isinstance(outcome, dict):
        fail("operation_outcome_unverified")
    status = outcome.get("status")
    if status in {"pending", "unknown"}:
        if set(outcome) != {"status"}:
            fail("operation_outcome_unverified")
        return value
    if status == "failed":
        if not text(outcome.get("code")) or set(outcome) - {"status", "code", "message", "rewindReason", "details"}:
            fail("operation_outcome_unverified")
        if "message" in outcome and not text(outcome["message"], empty=True):
            fail("operation_refusal_recovery_unverified")
        if "rewindReason" in outcome and outcome["rewindReason"] not in {
                "unsupported", "history-not-paginated", "busy", "stale-epoch", "invalid-target", "history-limit",
                "provider-refused", "proof-mismatch", "outcome-unknown"}:
            fail("operation_refusal_recovery_unverified")
        if "details" in outcome:
            details = outcome["details"]
            if (not isinstance(details, dict) or set(details) - {"reason", "ownerVerdict", "rewindReason", "currentFence", "currentRevision"}
                    or any(not text(details[key]) for key in ("reason", "rewindReason") if key in details)
                    or "ownerVerdict" in details and details["ownerVerdict"] not in {"live", "unverifiable", "exited"}
                    or any(not integer(details[key]) for key in ("currentFence", "currentRevision") if key in details)):
                fail("operation_refusal_recovery_unverified")
        return value
    if (status != "succeeded" or not text(outcome.get("sessionId"), empty=True)
            or set(outcome) - {"status", "sessionId", "conversationCommand", "rewind", "launch"}):
        fail("operation_outcome_unverified")
    if outcome["sessionId"] not in selected:
        after = dict(outcome)
        if "conversationCommand" in after:
            after["conversationCommand"] = command(after["conversationCommand"], selected)
        if "rewind" in after and (not isinstance(after["rewind"], dict) or set(after["rewind"]) != {"itemId", "epoch"}
                or any(not text(item, empty=True) for item in after["rewind"].values())):
            fail("rewind_result_unverified")
        if "launch" not in after or _launch(after["launch"]) not in selected:
            return value if after == outcome else {**value, "outcome": after}
    # A key must survive or a repeated request can create a second session.
    # Opaque request fingerprints may contain content; changing those to a
    # hash leaves the old key conflicting rather than admitting another spawn.
    fingerprint = value["fingerprint"]
    if re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) is None:
        fingerprint = "larj-cleaned-sha256:" + hashlib.sha256(fingerprint.encode()).hexdigest()
    return {**value, "fingerprint": fingerprint, "outcome": {"status": "unknown"}}


def read_selection(db, selected):
    qualify(db)
    result = []
    for sid, raw in rows(db, "SELECT session_id,record_json FROM agent_session_records ORDER BY session_id"):
        if sid not in selected:
            continue
        value = decode(raw)
        record = parse_orca_record(sid, value)
        if (record.host != "local" or record.wsl_distro is not None or record.provider != "codex"
                or record.unsupported_recovery):
            fail("selected_record_recovery_unverified")
        result.append(record)
    return tuple(result)


def transform(db, selected):
    # Plan and apply use the same deterministic operation. Inputs are exact
    # frontend session IDs; SQL names are constants from the registered schema.
    for key, raw in rows(db, "SELECT operation_key,row_json FROM agent_session_operations ORDER BY operation_key"):
        value = decode(raw)
        after = operation(key, value, selected)
        if after != value:
            db.execute("UPDATE agent_session_operations SET row_json=? WHERE operation_key=?", (encode(after), key))
    for sid, raw in rows(db, "SELECT session_id,record_json FROM agent_session_records"):
        if sid not in selected:
            value = decode(raw)
            after = record_references(value, selected)
            if after != value:
                db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (encode(after), sid))
    for sid in sorted(selected):
        for table in SESSION_TABLES:
            db.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))


def count_remaining(db, selected):
    count = sum(db.execute(f"SELECT count(*) FROM {table} WHERE session_id=?", (sid,)).fetchone()[0]
                for table in SESSION_TABLES for sid in selected)
    for key, raw in rows(db, "SELECT operation_key,row_json FROM agent_session_operations ORDER BY operation_key"):
        value = decode(raw)
        count += operation(key, value, selected) != value
    for sid, raw in rows(db, "SELECT session_id,record_json FROM agent_session_records"):
        if sid not in selected:
            value = decode(raw)
            count += record_references(value, selected) != value
    return count


def freeze(root, selected):
    return frozen_sqlite.freeze(root, RELATIVE, qualifier=qualify, transform=lambda db: transform(db, selected))


def apply(evidence, selected, *, phase_callback):
    frozen_sqlite.apply(evidence, qualifier=qualify, transform=lambda db: transform(db, selected), phase_callback=phase_callback)


def remaining(evidence, selected, *, terminal_verified=False):
    return frozen_sqlite.remaining(evidence, qualifier=qualify, count=lambda db: count_remaining(db, selected),
                                   terminal_verified=terminal_verified)
