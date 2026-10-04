"""Independent WorkBuddy 5.6.2 metadata and exact local-session writer.

Transcript files are streamed only for hashes. Shared JSON is parsed solely
to locate documented ID references; titles and message content never enter
the metadata projection. SQLite backups are private, temporary rollback
copies, not operation receipts.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from .client_contracts import ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle, ReferenceSnapshot, SourceFailure
from .record_identity import EngineCapability, ProjectKey, RecordClassification, RecordKey, StoreKey, canonical_path
from .sqlite_utils import connect_readonly

KIND = "delete_workbuddy_session"
SCHEMA = "larj.workbuddy-session-evidence.v1"
MAX_ENTRIES = 20000
MAX_JSON_BYTES = 16 * 1024 * 1024
META_COLUMNS = ("id", "cwd", "user_id", "status", "created_at", "updated_at", "last_activity_at", "deleted_at",
                "is_playground", "source_mode", "is_background_automation", "mode", "project_id", "transport",
                "conversation_origin", "visibility", "group_id", "agent_dirty", "agent_dirty_at", "agent_last_synced",
                "verified_at", "unread", "buddy_snapshot_id")
TERMINAL = {"completed", "deleted", "failed", "cancelled", "canceled", "error"}
ARTIFACT_SUFFIXES = (".jsonl", ".meta.json")
SIDEBAR_KEYS = {"id", "title", "state", "transport", "kind", "lastActivityAt", "isPinned", "conversationOrigin",
                "updatedAt", "isUserDefinedTitle", "space"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_EXPECTED = json.loads(files(__package__).joinpath("workbuddy_schema_562.json").read_text(encoding="utf-8"))


class WorkBuddyStoreError(RuntimeError):
    def __init__(self, code, *, rolled_back=False, unknown=False):
        super().__init__(str(code))
        self.kind = str(code).split(":", 1)[0]
        self.outcome_known_rolled_back = rolled_back
        self.outcome_unknown = unknown


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def valid_id(value):
    return isinstance(value, str) and bool(_UUID.fullmatch(value))


def _strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise WorkBuddyStoreError("workbuddy_json_duplicate_key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(WorkBuddyStoreError("workbuddy_json_nonfinite_number")))


def _scalar(value, expected, *, nullable=True):
    return (value is None and nullable) or (isinstance(value, expected) and not isinstance(value, bool))


def local_root(value):
    from .path_identity import is_local_absolute_locator
    raw = os.fspath(Path(value).expanduser())
    if not is_local_absolute_locator(raw):
        raise WorkBuddyStoreError("workbuddy_root_unproven")
    return Path(raw).absolute()


def plain_path(path, root, *, regular=False, optional=False):
    """Validate the lexical path, every parent, reparse points and hardlinks."""
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise WorkBuddyStoreError("workbuddy_path_escape") from exc
    if ".." in parts:
        raise WorkBuddyStoreError("workbuddy_path_escape")
    # Explicit roots can themselves be under a junction/symlink. Validate
    # the complete anchor chain, not only descendants of the given root.
    chain = list(reversed(root.parents)) + [root]
    for part in parts:
        chain.append(chain[-1] / part)
    for index, candidate in enumerate(chain):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if optional and index == len(chain) - 1:
                return None
            raise
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or (index < len(chain) - 1 and not stat.S_ISDIR(info.st_mode))):
            raise WorkBuddyStoreError("workbuddy_link_or_nonordinary_path")
        if index == len(chain) - 1:
            if regular and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise WorkBuddyStoreError("workbuddy_link_or_nonordinary_file")
            if not regular and not stat.S_ISDIR(info.st_mode):
                raise WorkBuddyStoreError("workbuddy_nonordinary_directory")
    return info


def entries(directory, *, optional=False):
    directory = Path(directory)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if optional:
            return ()
        raise
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise WorkBuddyStoreError("workbuddy_link_or_nonordinary_directory")
    with os.scandir(directory) as iterator:
        result = []
        for entry in iterator:
            result.append(directory / entry.name)
            if len(result) > MAX_ENTRIES:
                raise WorkBuddyStoreError("workbuddy_discovery_limit_exceeded")
    return tuple(sorted(result, key=lambda path: path.name))


def fingerprint(path, root, *, optional=False):
    info = plain_path(path, root, regular=True, optional=optional)
    if info is None:
        return None
    sha = hashlib.sha256()
    with Path(path).open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise WorkBuddyStoreError("workbuddy_file_changed_during_read")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    after = plain_path(path, root, regular=True)
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity:
        raise WorkBuddyStoreError("workbuddy_file_changed_during_read")
    return {"path": str(Path(path).absolute()), "sha256": sha.hexdigest(), "size": info.st_size,
            "identity": [info.st_dev, info.st_ino], "mtime_ns": info.st_mtime_ns, "nlink": info.st_nlink}


def _sql(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().rstrip(";")


def schema_fingerprint(db):
    actual = [list(row) for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    normalize = lambda rows: [[*row[:3], _sql(row[3])] for row in rows]
    if normalize(actual) != normalize(_EXPECTED["objects"]):
        raise WorkBuddyStoreError("workbuddy_schema_unknown")
    for table, expected in _EXPECTED["columns"].items():
        actual_columns = [list(row) for row in db.execute(f'PRAGMA table_info("{table}")')]
        if actual_columns != expected or list(db.execute(f'PRAGMA foreign_key_list("{table}")')):
            raise WorkBuddyStoreError("workbuddy_schema_unknown")
    return digest(normalize(actual))


def _sqlite_digest_file(path):
    """Hash SQLite pages without reading cells or nondeterministic counters.

    Backups include committed WAL pages. The two journal-mode bytes and the
    change/version counters differ between a live WAL store and a private
    simulation; they do not describe record content or schema.
    """
    sha = hashlib.sha256()
    with Path(path).open("rb") as handle:
        header = bytearray(handle.read(100))
        if len(header) != 100 or header[:16] != b"SQLite format 3\0":
            raise WorkBuddyStoreError("workbuddy_database_unreadable")
        header[18:20] = b"\0" * 2
        header[24:28] = b"\0" * 4
        header[92:100] = b"\0" * 8
        sha.update(header)
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _backup(db, path):
    with closing(sqlite3.connect(path)) as target:
        db.backup(target)


def database_digest(db):
    with tempfile.TemporaryDirectory(prefix="larj-workbuddy-probe-") as directory:
        path = Path(directory) / "snapshot.sqlite"
        _backup(db, path)
        return _sqlite_digest_file(path)


def _bounded_rows(db, sql):
    rows = db.execute(sql).fetchmany(MAX_ENTRIES + 1)
    if len(rows) > MAX_ENTRIES:
        raise WorkBuddyStoreError("workbuddy_discovery_limit_exceeded")
    return rows


def _database_snapshot(root):
    database = root / "workbuddy.db"
    identity = fingerprint(database, root)
    with closing(connect_readonly(database)) as db:
        db.execute("BEGIN")
        schema = schema_fingerprint(db)
        rows = {row[0]: dict(zip(META_COLUMNS, row)) for row in _bounded_rows(db,
            "SELECT " + ",".join(META_COLUMNS) + " FROM sessions ORDER BY id")}
        usage = {row[0]: dict(zip(("session_id", "used", "size", "updated_at"), row)) for row in _bounded_rows(db,
            "SELECT session_id,used,size,updated_at FROM session_usage ORDER BY session_id")}
        dependencies = {
            "automation_runs": [row[0] for row in _bounded_rows(db, "SELECT thread_id FROM automation_runs ORDER BY thread_id")],
            "automation_runtime_state": [row[0] for row in _bounded_rows(db,
                "SELECT running_conversation_id FROM automation_runtime_state WHERE running_conversation_id IS NOT NULL")],
            "automation_delivery_outbox": [row[0] for row in _bounded_rows(db, "SELECT run_id FROM automation_delivery_outbox ORDER BY run_id")],
        }
        snapshot_sha = database_digest(db)
    if fingerprint(database, root) != identity:
        raise WorkBuddyStoreError("workbuddy_database_changed_during_read")
    for row in rows.values():
        string_fields = ("cwd", "status", "transport", "source_mode", "mode", "project_id", "conversation_origin", "visibility", "group_id", "buddy_snapshot_id")
        integer_fields = ("created_at", "updated_at", "last_activity_at", "deleted_at", "is_playground", "is_background_automation",
                          "agent_dirty", "agent_dirty_at", "agent_last_synced", "verified_at", "unread")
        if (not valid_id(row["id"]) or not valid_id(row["user_id"])
                or any(not _scalar(row[field], str, nullable=field not in ("cwd", "status", "transport")) for field in string_fields)
                or any(not _scalar(row[field], int, nullable=field not in ("created_at", "updated_at")) for field in integer_fields)):
            raise WorkBuddyStoreError("workbuddy_session_metadata_unknown")
        if any(isinstance(row[field], str) and (len(row[field]) > 4096 or "\x00" in row[field]) for field in string_fields):
            raise WorkBuddyStoreError("workbuddy_session_metadata_unknown")
        if (any(row[field] is not None and not 0 <= row[field] <= 2**63 - 1 for field in integer_fields)
                or any(row[field] not in (None, 0, 1) for field in ("is_playground", "is_background_automation", "agent_dirty", "unread"))):
            raise WorkBuddyStoreError("workbuddy_session_metadata_unknown")
    for item in usage.values():
        if (not valid_id(item["session_id"])
                or any(not _scalar(item[field], int, nullable=False) or not 0 <= item[field] <= 2**63 - 1
                       for field in ("used", "size", "updated_at"))):
            raise WorkBuddyStoreError("workbuddy_usage_metadata_unknown")
    if any(not _scalar(value, str) or (isinstance(value, str) and (len(value) > 4096 or "\x00" in value))
           for values in dependencies.values() for value in values):
        raise WorkBuddyStoreError("workbuddy_dependency_metadata_unknown")
    sidecars = {str(database) + suffix: fingerprint(Path(str(database) + suffix), root, optional=True)
                for suffix in ("-wal", "-shm", "-journal")}
    shm = sidecars[str(database) + "-shm"]
    if shm is not None:
        # SQLite readers update read marks/mtime themselves. This is a lock
        # coordination file, not committed record data. Retain ordinary-file
        # identity and size; canonical database/WAL fingerprints protect data.
        sidecars[str(database) + "-shm"] = {key: value for key, value in shm.items() if key not in ("sha256", "mtime_ns")}
    return {"rows": rows, "usage": usage, "dependencies": dependencies, "schema_sha256": schema,
            "database_sha256": snapshot_sha, "database_file": identity, "database_sidecars": sidecars}


def _ui_paths(root, user_ids):
    users = set(user_ids)
    sources = set()
    for path in entries(root):
        if valid_id(path.name):
            plain_path(path, root)
            users.add(path.name)
    for user_id in users:
        path = root / user_id / "sidebar-list-snapshot.json"
        if path.parent.exists():
            sources.add((path, "sidebar"))
    for directory in entries(root / "storage", optional=True):
        match = re.fullmatch(r"user-([0-9a-f-]{36})(?:-[A-Za-z0-9_-]+)?", directory.name)
        if not match or not valid_id(match[1]):
            continue  # Settings/plugins storage is not a conversation source.
        plain_path(directory, root)
        sources.add((directory / "conversations.json", "pinned"))
        global_directory = directory / "global"
        if global_directory.exists():
            plain_path(global_directory, root)
            sources.add((global_directory / "conversations.json", "pinned"))
    return tuple(sorted(sources, key=lambda item: str(item[0])))


def _read_ui(path, root, role):
    info = fingerprint(path, root, optional=True)
    if info is None:
        return {"path": str(path), "role": role, "file": None, "references": []}
    if info["size"] > MAX_JSON_BYTES:
        raise WorkBuddyStoreError("workbuddy_ui_schema_unknown")
    try:
        value = _strict_json(path.read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise WorkBuddyStoreError("workbuddy_ui_unreadable") from exc
    references = []
    if role == "sidebar":
        if (not isinstance(value, dict) or set(value) != {"version", "savedAt", "items"}
                or type(value["version"]) is not int or value["version"] != 1
                or not _scalar(value["savedAt"], (str, int), nullable=False) or not isinstance(value["items"], list)):
            raise WorkBuddyStoreError("workbuddy_ui_schema_unknown")
        if len(value["items"]) > MAX_ENTRIES:
            raise WorkBuddyStoreError("workbuddy_discovery_limit_exceeded")
        for index, item in enumerate(value["items"]):
            if not isinstance(item, dict) or not set(item) <= SIDEBAR_KEYS or not valid_id(item.get("id")):
                raise WorkBuddyStoreError("workbuddy_ui_schema_unknown")
            if (any(not _scalar(item.get(key), str) for key in ("state", "transport", "kind", "conversationOrigin"))
                    or any(not _scalar(item.get(key), (int, str)) for key in ("lastActivityAt", "updatedAt"))):
                raise WorkBuddyStoreError("workbuddy_ui_metadata_unknown")
            space = item.get("space")
            if space is not None and (not isinstance(space, dict) or not set(space) <= {"type", "name"}):
                raise WorkBuddyStoreError("workbuddy_ui_schema_unknown")
            references.append({"id": item["id"], "locator": f"items/{index}",
                "state": item.get("state"), "transport": item.get("transport"),
                "kind": item.get("kind"), "lastActivityAt": item.get("lastActivityAt"),
                "updatedAt": item.get("updatedAt"), "conversationOrigin": item.get("conversationOrigin")})
    else:
        if not isinstance(value, dict) or set(value) != {"pinned"} or not isinstance(value["pinned"], list):
            raise WorkBuddyStoreError("workbuddy_ui_schema_unknown")
        if len(value["pinned"]) > MAX_ENTRIES:
            raise WorkBuddyStoreError("workbuddy_discovery_limit_exceeded")
        for index, item in enumerate(value["pinned"]):
            if (not isinstance(item, dict) or set(item) != {"id", "groupKey"} or not valid_id(item.get("id"))
                    or not isinstance(item.get("groupKey"), str)):
                raise WorkBuddyStoreError("workbuddy_ui_schema_unknown")
            references.append({"id": item["id"], "locator": f"pinned/{index}"})
    if fingerprint(path, root) != info:
        raise WorkBuddyStoreError("workbuddy_ui_changed_during_read")
    return {"path": str(path), "role": role, "file": info, "references": references}


def _artifact_inventory(root):
    result, blockers, directories = {}, {}, {}
    total = 0

    def add(session_id, path, code=None):
        nonlocal total
        total += 1
        if total > MAX_ENTRIES:
            raise WorkBuddyStoreError("workbuddy_discovery_limit_exceeded")
        result.setdefault(session_id, []).append(fingerprint(path, root))
        if code:
            blockers.setdefault(session_id, []).append(code)

    for project in entries(root / "projects", optional=True):
        plain_path(project, root)
        for path in entries(project):
            session_id = path.name[:36]
            if not valid_id(session_id):
                # An unknown project artifact cannot silently turn an empty
                # database into successful complete-store discovery.
                raise WorkBuddyStoreError("workbuddy_unidentified_project_artifact")
            if path.name == session_id:
                plain_path(path, root)
                blockers.setdefault(session_id, []).append("workbuddy_subagent_closure_unproven")
                result.setdefault(session_id, [])
                directories.setdefault(session_id, []).append({"path": str(path), "entry_count": len(entries(path))})
                continue
            if path.name in {session_id + suffix for suffix in ARTIFACT_SUFFIXES}:
                add(session_id, path)
            elif path.name == session_id + ".file-rollback.ndjson":
                add(session_id, path, "workbuddy_unproven_rollback_sidecar")
            elif path.name == session_id + ".quickask":
                add(session_id, path, "workbuddy_quickask_contents_unproven" if path.lstat().st_size else None)
            else:
                add(session_id, path, "workbuddy_artifact_kind_unproven")
    for name in ("artifact-index", "file-tree-manifests", "media-index"):
        for path in entries(root / name, optional=True):
            session_id = path.name.removesuffix(".json")
            if not valid_id(session_id) or path.suffix != ".json":
                raise WorkBuddyStoreError("workbuddy_unidentified_session_artifact")
            add(session_id, path)
    if entries(root / "media-index-workspace", optional=True):
        raise WorkBuddyStoreError("workbuddy_shared_media_index_unproven")
    if (root / "session-artifacts.json").exists():
        fingerprint(root / "session-artifacts.json", root)
        raise WorkBuddyStoreError("workbuddy_session_artifacts_source_unproven")
    return result, blockers, directories


def _sync_snapshot(root):
    results, linked = [], set()
    columns = {"edge_sync_mapping": {"session_id", "conversation_id", "msg_channel", "created_at"},
               "edge_sync_image_mapping": {"blob_id", "cos_uri", "session_id", "created_at"},
               "edge_sync_artifact_cache": {"file_path", "mtime_ms", "size", "file_hash", "download_url", "smh_path",
                                             "content_type", "expires_at", "uploaded_at"}}
    for path in entries(root):
        if not path.name.startswith("edge-sync-mapping") or path.name.endswith(("-wal", "-shm", "-journal")):
            continue
        if path.name not in {"edge-sync-mapping.db", *(f"edge-sync-mapping-v{index}.db" for index in (2, 3, 4))}:
            raise WorkBuddyStoreError("workbuddy_sync_schema_unknown")
        identity = fingerprint(path, root)
        with closing(connect_readonly(path)) as db:
            db.execute("BEGIN")
            objects = list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
            if {row[1] for row in objects if row[0] == "table"} != set(columns) or any(
                    row[0] in {"trigger", "view"} for row in objects):
                raise WorkBuddyStoreError("workbuddy_sync_schema_unknown")
            for table, expected in columns.items():
                if {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')} != expected:
                    raise WorkBuddyStoreError("workbuddy_sync_schema_unknown")
            values = {row[0] for table in ("edge_sync_mapping", "edge_sync_image_mapping")
                      for row in _bounded_rows(db, f'SELECT session_id FROM "{table}"')}
            if any(not valid_id(value) for value in values):
                raise WorkBuddyStoreError("workbuddy_sync_metadata_unknown")
            ids = sorted(values)
            linked.update(ids)
            count = db.execute("SELECT COUNT(*) FROM edge_sync_artifact_cache").fetchone()[0]
            results.append({"path": str(path), "file": identity, "session_ids": ids,
                            "artifact_cache_count": count, "database_sha256": database_digest(db)})
        if fingerprint(path, root) != identity:
            raise WorkBuddyStoreError("workbuddy_sync_store_changed_during_read")
    return results, linked


def snapshot(root):
    root = local_root(root)
    plain_path(root, root)
    db = _database_snapshot(root)
    ui = [_read_ui(path, root, role) for path, role in _ui_paths(root, {r["user_id"] for r in db["rows"].values()})]
    artifacts, artifact_blockers, artifact_directories = _artifact_inventory(root)
    sync, sync_ids = _sync_snapshot(root)
    references = {}
    for source in ui:
        for item in source["references"]:
            references.setdefault(item["id"], []).append({"source": source["path"], "role": source["role"], **item})
    records = {}
    for session_id, row in db["rows"].items():
        codes = list(artifact_blockers.get(session_id, ()))
        if row["transport"] != "local" or row["conversation_origin"] not in (None, "local"):
            codes.append("workbuddy_remote_or_unknown_transport")
        if row["status"].casefold() not in TERMINAL:
            codes.append("workbuddy_session_not_terminal")
        if any(session_id in values for values in db["dependencies"].values()):
            codes.append("workbuddy_session_dependency_unproven")
        if session_id in sync_ids:
            codes.append("workbuddy_remote_sync_reference_unproven")
        record_refs = references.get(session_id, [])
        if any(item.get("transport") not in (None, "local") or item.get("conversationOrigin") not in (None, "local")
               for item in record_refs):
            codes.append("workbuddy_remote_or_unknown_ui_reference")
        # A file in another profile/project is not deduplicated by naked ID.
        record_files = sorted(artifacts.get(session_id, []), key=lambda value: value["path"])
        records[session_id] = {"session_id": session_id, "row": row, "usage": db["usage"].get(session_id),
            "artifacts": record_files, "artifact_directories": artifact_directories.get(session_id, []),
            "references": record_refs, "blocker_codes": sorted(set(codes)), "source_kind": "database"}
    for session_id in sorted((set(artifacts) | set(artifact_blockers) | set(references) | set(db["usage"])) - set(records)):
        records[session_id] = {"session_id": session_id, "row": None, "usage": db["usage"].get(session_id),
            "artifacts": sorted(artifacts.get(session_id, []), key=lambda value: value["path"]),
            "artifact_directories": artifact_directories.get(session_id, []),
            "references": references.get(session_id, []),
            "blocker_codes": ["workbuddy_record_identity_unproven", *artifact_blockers.get(session_id, ())],
            "source_kind": "orphan_artifact_or_ui_reference"}
    return {"root": str(root), "database": str(root / "workbuddy.db"), "database_snapshot": db,
            "ui_sources": ui, "sync_stores": sync, "records": records,
            "coverage": {"supported_schema": "WorkBuddy 5.6.2", "remote_delete": False,
                "content_addressed_attachment_blobs": "not_probed", "sandbox_work_products": "preserved",
                "logs_traces_audit": "not_conversation_stores", "unproven_artifacts": "inventory_only"}}


def evidence_for(observation, record):
    db = observation["database_snapshot"]
    return {"schema_version": SCHEMA, "root": observation["root"], "database": observation["database"],
            "session_id": record["session_id"], "row": record["row"], "usage": record["usage"],
            "schema_sha256": db["schema_sha256"], "database_sha256": db["database_sha256"],
            "database_file": db["database_file"], "database_sidecars": db["database_sidecars"],
            "artifacts": record["artifacts"], "references": record["references"],
            "ui_sources": observation["ui_sources"], "sync_stores": observation["sync_stores"],
            "shared_paths": [observation["database"], *(source["path"] for source in observation["ui_sources"]
                              if any(item["id"] == record["session_id"] for item in source["references"]))]}


def bind_batch(evidence):
    """Bind reproducible whole-batch after fingerprints into authorization."""
    root, ids = _validated_evidence(evidence)
    with tempfile.TemporaryDirectory(prefix="larj-workbuddy-plan-") as temporary:
        backup = Path(temporary) / "before.sqlite"
        with closing(connect_readonly(root / "workbuddy.db")) as db:
            db.execute("BEGIN")
            if schema_fingerprint(db) != evidence[0]["schema_sha256"]:
                raise WorkBuddyStoreError("workbuddy_frozen_state_changed")
            _backup(db, backup)
        if _sqlite_digest_file(backup) != evidence[0]["database_sha256"]:
            raise WorkBuddyStoreError("workbuddy_frozen_state_changed")
        after_database = _expected_after(backup, evidence)
    ui_after = {}
    for source in evidence[0]["ui_sources"]:
        if source["file"] is None:
            ui_after[source["path"]] = None
            continue
        original = Path(source["path"]).read_bytes()
        if hashlib.sha256(original).hexdigest() != source["file"]["sha256"]:
            raise WorkBuddyStoreError("workbuddy_frozen_ui_changed")
        after, count = _clean_json_bytes(original, source["role"], ids)
        if count != sum(item["id"] in ids for item in source["references"]):
            raise WorkBuddyStoreError("workbuddy_frozen_ui_changed")
        ui_after[source["path"]] = hashlib.sha256(after).hexdigest()
    fields = {"batch_selected_ids": sorted(ids), "after_database_sha256": after_database, "after_ui_sha256": ui_after}
    return [{**item, **fields} for item in evidence]


def freeze(root, session_ids):
    observation = snapshot(root)
    selected = []
    for sid in session_ids:
        record = observation["records"].get(sid)
        if not record or record["blocker_codes"]:
            raise WorkBuddyStoreError("workbuddy_record_boundary_unproven")
        selected.append(evidence_for(observation, record))
    return bind_batch(selected)


def freeze_actions(actions):
    groups = {}
    for action in actions:
        groups.setdefault(action.impact.external_storage_root, []).append(action)
    result = {}
    for group in groups.values():
        evidence = bind_batch([action.impact.external_action_payload["workbuddy_session_evidence"] for action in group])
        by_id = {item["session_id"]: item for item in evidence}
        for action in group:
            item = by_id[action.target.thread_id]
            impact = replace(action.impact, external_action_payload={**action.impact.external_action_payload,
                             "workbuddy_session_evidence": item})
            result[action.action_id] = replace(action, impact=impact, snapshot_fingerprint=digest(item))
    return tuple(result[action.action_id] for action in actions)


def select_candidates(context, scope, blocker):
    """WorkBuddy's exact-ID and soft-deleted-project selection contract."""
    from .record_identity import resolve_project_selector, ProjectSelectionError
    errors = [blocker(code, code, scope="workbuddy_inventory") for code in context.plan.errors]
    actions = tuple(action for action in context.plan.actions
                    if not scope.get("engines") or "workbuddy" in scope["engines"])
    wanted = set(scope.get("record_ids", ()))
    if wanted:
        selected = tuple(action for action in actions if action.target.thread_id in wanted)
        found = {action.target.thread_id for action in selected}
        errors.extend(blocker("record_not_found", "WorkBuddy requires an exact full session ID", scope="selection") for _ in wanted - found)
    else:
        deleted = tuple(action for action in actions if not action.requires_explicit_selection)
        if scope.get("all_projects"):
            selected = tuple(action for action in deleted if action.impact.external_action_payload.get("cwd"))
            if any(action.impact.external_action_payload["workbuddy_session_evidence"]["row"] is None
                   or (not action.requires_explicit_selection and not action.impact.external_action_payload.get("cwd"))
                   for action in actions):
                errors.append(blocker("workbuddy_inventory_coverage_unproven", "Unattributed WorkBuddy artifacts or UI-only IDs remain", scope="all_projects"))
        else:
            project_map = {project.stable_id: project for action in actions
                if (cwd := action.impact.external_action_payload.get("cwd"))
                for project in (ProjectKey.from_path("workbuddy", cwd),)}
            selected_projects = set()
            for selector in scope.get("projects", ()):
                try:
                    selected_projects.add(resolve_project_selector(project_map.values(), selector, client="workbuddy").stable_id)
                except ProjectSelectionError as exc:
                    errors.append(blocker("ambiguous_project" if exc.matches else "project_not_found", str(exc), scope="selection"))
            selected = tuple(action for action in deleted
                if (cwd := action.impact.external_action_payload.get("cwd"))
                and ProjectKey.from_path("workbuddy", cwd).stable_id in selected_projects)
    for action in selected:
        if not action.available:
            for code in (action.unavailable_reason or "workbuddy_record_boundary_unproven").split(", "):
                errors.append(blocker(code, code, scope="action:" + action.action_id, action_id=action.action_id))
    executable = tuple(action for action in selected if action.available)
    if executable and not errors:
        executable = freeze_actions(executable)
    if not executable and not errors:
        errors.append(blocker("empty_scope", "No eligible WorkBuddy records matched this scope", scope="selection"))
    return executable, errors


def action_id(root, session_id):
    return KIND + ":" + digest([canonical_path(root), session_id])[:32]


def build_inventory(adapters, *, engines=()):
    from .client_inventory import ClientInventory, ClientTarget
    selected = tuple(adapter for adapter in adapters if isinstance(adapter, WorkBuddyAdapter))
    if not selected:
        raise WorkBuddyStoreError("workbuddy_adapter_missing")
    descriptors, errors, targets, projects, references, scanned = [], [], [], {}, [], []
    for adapter in selected:
        snap = adapter.snapshot_references()
        descriptors.append(snap.descriptor)
        errors.extend(snap.errors)
        references.extend(snap.references)
        observation = adapter.observation
        if observation is None:
            continue
        scanned.append(Path(observation["database"]))
        if engines and "workbuddy" not in engines:
            continue
        store = StoreKey("workbuddy", adapter.profile_root, kind="workbuddy_root")
        for record in observation["records"].values():
            row, sid = record["row"], record["session_id"]
            codes = tuple(record["blocker_codes"])
            capability = EngineCapability("workbuddy", "workbuddy", native_delete=not codes, verify=True,
                blockers=tuple({"blocker_code": code, "scope": "workbuddy_session"} for code in codes),
                reason="Exact verified local WorkBuddy record closure" if not codes else "WorkBuddy record is inventory-only")
            project = ProjectKey.from_path("workbuddy", row["cwd"]) if row and row["cwd"] else None
            if project:
                projects[project.stable_id] = project
            refs = tuple(r for r in snap.references if r.frontend_id == sid)
            targets.append(ClientTarget("workbuddy", "workbuddy", RecordKey(store, sid, kind="workbuddy_session"),
                project, sid, tuple(r.binding_key for r in refs),
                RecordClassification.PARTIAL_REMOTE if "workbuddy_remote_or_unknown_transport" in codes else
                RecordClassification.HEALTHY if row else RecordClassification.UNVERIFIED,
                capability, action_ids=(action_id(adapter.profile_root, sid),) if not codes else (),
                blocker_codes=codes, blockers=capability.blockers, references=refs,
                frontend_binding_keys=tuple(r.binding_key for r in refs),
                record_metadata={"session": row, "usage": record["usage"], "source_kind": record["source_kind"],
                    "artifact_count": len(record["artifacts"]), "artifacts": record["artifacts"],
                    "artifact_directory_count": len(record["artifact_directories"]), "artifact_directories": record["artifact_directories"],
                    "ui_reference_count": len(record["references"]), "coverage": observation["coverage"]}))
    engine_names = tuple(engines) or ("workbuddy",)
    return ClientInventory(client="workbuddy", engines=engine_names, projects=tuple(projects.values()), records=(),
        frontend_sessions=(), unmapped_frontend_sessions=(), targets=tuple(targets),
        capabilities={engine: EngineCapability("workbuddy", engine, native_delete=engine == "workbuddy") for engine in engine_names},
        errors=tuple(errors), descriptors=tuple(descriptors), references=tuple(references), scanned_databases=tuple(scanned),
        scanned_resources=tuple((canonical_path(path), "workbuddy_sessions") for path in scanned))


class WorkBuddyAdapter:
    """Typed independent store; never inherits a Codex or Claude home."""
    name = "workbuddy"
    inventory_engines = ("workbuddy",)

    def __init__(self, *, profile_root):
        self.profile_root = local_root(profile_root)
        self.owner_process_root = self.profile_root
        self.observation = None
        self._snapshot = None

    def describe_client(self):
        return self.snapshot_references().descriptor

    def snapshot_references(self, *, refresh=False):
        if self._snapshot is not None and not refresh:
            return self._snapshot
        store = StoreKey("workbuddy", self.profile_root, kind="workbuddy_root")
        sources, refs, failures = [self.profile_root / "workbuddy.db"], [], []
        try:
            self.observation = snapshot(self.profile_root)
            for source in self.observation["ui_sources"]:
                sources.append(Path(source["path"]))
                for item in source["references"]:
                    row = self.observation["database_snapshot"]["rows"].get(item["id"])
                    binding = digest(["workbuddy", str(self.profile_root), source["path"], item["locator"], item["id"]])
                    refs.append(ClientReference("workbuddy", Path(source["path"]), item["id"], item["id"], "workbuddy", "workbuddy",
                        binding, native_record=RecordKey(store, item["id"], kind="workbuddy_session") if row else None,
                        kind=ReferenceKind.DESKTOP_CATALOG,
                        lifecycle=ReferenceLifecycle.DELETED if row and (row["deleted_at"] or 0) > 0 else ReferenceLifecycle.UNKNOWN,
                        source_locator=item["locator"], evidence_complete=row is not None))
        except (OSError, ValueError, sqlite3.Error, WorkBuddyStoreError) as exc:
            self.observation = None
            failures.append(SourceFailure(str(sources[0]), getattr(exc, "kind", "workbuddy_inventory_unreadable"),
                profile_root=self.profile_root, database=sources[0], store=store, error_type="WorkBuddyInventoryIncomplete"))
        descriptor = ClientDescriptor("workbuddy", profile_root=self.profile_root, sources=tuple(sources), native_stores=(store,),
            owner_process_root=self.profile_root, inventory_engines=("workbuddy",),
            capability_limits=(EngineCapability("workbuddy", "workbuddy", native_delete=True, verify=True),))
        self._snapshot = ReferenceSnapshot(descriptor, tuple(refs), tuple(failures))
        return self._snapshot

    def invalidate_frontend_snapshot(self):
        self._snapshot = None
        self.observation = None

    def native_catalog_for(self, engine):
        return None  # This store has its own records, never a native CLI alias.

    def registered_capability(self, engine):
        return EngineCapability("workbuddy", engine, native_delete=engine == "workbuddy", verify=True)

    def inspect_runtime(self):
        from .workbuddy_runtime import probe
        try:
            return probe(self.profile_root)
        except Exception as exc:
            return {"owner_client": "workbuddy", "owner_process_root": str(self.profile_root),
                "probe_complete": False, "coverage_complete": False, "clients_closed": None,
                "errors": [getattr(exc, "kind", "workbuddy_writer_coverage_unknown")]}


def build_context(adapters, service, *, engines=(), refresh=False):
    from .cleaner import ScanReport
    from .planning import ActionImpact, ActionKind, CandidateAction, ScanStatus, StorageLocation, TargetRef, RiskLevel, storage_id_for_path
    selected = tuple(adapter for adapter in adapters if isinstance(adapter, WorkBuddyAdapter))
    if refresh:
        for adapter in selected:
            adapter.invalidate_frontend_snapshot()
    inventory = build_inventory(selected, engines=engines)
    context = service.prepare_report(ScanReport(), active_adapters=selected, platforms=("workbuddy",))
    actions, storages = [], []
    for adapter in selected:
        storages.append(StorageLocation(storage_id_for_path(adapter.profile_root), "WorkBuddy profile", adapter.profile_root,
            scan_status=ScanStatus.OK if adapter.observation else ScanStatus.FAILED))
        observation = adapter.observation
        if observation is None or (engines and "workbuddy" not in engines):
            continue
        for record in observation["records"].values():
            row, sid, codes = record["row"], record["session_id"], record["blocker_codes"]
            evidence = evidence_for(observation, record)
            actions.append(CandidateAction(action_id(adapter.profile_root, sid), ActionKind.DELETE_WORKBUDDY_SESSION,
                TargetRef(storage_id_for_path(adapter.profile_root), sid), RiskLevel.HIGH if not codes else RiskLevel.BLOCKED,
                available=not codes, unavailable_reason=", ".join(codes) if codes else None,
                impact=ActionImpact(index_record_count=int(row is not None), affected_thread_ids=(sid,),
                    owner_client="workbuddy", owner_process_root=str(adapter.profile_root),
                    resource_path=observation["database"], external_storage_root=str(adapter.profile_root),
                    external_engine="workbuddy", external_artifact_paths=tuple(item["path"] for item in record["artifacts"]),
                    frontend_reference_count=len(record["references"]), frontend_references_preserved=False,
                    external_action_payload={"cwd": row["cwd"] if row else None, "workbuddy_session_evidence": evidence}),
                snapshot_fingerprint=digest(evidence), resource_kind="workbuddy_session",
                requires_explicit_selection=bool(row and not (row["deleted_at"] is not None and row["deleted_at"] > 0))))
    plan = replace(context.plan, storages=tuple(storages), actions=tuple(actions), errors=tuple(error.message for error in inventory.errors),
                   plan_fingerprint="workbuddy:v1:" + digest([action.to_dict() for action in actions]))
    return replace(context, plan=plan, actions=service.typed_actions(plan), frontend_scan_coverage=inventory.scanned_resources)


def _validated_evidence(evidence):
    if not evidence or any(not isinstance(item, dict) or item.get("schema_version") != SCHEMA for item in evidence):
        raise WorkBuddyStoreError("workbuddy_frozen_evidence_missing")
    roots = {item["root"] for item in evidence}
    ids = {item["session_id"] for item in evidence}
    if len(roots) != 1 or len(ids) != len(evidence) or any(not valid_id(value) for value in ids):
        raise WorkBuddyStoreError("workbuddy_frozen_scope_invalid")
    root = local_root(next(iter(roots)))
    common = ("database", "schema_sha256", "database_sha256", "database_file", "database_sidecars", "ui_sources", "sync_stores")
    first = evidence[0]
    if first["database"] != str(root / "workbuddy.db") or any(
            any(item[key] != first[key] for key in common) for item in evidence):
        raise WorkBuddyStoreError("workbuddy_frozen_scope_invalid")
    if "batch_selected_ids" in first:
        if any(set(item.get("batch_selected_ids", ())) != ids or item.get("after_database_sha256") != first.get("after_database_sha256")
               or item.get("after_ui_sha256") != first.get("after_ui_sha256") for item in evidence):
            raise WorkBuddyStoreError("workbuddy_frozen_batch_changed")
    for item in evidence:
        if (not isinstance(item.get("row"), dict) or item["row"].get("id") != item["session_id"]
                or item["row"].get("transport") != "local"):
            raise WorkBuddyStoreError("workbuddy_frozen_identity_invalid")
        for artifact in item["artifacts"]:
            path = Path(artifact["path"])
            try:
                parts = path.relative_to(root).parts
            except ValueError as exc:
                raise WorkBuddyStoreError("workbuddy_path_escape") from exc
            project_artifact = (len(parts) == 3 and parts[0] == "projects"
                and parts[-1] in {item["session_id"] + suffix for suffix in (*ARTIFACT_SUFFIXES, ".quickask")})
            index_artifact = (len(parts) == 2 and parts[0] in ("artifact-index", "file-tree-manifests", "media-index")
                and parts[-1] == item["session_id"] + ".json")
            if not (project_artifact or index_artifact):
                raise WorkBuddyStoreError("workbuddy_frozen_artifact_path_invalid")
            plain_path(artifact["path"], root, regular=True, optional=True)
        for source in item["ui_sources"]:
            # Frozen paths cannot become arbitrary JSON write targets.
            path = Path(source["path"])
            relative = path.relative_to(root)
            if source["role"] == "sidebar":
                if len(relative.parts) != 2 or not valid_id(relative.parts[0]) or relative.parts[1] != "sidebar-list-snapshot.json":
                    raise WorkBuddyStoreError("workbuddy_frozen_ui_path_invalid")
            elif source["role"] == "pinned":
                if (len(relative.parts) not in (3, 4) or relative.parts[0] != "storage"
                        or relative.parts[-1] != "conversations.json"
                        or (len(relative.parts) == 4 and relative.parts[-2] != "global")
                        or not re.fullmatch(r"user-[0-9a-f-]{36}(?:-[A-Za-z0-9_-]+)?", relative.parts[1])):
                    raise WorkBuddyStoreError("workbuddy_frozen_ui_path_invalid")
            else:
                raise WorkBuddyStoreError("workbuddy_frozen_ui_path_invalid")
    return root, ids


def recovery_directory(evidence):
    root, ids = _validated_evidence(evidence)
    binding = digest([str(root), sorted(ids), sorted(digest(item) for item in evidence)])
    return root / (".larj-workbuddy-sessions-" + binding)


def _clean_json_bytes(original, role, ids):
    value = _strict_json(original)
    field = "items" if role == "sidebar" else "pinned"
    before = value[field]
    value[field] = [item for item in before if item["id"] not in ids]
    removed = len(before) - len(value[field])
    if not removed:
        return original, 0
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n", removed


def _delete_rows(db, evidence):
    session_count = usage_count = 0
    for item in sorted(evidence, key=lambda item: item["session_id"]):
        sid = item["session_id"]
        affected = db.execute("DELETE FROM session_usage WHERE session_id=?", (sid,)).rowcount
        if affected != int(item["usage"] is not None):
            raise WorkBuddyStoreError("workbuddy_usage_affected_count_mismatch")
        usage_count += affected
        affected = db.execute("DELETE FROM sessions WHERE id=?", (sid,)).rowcount
        if affected != 1:
            raise WorkBuddyStoreError("workbuddy_session_affected_count_mismatch")
        session_count += affected
    return session_count, usage_count


def _expected_after(backup, evidence):
    with tempfile.TemporaryDirectory(prefix="larj-workbuddy-verify-") as temporary:
        expected = Path(temporary) / "expected.sqlite"
        with closing(connect_readonly(backup)) as source:
            if schema_fingerprint(source) != evidence[0]["schema_sha256"]:
                raise WorkBuddyStoreError("workbuddy_recovery_schema_changed")
            _backup(source, expected)
        with closing(sqlite3.connect(expected)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("BEGIN IMMEDIATE")
            _delete_rows(db, evidence)
            db.commit()
        return _sqlite_digest_file(expected)


def _atomic_bytes(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".larj-workbuddy-", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _discard_recovery(directory, expected_names):
    root = directory.parent
    plain_path(directory, root)
    allowed = set(expected_names) | {"database.sqlite-wal", "database.sqlite-shm", "database.sqlite-journal"}
    paths = entries(directory)
    if any(path.name not in allowed for path in paths):
        raise WorkBuddyStoreError("workbuddy_recovery_cleanup_unproven")
    for path in paths:
        plain_path(path, root, regular=True)
    # Manifest is removed last. If cleanup is interrupted after the main copy
    # disappears, its frozen hashes still permit an exact after-state proof.
    for path in sorted(paths, key=lambda path: (path.name == "manifest.json", path.name == "database.sqlite", path.name)):
        path.unlink()
    directory.rmdir()


def _write_private(path, data, identities):
    with path.open("xb") as handle:
        info = os.fstat(handle.fileno())
        identities[path.name] = (info.st_dev, info.st_ino)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _discard_preparation(directory, directory_identity, identities):
    """Remove only files this invocation exclusively created before mutation."""
    root = directory.parent
    info = plain_path(directory, root)
    if (info.st_dev, info.st_ino) != directory_identity:
        raise WorkBuddyStoreError("workbuddy_recovery_preparation_identity_changed")
    paths = entries(directory)
    if any(path.name not in identities for path in paths):
        raise WorkBuddyStoreError("workbuddy_recovery_preparation_files_changed")
    for path in paths:
        info = plain_path(path, root, regular=True)
        if (info.st_dev, info.st_ino) != identities[path.name]:
            raise WorkBuddyStoreError("workbuddy_recovery_preparation_identity_changed")
    for path in paths:
        info = plain_path(directory, root)
        file_info = plain_path(path, root, regular=True)
        if ((info.st_dev, info.st_ino) != directory_identity
                or (file_info.st_dev, file_info.st_ino) != identities[path.name]):
            raise WorkBuddyStoreError("workbuddy_recovery_preparation_identity_changed")
        path.unlink()
    directory.rmdir()


def _prepare_recovery(evidence):
    root, ids = _validated_evidence(evidence)
    directory = recovery_directory(evidence)
    try:
        directory.mkdir()  # Never overwrite unresolved earlier recovery.
    except FileExistsError as exc:
        raise WorkBuddyStoreError("workbuddy_recovery_preparation_unknown", unknown=True) from exc
    info = plain_path(directory, root)
    directory_identity = (info.st_dev, info.st_ino)
    identities = {}
    try:
        backup = directory / "database.sqlite"
        _write_private(backup, b"", identities)
        with closing(connect_readonly(root / "workbuddy.db")) as source:
            _backup(source, backup)
        if _sqlite_digest_file(backup) != evidence[0]["database_sha256"]:
            raise WorkBuddyStoreError("workbuddy_backup_changed")
        manifest = {"schema_version": "larj.workbuddy-rollback.v1",
            "evidence_sha256": digest(sorted(evidence, key=lambda item: item["session_id"])),
            "after_database_sha256": _expected_after(backup, evidence), "json_files": {}}
        if manifest["after_database_sha256"] != evidence[0]["after_database_sha256"]:
            raise WorkBuddyStoreError("workbuddy_frozen_after_state_changed")
        writes, removed_count = [], 0
        for index, source in enumerate(evidence[0]["ui_sources"]):
            count = sum(item["id"] in ids for item in source["references"])
            if not count:
                continue
            path = Path(source["path"])
            original = path.read_bytes()
            after, removed = _clean_json_bytes(original, source["role"], ids)
            if removed != count or hashlib.sha256(original).hexdigest() != source["file"]["sha256"]:
                raise WorkBuddyStoreError("workbuddy_ui_reference_count_changed")
            name = f"ui-{index}.json"
            _write_private(directory / name, original, identities)
            manifest["json_files"][str(path)] = {"backup_name": name, "before_sha256": source["file"]["sha256"],
                                                 "after_sha256": hashlib.sha256(after).hexdigest()}
            writes.append((path, after, source["file"]))
            removed_count += removed
        _write_private(directory / "manifest.json", json.dumps(manifest, sort_keys=True).encode("utf-8"), identities)
        return directory, writes, removed_count, directory_identity, identities
    except Exception as exc:
        # No original-store mutation has been dispatched during preparation.
        # A failed cleanup must retain the operation's shared-store occupancy.
        try:
            _discard_preparation(directory, directory_identity, identities)
        except Exception as cleanup_error:
            raise WorkBuddyStoreError("workbuddy_recovery_preparation_unknown", unknown=True) from cleanup_error
        raise WorkBuddyStoreError(getattr(exc, "kind", "workbuddy_recovery_preparation_failed"), rolled_back=True) from exc


def remaining(evidence, *, terminal_verified=False):
    """Read-only recovery: never resends deletion or restores a shared store."""
    root, ids = _validated_evidence(evidence)
    directory = recovery_directory(evidence)
    current = snapshot(root)
    db = current["database_snapshot"]
    first = evidence[0]
    if "batch_selected_ids" not in first:
        raise WorkBuddyStoreError("workbuddy_frozen_after_evidence_missing")
    if db["schema_sha256"] != first["schema_sha256"]:
        raise WorkBuddyStoreError("workbuddy_recovery_schema_changed")
    # A coordinator may supply this only after validating a durable terminal
    # child receipt against the immutable parent projection. Once temporary
    # rollback is gone, later approved changes to other sessions need not
    # reproduce this operation's entire historical database image. An
    # unfinished/unknown operation always takes the strict proof below.
    if terminal_verified and not directory.exists():
        linked = {sid for source in current["sync_stores"] for sid in source["session_ids"]}
        return sorted(sid for sid in ids if sid in current["records"] or sid in linked)
    # Verification includes current discovery, so newly created copies and UI
    # sources of these exact IDs cannot hide behind frozen path disappearance.
    frozen_artifacts = {item["session_id"]: item["artifacts"] for item in evidence}
    artifact_states = []
    for sid in ids:
        known = {item["path"]: item for item in frozen_artifacts[sid]}
        record = current["records"].get(sid)
        actual = {item["path"]: item for item in record["artifacts"]} if record else {}
        if set(actual) - set(known) or any(item != known[path] for path, item in actual.items()):
            raise WorkBuddyStoreError("workbuddy_recovery_artifacts_changed")
        artifact_states.extend("before" if path in actual else "after" for path in known)
        if record and any(code not in ("workbuddy_record_identity_unproven",) for code in record["blocker_codes"]):
            raise WorkBuddyStoreError("workbuddy_recovery_record_boundary_unproven")
    current_sources = {source["path"]: source for source in current["ui_sources"]}
    frozen_sources = {source["path"]: source for source in first["ui_sources"]}
    for path, source in current_sources.items():
        if path not in frozen_sources and any(item["id"] in ids for item in source["references"]):
            raise WorkBuddyStoreError("workbuddy_recovery_new_ui_reference")
    if current["sync_stores"] != first["sync_stores"]:
        raise WorkBuddyStoreError("workbuddy_recovery_sync_store_changed")
    exists = directory.exists()
    manifest = None
    expected_after = first["after_database_sha256"]
    if exists:
        plain_path(directory, root)
        manifest_path = directory / "manifest.json"
        plain_path(manifest_path, root, regular=True)
        manifest = _strict_json(manifest_path.read_bytes())
        if (manifest.get("schema_version") != "larj.workbuddy-rollback.v1"
                or manifest.get("evidence_sha256") != digest(sorted(evidence, key=lambda item: item["session_id"]))):
            raise WorkBuddyStoreError("workbuddy_recovery_binding_changed")
        backup = directory / "database.sqlite"
        if backup.exists():
            plain_path(backup, root, regular=True)
            if _sqlite_digest_file(backup) != first["database_sha256"]:
                raise WorkBuddyStoreError("workbuddy_recovery_original_unproven")
            expected_after = _expected_after(backup, evidence)
            if expected_after != manifest["after_database_sha256"] or expected_after != first["after_database_sha256"]:
                raise WorkBuddyStoreError("workbuddy_recovery_after_unproven")
        else:
            if manifest["after_database_sha256"] != first["after_database_sha256"]:
                raise WorkBuddyStoreError("workbuddy_recovery_after_unproven")
            for suffix in ("-wal", "-journal"):
                path = directory / ("database.sqlite" + suffix)
                if path.exists() and fingerprint(path, root)["size"]:
                    raise WorkBuddyStoreError("workbuddy_recovery_original_unproven")
    db_before = db["database_sha256"] == first["database_sha256"]
    db_after = db["database_sha256"] == expected_after
    ui_states = []
    for index, source in enumerate(first["ui_sources"]):
        actual = current_sources.get(source["path"])
        if actual is None:
            actual = _read_ui(Path(source["path"]), root, source["role"])
        original = source["file"]
        if original is None:
            if actual["file"] is not None:
                raise WorkBuddyStoreError("workbuddy_recovery_ui_changed")
            continue
        before_hash = original["sha256"]
        actual_hash = actual["file"]["sha256"] if actual["file"] else None
        expected_hash = first["after_ui_sha256"].get(source["path"])
        selected_count = sum(item["id"] in ids for item in source["references"])
        if not selected_count:
            expected_hash = before_hash
        elif manifest is not None:
            entry = manifest["json_files"].get(source["path"])
            if not isinstance(entry, dict) or entry["before_sha256"] != before_hash:
                raise WorkBuddyStoreError("workbuddy_recovery_json_original_unproven")
            if entry["after_sha256"] != expected_hash or not re.fullmatch(r"ui-[0-9]+\.json", str(entry.get("backup_name", ""))):
                raise WorkBuddyStoreError("workbuddy_recovery_json_original_unproven")
            backup = directory / entry["backup_name"]
            if backup.exists():
                plain_path(backup, root, regular=True)
                raw = backup.read_bytes()
                after_bytes, removed = _clean_json_bytes(raw, source["role"], ids)
                if hashlib.sha256(raw).hexdigest() != before_hash or hashlib.sha256(after_bytes).hexdigest() != expected_hash or removed != selected_count:
                    raise WorkBuddyStoreError("workbuddy_recovery_json_original_unproven")
            elif db_before:
                raise WorkBuddyStoreError("workbuddy_recovery_json_original_unproven")
        if actual_hash == before_hash:
            ui_states.append("before")
        elif expected_hash is not None and actual_hash == expected_hash:
            ui_states.append("after")
        else:
            raise WorkBuddyStoreError("workbuddy_recovery_ui_changed")
    required_ui = [source for source in first["ui_sources"] if any(item["id"] in ids for item in source["references"])]
    # Unchanged unrelated JSON is compatible with both before and after.
    all_ui_before = all(current_sources.get(source["path"], {}).get("file") == source["file"] for source in required_ui)
    all_ui_after = all(not any(item["id"] in ids for item in source["references"]) for source in current["ui_sources"])
    full_before = db_before and all_ui_before and all(state == "before" for state in artifact_states)
    full_after = db_after and all_ui_after and all(state == "after" for state in artifact_states)
    if not full_before and not full_after:
        raise WorkBuddyStoreError("workbuddy_recovery_mixed_or_changed_state")
    if exists:
        if not manifest or (not (directory / "database.sqlite").exists() and not full_after):
            raise WorkBuddyStoreError("workbuddy_recovery_original_unproven")
        expected_names = {"manifest.json", "database.sqlite", *(item["backup_name"] for item in manifest["json_files"].values())}
        _discard_recovery(directory, expected_names)
    return sorted(ids) if full_before else []


@dataclass(frozen=True)
class WorkBuddyCleanupResult:
    deleted_ids: tuple[str, ...]
    deleted_session_count: int
    deleted_usage_count: int
    deleted_artifact_count: int
    removed_ui_reference_count: int
    status: str = "deleted"

    def to_dict(self):
        return {"status": self.status, "deleted_ids": list(self.deleted_ids), "verified": True,
                "deleted_session_count": self.deleted_session_count, "deleted_usage_count": self.deleted_usage_count,
                "deleted_artifact_count": self.deleted_artifact_count, "removed_ui_reference_count": self.removed_ui_reference_count,
                "remote_delete": False, "temporary_rollback_retained": False}


def execute(evidence, *, client_inspector=None, phase_callback=None):
    from .workbuddy_runtime import require_closed
    root, ids = _validated_evidence(evidence)
    if "batch_selected_ids" not in evidence[0]:
        raise WorkBuddyStoreError("workbuddy_frozen_after_evidence_missing")
    require_closed(root, client_inspector)
    observation = snapshot(root)
    for item in evidence:
        record = observation["records"].get(item["session_id"])
        original = {key: value for key, value in item.items() if key not in ("batch_selected_ids", "after_database_sha256", "after_ui_sha256")}
        if not record or record["blocker_codes"] or evidence_for(observation, record) != original:
            raise WorkBuddyStoreError("workbuddy_frozen_state_changed")
    directory, writes, removed_count, directory_identity, private_identities = _prepare_recovery(evidence)
    db, committed, started = None, False, False
    try:
        require_closed(root, client_inspector)
        fresh = snapshot(root)
        if any(evidence_for(fresh, fresh["records"][item["session_id"]]) != {
                key: value for key, value in item.items() if key not in ("batch_selected_ids", "after_database_sha256", "after_ui_sha256")}
               for item in evidence):
            raise WorkBuddyStoreError("workbuddy_frozen_state_changed")
        db = sqlite3.connect(root / "workbuddy.db")
        db.execute("BEGIN IMMEDIATE")
        if schema_fingerprint(db) != evidence[0]["schema_sha256"]:
            raise WorkBuddyStoreError("workbuddy_schema_changed")
        # A separate reader sees the committed database under our reserved
        # writer lock. Backing up the write-transaction connection can hang.
        with closing(connect_readonly(root / "workbuddy.db")) as locked_source:
            if database_digest(locked_source) != evidence[0]["database_sha256"]:
                raise WorkBuddyStoreError("workbuddy_database_changed_under_lock")
        require_closed(root, client_inspector)
        if phase_callback:
            phase_callback("mutation_started")
        started = True
        session_count, usage_count = _delete_rows(db, evidence)
        # Exact DB commit precedes file writes. A failure after this point
        # stays unknown until read-only evidence proves the entire closure.
        db.commit()
        committed = True
        db.close()
        db = None
        for path, after, original in writes:
            if fingerprint(path, root) != original:
                raise WorkBuddyStoreError("workbuddy_ui_changed_before_write")
            _atomic_bytes(path, after)
        artifact_count = 0
        for item in evidence:
            for artifact in item["artifacts"]:
                path = Path(artifact["path"])
                if fingerprint(path, root) != artifact:
                    raise WorkBuddyStoreError("workbuddy_artifact_changed_before_unlink")
                path.unlink()
                artifact_count += 1
        if remaining(evidence):
            raise WorkBuddyStoreError("workbuddy_deleted_records_remain")
        if phase_callback:
            phase_callback("verified")
        return WorkBuddyCleanupResult(tuple(sorted(ids)), session_count, usage_count, artifact_count, removed_count)
    except Exception as exc:
        if db is not None:
            try:
                try:
                    db.rollback()
                finally:
                    db.close()
            except Exception as recovery_error:
                raise WorkBuddyStoreError("workbuddy_transaction_recovery_unknown", unknown=True) from recovery_error
        rolled_back = False
        if not committed:
            try:
                rolled_back = set(remaining(evidence)) == ids
            except Exception:
                pass
        if not started and not rolled_back:
            # A later preflight can observe an outside writer's drift. We have
            # not touched original rows/JSON/artifacts, so owned private copies
            # can still be removed without claiming the old store is unchanged.
            try:
                _discard_preparation(directory, directory_identity, private_identities)
                rolled_back = True
            except Exception:
                pass
        unknown = committed or not rolled_back
        code = getattr(exc, "kind", "workbuddy_mutation_failed")
        if unknown and "unknown" not in code.casefold():
            code = "workbuddy_recovery_unknown: " + code
        raise WorkBuddyStoreError(code, rolled_back=rolled_back, unknown=unknown) from exc
