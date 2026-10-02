"""Inventory-only Orca metadata; no session launch, bridge, migration or writer."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from ..client_contracts import (
    ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle,
    ReferenceSnapshot, SourceFailure,
)
from ..orca_discovery import (
    OrcaDiscoveryError, local_orca_path, prove_account_home, prove_runtime_home,
    require_plain_directory, require_plain_file,
)
from ..orca_metadata import read_orca_journal
from ..record_identity import EngineCapability, RecordKey, StoreKey, canonical_path


class OrcaAdapter:
    """One known userData root, with independent account/runtime StoreKeys.

    Descriptor discovery reads bounded persisted metadata, never native files.
    Only an explicit native catalog request traverses proven local homes.
    """

    def __init__(self, *, profile_root: str | Path, codex_bin_hint: Path | None = None) -> None:
        self.profile_root = local_orca_path(profile_root)
        self.codex_bin_hint = codex_bin_hint
        self._snapshot: ReferenceSnapshot | None = None
        self._catalog = None

    def describe_client(self) -> ClientDescriptor:
        return self.snapshot_references().descriptor

    def snapshot_references(self, *, refresh: bool = False) -> ReferenceSnapshot:
        if self._snapshot is not None and not refresh:
            return self._snapshot
        root = self.profile_root
        journal = root / "agent-session-journal.db"
        sources = [journal]
        errors: list[SourceFailure] = []
        stores: dict[str, StoreKey] = {}
        references: list[ClientReference] = []
        records = ()

        def failure(code: str, source: Path = journal, store: StoreKey | None = None) -> None:
            errors.append(SourceFailure(str(source), code, profile_root=root, database=source,
                                        store=store, error_type="OrcaInventoryIncomplete"))

        root_ready = False
        try:
            require_plain_directory(root)
            root_ready = True
        except (OSError, ValueError) as exc:
            failure(str(exc) if isinstance(exc, OrcaDiscoveryError) else "profile_unavailable")
        if root_ready:
            accounts = root / "codex-accounts"
            try:
                require_plain_directory(accounts)
            except FileNotFoundError:
                pass  # No account directories is independent of journal refs.
            except (OSError, ValueError):
                failure("account_inventory_unavailable", accounts)
            else:
                try:
                    entries = sorted(accounts.iterdir(), key=lambda p: p.name)
                except OSError:
                    entries = []
                    failure("account_inventory_unavailable", accounts)
                for entry in entries:
                    try:
                        info = entry.lstat()
                        if stat.S_ISREG(info.st_mode):
                            continue  # Ordinary files are not account directories.
                        require_plain_directory(entry)
                        home = entry / "home"
                        proven = prove_account_home(root, home, account_id=entry.name)
                        store = StoreKey("codex", proven)
                        stores[store.value] = store
                    except (OSError, ValueError):
                        # This identifies the affected path, not ownership.
                        failure("account_ownership_unproven", entry / "home" / ".orca-managed-home",
                                StoreKey("codex", entry / "home"))
            try:
                require_plain_file(journal)
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = journal.with_name(journal.name + suffix)
                    try:
                        require_plain_file(sidecar)
                    except FileNotFoundError:
                        continue
                    sources.append(sidecar)
                records, journal_errors = read_orca_journal(journal, root)
                errors.extend(journal_errors)
                # SQLite's read-only WAL reader may create lock/side files.
                # Observe the known family; never remove it after inventory.
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = journal.with_name(journal.name + suffix)
                    try:
                        require_plain_file(sidecar)
                    except FileNotFoundError:
                        continue
                    if sidecar not in sources:
                        sources.append(sidecar)
            except (OSError, ValueError) as exc:
                failure(str(exc) if isinstance(exc, OrcaDiscoveryError) else "journal_unavailable")

        for record in records:
            store = None
            complete = not record.unsupported_recovery
            if record.unsupported_recovery:
                failure("record_recovery_metadata_not_covered")
            if record.provider != "codex":
                complete = False
                candidate = None
                if record.host == "local" and record.wsl_distro is None:
                    try:
                        candidate = StoreKey(record.provider, local_orca_path(record.home_locator, host=record.host))
                    except ValueError:
                        pass
                failure("provider_native_catalog_not_supported", store=candidate)
            else:
                candidate_store = None
                try:
                    if record.host != "local" or record.wsl_distro is not None:
                        raise OrcaDiscoveryError("execution_location_not_supported")
                    home = local_orca_path(record.home_locator, host=record.host)
                    account_layout = home.name == "home" and home.parent.parent == root / "codex-accounts"
                    runtime_layout = home == root / "codex-runtime-home" / "home"
                    if account_layout or runtime_layout:
                        candidate_store = StoreKey("codex", home)
                    store = next((s for s in stores.values() if s.canonical_path == canonical_path(home)), None)
                    if store is None:
                        # A persisted local accountHome is required. The
                        # runtime directory name alone never declares a store.
                        proven = (prove_account_home(root, home) if account_layout else
                                  prove_runtime_home(root, record.home_locator, host=record.host,
                                                     wsl_distro=record.wsl_distro))
                        store = StoreKey("codex", proven)
                        stores[store.value] = store
                except (OSError, ValueError):
                    complete = False
                    failure("record_native_root_unproven", store=candidate_store)
            for index, handle in enumerate(record.handles):
                kind = ReferenceKind.CURRENT if index == len(record.handles) - 1 else ReferenceKind.HISTORY
                locator = f"agent_session_records/{record.session_id}/providerHandleChain/{handle.link_id}"
                binding = json.dumps(("orca", canonical_path(journal), record.session_id, handle.link_id,
                                      kind.value, handle.provider, handle.native_id, handle.leaf_uuid), separators=(",", ":"))
                references.append(ClientReference(
                    "orca", journal, record.session_id, handle.native_id, handle.provider, handle.provider, binding,
                    native_record=RecordKey(store, handle.native_id) if store is not None else None,
                    kind=kind, lifecycle=ReferenceLifecycle.HISTORICAL if kind is ReferenceKind.HISTORY else ReferenceLifecycle.UNKNOWN,
                    source_locator=locator, evidence_complete=complete,
                    host=record.host, path_namespace="wsl" if record.wsl_distro is not None else "local",
                    opaque_native_locator=record.home_locator if store is None else None,
                ))

        # Retired sources and profile restoration metadata are separate
        # coverage. They cannot be ignored or substituted for current schema4.
        recovery_sources = [root / "agent-sessions" / f"agent-sessions.json{suffix}" for suffix in ("", ".bak")] if root_ready else []
        if root_ready:
            recovery_sources.extend(root / ("orca-data.json" + suffix)
                                    for suffix in ("", *(f".bak.{i}" for i in range(1, 6))))
            recovery_sources.append(root / "agent-hooks" / "last-status.json")
            hooks = root / "agent-hooks"
            try:
                hooks.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                sources.append(hooks)
                failure("agent_hooks_namespaces_not_covered", hooks)
            else:
                sources.append(hooks)
                failure("agent_hooks_namespaces_not_covered", hooks)
        try:
            profiles = root / "profiles"
            try:
                if root_ready:
                    require_plain_directory(profiles)
                else:
                    raise FileNotFoundError
            except FileNotFoundError:
                pass
            else:
                for profile in sorted(profiles.iterdir(), key=lambda p: p.name):
                    require_plain_directory(profile)
                    recovery_sources.extend(profile / ("profile-state.db" + suffix) for suffix in ("", "-wal", "-shm", "-journal"))
                    recovery_sources.extend(profile / ("orca-data.json" + suffix)
                                            for suffix in ("", *(f".bak.{i}" for i in range(1, 6))))
        except (OSError, ValueError):
            failure("profile_restore_sources_unavailable", root / "profiles")
        for source in recovery_sources:
            try:
                require_plain_directory(source.parent)
                source.lstat()
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                pass
            sources.append(source)
            failure("legacy_or_profile_restore_source_not_read", source)
        if root_ready:
            runtime_source = root / "orca-runtime.json"
            try:
                runtime_source.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                sources.append(runtime_source)
                failure("runtime_verification_not_covered", runtime_source)
            else:
                sources.append(runtime_source)
                failure("runtime_verification_not_covered", runtime_source)

        engines = tuple(dict.fromkeys(("codex", *(r.engine for r in references if r.engine))))
        descriptor = ClientDescriptor("orca", profile_root=root, sources=tuple(sources),
            native_stores=tuple(stores.values()), owner_process_root=root, inventory_engines=engines,
            capability_limits=tuple(EngineCapability("orca", engine, inventory=True, verify=False,
                reason="Orca metadata is inventory-only; no writer or complete runtime verification is supported")
                for engine in engines))
        self._snapshot = ReferenceSnapshot(descriptor, tuple(references), tuple(dict.fromkeys(errors)))
        if refresh:
            self._catalog = None
        return self._snapshot

    def native_catalog_for(self, engine: str):
        if engine != "codex" or not self.describe_client().native_stores:
            return None
        if self._catalog is None:
            self._catalog = self.native_catalog_group(engine, (self,))
        return self._catalog

    @staticmethod
    def native_catalog_group(engine: str, adapters: Iterable[OrcaAdapter]):
        if engine != "codex":
            return None
        from .native import NativeIntegrityAdapter
        from ..client_inventory import _SnapshotAdapter
        from ..inventory import build_session_catalog
        adapters = tuple(adapters)
        homes = {}
        errors = []
        for adapter in adapters:
            for store in adapter.describe_client().native_stores:
                if store.backend != engine:
                    continue
                try:
                    # Never let a metadata/database symlink escape the known
                    # store. Rollout directories/leaves are checked in the
                    # existing catalog pass rather than a second scanner.
                    for name in ("state_5.sqlite", "state_5.sqlite-wal", "state_5.sqlite-shm", "state_5.sqlite-journal", "session_index.jsonl"):
                        path = store.path / name
                        try:
                            require_plain_file(path)
                        except FileNotFoundError:
                            pass
                    native = NativeIntegrityAdapter(codex_home=store.path, codex_bin_hint=adapter.codex_bin_hint)
                    # Orca owns the reference reader. An empty frozen native
                    # projection must not discover Desktop sqlite/global-state
                    # sources or follow their links beyond this store.
                    homes.setdefault(store.canonical_path, _SnapshotAdapter(
                        source=native, rows=(), codex_home=store.path, database=native.database))
                except (OSError, ValueError):
                    errors.append(SourceFailure(str(store.path), "native_metadata_path_unproven",
                        profile_root=adapter.profile_root, store=store, error_type="OrcaInventoryIncomplete"))
        if not homes:
            from ..inventory import SessionCatalog
            return SessionCatalog(errors=tuple(errors), active_adapters=adapters)
        catalog = build_session_catalog(tuple(homes.values()), guard_adapters=adapters, plain_native_paths=True)
        catalog = replace(catalog, errors=(*catalog.errors, *errors))
        if len(adapters) == 1:
            adapters[0]._catalog = catalog
        return catalog
