from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .blocker_codes import (
    STANDALONE_RELATION_CLEANUP_UNAVAILABLE,
    exact_blocker_codes,
)
from .cleaner import (
    AppServerFactory,
    BinaryResolver,
    CleanupReport,
    ExpectedDeletionScope,
    clean_findings,
    finding_key,
)
from .codex_desktop_state import (
    ClientInspector,
    DesktopCleanupResult,
    execute_desktop_state_cleanup,
)
from .legacy_index import LegacyIndexRepairResult, repair_legacy_index
from .frontend_reference_cleanup import (
    FrontendReferenceCleanupResult,
    execute_frontend_reference_cleanup,
)
from .models import Finding
from .planning import normalize_storage_path
from .targeted_guard import (
    TargetedReferenceGuard,
    affected_scope_by_finding,
)

if TYPE_CHECKING:
    from .cleanup_service import CleanupContext


FindingMapper = Callable[
    [Sequence[Any], Any, Sequence[Finding]],
    list[Finding],
]
IntegrityApprovalBuilder = Callable[
    [Sequence[Finding], Sequence[Any], Any],
    dict[tuple[str, str], frozenset[str]],
]
DesktopFingerprintResolver = Callable[[Any, Any], str]
ExecutionActionStateCallback = Callable[[str, Any, Any | None], None]
Cleaner = Callable[..., CleanupReport]


class ExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "evidence_changed",
        matches: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.matches = tuple(str(value) for value in matches)


@dataclass(frozen=True)
class ExecutionOutcome:
    """Structured result of one prevalidated physical mutation batch."""

    mutation_kind: str
    selected_actions: tuple[Any, ...]
    plan: Any
    cleanup_report: CleanupReport | None = None
    legacy_repair: LegacyIndexRepairResult | None = None
    desktop_cleanup: DesktopCleanupResult | None = None
    frontend_cleanup: FrontendReferenceCleanupResult | None = None

    @property
    def ok(self) -> bool:
        if self.cleanup_report is not None:
            return self.cleanup_report.ok
        return (
            self.legacy_repair is not None
            or self.desktop_cleanup is not None
            or self.frontend_cleanup is not None
        )

    def audit_payload(self) -> dict[str, Any]:
        """Return mutation evidence without observations or chat bodies."""

        if self.legacy_repair is not None:
            action = self.selected_actions[0]
            return {
                "command": "clean",
                "status": "repaired",
                "action_id": str(action.action_id),
                "selected_action": action.to_dict(),
                "plan_fingerprint": str(self.plan.plan_fingerprint),
                "repair": self.legacy_repair.to_dict(),
            }
        if self.desktop_cleanup is not None:
            return {
                "command": "clean",
                "mutation_kind": "remove_desktop_state",
                "selected_action_ids": [
                    str(action.action_id) for action in self.selected_actions
                ],
                "result": self.desktop_cleanup.to_dict(),
                "plan_fingerprint": str(self.plan.plan_fingerprint),
            }
        if self.frontend_cleanup is not None:
            return {
                "command": "clean",
                "mutation_kind": "remove_frontend_reference",
                "selected_action_ids": [
                    str(action.action_id) for action in self.selected_actions
                ],
                "result": self.frontend_cleanup.to_dict(),
                "plan_fingerprint": str(self.plan.plan_fingerprint),
            }
        assert self.cleanup_report is not None
        action_by_target = {
            (
                str(action.target.storage_id),
                str(action.target.thread_id),
            ): action
            for action in self.selected_actions
        }
        storage_ids = {
            normalize_storage_path(storage.path): str(storage.storage_id)
            for storage in self.plan.storages
        }
        results: list[dict[str, Any]] = []
        for result in self.cleanup_report.results:
            storage_id = storage_ids.get(
                normalize_storage_path(result.finding.codex_home),
                "",
            )
            action = action_by_target.get(
                (storage_id, str(result.finding.thread_id))
            )
            results.append(
                {
                    **result.to_dict(),
                    "action_id": (
                        str(action.action_id) if action is not None else None
                    ),
                }
            )
        return {
            "command": "clean",
            "confirmation_required": False,
            "mutation_kind": "delete_conversation",
            "selected_action_ids": [
                str(action.action_id) for action in self.selected_actions
            ],
            "plan_fingerprint": str(self.plan.plan_fingerprint),
            **{
                key: value
                for key, value in self.cleanup_report.to_dict().items()
                if key != "results"
            },
            "results": results,
        }


def execute_prevalidated_actions(
    context: CleanupContext,
    selected_actions: Sequence[Any],
    *,
    timeout: float,
    app_server_factory: AppServerFactory,
    binary_resolver: BinaryResolver,
    client_inspector: ClientInspector,
    action_state_callback: ExecutionActionStateCallback | None = None,
    finding_mapper: FindingMapper | None = None,
    integrity_approval_builder: IntegrityApprovalBuilder | None = None,
    desktop_fingerprint_resolver: DesktopFingerprintResolver | None = None,
    cleaner: Cleaner | None = None,
) -> ExecutionOutcome:
    """Execute an already-authorized batch without another full scan."""

    actions = tuple(selected_actions)
    mutation_kinds = {_enum_value(action.kind) for action in actions}
    if len(mutation_kinds) != 1:
        raise ExecutionError(
            "One execution batch must contain exactly one mutation kind.",
            kind="mixed_mutation_kinds",
            matches=[str(action.action_id) for action in actions],
        )
    mutation_kind = next(iter(mutation_kinds), "")
    plan = context.plan

    if mutation_kind == "repair_legacy_index":
        if len(actions) != 1:
            raise ExecutionError(
                "One execution batch can repair only one legacy index.",
                kind="multiple_legacy_repairs",
                matches=[str(action.action_id) for action in actions],
            )
        action = actions[0]
        storage = _storage_for_action(action, plan)
        if action_state_callback is not None:
            action_state_callback("guard_started", action, None)
            action_state_callback("mutation_started", action, None)
        result = repair_legacy_index(
            Path(storage.path),
            approved_snapshot_fingerprint=action.snapshot_fingerprint,
        )
        if action_state_callback is not None:
            action_state_callback("verified", action, None)
        return ExecutionOutcome(
            mutation_kind=mutation_kind,
            selected_actions=actions,
            plan=plan,
            legacy_repair=result,
        )

    if mutation_kind == "remove_desktop_state":
        storage_ids = {str(action.target.storage_id) for action in actions}
        if len(storage_ids) != 1:
            raise ExecutionError(
                "One Desktop state batch must target one physical store.",
                kind="multiple_desktop_storages",
                matches=[str(action.action_id) for action in actions],
            )
        storage = _storage_for_action(actions[0], plan)
        resolve_fingerprint = (
            desktop_fingerprint_resolver
            or desktop_state_snapshot_fingerprint
        )
        approved = {
            str(action.target.thread_id): resolve_fingerprint(action, plan)
            for action in actions
        }
        if action_state_callback is not None:
            for action in actions:
                action_state_callback("guard_started", action, None)
            for action in actions:
                action_state_callback("mutation_started", action, None)
        result = execute_desktop_state_cleanup(
            Path(storage.path),
            approved,
            client_inspector=client_inspector,
        )
        if action_state_callback is not None:
            for action in actions:
                action_state_callback("verified", action, None)
        return ExecutionOutcome(
            mutation_kind=mutation_kind,
            selected_actions=actions,
            plan=plan,
            desktop_cleanup=result,
        )

    if mutation_kind == "remove_frontend_reference":
        database_paths = {
            str(path)
            for action in actions
            for path in getattr(
                action.impact,
                "frontend_database_paths",
                (),
            )
        }
        if len(database_paths) != 1:
            raise ExecutionError(
                "One frontend reference batch must target one physical database.",
                kind="multiple_frontend_storages",
                matches=[str(action.action_id) for action in actions],
            )
        reference_evidence = tuple(
            dict(item)
            for action in actions
            for item in getattr(
                action.impact,
                "frontend_reference_evidence",
                (),
            )
        )
        if action_state_callback is not None:
            for action in actions:
                action_state_callback("guard_started", action, None)
            for action in actions:
                action_state_callback("mutation_started", action, None)
        storage = _storage_for_action(actions[0], plan)
        result = execute_frontend_reference_cleanup(
            Path(storage.path),
            reference_evidence,
            client_inspector=client_inspector,
        )
        if action_state_callback is not None:
            for action in actions:
                action_state_callback("verified", action, None)
        return ExecutionOutcome(
            mutation_kind=mutation_kind,
            selected_actions=actions,
            plan=plan,
            frontend_cleanup=result,
        )

    if mutation_kind != "delete_conversation":
        raise ExecutionError(
            f"Unsupported mutation kind: {mutation_kind or '<empty>'}",
            kind="unavailable",
            matches=[str(action.action_id) for action in actions],
        )

    map_findings = finding_mapper or findings_for_actions
    selected_findings = map_findings(
        actions,
        plan,
        context.report.findings,
    )
    if len(selected_findings) != len(actions):
        raise ExecutionError(
            "The authorized actions no longer map uniquely to scan evidence.",
            matches=[str(action.action_id) for action in actions],
        )

    expected_scopes = {
        finding_key(finding): ExpectedDeletionScope(
            descendant_thread_ids=tuple(action.impact.descendant_thread_ids),
            indexed_thread_ids=tuple(
                getattr(action.impact, "indexed_thread_ids", ())
            ),
            rollout_paths=tuple(action.impact.rollout_paths),
            rollout_state_fingerprints=tuple(
                action.impact.rollout_state_fingerprints
            ),
            conversation_metadata_fingerprints=(
                tuple(raw_metadata_fingerprints)
                if (
                    raw_metadata_fingerprints := getattr(
                        action.impact,
                        "conversation_metadata_fingerprints",
                        None,
                    )
                )
                is not None
                else None
            ),
        )
        for finding, action in zip(selected_findings, actions, strict=True)
    }
    build_integrity_approvals = (
        integrity_approval_builder or integrity_delete_approvals
    )
    approved_integrity_deletes = build_integrity_approvals(
        selected_findings,
        actions,
        plan,
    )
    storage_paths = {
        str(storage.storage_id): Path(storage.path)
        for storage in plan.storages
    }
    reference_guard = TargetedReferenceGuard(
        active_adapters=context.active_adapters,
        affected_thread_ids=affected_scope_by_finding(actions, storage_paths),
        adapter_builder=context.adapter_builder,
    )
    actions_by_finding = {
        finding_key(finding): action
        for finding, action in zip(selected_findings, actions, strict=True)
    }

    def forward_action_state(
        phase: str,
        finding: Finding,
        result: Any | None,
    ) -> None:
        if action_state_callback is None:
            return
        action = actions_by_finding.get(finding_key(finding))
        if action is None:
            raise ExecutionError(
                "An execution checkpoint could not be bound to its action."
            )
        action_state_callback(phase, action, result)

    run_cleaner = cleaner or clean_findings
    report = run_cleaner(
        selected_findings,
        timeout=timeout,
        app_server_factory=app_server_factory,
        binary_resolver=binary_resolver,
        explicit_selection=True,
        expected_scopes=expected_scopes,
        approved_integrity_deletes=approved_integrity_deletes,
        pre_delete_validator=reference_guard.check,
        action_state_callback=forward_action_state,
    )
    return ExecutionOutcome(
        mutation_kind=mutation_kind,
        selected_actions=actions,
        plan=plan,
        cleanup_report=report,
    )


def _storage_for_action(action: Any, plan: Any) -> Any:
    storage = next(
        (
            current
            for current in plan.storages
            if str(current.storage_id) == str(action.target.storage_id)
        ),
        None,
    )
    if storage is None:
        raise ExecutionError(
            "The action cannot be mapped to its physical store.",
            matches=[str(action.action_id)],
        )
    return storage


def desktop_state_snapshot_fingerprint(action: Any, plan: Any) -> str:
    observation_ids = {
        str(value) for value in getattr(action, "observation_ids", ())
    }
    matches = [
        observation
        for observation in getattr(plan, "observations", ())
        if str(getattr(observation, "observation_id", ""))
        in observation_ids
        and str(getattr(observation, "finding_type", ""))
        == "desktop_state_orphan"
        and str(getattr(observation.target, "thread_id", ""))
        == str(action.target.thread_id)
    ]
    if len(matches) != 1:
        raise ExecutionError(
            "Desktop state action has no unique approved snapshot.",
            matches=[str(action.action_id)],
        )
    fingerprint = matches[0].details.get(
        "desktop_state_snapshot_fingerprint"
    )
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ExecutionError(
            "Desktop state action is missing its complete fingerprint.",
            matches=[str(action.action_id)],
        )
    return fingerprint


def integrity_delete_approvals(
    findings: Sequence[Finding],
    actions: Sequence[Any],
    plan: Any,
) -> dict[tuple[str, str], frozenset[str]]:
    observations = {
        str(observation.observation_id): observation
        for observation in plan.observations
    }
    approvals: dict[tuple[str, str], frozenset[str]] = {}
    for finding, action in zip(findings, actions, strict=True):
        if (
            _enum_value(action.risk) != "high"
            or not bool(action.available)
            or _enum_value(action.kind) != "delete_conversation"
        ):
            continue
        target_key = (
            str(action.target.storage_id),
            str(action.target.thread_id),
        )
        action_observation_ids = {
            str(observation_id)
            for observation_id in action.observation_ids
        }
        target_observations = [
            observation
            for observation_id in action.observation_ids
            if (
                (observation := observations.get(str(observation_id)))
                is not None
                and (
                    str(observation.target.storage_id),
                    str(observation.target.thread_id),
                )
                == target_key
            )
        ]
        finding_types = {
            str(observation.finding_type)
            for observation in target_observations
            if str(observation.finding_type)
            in {"duplicate_rollout", "index_rollout_path_mismatch"}
        }
        residual_observations = [
            observation
            for observation in observations.values()
            if (
                (
                    str(observation.target.storage_id),
                    str(observation.target.thread_id),
                )
                == target_key
                and str(observation.finding_type) == "residual_spawn_edge"
            )
        ]
        if (
            len(residual_observations) == 1
            and str(residual_observations[0].observation_id)
            in action_observation_ids
            and _residual_delete_approval_is_narrow(
                residual_observations[0], action
            )
        ):
            finding_types.add("residual_spawn_edge")
        if finding_types:
            approvals[finding_key(finding)] = frozenset(finding_types)
    return approvals


def _residual_delete_approval_is_narrow(
    observation: Any,
    action: Any,
) -> bool:
    details = observation.details
    target_thread_id = str(action.target.thread_id)
    if (
        str(observation.platform).lower() != "native"
        or str(observation.target.thread_id) != target_thread_id
    ):
        return False
    if any(
        details.get(key) is True
        for key in (
            "needs_quarantine",
            "originator_conflict",
            "source_conflict",
            "identity_conflict",
            "metadata_mismatch",
            "active_reference",
            "live_reference_guard",
        )
    ):
        return False
    relation_only = exact_blocker_codes(
        details,
        STANDALONE_RELATION_CLEANUP_UNAVAILABLE,
    )
    impact = action.impact
    has_exact_target_artifact = bool(
        impact.index_record_count or impact.rollout_file_count
    )
    parent_id = details.get("parent_thread_id")
    declared_parent_ids = details.get("source_parent_ids")
    evidence = details.get("subagent_evidence")
    artifact_flag_names = (
        "parent_index_missing",
        "child_index_missing",
        "parent_rollout_present",
        "child_rollout_present",
    )
    declared_parent_set = (
        {
            item
            for item in declared_parent_ids
            if isinstance(item, str) and item
        }
        if isinstance(declared_parent_ids, (list, tuple, set, frozenset))
        else set()
    )
    target_is_indexed = target_thread_id in tuple(
        str(item)
        for item in getattr(impact, "indexed_thread_ids", ())
    )
    return (
        isinstance(parent_id, str)
        and bool(parent_id)
        and details.get("child_thread_id") == target_thread_id
        and isinstance(
            declared_parent_ids,
            (list, tuple, set, frozenset),
        )
        and declared_parent_set <= {parent_id}
        and isinstance(evidence, (list, tuple, set, frozenset))
        and all(isinstance(item, str) and bool(item) for item in evidence)
        and all(
            type(details.get(name)) is bool
            for name in artifact_flag_names
        )
        and isinstance(details.get("edge_status"), str)
        and details["edge_status"].lower() == "closed"
        and details.get("source_conflict") is False
        and details.get("child_index_missing") is (not target_is_indexed)
        and (
            details.get("child_rollout_present") is False
            or bool(impact.rollout_file_count)
        )
        and details.get("thread_delete_supported") is False
        and details.get("cleanable") is False
        and details.get("direct_database_edit_supported") is False
        and relation_only
        and (
            details.get("child_rollout_present") is True
            or details.get("child_index_missing") is False
        )
        and has_exact_target_artifact
    )


def findings_for_actions(
    actions: Sequence[Any],
    plan: Any,
    findings: Sequence[Finding],
) -> list[Finding]:
    storage_paths = {
        str(storage.storage_id): normalize_storage_path(storage.path)
        for storage in plan.storages
    }
    by_target: dict[tuple[str, str], Finding] = {}
    for finding in findings:
        key = (
            normalize_storage_path(finding.codex_home),
            finding.thread_id,
        )
        by_target.setdefault(key, finding)

    selected: list[Finding] = []
    for action in actions:
        storage_path = storage_paths.get(str(action.target.storage_id))
        finding = by_target.get(
            (storage_path, str(action.target.thread_id))
        )
        if finding is not None:
            selected.append(finding)
    return selected


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


__all__ = [
    "ExecutionError",
    "ExecutionOutcome",
    "desktop_state_snapshot_fingerprint",
    "execute_prevalidated_actions",
    "findings_for_actions",
    "integrity_delete_approvals",
]
