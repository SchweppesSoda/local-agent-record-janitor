"""Bounded, read-only Paseo agent registry observations.

Contract: getpaseo/paseo 05b074764dd1be4b7b04c7ab403ebeb88393aee3.
AgentStorage uses unversioned JSON at agents/*.json and agents/*/*.json.
The registry is not a provider's native store. IDs, cwd and saved status never
establish native ownership or prove the daemon and provider writers stopped.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .client_contracts import ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle, ReferenceSnapshot, SourceFailure
from .path_identity import is_local_absolute_locator
from .record_identity import EngineCapability, ProjectKey, RecordClassification, RecordKey, StoreKey, canonical_path, normalize_engine

UPSTREAM_COMMIT = "05b074764dd1be4b7b04c7ab403ebeb88393aee3"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 20000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}\Z")
_FIELDS = frozenset((
    "id", "provider", "cwd", "workspaceId", "createdAt", "updatedAt", "lastActivityAt",
    "lastUserMessageAt", "title", "labels", "lastStatus", "lastModeId", "config", "runtimeInfo",
    "features", "persistence", "lastError", "requiresAttention", "attentionReason",
    "attentionTimestamp", "internal", "archivedAt", "owner",
))
_STATUSES = frozenset(("initializing", "idle", "running", "error", "closed"))


class PaseoInventoryError(ValueError):
    """Fixed diagnostics; never interpolate private JSON or parser excerpts."""


def local_root(value) -> Path:
    raw = os.fspath(Path(value).expanduser())
    if not is_local_absolute_locator(raw) or any(ord(char) < 32 for char in raw):
        raise PaseoInventoryError("paseo_local_root_unproven")
    return Path(raw)


def default_root() -> Path:
    # Match resolvePaseoHome's relative env handling, but never map a remote
    # or foreign-host spelling into this host's filesystem.
    raw = os.environ.get("PASEO_HOME")
    if raw is None:
        return local_root(Path.home() / ".paseo")
    if not raw or "://" in raw or raw.startswith(("\\\\", "//")):
        raise PaseoInventoryError("paseo_environment_root_unproven")
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute() and not re.match(r"^[A-Za-z]:", raw):
        if raw.startswith(("/", "\\")):
            raise PaseoInventoryError("paseo_environment_root_unproven")
        expanded = expanded.absolute()
    return local_root(expanded)


def _plain(path: Path, *, directory=False):
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        is_directory = item != path or directory
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode))):
            raise PaseoInventoryError("paseo_path_redirected_or_special")
    return info


def _identity(info):
    # Windows Python lstat and fstat can report different ctime semantics
    # (creation vs change time). File identity, size and mtime are comparable.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PaseoInventoryError("paseo_json_duplicate_key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise PaseoInventoryError("paseo_json_invalid")


def _text(value, *, identity=False, maximum=4096):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
            or identity and not _ID.fullmatch(value)):
        raise PaseoInventoryError("paseo_metadata_shape_invalid")
    return value


def _engine(provider):
    # Plugin/custom provider names stay distinct; aliases are not proof of
    # a built-in provider implementation or a native storage root.
    return provider if provider in {"codex", "claude", "pi"} else normalize_engine("unsupported:" + provider)


def _project(value):
    if not isinstance(value, dict) or not set(value) <= _FIELDS:
        raise PaseoInventoryError("paseo_schema_unverified")
    row = {name: _text(value.get(name), identity=name in {"id", "provider"})
           for name in ("id", "provider", "cwd", "createdAt", "updatedAt")}
    for name in ("workspaceId", "lastActivityAt", "lastUserMessageAt", "archivedAt"):
        item = value.get(name)
        row[name] = _text(item, identity=name == "workspaceId") if item is not None else None
    status = value.get("lastStatus", "closed")
    if not isinstance(status, str) or status not in _STATUSES:
        raise PaseoInventoryError("paseo_status_unverified")
    row["lastStatus"] = status
    row["internal"] = value.get("internal", False)
    if type(row["internal"]) is not bool:
        raise PaseoInventoryError("paseo_metadata_shape_invalid")
    labels = value.get("labels", {})
    if not isinstance(labels, dict):
        raise PaseoInventoryError("paseo_metadata_shape_invalid")
    parent = labels.get("paseo.parent-agent-id")
    row["parent_agent_id"] = _text(parent.strip(), identity=True) if isinstance(parent, str) and parent.strip() else None
    row["owner"] = None
    if "owner" in value:
        owner = value["owner"]
        if not isinstance(owner, dict) or owner.get("kind") != "daemon":
            raise PaseoInventoryError("paseo_owner_unverified")
        row["owner"] = {"kind": "daemon", **{name: _text(owner.get(name), identity=True)
                       for name in ("daemonId", "executionId")}}
    handles = []
    for field in ("persistence", "runtimeInfo"):
        item = value.get(field)
        if item is None and field == "persistence" or field not in value:
            continue
        if not isinstance(item, dict):
            raise PaseoInventoryError("paseo_reference_shape_invalid")
        provider = _text(item.get("provider"), identity=True)
        session_id = item.get("sessionId")
        if "sessionId" not in item or session_id is None and field == "persistence":
            raise PaseoInventoryError("paseo_reference_shape_invalid")
        if session_id is not None:
            handles.append({"provider": provider, "value": _text(session_id, identity=True),
                            "kind": "id", "locator": field + "/sessionId"})
        if field == "persistence" and isinstance(item.get("nativeHandle"), str):
            native = item["nativeHandle"]
            if provider in {"claude", "codex", "copilot", "opencode", "pi", "omp"}:
                kind = "path" if provider in {"pi", "omp"} else "id"
                handles.append({"provider": provider, "value": _text(native, identity=kind == "id"),
                                "kind": kind, "locator": field + "/nativeHandle"})
    row["references"] = handles
    # Unknown/object nativeHandle and metadata are provider-defined and may
    # contain full session state or secrets. Only the proven scalar built-in
    # handles above are exposed, never opened or treated as native roots.
    persistence = value.get("persistence") or {}
    row["opaque_native_handle_present"] = persistence.get("nativeHandle") is not None
    row["native_handle_projected"] = any(handle["locator"] == "persistence/nativeHandle" for handle in handles)
    row["provider_metadata_present"] = persistence.get("metadata") is not None
    return row


def _read(path: Path, budget: list[int]):
    before = _plain(path)
    remaining = MAX_TOTAL_BYTES - budget[0]
    if before.st_size > MAX_FILE_BYTES or before.st_size > remaining:
        raise PaseoInventoryError("paseo_inventory_budget_exceeded")
    with path.open("rb") as stream:
        if _identity(before) != _identity(os.fstat(stream.fileno())):
            raise PaseoInventoryError("paseo_snapshot_changed")
        limit = min(MAX_FILE_BYTES, remaining) + 1
        # Reserve before reading: even an interrupted/growing read consumes
        # the aggregate budget. A single extra byte detects an overrun.
        budget[0] += limit
        raw = stream.read(limit)
        budget[0] -= limit - len(raw)
        after_open = os.fstat(stream.fileno())
    if len(raw) > MAX_FILE_BYTES or budget[0] > MAX_TOTAL_BYTES:
        raise PaseoInventoryError("paseo_inventory_budget_exceeded")
    if _identity(before) != _identity(after_open) or _identity(before) != _identity(_plain(path)):
        raise PaseoInventoryError("paseo_snapshot_changed")
    row = _project(json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs, parse_constant=_invalid_constant))
    row["source"] = str(path)
    row["fingerprint"] = hashlib.sha256(raw).hexdigest()
    return row


def _entries(path: Path, budget: list[int]):
    _plain(path, directory=True)
    result = []
    with os.scandir(path) as iterator:
        for entry in iterator:
            budget[1] += 1
            if budget[1] > MAX_ENTRIES:
                raise PaseoInventoryError("paseo_inventory_budget_exceeded")
            result.append(Path(entry.path))
    return sorted(result, key=lambda item: item.name)


class PaseoAdapter:
    def __init__(self, *, profile_root):
        self.profile_root = local_root(profile_root)
        self.observations = ()
        self._snapshot = None

    def describe_client(self):
        return self.snapshot_references().descriptor

    def snapshot_references(self, *, refresh=False):
        if self._snapshot is not None and not refresh:
            return self._snapshot
        base = self.profile_root / "agents"
        sources, errors, rows, references = [base], [], [], []
        budget = [0, 0]
        directories = {}

        def failure(path, code, *, blocks_inventory=True):
            errors.append(SourceFailure(str(path), code, profile_root=self.profile_root, database=path,
                error_type="PaseoInventoryIncomplete" if blocks_inventory else "PaseoCleanupUnavailable",
                blocks_inventory=blocks_inventory))

        def scan(directory, *, project=False):
            try:
                before = _identity(_plain(directory, directory=True))
                entries = _entries(directory, budget)
                directories[directory] = before
            except (OSError, ValueError) as exc:
                failure(directory, str(exc) if isinstance(exc, PaseoInventoryError) else "paseo_registry_unreadable")
                return
            for path in entries:
                try:
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise PaseoInventoryError("paseo_path_redirected_or_special")
                    if stat.S_ISDIR(info.st_mode):
                        if not project:
                            scan(path, project=True)
                        else:
                            failure(path, "paseo_nested_registry_not_covered")
                        continue
                    if not path.name.endswith(".json"):
                        continue  # Atomic .tmp files are unpublished, not records.
                    sources.append(path)
                    row = _read(path, budget)
                    rows.append(row)
                except (OSError, ValueError, RecursionError) as exc:
                    failure(path, str(exc) if isinstance(exc, PaseoInventoryError) else "paseo_record_unreadable")
        scan(base)
        for directory, before in directories.items():
            try:
                if before != _identity(_plain(directory, directory=True)):
                    raise PaseoInventoryError("paseo_snapshot_changed")
            except (OSError, ValueError) as exc:
                failure(directory, str(exc) if isinstance(exc, PaseoInventoryError) else "paseo_registry_unreadable")
        by_id = defaultdict(list)
        for row in rows:
            by_id[row["id"]].append(row)
            source = Path(row["source"])
            for handle in row["references"]:
                binding = hashlib.sha256(json.dumps([canonical_path(source), row["id"], handle], sort_keys=True).encode()).hexdigest()
                references.append(ClientReference("paseo", source, row["id"], handle["value"] if handle["kind"] == "id" else None,
                    handle["provider"], _engine(handle["provider"]), binding, kind=ReferenceKind.RESTORE,
                    lifecycle=ReferenceLifecycle.RESTORABLE, source_locator=handle["locator"], evidence_complete=False,
                    path_namespace="opaque" if handle["kind"] == "path" and not is_local_absolute_locator(handle["value"]) else "local",
                    opaque_native_locator=handle["value"]))
            if any(handle["provider"] != row["provider"] for handle in row["references"]):
                failure(source, "paseo_provider_reference_conflict")
            if row["opaque_native_handle_present"] and not row["native_handle_projected"]:
                failure(source, "paseo_native_handle_opaque", blocks_inventory=False)
        for copies in by_id.values():
            if len(copies) > 1:
                for row in copies:
                    failure(Path(row["source"]), "paseo_duplicate_agent_snapshots")
        failure(base, "paseo_native_roots_and_writer_coverage_unverified", blocks_inventory=False)
        engines = tuple(dict.fromkeys(_engine(row["provider"]) for row in rows)) or ("unknown",)
        descriptor = ClientDescriptor("paseo", profile_root=self.profile_root, sources=tuple(sources),
            owner_process_root=self.profile_root, inventory_engines=engines,
            capability_limits=tuple(_capability(engine) for engine in engines))
        self.observations = tuple(rows)
        self._snapshot = ReferenceSnapshot(descriptor, tuple(references), tuple(errors))
        return self._snapshot

    def native_catalog_for(self, engine):
        return None

    def inspect_runtime(self):
        snapshot = self.snapshot_references()
        return {"owner_client": "paseo", "owner_process_root": str(self.profile_root),
            "check_mode": "persisted_metadata", "probe_source": "not_requested",
            "clients_closed": None, "probe_complete": False, "coverage_complete": False,
            "metadata_complete": not any(error.blocks_inventory for error in snapshot.errors),
            "errors": ["paseo_native_roots_and_writer_coverage_unverified"]}


def _capability(engine):
    return EngineCapability("paseo", engine, verify=False,
        reason="Paseo registry metadata only; native roots, daemon writers and deletion effects are unverified")


def build_inventory(adapters, *, engines=()):
    from .client_inventory import ClientInventory, ClientTarget
    selected = tuple(adapter for adapter in adapters if isinstance(adapter, PaseoAdapter))
    if not selected:
        raise PaseoInventoryError("paseo_adapter_missing")
    descriptors, errors, references, targets, projects, resources = [], [], [], [], {}, []
    seen_profiles, observed_engines = set(), []
    for adapter in selected:
        snapshot = adapter.snapshot_references()
        errors.extend(snapshot.errors)
        profile = canonical_path(adapter.profile_root)
        if profile in seen_profiles:
            continue
        seen_profiles.add(profile)
        descriptors.append(snapshot.descriptor)
        references.extend(snapshot.references)
        observed_engines.extend(snapshot.descriptor.inventory_engines)
        store = StoreKey("paseo", adapter.profile_root, kind="paseo_registry")
        by_id = defaultdict(list)
        references_by_id = defaultdict(list)
        for reference in snapshot.references:
            references_by_id[reference.frontend_id].append(reference)
        for row in adapter.observations:
            by_id[row["id"]].append(row)
            resources.append((canonical_path(Path(row["source"])), "paseo_agent_metadata"))
        for identifier, copies in by_id.items():
            # Do not choose the filesystem-enumeration 'winner' from conflicting
            # copies. Each distinct provider remains observable, with one agent
            # identity per profile and provider projection.
            for engine in dict.fromkeys(_engine(row["provider"]) for row in copies):
                if engines and engine not in engines:
                    continue
                rows = [row for row in copies if _engine(row["provider"]) == engine]
                paths = {row["cwd"] for row in rows}
                cwd = next(iter(paths)) if len(paths) == 1 else None
                project = ProjectKey.from_path("paseo", cwd) if cwd and is_local_absolute_locator(cwd) else None
                if project:
                    projects[project.stable_id] = project
                refs = tuple(references_by_id[identifier])
                blockers = ["paseo_native_roots_and_writer_coverage_unverified"]
                if len(copies) > 1:
                    blockers.append("paseo_duplicate_agent_snapshots")
                targets.append(ClientTarget("paseo", engine, RecordKey(store, identifier, kind="paseo_agent"),
                    project, None, (identifier,), RecordClassification.UNVERIFIED, _capability(engine),
                    references=refs, frontend_binding_keys=tuple(ref.binding_key for ref in refs),
                    is_subagent=any(row["parent_agent_id"] for row in copies), lineage_status="unknown",
                    blocker_codes=tuple(blockers), record_metadata={
                        "scope": "persisted_agent_registry", "upstream_commit": UPSTREAM_COMMIT,
                        "snapshot_count": len(copies), "snapshots": rows,
                        "coverage": {"native_root_verified": False, "writer_coverage_complete": False,
                            "saved_status_is_live_probe": False, "native_transcripts_read": False,
                            "full_record_closure": False, "remote_delete": False,
                            "unscanned_stores": ["schedules", "projects_and_workspaces", "provider_histories",
                                                 "daemon_memory", "client_caches"]},
                    }))
    names = tuple(engines) or tuple(dict.fromkeys(observed_engines))
    return ClientInventory(client="paseo", engines=names, projects=tuple(projects.values()), records=(),
        frontend_sessions=(), unmapped_frontend_sessions=(), targets=tuple(targets),
        capabilities={engine: _capability(engine) for engine in names}, errors=tuple(errors),
        descriptors=tuple(descriptors), references=tuple(references), scanned_resources=tuple(resources))
