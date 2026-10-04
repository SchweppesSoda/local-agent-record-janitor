"""Private simulation and exact transactions for qualified client databases.

Callers supply the registered schema and deterministic transformation, hold the
product lifecycle exclusion, and own the durable mutation journal. This module
is deliberately not a deletion command or a schema discovery mechanism.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import struct
import tempfile

from . import frozen_files
from .sqlite_utils import connect_readonly

SCHEMA = "larj.frozen-sqlite.v1"
MAX_ROWS = 400000
MAX_BYTES = 512 * 1024 * 1024


class FrozenSQLiteError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.kind = "sqlite_" + code


def fail(code):
    raise FrozenSQLiteError(code)


def logical_hash(db):
    """Bind schema, typed values and row identity without exporting contents."""
    sha, size, count = hashlib.sha256(), 0, 0
    def field(value):
        nonlocal size
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
            fail("cell_type_unverified")
        size += len(raw)
        if size > MAX_BYTES:
            fail("budget_exceeded")
        sha.update(struct.pack("!Q", len(raw)))
        sha.update(raw)
    field(db.execute("PRAGMA user_version").fetchone()[0])
    field(db.execute("PRAGMA application_id").fetchone()[0])
    objects = list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
    if len(objects) > 4096:
        fail("schema_budget_exceeded")
    for row in objects:
        for value in row:
            field(value)
    for kind, name, _, sql in objects:
        if kind != "table":
            continue
        if re.match(r"CREATE\s+VIRTUAL\s+TABLE", sql or "", re.I):
            fail("virtual_table_unregistered")
        quoted = '"' + name.replace('"', '""') + '"'
        field(name)
        if re.search(r"\bWITHOUT\s+ROWID\b", sql or "", re.I):
            keys = sorted((r[5], r[1]) for r in db.execute(f"PRAGMA table_info({quoted})") if r[5])
            if not keys:
                fail("primary_key_unverified")
            order = ",".join('"' + name.replace('"', '""') + '"' for _, name in keys)
            projection = "*"
        else:
            # The registered schemas must not shadow the SQLite row identity.
            columns = {str(r[1]).casefold() for r in db.execute(f"PRAGMA table_info({quoted})")}
            if columns & {"rowid", "_rowid_", "oid"}:
                fail("row_identity_shadowed")
            order, projection = "rowid", "rowid,*"
        for row in db.execute(f"SELECT {projection} FROM {quoted} ORDER BY {order}"):
            count += 1
            if count > MAX_ROWS:
                fail("budget_exceeded")
            field(len(row))
            for value in row:
                field(value)
        field(None)
    return sha.hexdigest()


def path_for(root, relative):
    path = frozen_files.checked_path(root, relative)
    info = path.lstat()
    frozen_files._plain(info)
    total = info.st_size
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = frozen_files.checked_path(root, relative + suffix)
        try:
            info = sidecar.lstat()
            frozen_files._plain(info)
            total += info.st_size
        except FileNotFoundError:
            pass
    if total > MAX_BYTES:
        fail("database_family_budget_exceeded")
    return path


@contextmanager
def fence(root, relative, identity):
    path = path_for(root, relative)
    with frozen_files._parent_fence(root, relative):
        descriptor = (frozen_files._windows_fd(path) if os.name == "nt" else
                      os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)))
        try:
            frozen_files._plain(os.fstat(descriptor))
            if frozen_files._identity(os.fstat(descriptor)) != identity or frozen_files._identity(path.lstat()) != identity:
                fail("database_replaced")
            yield path
            if frozen_files._identity(path_for(root, relative).lstat()) != identity:
                fail("database_replaced")
        finally:
            os.close(descriptor)


def _qualify(db, qualifier):
    qualifier(db)
    if [tuple(row) for row in db.execute("PRAGMA quick_check")] != [("ok",)]:
        fail("integrity_unverified")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        fail("relationships_inconsistent")


def freeze(root, relative, *, qualifier, transform):
    path = path_for(root, relative)
    identity = frozen_files._identity(path.lstat())
    with fence(root, relative, identity), closing(connect_readonly(path)) as source:
        source.execute("BEGIN")
        _qualify(source, qualifier)
        before = logical_hash(source)
        with tempfile.TemporaryDirectory(prefix="larj-sqlite-closure-") as directory:
            with closing(sqlite3.connect(Path(directory) / "copy.sqlite")) as copy:
                source.backup(copy)
                _qualify(copy, qualifier)
                if logical_hash(copy) != before:
                    fail("private_copy_changed")
                copy.execute("PRAGMA foreign_keys=ON")
                copy.execute("PRAGMA secure_delete=ON")
                copy.execute("BEGIN IMMEDIATE")
                transform(copy)
                staged = logical_hash(copy)
                copy.commit()
                _qualify(copy, qualifier)
                after = logical_hash(copy)
    return {"schema_version": SCHEMA, "root": str(root), "relative": relative,
            "identity": identity, "before_sha256": before, "staged_sha256": staged, "after_sha256": after}


def validate(evidence):
    if evidence.get("schema_version") != SCHEMA:
        fail("evidence_schema_unverified")
    for field in ("before_sha256", "staged_sha256", "after_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get(field, ""))):
            fail("evidence_hash_invalid")
    root = Path(evidence["root"])
    path = path_for(root, evidence["relative"])
    if frozen_files._identity(path.lstat()) != evidence["identity"]:
        fail("database_replaced")
    return root, path


def apply(evidence, *, qualifier, transform, phase_callback):
    root, _ = validate(evidence)
    with fence(root, evidence["relative"], evidence["identity"]) as path:
        with closing(sqlite3.connect(path, timeout=0)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            _qualify(db, qualifier)
            if logical_hash(db) != evidence["before_sha256"]:
                fail("before_state_changed")
            phase_callback("mutation_started")
            transform(db)
            if logical_hash(db) != evidence["staged_sha256"]:
                fail("staged_state_unverified")
            db.commit()
        with closing(connect_readonly(path)) as db:
            db.execute("BEGIN")
            _qualify(db, qualifier)
            if logical_hash(db) != evidence["after_sha256"]:
                fail("committed_state_unverified")


def remaining(evidence, *, qualifier, count, terminal_verified=False):
    root, _ = validate(evidence)
    with fence(root, evidence["relative"], evidence["identity"]) as path, closing(connect_readonly(path)) as db:
        db.execute("BEGIN")
        _qualify(db, qualifier)
        result = count(db)
        if type(result) is not int or result < 0:
            fail("residual_count_invalid")
        if not result and not terminal_verified and logical_hash(db) != evidence["after_sha256"]:
            fail("after_state_unverified")
        return result
