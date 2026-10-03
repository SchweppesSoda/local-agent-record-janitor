"""Body-free fixed native metadata for isolated P5 fixtures."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


def create_native_schema(home):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "orca_codex_0160_schema.json").read_text())
    for name, value in fixture.items():
        with closing(sqlite3.connect(home / name)) as connection:
            rows = value["sqlite_master"]
            for kind in ("table", "index", "view", "trigger"):
                for row in rows:
                    if row[0] == kind and row[3] and not row[1].startswith("sqlite_"):
                        connection.execute(row[3])
            if any(row[1] == "sqlite_sequence" for row in rows):
                # Native migrations retain sqlite_sequence after dropping
                # their earlier AUTOINCREMENT table. SQLite owns this table.
                connection.execute("CREATE TABLE janitor_fixture_sequence(id INTEGER PRIMARY KEY AUTOINCREMENT)")
                connection.execute("DROP TABLE janitor_fixture_sequence")
            for version, description, success, checksum in value["migrations"]:
                connection.execute("INSERT INTO _sqlx_migrations(version,description,success,checksum,execution_time) VALUES(?,?,?,?,0)",
                                   (version, description, success, bytes.fromhex(checksum)))
            if name == "state_5.sqlite":
                connection.execute("INSERT INTO backfill_state(id,status,last_watermark,last_success_at,updated_at) VALUES(1,'complete',NULL,1790998710,1790998710)")
            connection.commit()


def add_native_record(home, thread_id, *, parent=None):
    path = home / "sessions" / "2026" / "10" / "03" / ("rollout-" + thread_id + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    source = {"subAgent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}} if parent else "cli"
    path.write_text(json.dumps({"timestamp": "2026-10-03T00:00:00.000Z", "type": "session_meta", "payload": {
        "id": thread_id, "timestamp": "2026-10-03T00:00:00.000Z", "cwd": str(home.parent),
        "originator": "codex_cli_rs", "source": source, "cli_version": "0.160.0",
        "model_provider": "janitor_local"}}) + "\n", encoding="utf-8")
    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
        connection.execute("INSERT INTO threads(id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode,cli_version,thread_source,originator) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (thread_id, str(path), 1790985600, 1790985600, json.dumps(source) if parent else source,
             "janitor_local", str(home.parent), "", '{"type":"read-only"}', "never", "0.160.0",
             "subAgent" if parent else "cli", "codex_cli_rs"))
        if parent:
            connection.execute("INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES(?,?,?)",
                               (parent, thread_id, "closed"))
        connection.commit()
    return path
