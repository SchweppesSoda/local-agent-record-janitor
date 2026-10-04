"""Admission for mutations sharing one trusted local operation root.

The permanent OS lock protects journal inspection through result publication.
Tickets are thread-local, bounded to frozen targets, and never supplied by CLI
arguments. Direct callers borrow admission without creating another journal.
"""

from __future__ import annotations

import os
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .legacy_index import _fsync_directory
from .path_identity import canonical_existing_path_key
from .operation_store import (
    OperationLockedError, OperationStore, OperationStoreError,
    _file_identity, _optional_lstat, _required_lstat,
    _validate_directory, _validate_regular_file,
)


class MutationRootLockedError(OperationLockedError):
    kind = "mutation_root_locked"


class UnknownMutationError(OperationStoreError):
    kind = "store_mutation_outcome_unknown"

    def __init__(self, message: str, *, recovery_required: bool = False) -> None:
        super().__init__(message)
        # A fresh sibling was definitely not dispatched. An ambiguous owner
        # journal instead requires recovery even without a started flag.
        self.recovery_required = recovery_required


@dataclass(frozen=True)
class MutationScope:
    root: Path
    # IDs and lexical frozen artifact paths. None denotes the entire root
    # when an exact footprint cannot be proved.
    target_ids: frozenset[str] | None


@dataclass(frozen=True)
class _Ticket:
    scopes: tuple[MutationScope, ...]
    operation_directory: Path | None
    plan_hash: str | None


class _RootMutex:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.depth = 0
        self.fd: int | None = None


_registry_lock = threading.Lock()
_registry_pid = os.getpid()
_registry: dict[str, _RootMutex] = {}
_thread = threading.local()


def _value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def _kind(action: Any) -> str:
    raw = _value(action, "kind", "")
    return str(getattr(raw, "value", raw))


def action_target_ids(action: Any) -> frozenset[str] | None:
    kind = _kind(action)
    if kind not in {
        "delete_conversation", "delete_pi_session", "delete_claude_session",
        "remove_desktop_state", "remove_frontend_reference", "delete_frontend_session",
        "delete_project_item", "delete_native_project", "remove_broken_relation",
        "delete_workbuddy_session", "remove_workbuddy_ui_reference",
    }:
        return None
    target = _value(action, "target", {})
    impact = _value(action, "impact", {})
    ids = {str(value) for value in (
        _value(target, "thread_id"), _value(action, "thread_id"),
    ) if value}
    for item in (action, impact):
        for key in ("affected_thread_ids", "descendant_thread_ids", "indexed_thread_ids"):
            ids.update(str(value) for value in (_value(item, key, ()) or ()) if value)
    payload = _value(impact, "external_action_payload", {}) or {}
    for key in ("frontend_session_id", "project_id"):
        if _value(payload, key):
            ids.add(str(_value(payload, key)))
    native_project = _value(payload, "native_project_evidence", {}) or {}
    ids.update(str(value) for value in (_value(native_project, "mapped_ids", ()) or ()) if value)
    # UI targets and native targets are different IDs. Exact frozen reference
    # and session evidence connects them within this operation root; ignoring
    # either end would let a frontend batch bypass a native unknown batch.
    id_fields = frozenset({
        "thread_id", "native_session_id", "session_id", "sdk_session_id",
        "expected_sdk_session_id", "cindy_session_id", "conversation_id",
        "frontend_session_id", "platform_session_id", "project_id",
        "parent_thread_id", "child_thread_id", "parent_id", "child_id",
    })
    nested_fields = frozenset({
        "expected", "locator", "native_session", "cindy_references",
        "frontend_references", "frontend_reference_snapshot", "frontend_reference_evidence",
        "frontend_session_evidence", "relation_evidence", "frontend_project_evidence",
    })

    def collect(item: Any) -> None:
        if isinstance(item, (tuple, list)):
            for member in item:
                collect(member)
        elif isinstance(item, Mapping):
            for key in id_fields:
                value = item.get(key)
                if isinstance(value, str) and value:
                    ids.add(value)
            for key in nested_fields:
                collect(item.get(key))

    collect(payload)
    collect(impact.to_dict() if callable(getattr(impact, "to_dict", None)) else impact)
    paths = [*(_value(impact, "external_artifact_paths", ()) or ()),
             *(_value(impact, "rollout_paths", ()) or ())]
    if kind in {"delete_workbuddy_session", "remove_workbuddy_ui_reference"}:
        evidence = _value(payload, "workbuddy_session_evidence", {}) or {}
        # SQLite and JSON rollback affect shared files. Different session IDs
        # cannot pass an unresolved sibling that owns those same files.
        paths.extend(_value(evidence, "shared_paths", ()) or ())
        if not paths:
            return None
    if kind in {"delete_pi_session", "delete_claude_session"}:
        paths.extend(_value(payload, "transcript_paths", ()) or ())
        if _value(payload, "path"):
            paths.append(_value(payload, "path"))
        if _value(_value(payload, "file", {}), "path"):
            paths.append(_value(_value(payload, "file", {}), "path"))
        for entry in (_value(payload, "manifest", ()) or ()):
            paths.append(_value(entry, "path"))
        if not paths:
            return None
    markers = _path_markers(paths)
    if markers is None:
        return None
    ids.update(markers)
    # The absence of a target is a root-wide limit, never empty permission.
    return frozenset(ids) if ids else None


def _path_markers(paths: Iterable[Any]) -> frozenset[str] | None:
    result = set()
    for path in paths:
        if not isinstance(path, (str, os.PathLike)) or not os.path.isabs(os.fspath(path)):
            return None
        # No filesystem resolution: an old missing file or newly replaced
        # symlink must retain exactly the old plan's lexical path footprint.
        result.add("path:" + os.path.normcase(os.path.normpath(os.fspath(path))))
    return frozenset(result)


def _rebase_target_paths(ids: frozenset[str] | None, anchor: Path, root: Path) -> frozenset[str] | None:
    if ids is None:
        return None
    lexical_anchor = os.path.normcase(os.path.abspath(os.fspath(anchor.expanduser())))
    result = set()
    for value in ids:
        if not value.startswith("path:"):
            result.add(value)
            continue
        path = value[5:]
        try:
            if os.path.commonpath((lexical_anchor, path)) != lexical_anchor:
                return None
            relative = os.path.relpath(path, lexical_anchor)
        except (OSError, ValueError):
            return None
        # Only the existing root has proven identity. Rebase its frozen
        # lexical descendants without resolving a missing/replaced artifact.
        # This covers long/8.3/extended root aliases, not undiscovered aliases
        # of individual files or copies outside the approved root.
        result.add("path:" + os.path.normcase(os.path.normpath(os.path.join(root, relative))))
    return frozenset(result)


def _merge_scopes(scopes: Iterable[MutationScope]) -> tuple[MutationScope, ...]:
    grouped: dict[str, MutationScope] = {}
    for scope in scopes:
        try:
            resolved = scope.root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise OperationStoreError(f"Could not prove mutation storage root: {scope.root}: {exc}") from exc
        root = Path(canonical_existing_path_key(resolved))
        key = os.path.normcase(os.fspath(root))
        prior = grouped.get(key)
        ids = _rebase_target_paths(scope.target_ids, scope.root, root)
        if prior is not None:
            ids = None if prior.target_ids is None or ids is None else prior.target_ids | ids
        grouped[key] = MutationScope(root, ids)
    return tuple(grouped[key] for key in sorted(grouped))


def scopes_for_actions(plan: Any, actions: Iterable[Any]) -> tuple[MutationScope, ...]:
    storage_paths = {
        str(_value(storage, "storage_id")): Path(_value(storage, "path"))
        for storage in (_value(plan, "storages", ()) or ())
    }
    scopes = []
    for action in actions:
        target = _value(action, "target", {})
        storage_id = str(_value(target, "storage_id", _value(action, "storage_id", "")))
        root = storage_paths.get(storage_id)
        if root is None:
            raw_root = _value(_value(action, "impact", {}), "external_storage_root")
            if raw_root:
                root = Path(raw_root)
        if root is None:
            raise OperationStoreError("Mutation target has no trusted local storage root")
        scopes.append(MutationScope(root, action_target_ids(action)))
    return _merge_scopes(scopes)


def scopes_for_frozen_plan(plan: Mapping[str, Any]) -> tuple[MutationScope, ...]:
    if plan.get("schema_version") == "larj.child-operation-plan.v2":
        from .orca_target_safety import EVIDENCE_SCHEMA, LEGACY_EVIDENCE_SCHEMA, FRONTEND_EVIDENCE_SCHEMA, evidence_for_actions
        boundary = plan.get("startup_boundary")
        target = plan.get("target", {})
        if (not isinstance(boundary, Mapping) or boundary.get("schema_version") != "larj.orca-startup-boundary.v1"
                or boundary.get("coordination_scope") != "root_wide"
                or boundary.get("home") != target.get("codex_home")
                or not isinstance(boundary.get("target_safety_evidence"), list)
                or any(value.get("schema_version") not in {EVIDENCE_SCHEMA, LEGACY_EVIDENCE_SCHEMA, FRONTEND_EVIDENCE_SCHEMA} or value.get("native_delete") is not True
                       or value.get("api_boundary") != "validated_fixed_runtime"
                       for value in boundary["target_safety_evidence"])):
            raise OperationStoreError("Orca child has no trustworthy frozen startup scope")
        projection = {"actions": plan.get("actions"), "target_safety_evidence": boundary["target_safety_evidence"],
                      "storages": [{"storage_id": target.get("storage_id"), "path": target.get("codex_home")} ]}
        try:
            evidence_for_actions(projection, projection["actions"])
        except (ValueError, KeyError, TypeError) as exc:
            raise OperationStoreError("Orca child startup scope differs from approval") from exc
        return _merge_scopes((MutationScope(Path(target["codex_home"]), None),))
    if plan.get("schema_version") == "larj.child-operation-plan.v1":
        actions = plan.get("actions")
    elif plan.get("schema_version") == "larj.agent-plan.v1":
        actions = _value(plan.get("authorization", {}), "root_actions")
    else:
        raise OperationStoreError("Unknown journal plan schema cannot authorize mutation")
    if not isinstance(actions, list) or any(not isinstance(item, Mapping) for item in actions):
        raise OperationStoreError("Journal plan has no trustworthy frozen action scope")
    root = Path(str(_value(plan.get("target", {}), "codex_home", "")))
    ids: frozenset[str] | None = frozenset() if actions else None
    for action in actions:
        item_ids = action_target_ids(action)
        ids = None if ids is None or item_ids is None else ids | item_ids
    return _merge_scopes((MutationScope(root, ids),))


def scopes_for_manual_plan(plan: Any) -> tuple[MutationScope, ...]:
    scopes = []
    for action in plan.actions:
        markers = _path_markers(getattr(getattr(action, "expected_scope", None), "rollout_paths", ()))
        ids = frozenset(str(value) for value in (action.thread_id, *getattr(action, "descendants", ())))
        scopes.append(MutationScope(Path(action.codex_home), None if markers is None else ids | markers))
    return _merge_scopes(scopes)


def frozen_operation_roots(document: Mapping[str, Any]) -> tuple[Path, ...]:
    paths = {str(storage.get("storage_id")): Path(str(storage.get("path")))
             for storage in document.get("storages", ()) if isinstance(storage, Mapping)}
    batches = document.get("child_batches", ())
    involved = {str(batch.get("storage_id")) for batch in batches if isinstance(batch, Mapping)}
    if not involved:
        involved = {str(_value(_value(action, "target", {}), "storage_id", _value(action, "storage_id")))
                    for action in document.get("actions", ())}
    if involved - paths.keys():
        raise OperationStoreError("Frozen operation has an unresolved mutation root")
    extra = set()
    from .herdr_cleanup import evidence_from_document, validate as validate_herdr
    herdr = evidence_from_document(document)
    if herdr is not None:
        validate_herdr(herdr)
        for target in herdr["native_targets"]:
            # Pi's durable operation root is agent_dir; session_root is a
            # bounded transcript layout and must not receive journal trees.
            extra.add(Path(target["binding"]["agent_dir"] if target["engine"] == "pi" else target["root"]))
    for action in document.get("actions", ()):
        if action.get("kind") == "delete_orca_frontend":
            from .orca_cleanup import validate_action
            from .orca_discovery import prove_account_home
            closure = validate_action(document, action)
            for record in closure["records"]:
                extra.add(prove_account_home(Path(closure["root"]), Path(record["home"])))
    return tuple(scope.root for scope in _merge_scopes(MutationScope(path, None)
                 for path in {*extra, *(paths[key] for key in involved)}))


def _operations_directory(root: Path) -> Path:
    _validate_directory(root, _required_lstat(root))
    current = root
    for part in (".local-agent-record-janitor", "operations"):
        candidate = current / part
        if _optional_lstat(candidate) is None:
            try:
                candidate.mkdir(mode=0o700)
                _fsync_directory(current)
            except FileExistsError:
                pass
        _validate_directory(candidate, _required_lstat(candidate))
        current = candidate
    if current.resolve(strict=True) != root / ".local-agent-record-janitor" / "operations":
        raise OperationStoreError("Mutation lock directory escapes the trusted root")
    return current


def _os_lock(fd: int, *, unlock: bool = False) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _reset_process_state_if_needed() -> None:
    global _registry_pid, _registry, _registry_lock
    if _registry_pid != os.getpid():
        # A fork must not reuse a parent's held RLock or inherited descriptor.
        for inherited in _registry.values():
            if inherited.fd is not None:
                os.close(inherited.fd)
        _registry_pid = os.getpid()
        _registry = {}
        _registry_lock = threading.Lock()
        _thread.tickets = ()
        _thread.roots = ()


@contextmanager
def _root_lock(root: Path) -> Iterator[None]:
    _reset_process_state_if_needed()
    key = os.path.normcase(os.fspath(root))
    with _registry_lock:
        mutex = _registry.setdefault(key, _RootMutex())
    if not mutex.lock.acquire(blocking=False):
        raise MutationRootLockedError(f"Another thread owns the mutation root: {root}")
    fd: int | None = None
    try:
        if mutex.depth:
            mutex.depth += 1
            try:
                yield
            finally:
                mutex.depth -= 1
            return
        directory = _operations_directory(root)
        lock_path = directory / ".mutation.lock"
        before = _optional_lstat(lock_path)
        if before is not None:
            _validate_regular_file(lock_path, before)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        created = False
        if before is None:
            try:
                fd = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                before = _required_lstat(lock_path)
                _validate_regular_file(lock_path, before)
        if fd is None:
            fd = os.open(lock_path, flags)
        os.set_inheritable(fd, False)
        opened = os.fstat(fd)
        _validate_regular_file(lock_path, opened)
        identity = _file_identity(opened)
        if (before is not None and _file_identity(before) != identity) or _file_identity(_required_lstat(lock_path)) != identity:
            raise OperationStoreError("Mutation lock identity changed while opening it")
        if created:
            os.fsync(fd)
            _fsync_directory(directory)
        try:
            _os_lock(fd)
        except OSError as exc:
            raise MutationRootLockedError(f"Another process owns the mutation root: {root}") from exc
        mutex.fd, mutex.depth = fd, 1
        try:
            _operations_directory(root)
            _validate_regular_file(lock_path, _required_lstat(lock_path))
            if _file_identity(_required_lstat(lock_path)) != identity:
                raise OperationStoreError("Mutation lock identity changed while held")
            yield
        finally:
            mutex.fd, mutex.depth = None, 0
            _os_lock(fd, unlock=True)
            _operations_directory(root)
            after = _required_lstat(lock_path)
            _validate_regular_file(lock_path, after)
            if _file_identity(after) != identity:
                raise OperationStoreError("Mutation lock identity changed while held")
    finally:
        if fd is not None:
            os.close(fd)
        mutex.lock.release()


@contextmanager
def mutation_roots(roots: Iterable[Path]) -> Iterator[None]:
    """Hold sorted physical roots, including verification and publication."""
    _reset_process_state_if_needed()
    scopes = _merge_scopes(MutationScope(Path(root), None) for root in roots)
    previous = getattr(_thread, "roots", ())
    keys = tuple(os.path.normcase(os.fspath(scope.root)) for scope in scopes)
    if previous and any(key not in previous for key in keys):
        raise OperationStoreError("Nested mutation may not add an unfrozen storage root")
    with ExitStack() as stack:
        for scope in scopes:
            stack.enter_context(_root_lock(scope.root))
        _thread.roots = tuple(sorted(set(previous) | set(keys)))
        try:
            yield
        finally:
            _thread.roots = previous


def _covers(outer: MutationScope, inner: MutationScope) -> bool:
    return outer.root == inner.root and (
        outer.target_ids is None or
        (inner.target_ids is not None and inner.target_ids <= outer.target_ids)
    )


def _journal_unknown(store: OperationStore) -> tuple[bool, bool]:
    """Return occupancy and the durable irreversible-attempt marker."""
    locked = store.lock_exists()
    result = store.read_result()
    if result is not None and result.get("compacted") is True:
        return locked, bool(result.get("mutation_started"))
    state = store.read_state()
    events = store.read_events()
    if state is None:
        if events or store.lock_exists():
            raise OperationStoreError("Journal has activity without its durable state")
        return True, False
    if state.get("next_event_sequence", 1) != len(events) + 1:
        raise OperationStoreError("Journal state is behind its durable events")
    started = any(bool(state.get(key)) for key in (
        "mutation_started", "mutation_attempted", "attempted",
    )) or any(event.get("event") == "mutation_started" for event in events)
    goal = str(state.get("goal_status") or "")
    terminal = bool(events and events[-1].get("goal_status") == goal
                    and events[-1].get("event") in {"batch_finished", "operation_finished", "verification_finished"}
                    and goal in {"complete", "completed_with_residuals"}
                    and state.get("phase") == "finished")
    if result is not None and result.get("goal_status") != goal:
        raise OperationStoreError("Journal result contradicts its durable state")
    if terminal:
        return locked, started
    safe_unstarted = bool(not started and not state.get("modified")
                          and state.get("current_action_state") == "not_started"
                          and (state.get("phase") == "preflight"
                               or (state.get("phase") == "blocked" and goal == "blocked")))
    return bool(not safe_unstarted or locked), started


def _check_siblings(scope: MutationScope, owner: OperationStore | None) -> None:
    tickets = getattr(_thread, "tickets", ())
    for entry in sorted(_operations_directory(scope.root).iterdir()):
        if entry.name == ".mutation.lock":
            continue
        _validate_directory(entry, _required_lstat(entry))
        store = OperationStore(scope.root, entry.name)
        if owner is not None and store.directory == owner.directory:
            continue
        if any(ticket.operation_directory == store.directory for ticket in tickets):
            continue
        try:
            if store.receipt_path.exists():
                # Admission never performs expiry/compaction cleanup. Known
                # receipts remain known; unknown journals never have a TTL.
                if store._read_receipt() is not None and not store.lock_exists():
                    continue
            frozen = store.read_plan()
            frozen_scope = scopes_for_frozen_plan(frozen)[0]
            unknown, _ = _journal_unknown(store)
        except OperationStoreError as exc:
            raise UnknownMutationError(f"Untrusted sibling journal blocks root {scope.root}: {entry.name}: {exc}") from exc
        if unknown and (scope.target_ids is None or frozen_scope.target_ids is None
                        or scope.target_ids & frozen_scope.target_ids):
            raise UnknownMutationError(
                f"Unknown operation {store.operation_id} occupies targets in {scope.root}; run its status/verify before new mutation"
            )


def check_operation_root_admission(document: Mapping[str, Any]) -> None:
    """Check every frozen child root before the first cross-store mutation.

    The coordinator must hold mutation_roots for this whole interval. This
    grants no execution ticket and cannot bypass individual child admission.
    """
    held = set(getattr(_thread, "roots", ()))
    for root in frozen_operation_roots(document):
        if os.path.normcase(os.fspath(root)) not in held:
            raise OperationStoreError("Operation admission requires all frozen root locks")
        _check_siblings(MutationScope(root, None), None)


@contextmanager
def mutation_guard(scopes: Iterable[MutationScope], *, store: OperationStore | None = None) -> Iterator[None]:
    selected = _merge_scopes(scopes)
    if not selected:
        yield
        return
    with mutation_roots(scope.root for scope in selected):
        previous = getattr(_thread, "tickets", ())
        if previous:
            if store is not None:
                frozen = store.read_plan()
                owner_key = canonical_existing_path_key(store.directory)
                if not any(ticket.operation_directory is not None
                           and canonical_existing_path_key(ticket.operation_directory) == owner_key
                           and ticket.plan_hash == frozen.get("plan_sha256") for ticket in previous):
                    raise OperationStoreError("Nested journal does not own the execution ticket")
            if not all(any(_covers(outer, inner) for ticket in previous for outer in ticket.scopes)
                       for inner in selected):
                raise OperationStoreError("Nested mutation exceeds the frozen execution ticket")
            yield
            return
        frozen_hash: str | None = None
        if store is not None:
            frozen = store.read_plan()
            allowed = scopes_for_frozen_plan(frozen)
            if not all(any(_covers(outer, inner) for outer in allowed) for inner in selected):
                raise OperationStoreError("Mutation exceeds its accepted journal plan")
            unknown, started = _journal_unknown(store)
            if unknown or started:
                raise UnknownMutationError("An ambiguous or already attempted operation cannot receive a mutation ticket",
                                           recovery_required=True)
            state = store.read_state()
            if state is not None and not (
                state.get("phase") == "preflight"
                or (state.get("goal_status") == "blocked"
                    and not state.get("modified")
                    and not state.get("attempted")
                    and state.get("current_action_state") == "not_started")
            ):
                raise UnknownMutationError("Only unstarted or proven pre-mutation blocked journals are resumable",
                                           recovery_required=True)
            frozen_hash = str(frozen["plan_sha256"])
        for scope in selected:
            _check_siblings(scope, store)
        _thread.tickets = (*previous, _Ticket(selected, None if store is None else store.directory, frozen_hash))
        try:
            yield
        finally:
            _thread.tickets = previous


def guard_manual_execution(function: Any) -> Any:
    @wraps(function)
    def execute(plan: Any, *args: Any, **kwargs: Any) -> Any:
        # Keep pre-existing authorization errors ahead of filesystem work.
        if (not kwargs.get("clients_closed") or not plan.selected or not plan.plan_fingerprint
                or plan.plan_fingerprint != kwargs.get("approved_plan_fingerprint")):
            return function(plan, *args, **kwargs)
        try:
            with mutation_guard(scopes_for_manual_plan(plan)):
                return function(plan, *args, **kwargs)
        except OperationStoreError as exc:
            from .manual_delete import ManualDeletePlanError
            error = ManualDeletePlanError(str(exc))
            error.kind = getattr(exc, "kind", "mutation_root_untrusted")
            raise error from exc
    return execute


def guard_gui_execution(function: Any) -> Any:
    @wraps(function)
    def execute(plan: Any, *args: Any, **kwargs: Any) -> Any:
        if (not kwargs.get("clients_closed")
                or plan.plan_fingerprint != kwargs.get("approved_plan_fingerprint")):
            return function(plan, *args, **kwargs)
        scopes = (*scopes_for_manual_plan(plan.native_plan), *(
            MutationScope(target.codex_home, frozenset((target.thread_id,)))
            for target in plan.desktop_targets
        ))
        with mutation_guard(scopes):
            return function(plan, *args, **kwargs)
    return execute


def guard_finding_execution(function: Any) -> Any:
    @wraps(function)
    def execute(findings: Iterable[Any], *args: Any, **kwargs: Any) -> Any:
        rows = tuple(findings)
        descendants = kwargs.get("approved_descendants") or {}
        scopes = []
        for finding in rows:
            if not finding.has_codex_artifacts:
                continue
            root = finding.codex_home
            key = (canonical_existing_path_key(root), finding.thread_id)
            approved = descendants.get(key, descendants.get(finding.thread_id, ()))
            expected = (kwargs.get("expected_scopes") or {}).get(key)
            paths = getattr(expected, "rollout_paths", ())
            if not paths:
                paths = [*((finding.details or {}).get("rollout_paths", ()) or ())]
                if finding.rollout is not None:
                    paths.append(finding.rollout.path)
            markers = _path_markers(paths)
            scopes.append(MutationScope(root, None if markers is None else frozenset((finding.thread_id, *approved)) | markers))
        with mutation_guard(scopes):
            return function(rows, *args, **kwargs)
    return execute
