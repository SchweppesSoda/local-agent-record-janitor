from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .adapters.base import FrontendAdapter
from .action_registry import action_capability
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


@dataclass(frozen=True)
class MutationBatch:
    """A write-coordinated child batch of one immutable operation.

    A batch never spans physical stores or mutation families. The optional
    resource key further separates frontend/relation writers that target
    different database files while preserving one user-level operation.
    """

    storage_id: str
    mutation_family: str
    actions: tuple[Any, ...]
    resource_key: tuple[str, ...] = ()

    @property
    def batch_id(self) -> str:
        suffix = ":".join(self.resource_key)
        return ":".join(
            item
            for item in (self.storage_id, self.mutation_family, suffix)
            if item
        )


def partition_actions(actions: Iterable[Any]) -> tuple[MutationBatch, ...]:
    """Partition selected actions into stable, write-safe child batches.

    The function is intentionally metadata-only: it does not scan adapters,
    rebuild catalogs, or inspect the filesystem. Callers can therefore use it
    after the single immutable plan snapshot and before executing mutations.
    """

    groups: dict[tuple[str, str, tuple[str, ...]], list[Any]] = {}
    order: list[tuple[str, str, tuple[str, ...]]] = []
    storage_order: dict[str, int] = {}
    family_order: dict[str, int] = {}
    for action in actions:
        capability = action_capability(getattr(action, "kind", ""))
        family = str(capability.mutation_family or "")
        if not family:
            family = str(getattr(getattr(action, "kind", ""), "value", ""))
        storage_id = str(
            getattr(getattr(action, "target", None), "storage_id", "")
        )
        resource_key = _mutation_resource_key(action, family)
        key = (storage_id, family, resource_key)
        if key not in groups:
            groups[key] = []
            order.append(key)
            storage_order.setdefault(storage_id, len(storage_order))
            family_order.setdefault(family, len(family_order))
        groups[key].append(action)
    # Dependency order is part of the operation contract. In particular, a
    # frontend/reference or relation/index writer must not run before its
    # native record delete has been durably verified. The secondary keys keep
    # independent stores deterministic without allowing the incidental input
    # action order to choose the mutation family order.
    order.sort(
        key=lambda key: (
            _mutation_family_rank(key[1]),
            storage_order.get(key[0], 0),
            _mutation_family_tie_breaker(key[1]),
            family_order.get(key[1], 0),
            key[1],
            key[2],
        )
    )
    return tuple(
        MutationBatch(
            storage_id=storage_id,
            mutation_family=family,
            resource_key=resource_key,
            actions=tuple(group),
        )
        for storage_id, family, resource_key in order
        for group in (groups[(storage_id, family, resource_key)],)
    )


def _mutation_family_rank(family: str) -> int:
    """Return the explicit native → frontend → relation/index → project rank."""

    normalized = str(family).strip().casefold()
    if normalized in {
        "remove_frontend_reference",
    } or "frontend" in normalized or "reference" in normalized:
        return 1
    if normalized in {
        "remove_broken_relation",
        "repair_legacy_index",
    } or "relation" in normalized or "index" in normalized:
        return 2
    if "project" in normalized:
        return 3
    # Native conversation/session/desktop mutations are intentionally first;
    # unknown families are left in the conservative native slot and are still
    # blocked by the capability registry before they can be executed.
    return 0


def _mutation_family_tie_breaker(family: str) -> int:
    normalized = str(family).strip().casefold()
    return {
        "delete_conversation": 0,
        "delete_pi_session": 1,
        "delete_claude_session": 2,
        "remove_desktop_state": 3,
        "remove_frontend_reference": 10,
        "delete_frontend_session": 11,
        "delete_project_item": 30,
        "remove_broken_relation": 20,
        "repair_legacy_index": 21,
    }.get(normalized, 99)


def _mutation_resource_key(action: Any, family: str) -> tuple[str, ...]:
    impact = getattr(action, "impact", None)
    if impact is None:
        return ()
    attributes: tuple[str, ...]
    if family == "remove_frontend_reference":
        attributes = ("frontend_database_paths",)
    elif family == "delete_frontend_session":
        attributes = ("frontend_session_database_paths",)
    elif family == "delete_project_item":
        attributes = ("frontend_project_database_paths",)
    elif family == "remove_broken_relation":
        attributes = ("relation_database_paths",)
    elif family == "remove_desktop_state":
        attributes = ("desktop_database_paths", "desktop_global_state_paths")
    else:
        attributes = ("external_storage_root",)
    values: list[str] = []
    for attribute in attributes:
        value = getattr(impact, attribute, ())
        if isinstance(value, (str, Path)):
            values.append(str(value))
        else:
            try:
                values.extend(str(item) for item in value)
            except TypeError:
                continue
    return tuple(sorted(dict.fromkeys(item for item in values if item)))


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
    session_engine: str | None = None
    session_catalog_builder: Callable[[], Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    session_native_plan: Any | None = field(
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
        self._operation_coordinator: Any | None = None

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

    def partition(self, actions: Iterable[Any]) -> tuple[MutationBatch, ...]:
        """Return write-safe child batches without touching live storage."""

        return partition_actions(actions)

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
        session_executor: Any = None,
        session_preflight_verified: bool = False,
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
            session_executor=session_executor,
            session_preflight_verified=session_preflight_verified,
        )

    def prepare_session_catalog(
        self,
        engine: str,
        catalog: Any,
        *,
        catalog_builder: Callable[[], Any],
        target_root: Path | None = None,
    ) -> CleanupContext:
        """Adapt one Pi/Claude inventory to the shared immutable plan model."""

        from .session_cleanup import build_session_cleanup_context

        return build_session_cleanup_context(
            engine,
            catalog,
            catalog_builder=catalog_builder,
            target_root=target_root,
            captured_at=self._clock(),
            typed_action_builder=self.typed_actions,
        )

    def prepare_sessions(
        self,
        engine: str,
        catalog_builder: Callable[[], Any],
        *,
        target_root: Path | None = None,
    ) -> CleanupContext:
        return self.prepare_session_catalog(
            engine,
            catalog_builder(),
            catalog_builder=catalog_builder,
            target_root=target_root,
        )

    @property
    def operation_coordinator(self) -> Any:
        """Lazily construct the shared plan/apply operation coordinator."""

        if self._operation_coordinator is None:
            from .operation_coordinator import OperationCoordinator

            self._operation_coordinator = OperationCoordinator(self)
        return self._operation_coordinator

    def plan_operation(self, **kwargs: Any) -> dict[str, Any]:
        return self.operation_coordinator.plan_operation(**kwargs)

    def apply_operation(self, **kwargs: Any) -> dict[str, Any]:
        return self.operation_coordinator.apply_operation(**kwargs)

    def run_operation(self, **kwargs: Any) -> dict[str, Any]:
        return self.operation_coordinator.run_operation(**kwargs)

    def status_operation(self, **kwargs: Any) -> dict[str, Any]:
        return self.operation_coordinator.status_operation(**kwargs)

    def verify_operation(self, **kwargs: Any) -> dict[str, Any]:
        return self.operation_coordinator.verify_operation(**kwargs)

    # Compatibility aliases for callers that used the old verb-specific names.
    plan_delete = plan_operation
    apply_delete = apply_operation
    run_delete = run_operation
    get_operation_status = status_operation
    verify_delete = verify_operation


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
    if kind is MutationKind.DELETE_FRONTEND_SESSION:
        return RecordKind.FRONTEND_SESSION
    if kind is MutationKind.REMOVE_DESKTOP_STATE:
        return RecordKind.DESKTOP_STATE
    if kind is MutationKind.DELETE_PI_SESSION:
        return RecordKind.PI_SESSION
    if kind is MutationKind.DELETE_CLAUDE_SESSION:
        return RecordKind.CLAUDE_SESSION
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


from .operation_coordinator import OperationCoordinator, OperationCoordinatorError


__all__ = [
    "AdapterBuilder",
    "CleanupContext",
    "CleanupService",
    "Driver",
    "Executor",
    "Guard",
    "MutationBatch",
    "OperationCoordinator",
    "OperationCoordinatorError",
    "Planner",
    "StoreSnapshot",
    "Verifier",
    "filter_candidate_platforms",
    "filter_supplied_adapters",
    "partition_actions",
    "selected_platforms",
]
