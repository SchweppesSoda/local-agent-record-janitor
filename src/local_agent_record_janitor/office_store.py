"""Body-free inventories for QwenWork CN and QoderWork CN.

office_cleanup qualifies the complete selected closure at plan time; ordinary
inventory never opens Chromium storage or reads conversation bodies.
"""

from __future__ import annotations

from contextlib import closing
from collections import Counter, defaultdict
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time

from .client_contracts import ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle, ReferenceSnapshot, SourceFailure
from .path_identity import is_local_absolute_locator
from .record_identity import EngineCapability, ProjectKey, RecordClassification, RecordKey, StoreKey, canonical_path
from .sqlite_utils import connect_readonly

PROFILES = {"qoderwork": ("QoderWork CN", "QoderWork CN Dev", "QoderWork CN Canary"),
            "qwenwork": ("QwenWorkCN", "QwenWorkCNDev", "QwenWorkCNCanary")}
SDK_DIRECTORY = {"qoderwork": ".qoderworkcn", "qwenwork": ".qwenworkcn"}
SCHEMAS = json.loads(files(__package__).joinpath("office_schemas.json").read_text(encoding="utf-8"))
MAX_ROWS = 20000
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def local_root(value) -> Path:
    raw = os.fspath(Path(value).expanduser())
    if not is_local_absolute_locator(raw) or any(ord(char) < 32 for char in raw):
        raise ValueError("office_local_root_unproven")
    return Path(raw)


def _plain(path: Path, *, directory=False):
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("office_path_redirected")
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError("office_path_redirected")
    return info


def _rows(connection, query):
    rows = connection.execute(query).fetchmany(MAX_ROWS + 1)
    if len(rows) > MAX_ROWS:
        raise ValueError("office_inventory_budget_exceeded")
    return [dict(row) for row in rows]


def _identifier(value, *, nullable=False):
    if value is None and nullable:
        return value
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("office_metadata_identity_invalid")
    return value


def read_database(client: str, root: Path) -> dict:
    database = root / "data" / "agents.db"
    before = _plain(database)
    # Sidecar links are checked before SQLite can follow them. Missing files
    # are observations only; no initialization or immutable=1 shortcuts.
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            _plain(Path(str(database) + suffix))
        except FileNotFoundError:
            pass
    with closing(connect_readonly(database)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        deadline = time.monotonic() + 5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        connection.execute("BEGIN")
        schema = [list(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
        if schema != SCHEMAS[client]["objects"]:
            raise ValueError("office_schema_unverified")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("office_relations_inconsistent")
        projects = _rows(connection, "SELECT id,path,created_at,updated_at FROM projects ORDER BY id")
        chats = _rows(connection, "SELECT id,project_id,created_at,updated_at,archived_at,deleted_at,worktree_path,source,chat_type,source_chat_id FROM chats ORDER BY id")
        children = _rows(connection, "SELECT id,chat_id,session_id,mode,created_at,updated_at,(stream_id IS NOT NULL AND stream_id<>'') AS stream_active FROM sub_chats ORDER BY id")
        counts = _rows(connection, "SELECT chat_id,sub_chat_id,count(*) AS message_count FROM messages GROUP BY chat_id,sub_chat_id")
        remote = _rows(connection, "SELECT chat_id,sub_chat_id FROM rc_session_mappings ORDER BY chat_id,sub_chat_id")
        scheduled = _rows(connection, "SELECT chat_id,sub_chat_id,count(*) AS run_count FROM task_run_logs GROUP BY chat_id,sub_chat_id")
        nudges = _rows(connection, "SELECT sub_chat_id,count(*) AS nudge_count FROM nudge_logs GROUP BY sub_chat_id")
    after = _plain(database)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("office_database_replaced")
    by_project = {_identifier(row["id"]): row for row in projects}
    by_chat = {_identifier(row["id"]): row for row in chats}
    by_child = {_identifier(row["id"]): row for row in children}
    for row in chats:
        if row["project_id"] not in by_project:
            raise ValueError("office_relations_inconsistent")
        if any(row[name] is not None and type(row[name]) is not int for name in ("created_at", "updated_at", "archived_at", "deleted_at")):
            raise ValueError("office_metadata_shape_invalid")
    for row in children:
        if row["chat_id"] not in by_chat:
            raise ValueError("office_relations_inconsistent")
        _identifier(row["session_id"], nullable=True)
        _identifier(row["mode"])
        if any(row[name] is not None and type(row[name]) is not int for name in ("created_at", "updated_at")):
            raise ValueError("office_metadata_shape_invalid")
        row["stream_active"] = bool(row["stream_active"])
    for row in counts:
        if row["chat_id"] not in by_chat or row["sub_chat_id"] not in by_child or by_child[row["sub_chat_id"]]["chat_id"] != row["chat_id"]:
            raise ValueError("office_relations_inconsistent")
    for row in remote:
        if row["sub_chat_id"] not in by_child or by_child[row["sub_chat_id"]]["chat_id"] != row["chat_id"]:
            raise ValueError("office_relations_inconsistent")
    reference_errors = []
    if any(row["sub_chat_id"] is not None and not any(identifier == row["sub_chat_id"] or identifier[:8] == row["sub_chat_id"]
            for identifier in by_child) for row in nudges):
        reference_errors.append("office_nudge_references_unresolved")
    if any((row["chat_id"] is not None and row["chat_id"] not in by_chat)
           or (row["sub_chat_id"] is not None and (row["sub_chat_id"] not in by_child
               or row["chat_id"] is not None and by_child[row["sub_chat_id"]]["chat_id"] != row["chat_id"]))
           for row in scheduled):
        reference_errors.append("office_task_run_references_unresolved")
    return {"database": str(database), "schema_version": SCHEMAS[client]["version"], "edition": "CN",
            "projects": by_project, "chats": by_chat, "sub_chats": children, "message_counts": counts,
            "cloud_references": remote, "task_run_references": scheduled, "nudge_references": nudges,
            "reference_errors": reference_errors}


def sdk_artifacts(root: Path, session_ids: set[str]) -> dict[str, list[dict]]:
    """Bounded filename observations; never parse SDK transcript bodies."""
    result = {identifier: [] for identifier in session_ids}
    try:
        _plain(root, directory=True)
    except FileNotFoundError:
        return result
    visited = 0
    for family, depth in (("projects", 1), ("file-history", 0), ("tmp", 1), ("logs/sessions", 1)):
        base = root / family
        try:
            _plain(base, directory=True)
        except FileNotFoundError:
            continue
        parents = [base]
        if depth:
            parents = []
            for path in base.iterdir():
                visited += 1
                if visited > MAX_ROWS:
                    raise ValueError("office_sdk_inventory_budget_exceeded")
                _plain(path, directory=True)
                parents.append(path)
        for parent in parents:
            for identifier in sorted(session_ids):
                if not _UUID.fullmatch(identifier):
                    continue
                names = ((identifier + ".jsonl", identifier) if family == "projects" else
                         (identifier, "logs/session-" + identifier + ".jsonl", "tool-outputs/session-" + identifier)
                         if family == "tmp" else (identifier,))
                for name in names:
                    visited += 1
                    if visited > MAX_ROWS:
                        raise ValueError("office_sdk_inventory_budget_exceeded")
                    path = parent / name
                    try:
                        info = path.lstat()
                    except FileNotFoundError:
                        continue
                    _plain(path, directory=stat.S_ISDIR(info.st_mode))
                    result[identifier].append({"path": str(path), "family": family,
                        "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file"})
    return result


def default_profile_roots(client: str, appdata=None) -> tuple[Path, ...]:
    if appdata is not None:
        base = local_root(appdata)
    elif os.name == "nt":
        base = local_root(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = local_root(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    roots = []
    for name in PROFILES[client]:
        root = base / name
        try:
            root.lstat()
        except FileNotFoundError:
            continue
        roots.append(root)
    return tuple(roots) or (base / PROFILES[client][0],)


class OfficeAdapter:
    def __init__(self, *, client, profile_root, sdk_root=None):
        if client not in PROFILES:
            raise ValueError("office_product_unsupported")
        self.client = self.name = client
        self.inventory_engines = (client,)
        self.profile_root = local_root(profile_root)
        self.sdk_root = local_root(sdk_root or Path.home() / SDK_DIRECTORY[client])
        self.observation = None
        self._snapshot = None

    def describe_client(self):
        return self.snapshot_references().descriptor

    def snapshot_references(self, *, refresh=False):
        if self._snapshot is not None and not refresh:
            return self._snapshot
        self.observation = None
        source = self.profile_root / "data" / "agents.db"
        store = StoreKey(self.client, self.profile_root, kind="office_profile")
        errors, references = [], []
        try:
            self.observation = read_database(self.client, self.profile_root)
            ids = {row["session_id"] for row in self.observation["sub_chats"] if row["session_id"]}
            if any(not _UUID.fullmatch(identifier) for identifier in ids):
                raise ValueError("office_sdk_identity_unverified")
            self.observation["sdk_artifacts"] = sdk_artifacts(self.sdk_root, ids)
        except (OSError, ValueError, sqlite3.Error) as exc:
            code = str(exc) if str(exc).startswith("office_") else "office_inventory_unreadable"
            errors.append(SourceFailure(str(source), code, profile_root=self.profile_root,
                database=source, store=store, error_type="OfficeInventoryIncomplete"))
        if self.observation is not None:
            errors.extend(SourceFailure(str(source), code, profile_root=self.profile_root, database=source,
                store=store, error_type="OfficeUnassignedReference", blocks_inventory=False)
                for code in self.observation["reference_errors"])
            for row in self.observation["chats"].values():
                binding = hashlib.sha256(json.dumps([self.client, canonical_path(source), row["id"]]).encode()).hexdigest()
                references.append(ClientReference(self.client, source, row["id"], row["id"], self.client, self.client,
                    binding, native_record=RecordKey(store, row["id"], kind="office_chat"),
                    kind=ReferenceKind.CURRENT, lifecycle=ReferenceLifecycle.DELETED if row["deleted_at"] is not None else ReferenceLifecycle.UNKNOWN,
                    source_locator="chats/id/" + row["id"], evidence_complete=not errors))
        descriptor = ClientDescriptor(self.client, profile_root=self.profile_root, sources=(source,), native_stores=(store,),
            owner_process_root=self.profile_root, inventory_engines=(self.client,),
            capability_limits=(EngineCapability(self.client, self.client, native_delete=True, frontend_session_delete=True, verify=True),))
        self._snapshot = ReferenceSnapshot(descriptor, tuple(references), tuple(errors))
        return self._snapshot

    def native_catalog_for(self, engine):
        return None


def build_inventory(adapters, *, client, engines=()):
    from .client_inventory import ClientInventory, ClientTarget
    selected = tuple(a for a in adapters if isinstance(a, OfficeAdapter) and a.client == client)
    if not selected:
        raise ValueError("office_adapter_missing")
    descriptors, errors, references, targets, projects, databases = [], [], [], [], {}, []
    seen_profiles = {}
    capability = EngineCapability(client, client, native_delete=True, frontend_session_delete=True, verify=True,
        reason="Exact-schema local conversation cleanup; complete selected closure is checked by delete plan")
    for adapter in selected:
        snap = adapter.snapshot_references()
        descriptors.append(snap.descriptor)
        errors.extend(snap.errors)
        identity = canonical_path(adapter.profile_root)
        sdk_identity = canonical_path(adapter.sdk_root)
        if identity in seen_profiles:
            if seen_profiles[identity] != sdk_identity:
                errors.append(SourceFailure(str(adapter.profile_root), "office_profile_scope_conflict",
                    profile_root=adapter.profile_root, store=snap.descriptor.native_stores[0],
                    error_type="OfficeInventoryIncomplete"))
            continue
        seen_profiles[identity] = sdk_identity
        references.extend(snap.references)
        observed = adapter.observation
        if observed is None or engines and client not in engines:
            continue
        databases.append(Path(observed["database"]))
        children_by_chat = defaultdict(list)
        child_chat = {}
        for row in observed["sub_chats"]:
            children_by_chat[row["chat_id"]].append(row)
            child_chat[row["id"]] = row["chat_id"]
        message_counts = Counter()
        for row in observed["message_counts"]:
            message_counts[row["chat_id"]] += row["message_count"]
        cloud_counts = Counter(row["chat_id"] for row in observed["cloud_references"])
        run_counts, nudge_counts = Counter(), Counter()
        for row in observed["task_run_references"]:
            for identifier in {row["chat_id"], child_chat.get(row["sub_chat_id"])} - {None}:
                run_counts[identifier] += row["run_count"]
        for row in observed["nudge_references"]:
            owners = {chat for child, chat in child_chat.items() if child == row["sub_chat_id"] or child[:8] == row["sub_chat_id"]}
            for owner in owners:
                nudge_counts[owner] += row["nudge_count"]
        for ref in snap.references:
            chat = observed["chats"][ref.native_id]
            project_row = observed["projects"][chat["project_id"]]
            path = chat["worktree_path"] or project_row["path"]
            project = ProjectKey.from_path(client, path) if isinstance(path, str) and is_local_absolute_locator(path) else None
            if project:
                projects[project.stable_id] = project
            children = children_by_chat[ref.native_id]
            cloud = cloud_counts[ref.native_id]
            targets.append(ClientTarget(client, client, ref.native_record, project, ref.native_id,
                (ref.binding_key,), RecordClassification.PARTIAL_REMOTE if cloud else RecordClassification.UNVERIFIED,
                capability, references=(ref,), frontend_binding_keys=(ref.binding_key,),
                blocker_codes=("office_remote_session_requires_remote_contract",) if cloud else (),
                record_metadata={"schema_version": observed["schema_version"], "edition": "CN",
                    "created_at": chat["created_at"], "updated_at": chat["updated_at"],
                    "archived_at": chat["archived_at"], "deleted_at": chat["deleted_at"],
                    "sub_chats": children,
                    "message_count": message_counts[ref.native_id],
                    "message_count_scope": "normalized_messages_table",
                    "cloud_reference_count": cloud,
                    "task_run_count": run_counts[ref.native_id],
                    "nudge_count": nudge_counts[ref.native_id],
                    "sdk_root": str(adapter.sdk_root),
                    "sdk_artifacts": [artifact for row in children for artifact in observed.get("sdk_artifacts", {}).get(row["session_id"], ())],
                    "coverage": {"scope": "database_metadata_and_known_sdk_paths", "message_bodies_read": False,
                        "full_record_closure": False, "closure_validation": "delete_plan_required", "cloud_delete": False,
                        "preserved_shared_data": ["global_input_history", "global_logs", "shared_memories", "generated_outputs", "account_settings"]}}))
    names = tuple(engines) or (client,)
    return ClientInventory(client=client, engines=names, projects=tuple(projects.values()), records=(), frontend_sessions=(),
        unmapped_frontend_sessions=(), targets=tuple(targets), capabilities={name: EngineCapability(client, name, verify=True,
            native_delete=name == client, frontend_session_delete=name == client,
            reason=capability.reason) for name in names}, errors=tuple(errors), descriptors=tuple(descriptors),
        references=tuple(references), scanned_databases=tuple(databases),
        scanned_resources=tuple((canonical_path(path), "office_chats") for path in databases))
