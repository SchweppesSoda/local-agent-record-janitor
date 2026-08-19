from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .legacy_index import _fsync_directory


OPERATION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_STATE_SCHEMA = "larj.agent-state.v1"
_RESULT_SCHEMA = "larj.agent-result.v1"
_GOAL_STATUSES = frozenset(
    {"unknown", "blocked", "complete", "completed_with_residuals"}
)
_STATE_PHASES = frozenset(
    {"preflight", "blocked", "executing", "finished", "recovery_required"}
)


class OperationStoreError(RuntimeError):
    """An operation journal could not be trusted or persisted safely."""


class OperationLockedError(OperationStoreError):
    """Another process may still own the operation mutation gate."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_operation_id(operation_id: str) -> str:
    if (
        not OPERATION_ID_PATTERN.fullmatch(operation_id)
        or operation_id in {".", ".."}
    ):
        raise OperationStoreError("Invalid operation ID")
    return operation_id


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def plan_payload_without_hash(plan: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(plan)
    payload.pop("plan_sha256", None)
    return payload


def plan_sha256(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(plan_payload_without_hash(plan))
    ).hexdigest()


def strict_json_load(path: Path) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise OperationStoreError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw, object_pairs_hook=reject_duplicates)
    except OperationStoreError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperationStoreError(f"Could not read trusted JSON {path}: {exc}") from exc


def write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(value) + b"\n"
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        raise OperationStoreError(f"Refusing to overwrite existing file: {path}") from exc
    except OSError as exc:
        raise OperationStoreError(f"Could not write {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.agent-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(canonical_json_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError as exc:
        raise OperationStoreError(f"Could not atomically write {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class OperationStore:
    def __init__(self, codex_home: Path, operation_id: str) -> None:
        self.codex_home = codex_home.expanduser().resolve()
        self.operation_id = validate_operation_id(operation_id)
        self.directory = (
            self.codex_home
            / ".local-agent-record-janitor"
            / "operations"
            / self.operation_id
        )
        self.plan_path = self.directory / "plan.json"
        self.events_path = self.directory / "events.jsonl"
        self.state_path = self.directory / "state.json"
        self.result_path = self.directory / "result.json"
        self.lock_path = self.directory / "apply.lock"

    def accept_plan(self, plan: Mapping[str, Any]) -> None:
        self._ensure_directory(create=True)
        expected_hash = str(plan.get("plan_sha256") or "")
        self._validate_plan_binding(plan)
        plan_state = _optional_lstat(self.plan_path)
        if plan_state is not None:
            _validate_regular_file(self.plan_path, plan_state)
            if not stat.S_ISREG(plan_state.st_mode):
                raise OperationStoreError(
                    "Operation directory exists without a trusted plan"
                )
            existing = strict_json_load(self.plan_path)
            if (
                not isinstance(existing, dict)
                or str(existing.get("plan_sha256") or "") != expected_hash
                or plan_sha256(existing) != expected_hash
                or canonical_json_bytes(existing) != canonical_json_bytes(plan)
            ):
                raise OperationStoreError(
                    "Operation ID is already bound to a different plan"
                )
            return
        write_new_json(self.plan_path, dict(plan))
        self._assert_safe()

    def read_plan(self) -> dict[str, Any]:
        self._assert_safe()
        _validate_regular_file(self.plan_path, _required_lstat(self.plan_path))
        value = strict_json_load(self.plan_path)
        if not isinstance(value, dict):
            raise OperationStoreError("Stored operation plan is not a JSON object")
        embedded_hash = str(value.get("plan_sha256") or "")
        if not embedded_hash or plan_sha256(value) != embedded_hash:
            raise OperationStoreError("Stored operation plan failed integrity validation")
        self._validate_plan_binding(value)
        self._assert_safe()
        return value

    def read_state(self) -> dict[str, Any] | None:
        self._assert_safe()
        state_stat = _optional_lstat(self.state_path)
        if state_stat is None:
            return None
        _validate_regular_file(self.state_path, state_stat)
        value = strict_json_load(self.state_path)
        if not isinstance(value, dict):
            raise OperationStoreError("Operation state is not a JSON object")
        self._validate_state_document(value)
        self._assert_safe()
        return value

    def write_state(self, state: Mapping[str, Any]) -> None:
        self._assert_safe()
        self._validate_state_document(state)
        existing = _optional_lstat(self.state_path)
        if existing is not None:
            _validate_regular_file(self.state_path, existing)
        atomic_write_json(self.state_path, dict(state))
        self._assert_safe()

    def append_event(
        self,
        event: Mapping[str, Any],
        *,
        state_updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._assert_safe()
        current_state = self.read_state() or {}
        plan = self.read_plan()
        events = self.read_events()
        sequence = len(events) + 1
        payload = {
            "sequence": sequence,
            "recorded_at": utc_now(),
            "operation_id": self.operation_id,
            "plan_sha256": str(plan["plan_sha256"]),
            **dict(event),
        }
        if (
            payload.get("operation_id") != self.operation_id
            or payload.get("plan_sha256") != plan["plan_sha256"]
        ):
            raise OperationStoreError("Event binding cannot be overridden")
        events_state = _optional_lstat(self.events_path)
        created = events_state is None
        if events_state is not None:
            _validate_regular_file(self.events_path, events_state)
        try:
            with self.events_path.open("ab") as handle:
                handle.write(canonical_json_bytes(payload) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            if created:
                _fsync_directory(self.directory)
        except OSError as exc:
            raise OperationStoreError(
                f"Could not append operation event: {exc}"
            ) from exc
        if state_updates is not None:
            current_state.update(dict(state_updates))
        current_state["next_event_sequence"] = sequence + 1
        current_state["updated_at"] = utc_now()
        self.write_state(current_state)
        self._assert_safe()
        return payload

    def read_events(self) -> list[dict[str, Any]]:
        self._assert_safe()
        event_stat = _optional_lstat(self.events_path)
        if event_stat is None:
            return []
        _validate_regular_file(self.events_path, event_stat)
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise OperationStoreError(
                "Could not trust the existing operation event journal"
            ) from exc
        plan = self.read_plan()
        events: list[dict[str, Any]] = []
        for sequence, line in enumerate(lines, start=1):
            try:
                value = _strict_json_text(line)
            except (OperationStoreError, json.JSONDecodeError) as exc:
                raise OperationStoreError(
                    "Could not trust the existing operation event journal"
                ) from exc
            if (
                not isinstance(value, dict)
                or value.get("sequence") != sequence
                or value.get("operation_id") != self.operation_id
                or value.get("plan_sha256") != plan["plan_sha256"]
                or not isinstance(value.get("event"), str)
            ):
                raise OperationStoreError(
                    "Operation event journal binding or sequence is invalid"
                )
            events.append(value)
        self._assert_safe()
        return events

    def write_result(self, result: Mapping[str, Any]) -> None:
        self._assert_safe()
        self._validate_result_document(result)
        existing = _optional_lstat(self.result_path)
        if existing is not None:
            _validate_regular_file(self.result_path, existing)
        atomic_write_json(self.result_path, dict(result))
        self._assert_safe()

    def read_result(self) -> dict[str, Any] | None:
        self._assert_safe()
        result_stat = _optional_lstat(self.result_path)
        if result_stat is None:
            return None
        _validate_regular_file(self.result_path, result_stat)
        value = strict_json_load(self.result_path)
        if not isinstance(value, dict):
            raise OperationStoreError("Operation result is not a JSON object")
        self._validate_result_document(value)
        self._assert_safe()
        return value

    def lock_exists(self) -> bool:
        self._assert_safe()
        return _optional_lstat(self.lock_path) is not None

    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        self._ensure_directory(create=False)
        existing_lock = _optional_lstat(self.lock_path)
        if existing_lock is not None:
            _reject_reparse(self.lock_path, existing_lock)
            raise OperationLockedError(
                "Operation apply lock already exists; status is unknown"
            )
        try:
            with self.lock_path.open("x", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"pid": os.getpid(), "created_at": utc_now()},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(self.directory)
        except FileExistsError as exc:
            raise OperationLockedError(
                "Operation apply lock already exists; status is unknown"
            ) from exc
        created_lock_state = _required_lstat(self.lock_path)
        _validate_regular_file(self.lock_path, created_lock_state)
        created_lock_identity = _file_identity(created_lock_state)
        try:
            self._assert_safe()
            yield
        finally:
            lock_state = _required_lstat(self.lock_path)
            _validate_regular_file(self.lock_path, lock_state)
            if _file_identity(lock_state) != created_lock_identity:
                raise OperationStoreError(
                    "Operation apply lock identity changed while it was held"
                )
            self.lock_path.unlink()
            _fsync_directory(self.directory)

    def _ensure_directory(self, *, create: bool) -> None:
        _validate_directory(self.codex_home, _required_lstat(self.codex_home))
        current = self.codex_home
        for part in (
            ".local-agent-record-janitor",
            "operations",
            self.operation_id,
        ):
            candidate = current / part
            candidate_stat = _optional_lstat(candidate)
            if candidate_stat is None:
                if not create:
                    raise OperationStoreError(
                        f"Operation directory does not exist: {candidate}"
                    )
                try:
                    candidate.mkdir()
                    _fsync_directory(current)
                except OSError as exc:
                    raise OperationStoreError(
                        f"Could not create operation directory {candidate}: {exc}"
                    ) from exc
                candidate_stat = _required_lstat(candidate)
            _validate_directory(candidate, candidate_stat)
            current = candidate
        self._assert_within_home()

    def _assert_safe(self) -> None:
        self._ensure_directory(create=False)
        for path in (
            self.plan_path,
            self.events_path,
            self.state_path,
            self.result_path,
            self.lock_path,
        ):
            value = _optional_lstat(path)
            if value is not None:
                _validate_regular_file(path, value)

    def _assert_within_home(self) -> None:
        try:
            home = self.codex_home.resolve(strict=True)
            directory = self.directory.resolve(strict=True)
            common = os.path.commonpath((os.fspath(home), os.fspath(directory)))
        except (OSError, ValueError) as exc:
            raise OperationStoreError(
                f"Could not prove operation directory boundary: {exc}"
            ) from exc
        if os.path.normcase(common) != os.path.normcase(os.fspath(home)):
            raise OperationStoreError("Operation directory escapes target CODEX_HOME")

    def _validate_plan_binding(self, plan: Mapping[str, Any]) -> None:
        target = plan.get("target")
        if (
            str(plan.get("operation_id") or "") != self.operation_id
            or not isinstance(target, Mapping)
        ):
            raise OperationStoreError("Operation plan binding is invalid")
        target_home = Path(str(target.get("codex_home") or ""))
        try:
            same_target = target_home.is_absolute() and os.path.samefile(
                target_home,
                self.codex_home,
            )
        except OSError:
            same_target = False
        if not same_target:
            raise OperationStoreError("Operation plan target does not match its store")

    def _validate_state_document(self, state: Mapping[str, Any]) -> None:
        plan = self.read_plan()
        goal = state.get("goal_status")
        if (
            state.get("schema_version") != _STATE_SCHEMA
            or state.get("operation_id") != self.operation_id
            or state.get("plan_sha256") != plan["plan_sha256"]
            or goal not in _GOAL_STATUSES
            or state.get("phase") not in _STATE_PHASES
            or type(state.get("goal_satisfied")) is not bool
            or state.get("goal_satisfied") is not (goal == "complete")
            or type(state.get("modified")) is not bool
            or type(state.get("mutation_started")) is not bool
        ):
            raise OperationStoreError("Operation state schema or binding is invalid")
        if self.events_path.exists():
            events = self.read_events()
            if any(event.get("event") == "mutation_started" for event in events) and not state[
                "mutation_started"
            ]:
                raise OperationStoreError(
                    "Operation state contradicts the durable mutation gate"
                )

    def _validate_result_document(self, result: Mapping[str, Any]) -> None:
        plan = self.read_plan()
        goal = result.get("goal_status")
        if (
            result.get("schema_version") != _RESULT_SCHEMA
            or result.get("document_type") != "operation_result"
            or result.get("command") != "agent"
            or result.get("mode") != "agent"
            or result.get("operation_id") != self.operation_id
            or result.get("plan_sha256") != plan["plan_sha256"]
            or goal not in _GOAL_STATUSES
            or type(result.get("goal_satisfied")) is not bool
            or result.get("goal_satisfied") is not (goal == "complete")
            or type(result.get("modified")) is not bool
            or type(result.get("mutation_started")) is not bool
            or not isinstance(result.get("blockers"), list)
            or not isinstance(result.get("counts"), Mapping)
        ):
            raise OperationStoreError("Operation result schema or binding is invalid")
        events = self.read_events()
        if not events or events[-1].get("goal_status") != goal:
            raise OperationStoreError(
                "Operation result is not backed by the durable event journal"
            )
        blockers = plan.get("authorization", {}).get("blockers", [])
        if goal == "complete":
            if blockers:
                raise OperationStoreError(
                    "Blocked plan cannot have a complete result"
                )
            verification = result.get("verification")
            final_scope = result.get("final_scope_verification")
            if not (
                isinstance(verification, Mapping)
                and verification.get("all_satisfied") is True
                and isinstance(final_scope, Mapping)
                and final_scope.get("all_satisfied") is True
            ):
                raise OperationStoreError(
                    "Complete result lacks successful exact and target verification"
                )


def _strict_json_text(raw: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise OperationStoreError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=reject_duplicates)


def _optional_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OperationStoreError(f"Could not inspect trusted path {path}: {exc}") from exc


def _required_lstat(path: Path) -> os.stat_result:
    value = _optional_lstat(path)
    if value is None:
        raise OperationStoreError(f"Required trusted path does not exist: {path}")
    return value


def _reject_reparse(path: Path, value: os.stat_result) -> None:
    attributes = getattr(value, "st_file_attributes", 0)
    if stat.S_ISLNK(value.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise OperationStoreError(
            f"Symlink, junction, or reparse-point path is not allowed: {path}"
        )


def _validate_directory(path: Path, value: os.stat_result) -> None:
    _reject_reparse(path, value)
    if not stat.S_ISDIR(value.st_mode):
        raise OperationStoreError(f"Trusted operation path is not a directory: {path}")


def _validate_regular_file(path: Path, value: os.stat_result) -> None:
    _reject_reparse(path, value)
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise OperationStoreError(
            f"Trusted operation path is not one unique regular file: {path}"
        )


def _file_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000))),
    )


__all__ = [
    "OperationLockedError",
    "OperationStore",
    "OperationStoreError",
    "atomic_write_json",
    "canonical_json_bytes",
    "plan_sha256",
    "strict_json_load",
    "utc_now",
    "validate_operation_id",
    "write_new_json",
]
