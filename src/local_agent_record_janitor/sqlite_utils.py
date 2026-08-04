from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open a SQLite database without allowing accidental writes."""
    resolved = path.expanduser().resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None

