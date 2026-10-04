"""Read-only recovery of a self-contained native child journal.

The child keeps its original identity and hash. No parent authorization is
reconstructed, no record writer is called, and detailed evidence is retained.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Mapping

from .mutation_guard import mutation_roots, scopes_for_frozen_plan
from .operation_store import OperationStore, OperationStoreError, strict_json_load


def query_child_operation(coordinator: Any, path: Path | None, *,
                          operation_id: str | None, codex_home: Path | None,
                          verify: bool, progress_callback: Any = None) -> dict | None:
    if path is None:
        return None
    raw = strict_json_load(path)
    if not isinstance(raw, dict) or not str(raw.get("schema_version", "")).startswith("larj.child-operation-plan."):
        return None
    command = "verify" if verify else "status"
    result = {
        "schema_version": "larj.operation-result.v1", "document_type": "operation_result",
        "command": "operation", "subcommand": command,
        "operation_id": operation_id or raw.get("operation_id"),
        "goal_status": "unknown", "goal_satisfied": False,
        "modified": False, "mutation_started": False, "counts": {}, "blockers": [],
    }
    try:
        home = Path(raw["target"]["codex_home"])
        if not home.is_absolute():
            raise OperationStoreError("Child journal requires an absolute native home")
        store = OperationStore(home, str(result["operation_id"]))
        if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(store.plan_path)):
            raise OperationStoreError("Child recovery requires the original journal plan location")
        if codex_home is not None and not os.path.samefile(home, codex_home):
            raise OperationStoreError("Explicit native home differs from child journal")
        # Use the existing journal trust checks before acquiring a root lock.
        plan = store.read_plan()
        result.update(plan_sha256=plan["plan_sha256"], parent_operation_id=plan.get("parent_operation_id"),
                      plan_path=str(store.plan_path), recovery_scope="child_only")
        with mutation_roots((home,)):
            plan = store.read_plan()
            state, _ = _read_checkpoint(store, plan)
            result.update(modified=state["modified"], mutation_started=state["mutation_started"])
            actions = _native_actions(plan)
            result["counts"] = {"action_count": len(actions), "batch_count": 1}
            if store.lock_exists():
                raise OperationStoreError("Existing apply.lock keeps the child outcome unknown")
            if not verify:
                result.update(goal_status=state["goal_status"], goal_satisfied=state["goal_satisfied"])
                if state["goal_status"] == "unknown":
                    result["blockers"] = [{"blocker_code": "recovery_required", "scope": "child",
                                           "message": "run operation verify for this child journal", "retryable": True}]
                return result
            # Acquire the existing per-operation gate too; never remove an old lock.
            with store.mutation_lock():
                state, _ = _read_checkpoint(store, plan)
                coordinator._emit_progress(progress_callback, "inventory", "started",
                                           operation_id=store.operation_id)
                residuals = _native_residuals(coordinator, plan, home, actions)
                coordinator._emit_progress(progress_callback, "inventory", "completed",
                    operation_id=store.operation_id,
                    counts={"action_count": len(actions), "residual_count": len(residuals)})
                goal = "completed_with_residuals" if residuals else "complete"
                verified = [a["action_id"] for a in actions if a["action_id"] not in residuals]
                blockers = ([{"blocker_code": "residual_records", "scope": "child",
                              "message": "approved child records remain", "retryable": True}]
                            if residuals else [])
                # A fresh observation resolves ambiguity even when an old writer
                # failed before its mutation marker. Do not claim it changed data.
                store.append_event({"event": "verification_finished", "goal_status": goal,
                    "verified_action_ids": verified, "residual_action_ids": residuals},
                    state_updates={"phase": "finished", "goal_status": goal,
                        "goal_satisfied": goal == "complete", "current_action_state": "verified"})
                counts = {"action_count": len(actions), "batch_count": 1, "residual_count": len(residuals)}
                durable = {
                    "schema_version": "larj.agent-result.v1", "document_type": "operation_result",
                    "command": "agent", "mode": "agent", "subcommand": "verify", "phase": "finished",
                    "operation_id": store.operation_id, "plan_sha256": plan["plan_sha256"],
                    "goal_status": goal, "goal_satisfied": goal == "complete",
                    "modified": state["modified"], "mutation_started": state["mutation_started"],
                    "blockers": blockers, "counts": counts,
                    "action_ids": [a["action_id"] for a in actions], "verified_action_ids": verified,
                    "residual_action_ids": residuals,
                    "verification": {"all_satisfied": not residuals, "verified_action_ids": verified},
                    "final_scope_verification": {"all_satisfied": not residuals, "scan_complete": True},
                }
                store.write_result(durable)
                # Keep the original child plan/events: its parent may be lost.
                result.update(goal_status=goal, goal_satisfied=not residuals, counts=counts,
                              blockers=blockers, residual_action_ids=residuals)
        return result
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        result.update(goal_status="unknown", goal_satisfied=False,
                      blockers=[{"blocker_code": "child_recovery_unproven", "scope": "child",
                                 "message": str(exc), "retryable": True}])
        return result


def _read_checkpoint(store: OperationStore, plan: Mapping[str, Any]) -> tuple[dict, list]:
    state = store.read_state()
    events = store.read_events()
    if (state is None or not events or events[0].get("event") != "plan_accepted"
            or state.get("next_event_sequence") != len(events) + 1
            or any(e.get("parent_operation_id", plan.get("parent_operation_id")) != plan.get("parent_operation_id")
                   for e in events)):
        raise OperationStoreError("Child checkpoint or event sequence is incomplete")
    if store.receipt_path.exists():
        raise OperationStoreError("Child has a receipt alongside its journal; recover through the parent plan")
    result = store.read_result()
    if result is not None and result.get("goal_status") != state.get("goal_status"):
        raise OperationStoreError("Child result contradicts its checkpoint")
    if any(e.get("event") in {"mutation_started", "orca_runtime_startup"} for e in events) and not state["mutation_started"]:
        raise OperationStoreError("Child checkpoint contradicts its mutation history")
    if state["goal_status"] in {"complete", "completed_with_residuals"} and (
            state["phase"] != "finished" or events[-1].get("goal_status") != state["goal_status"]
            or events[-1].get("event") not in {"batch_finished", "verification_finished"}):
        raise OperationStoreError("Child terminal state is not backed by its event journal")
    return state, events


def _native_actions(plan: Mapping[str, Any]) -> list[dict]:
    if (plan.get("schema_version") != "larj.child-operation-plan.v1"
            or plan.get("mutation_family") != "delete_conversation" or "startup_boundary" in plan):
        raise OperationStoreError("Standalone recovery supports only v1 native conversation children; use the parent plan")
    scopes_for_frozen_plan(plan)
    actions = plan.get("actions")
    if not isinstance(actions, list) or not actions or not plan.get("parent_operation_id"):
        raise OperationStoreError("Child has no frozen native action scope")
    seen = set()
    home = Path(plan["target"]["codex_home"])
    storage_id = plan["target"]["storage_id"]
    for action in actions:
        target, impact = action.get("target", {}), action.get("impact", {})
        if not isinstance(target, Mapping) or not isinstance(impact, Mapping):
            raise OperationStoreError("Child target or impact is malformed")
        action_id = action.get("action_id")
        if (action.get("kind") != "delete_conversation" or not isinstance(action_id, str)
                or not action_id or action_id in seen or target.get("storage_id") != storage_id
                or not isinstance(target.get("thread_id"), str) or not target["thread_id"]):
            raise OperationStoreError("Child action identity or mutation family is unproven")
        seen.add(action_id)
        payload = impact.get("external_action_payload", {})
        if not isinstance(payload, Mapping):
            raise OperationStoreError("Child external evidence is malformed")
        if (impact.get("external_engine", "codex") != "codex"
                or any(value for key, value in impact.items() if key.startswith("frontend_") and key != "frontend_references_preserved")
                or any(payload.get(key) for key in ("frontend_references", "cindy_references", "frontend_session_id"))
                or impact.get("external_artifact_paths")):
            raise OperationStoreError("Child contains non-native evidence; use the parent plan")
        if impact.get("external_storage_root") and not os.path.samefile(home, impact["external_storage_root"]):
            raise OperationStoreError("Child action points at another physical store")
        for key in ("affected_thread_ids", "descendant_thread_ids", "indexed_thread_ids", "rollout_paths"):
            values = impact.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise OperationStoreError("Child frozen footprint is malformed")
        if target["thread_id"] not in impact.get("affected_thread_ids", []):
            raise OperationStoreError("Child target is absent from its frozen closure")
        for raw_path in impact.get("rollout_paths", []):
            _artifact_present(home, raw_path)
    return actions


def _artifact_present(home: Path, raw_path: str) -> bool:
    artifact = Path(os.path.normpath(raw_path))
    if not artifact.is_absolute() or not artifact.is_relative_to(home):
        raise OperationStoreError("Child artifact escapes its native store")
    current = home
    parts = artifact.relative_to(home).parts
    if not parts:
        raise OperationStoreError("Child rollout path names the store directory")
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OperationStoreError("Child artifact has a linked path component")
        expected = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
        if not expected(info.st_mode):
            raise OperationStoreError("Child artifact path changed filesystem kind")
    return True


def _native_residuals(coordinator: Any, plan: Mapping[str, Any], home: Path, actions: list[dict]) -> list[str]:
    from .adapters import NativeIntegrityAdapter

    context = coordinator._build_context("native", (NativeIntegrityAdapter(codex_home=home),), codex_home=home)[0]
    if not context.plan.scan_complete:
        raise OperationStoreError("Child native inventory is incomplete")
    storage_id = plan["target"]["storage_id"]
    # This is a verification view of the child, not a reconstructed parent
    # plan. It is never hashed, persisted, or accepted by an apply path.
    view = {"actions": actions, "storages": [{"storage_id": storage_id, "path": str(home)}],
            "child_batches": [{"storage_id": storage_id, "mutation_family": "delete_conversation",
                               "action_ids": [a["action_id"] for a in actions]}]}
    residuals = set(coordinator._residual_action_ids(view, context))
    for action in actions:
        for path in action["impact"].get("rollout_paths", []):
            if _artifact_present(home, path):
                residuals.add(action["action_id"])
    return [a["action_id"] for a in actions if a["action_id"] in residuals]
