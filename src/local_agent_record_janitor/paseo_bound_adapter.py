"""Paseo references qualified by an explicit per-agent native-store manifest."""
import json
from pathlib import Path

from . import frozen_files, paseo_cleanup_files as files
from .paseo_store import PaseoAdapter, local_root
from .herdr_bound_adapter import _FrozenPathRecord, native_catalog
from .client_contracts import ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle, ReferenceSnapshot
from .record_identity import RecordKey, StoreKey, EngineCapability, canonical_path

SCHEMA = "larj.paseo-bindings.v1"


def validate_manifest(value):
    fields = {"schema_version", "profile_root", "server_id", "runtime_binaries", "native_stores", "desktop_profiles"}
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != SCHEMA:
        files.fail("binding_manifest_invalid")
    local_root(value["profile_root"])
    if not isinstance(value["server_id"], str) or not files.ID.fullmatch(value["server_id"]):
        files.fail("binding_server_id_invalid")
    if not isinstance(value["runtime_binaries"], list) or not value["runtime_binaries"]:
        files.fail("binding_runtime_required")
    for binary in value["runtime_binaries"]:
        local_root(binary)
    if not isinstance(value["native_stores"], list) or not value["native_stores"]:
        files.fail("binding_native_stores_required")
    seen = set()
    for item in value["native_stores"]:
        if (not isinstance(item, dict) or set(item) - {"agent_id", "engine", "root", "agent_dir", "codex_binary"}
                or not {"agent_id", "engine", "root"} <= item.keys() or item["engine"] not in {"codex", "pi", "claude"}
                or not isinstance(item["agent_id"], str) or not files.ID.fullmatch(item["agent_id"])):
            files.fail("binding_store_invalid")
        if item["agent_id"] in seen:
            files.fail("binding_store_ambiguous")
        seen.add(item["agent_id"])
        local_root(item["root"])
        if item["engine"] == "pi" and "agent_dir" not in item:
            files.fail("binding_pi_agent_dir_required")
        for key in ("agent_dir", "codex_binary"):
            if key in item:
                local_root(item[key])
    if not isinstance(value["desktop_profiles"], list):
        files.fail("binding_desktop_profiles_invalid")
    profiles = set()
    for item in value["desktop_profiles"]:
        if not isinstance(item, dict) or set(item) != {"root", "electron_runtime", "runtime_binaries"}:
            files.fail("binding_desktop_profile_invalid")
        local_root(item["root"]); local_root(item["electron_runtime"])
        if canonical_path(item["root"]) in profiles or not isinstance(item["runtime_binaries"], list) or not item["runtime_binaries"]:
            files.fail("binding_desktop_profile_ambiguous")
        profiles.add(canonical_path(item["root"]))
        for binary in item["runtime_binaries"]:
            local_root(binary)
    return value


def load_manifest(path):
    path = Path(path)
    _evidence, raw = frozen_files.read_file(path.parent, path.name, content=True, limit=1024 * 1024)
    return validate_manifest(files.decode(raw))


class PaseoBoundAdapter(PaseoAdapter):
    def __init__(self, manifest):
        self.manifest = json.loads(json.dumps(validate_manifest(manifest)))
        super().__init__(profile_root=manifest["profile_root"])
        self.bindings = {item["agent_id"]: item for item in manifest["native_stores"]}
        self.frozen_references = ()

    def snapshot_references(self, *, refresh=False):
        _proof, server = frozen_files.read_file(self.profile_root, "server-id", content=True, limit=256)
        if server.decode("utf-8").strip() != self.manifest["server_id"]:
            files.fail("binding_server_id_changed")
        entries, _directories = files.rows(self.profile_root)
        references, observations = [], []
        stores = {}
        seen = {}
        for entry in entries:
            if entry["kind"] != "agent":
                continue
            row = entry["row"]
            binding = self.bindings.get(row["id"])
            if binding is None or binding["engine"] != row["provider"]:
                files.fail("agent_native_root_unbound")
            if (row["opaque_native_handle_present"] and not row["native_handle_projected"]
                    or any(h["provider"] != row["provider"] for h in row["references"])):
                files.fail("agent_handle_unverified")
            metadata = (entry["value"].get("persistence") or {}).get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                files.fail("agent_metadata_unverified")
            # Built-in providers persist configuration and private tool state
            # here. The whole owned row is removed; metadata never grants a
            # native root or substitutes for its scalar resume handle.
            engine, root = binding["engine"], canonical_path(binding["root"])
            ids = {h["value"] for h in row["references"] if h["kind"] == "id"}
            paths = {h["value"] for h in row["references"] if h["kind"] == "path"}
            if len(ids) > 1 or engine != "pi" and paths or engine == "pi" and row["references"] and len(paths) != 1:
                files.fail("agent_handle_conflict")
            native_id, native_path, frozen_path = next(iter(ids), None), None, None
            if paths:
                native_path = local_root(next(iter(paths)))
                try:
                    native_path.relative_to(local_root(binding["root"]))
                except ValueError:
                    files.fail("pi_path_outside_bound_root")
                frozen = [ref for ref in self.frozen_references if ref["frontend_id"] == row["id"]
                    and ref["engine"] == "pi" and str(Path(ref["native_record"]["path"])) == str(native_path)]
                if frozen:
                    native_id = frozen[0]["native_id"]
                    frozen_path = frozen[0]["native_record"]["canonical_path"]
                    try:
                        native_path.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        if canonical_path(native_path) != frozen_path:
                            files.fail("pi_path_binding_changed")
                else:
                    catalog = native_catalog(binding)
                    records = [r for r in catalog.records if canonical_path(r.path) == canonical_path(native_path)]
                    if catalog.errors or len(records) != 1 or records[0].cwd is None or canonical_path(records[0].cwd) != canonical_path(row["cwd"]):
                        files.fail("pi_header_unverified")
                    native_id = records[0].session_id
                if ids and ids != {native_id}:
                    files.fail("pi_header_identity_conflict")
            identity = (engine, root, native_id, str(native_path) if native_path else None)
            if row["id"] in seen and seen[row["id"]] != identity:
                files.fail("duplicate_agent_binding_conflict")
            seen[row["id"]] = identity
            row = {**row, "source": str(self.profile_root / entry["path"]), "fingerprint": entry["before"]["sha256"]}
            observations.append(row)
            store = stores.setdefault((engine, root), StoreKey(engine, Path(root),
                kind="session_root" if engine == "pi" else "config_dir" if engine == "claude" else "directory"))
            if native_id is None:
                continue
            key = RecordKey(store, native_id, kind="session" if engine != "codex" else "record", path=native_path)
            if frozen_path is not None:
                key = _FrozenPathRecord(store, native_id, kind="session", path=native_path, approved_path_key=frozen_path)
            for handle in row["references"]:
                # Pi UUID and path fields both bind to the same exact native file.
                references.append(ClientReference("paseo", Path(row["source"]), row["id"], native_id, engine, engine,
                    json.dumps(["paseo", canonical_path(self.profile_root), entry["path"], row["id"], handle["locator"], key.to_dict()], sort_keys=True),
                    native_record=key, kind=ReferenceKind.RESTORE, lifecycle=ReferenceLifecycle.RESTORABLE,
                    source_locator=handle["locator"], evidence_complete=True, opaque_native_locator=handle["value"]))
        engines = tuple(sorted({item["engine"] for item in self.manifest["native_stores"]}))
        descriptor = ClientDescriptor("paseo", profile_root=self.profile_root,
            sources=tuple(self.profile_root / item["path"] for item in entries), native_stores=tuple(stores.values()),
            owner_process_root=self.profile_root, inventory_engines=engines,
            capability_limits=tuple(EngineCapability("paseo", engine, inventory=True, verify=True,
                reason="Paseo mutation requires an exact approved closure and a held daemon lifecycle") for engine in engines))
        self.observations = tuple(observations)
        return ReferenceSnapshot(descriptor, tuple(references))

    def describe_client(self):
        return self.snapshot_references().descriptor
