"""Explicit native-root attribution for the qualified Herdr cleanup path."""
from pathlib import Path
import json
from dataclasses import dataclass

from . import frozen_files, herdr_cleanup_files as closure, herdr_cleanup_json as codec
from .client_contracts import ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle, ReferenceSnapshot
from .herdr_discovery import local_herdr_path, valid_session_name
from .record_identity import StoreKey, RecordKey, EngineCapability, canonical_path

SCHEMA = "larj.herdr-bindings.v1"


@dataclass(frozen=True)
class _FrozenPathRecord(RecordKey):
    approved_path_key: str | None = None

    @property
    def canonical_path(self):
        return self.approved_path_key


def validate_manifest(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "profile_root", "runtime_binaries", "native_stores"}
            or value["schema_version"] != SCHEMA or not isinstance(value["native_stores"], list)
            or not value["native_stores"] or not isinstance(value["runtime_binaries"], list) or not value["runtime_binaries"]):
        codec.fail("binding_manifest_invalid")
    local_herdr_path(value["profile_root"])
    seen = set()
    for binary in value["runtime_binaries"]:
        local_herdr_path(binary)
    if len(set(map(canonical_path, value["runtime_binaries"]))) != len(value["runtime_binaries"]):
        codec.fail("binding_runtime_duplicate")
    for item in value["native_stores"]:
        if (not isinstance(item, dict) or set(item) - {"session", "engine", "root", "agent_dir", "codex_binary"}
                or not {"session", "engine", "root"} <= item.keys()
                or item["session"] != "default" and not valid_session_name(item["session"])
                or item["engine"] not in {"codex", "claude", "pi"}):
            codec.fail("binding_store_invalid")
        local_herdr_path(item["root"])
        if item["engine"] == "pi" and "agent_dir" not in item:
            codec.fail("binding_pi_agent_dir_required")
        for key in ("agent_dir", "codex_binary"):
            if key in item:
                local_herdr_path(item[key])
        identity = item["session"], item["engine"]
        if identity in seen:
            codec.fail("binding_store_ambiguous")
        seen.add(identity)
    return value


def load_manifest(path):
    path = Path(path)
    evidence, raw = frozen_files.read_file(path.parent, path.name, content=True, limit=1024 * 1024)
    return validate_manifest(codec.decode(raw))


class HerdrBoundAdapter:
    def __init__(self, manifest):
        self.manifest = json.loads(json.dumps(validate_manifest(manifest)))
        self.profile_root = local_herdr_path(manifest["profile_root"])
        self.raw_profile_locator = manifest["profile_root"]
        self.bindings = {(item["session"], item["engine"]): item for item in manifest["native_stores"]}
        self.frozen_references = ()

    def read(self, root, relative, **kwargs):
        from .herdr_lifecycle import current_boundary
        boundary = current_boundary(self.profile_root)
        return (boundary.files.read if boundary else frozen_files.read_file)(root, relative, **kwargs)

    def observations(self):
        paths, _ = closure.sources(self.profile_root)
        values = []
        for relative, session, role in paths:
            if role == "history":
                continue
            _proof, raw = self.read(self.profile_root, relative, content=True, limit=16 * 1024 * 1024)
            value = codec.decode(raw)
            codec.normalized(value)
            for wi, workspace in enumerate(value["workspaces"]):
                for ti, tab in enumerate(workspace["tabs"]):
                    for pid, pane in tab["panes"].items():
                        handle = pane.get("agent_session")
                        if not isinstance(handle, dict):
                            if pane.get("agent_resume") is not None or pane.get("launch_argv") is not None:
                                codec.fail("unbound_resume_pane")
                            continue
                        engine = handle.get("agent")
                        binding = self.bindings.get((session, engine))
                        if binding is None or handle.get("source") != "herdr:" + str(engine):
                            codec.fail("pane_native_root_unbound")
                        if handle.get("kind") != ("path" if engine == "pi" else "id"):
                            codec.fail("pane_native_handle_unverified")
                        locator = f"workspaces/{wi}/tabs/{ti}/panes/{pid}"
                        values.append({"relative": relative, "session": session, "locator": locator,
                            "frontend_id": session + "/" + locator, "engine": engine, "kind": handle["kind"],
                            "value": handle["value"], "native_root": canonical_path(binding["root"]), "binding": binding})
        return values

    def snapshot_references(self, *, refresh=False):
        # Build all sources on each guard read. A held boundary supplies actual
        # same-handle bytes, never a cached "closed" snapshot.
        paths, _ = closure.sources(self.profile_root)
        references = []
        engines = sorted({item["engine"] for item in self.manifest["native_stores"]})
        stores = tuple(StoreKey(engine, Path(root), kind="session_root" if engine == "pi" else "config_dir" if engine == "claude" else "directory")
            for engine, root in sorted({(item["engine"], canonical_path(item["root"])) for item in self.manifest["native_stores"]}))
        descriptor = ClientDescriptor("herdr", profile_root=self.profile_root,
            sources=tuple(self.profile_root / relative for relative, _s, _r in paths), native_stores=stores,
            owner_process_root=self.profile_root, inventory_engines=tuple(engines),
            capability_limits=tuple(EngineCapability("herdr", engine, inventory=True, verify=True,
                reason="Herdr mutation requires an exact approved closure and held lifecycle boundary") for engine in engines))
        for item in self.observations():
            engine = item["engine"]
            native_id, path = item["value"], None
            if engine == "pi":
                path = local_herdr_path(item["value"])
                root = Path(item["binding"]["root"])
                try:
                    path.relative_to(root)
                except ValueError:
                    codec.fail("pi_path_outside_bound_root")
                # The exact path is retained even after native deletion. The
                # frozen ticket supplies its verified ID in that case.
                from .herdr_cleanup import frozen_pi_id
                native_id = frozen_pi_id(path)
                if native_id is None:
                    values = {ref["native_id"] for ref in self.frozen_references if ref["engine"] == "pi"
                              and str(Path(ref["opaque_native_locator"])) == str(path)}
                    if len(values) > 1:
                        codec.fail("pi_path_identity_ambiguous")
                    native_id = next(iter(values), None)
                if native_id is None:
                    catalog = native_catalog(item["binding"])
                    records = [record for record in catalog.records if canonical_path(record.path) == canonical_path(path)]
                    if len(records) != 1:
                        codec.fail("pi_header_unverified")
                    native_id = records[0].session_id
            store = next(store for store in stores if store.backend == engine and store.canonical_path == item["native_root"])
            key = RecordKey(store, native_id, kind="session" if engine != "codex" else "record", path=path)
            if engine == "pi":
                frozen = [ref["native_record"] for ref in self.frozen_references if ref["engine"] == "pi"
                    and str(Path(ref["opaque_native_locator"])) == str(path) and ref["native_id"] == native_id]
                if frozen:
                    approved = frozen[0]
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        if canonical_path(path) != approved["canonical_path"]:
                            codec.fail("pi_path_binding_changed")
                    key = _FrozenPathRecord(store, native_id, kind="session", path=path,
                                            approved_path_key=approved["canonical_path"])
            role = ReferenceKind.CURRENT if item["relative"].endswith("session.json") else ReferenceKind.RESTORE
            references.append(ClientReference("herdr", self.profile_root / item["relative"], item["frontend_id"], native_id,
                engine, engine, json.dumps(["herdr", canonical_path(self.profile_root), item["relative"], item["session"],
                    item["locator"], role.value, key.to_dict()], sort_keys=True, separators=(",", ":")), native_record=key,
                kind=role, lifecycle=ReferenceLifecycle.RESTORABLE, source_locator=item["locator"] + "/agent_session",
                evidence_complete=True, opaque_native_locator=item["value"]))
        return ReferenceSnapshot(descriptor, tuple(references))

    def describe_client(self):
        return self.snapshot_references().descriptor


def native_catalog(binding):
    from types import SimpleNamespace
    if binding["engine"] == "pi":
        from .session_catalog_factory import build_pi_catalog
        return build_pi_catalog(SimpleNamespace(pi_agent_dir=Path(binding["agent_dir"]), pi_session_dir=Path(binding["root"])))
    if binding["engine"] == "claude":
        from .session_catalog_factory import build_claude_catalog
        return build_claude_catalog(SimpleNamespace(claude_config_dir=Path(binding["root"])))
    from .adapters import NativeIntegrityAdapter
    from .inventory import build_session_catalog
    return build_session_catalog((NativeIntegrityAdapter(codex_home=Path(binding["root"]),
        codex_bin_hint=Path(binding["codex_binary"]) if binding.get("codex_binary") else None),))
