"""Fail-closed, manifest-level deletion for Claude Code sessions.

Only paths present in an approved :class:`ClaudeSessionRecord` manifest are
removed.  Shared Claude configuration and project records are outside this
executor's vocabulary and cannot be selected accidentally.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .claude_sessions import (
    _AUXILIARY_ROOTS,
    _auxiliary_pattern_requires_file,
    _auxiliary_target_name_matches,
)


class ClaudeDeletePlanError(ValueError):
    """A Claude deletion is incomplete, stale, or unsafe."""


class ClaudeDeleteSelectionError(ClaudeDeletePlanError):
    """A selector did not name exactly one executable Claude action."""


@dataclass(frozen=True)
class ClaudeDeleteAction:
    action_id: str
    config_dir: Path
    session_id: str
    transcript_paths: tuple[Path, ...]
    manifest: tuple[Any, ...]
    frontend_reference_snapshot: tuple[Mapping[str, Any], ...]
    classification: str
    deletable: bool
    blockers: tuple[str, ...]
    catalog_blocking_failures: tuple[str, ...]
    available: bool
    unavailable_reasons: tuple[str, ...]
    snapshot_fingerprint: str
    record: Any = field(repr=False, compare=False)
    risk: str = "high"

    @property
    def preserved_shared_records(self) -> tuple[str, ...]:
        return (
            "credentials/settings/plugins/skills/agents/commands",
            "project memory/CLAUDE.md/.claude.json",
            "stats-cache/shared history/shared index",
        )

    def approval_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "claude_session",
            "action_id": self.action_id,
            "config_dir": _normal_path(self.config_dir),
            "session_id": self.session_id,
            "transcript_paths": [_normal_path(path) for path in self.transcript_paths],
            "manifest": [_manifest_payload(item) for item in self.manifest],
            "frontend_reference_snapshot": [dict(item) for item in self.frontend_reference_snapshot],
            "classification": self.classification,
            "deletable": self.deletable,
            "blockers": list(self.blockers),
            "catalog_blocking_failures": list(self.catalog_blocking_failures),
            "risk": self.risk,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.approval_payload(),
            "available": self.available,
            "unavailable_reasons": list(self.unavailable_reasons),
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "preserved_shared_records": list(self.preserved_shared_records),
        }


@dataclass(frozen=True)
class ClaudeDeletePlan:
    actions: tuple[ClaudeDeleteAction, ...] = ()
    errors: tuple[str, ...] = ()
    selected: bool = False
    plan_fingerprint: str | None = None

    @property
    def executable_actions(self) -> tuple[ClaudeDeleteAction, ...]:
        return tuple(item for item in self.actions if item.available)

    def with_selected_actions(self, selectors: Iterable[str]) -> "ClaudeDeletePlan":
        raw = tuple(selectors)
        if not raw:
            raise ClaudeDeleteSelectionError("At least one explicit Claude action is required")
        selected: list[ClaudeDeleteAction] = []
        selected_ids: set[str] = set()
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ClaudeDeleteSelectionError("Claude delete selectors must be non-empty strings")
            selector = item.strip()
            if selector.lower() == "all":
                raise ClaudeDeleteSelectionError("Claude deletion never accepts an all selector")
            matches = [action for action in self.actions if action.action_id == selector]
            if not matches:
                matches = [action for action in self.actions if action.session_id == selector]
            if not matches:
                raise ClaudeDeleteSelectionError(f"Claude delete selector {selector!r} matched no action")
            if len(matches) != 1:
                raise ClaudeDeleteSelectionError(f"Claude delete selector {selector!r} is ambiguous")
            action = matches[0]
            if not action.available:
                raise ClaudeDeleteSelectionError(
                    f"Claude delete target {selector!r} is unavailable: "
                    + "; ".join(action.unavailable_reasons)
                )
            if action.action_id in selected_ids:
                raise ClaudeDeleteSelectionError(f"Claude action {action.action_id!r} was selected more than once")
            selected_ids.add(action.action_id)
            selected.append(action)
        ordered = tuple(sorted(selected, key=lambda action: action.action_id))
        fingerprint = _fingerprint({
            "schema_version": 1,
            "kind": "claude_delete_plan",
            "actions": [action.approval_payload() for action in ordered],
        })
        return ClaudeDeletePlan(ordered, selected=True, plan_fingerprint=fingerprint)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "plan_fingerprint": self.plan_fingerprint,
            "actions": [action.to_dict() for action in self.actions],
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class ClaudeDeleteItemResult:
    action_id: str
    config_dir: Path
    session_id: str
    status: str  # deleted | not_deleted | unknown
    deleted_paths: tuple[str, ...] = ()
    not_deleted_paths: tuple[str, ...] = ()
    error: str | None = None
    preserved_shared_records: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaudeDeleteResult:
    results: tuple[ClaudeDeleteItemResult, ...]

    @property
    def deleted(self) -> tuple[ClaudeDeleteItemResult, ...]:
        return tuple(item for item in self.results if item.status == "deleted")

    @property
    def not_deleted(self) -> tuple[ClaudeDeleteItemResult, ...]:
        return tuple(item for item in self.results if item.status == "not_deleted")

    @property
    def unknown(self) -> tuple[ClaudeDeleteItemResult, ...]:
        return tuple(item for item in self.results if item.status == "unknown")

    def to_dict(self) -> dict[str, Any]:
        return {"results": [{
            "action_id": item.action_id,
            "config_dir": str(item.config_dir),
            "session_id": item.session_id,
            "status": item.status,
            "deleted_paths": list(item.deleted_paths),
            "not_deleted_paths": list(item.not_deleted_paths),
            "error": item.error,
            "preserved_shared_records": list(item.preserved_shared_records),
        } for item in self.results]}


def build_claude_delete_plan(catalog: Any) -> ClaudeDeletePlan:
    """Create a read-only action list from a complete Claude catalog."""
    records, record_errors = _sequence_attr(catalog, ("records", "sessions"))
    failures, failure_errors = _sequence_attr(catalog, ("errors", "failures"))
    errors = [*record_errors, *failure_errors]
    seen_actions: set[str] = set()
    seen_identities: set[tuple[str, str]] = set()
    seen_paths: set[str] = set()
    valid: list[Any] = []
    duplicates: set[str] = set()
    for record in records:
        try:
            action_id = _string(record, "action_id")
            config = _path(record, "config_dir")
            session_id = _string(record, "session_id")
            manifest = _tuple(record, "manifest")
            identity = (_normal_path(config), session_id)
            if action_id in seen_actions or identity in seen_identities:
                duplicates.add(action_id)
                errors.append("Claude catalog contains a duplicate storage-qualified session identity")
            seen_actions.add(action_id)
            seen_identities.add(identity)
            for entry in manifest:
                key = _normal_path(_manifest_path(entry))
                if key in seen_paths:
                    errors.append(f"Claude catalog contains duplicate manifest path {key}")
                    duplicates.add(action_id)
                seen_paths.add(key)
            valid.append(record)
        except ClaudeDeletePlanError as exc:
            errors.append(str(exc))
    structural_blocking = (
        ("catalog structure is incomplete: " + "; ".join(dict.fromkeys(errors)),)
        if errors else ()
    )
    ordered = sorted(
        valid,
        key=lambda item: (
            _normal_path(_path(item, "config_dir")),
            _string(item, "session_id"),
        ),
    )
    actions = tuple(
        _make_action(
            record,
            _blocking_failures(
                failures,
                structural_blocking,
                config_dir=_path(record, "config_dir"),
            ),
            duplicates,
        )
        for record in ordered
    )
    all_blocking = _blocking_failures(failures, structural_blocking)
    return ClaudeDeletePlan(
        actions, tuple(dict.fromkeys([*errors, *all_blocking]))
    )


def execute_claude_delete(
    plan: ClaudeDeletePlan,
    *,
    catalog_builder: Callable[[], Any],
    approved_plan_fingerprint: str,
    clients_closed: bool,
    lstat_fn: Callable[[str | os.PathLike[str]], os.stat_result] = os.lstat,
    read_bytes_fn: Callable[[Path], bytes] | None = None,
    unlink_fn: Callable[[Path], None] | None = None,
    rmdir_fn: Callable[[Path], None] | None = None,
) -> ClaudeDeleteResult:
    """Rebuild, compare, verify, and remove only approved manifest paths."""
    if not clients_closed:
        raise ClaudeDeletePlanError("Claude deletion requires an explicit clients-closed confirmation")
    if not plan.selected or not plan.actions or not plan.plan_fingerprint:
        raise ClaudeDeletePlanError("Claude deletion requires a non-empty selected plan")
    if not isinstance(approved_plan_fingerprint, str) or not hmac.compare_digest(
        plan.plan_fingerprint, approved_plan_fingerprint,
    ):
        raise ClaudeDeletePlanError("The approved Claude plan fingerprint does not match")
    if not callable(catalog_builder):
        raise ClaudeDeletePlanError("catalog_builder must be callable")
    refreshed = build_claude_delete_plan(catalog_builder()).with_selected_actions(
        action.action_id for action in plan.actions
    )
    if not hmac.compare_digest(approved_plan_fingerprint, refreshed.plan_fingerprint or ""):
        raise ClaudeDeletePlanError("The Claude deletion plan changed after approval; nothing was deleted")

    digest_file = _stream_sha256 if read_bytes_fn is None else (
        lambda path: hashlib.sha256(read_bytes_fn(path)).hexdigest()
    )
    unlink = unlink_fn or (lambda path: path.unlink())
    rmdir = rmdir_fn or (lambda path: path.rmdir())
    results: list[ClaudeDeleteItemResult] = []
    for approved in refreshed.actions:
        mutated: list[str] = []
        try:
            # Re-read both physical inventory and Cindy references immediately
            # before this action's first mutation.
            current_plan = build_claude_delete_plan(catalog_builder()).with_selected_actions((approved.action_id,))
            current = current_plan.actions[0]
            if not hmac.compare_digest(
                _fingerprint(approved.approval_payload()),
                _fingerprint(current.approval_payload()),
            ):
                raise ClaudeDeletePlanError("Claude session manifest or frontend reference snapshot changed after approval")
            _verify_manifest_preflight(
                current, lstat_fn=lstat_fn, digest_file=digest_file
            )
            files = sorted(
                (item for item in current.manifest if _manifest_type(item) == "file"),
                key=lambda item: _manifest_relative(item), reverse=True,
            )
            directories = sorted(
                (item for item in current.manifest if _manifest_type(item) == "directory"),
                key=lambda item: (len(Path(_manifest_relative(item)).parts), _manifest_relative(item)),
                reverse=True,
            )
            for entry in files:
                # Hashing is deliberately not repeated here.  The complete
                # hash pass happened before the final directory/scope
                # enumeration, so another long hash would reopen the exact
                # enumeration-drift window that preflight closes.
                _verify_file_identity_before_unlink(
                    current, entry, lstat_fn=lstat_fn
                )
                path = _manifest_path(entry)
                unlink(path)
                mutated.append(_normal_path(path))
            for entry in directories:
                path = _manifest_path(entry)
                _reject_reparse_path(path, current.config_dir, lstat_fn)
                info = lstat_fn(path)
                if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
                    raise ClaudeDeletePlanError("Approved Claude directory became unsafe before removal")
                expected_identity = _stable_directory_identity_from_manifest(
                    entry
                )
                current_identity = _stable_directory_identity(info)
                # Directory mtime/size/ctime legitimately change as approved
                # children are unlinked.  Device, inode/file ID, mode and file
                # attributes do not.  Never rmdir a replacement merely because
                # it is another ordinary empty directory.
                if expected_identity[1] == 0:
                    raise ClaudeDeletePlanError(
                        "Claude filesystem exposes no reliable directory file ID"
                    )
                if current_identity != expected_identity:
                    raise ClaudeDeletePlanError(
                        "Approved Claude directory was replaced during deletion"
                    )
                rmdir(path)
                mutated.append(_normal_path(path))
            remaining = _remaining_paths(current, lstat_fn)
            if remaining:
                results.append(_result(current, "unknown", mutated, remaining,
                                       "approved paths still exist after deletion"))
            else:
                results.append(_result(current, "deleted", mutated, ()))
        except ClaudeDeletePlanError as exc:
            remaining = _remaining_paths(approved, lstat_fn)
            status = "unknown" if mutated else "not_deleted"
            results.append(_result(approved, status, mutated, remaining, str(exc)))
        except FileNotFoundError as exc:
            remaining = _remaining_paths(approved, lstat_fn)
            status = "unknown" if mutated else "not_deleted"
            results.append(_result(approved, status, mutated, remaining,
                                   f"approved Claude path disappeared unexpectedly: {exc}"))
        except OSError as exc:
            remaining = _remaining_paths(approved, lstat_fn)
            results.append(_result(approved, "unknown", mutated, remaining,
                                   f"could not safely delete Claude session: {exc}"))
        except Exception as exc:
            remaining = _remaining_paths(approved, lstat_fn)
            results.append(_result(approved, "unknown", mutated, remaining,
                                   f"unexpected Claude deletion verification failure: {exc}"))
    return ClaudeDeleteResult(tuple(results))


def _make_action(record: Any, blocking: tuple[str, ...], duplicates: set[str]) -> ClaudeDeleteAction:
    action_id = _string(record, "action_id")
    config = _absolute_path(_path(record, "config_dir"))
    session_id = _string(record, "session_id")
    transcripts = tuple(sorted((_absolute_path(_path_value(path)) for path in _tuple(record, "transcript_paths")), key=_normal_path))
    manifest = tuple(sorted(_tuple(record, "manifest"), key=_manifest_relative))
    snapshots = tuple(_mapping(item, "frontend reference snapshot") for item in _tuple_any(
        record, ("frontend_reference_snapshot",), default=(),
    ))
    classification = _string(record, "classification")
    deletable = _boolean(record, "deletable")
    blockers = tuple(str(item) for item in _tuple(record, "blockers"))
    reasons: list[str] = []
    if not manifest:
        reasons.append("Claude session has an empty physical manifest")
    manifest_paths = {_normal_path(_manifest_path(item)) for item in manifest}
    if any(_normal_path(path) not in manifest_paths for path in transcripts):
        reasons.append("Claude transcript is missing from its deletion manifest")
    for entry in manifest:
        try:
            relative = _manifest_relative(entry)
            path = _manifest_path(entry)
            if _relative(path, config) != relative:
                reasons.append("Claude manifest path does not match its approved relative path")
            _validate_delete_scope(
                relative, session_id, node_type=_manifest_type(entry)
            )
            if (
                _manifest_type(entry) == "directory"
                and _manifest_stat(entry)[1] == 0
            ):
                reasons.append(
                    "Claude filesystem exposes no reliable directory file ID"
                )
            _validate_current_entry(entry)
        except ClaudeDeletePlanError as exc:
            reasons.append(str(exc))
    if classification not in ("unreferenced", "deleted_frontend_reference"):
        reasons.append(f"Claude classification {classification!r} is not physically deletable")
    if not deletable:
        reasons.append("Claude inventory marks the session as not deletable")
    reasons.extend(blockers)
    reasons.extend(blocking)
    if action_id in duplicates:
        reasons.append("Claude catalog contains a duplicate action or manifest path")
    payload = {
        "schema_version": 1,
        "kind": "claude_session",
        "action_id": action_id,
        "config_dir": _normal_path(config),
        "session_id": session_id,
        "transcript_paths": [_normal_path(path) for path in transcripts],
        "manifest": [_manifest_payload(item) for item in manifest],
        "frontend_reference_snapshot": [dict(item) for item in snapshots],
        "classification": classification,
        "deletable": deletable,
        "blockers": list(blockers),
        "catalog_blocking_failures": list(blocking),
        "risk": "high",
    }
    unique_reasons = tuple(dict.fromkeys(reasons))
    return ClaudeDeleteAction(
        action_id, config, session_id, transcripts, manifest, snapshots,
        classification, deletable, blockers, blocking, not unique_reasons,
        unique_reasons, _fingerprint(payload), record,
    )


def _verify_manifest_preflight(
    action: ClaudeDeleteAction,
    *,
    lstat_fn: Callable[..., os.stat_result],
    digest_file: Callable[[Path], str],
) -> None:
    """Finish every potentially long check before the first mutation.

    Files are hashed first.  Directory identities and exact direct-child sets
    are checked only after that pass, so a file-reader callback (or another
    process) cannot add an unapproved node behind an already-validated
    directory.  A final lightweight scope enumeration catches new transcript
    copies and newly-created exact auxiliary targets outside the old manifest.
    """
    seen: set[str] = set()
    for entry in action.manifest:
        path = _manifest_path(entry)
        key = _normal_path(path)
        if key in seen:
            raise ClaudeDeletePlanError("Approved Claude manifest contains a duplicate path")
        seen.add(key)
        if _relative(path, action.config_dir) != _manifest_relative(entry):
            raise ClaudeDeletePlanError("Approved Claude manifest escaped its config directory")
        _validate_delete_scope(
            _manifest_relative(entry), action.session_id,
            node_type=_manifest_type(entry),
        )
        if _manifest_type(entry) not in ("file", "directory"):
            raise ClaudeDeletePlanError(
                "Approved Claude manifest has an unknown node type"
            )
        if (
            _manifest_type(entry) == "directory"
            and _manifest_stat(entry)[1] == 0
        ):
            raise ClaudeDeletePlanError(
                "Claude filesystem exposes no reliable directory file ID"
            )

    files = tuple(
        entry for entry in action.manifest if _manifest_type(entry) == "file"
    )
    directories = tuple(
        entry
        for entry in action.manifest
        if _manifest_type(entry) == "directory"
    )
    for entry in files:
        _verify_file_contents(
            action, entry, lstat_fn=lstat_fn, digest_file=digest_file
        )
    # This is intentionally one final phase, rather than "directories, then
    # scope".  Scope parents are snapshotted first, every approved directory
    # tree is validated inside that snapshot, and all guards are checked again
    # at the end.  Consequently there is no phase boundary at which a node can
    # be added after its directory was accepted but before scope starts.
    _verify_exact_scope_snapshot(
        action, directories=directories, lstat_fn=lstat_fn
    )


def _verify_file_contents(
    action: ClaudeDeleteAction,
    entry: Any,
    *,
    lstat_fn: Callable[..., os.stat_result],
    digest_file: Callable[[Path], str],
) -> None:
    path = _manifest_path(entry)
    _reject_reparse_path(path, action.config_dir, lstat_fn)
    info = lstat_fn(path)
    expected = _manifest_stat(entry)
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise ClaudeDeletePlanError("Approved Claude file changed node type")
    if _stat_identity(info) != expected:
        raise ClaudeDeletePlanError("Claude session file stat changed after approval")
    if digest_file(path) != _manifest_sha(entry):
        raise ClaudeDeletePlanError("Claude session file content changed after approval")
    if _stat_identity(lstat_fn(path)) != expected:
        raise ClaudeDeletePlanError("Claude session file changed during hashing")


def _verify_directories_and_children(
    action: ClaudeDeleteAction,
    directories: Sequence[Any],
    *,
    lstat_fn: Callable[..., os.stat_result],
) -> None:
    entries_by_path = {
        _normal_path(_manifest_path(entry)): entry for entry in action.manifest
    }
    expected_children: dict[str, set[str]] = {
        _normal_path(_manifest_path(entry)): set() for entry in directories
    }
    for entry in action.manifest:
        key = _normal_path(_manifest_path(entry))
        parent = _normal_path(_manifest_path(entry).parent)
        if parent in expected_children:
            expected_children[parent].add(key)

    for directory in directories:
        path = _manifest_path(directory)
        key = _normal_path(path)
        _reject_reparse_path(path, action.config_dir, lstat_fn)
        before = lstat_fn(path)
        if _is_reparse(before) or not stat.S_ISDIR(before.st_mode):
            raise ClaudeDeletePlanError(
                "Approved Claude directory changed node type"
            )
        if _stat_identity(before) != _manifest_stat(directory):
            raise ClaudeDeletePlanError(
                "Claude session directory changed after file verification"
            )
        try:
            children = tuple(path.iterdir())
        except OSError as exc:
            raise ClaudeDeletePlanError(
                "Could not enumerate approved Claude directory"
            ) from exc
        actual = {_normal_path(child) for child in children}
        if actual != expected_children[key]:
            raise ClaudeDeletePlanError(
                "Claude session directory child set changed after approval"
            )
        for child in children:
            child_key = _normal_path(child)
            approved = entries_by_path.get(child_key)
            if approved is None:  # Kept explicit even though the set check did it.
                raise ClaudeDeletePlanError(
                    "Claude session directory contains an unapproved child"
                )
            child_info = lstat_fn(child)
            if _is_reparse(child_info):
                raise ClaudeDeletePlanError(
                    "Claude session directory contains a reparse child"
                )
            expected_type = _manifest_type(approved)
            if (
                expected_type == "file"
                and not stat.S_ISREG(child_info.st_mode)
            ) or (
                expected_type == "directory"
                and not stat.S_ISDIR(child_info.st_mode)
            ):
                raise ClaudeDeletePlanError(
                    "Claude session directory child changed node type"
                )
            if _stat_identity(child_info) != _manifest_stat(approved):
                raise ClaudeDeletePlanError(
                    "Claude session directory child changed after approval"
                )
        if _stat_identity(lstat_fn(path)) != _stat_identity(before):
            raise ClaudeDeletePlanError(
                "Claude session directory changed while enumerated"
            )


def _verify_exact_scope_snapshot(
    action: ClaudeDeleteAction,
    *,
    directories: Sequence[Any],
    lstat_fn: Callable[..., os.stat_result],
) -> None:
    """Validate exact scope and the manifest tree as one final traversal."""
    expected = _expected_scope_targets(action)
    actual: dict[str, str] = {}
    guards: dict[str, tuple[Path, tuple[int, int, int, int, int, int, int]]] = {}
    config_before = lstat_fn(action.config_dir)
    if _is_reparse(config_before) or not stat.S_ISDIR(config_before.st_mode):
        raise ClaudeDeletePlanError("Claude config directory became unsafe")
    guards[_normal_path(action.config_dir)] = (
        action.config_dir,
        _stat_identity(config_before),
    )

    projects = action.config_dir / "projects"
    projects_info = _optional_lstat(projects, lstat_fn)
    if projects_info is not None:
        if _is_reparse(projects_info) or not stat.S_ISDIR(projects_info.st_mode):
            raise ClaudeDeletePlanError("Claude projects directory became unsafe")
        projects_before = _stat_identity(projects_info)
        guards[_normal_path(projects)] = (projects, projects_before)
        try:
            project_paths = tuple(projects.iterdir())
        except OSError as exc:
            raise ClaudeDeletePlanError(
                "Could not enumerate Claude projects for final scope check"
            ) from exc
        for project in project_paths:
            project_info = lstat_fn(project)
            if _is_reparse(project_info) or not stat.S_ISDIR(project_info.st_mode):
                raise ClaudeDeletePlanError(
                    "Claude project entry became unsafe during scope check"
                )
            project_before = _stat_identity(project_info)
            guards[_normal_path(project)] = (project, project_before)
            try:
                children = tuple(project.iterdir())
            except OSError as exc:
                raise ClaudeDeletePlanError(
                    "Could not enumerate Claude project for final scope check"
                ) from exc
            for child in children:
                if child.name not in (
                    action.session_id,
                    action.session_id + ".jsonl",
                ):
                    continue
                _record_scope_target(child, actual, lstat_fn)

    for root_name in _AUXILIARY_ROOTS:
        root = action.config_dir / root_name
        root_info = _optional_lstat(root, lstat_fn)
        if root_info is None:
            continue
        if _is_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise ClaudeDeletePlanError(
                "Claude auxiliary root became unsafe during scope check"
            )
        root_before = _stat_identity(root_info)
        guards[_normal_path(root)] = (root, root_before)
        try:
            children = tuple(root.iterdir())
        except OSError as exc:
            raise ClaudeDeletePlanError(
                "Could not enumerate Claude auxiliary root"
            ) from exc
        for child in children:
            if _auxiliary_target_name_matches(
                root_name, child.name, action.session_id
            ):
                _record_scope_target(child, actual, lstat_fn)
    if actual != expected:
        raise ClaudeDeletePlanError(
            "Claude exact session scope changed after manifest verification"
        )

    # Crucially this tree check is *inside* the final scope snapshot.  A rogue
    # child injected by the config_before lstat hook is therefore visible here.
    _verify_directories_and_children(
        action, directories, lstat_fn=lstat_fn
    )

    # Recheck every approved file without another hash, then every approved
    # directory and shared scope parent.  These are quick identity guards; a
    # second hash pass would create a fresh long enumeration window.
    for entry in action.manifest:
        if _manifest_type(entry) == "file":
            _verify_file_identity_before_unlink(
                action, entry, lstat_fn=lstat_fn
            )
    for directory in directories:
        path = _manifest_path(directory)
        info = lstat_fn(path)
        if (
            _is_reparse(info)
            or not stat.S_ISDIR(info.st_mode)
            or _stat_identity(info) != _manifest_stat(directory)
        ):
            raise ClaudeDeletePlanError(
                "Claude session directory changed during final traversal"
            )
    for path, identity in guards.values():
        if _stat_identity(lstat_fn(path)) != identity:
            raise ClaudeDeletePlanError(
                "Claude scope parent changed during final traversal"
            )


def _expected_scope_targets(action: ClaudeDeleteAction) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in action.manifest:
        relative = _manifest_relative(entry)
        parts = relative.split("/")
        is_root = (
            len(parts) == 3
            and parts[0] == "projects"
            and parts[2] in (
                action.session_id,
                action.session_id + ".jsonl",
            )
        ) or (
            len(parts) == 2
            and _auxiliary_target_name_matches(
                parts[0], parts[1], action.session_id
            )
        )
        if is_root:
            result[_normal_path(_manifest_path(entry))] = _manifest_type(entry)
    return result


def _record_scope_target(
    path: Path,
    result: dict[str, str],
    lstat_fn: Callable[..., os.stat_result],
) -> None:
    info = lstat_fn(path)
    if _is_reparse(info):
        raise ClaudeDeletePlanError(
            "Claude exact session target became a reparse point"
        )
    if stat.S_ISREG(info.st_mode):
        node_type = "file"
    elif stat.S_ISDIR(info.st_mode):
        node_type = "directory"
    else:
        raise ClaudeDeletePlanError(
            "Claude exact session target has an unknown node type"
        )
    result[_normal_path(path)] = node_type


def _optional_lstat(
    path: Path, lstat_fn: Callable[..., os.stat_result]
) -> os.stat_result | None:
    try:
        return lstat_fn(path)
    except FileNotFoundError:
        return None


def _verify_file_identity_before_unlink(
    action: ClaudeDeleteAction,
    entry: Any,
    *,
    lstat_fn: Callable[..., os.stat_result],
) -> None:
    path = _manifest_path(entry)
    _reject_reparse_path(path, action.config_dir, lstat_fn)
    info = lstat_fn(path)
    if (
        _is_reparse(info)
        or not stat.S_ISREG(info.st_mode)
        or _stat_identity(info) != _manifest_stat(entry)
    ):
        raise ClaudeDeletePlanError(
            "Claude session file changed after final preflight"
        )


def _validate_delete_scope(
    relative: str, session_id: str, *, node_type: str
) -> None:
    parts = Path(relative).parts
    allowed = False
    if len(parts) >= 3 and parts[0] == "projects":
        allowed = (len(parts) == 3 and parts[2] == session_id + ".jsonl") or parts[2] == session_id
    elif len(parts) >= 2 and parts[0] in _AUXILIARY_ROOTS:
        if parts[1] == session_id:
            # Legacy/exact targets may be directories with descendants or a
            # regular file, as supported before current Claude layouts.
            allowed = True
        elif len(parts) == 2 and _auxiliary_target_name_matches(
            parts[0], parts[1], session_id
        ):
            allowed = node_type == "file" and _auxiliary_pattern_requires_file(
                parts[0], parts[1], session_id
            )
    if not allowed:
        raise ClaudeDeletePlanError("Claude manifest contains a path outside exact session-owned scopes")


def _validate_current_entry(entry: Any) -> None:
    path = _manifest_path(entry)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ClaudeDeletePlanError("Could not inspect Claude manifest path while planning") from exc
    if _is_reparse(info) or (not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode)):
        raise ClaudeDeletePlanError("Claude manifest path is not a safe file or directory")
    if _stat_identity(info) != _manifest_stat(entry):
        raise ClaudeDeletePlanError("Claude manifest path changed after inventory")


def _remaining_paths(action: ClaudeDeleteAction, lstat_fn: Callable[..., os.stat_result]) -> tuple[str, ...]:
    remaining: list[str] = []
    for entry in action.manifest:
        path = _manifest_path(entry)
        try:
            lstat_fn(path)
        except FileNotFoundError:
            continue
        except Exception:
            remaining.append(_normal_path(path) + " (unknown)")
        else:
            remaining.append(_normal_path(path))
    return tuple(remaining)


def _result(action: ClaudeDeleteAction, status: str, deleted: Sequence[str], remaining: Sequence[str], error: str | None = None) -> ClaudeDeleteItemResult:
    return ClaudeDeleteItemResult(
        action.action_id, action.config_dir, action.session_id, status,
        tuple(deleted), tuple(remaining), error, action.preserved_shared_records,
    )


def _blocking_failures(
    failures: Sequence[Any],
    errors: Sequence[str],
    *,
    config_dir: Path | None = None,
) -> tuple[str, ...]:
    result = list(errors)
    for failure in failures:
        if config_dir is not None and not _failure_applies(failure, config_dir):
            continue
        blocks = _failure_field(failure, "blocks_delete")
        if blocks is None:
            result.append("Claude catalog failure has no blocks_delete classification")
        elif blocks is True:
            message = _failure_field(failure, "message")
            result.append("Blocking Claude inventory failure: " + (message if isinstance(message, str) and message else repr(failure)))
    return tuple(dict.fromkeys(result))


def _failure_applies(failure: Any, config_dir: Path) -> bool:
    root = _failure_field(failure, "config_dir")
    if root is None:
        return True
    try:
        return _normal_path(Path(root)) == _normal_path(config_dir)
    except (TypeError, ValueError, OSError):
        return True


def _failure_field(failure: Any, name: str) -> Any:
    if isinstance(failure, Mapping):
        return failure.get(name)
    return getattr(failure, name, None)


def _sequence_attr(value: Any, names: Sequence[str]) -> tuple[tuple[Any, ...], list[str]]:
    for name in names:
        if hasattr(value, name):
            found = getattr(value, name)
            if isinstance(found, (str, bytes)):
                return (), [f"Claude catalog {name} is not a sequence"]
            try:
                return tuple(found), []
            except TypeError:
                return (), [f"Claude catalog {name} is not iterable"]
    return (), [f"Claude catalog is missing {'/'.join(names)}"]


def _manifest_payload(entry: Any) -> Mapping[str, Any]:
    method = getattr(entry, "approval_payload", None)
    if not callable(method):
        raise ClaudeDeletePlanError("Claude manifest entry is missing approval_payload")
    payload = method()
    return _mapping(payload, "manifest approval payload")


def _manifest_path(entry: Any) -> Path:
    return _path(entry, "path")


def _manifest_relative(entry: Any) -> str:
    return _string(entry, "relative_path")


def _manifest_type(entry: Any) -> str:
    return _string(entry, "node_type")


def _manifest_sha(entry: Any) -> str:
    return _string(entry, "sha256")


def _manifest_stat(entry: Any) -> tuple[int, int, int, int, int, int, int]:
    names = ("stat_dev", "stat_ino", "stat_mode", "stat_size", "stat_mtime_ns", "stat_ctime_ns", "stat_file_attributes")
    return tuple(_integer(entry, name) for name in names)  # type: ignore[return-value]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ClaudeDeletePlanError(f"Claude {label} is not a mapping")
    return value


def _string(value: Any, name: str) -> str:
    found = getattr(value, name, None)
    if not isinstance(found, str) or not found:
        raise ClaudeDeletePlanError(f"Claude record has invalid {name}")
    return found


def _boolean(value: Any, name: str) -> bool:
    found = getattr(value, name, None)
    if not isinstance(found, bool):
        raise ClaudeDeletePlanError(f"Claude record has invalid {name}")
    return found


def _integer(value: Any, name: str) -> int:
    found = getattr(value, name, None)
    if not isinstance(found, int) or isinstance(found, bool) or found < 0:
        raise ClaudeDeletePlanError(f"Claude manifest has invalid {name}")
    return found


def _path(value: Any, name: str) -> Path:
    return _path_value(getattr(value, name, None), name)


def _path_value(value: Any, name: str = "path") -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ClaudeDeletePlanError(f"Claude record has invalid {name}")
    return Path(value)


def _tuple(value: Any, name: str) -> tuple[Any, ...]:
    found = getattr(value, name, None)
    if isinstance(found, (str, bytes)):
        raise ClaudeDeletePlanError(f"Claude record has invalid {name}")
    try:
        return tuple(found)
    except TypeError as exc:
        raise ClaudeDeletePlanError(f"Claude record has invalid {name}") from exc


def _tuple_any(value: Any, names: Sequence[str], *, default: Sequence[Any]) -> tuple[Any, ...]:
    for name in names:
        if hasattr(value, name):
            return _tuple(value, name)
    return tuple(default)


def _relative(path: Path, root: Path) -> str:
    try:
        return _absolute_path(path).relative_to(_absolute_path(root)).as_posix()
    except ValueError as exc:
        raise ClaudeDeletePlanError("Claude manifest path is outside its config directory") from exc


def _reject_reparse_path(path: Path, root: Path, lstat_fn: Callable[..., os.stat_result]) -> None:
    root = _absolute_path(root)
    target = _absolute_path(path)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ClaudeDeletePlanError("Approved Claude path escaped its config directory") from exc
    candidate = root
    for part in (None, *relative.parts):
        if part is not None:
            candidate = candidate / part
        info = lstat_fn(candidate)
        if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
            raise ClaudeDeletePlanError("Approved Claude path contains a symlink or reparse point")


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (getattr(info, "st_dev", 0), getattr(info, "st_ino", 0), info.st_mode,
            info.st_size, info.st_mtime_ns, getattr(info, "st_ctime_ns", 0),
            getattr(info, "st_file_attributes", 0))


def _stable_directory_identity(
    info: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        getattr(info, "st_dev", 0),
        getattr(info, "st_ino", 0),
        info.st_mode,
        getattr(info, "st_file_attributes", 0),
        # On Windows Python exposes the NTFS creation time as st_ctime_ns;
        # deleting a child does not change it (unlike POSIX inode ctime).  It
        # is an additional replacement guard on filesystems whose st_ino/file
        # ID support is weak.  The manifest already binds this value.
        getattr(info, "st_ctime_ns", 0) if os.name == "nt" else 0,
    )


def _stable_directory_identity_from_manifest(
    entry: Any,
) -> tuple[int, int, int, int, int]:
    full = _manifest_stat(entry)
    return (
        full[0], full[1], full[2], full[6],
        full[5] if os.name == "nt" else 0,
    )


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _normal_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ClaudeDeleteAction", "ClaudeDeleteItemResult", "ClaudeDeletePlan",
    "ClaudeDeletePlanError", "ClaudeDeleteResult", "ClaudeDeleteSelectionError",
    "build_claude_delete_plan", "execute_claude_delete",
]
