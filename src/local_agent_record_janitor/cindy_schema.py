"""Read-only proof of Cindy's supported message-index side effects.

The fingerprints bind complete trigger definitions, not names. They come from
the pinned upstream migrations documented in docs/cindy-storage-contract.md.
Unknown triggers and modified index layouts stay inventory-only.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3


class CindySchemaError(RuntimeError):
    pass


def sql_fingerprint(sql: str) -> str:
    # Preserve quoted strings/identifiers exactly; ignore only SQL whitespace
    # and comments. Never strip whitespace from inside a literal.
    tokens = re.findall(
        r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
        r"`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|[^\s]",
        sql, re.S,
    )
    normalized = [
        token if token[0] in "'\"`[" else token.casefold()
        for token in tokens if not token.startswith(("--", "/*"))
    ]
    # SQLite drops IF NOT EXISTS when persisting CREATE TRIGGER statements.
    if normalized[:5] == ["create", "trigger", "if", "not", "exists"]:
        del normalized[2:5]
    while normalized and normalized[-1] == ";":
        normalized.pop()
    return hashlib.sha256("\0".join(normalized).encode()).hexdigest()


# Pinned upstream SQL fingerprints, exercised using attributed SQL fixtures.
FTS_TRIGGER_VERSIONS: dict[str, dict[str, str]] = {
    "0017": {
        "messages_fts_delete": "80f3d1a0882acee22e413b0fc6ccac5128a0a413b06699c87f15a628a5c8313c",
        "messages_fts_insert": "efb3d9872e7556cd8f0bff50ae9f801226f8913e2a347339ca4a740a94e99144",
        "messages_fts_update": "9327a93d77f3f3b266000a7048e960f14f850f0c5a34e21f3a42d2c3a37b7b6a"
    },
    "0066": {
        "messages_fts_delete": "80f3d1a0882acee22e413b0fc6ccac5128a0a413b06699c87f15a628a5c8313c",
        "messages_fts_insert": "7cf3262f5967c5dce14c0b42e1cb1d84db2e0911bbd40f62563390e9c258f4f4",
        "messages_fts_update": "c9dcf458490640e94470e166c9c83e6f8fb4d33ebab5c98b6152f535dcbb384b"
    },
    "0095": {
        "messages_fts_delete": "80f3d1a0882acee22e413b0fc6ccac5128a0a413b06699c87f15a628a5c8313c",
        "messages_fts_insert": "7cf3262f5967c5dce14c0b42e1cb1d84db2e0911bbd40f62563390e9c258f4f4",
        "messages_fts_update": "26c305fb389e1c3296bae584ad79931804895af4c7d447689007c6e9fb38b946"
    },
    "0096": {
        "messages_fts_delete": "708bfa5601f8e886b2001c99fa832767a6f3b0991dd21065634c5f4c0227a16d",
        "messages_fts_insert": "c4ffcf97fc8b94124f00263b8d8d941a974d7c4375bf1567010168f20fc2cc92",
        "messages_fts_update": "7f8ed3e8e22a73d03e4c70dcfc7cf53dbda976e95abd18a482db485bc1a5386e"
    },
    "0100": {
        "messages_fts_delete": "708bfa5601f8e886b2001c99fa832767a6f3b0991dd21065634c5f4c0227a16d",
        "messages_fts_insert": "ece3725c50e68adc99fe5151538f3cd90130626114b4d82c0e38a2d709b800e6",
        "messages_fts_update": "ded9f0deb01653dc166cbf0aa91f72d7944c4c0ca70f7ac8fd0a1a21cc0771ae"
    }
}


def guard_cindy_triggers(db: sqlite3.Connection, table: str) -> str | None:
    rows = db.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=?",
        (table,),
    ).fetchall()
    if not rows:
        return None
    observed = {str(row[0]): sql_fingerprint(str(row[1] or "")) for row in rows}
    version = next((name for name, expected in FTS_TRIGGER_VERSIONS.items()
                    if table == "messages" and observed == expected), None)
    if version is None:
        raise CindySchemaError(f"Unsupported Cindy triggers on {table}: " + ", ".join(sorted(observed)))
    fts = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='messages_fts'").fetchone()
    expected_fts = (
        "CREATE VIRTUAL TABLE messages_fts USING fts5("
        "message_id UNINDEXED, session_id UNINDEXED, role UNINDEXED, content, "
        "tokenize='porter unicode61')"
    )
    if fts is None or sql_fingerprint(str(fts[0])) != sql_fingerprint(expected_fts):
        raise CindySchemaError("Unsupported Cindy messages_fts layout")
    dependencies = ["messages_fts", "messages_fts_data", "messages_fts_idx",
                    "messages_fts_content", "messages_fts_docsize", "messages_fts_config"]
    if version >= "0096":
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(messages_fts_rows)")}
        if set(columns) != {"fts_rowid", "message_id"} or columns["fts_rowid"][5] != 1:
            raise CindySchemaError("Unsupported Cindy messages_fts_rows layout")
        indexes = db.execute("PRAGMA index_list(messages_fts_rows)").fetchall()
        unique_message_id = any(
            row[2] and not row[4] and
            [info[2] for info in db.execute('PRAGMA index_info("' + str(row[1]).replace('"', '""') + '")')]
            == ["message_id"] for row in indexes
        )
        if not unique_message_id:
            raise CindySchemaError("Cindy messages_fts_rows requires a unique message_id")
        dependencies.append("messages_fts_rows")
    for dependency in dependencies:
        guard_cindy_triggers(db, dependency)
    return version


def guard_cindy_session_schema(db: sqlite3.Connection) -> None:
    """Check every explicitly modified table before a hard-delete plan is ready."""
    for table in ("sessions", "messages", "messages_fts", "messages_fts_rows",
                  "embedding_jobs", "chat_messages_vec_v1", "media_refs",
                  "skill_usage_sources", "skill_usage_exposures", "ghost_cards"):
        guard_cindy_triggers(db, table)
