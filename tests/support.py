from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def create_aionui_database(
    path: Path,
    *,
    conversations: list[str] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    metadata: list[tuple[str, str]] | None = None,
    include_metadata_table: bool = True,
) -> None:
    conversations = conversations or []
    sessions = sessions or []
    metadata = metadata or []
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY
            );
            CREATE TABLE acp_session (
                conversation_id TEXT NOT NULL,
                session_id TEXT,
                agent_id TEXT,
                agent_source TEXT,
                session_status TEXT,
                last_active_at INTEGER
            );
            """
        )
        if include_metadata_table:
            connection.execute(
                """
                CREATE TABLE agent_metadata (
                    agent_id TEXT PRIMARY KEY,
                    backend TEXT
                )
                """
            )
        connection.executemany(
            "INSERT INTO conversations (id) VALUES (?)",
            ((conversation_id,) for conversation_id in conversations),
        )
        connection.executemany(
            """
            INSERT INTO acp_session (
                conversation_id,
                session_id,
                agent_id,
                agent_source,
                session_status,
                last_active_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    row["conversation_id"],
                    row.get("session_id"),
                    row.get("agent_id"),
                    row.get("agent_source"),
                    row.get("session_status"),
                    row.get("last_active_at"),
                )
                for row in sessions
            ),
        )
        if include_metadata_table:
            connection.executemany(
                "INSERT INTO agent_metadata (agent_id, backend) VALUES (?, ?)",
                metadata,
            )
        connection.commit()


def create_cindy_database(path: Path, sessions: list[dict[str, Any]]) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                sdk_session_id TEXT,
                status TEXT,
                source TEXT,
                created_at INTEGER,
                updated_at INTEGER,
                parent_session_id TEXT,
                agent_kind TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO sessions (
                id,
                sdk_session_id,
                status,
                source,
                created_at,
                updated_at,
                parent_session_id,
                agent_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    row["id"],
                    row.get("sdk_session_id"),
                    row.get("status"),
                    row.get("source"),
                    row.get("created_at"),
                    row.get("updated_at"),
                    row.get("parent_session_id"),
                    row.get("agent_kind"),
                )
                for row in sessions
            ),
        )
        connection.commit()


def write_rollout(
    codex_home: Path,
    thread_id: str,
    *,
    originator: str | None,
    archived: bool = False,
    use_session_id: bool = False,
    source: Any = "app-server",
    first_record_type: str = "session_meta",
) -> Path:
    root_name = "archived_sessions" if archived else "sessions"
    path = codex_home / root_name / "2026" / "07" / "31" / f"rollout-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        ("session_id" if use_session_id else "id"): thread_id,
        "cwd": str(codex_home.parent),
        "timestamp": "2026-07-31T00:00:00Z",
        "source": source,
    }
    if originator is not None:
        payload["originator"] = originator
    first_record = {"type": first_record_type, "payload": payload}
    second_record = {"type": "event_msg", "payload": {"type": "user_message"}}
    path.write_text(
        json.dumps(first_record) + "\n" + json.dumps(second_record) + "\n",
        encoding="utf-8",
    )
    return path


def create_thread_index(
    codex_home: Path,
    rows: list[dict[str, Any]],
    *,
    include_threads_table: bool = True,
    spawn_edges: list[dict[str, Any]] | None = None,
    include_spawn_edges_table: bool = True,
) -> Path:
    spawn_edges = spawn_edges or []
    codex_home.mkdir(parents=True, exist_ok=True)
    path = codex_home / "state_5.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        if include_threads_table:
            connection.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER,
                    updated_at INTEGER,
                    source TEXT,
                    thread_source TEXT,
                    agent_nickname TEXT,
                    agent_role TEXT
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO threads (
                    id,
                    rollout_path,
                    archived,
                    created_at,
                    updated_at,
                    source,
                    thread_source,
                    agent_nickname,
                    agent_role
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        row["id"],
                        row.get("rollout_path"),
                        row.get("archived", 0),
                        row.get("created_at", 1),
                        row.get("updated_at", 2),
                        _sqlite_source(row.get("source")),
                        row.get("thread_source"),
                        row.get("agent_nickname"),
                        row.get("agent_role"),
                    )
                    for row in rows
                ),
            )
        else:
            connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
        if include_spawn_edges_table:
            connection.execute(
                """
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT NOT NULL,
                    child_thread_id TEXT NOT NULL PRIMARY KEY,
                    status TEXT NOT NULL
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO thread_spawn_edges (
                    parent_thread_id,
                    child_thread_id,
                    status
                ) VALUES (?, ?, ?)
                """,
                (
                    (
                        row["parent_thread_id"],
                        row["child_thread_id"],
                        row.get("status", "closed"),
                    )
                    for row in spawn_edges
                ),
            )
        connection.commit()
    return path


def _sqlite_source(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value
