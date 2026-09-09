"""Exact, body-free cleanup of stale Desktop local-environment registrations.

Only the two named global-state files are writable. Project directories,
native databases, cloud projects and unknown reference schemas are never
modified. Evidence binds whole-file hashes, including an absent backup.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .codex_desktop_state import running_related_clients
from .sqlite_utils import connect_readonly
from .record_identity import canonical_path

KIND = "delete_native_project"
STATE_FILES = (".codex-global-state.json", ".codex-global-state.json.bak")
SCHEMA = "native-local-project.v1"
LIVE_DIRECTORY = "Project directory still exists; registration is inventory-only"


class NativeProjectError(RuntimeError):
    def __init__(self, message: str, *, rolled_back: bool = False, unknown: bool = False):
        super().__init__(message)
        self.outcome_known_rolled_back = rolled_back
        self.outcome_unknown = unknown


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_hash(value: Any) -> str:
    return _hash(json.dumps(value, sort_keys=True, ensure_ascii=False,
                            separators=(",", ":")).encode("utf-8"))


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NativeProjectError("Duplicate JSON keys: unsupported global state")
        result[key] = value
    return result


def _read_files(home: Path) -> dict[str, tuple[bytes, dict[str, Any]] | None]:
    result = {}
    for name in STATE_FILES:
        path = home / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            result[name] = None
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink():
            raise NativeProjectError("Global state must be a regular, unlinked file")
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        if not isinstance(value, dict) or not isinstance(value.get("local-projects", {}), dict):
            raise NativeProjectError("Unsupported local-projects schema")
        result[name] = (raw, value)
    return result


def _contains(value: Any, tokens: Sequence[str]) -> bool:
    if isinstance(value, dict):
        return any(any(t in key for t in tokens) or _contains(v, tokens)
                   for key, v in value.items())
    if isinstance(value, list):
        return any(_contains(v, tokens) for v in value)
    return isinstance(value, str) and any(t in value for t in tokens)


def _remove(value: dict[str, Any], project_id: str, home: Path) -> tuple[dict[str, Any], int, set[str]]:
    """Delete only schema-qualified keys; reject any unexplained reference."""
    result = copy.deepcopy(value)
    count = 0
    if project_id in result.get("local-projects", {}):
        del result["local-projects"][project_id]
        count += 1
    atoms = result.get("electron-persisted-atom-state", {})
    if not isinstance(atoms, dict):
        raise NativeProjectError("Unsupported sidebar state schema")
    for prefix in ("sidebar-project-expanded-v1-chatgpt:", "sidebar-project-expanded-v1-codex:"):
        key = prefix + project_id
        if key in atoms:
            if not isinstance(atoms[key], bool):
                raise NativeProjectError("Unsupported sidebar reference value")
            del atoms[key]
            count += 1
    mappings = result.get("app-server-project-id-by-legacy-project-id-by-host", {})
    if not isinstance(mappings, dict):
        raise NativeProjectError("Unsupported project mapping schema")
    mapped: set[str] = set()
    for host, items in mappings.items():
        if not isinstance(items, dict):
            raise NativeProjectError("Unsupported host mapping schema")
        if project_id not in items:
            continue
        if host != "local:" + str(home):
            raise NativeProjectError("Project has a different host mapping")
        mapped_id = items[project_id]
        if not isinstance(mapped_id, str) or not mapped_id or mapped_id == project_id:
            raise NativeProjectError("Unsupported mapped project ID")
        mapped.add(mapped_id)
        del items[project_id]
        count += 1
    if _contains(result, (project_id, *mapped)):
        raise NativeProjectError("Project has references outside the supported schema")
    return result, count, mapped


def _missing_directory(path: str) -> bool:
    root = Path(path)
    if not root.is_absolute() or ".." in root.parts:
        raise NativeProjectError("Project root must be an absolute local path")
    # Do not treat an offline drive/share or a dangling symlink as a stale root.
    if str(root).startswith(("\\\\", "//")):
        raise NativeProjectError("Network roots are inventory-only")
    if not Path(root.anchor).is_dir():
        raise NativeProjectError("Project volume is unavailable")
    missing = False
    for index, ancestor in enumerate((root, *root.parents)):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            if index == 0:
                missing = True
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise NativeProjectError("Linked or reparse-point project roots are inventory-only")
        if not stat.S_ISDIR(info.st_mode):
            raise NativeProjectError("Project root ancestry is not a directory")
    return missing


def _native_guard(home: Path, ids: set[str], roots: Sequence[str]) -> None:
    """Read only project/thread identifiers; never read message bodies."""
    database = home / "state_5.sqlite"
    if database.is_symlink() or not database.is_file():
        raise NativeProjectError("Native database is unavailable")
    with closing(connect_readonly(database)) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "threads" not in tables:
            raise NativeProjectError("Native thread schema is unavailable")
        for table, column, values in (("projects", "id", ids),
                                      ("project_roots", "project_id", ids),
                                      ("project_roots", "path", roots),
                                      ("threads", "project_id", ids),
                                      ("threads", "cwd", roots)):
            if table not in tables:
                continue
            columns = {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
            if column not in columns:
                if table == "threads" and column == "project_id":
                    continue  # Older native schemas have cwd but no projects.
                raise NativeProjectError("Unproven native project reference schema")
            for item in values:
                if column in {"cwd", "path"}:
                    expected = canonical_path(item).replace("\\", "/").rstrip("/")
                    for row in connection.execute(f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
                        actual = canonical_path(row[0]).replace("\\", "/").rstrip("/")
                        if actual == expected or actual.startswith(expected + "/"):
                            raise NativeProjectError("Native project or thread path references remain")
                    continue
                if connection.execute(f'SELECT 1 FROM "{table}" WHERE "{column}" = ? LIMIT 1', (item,)).fetchone():
                    raise NativeProjectError("Native project or thread references remain")


@dataclass(frozen=True)
class NativeProject:
    home: Path
    project_id: str
    name: str
    roots: tuple[str, ...]
    evidence: Mapping[str, Any]
    blockers: tuple[str, ...] = ()

    @property
    def action_id(self) -> str:
        return KIND + ":" + _json_hash([str(self.home), self.project_id])[:32]

    def to_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "name": self.name,
                "root_paths": list(self.roots), "codex_home": str(self.home),
                "classification": self.classification,
                "available": not self.blockers, "blockers": list(self.blockers),
                "evidence": dict(self.evidence)}

    @property
    def classification(self) -> str:
        if self.blockers == (LIVE_DIRECTORY,):
            return "healthy"
        return "unknown_operation" if self.blockers else "orphan_project"


def discover_native_projects(home: Path) -> tuple[NativeProject, ...]:
    home = home.expanduser().resolve()
    files = _read_files(home)
    hashes = {name: _hash(item[0]) if item else None for name, item in files.items()}
    registrations: dict[str, list[Any]] = {}
    for item in files.values():
        if item:
            for project_id, project in item[1].get("local-projects", {}).items():
                registrations.setdefault(project_id, []).append(project)
    projects = []
    for project_id, copies in sorted(registrations.items()):
        blockers: list[str] = []
        roots: tuple[str, ...] = ()
        name = project_id
        counts = {}
        mapped: set[str] = set()
        try:
            if not project_id or not isinstance(copies[0], dict):
                raise NativeProjectError("Unsupported project registration")
            first = copies[0]
            name = first.get("name", project_id)
            if not isinstance(name, str):
                name = project_id
            allowed = {"id", "name", "rootPaths", "createdAt", "updatedAt"}
            if any(p != first for p in copies) or set(first) - allowed or first.get("id") != project_id:
                raise NativeProjectError("Project copies differ or schema is unsupported")
            raw_roots = first.get("rootPaths")
            if not isinstance(raw_roots, list) or not raw_roots or not all(isinstance(r, str) and r for r in raw_roots):
                raise NativeProjectError("Unsupported project rootPaths")
            roots = tuple(raw_roots)
            if not all(_missing_directory(r) for r in roots):
                raise NativeProjectError(LIVE_DIRECTORY)
            for filename, item in files.items():
                if item:
                    _, count, identifiers = _remove(item[1], project_id, home)
                    counts[filename] = count
                    mapped.update(identifiers)
            # A mapping may have a dependency in the other global-state copy.
            for item in files.values():
                if item:
                    after, _, _ = _remove(item[1], project_id, home)
                    if _contains(after, (project_id, *mapped, *roots)):
                        raise NativeProjectError("Project has unapproved cross-file references")
            _native_guard(home, {project_id, *mapped}, roots)
        except (NativeProjectError, OSError, ValueError, sqlite3.Error) as exc:
            blockers.append(str(exc))
        evidence = {"schema": SCHEMA, "home": str(home), "project_id": project_id,
                    "file_sha256": hashes, "entry_counts": counts,
                    "mapped_ids": sorted(mapped), "root_paths": list(roots)}
        projects.append(NativeProject(home, project_id, name, roots, evidence, tuple(blockers)))
    return tuple(projects)


@dataclass(frozen=True)
class NativeProjectCleanupResult:
    project_ids: tuple[str, ...]
    files_changed: tuple[str, ...]
    entries_removed: int
    status: str = "deleted"

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "project_ids": list(self.project_ids),
                "files_changed": list(self.files_changed), "entries_removed": self.entries_removed,
                "other_data_unchanged": True, "verified": True}


def execute_native_project_cleanup(evidence: Sequence[Mapping[str, Any]], *,
                                   client_inspector: Callable | None = None,
                                   phase_callback: Callable[[str], None] | None = None) -> NativeProjectCleanupResult:
    if not evidence or len({str(e.get("home")) for e in evidence}) != 1:
        raise NativeProjectError("Select projects from exactly one native home")
    home = Path(str(evidence[0]["home"])).resolve()
    ids = tuple(str(e["project_id"]) for e in evidence)
    if len(set(ids)) != len(ids):
        raise NativeProjectError("Duplicate project selection")
    inspect = client_inspector or running_related_clients

    def guard_clients() -> None:
        if inspect(home):
            raise NativeProjectError("Owning clients are still running")

    guard_clients()
    if phase_callback:
        phase_callback("guard_started")
    fresh = {p.project_id: p for p in discover_native_projects(home)}
    for approved in evidence:
        item = fresh.get(str(approved["project_id"]))
        if item is None or item.blockers or dict(item.evidence) != dict(approved):
            raise NativeProjectError("Frozen project evidence changed or is blocked")
    files = _read_files(home)
    expected_hashes = evidence[0]["file_sha256"]
    if {n: _hash(v[0]) if v else None for n, v in files.items()} != expected_hashes:
        raise NativeProjectError("Global state changed during preflight")
    changes = []
    removed = 0
    for filename, item in files.items():
        if item is None:
            continue
        raw, before = item
        after = before
        count = 0
        for project_id in ids:
            after, delta, _ = _remove(after, project_id, home)
            count += delta
        if count:
            payload = json.dumps(after, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            changes.append((home / filename, raw, payload))
            removed += count
    expected_count = sum(sum(e["entry_counts"].values()) for e in evidence)
    if not changes or removed != expected_count:
        raise NativeProjectError("Unexpected affected entry count")
    staged: list[Path] = []
    backups: list[Path] = []
    written: list[int] = []
    started = False
    manifest: Path | None = None

    def temporary(raw: bytes, prefix: str) -> Path:
        fd, name = tempfile.mkstemp(prefix=prefix, dir=home)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return Path(name)

    try:
        for path, raw, payload in changes:
            backups.append(temporary(raw, ".larj-project-rollback-"))
            staged.append(temporary(payload, ".larj-project-stage-"))
            os.chmod(staged[-1], stat.S_IMODE(path.stat().st_mode))
        manifest = temporary(json.dumps({
            "schema": SCHEMA, "home": str(home), "project_ids": ids,
            "files": [{"path": str(path), "rollback_path": str(backups[i]),
                       "before_sha256": _hash(raw), "after_sha256": _hash(payload)}
                      for i, (path, raw, payload) in enumerate(changes)],
        }, ensure_ascii=False).encode("utf-8"), ".larj-project-recovery-")
        guard_clients()
        current = _read_files(home)
        if {n: _hash(v[0]) if v else None for n, v in current.items()} != expected_hashes:
            raise NativeProjectError("Concurrent modification before apply")
        # Recheck filesystem/native references at the final write boundary.
        for approved in evidence:
            if not all(_missing_directory(r) for r in approved["root_paths"]):
                raise NativeProjectError("Project directory reappeared")
            _native_guard(home, {approved["project_id"], *approved["mapped_ids"]}, approved["root_paths"])
        if phase_callback:
            phase_callback("mutation_started")
        started = True
        for index, (path, raw, payload) in enumerate(changes):
            if path.read_bytes() != raw:
                raise NativeProjectError("Concurrent modification during apply")
            os.replace(staged[index], path)
            written.append(index)
        for path, raw, payload in changes:
            if path.read_bytes() != payload:
                raise NativeProjectError("Post-write verification failed")
        remaining = remaining_native_project_markers(
            home, ids, required_files=tuple(n for n, h in expected_hashes.items() if h),
            mapped_ids=tuple({i for e in evidence for i in e["mapped_ids"]}),
        )
        if remaining:
            raise NativeProjectError("Approved project markers remain")
    except Exception as exc:
        try:
            for index in reversed(written):
                path, raw, payload = changes[index]
                if path.read_bytes() != payload:
                    raise NativeProjectError("Concurrent write prevents trusted rollback")
                os.replace(backups[index], path)
                if path.read_bytes() != raw:
                    raise NativeProjectError("Rollback verification failed")
        except Exception as recovery:
            raise NativeProjectError(f"Unknown project mutation; recovery manifest: {manifest}", unknown=True) from recovery
        for backup in backups:
            backup.unlink(missing_ok=True)
        if manifest is not None:
            manifest.unlink(missing_ok=True)
        raise NativeProjectError(str(exc), rolled_back=started) from exc
    finally:
        for stage in staged:
            stage.unlink(missing_ok=True)
    for backup in backups:
        backup.unlink()
    if manifest is not None:
        manifest.unlink()
    if phase_callback:
        phase_callback("verified")
    return NativeProjectCleanupResult(ids, tuple(str(c[0]) for c in changes), removed)


def remaining_native_project_markers(home: Path, project_ids: Sequence[str], *,
                                     required_files: Sequence[str] = (),
                                     mapped_ids: Sequence[str] = ()) -> tuple[str, ...]:
    files = _read_files(home)
    if any(files.get(name) is None for name in required_files):
        raise NativeProjectError("Frozen global-state file is unavailable during verification")
    return tuple(project_id for project_id in project_ids
                 if any(item and _contains(item[1], (project_id, *mapped_ids)) for item in files.values()))


def verify_native_project_recovery(home: Path, evidence: Sequence[Mapping[str, Any]]) -> None:
    """Resolve only frozen, exact rollback evidence; never repair live files.

    A completed write or fully restored rollback can release temporary copies.
    Mixed/changed files remain unknown even if the target ID is now absent.
    """
    home = home.resolve()
    approved = {str(e["project_id"]): e for e in evidence}
    for manifest_path in home.glob(".larj-project-recovery-*"):
        if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 1024 * 1024:
            raise NativeProjectError("Untrusted native project recovery manifest", unknown=True)
        manifest = json.loads(manifest_path.read_bytes(), object_pairs_hook=_unique_pairs)
        ids = manifest.get("project_ids", [])
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise NativeProjectError("Unsupported project recovery manifest", unknown=True)
        if not set(ids).intersection(approved):
            continue
        if (not set(ids).issubset(approved) or manifest.get("schema") != SCHEMA
                or manifest.get("home") != str(home) or len(set(ids)) != len(ids)):
            raise NativeProjectError("Recovery manifest is outside frozen scope", unknown=True)
        expected_files = {name for name in STATE_FILES
                          if sum(approved[i]["entry_counts"].get(name, 0) for i in ids)}
        records = manifest.get("files")
        if not isinstance(records, list) or len(records) != len(expected_files):
            raise NativeProjectError("Incomplete recovery file evidence", unknown=True)
        seen = set()
        backups = []
        states = set()
        for record in records:
            path = Path(record["path"])
            backup = Path(record["rollback_path"])
            if (path.parent != home or path.name not in expected_files or path.name in seen
                    or backup.parent != home or not backup.name.startswith(".larj-project-rollback-")):
                raise NativeProjectError("Recovery paths are outside frozen scope", unknown=True)
            seen.add(path.name)
            for candidate in (path, backup):
                info = candidate.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                    raise NativeProjectError("Unsafe recovery evidence file", unknown=True)
            raw = backup.read_bytes()
            if (_hash(raw) != record["before_sha256"]
                    or any(approved[i]["file_sha256"].get(path.name) != _hash(raw) for i in ids)):
                raise NativeProjectError("Rollback does not match frozen file hash", unknown=True)
            after = json.loads(raw, object_pairs_hook=_unique_pairs)
            for project_id in ids:
                after, _, _ = _remove(after, project_id, home)
            expected_after = _hash(json.dumps(after, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if expected_after != record["after_sha256"]:
                raise NativeProjectError("Recovery after hash is not the approved edit", unknown=True)
            current = _hash(path.read_bytes())
            if current == expected_after:
                states.add("after")
            elif current == _hash(raw):
                states.add("before")
            else:
                raise NativeProjectError("Unknown recovery: unrelated global state changed", unknown=True)
            backups.append(backup)
        if len(states) != 1:
            raise NativeProjectError("Unknown recovery: files are only partly changed", unknown=True)
        for backup in backups:
            backup.unlink()
        manifest_path.unlink()


def projects_for_adapters(adapters: Sequence[Any]) -> tuple[NativeProject, ...]:
    homes = {Path(a.codex_home).resolve() for a in adapters
             if str(getattr(a, "name", "")) in {"native", "codex-native", "codex-desktop"}}
    return tuple(p for home in sorted(homes) for p in discover_native_projects(home))


def project_action(project: NativeProject) -> Any:
    from .planning import ActionImpact, ActionKind, CandidateAction, RiskLevel, TargetRef, storage_id_for_path
    return CandidateAction(
        action_id=project.action_id, kind=ActionKind.DELETE_NATIVE_PROJECT,
        target=TargetRef(storage_id_for_path(project.home), project.project_id),
        risk=RiskLevel.REVIEW, available=not project.blockers,
        unavailable_reason="; ".join(project.blockers) or None,
        impact=ActionImpact(external_storage_root=str(project.home),
                            external_action_payload={"project_id": project.project_id,
                                                     "project_label": project.name,
                                                     "project_paths": list(project.roots),
                                                     "native_project_evidence": dict(project.evidence)}),
        snapshot_fingerprint=_json_hash(project.evidence),
        requires_explicit_selection=True, resource_kind="native_project")


def merge_project_context(context: Any, adapters: Sequence[Any], service: Any) -> Any:
    from .planning import ScanStatus, StorageLocation, storage_id_for_path
    projects = projects_for_adapters(adapters)
    projects = tuple(p for p in projects if p.blockers != (LIVE_DIRECTORY,))
    if not projects:
        return context
    storages = list(context.plan.storages)
    known = {s.storage_id for s in storages}
    for project in projects:
        sid = storage_id_for_path(project.home)
        if sid not in known:
            storages.append(StorageLocation(sid, "Official native CODEX_HOME", project.home, scan_status=ScanStatus.OK))
            known.add(sid)
    plan = replace(context.plan, actions=(*context.plan.actions, *(project_action(p) for p in projects)), storages=tuple(storages))
    plan = replace(plan, plan_fingerprint="native-projects:v1:" + _json_hash(plan.to_dict()))
    return replace(context, plan=plan, actions=service.typed_actions(plan))


def project_target(project: NativeProject) -> Any:
    from .client_inventory import ClientTarget
    from .record_identity import ProjectKey, RecordClassification, RecordKey, StoreKey, capability_for
    capability = replace(capability_for("native", "codex"), native_delete=False,
                         frontend_project_delete=not project.blockers,
                         reason="Exact local-environment registration cleanup")
    return ClientTarget(client="native", engine="codex",
                        record_key=RecordKey(StoreKey("native", project.home, kind="codex_home"), project.project_id, kind="project"),
                        project_key=ProjectKey.from_id("native", project.project_id, display_name=project.name),
                        native_thread_id=None, frontend_reference_ids=(),
                        classification=RecordClassification(project.classification),
                        capability=capability, action_ids=(project.action_id,) if not project.blockers else (),
                        blocker_codes=("native_project_inventory_only",) if project.blockers else (),
                        blockers=tuple({"blocker_code": "native_project_inventory_only", "message": b} for b in project.blockers),
                        project_row_evidence=(dict(project.evidence),))


def append_native_inventory(inventory: Any, adapters: Sequence[Any]) -> Any:
    projects = projects_for_adapters(adapters)
    if not projects:
        return inventory
    targets = tuple(project_target(p) for p in projects)
    keys = {p.stable_id: p for p in inventory.projects}
    for target in targets:
        keys[target.project_key.stable_id] = target.project_key
    capabilities = dict(inventory.capabilities)
    from .record_identity import capability_for
    capabilities["codex"] = replace(capabilities.get("codex", capability_for("native", "codex")),
                                     frontend_project_delete=any(not p.blockers for p in projects))
    return replace(inventory, projects=tuple(keys.values()), targets=(*inventory.targets, *targets),
                   project_items=(*inventory.project_items, *projects), capabilities=capabilities)
