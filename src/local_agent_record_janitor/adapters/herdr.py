"""Herdr persisted references and explicitly requested runtime metadata."""

from __future__ import annotations

import json
import copy
from collections import Counter
import os
import stat
from pathlib import Path

from ..client_contracts import (
    ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle,
    ReferenceSnapshot, SourceFailure,
)
from ..herdr_discovery import (
    HerdrDiscoveryError, bounded_entries, local_herdr_path, require_plain_directory,
    raw_herdr_join, valid_recovery_name, valid_session_name,
)
from ..herdr_metadata import read_herdr_snapshot
from ..path_identity import is_local_absolute_locator
from ..record_identity import EngineCapability, canonical_path, normalize_engine
from ..herdr_runtime import RuntimeBudget, RuntimeObservation, probe_runtime


class HerdrAdapter:
    def __init__(self, *, profile_root: str | Path | None, host: str = "local", path_namespace: str = "local",
                 discovery_error: str | None = None, inspect_live: bool = False) -> None:
        from ..client_contracts import require_local_location
        require_local_location(host, path_namespace)
        self.profile_root = local_herdr_path(profile_root, host=host, path_namespace=path_namespace) if profile_root is not None else None
        self.raw_profile_locator = os.fspath(profile_root) if profile_root is not None else None
        self.inspect_live = inspect_live
        self.discovery_error = discovery_error
        self._snapshot: ReferenceSnapshot | None = None
        self._runtime: dict | None = None

    def describe_client(self) -> ClientDescriptor:
        return self.snapshot_references().descriptor

    def snapshot_references(self, *, refresh: bool = False) -> ReferenceSnapshot:
        if self._snapshot is not None and not refresh:
            return self._snapshot
        root = self.profile_root
        sources, errors, references = [], [], []
        known_sessions, current_observations, current_complete = [], {}, {}
        runtime_sessions = []

        def failure(code: str, source: Path | None = None) -> None:
            if source is not None and source not in sources:
                sources.append(source)
            errors.append(SourceFailure(str(source) if source is not None else "herdr-discovery", code,
                profile_root=root, database=source, error_type="HerdrInventoryIncomplete"))

        def append_observations(source, session_name, role, observations, codes, *, live=False, endpoint_namespace=None):
            for item in observations:
                engine = normalize_engine(item.agent if item.source == "herdr:" + item.agent and item.agent in {"codex", "claude", "pi"} else "unsupported:" + item.agent)
                frontend_id = f"{session_name}/{item.locator.rsplit('/agent_session', 1)[0]}"
                binding_parts = ("herdr", canonical_path(source), session_name, item.locator,
                    role.value, item.source, item.agent, item.kind, item.value)
                if live:
                    binding_parts += (endpoint_namespace,)
                binding = json.dumps(binding_parts, ensure_ascii=True, separators=(",", ":"))
                lifecycle = (ReferenceLifecycle.LIVE if live else ReferenceLifecycle.RESTORABLE
                             if role is ReferenceKind.RESTORE else ReferenceLifecycle.UNKNOWN)
                references.append(ClientReference("herdr", source, frontend_id,
                    item.value if item.kind == "id" else None, item.agent, engine, binding,
                    kind=role, lifecycle=lifecycle, source_locator=item.locator, evidence_complete=item.supported and not codes,
                    path_namespace="opaque" if item.kind == "path" and not is_local_absolute_locator(item.value) else "local",
                    opaque_native_locator=item.value))

        def read(source: Path, session_name: str, role: ReferenceKind) -> None:
            if source not in sources:
                sources.append(source)
            observations, codes = read_herdr_snapshot(source)
            for code in codes:
                failure(code, source)
            append_observations(source, session_name, role, observations, codes)
            if role is ReferenceKind.CURRENT:
                current_observations[session_name] = observations
                current_complete[session_name] = not codes

        def scan_session(directory: Path, name: str) -> None:
            known_sessions.append((directory, name))
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
            failure("runtime_writer_coverage_unknown" if self.inspect_live else "live_metadata_not_probed", root)
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
        if self.inspect_live and self.raw_profile_locator is not None:
            budget = RuntimeBudget()
            for directory, name in known_sessions:
                raw_directory = self.raw_profile_locator if name == "default" else raw_herdr_join(
                    raw_herdr_join(self.raw_profile_locator, "sessions"), name)
                raw_endpoint = raw_herdr_join(raw_directory, "herdr.sock")
                endpoint = local_herdr_path(raw_endpoint)
                sources.append(endpoint)
                observation = (RuntimeObservation(errors=("live_socket_override_not_covered",))
                               if name == "default" and "HERDR_SOCKET_PATH" in os.environ else probe_runtime(raw_endpoint, budget))
                for code in observation.errors:
                    failure(code, endpoint)
                metadata = observation.metadata
                values_match = None
                if metadata is not None:
                    append_observations(endpoint, name, ReferenceKind.CURRENT, metadata.observations, observation.errors,
                                        live=True, endpoint_namespace=raw_endpoint)
                    if current_complete.get(name) and not metadata.errors:
                        key = lambda item: (item.source, item.agent, item.kind, item.value)
                        values_match = Counter(map(key, current_observations[name])) == Counter(map(key, metadata.observations))
                        if not values_match:
                            failure("live_persisted_reference_values_differ", endpoint)
                row = observation.to_dict(name, raw_endpoint)
                row.update(reference_values_match=values_match, pane_identity_mapping="unproven",
                           persisted_current_reference_count=len(current_observations.get(name, ())),
                           live_reference_count=len(metadata.observations) if metadata is not None else None)
                runtime_sessions.append(row)
        engines = tuple(dict.fromkeys(("codex", "claude", "pi", *(r.engine for r in references if r.engine))))
        descriptor = ClientDescriptor("herdr", profile_root=root, sources=tuple(sources), owner_process_root=root,
            inventory_engines=engines, capability_limits=tuple(EngineCapability("herdr", engine, inventory=True, verify=False,
                reason="Herdr observations are inventory-only; native roots and complete writer ownership remain unproven") for engine in engines))
        self._snapshot = ReferenceSnapshot(descriptor, tuple(references), tuple(dict.fromkeys(errors)))
        self._runtime = {
            "owner_client": "herdr", "owner_process_root": str(root) if root is not None else None,
            "check_mode": "json_api_metadata", "probe_source": "herdr_protocol22" if self.inspect_live else "not_requested",
            "probe_complete": bool(runtime_sessions) and all(row["probe_complete"] for row in runtime_sessions),
            "coverage_complete": False,
            "clients_closed": False if any(row["server_active"] for row in runtime_sessions) else None,
            "coverage": {"scope": "profile_sessions", "endpoint_namespace": "known_original_spelling",
                "other_runtime_writers": "not_proven", "native_store_ownership": "unproven",
                "persisted_live_pane_mapping": "unproven"},
            "errors": list(dict.fromkeys(error.message for error in errors)), "sessions": runtime_sessions,
        }
        return self._snapshot

    def inspect_runtime(self) -> dict:
        """Project the cached inventory observation; never query a second time."""
        self.snapshot_references()
        return copy.deepcopy(self._runtime)

    def native_catalog_for(self, engine: str):
        return None  # No native root is present in persisted snapshot schema3.
