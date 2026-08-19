from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any


def quote_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Invalid SQLite identifier")
    return '"' + value.replace('"', '""') + '"'


def table_schema(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        f"PRAGMA table_info({quote_identifier(table)})"
    ).fetchall()
    return tuple(
        {
            "cid": int(row["cid"]),
            "name": str(row["name"]),
            "type": str(row["type"] or ""),
            "notnull": bool(row["notnull"]),
            "default": sqlite_value(row["dflt_value"]),
            "pk": int(row["pk"]),
        }
        for row in rows
        if isinstance(row["name"], str) and row["name"]
    )


def schema_fingerprint(schema: Sequence[Mapping[str, Any]]) -> str:
    return fingerprint_json([dict(value) for value in schema])


def row_fingerprint(
    row: Mapping[str, Any] | sqlite3.Row,
    columns: Sequence[str],
) -> str:
    return fingerprint_json(
        [
            {"name": column, "value": sqlite_value(row[column])}
            for column in columns
        ]
    )


def sqlite_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bytes):
        return {
            "type": "blob",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    raise TypeError(f"Unsupported SQLite value type: {type(value).__name__}")


def fingerprint_json(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "fingerprint_json",
    "quote_identifier",
    "row_fingerprint",
    "schema_fingerprint",
    "sqlite_value",
    "table_schema",
    "text_sha256",
]
