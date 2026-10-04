"""Coordinator integration for an exact Orca frontend and native closure."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from . import orca_frontend as frontend, orca_journal_cleanup as journal
from .client_contracts import describe_adapter
from .record_identity import canonical_path
from .sqlite_utils import connect_readonly
from .office_cleanup import digest

KIND = "delete_orca_frontend"


def action_id(root, sid):
    return KIND + ":" + digest([canonical_path(root), sid])[:32]


def frontend_evidence(document):
    result = {}
    for action in document.get("actions", ()):
        if action.get("kind") != KIND:
            continue
        item = action.get("impact", {}).get("external_action_payload", {}).get("orca_frontend_evidence")
        if not isinstance(item, dict):
            journal.fail("frontend_action_evidence_missing")
        key = canonical_path(item["root"])
        if key in result and result[key] != item:
            journal.fail("frontend_action_evidence_conflict")
        result[key] = item
    return result


def covers_error(evidence, error):
    """Only old reader coverage gaps actually covered by this frozen writer."""
    root = Path(evidence["root"])
    if error.profile_root is None or canonical_path(error.profile_root) != canonical_path(root):
        return False
    paths = {canonical_path(root / item["evidence"]["before"]["path"]) for item in evidence["json_files"]}
    paths.update(canonical_path(root / (item["relative"] + suffix))
                 for item in evidence["profile_databases"] for suffix in ("", "-wal", "-shm", "-journal"))
    paths.update(canonical_path(root / item["path"]) for item in evidence["hook_observations"] if "sha256" in item)
    source = canonical_path(error.source)
    covered = (error.message == "legacy_or_profile_restore_source_not_read" and source in paths
        or error.message == "agent_hooks_namespaces_not_covered" and source == canonical_path(root / "agent-hooks")
        or error.message == "runtime_verification_not_covered" and source == canonical_path(root / "orca-runtime.json")
            and any(item["path"] == "orca-runtime.json" and "pid" in item for item in evidence["runtime_observations"]))
    return (covered and error.error_type == "OrcaInventoryIncomplete" and error.store is None
            and ("covered_errors" not in evidence or error.to_dict() in evidence["covered_errors"]))


def covers_reference(evidence, reference):
    native = reference.native_record
    from .client_contracts import ReferenceKind
    if (reference.client != "orca" or reference.evidence_complete is not True or native is None
            or reference.host != "local" or reference.path_namespace != "local" or native.store.backend != "codex"
            or reference.engine != "codex" or reference.kind not in {ReferenceKind.CURRENT, ReferenceKind.HISTORY}
            or canonical_path(reference.source) != canonical_path(Path(evidence["root"]) / journal.RELATIVE)
            or reference.frontend_id not in evidence["session_ids"]):
        return False
    return any(record["session_id"] == reference.frontend_id
        and canonical_path(record["home"]) == native.store.canonical_path and native.record_id in record["native_ids"]
        for record in evidence["records"])


def validate_action(document, action):
    from .operation_coordinator import OperationCoordinator
    from .agent_operations import action_binding
    from types import SimpleNamespace
    if isinstance(action, dict):
        value = OperationCoordinator._metadata(action)
    else:
        # CandidateAction does not serialize the coordinator's two derived
        # fields. Recompute both from this current action, then compare the
        # entire result to approval; neither binding nor classification is
        # copied from approval or exempted from the equality check.
        if action.observation_ids:
            journal.fail("frontend_action_observations_unexpected")
        value = OperationCoordinator._metadata(action.to_dict())
        value["binding"] = OperationCoordinator._metadata(action_binding(action))
        context = SimpleNamespace(plan=SimpleNamespace(observations=()))
        value["classification"] = OperationCoordinator._classification(context, action, client="orca")
    impact = value.get("impact", {})
    payload = impact.get("external_action_payload", {})
    closure = payload.get("orca_frontend_evidence")
    frontend.validate(closure)
    sid = value.get("target", {}).get("thread_id")
    root = closure["root"]
    if (value.get("kind") != KIND or value.get("action_id") != action_id(root, sid)
            or sid not in closure["session_ids"] or impact.get("owner_client") != "orca"
            or impact.get("owner_process_root") != root or impact.get("external_storage_root") != root):
        journal.fail("frontend_action_scope_changed")
    approved = [item for item in document.get("actions", ()) if item.get("action_id") == value.get("action_id")]
    storage = [item for item in document.get("storages", ())
               if item.get("storage_id") == value.get("target", {}).get("storage_id")]
    peers = [item for item in document.get("actions", ()) if item.get("kind") == KIND
             and item.get("impact", {}).get("owner_process_root") == root]
    if len(approved) != 1 or approved[0] != value:
        journal.fail("frontend_approved_action_changed:" + ",".join(sorted(key for key in set(value) | set(approved[0] if approved else {})
                     if not approved or value.get(key) != approved[0].get(key))))
    if (len(storage) != 1
            or canonical_path(storage[0]["path"]) != canonical_path(root)
            or sorted(item["target"]["thread_id"] for item in peers) != sorted(closure["session_ids"])
            or any(item["impact"]["external_action_payload"].get("orca_frontend_evidence") != closure for item in peers)):
        journal.fail("frontend_action_scope_changed")
    expected = []
    for evidence in document.get("target_safety_evidence", ()):
        if canonical_path(evidence["frozen"]["profile_root"]) == canonical_path(root):
            if evidence["frozen"].get("frontend") != closure:
                journal.fail("frontend_native_proof_mismatch")
            expected.append(evidence["action_id"])
    if sorted(payload.get("requires_action_ids", ())) != sorted(expected):
        journal.fail("frontend_dependency_scope_changed")
    return closure


def _selected(adapters, selectors):
    from .orca_metadata import parse_orca_record
    requests = {}
    found = set()
    matches_by_selector = {}
    for adapter in adapters:
        root = adapter.profile_root
        values = []
        try:
            root.lstat()
        except FileNotFoundError:
            continue
        try:
            (root / journal.RELATIVE).lstat()
        except FileNotFoundError:
            pass
        else:
            with closing(connect_readonly(root / journal.RELATIVE)) as db:
                journal.qualify(db)
                values.extend(journal.rows(db, "SELECT session_id,record_json FROM agent_session_records"))
        for relative, family in frontend.sources(root)[0]:
            if family == "legacy_records":
                _, raw = frontend.frozen_files.read_file(root, relative, content=True)
                value = journal.decode(raw)
                values.extend((sid, journal.encode(record)) for sid, record in value.get("records", {}).items())
        for sid, raw in values:
            record = parse_orca_record(sid, journal.decode(raw))
            identifiers = {sid, "orca:" + sid, *(handle.native_id for handle in record.handles)}
            matches = identifiers & set(selectors)
            if matches:
                for selector in matches:
                    matches_by_selector.setdefault(selector, set()).add((canonical_path(root), sid))
                found.update(matches)
                requests.setdefault(canonical_path(root), (adapter, set()))[1].add(sid)
    if found != set(selectors):
        return None
    # A raw frontend/native ID shared by several userData roots is ambiguous.
    for selector in selectors:
        if len(matches_by_selector.get(selector, ())) > 1:
            journal.fail("frontend_selector_ambiguous")
    return requests


def _native_absent(home, ids, *, rollout_paths=()):
    from .adapters import NativeIntegrityAdapter
    from .inventory import build_session_catalog
    from .cleaner import verify_finding_deleted
    from .models import Finding
    catalog = build_session_catalog((NativeIntegrityAdapter(codex_home=Path(home)),), plain_native_paths=True)
    if catalog.errors or canonical_path(home) not in {canonical_path(p) for p in catalog.scanned_native_homes}:
        journal.fail("native_dependency_unverified")
    if any(record.thread_id in ids and (record.artifact_present or record.legacy_indexed)
           for record in catalog.records):
        return False
    for sid in ids:
        finding = Finding(platform="native", platform_session_id=sid, thread_id=sid,
            reason="Orca exact frontend dependency", platform_db=Path(home) / "state_5.sqlite", codex_home=Path(home),
            details={"planned_impact_thread_ids": [sid], "planned_known_artifact_paths": list(rollout_paths)})
        result = verify_finding_deleted(finding)
        if result.status == "unknown":
            journal.fail("native_dependency_unverified")
        if not result.deleted:
            return False
    return True


def build_context(coordinator, adapters, guards, selectors, engines, *, document=None):
    from .adapters import NativeIntegrityAdapter
    from .inventory import build_session_catalog
    from .manual_delete import build_manual_delete_plan
    from .orca_target_safety import freeze_orca_target, FRONTEND_EVIDENCE_SCHEMA
    from .orca_authorization import coordinator_scope
    from .client_capability_guards import restrict_cleanup_context
    from .planning import CandidateAction, ActionKind, TargetRef, RiskLevel, ActionImpact, StorageLocation, ScanStatus, storage_id_for_path

    selected = tuple(a for a in adapters if describe_adapter(a).client == "orca")
    requests = _selected(selected, selectors)
    if requests is None or not requests:
        return None
    frontend_only = document is not None and document.get("actions") and all(a.get("kind") == KIND for a in document["actions"])
    if not frontend_only and any(adapter.codex_bin_hint is None for adapter, _ids in requests.values()):
        # Keep the established inventory-only plan (and its persisted guard
        # sources) when no exact registered native runtime was supplied.
        return None
    previous = frontend_evidence(document or {})
    closures, owners, desired = {}, {}, {}
    for key, (adapter, ids) in requests.items():
        old = previous.get(key)
        closure = frontend.freeze(adapter.profile_root, ids, timestamp=old["timestamp"] if old else None)
        if old is not None and closure != old:
            journal.fail("frontend_evidence_changed")
        closures[key] = closure
        for record in closure["records"]:
            home = canonical_path(record["home"])
            owners[home] = adapter
            desired.setdefault(home, set()).update(record["native_ids"])
    native = tuple(NativeIntegrityAdapter(codex_home=Path(home), codex_bin_hint=owners[home].codex_bin_hint) for home in desired)
    # The full references remain protection adapters; the native catalog only
    # determines complete native cascades, never grants frontend deletion.
    catalog = build_session_catalog(native)
    if catalog.errors:
        journal.fail("native_catalog_incomplete")
    # The initial catalog retains ownership and cascades even while the
    # public capability is read-only. Qualify that exact closure before asking
    # the ordinary native planner to construct any writable action.
    roots = [r for r in catalog.records if r.thread_id in desired.get(canonical_path(r.codex_home), ())
             and r.artifact_present]
    roots = [r for r in roots if not any(r is not other and r.codex_home == other.codex_home
             and r.thread_id in other.descendant_thread_ids for other in roots)]
    bootstrap = {}
    for record in roots:
        home = canonical_path(record.codex_home)
        affected = sorted({record.thread_id, *record.descendant_thread_ids})
        paths = sorted({str(rollout.path) for item in catalog.records
                        if canonical_path(item.codex_home) == home and item.thread_id in affected
                        for rollout in item.rollouts})
        proof = freeze_orca_target(owners[home], Path(home), record.thread_id, affected_ids=affected,
            rollout_paths=paths, binary=owners[home].codex_bin_hint,
            evidence_schema=FRONTEND_EVIDENCE_SCHEMA,
            frontend_evidence=closures[canonical_path(owners[home].profile_root)])
        if proof["preflight_complete"] is not True:
            journal.fail("native_target_unqualified:" + ",".join(proof["blocker_codes"]))
        bootstrap[(home, record.thread_id)] = proof
    with coordinator_scope(tuple(bootstrap.values()), frontend_closures=tuple(closures.values())):
        catalog = build_session_catalog((*native, *guards))
        manual = build_manual_delete_plan(catalog)
        result = coordinator._native_manual_context(native, catalog, manual)
    context = result[0]
    storage_paths = {str(s.storage_id): canonical_path(s.path) for s in context.plan.storages}
    chosen = [a for a in context.plan.actions if str(a.kind.value) == "delete_conversation"
              and a.target.thread_id in desired.get(storage_paths.get(str(a.target.storage_id)), ())]
    # The native planner includes descendants in an explicit root approval.
    chosen = [a for a in chosen if not any(a is not other and a.target.storage_id == other.target.storage_id
               and a.target.thread_id in other.impact.descendant_thread_ids for other in chosen)]
    proofs = []
    for action in chosen:
        home = storage_paths[str(action.target.storage_id)]
        adapter = owners[home]
        closure = closures[canonical_path(adapter.profile_root)]
        affected = sorted({action.target.thread_id, *action.impact.affected_thread_ids, *action.impact.descendant_thread_ids})
        proof = bootstrap[(home, action.target.thread_id)]
        if (affected != proof["frozen"]["affected_thread_ids"]
                or sorted(map(str, action.impact.rollout_paths)) != proof["frozen"]["rollout_paths"]):
            journal.fail("native_planner_closure_changed")
        proof["action_id"] = str(action.action_id)
        proofs.append(proof)
    covered = {(canonical_path(p["frozen"]["home"]), sid) for p in proofs for sid in p["frozen"]["affected_thread_ids"]}
    for home, ids in desired.items():
        missing = ids - {sid for owner, sid in covered if owner == home}
        if missing and not _native_absent(home, missing):
            journal.fail("native_dependency_missing")
    storages = list(context.plan.storages)
    actions = []
    for action in chosen:
        home = storage_paths[str(action.target.storage_id)]
        closure = closures[canonical_path(owners[home].profile_root)]
        aliases = [r["session_id"] for r in closure["records"] if canonical_path(r["home"]) == home
                   and set(r["native_ids"]) & {action.target.thread_id, *action.impact.affected_thread_ids}]
        payload = {**(action.impact.external_action_payload or {}), "orca_frontend_ids": sorted(set(aliases)),
                   "orca_target_evidence": next(p for p in proofs if p["action_id"] == action.action_id)}
        actions.append(replace(action, impact=replace(action.impact, external_action_payload=payload)))
    for key, closure in closures.items():
        root = Path(closure["root"])
        storage_id = storage_id_for_path(root)
        storages.append(StorageLocation(storage_id, "Orca frontend", root, scan_status=ScanStatus.OK))
        dependencies = [p["action_id"] for p in proofs if canonical_path(p["frozen"]["profile_root"]) == key]
        if document:
            dependencies = next((a["impact"]["external_action_payload"].get("requires_action_ids", [])
                for a in document.get("actions", ()) if a.get("kind") == KIND
                and canonical_path(a["impact"]["owner_process_root"]) == key), dependencies)
        for sid in closure["session_ids"]:
            payload = {"frontend_session_id": sid, "orca_frontend_evidence": closure, "requires_action_ids": dependencies}
            actions.append(CandidateAction(action_id(root, sid), ActionKind(KIND), TargetRef(storage_id, sid),
                RiskLevel.HIGH, True, None, ActionImpact(owner_client="orca", owner_process_root=str(root),
                    external_engine="codex", external_storage_root=str(root), resource_path=str(root),
                    affected_thread_ids=(sid,), external_action_payload=payload, frontend_references_preserved=False),
                digest(closure), resource_kind="orca_session", requires_explicit_selection=True))
    plan = replace(context.plan, actions=tuple(actions), storages=tuple(storages),
                   plan_fingerprint="orca-full:v1:" + digest([a.to_dict() for a in actions]))
    context = replace(context, plan=plan, actions=coordinator.service.typed_actions(plan))
    # Tickets are scoped to immutable complete native actions. Frontend-only
    # plans use their own fully frozen closure at the frontend writer boundary.
    with coordinator_scope(proofs, frontend_closures=tuple(closures.values())):
        context = restrict_cleanup_context(context, (*native, *guards), coordinator.service.typed_actions)
    return context, context.active_adapters, replace(catalog, active_adapters=context.active_adapters), \
        replace(manual, active_adapters=context.active_adapters), result[4], {}


@dataclass(frozen=True)
class OrcaCleanupResult:
    deleted_ids: tuple[str, ...]
    status: str = "deleted"

    def to_dict(self):
        return {"status": self.status, "deleted_ids": list(self.deleted_ids)}


def execute(evidence, *, client_inspector, phase_callback):
    from .orca_runtime_guard import require_closed
    from .orca_authorization import permits_frontend_evidence
    if not permits_frontend_evidence(evidence):
        journal.fail("frontend_execution_ticket_required")
    for record in evidence["records"]:
        if not _native_absent(record["home"], record["native_ids"]):
            journal.fail("native_dependency_remains")
    frontend.apply(evidence, phase_callback=phase_callback,
        require_closed=lambda: require_closed(evidence, client_inspector=client_inspector))
    return OrcaCleanupResult(tuple(evidence["session_ids"]))
