"""Synthetic metadata from Orca efbf651c7bb2eec778daf1844f8228e70809ec9f."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

HISTORY_ID = "11111111-1111-4111-8111-111111111111"
CURRENT_ID = "22222222-2222-4222-8222-222222222222"
SENTINEL = "ORCA_PRIVATE_OPTIONS_SENTINEL"

# These are the ten base tables in the fixed schema, not a product init call.
DDL = """
PRAGMA user_version=4;
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


def make_record(home: Path, session_id: str = "orca_fixture_01", *, empty: bool = False) -> dict:
    return {
        "schemaVersion": 2, "sessionId": session_id,
        "location": {"executionHostId": "local", "wslDistro": None,
                     "workspaceId": "workspace-fixture", "workspaceKind": "folder"},
        "provider": "codex", "accountHome": {"variable": "CODEX_HOME", "path": str(home)},
        "providerHandleChain": [] if empty else [
            {"linkId": "link_old", "handle": {"provider": "codex", "threadId": HISTORY_ID},
             "origin": "created", "mintedAtFence": 1, "observedAt": 1700000000000},
            {"linkId": "link_new", "handle": {"provider": "codex", "threadId": CURRENT_ID},
             "origin": "forked", "mintedAtFence": 2, "observedAt": 1700000000001,
             "forkedFromKey": 'codex:"' + HISTORY_ID + '"'},
        ],
        "lease": {"sessionId": session_id, "runtimeKind": "native", "runtimeFence": 2,
                  "handoffStage": None, "provenHandleLinkId": None, "ownerProcess": None,
                  "reservedSpawnToken": None, "leaseDeadlineAt": 1700000000002,
                  "lastRenewedAt": 1700000000002, "handoffOperationId": None,
                  "journalCheckpoint": None, "claimKeyId": "fixture-key",
                  "claimStatus": "released", "unreconciled": False, "deathEvidence": None},
        "createdAt": 1700000000000, "updatedAt": 1700000000002,
        "options": {"model": SENTINEL},
    }


def create_profile(root: Path, *, accounts: int = 2, tabs: str = "recorded-empty") -> tuple[Path, ...]:
    root.mkdir(parents=True)
    homes = []
    records = []
    for number in range(1, accounts + 1):
        account_id = f"account-{number}"
        home = root / "codex-accounts" / account_id / "home"
        (home / "sessions").mkdir(parents=True)
        (home / ".orca-managed-home").write_text(account_id + "\n", encoding="utf-8")
        homes.append(home)
        records.append(make_record(home, f"orca_fixture_{number:02}"))
    if homes:
        records.append(make_record(homes[0], "orca_fixture_empty", empty=True))
    with closing(sqlite3.connect(root / "agent-session-journal.db")) as connection:
        connection.executescript(DDL)
        connection.executemany("INSERT INTO agent_session_records VALUES (?, ?)",
                               [(r["sessionId"], json.dumps(r)) for r in records])
        if tabs in {"recorded-empty", "recorded"}:
            connection.execute("INSERT INTO agent_session_store_meta VALUES ('session_tabs_recorded', 'ignored')")
        if tabs in {"recorded", "unrecorded"}:
            connection.executemany("INSERT INTO agent_session_tabs VALUES (?, ?, ?)",
                                   [(f"tab-{i}", r["sessionId"], i) for i, r in enumerate(records)])
        connection.commit()
    return tuple(homes)


def replace_record(root: Path, record: dict) -> None:
    with closing(sqlite3.connect(root / "agent-session-journal.db")) as connection:
        connection.execute("INSERT OR REPLACE INTO agent_session_records VALUES (?, ?)",
                           (record["sessionId"], json.dumps(record)))
        connection.commit()
