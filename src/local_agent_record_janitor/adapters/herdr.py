"""Persisted-only Herdr references, with explicit live coverage limitations."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from ..client_contracts import (
    ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle,
    ReferenceSnapshot, SourceFailure,
)
from ..herdr_discovery import (
    HerdrDiscoveryError, bounded_entries, local_herdr_path, require_plain_directory,
    valid_recovery_name, valid_session_name,
)
from ..herdr_metadata import read_herdr_snapshot
from ..path_identity import is_local_absolute_locator
from ..record_identity import EngineCapability, canonical_path, normalize_engine


class HerdrAdapter:
    def __init__(self, *, profile_root: str | Path | None, host: str = "local", path_namespace: str = "local",
                 discovery_error: str | None = None) -> None:
        from ..client_contracts import require_local_location
        require_local_location(host, path_namespace)
        self.profile_root = local_herdr_path(profile_root, host=host, path_namespace=path_namespace) if profile_root is not None else None
        self.discovery_error = discovery_error
        self._snapshot: ReferenceSnapshot | None = None

    def describe_client(self) -> ClientDescriptor:
        return self.snapshot_references().descriptor

    def snapshot_references(self, *, refresh: bool = False) -> ReferenceSnapshot:
        if self._snapshot is not None and not refresh:
            return self._snapshot
        root = self.profile_root
        sources, errors, references = [], [], []

        def failure(code: str, source: Path | None = None) -> None:
            if source is not None and source not in sources:
                sources.append(source)
            errors.append(SourceFailure(str(source) if source is not None else "herdr-discovery", code,
                profile_root=root, database=source, error_type="HerdrInventoryIncomplete"))

        def read(source: Path, session_name: str, role: ReferenceKind) -> None:
            if source not in sources:
                sources.append(source)
            observations, codes = read_herdr_snapshot(source)
            for code in codes:
                failure(code, source)
            for item in observations:
                engine = normalize_engine(item.agent if item.source == "herdr:" + item.agent and item.agent in {"codex", "claude", "pi"} else "unsupported:" + item.agent)
                frontend_id = f"{session_name}/{item.locator.rsplit('/agent_session', 1)[0]}"
                binding = json.dumps(("herdr", canonical_path(source), session_name, item.locator,
                    role.value, item.source, item.agent, item.kind, item.value), ensure_ascii=True, separators=(",", ":"))
                references.append(ClientReference("herdr", source, frontend_id,
                    item.value if item.kind == "id" else None, item.agent, engine, binding,
                    kind=role, lifecycle=ReferenceLifecycle.RESTORABLE if role is ReferenceKind.RESTORE else ReferenceLifecycle.UNKNOWN,
                    source_locator=item.locator, evidence_complete=item.supported and not codes,
                    path_namespace="opaque" if item.kind == "path" and not is_local_absolute_locator(item.value) else "local",
                    opaque_native_locator=item.value))

        def scan_session(directory: Path, name: str) -> None:
            read(directory / "session.json", name, ReferenceKind.CURRENT)
            for dirname in ("session-snapshots", "session-backups"):
                recovery = directory / dirname
                try:
                    entries = bounded_entries(recovery)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError) as exc:
                    failure(str(exc) if isinstance(exc, HerdrDiscoveryError) else "recovery_inventory_unavailable", recovery)
                    continue
                for entry in entries:
                    if entry.name.endswith(".pending"):
                        continue  # Unpublished temporary file; never read it.
                    if not valid_recovery_name(entry.name):
                        failure("recovery_filename_not_covered", entry)
                        continue
                    read(entry, name, ReferenceKind.RESTORE)

        if self.discovery_error:
            failure(self.discovery_error)
        if root is not None:
            # Persisted files and socket markers cannot establish running or
            # closed state. Never call Herdr's connecting session_info helper.
            failure("live_metadata_not_probed", root)
            try:
                require_plain_directory(root)
            except (OSError, ValueError) as exc:
                failure(str(exc) if isinstance(exc, HerdrDiscoveryError) else "profile_unavailable", root / "session.json")
            else:
                scan_session(root, "default")
                sessions = root / "sessions"
                try:
                    entries = bounded_entries(sessions)
                except FileNotFoundError:
                    entries = ()
                except (OSError, ValueError) as exc:
                    entries = ()
                    failure(str(exc) if isinstance(exc, HerdrDiscoveryError) else "session_inventory_unavailable", sessions)
                for entry in entries:
                    try:
                        info = entry.lstat()
                        if stat.S_ISREG(info.st_mode):
                            continue
                        if not valid_session_name(entry.name):
                            raise HerdrDiscoveryError("session_name_not_covered")
                        require_plain_directory(entry)
                    except (OSError, ValueError) as exc:
                        failure(str(exc) if isinstance(exc, HerdrDiscoveryError) else "session_directory_unavailable", entry)
                        continue
                    scan_session(entry, entry.name)
        engines = tuple(dict.fromkeys(("codex", "claude", "pi", *(r.engine for r in references if r.engine))))
        descriptor = ClientDescriptor("herdr", profile_root=root, sources=tuple(sources), owner_process_root=root,
            inventory_engines=engines, capability_limits=tuple(EngineCapability("herdr", engine, inventory=True, verify=False,
                reason="Herdr persistence is inventory-only; native roots and live runtime verification are unproven") for engine in engines))
        self._snapshot = ReferenceSnapshot(descriptor, tuple(references), tuple(dict.fromkeys(errors)))
        return self._snapshot

    def native_catalog_for(self, engine: str):
        return None  # No native root is present in persisted snapshot schema3.
