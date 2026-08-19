from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .adapters.base import FrontendAdapter
from .blocker_codes import cleanup_blocker_codes
from .cleaner import ScanReport, scan_adapters
from .codex_desktop_state import ClientInspector
from .core_types import (
    Action,
    BlockerCode,
    Evidence,
    MutationKind,
    RecordKind,
    RecordRef,
    StorageKind,
    StorageRef,
    blocker_codes,
)
from .models import Finding
from .planning import (
    CleanupPlan,
    normalize_storage_path,
    storage_id_for_path,
)


ScanEngine = Callable[..., ScanReport]
PlanEngine = Callable[[ScanReport], CleanupPlan]
AdapterBuilder = Callable[[], Sequence[FrontendAdapter]]


class Driver(Protocol):
    def scan(
        self,
        adapters: Iterable[FrontendAdapter],
        *,
        platforms: Sequence[str] | None = None,
        require_codex_artifacts: bool = True,
    ) -> StoreSnapshot: ...


class Planner(Protocol):
    def plan(self, snapshot: StoreSnapshot) -> CleanupPlan: ...


class Guard(Protocol):
    def check(self, action: Action) -> object: ...


class Executor(Protocol):
    def execute(self, action: Action) -> object: ...


class Verifier(Protocol):
    def verify(self, action: Action) -> object: ...


@dataclass(frozen=True)
class StoreSnapshot:
    """One full, immutable view of the selected physical stores."""

    snapshot_id: str
    captured_at: str
    platforms: tuple[str, ...]
    storages: tuple[StorageRef, ...]
    records: tuple[RecordRef, ...]
    evidence: tuple[Evidence, ...]
    scan_complete: bool
    blocker_codes: tuple[BlockerCode, ...]
    report: ScanReport = field(repr=False, compare=False)
    active_adapters: tuple[FrontendAdapter, ...] = field(
        repr=False,
        compare=False,
    )

    def to_dict(self) -> dict[str, Any]:
        """Return metadata only; raw findings and chat content are excluded."""

        return {
            "snapshot_id": self.snapshot_id,
            "captured_at": self.captured_at,
            "platforms": list(self.platforms),
            "storage_count": len(self.storages),
            "record_count": len(self.records),
            "evidence_count": len(self.evidence),
            "storages": [storage.to_dict() for storage in self.storages],
            "records": [record.to_dict() for record in self.records],
            "evidence": [item.to_dict() for item in self.evidence],
            "scan_complete": self.scan_complete,
            "blocker_codes": [str(code) for code in self.blocker_codes],
        }


@dataclass(frozen=True)
class CleanupContext:
    """Compatibility bundle shared by human and Agent drivers."""

    snapshot: StoreSnapshot
    plan: CleanupPlan
    actions: tuple[Action, ...]
    adapter_builder: AdapterBuilder | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def report(self) -> ScanReport:
        return self.snapshot.report

    @property
    def active_adapters(self) -> tuple[FrontendAdapter, ...]:
        return self.snapshot.active_adapters

    def legacy_dict(self) -> dict[str, Any]:
        """Preserve the pre-0.2 internal facade while callers migrate."""

        return {
            "platforms": list(self.snapshot.platforms),
            "active_adapters": list(self.active_adapters),
            "adapter_builder": self.adapter_builder,
            "report": self.report,
            "plan": self.plan,
            "snapshot": self.snapshot,
            "typed_actions": self.actions,
        }


class CleanupService:
    """Single orchestration entry used by both human and Agent CLIs."""

    def __init__(
        self,
        *,
        scanner: ScanEngine | None = None,
        planner: PlanEngine | None = None,
        client_inspector: ClientInspector | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if planner is None:
            # Resolve at construction time so compatibility facades and tests
            # can inject the public planner without maintaining a second path.
            from . import planning as planning_module

            planner = planning_module.build_cleanup_plan
        self._scanner = scanner or scan_adapters
        self._planner = planner
        if client_inspector is None:
            from . import codex_desktop_state as desktop_state_module

            client_inspector = desktop_state_module.running_related_clients
        self._client_inspector = client_inspector
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def client_inspector(self) -> ClientInspector:
        return self._client_inspector

    def inspect_clients(self, storage: StorageRef | Path) -> tuple[str, ...]:
        path = storage.path if isinstance(storage, StorageRef) else storage
        return self._client_inspector(Path(path))

    def scan(
        self,
        adapters: Iterable[FrontendAdapter],
        *,
        platforms: Sequence[str] | None = None,
        require_codex_artifacts: bool = True,
    ) -> StoreSnapshot:
        active_adapters = tuple(adapters)
        report = self._scanner(
            active_adapters,
            require_codex_artifacts=require_codex_artifacts,
        )
        report = filter_candidate_platforms(report, platforms)
        return _store_snapshot(
            report,
            active_adapters=active_adapters,
            platforms=platforms,
            captured_at=self._clock(),
        )

    def snapshot_from_report(
        self,
        report: ScanReport,
        *,
        active_adapters: Iterable[FrontendAdapter] = (),
        platforms: Sequence[str] | None = None,
    ) -> StoreSnapshot:
        return _store_snapshot(
            filter_candidate_platforms(report, platforms),
            active_adapters=tuple(active_adapters),
            platforms=platforms,
            captured_at=self._clock(),
        )

    def plan(self, snapshot: StoreSnapshot) -> CleanupPlan:
        return self._planner(snapshot.report)

    def typed_actions(self, plan: CleanupPlan) -> tuple[Action, ...]:
        observations = {
            str(observation.observation_id): observation
            for observation in plan.observations
        }
        return tuple(
            _typed_action(candidate, observations)
            for candidate in plan.actions
        )

    def prepare(
        self,
        adapters: Iterable[FrontendAdapter],
        *,
        platforms: Sequence[str] | None = None,
        require_codex_artifacts: bool = True,
        adapter_builder: AdapterBuilder | None = None,
    ) -> CleanupContext:
        snapshot = self.scan(
            adapters,
            platforms=platforms,
            require_codex_artifacts=require_codex_artifacts,
        )
        plan = self.plan(snapshot)
        return CleanupContext(
            snapshot=snapshot,
            plan=plan,
            actions=self.typed_actions(plan),
            adapter_builder=adapter_builder,
        )

    def prepare_report(
        self,
        report: ScanReport,
        *,
        active_adapters: Iterable[FrontendAdapter] = (),
        platforms: Sequence[str] | None = None,
        adapter_builder: AdapterBuilder | None = None,
    ) -> CleanupContext:
        snapshot = self.snapshot_from_report(
            report,
            active_adapters=active_adapters,
            platforms=platforms,
        )
        plan = self.plan(snapshot)
        return CleanupContext(
            snapshot=snapshot,
            plan=plan,
            actions=self.typed_actions(plan),
            adapter_builder=adapter_builder,
        )

    def execute(
        self,
        context: CleanupContext,
        selected_actions: Sequence[Any],
        *,
        timeout: float,
        app_server_factory: Any,
        binary_resolver: Any,
        action_state_callback: Any = None,
        finding_mapper: Any = None,
        integrity_approval_builder: Any = None,
        desktop_fingerprint_resolver: Any = None,
        cleaner: Any = None,
    ) -> Any:
        """Execute one already-prevalidated physical mutation batch.

        Importing lazily keeps the typed snapshot layer independent from the
        concrete writers while ensuring both CLIs use this single entry.
        """

        from .execution import execute_prevalidated_actions

        return execute_prevalidated_actions(
            context,
            selected_actions,
            timeout=timeout,
            app_server_factory=app_server_factory,
            binary_resolver=binary_resolver,
            client_inspector=self._client_inspector,
            action_state_callback=action_state_callback,
            finding_mapper=finding_mapper,
            integrity_approval_builder=integrity_approval_builder,
            desktop_fingerprint_resolver=desktop_fingerprint_resolver,
            cleaner=cleaner,
        )


def selected_platforms(values: Sequence[str] | None) -> set[str]:
    normalized = {
        str(value).strip().lower()
        for value in values or ()
        if str(value).strip()
    }
    if not normalized or "all" in normalized:
        return {"aionui", "cindy", "native"}
    return normalized


def filter_supplied_adapters(
    adapters: Iterable[FrontendAdapter],
    platforms: Sequence[str] | None,
) -> list[FrontendAdapter]:
    supplied = list(adapters)
    if not platforms or "all" in platforms:
        return supplied
    selected = selected_platforms(platforms)
    return [
        adapter
        for adapter in supplied
        if str(getattr(adapter, "name", "")).lower() in selected
    ]


def filter_candidate_platforms(
    report: ScanReport,
    platforms: Sequence[str] | None,
) -> ScanReport:
    if not platforms or "all" in platforms:
        return report
    selected = selected_platforms(platforms)
    return ScanReport(
        findings=[
            finding
            for finding in report.findings
            if finding.platform.lower() in selected
            or (
                finding.platform.lower() == "codex-desktop"
                and "native" in selected
            )
        ],
        # Scanner/guard failures remain global blockers until they can be
        # assigned to one independently verifiable physical storage.
        errors=list(report.errors),
    )


def _store_snapshot(
    report: ScanReport,
    *,
    active_adapters: tuple[FrontendAdapter, ...],
    platforms: Sequence[str] | None,
    captured_at: datetime,
) -> StoreSnapshot:
    platform_values = tuple(sorted(selected_platforms(platforms)))
    storages_by_id: dict[str, StorageRef] = {}
    records_by_key: dict[tuple[str, str], RecordRef] = {}
    evidence: list[Evidence] = []

    for finding in report.findings:
        storage = _storage_ref(finding.codex_home)
        storages_by_id.setdefault(storage.storage_id, storage)
        record = RecordRef(
            storage_id=storage.storage_id,
            kind=RecordKind.CONVERSATION,
            record_id=finding.thread_id,
        )
        records_by_key.setdefault((storage.storage_id, finding.thread_id), record)
        fingerprint = _finding_fingerprint(finding)
        evidence.append(
            Evidence(
                evidence_id=f"evidence:v1:{fingerprint}",
                target=record,
                evidence_type=str(
                    finding.details.get("finding_type") or "frontend_finding"
                ),
                fingerprint=f"sha256:{fingerprint}",
                source=finding.platform,
            )
        )

    for failure in report.errors:
        if failure.codex_home is not None:
            storage = _storage_ref(failure.codex_home)
            storages_by_id.setdefault(storage.storage_id, storage)

    storages = tuple(
        sorted(storages_by_id.values(), key=lambda item: item.storage_id)
    )
    records = tuple(
        sorted(
            records_by_key.values(),
            key=lambda item: (item.storage_id, item.record_id),
        )
    )
    evidence_tuple = tuple(sorted(evidence, key=lambda item: item.evidence_id))
    scan_blockers = blocker_codes(["scan_incomplete"] if report.errors else [])
    snapshot_payload = {
        "platforms": list(platform_values),
        "storages": [storage.to_dict() for storage in storages],
        "records": [record.to_dict() for record in records],
        "evidence": [item.to_dict() for item in evidence_tuple],
        "failures": [
            {
                "platform": failure.platform,
                "error_type": failure.error_type,
                "codex_home": (
                    normalize_storage_path(failure.codex_home)
                    if failure.codex_home is not None
                    else None
                ),
            }
            for failure in report.errors
        ],
    }
    return StoreSnapshot(
        snapshot_id=f"snapshot:v1:{_sha256_json(snapshot_payload)}",
        captured_at=captured_at.astimezone(timezone.utc).isoformat(),
        platforms=platform_values,
        storages=storages,
        records=records,
        evidence=evidence_tuple,
        scan_complete=not report.errors,
        blocker_codes=scan_blockers,
        report=report,
        active_adapters=active_adapters,
    )


def _storage_ref(path: Path) -> StorageRef:
    normalized = Path(normalize_storage_path(path))
    return StorageRef(
        storage_id=storage_id_for_path(normalized),
        kind=StorageKind.CODEX_HOME,
        path=normalized,
        owner="codex",
    )


def _typed_action(
    candidate: Any,
    observations: Mapping[str, Any],
) -> Action:
    raw_kind = getattr(candidate.kind, "value", candidate.kind)
    mutation_kind = MutationKind(str(raw_kind))
    record_kind = _record_kind_for_action(candidate, mutation_kind)
    locator: tuple[tuple[str, str], ...] = ()
    resource_path = getattr(candidate.impact, "resource_path", None)
    if resource_path:
        locator = (("path", str(resource_path)),)
    target = RecordRef(
        storage_id=str(candidate.target.storage_id),
        kind=record_kind,
        record_id=str(candidate.target.thread_id),
        locator=locator,
    )
    codes: set[str] = set()
    if not candidate.available:
        for observation_id in candidate.observation_ids:
            observation = observations.get(str(observation_id))
            details = getattr(observation, "details", {})
            if isinstance(details, Mapping):
                codes.update(cleanup_blocker_codes(details))
        if not codes:
            codes.add("action_unavailable")
    return Action(
        action_id=str(candidate.action_id),
        kind=mutation_kind,
        target=target,
        snapshot_fingerprint=str(candidate.snapshot_fingerprint),
        evidence_ids=tuple(str(value) for value in candidate.observation_ids),
        available=bool(candidate.available),
        blocker_codes=tuple(BlockerCode(code) for code in sorted(codes)),
    )


def _record_kind_for_action(candidate: Any, kind: MutationKind) -> RecordKind:
    if getattr(candidate, "resource_kind", "conversation") == "legacy_index":
        return RecordKind.LEGACY_INDEX
    if kind is MutationKind.REMOVE_BROKEN_RELATION:
        return RecordKind.RELATION
    if kind is MutationKind.REMOVE_FRONTEND_REFERENCE:
        return RecordKind.FRONTEND_REFERENCE
    if kind is MutationKind.REMOVE_DESKTOP_STATE:
        return RecordKind.DESKTOP_STATE
    return RecordKind.CONVERSATION


def _finding_fingerprint(finding: Finding) -> str:
    return _sha256_json(_json_ready(finding.to_dict()))


def _json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_ready(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_json_ready(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
        return items
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _sha256_json(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "AdapterBuilder",
    "CleanupContext",
    "CleanupService",
    "Driver",
    "Executor",
    "Guard",
    "Planner",
    "StoreSnapshot",
    "Verifier",
    "filter_candidate_platforms",
    "filter_supplied_adapters",
    "selected_platforms",
]
