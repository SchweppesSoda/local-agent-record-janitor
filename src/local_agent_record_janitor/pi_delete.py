"""Fail-closed, file-level deletion for Pi session transcripts.

Pi has no local equivalent of Codex's ``thread/delete`` RPC.  This module is
therefore deliberately separate from :mod:`manual_delete`: it approves and
unlinks one exact transcript file, never an enclosing directory or a child
session.  The inventory is rebuilt before *every* unlink.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat as stat_module
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class PiDeletePlanError(ValueError):
    """A Pi transcript deletion is incomplete, stale, or unsafe."""


class PiDeleteSelectionError(PiDeletePlanError):
    """A selector did not name exactly one executable Pi transcript."""


@dataclass(frozen=True)
class PiDeleteAction:
    """One storage-qualified, permanent unlink of one Pi ``.jsonl`` file."""

    action_id: str
    pi_root: Path
    session_root: Path
    path: Path
    relative_path: str
    session_id: str
    version: int | None
    timestamp: str | None
    cwd: str | None
    parent_session: str | None
    child_paths: tuple[Path, ...]
    active: bool
    deletable: bool
    blockers: tuple[str, ...]
    stat_size: int
    stat_mtime_ns: int
    stat_dev: int
    stat_ino: int
    stat_mode: int
    stat_ctime_ns: int
    stat_file_attributes: int
    sha256: str
    catalog_blocking_failures: tuple[str, ...]
    available: bool
    unavailable_reasons: tuple[str, ...]
    snapshot_fingerprint: str
    record: Any = field(repr=False, compare=False)
    risk: str = "high"
    storage_kind: str = "standalone"
    cindy_profile_root: Path | None = None
    reference_classification: str = "unreferenced"
    cindy_references: tuple[Mapping[str, Any], ...] = ()

    @property
    def preserved_child_references(self) -> tuple[str, ...]:
        """Child transcript paths that are deliberately not cascaded."""
        return tuple(str(path) for path in self.child_paths)

    def approval_payload(self) -> dict[str, Any]:
        """Every fact that must still hold when the unlink happens."""
        return {
            "schema_version": 1,
            "action_id": self.action_id,
            "pi_root": _normal_path(self.pi_root),
            "session_root": _normal_path(self.session_root),
            "path": _normal_path(self.path),
            "relative_path": self.relative_path,
            "session_id": self.session_id,
            # Pi's first JSONL header is represented by these parsed fields.
            "header": {
                "session_id": self.session_id,
                "version": self.version,
                "timestamp": self.timestamp,
                "cwd": self.cwd,
                "parent_session": self.parent_session,
            },
            "child_paths": [_normal_path(item) for item in self.child_paths],
            "active": self.active,
            "deletable": self.deletable,
            "blockers": list(self.blockers),
            "stat": {
                "dev": self.stat_dev,
                "ino": self.stat_ino,
                "mode": self.stat_mode,
                "size": self.stat_size,
                "mtime_ns": self.stat_mtime_ns,
                "ctime_ns": self.stat_ctime_ns,
                "file_attributes": self.stat_file_attributes,
            },
            "sha256": self.sha256,
            "catalog_blocking_failures": list(self.catalog_blocking_failures),
            "storage_kind": self.storage_kind,
            "cindy_profile_root": (
                _normal_path(self.cindy_profile_root)
                if self.cindy_profile_root is not None
                else None
            ),
            "reference_classification": self.reference_classification,
            "cindy_references": list(self.cindy_references),
            "risk": self.risk,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.approval_payload(),
            "available": self.available,
            "unavailable_reasons": list(self.unavailable_reasons),
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "preserved_child_references": list(self.preserved_child_references),
        }


@dataclass(frozen=True)
class PiDeletePlan:
    actions: tuple[PiDeleteAction, ...] = ()
    errors: tuple[str, ...] = ()
    selected: bool = False
    plan_fingerprint: str | None = None

    @property
    def executable_actions(self) -> tuple[PiDeleteAction, ...]:
        return tuple(action for action in self.actions if action.available)

    def with_selected_actions(self, selectors: Iterable[str]) -> "PiDeletePlan":
        raw = tuple(selectors)
        if not raw:
            raise PiDeleteSelectionError("At least one explicit Pi action is required")
        selected: list[PiDeleteAction] = []
        selected_ids: set[str] = set()
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise PiDeleteSelectionError("Pi delete selectors must be non-empty strings")
            selector = item.strip()
            if selector.lower() == "all":
                raise PiDeleteSelectionError("Pi deletion never accepts an all selector")
            matches = [action for action in self.actions if action.action_id == selector]
            if not matches:
                matches = [action for action in self.actions if action.session_id == selector]
            if not matches:
                raise PiDeleteSelectionError(f"Pi delete selector {selector!r} matched no action")
            if len(matches) != 1:
                raise PiDeleteSelectionError(f"Pi delete selector {selector!r} is ambiguous")
            action = matches[0]
            if not action.available:
                raise PiDeleteSelectionError(
                    f"Pi delete target {selector!r} is unavailable: "
                    + "; ".join(action.unavailable_reasons)
                )
            if action.action_id in selected_ids:
                raise PiDeleteSelectionError(f"Pi action {action.action_id!r} was selected more than once")
            selected.append(action)
            selected_ids.add(action.action_id)
        # Parent/child references are informational: Pi has no cascade operation.
        # Selecting both is two independently fingerprinted unlinks, not an
        # implied request to recursively delete a tree.
        ordered = tuple(sorted(selected, key=lambda action: action.action_id))
        fingerprint = _fingerprint({"schema_version": 1, "actions": [item.approval_payload() for item in ordered]})
        return PiDeletePlan(actions=ordered, selected=True, plan_fingerprint=fingerprint)

    def to_dict(self) -> dict[str, Any]:
        return {"selected": self.selected, "plan_fingerprint": self.plan_fingerprint,
                "actions": [action.to_dict() for action in self.actions], "errors": list(self.errors)}


@dataclass(frozen=True)
class PiDeleteItemResult:
    action_id: str
    path: Path
    session_id: str
    status: str  # deleted | not_deleted | unknown
    error: str | None = None
    preserved_child_references: tuple[str, ...] = ()


@dataclass(frozen=True)
class PiDeleteResult:
    results: tuple[PiDeleteItemResult, ...]

    @property
    def deleted(self) -> tuple[PiDeleteItemResult, ...]:
        return tuple(item for item in self.results if item.status == "deleted")

    @property
    def not_deleted(self) -> tuple[PiDeleteItemResult, ...]:
        return tuple(item for item in self.results if item.status == "not_deleted")

    @property
    def unknown(self) -> tuple[PiDeleteItemResult, ...]:
        return tuple(item for item in self.results if item.status == "unknown")

    def to_dict(self) -> dict[str, Any]:
        return {"results": [{"action_id": item.action_id, "path": str(item.path),
                              "session_id": item.session_id, "status": item.status,
                              "error": item.error,
                              "preserved_child_references": list(item.preserved_child_references)}
                             for item in self.results]}


def build_pi_delete_plan(catalog: Any) -> PiDeletePlan:
    """Create a read-only plan from a complete Pi inventory (fail closed)."""
    records, record_error = _sequence_attr(catalog, ("records", "sessions"))
    failures, failure_error = _sequence_attr(catalog, ("errors", "failures"))
    errors = [*record_error, *failure_error]
    global_blocking, scoped_blocking, failure_messages = _partition_blocking_failures(
        failures, errors
    )
    seen_paths: dict[str, str] = {}
    seen_ids: dict[str, str] = {}
    valid: list[Any] = []
    for record in records:
        try:
            action_id = _string(record, "action_id")
            path = _path(record, "path")
            key = _normal_path(path)
        except PiDeletePlanError as exc:
            errors.append(str(exc))
            continue
        if key in seen_paths:
            errors.append(f"Pi inventory contains duplicate transcript target {key}")
        else:
            seen_paths[key] = action_id
        if action_id in seen_ids and seen_ids[action_id] != key:
            errors.append(f"Pi inventory contains duplicate stable action_id {action_id!r}")
        else:
            seen_ids[action_id] = key
        _string(record, "session_id")
        valid.append(record)
    if errors:
        structure_error = "catalog structure is incomplete: " + "; ".join(
            dict.fromkeys(errors)
        )
        global_blocking = (*global_blocking, structure_error)
    duplicate_paths = {path for path, action_id in seen_paths.items()
                       if sum(1 for item in valid if _normal_path(_path(item, "path")) == path) > 1}
    duplicate_ids = {action_id for action_id in seen_ids
                     if sum(1 for item in valid if _string(item, "action_id") == action_id) > 1}
    actions = tuple(_make_action(
                        record,
                        _blocking_for_record(record, global_blocking, scoped_blocking),
                        duplicate_paths,
                        duplicate_ids,
                    )
                    for record in sorted(valid, key=lambda item: _normal_path(_path(item, "path"))))
    return PiDeletePlan(
        actions=actions,
        errors=tuple(dict.fromkeys(errors + list(failure_messages))),
    )


def execute_pi_delete(
    plan: PiDeletePlan,
    *,
    catalog_builder: Callable[[], Any],
    approved_plan_fingerprint: str,
    clients_closed: bool,
    lstat_fn: Callable[[str | os.PathLike[str]], os.stat_result] = os.lstat,
    read_bytes_fn: Callable[[Path], bytes] | None = None,
    unlink_fn: Callable[[Path], None] | None = None,
) -> PiDeleteResult:
    """Rebuild, compare and permanently unlink precisely approved files only."""
    if not clients_closed:
        raise PiDeletePlanError("Pi deletion requires an explicit clients-closed confirmation")
    if not plan.selected or not plan.actions or not plan.plan_fingerprint:
        raise PiDeletePlanError("Pi deletion requires a non-empty selected plan")
    if not isinstance(approved_plan_fingerprint, str) or not hmac.compare_digest(plan.plan_fingerprint, approved_plan_fingerprint):
        raise PiDeletePlanError("The approved plan fingerprint does not match the selected plan")
    if not callable(catalog_builder):
        raise PiDeletePlanError("catalog_builder must be callable")
    # Full rebuild before side effects; selection also ensures newly blocked items fail.
    refreshed = build_pi_delete_plan(catalog_builder()).with_selected_actions(action.action_id for action in plan.actions)
    if not hmac.compare_digest(approved_plan_fingerprint, refreshed.plan_fingerprint or ""):
        raise PiDeletePlanError("The Pi deletion plan changed after approval; nothing was deleted")
    reader = read_bytes_fn or (lambda path: path.read_bytes())
    unlink = unlink_fn or (lambda path: path.unlink())
    results: list[PiDeleteItemResult] = []
    for approved in refreshed.actions:
        try:
            # A fresh catalog just before every unlink catches activity, children and
            # every inventory field, including catalog-wide blocking failures.
            current = build_pi_delete_plan(catalog_builder()).with_selected_actions((approved.action_id,)).actions[0]
            if not hmac.compare_digest(_fingerprint(approved.approval_payload()), _fingerprint(current.approval_payload())):
                raise PiDeletePlanError("Pi transcript inventory changed after approval")
            _verify_exact_file(current, lstat_fn=lstat_fn, read_bytes_fn=reader)
            unlink(current.path)
            try:
                lstat_fn(current.path)
            except FileNotFoundError:
                results.append(_result(current, "deleted"))
            else:
                results.append(_result(current, "unknown", "unlink returned but transcript still exists"))
        except PiDeletePlanError as exc:
            results.append(_result(approved, "not_deleted", str(exc)))
        except FileNotFoundError as exc:
            results.append(_result(approved, "not_deleted", f"transcript disappeared before deletion: {exc}"))
        except OSError as exc:
            results.append(_result(approved, "unknown", f"could not safely delete transcript: {exc}"))
        except Exception as exc:  # injected file functions and malformed data fail closed
            results.append(_result(approved, "unknown", f"unexpected deletion verification failure: {exc}"))
    return PiDeleteResult(tuple(results))


def _make_action(record: Any, blocking: tuple[str, ...], duplicate_paths: set[str], duplicate_ids: set[str]) -> PiDeleteAction:
    action_id = _string(record, "action_id")
    # ``agent_dir`` is Pi's public name.  ``pi_root`` remains accepted for
    # small third-party catalog shims produced before that name was settled.
    pi_root = _path_any(record, ("agent_dir", "pi_root"))
    session_root = _path(record, "session_root")
    path = _path(record, "path")
    session_id = _string(record, "session_id")
    version = _optional_integer(record, "version")
    timestamp = _optional_string(record, "timestamp")
    cwd = _optional_string(record, "cwd")
    parent = _optional_string(record, "parent_session")
    child_paths = tuple(sorted((_path_value(value) for value in _tuple_attr(record, "child_paths")), key=_normal_path))
    active = _boolean(record, "active")
    deletable = _boolean(record, "deletable")
    blockers = tuple(_tuple_attr(record, "blockers"))
    size = _integer(record, "stat_size")
    mtime = _integer(record, "stat_mtime_ns")
    stat_info: os.stat_result | None
    stat_problem: str | None = None
    try:
        stat_info = _safe_lstat(path)
    except PiDeletePlanError as exc:
        stat_info = None
        stat_problem = str(exc)
    dev = _stat_field(record, "stat_dev", stat_info.st_dev if stat_info else 0)
    ino = _stat_field(record, "stat_ino", stat_info.st_ino if stat_info else 0)
    mode = _stat_field(record, "stat_mode", stat_info.st_mode if stat_info else 0)
    ctime = _stat_field(record, "stat_ctime_ns", stat_info.st_ctime_ns if stat_info else 0)
    attributes = _stat_field(record, "stat_file_attributes", getattr(stat_info, "st_file_attributes", 0) if stat_info else 0)
    sha256 = _string(record, "sha256")
    storage_kind = getattr(record, "storage_kind", "standalone")
    if not isinstance(storage_kind, str) or not storage_kind:
        storage_kind = ""
    profile_value = getattr(record, "cindy_profile_root", None)
    cindy_profile_root = (
        _path_value(profile_value, "cindy_profile_root")
        if profile_value is not None
        else None
    )
    reference_classification = getattr(record, "reference_classification", "unreferenced")
    if not isinstance(reference_classification, str) or not reference_classification:
        reference_classification = ""
    reference_snapshots, reference_errors = _cindy_reference_snapshots(
        getattr(record, "cindy_references", ())
    )
    reasons: list[str] = []
    try:
        relative = _relative_path(path, session_root)
    except PiDeletePlanError as exc:
        relative = ""
        reasons.append(str(exc))
    if path.suffix.lower() != ".jsonl": reasons.append("Pi target is not a .jsonl transcript")
    if stat_problem:
        reasons.append(stat_problem)
    elif _stat_identity(stat_info) != (
        dev, ino, mode, size, mtime, ctime, attributes,
    ):
        reasons.append("Pi transcript changed after inventory and before delete planning")
    if active: reasons.append("Pi transcript is currently active")
    if not deletable: reasons.append("Pi inventory marks transcript as not deletable")
    reasons.extend(blockers)
    reasons.extend(blocking)
    if _normal_path(path) in duplicate_paths: reasons.append("Pi inventory has duplicate transcript target")
    if action_id in duplicate_ids: reasons.append("Pi inventory has duplicate action_id")
    if not storage_kind: reasons.append("Pi inventory has invalid storage_kind")
    if not reference_classification: reasons.append("Pi inventory has invalid reference_classification")
    reasons.extend(reference_errors)
    payload = {"action_id": action_id, "pi_root": _normal_path(pi_root), "session_root": _normal_path(session_root),
               "path": _normal_path(path), "relative_path": relative, "session_id": session_id, "version": version,
               "timestamp": timestamp, "cwd": cwd, "parent_session": parent,
               "child_paths": [_normal_path(item) for item in child_paths], "active": active, "deletable": deletable,
               "blockers": list(blockers), "stat": {"dev": dev, "ino": ino, "mode": mode, "size": size, "mtime_ns": mtime, "ctime_ns": ctime, "file_attributes": attributes}, "sha256": sha256,
               "catalog_blocking_failures": list(blocking), "storage_kind": storage_kind,
               "cindy_profile_root": _normal_path(cindy_profile_root) if cindy_profile_root else None,
               "reference_classification": reference_classification,
               "cindy_references": list(reference_snapshots), "risk": "high"}
    return PiDeleteAction(action_id, _absolute_path(pi_root), _absolute_path(session_root), _absolute_path(path),
                          relative, session_id, version, timestamp, cwd, parent, child_paths, active, deletable, blockers,
                           size, mtime, dev, ino, mode, ctime, attributes, sha256, blocking, not reasons, tuple(dict.fromkeys(reasons)), _fingerprint(payload), record,
                           storage_kind=storage_kind,
                           cindy_profile_root=(_absolute_path(cindy_profile_root) if cindy_profile_root else None),
                           reference_classification=reference_classification,
                           cindy_references=reference_snapshots)


def _verify_exact_file(action: PiDeleteAction, *, lstat_fn: Callable[[str | os.PathLike[str]], os.stat_result], read_bytes_fn: Callable[[Path], bytes]) -> None:
    path = action.path
    if _relative_path(path, action.session_root) != action.relative_path:
        raise PiDeletePlanError("approved target escaped its approved Pi session root")
    _reject_reparse_path(path, action.session_root, lstat_fn)
    info = lstat_fn(path)
    if not stat_module.S_ISREG(info.st_mode) or _is_reparse(info):
        raise PiDeletePlanError("approved Pi target is not a non-reparse regular file")
    if _stat_identity(info) != (
        action.stat_dev, action.stat_ino, action.stat_mode, action.stat_size,
        action.stat_mtime_ns, action.stat_ctime_ns, action.stat_file_attributes,
    ):
        raise PiDeletePlanError("Pi transcript stat changed after approval")
    contents = read_bytes_fn(path)
    if hashlib.sha256(contents).hexdigest() != action.sha256:
        raise PiDeletePlanError("Pi transcript content changed after approval")
    header = _read_header(contents)
    if _header_session_id(header) != action.session_id:
        raise PiDeletePlanError("Pi transcript header session id changed after approval")
    if _header_version(header) != action.version:
        raise PiDeletePlanError("Pi transcript header version changed after approval")
    if (_header_optional(header, ("timestamp",)) != action.timestamp
            or _header_optional(header, ("cwd",)) != action.cwd
            or _header_optional(header, ("parentSession", "parent_session", "parentId", "parent_id")) != action.parent_session):
        raise PiDeletePlanError("Pi transcript header changed after approval")
    # Read/hash validation itself is not the last operation: check the pathname
    # once more immediately before unlink so a replacement during validation is
    # not silently removed.  (The remaining OS-level race is inherent in a
    # pathname-only unlink API and is kept as small as this portable interface
    # permits.)
    final_info = lstat_fn(path)
    if (not stat_module.S_ISREG(final_info.st_mode) or _is_reparse(final_info)
            or _stat_identity(final_info) != _stat_identity(info)):
        raise PiDeletePlanError("Pi transcript changed or became unsafe before unlink")


def _read_header(contents: bytes) -> Mapping[str, Any]:
    try:
        first = next(line for line in contents.splitlines() if line.strip())
        value = json.loads(first)
    except (StopIteration, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiDeletePlanError(f"could not parse Pi transcript header: {exc}") from exc
    if not isinstance(value, Mapping): raise PiDeletePlanError("Pi transcript header is not a JSON object")
    return value


def _header_session_id(header: Mapping[str, Any]) -> str | None:
    return _header_optional(header, ("id", "sessionId", "session_id"))


def _header_version(header: Mapping[str, Any]) -> int | None:
    value = header.get("version")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _header_optional(header: Mapping[str, Any], names: Sequence[str]) -> str | None:
    for name in names:
        value = header.get(name)
        if value is None: continue
        return value if isinstance(value, str) else None
    return None


def _result(action: PiDeleteAction, status: str, error: str | None = None) -> PiDeleteItemResult:
    return PiDeleteItemResult(action.action_id, action.path, action.session_id, status, error, action.preserved_child_references)


def _partition_blocking_failures(
    failures: Sequence[Any],
    errors: Sequence[str],
) -> tuple[
    tuple[str, ...],
    Mapping[tuple[str, str], tuple[str, ...]],
    tuple[str, ...],
]:
    global_result = list(errors)
    scoped_result: dict[tuple[str, str], list[str]] = {}
    all_messages = list(errors)
    for failure in failures:
        blocks = getattr(failure, "blocks_delete", None)
        if blocks is False:
            continue
        if blocks is not True:
            reason = "catalog failure has no valid blocks_delete classification"
            global_result.append(reason)
            all_messages.append(reason)
            continue
        message = getattr(failure, "message", None)
        if not isinstance(message, str) or not message:
            message = "catalog failure has no valid message"
        reason = "Blocking Pi inventory failure: " + message
        all_messages.append(reason)
        scope = _failure_scope_key(failure)
        if scope is None:
            global_result.append(reason)
        else:
            scoped_result.setdefault(scope, []).append(reason)
    return (
        tuple(dict.fromkeys(global_result)),
        {
            key: tuple(dict.fromkeys(value))
            for key, value in scoped_result.items()
        },
        tuple(dict.fromkeys(all_messages)),
    )


def _blocking_for_record(
    record: Any,
    global_blocking: tuple[str, ...],
    scoped_blocking: Mapping[tuple[str, str], tuple[str, ...]],
) -> tuple[str, ...]:
    try:
        key = (
            _normal_path(_path_any(record, ("agent_dir", "pi_root"))),
            _normal_path(_path(record, "session_root")),
        )
    except PiDeletePlanError:
        return (
            *global_blocking,
            "Pi record storage scope cannot be safely qualified",
        )
    return tuple(dict.fromkeys((*global_blocking, *scoped_blocking.get(key, ()))))


def _failure_scope_key(failure: Any) -> tuple[str, str] | None:
    agent_value = getattr(failure, "agent_dir", None)
    if agent_value is None:
        agent_value = getattr(failure, "pi_root", None)
    session_value = getattr(failure, "session_root", None)
    if not _nonempty_path_value(agent_value) or not _nonempty_path_value(session_value):
        return None
    try:
        return (
            _normal_path(_path_value(agent_value, "failure.agent_dir")),
            _normal_path(_path_value(session_value, "failure.session_root")),
        )
    except PiDeletePlanError:
        return None


def _nonempty_path_value(value: Any) -> bool:
    return isinstance(value, (str, os.PathLike)) and bool(os.fspath(value))


def _cindy_reference_snapshots(
    values: Any,
) -> tuple[tuple[Mapping[str, Any], ...], list[str]]:
    if isinstance(values, (str, bytes)):
        return (), ["Pi inventory cindy_references is not a sequence"]
    try:
        references = tuple(values)
    except TypeError:
        return (), ["Pi inventory cindy_references is not iterable"]
    snapshots: list[Mapping[str, Any]] = []
    errors: list[str] = []
    for reference in references:
        try:
            if isinstance(reference, Mapping):
                payload = reference
            else:
                builder = getattr(reference, "approval_payload", None)
                if not callable(builder):
                    raise TypeError
                payload = builder()
            if not isinstance(payload, Mapping):
                raise TypeError
            normalized = _json_snapshot(payload)
            if not isinstance(normalized, Mapping):
                raise TypeError
            snapshots.append(normalized)
        except (TypeError, ValueError, OSError):
            errors.append("Pi inventory contains an invalid Cindy reference snapshot")
    snapshots.sort(
        key=lambda value: json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    )
    return tuple(snapshots), errors


def _json_snapshot(value: Any) -> Any:
    if isinstance(value, Path):
        return _normal_path(value)
    if isinstance(value, Mapping):
        return {str(key): _json_snapshot(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_snapshot(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("reference snapshot contains a non-JSON value")


def _sequence_attr(value: Any, names: Sequence[str]) -> tuple[tuple[Any, ...], list[str]]:
    for name in names:
        if hasattr(value, name):
            found = getattr(value, name)
            if isinstance(found, (str, bytes)):
                return (), [f"Pi catalog {name} is not a sequence"]
            try: return tuple(found), []
            except TypeError: return (), [f"Pi catalog {name} is not iterable"]
    return (), [f"Pi catalog is missing {'/'.join(names)}"]


def _string(value: Any, name: str) -> str:
    found = getattr(value, name, None)
    if not isinstance(found, str) or not found: raise PiDeletePlanError(f"Pi record has invalid {name}")
    return found


def _optional_string(value: Any, name: str) -> str | None:
    found = getattr(value, name, None)
    if found is not None and not isinstance(found, str): raise PiDeletePlanError(f"Pi record has invalid {name}")
    return found


def _integer(value: Any, name: str) -> int:
    found = getattr(value, name, None)
    if not isinstance(found, int) or isinstance(found, bool) or found < 0: raise PiDeletePlanError(f"Pi record has invalid {name}")
    return found


def _optional_integer(value: Any, name: str) -> int | None:
    found = getattr(value, name, None)
    if found is None:
        return None
    if not isinstance(found, int) or isinstance(found, bool) or found < 0:
        raise PiDeletePlanError(f"Pi record has invalid {name}")
    return found


def _boolean(value: Any, name: str) -> bool:
    found = getattr(value, name, None)
    if not isinstance(found, bool): raise PiDeletePlanError(f"Pi record has invalid {name}")
    return found


def _path(value: Any, name: str) -> Path: return _path_value(getattr(value, name, None), name)
def _path_any(value: Any, names: Sequence[str]) -> Path:
    for name in names:
        found = getattr(value, name, None)
        if found is not None:
            return _path_value(found, name)
    raise PiDeletePlanError(f"Pi record has invalid {'/'.join(names)}")
def _path_value(value: Any, name: str = "path") -> Path:
    if not isinstance(value, (str, os.PathLike)): raise PiDeletePlanError(f"Pi record has invalid {name}")
    return Path(value)
def _tuple_attr(value: Any, name: str) -> tuple[Any, ...]:
    found = getattr(value, name, None)
    if isinstance(found, (str, bytes)):
        raise PiDeletePlanError(f"Pi record has invalid {name}")
    try: return tuple(found)
    except TypeError as exc: raise PiDeletePlanError(f"Pi record has invalid {name}") from exc
def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise PiDeletePlanError(f"could not inspect Pi transcript for planning: {exc}") from exc
def _stat_field(value: Any, name: str, fallback: int) -> int:
    found = getattr(value, name, None)
    if found is None:
        return fallback
    if not isinstance(found, int) or isinstance(found, bool) or found < 0:
        raise PiDeletePlanError(f"Pi record has invalid {name}")
    return found
def _relative_path(path: Path, root: Path) -> str:
    # Do not resolve here: resolving a target symlink can make an escaped path
    # appear safe.  Reparse points are rejected component-by-component at use.
    try: return _absolute_path(path).relative_to(_absolute_path(root)).as_posix()
    except ValueError as exc: raise PiDeletePlanError("Pi transcript path is outside its session root") from exc
def _absolute_path(path: Path) -> Path: return Path(os.path.abspath(os.fspath(path)))
def _normal_path(path: Path) -> str: return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))
def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (getattr(info, "st_dev", 0), getattr(info, "st_ino", 0), info.st_mode,
            info.st_size, info.st_mtime_ns, getattr(info, "st_ctime_ns", 0),
            getattr(info, "st_file_attributes", 0))
def _reject_reparse_path(path: Path, root: Path, lstat_fn: Callable[[str | os.PathLike[str]], os.stat_result]) -> None:
    """Refuse a final target *or any ancestor* that can redirect traversal."""
    root = _absolute_path(root)
    target = _absolute_path(path)
    relative = target.relative_to(root)
    candidate = root
    for component in ((), *[(part,) for part in relative.parts]):
        if component:
            candidate = candidate / component[0]
        info = lstat_fn(candidate)
        if _is_reparse(info) or stat_module.S_ISLNK(info.st_mode):
            raise PiDeletePlanError("approved Pi path contains a symlink or reparse point")
def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = ["PiDeleteAction", "PiDeleteItemResult", "PiDeletePlan", "PiDeletePlanError", "PiDeleteResult", "PiDeleteSelectionError", "build_pi_delete_plan", "execute_pi_delete"]
