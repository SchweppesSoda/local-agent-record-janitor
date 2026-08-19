from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters.base import FrontendAdapter
from .cleaner import finding_key
from .models import Finding
from .path_identity import canonical_existing_path_key


AdapterBuilder = Callable[[], Sequence[FrontendAdapter]]
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

    active_adapters: tuple[FrontendAdapter, ...]
    affected_thread_ids: Mapping[TargetKey, frozenset[str]]
    adapter_builder: AdapterBuilder | None = None

    def check(self, finding: Finding) -> None:
        target_key = finding_key(finding)
        affected = self.affected_thread_ids.get(target_key)
        if affected is None:
            raise TargetedGuardError(
                "the action has no authorized targeted-reference scope"
            )
        adapters = (
            tuple(self.adapter_builder())
            if self.adapter_builder is not None
            else self.active_adapters
        )
        target_home = canonical_existing_path_key(finding.codex_home)
        live_by_source: dict[str, set[str]] = {}
        for adapter in adapters:
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
