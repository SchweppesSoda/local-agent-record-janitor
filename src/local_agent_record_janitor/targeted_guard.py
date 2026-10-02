from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cleaner import finding_key
from .client_contracts import ReferenceKind, ReferenceLifecycle, ReferenceSnapshot, describe_adapter
from .models import Finding
from .path_identity import canonical_existing_path_key
from .record_identity import canonical_path


AdapterBuilder = Callable[[], Sequence[Any]]
TargetKey = tuple[str, str]


class TargetedGuardError(RuntimeError):
    pass


@dataclass(frozen=True)
class TargetedReferenceGuard:
    """Fresh, action-local frontend reference check.

    Native rollout/index/relationship identity is checked by the cleaner's
    exact scope guard.  This component only re-reads frontend references for
    the current root and its approved descendants; it never invokes adapter
    ``scan`` or rebuilds a cleanup plan.
    """

    active_adapters: tuple[Any, ...]
    affected_thread_ids: Mapping[TargetKey, frozenset[str]]
    adapter_builder: AdapterBuilder | None = None

    def check(self, finding: Finding) -> None:
        target_key = finding_key(finding)
        affected = self.affected_thread_ids.get(target_key)
        if affected is None:
            raise TargetedGuardError(
                "the action has no authorized targeted-reference scope"
            )
        self._check_home(finding.codex_home, affected)

    def _check_home(self, home: Path, affected: frozenset[str]) -> None:
        adapters = (
            tuple(self.adapter_builder())
            if self.adapter_builder is not None
            else self.active_adapters
        )
        target_home = canonical_existing_path_key(home)
        live_by_source: dict[str, set[str]] = {}
        for adapter in adapters:
            typed_reader = getattr(adapter, "snapshot_references", None)
            if callable(typed_reader) and not callable(getattr(adapter, "snapshot_sessions", None)):
                self._check_typed(adapter, typed_reader, home, affected)
                continue
            name = str(getattr(adapter, "name", type(adapter).__name__))
            name_key = name.casefold()
            raw_home = getattr(adapter, "codex_home", None)
            if raw_home is None:
                # Compatibility-only native test adapters have no frontend
                # store and therefore cannot contribute a live reference.
                if name_key in {"native", "codex-desktop"}:
                    continue
                probe = getattr(adapter, "live_thread_ids_for", None)
                cached = getattr(adapter, "live_thread_ids", ())
                if not callable(probe) and not cached:
                    continue
            else:
                try:
                    adapter_home = canonical_existing_path_key(Path(raw_home))
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    raise TargetedGuardError(
                        f"could not prove {name} store ownership: {exc}"
                    ) from exc
                if adapter_home != target_home:
                    continue

            probe = getattr(adapter, "live_thread_ids_for", None)
            try:
                if callable(probe):
                    current = probe(set(affected))
                else:
                    current = {
                        value
                        for value in getattr(adapter, "live_thread_ids", ())
                        if value in affected
                    }
            except Exception as exc:
                raise TargetedGuardError(
                    f"could not inspect current {name} references: "
                    f"{str(exc) or repr(exc)}"
                ) from exc
            matching = {
                value
                for value in current
                if isinstance(value, str) and value in affected
            }
            if matching:
                live_by_source.setdefault(name, set()).update(matching)

        if live_by_source:
            rendered = "; ".join(
                f"{source}: {', '.join(sorted(thread_ids))}"
                for source, thread_ids in sorted(live_by_source.items())
            )
            raise TargetedGuardError(
                "a selected conversation or approved descendant gained a "
                f"live frontend reference: {rendered}"
            )

    @staticmethod
    def _check_typed(adapter: Any, reader: Any, home: Path, affected: frozenset[str]) -> None:
        """Check a known store's persistent references without guessing roots."""
        try:
            descriptor = describe_adapter(adapter)
            target_home = canonical_path(home)
            stores = tuple(store for store in descriptor.native_stores
                           if store.backend == "codex" and store.canonical_path == target_home)
            try:
                snapshot = reader(refresh=True)
            except Exception:
                if stores:
                    raise
                return  # Failed rootless sources cannot identify this store.
            if not isinstance(snapshot, ReferenceSnapshot) or snapshot.descriptor != descriptor:
                raise ValueError("typed reference snapshot changed its descriptor")
            def owns_store(store: Any) -> bool:
                return store.backend == "codex" and store.canonical_path == target_home
            associated = bool(stores) or any(
                reference.native_record is not None and owns_store(reference.native_record.store)
                for reference in snapshot.references
            ) or any(error.store is not None and owns_store(error.store) for error in snapshot.errors)
            if not associated:
                return  # Rootless/remote IDs do not join a local native store.
            for error in snapshot.errors:
                if not error.blocks_delete:
                    continue
                if error.store is not None and (error.store.backend != "codex"
                        or error.store.canonical_path != target_home):
                    continue
                raise TargetedGuardError(f"could not inspect current {descriptor.client} references: {error.message}")
            for reference in snapshot.references:
                native = reference.native_record
                if native is not None and native.store.backend == "codex" and native.store.canonical_path == target_home:
                    if native.record_id in affected:
                        if (reference.evidence_complete is True and reference.lifecycle is ReferenceLifecycle.DELETED
                                and reference.kind in {ReferenceKind.CURRENT, ReferenceKind.HISTORY}):
                            continue
                        raise TargetedGuardError(
                            f"a selected conversation or approved descendant retains a {reference.kind.value} "
                            f"{descriptor.client} reference: {native.record_id}")
            limit = descriptor.limit_for("codex")
            if not limit.native_delete:
                raise TargetedGuardError(f"client_capability_limit: {descriptor.client}/codex native_delete is unavailable")
        except TargetedGuardError:
            raise
        except Exception as exc:
            raise TargetedGuardError(f"could not inspect typed references: {str(exc) or repr(exc)}") from exc

    def check_manual(self, action: Any) -> None:
        """Reuse the exact frozen manual cascade; never discover descendants."""
        key = (canonical_existing_path_key(action.codex_home), str(action.thread_id))
        affected = self.affected_thread_ids.get(key)
        if affected is None:
            raise TargetedGuardError("the manual action has no authorized targeted-reference scope")
        self._check_home(action.codex_home, affected)


def affected_scope_by_finding(
    actions: Iterable[Any],
    storage_paths: Mapping[str, Path],
) -> dict[TargetKey, frozenset[str]]:
    result: dict[TargetKey, frozenset[str]] = {}
    for action in actions:
        storage_id = str(action.target.storage_id)
        path = storage_paths.get(storage_id)
        if path is None:
            continue
        root_id = str(action.target.thread_id)
        affected = {
            root_id,
            *(
                str(value)
                for value in getattr(
                    action.impact,
                    "affected_thread_ids",
                    (),
                )
            ),
            *(
                str(value)
                for value in getattr(
                    action.impact,
                    "descendant_thread_ids",
                    (),
                )
            ),
        }
        result[(canonical_existing_path_key(path), root_id)] = frozenset(
            affected
        )
    return result


__all__ = [
    "TargetedGuardError",
    "TargetedReferenceGuard",
    "affected_scope_by_finding",
]
