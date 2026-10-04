"""Exact Paseo native children, server records and bound desktop restoration closure."""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
from pathlib import Path

from . import paseo_cleanup_files as files, paseo_lifecycle, paseo_indexeddb, paseo_localstorage
from .paseo_bound_adapter import PaseoBoundAdapter, validate_manifest, native_catalog
from .herdr_cleanup import native_remaining, pi_reference_matches_action
from .office_cleanup import digest
from .record_identity import canonical_path

KIND = "delete_paseo_frontend"
SCHEMA = "larj.paseo-cleanup.v1"
_ticket = ContextVar("paseo_cleanup_ticket", default=None)


def evidence_from_document(document):
    values = [a["impact"]["external_action_payload"]["paseo_evidence"] for a in document.get("actions", ()) if a.get("kind") == KIND]
    if len(values) > 1:
        files.fail("multiple_profile_actions_unqualified")
    return values[0] if values else None


def action_id(evidence):
    return KIND + ":" + digest([canonical_path(evidence["root"]), evidence["selected_agent_ids"]])[:32]


def validate(evidence):
    if (not isinstance(evidence, dict) or evidence.get("schema_version") != SCHEMA
            or evidence.get("root") != evidence.get("files", {}).get("root")):
        files.fail("closure_invalid")
    manifest = validate_manifest(evidence["binding_manifest"])
    files.validate(evidence["files"])
    if (canonical_path(manifest["profile_root"]) != canonical_path(evidence["root"])
            or evidence["selected_agent_ids"] != evidence["files"]["agent_ids"]):
        files.fail("closure_root_changed")
    selected = set(evidence["selected_agent_ids"])
    if not selected <= {i["agent_id"] for i in manifest["native_stores"]}:
        files.fail("selected_agent_unbound")
    actions = evidence["native_actions"]
    if len({a["action_id"] for a in actions}) != len(actions):
        files.fail("native_action_duplicate")
    targets = evidence["native_targets"]
    if len({(t["engine"], t["root"]) for t in targets}) != len(targets):
        files.fail("native_target_duplicate")
    if {a["action_id"] for a in actions} != {aid for t in targets for aid in t["action_ids"]}:
        files.fail("native_action_scope_unproven")
    for target in targets:
        binding = target["binding"]
        if (binding not in manifest["native_stores"] or binding["agent_id"] not in selected
                or target["engine"] != binding["engine"] or target["root"] != canonical_path(binding["root"])):
            files.fail("native_target_binding_changed")
        covered = set()
        for action in actions:
            if action["action_id"] not in target["action_ids"]:
                continue
            kind = {"codex": "delete_conversation", "pi": "delete_pi_session", "claude": "delete_claude_session"}[target["engine"]]
            impact = action["impact"]
            payload = impact.get("external_action_payload") or {}
            root = payload.get("session_root") if target["engine"] == "pi" else impact.get("external_storage_root")
            root = root or action.get("paseo_native_root")
            if action["kind"] != kind or root is None or canonical_path(root) != target["root"]:
                files.fail("native_action_binding_changed")
            owners = [r for r in evidence["references"] if r["engine"] == target["engine"]
                and r["native_id"] == action["thread_id"] and r["native_record"]["store"]["canonical_path"] == target["root"]]
            if not owners or target["engine"] == "pi" and not any(
                    pi_reference_matches_action(r, payload["path"]) for r in owners):
                files.fail("native_action_owner_unproven")
            covered.update((action["thread_id"], *action["affected_thread_ids"], *impact.get("descendant_thread_ids", ())))
            if target["engine"] == "pi" and canonical_path(payload["path"]) not in set(map(canonical_path, target["paths"])):
                files.fail("pi_action_path_changed")
        covered.update(r["native_id"] for r in evidence["references"] if r["engine"] == target["engine"]
                       and r["native_record"]["store"]["canonical_path"] == target["root"])
        if covered != set(target["ids"]):
            files.fail("native_target_scope_unproven")
    owned = {canonical_path(Path(evidence["root"]) / item["before"]["path"]): item["id"]
             for item in evidence["files"]["files"] if item["kind"] == "agent" and item["id"] in selected}
    for ref in evidence["references"]:
        native = ref["native_record"]
        binding = next((b for b in manifest["native_stores"] if b["agent_id"] == ref["frontend_id"]), None)
        if (ref["client"] != "paseo" or ref["frontend_id"] not in selected or ref["host"] != "local"
                or ref["path_namespace"] != "local" or ref["evidence_complete"] is not True
                or owned.get(canonical_path(ref["source"])) != ref["frontend_id"] or binding is None
                or binding["engine"] != ref["engine"] or native["record_id"] != ref["native_id"]
                or native["store"]["backend"] != ref["engine"]
                or native["store"]["canonical_path"] != canonical_path(binding["root"])
                or ref["source_locator"] not in {"persistence/sessionId", "persistence/nativeHandle", "runtimeInfo/sessionId"}
                or not any(t["engine"] == ref["engine"] and t["root"] == native["store"]["canonical_path"]
                           and ref["native_id"] in t["ids"] for t in targets)):
            files.fail("reference_scope_changed")
        if ref["engine"] != "pi" or ref["source_locator"] != "persistence/nativeHandle":
            if ref["opaque_native_locator"] != ref["native_id"]:
                files.fail("reference_identity_changed")
        elif str(Path(ref["opaque_native_locator"])) != str(Path(native["path"])):
            files.fail("reference_path_changed")
    if len(evidence["desktop"]) != len(manifest["desktop_profiles"]):
        files.fail("desktop_scope_changed")
    for bound, desktop in zip(manifest["desktop_profiles"], evidence["desktop"]):
        for key, module in (("indexeddb", paseo_indexeddb), ("localstorage", paseo_localstorage)):
            item = desktop[key]
            module._validate(item)
            if (canonical_path(item["root"]) != canonical_path(bound["root"])
                    or item["agent_ids"] != evidence["selected_agent_ids"] or item["server_id"] != manifest["server_id"]):
                files.fail("desktop_scope_changed")


@contextmanager
def planning_scope(evidence):
    validate(evidence)
    token = _ticket.set((json.loads(json.dumps(evidence)), False))
    try:
        yield
    finally:
        _ticket.reset(token)


def permits(client, engine, root, ids, *, field="native_delete", execution=False):
    ticket = _ticket.get()
    if (client != "paseo" or not ticket or root is None or not ids
            or execution and (not ticket[1] or paseo_lifecycle.current_boundary(ticket[0]["root"]) is None)):
        return False
    evidence = ticket[0]
    if field == "frontend_session_delete":
        return canonical_path(root) == canonical_path(evidence["root"]) and set(ids) == {evidence["frontend_id"]}
    return field == "native_delete" and any(t["engine"] == engine and canonical_path(root) == t["root"]
        and set(ids) <= set(t["ids"]) for t in evidence["native_targets"])


def permits_reference(reference, *, execution=False):
    ticket = _ticket.get()
    return bool(ticket and reference.client == "paseo" and reference.to_dict() in ticket[0]["references"]
        and (not execution or ticket[1] and paseo_lifecycle.current_boundary(ticket[0]["root"]) is not None))


def bound_adapters(document, supplied=None):
    evidence = evidence_from_document(document)
    if evidence is None:
        return supplied
    from .client_contracts import describe_adapter
    adapter = PaseoBoundAdapter(evidence["binding_manifest"])
    adapter.frozen_references = evidence["references"]
    result = [adapter]
    for item in supplied or ():
        descriptor = describe_adapter(item)
        if descriptor.client == "paseo" and canonical_path(descriptor.profile_root) == canonical_path(adapter.profile_root):
            if not isinstance(item, PaseoBoundAdapter) or item.manifest != adapter.manifest:
                files.fail("binding_manifest_changed")
            continue
        result.append(item)
    return tuple(result)


def residuals(document):
    evidence = evidence_from_document(document)
    validate(evidence)
    result = []
    for target in evidence["native_targets"]:
        if native_remaining(target):
            if not target["action_ids"]:
                files.fail("absent_native_target_reappeared")
            result.extend(target["action_ids"])
    remaining = files.remaining(evidence["files"])
    for desktop in evidence["desktop"]:
        remaining += paseo_indexeddb.remaining(desktop["indexeddb"]) + paseo_localstorage.remaining(desktop["localstorage"])
    if remaining:
        result.append(action_id(evidence))
    return list(dict.fromkeys(result))


def check_source_references(adapter, evidence, references):
    files.remaining(evidence["files"])
    present = {canonical_path(row["source"]) for row in adapter.observations}
    fresh = [ref.to_dict() for ref in references if ref.frontend_id in evidence["selected_agent_ids"]]
    expected = [ref for ref in evidence["references"] if canonical_path(ref["source"]) in present]
    if sorted(map(digest, fresh)) != sorted(map(digest, expected)):
        files.fail("source_reference_scope_changed")


def build_context(coordinator, candidates, guards, selectors, engines, *, document=None):
    from .cleaner import ScanReport
    from .planning import CandidateAction, ActionKind, ActionImpact, TargetRef, StorageLocation, ScanStatus, RiskLevel, storage_id_for_path
    from .client_capability_guards import restrict_cleanup_context
    from .adapters import NativeIntegrityAdapter
    from .inventory import build_session_catalog
    from .manual_delete import build_manual_delete_plan
    selected = [a for a in candidates if isinstance(a, PaseoBoundAdapter)]
    if not selected:
        return None
    if len(selected) != 1 or not selectors:
        files.fail("exact_single_profile_selection_required")
    adapter = selected[0]
    old = evidence_from_document(document or {})
    references = adapter.snapshot_references(refresh=True).references
    if old:
        validate(old)
        # Bind the granted native scope back to the still-present frozen
        # server records before any native writer receives its exception.
        check_source_references(adapter, old, references)
        agent_ids = old["selected_agent_ids"]
        desired = [(t["binding"], set(t["ids"]), set(t["paths"])) for t in old["native_targets"]]
    else:
        available = {row["id"] for row in adapter.observations}
        agent_ids = {s.removeprefix("paseo:") for s in selectors}
        if not agent_ids or not agent_ids <= available:
            files.fail("selector_not_found")
        # The complete descendant set is made visible in the immutable plan.
        while True:
            expanded = agent_ids | {r["id"] for r in adapter.observations if r["parent_agent_id"] in agent_ids}
            if expanded == agent_ids:
                break
            agent_ids = expanded
        if any(engines and adapter.bindings[i]["engine"] not in engines for i in agent_ids):
            files.fail("selector_engine_mismatch")
        grouped = {}
        for ref in references:
            if ref.frontend_id not in agent_ids:
                continue
            binding = adapter.bindings[ref.frontend_id]
            key = ref.engine, ref.native_record.store.canonical_path
            entry = grouped.setdefault(key, (binding, set(), set()))
            if ref.engine == "pi" and canonical_path(binding["agent_dir"]) != canonical_path(entry[0]["agent_dir"]):
                files.fail("pi_agent_binding_ambiguous")
            entry[1].add(ref.native_id)
            if ref.native_record.path:
                entry[2].add(str(ref.native_record.path))
        desired = list(grouped.values())
        agent_ids = sorted(agent_ids)
    base = coordinator.service.prepare_report(ScanReport(), active_adapters=(), platforms=("paseo",))
    native_contexts, actions, action_contexts, targets, manual_map = [], [], {}, [], {}
    manual_catalog = manual_plan = None
    for binding, ids, exact_paths in desired:
        engine, root = binding["engine"], Path(binding["root"])
        if engine == "codex":
            native = (NativeIntegrityAdapter(codex_home=root,
                codex_bin_hint=Path(binding["codex_binary"]) if binding.get("codex_binary") else None),)
            catalog = build_session_catalog(native)
            if catalog.errors:
                files.fail("native_catalog_incomplete")
            if manual_catalog is not None:
                files.fail("multiple_codex_roots_unqualified")
            plan = build_manual_delete_plan(catalog)
            result = coordinator._native_manual_context(native, catalog, plan)
            context, manual_catalog, manual_plan = result[0], catalog, plan
            chosen = [a for a in context.plan.actions if a.target.thread_id in ids]
            chosen = [a for a in chosen if not any(a is not peer and a.target.thread_id in peer.impact.descendant_thread_ids for peer in chosen)]
            for action in chosen:
                ids.update(action.impact.affected_thread_ids)
                ids.update(action.impact.descendant_thread_ids)
                exact_paths.update(map(str, action.impact.rollout_paths))
            manual_map.update({a.action_id: result[4][a.action_id] for a in chosen if a.action_id in result[4]})
        else:
            catalog = native_catalog(binding)
            if catalog.errors:
                files.fail("native_catalog_incomplete")
            context = coordinator.service.prepare_session_catalog(engine, catalog,
                catalog_builder=lambda b=binding: native_catalog(b), target_root=None)
            chosen = [a for a in context.plan.actions if a.target.thread_id in ids and
                (engine != "pi" or canonical_path(a.impact.external_action_payload["path"]) in set(map(canonical_path, exact_paths)))]
            if engine == "pi" and chosen:
                exact_paths = {a.impact.external_action_payload["path"] for a in chosen}
        action_contexts.update({str(a.action_id): context for a in chosen})
        actions.extend(chosen)
        native_contexts.append(context)
        target = {"engine": engine, "root": canonical_path(root), "ids": sorted(ids), "paths": sorted(exact_paths),
            "binding": binding, "action_ids": sorted(str(a.action_id) for a in chosen)}
        if old:
            previous = next(t for t in old["native_targets"] if t["engine"] == engine and t["root"] == canonical_path(root))
            if set(target["action_ids"]) - set(previous["action_ids"]) or not set(ids) <= set(previous["ids"]):
                files.fail("native_scope_changed")
            target = previous
        if not chosen and native_remaining(target):
            files.fail("native_writer_missing")
        targets.append(target)
    if old:
        evidence = old
        files.remaining(evidence["files"])
    else:
        def matches(ref):
            return any(t["engine"] == ref.engine and t["root"] == ref.native_record.store.canonical_path
                and ref.native_id in t["ids"] and (ref.engine != "pi" or ref.native_record.canonical_path in set(map(canonical_path, t["paths"]))) for t in targets)
        if any(matches(ref) and ref.frontend_id not in agent_ids for ref in references):
            files.fail("unselected_agent_shares_native_record")
        approved_refs = [ref for ref in references if ref.frontend_id in agent_ids]
        evidence = {"schema_version": SCHEMA, "root": str(adapter.profile_root), "binding_manifest": adapter.manifest,
            "selected_agent_ids": agent_ids, "files": files.freeze(adapter.profile_root, agent_ids), "native_targets": targets,
            "references": [ref.to_dict() for ref in approved_refs], "selectors": list(selectors),
            "frontend_id": "paseo:" + digest(agent_ids)[:24], "desktop": []}
        for profile in adapter.manifest["desktop_profiles"]:
            root = Path(profile["root"])
            evidence["desktop"].append({"indexeddb": paseo_indexeddb.freeze(root, profile["electron_runtime"], adapter.manifest["server_id"], agent_ids),
                "localstorage": paseo_localstorage.freeze(root, adapter.manifest["server_id"], agent_ids)})
        from .agent_operations import action_binding
        evidence["native_actions"] = [action_binding(a) for a in actions]
        for item in evidence["native_actions"]:
            if item["kind"] == "delete_conversation":
                item["paseo_native_root"] = next(t["root"] for t in targets if item["action_id"] in t["action_ids"])
        evidence["lifecycle"] = paseo_lifecycle.freeze(adapter.manifest)
    adapter.frozen_references = evidence["references"]
    base = coordinator._merge_cleanup_contexts(base, tuple(native_contexts)) if native_contexts else base
    sid = storage_id_for_path(adapter.profile_root)
    payload = {"paseo_evidence": evidence, "requires_action_ids": sorted(a for t in evidence["native_targets"] for a in t["action_ids"])}
    actions.append(CandidateAction(action_id(evidence), ActionKind(KIND), TargetRef(sid, evidence["frontend_id"]),
        RiskLevel.HIGH, True, None, ActionImpact(owner_client="paseo", owner_process_root=str(adapter.profile_root),
            external_engine=adapter.bindings[agent_ids[0]]["engine"], external_storage_root=str(adapter.profile_root), resource_path=str(adapter.profile_root),
            affected_thread_ids=(evidence["frontend_id"],), external_action_payload=payload, frontend_references_preserved=False),
        digest(evidence), resource_kind="paseo_agent", requires_explicit_selection=True))
    storages = (*base.plan.storages, StorageLocation(sid, "Paseo frontend", adapter.profile_root, scan_status=ScanStatus.OK))
    plan = replace(base.plan, actions=tuple(actions), storages=storages,
        plan_fingerprint="paseo-full:v1:" + digest([a.to_dict() for a in actions]))
    context = replace(base, plan=plan, actions=coordinator.service.typed_actions(plan))
    with planning_scope(evidence):
        context = restrict_cleanup_context(context, guards, coordinator.service.typed_actions)
        action_contexts = {key: restrict_cleanup_context(value, guards, coordinator.service.typed_actions) for key, value in action_contexts.items()}
    return context, context.active_adapters, manual_catalog, manual_plan, manual_map, action_contexts


@contextmanager
def execution_scope(document, actions, *, clients_closed, context, manual_actions, census=None):
    from .operation_store import plan_sha256
    from .operation_coordinator import OperationCoordinator
    from .agent_operations import action_binding
    evidence = evidence_from_document(document)
    validate(evidence)
    if clients_closed is not True or document.get("plan_sha256") != plan_sha256(document):
        files.fail("approved_execution_required")
    approved = {a["action_id"]: a for a in document["actions"]}
    frontend = approved.get(action_id(evidence))
    if frontend is None or set(frontend["impact"]["external_action_payload"].get("requires_action_ids", ())) != {a["action_id"] for a in evidence["native_actions"]}:
        files.fail("native_dependencies_changed")
    for native in evidence["native_actions"]:
        value = {key: item for key, item in native.items() if key != "paseo_native_root"}
        if approved.get(native["action_id"], {}).get("binding") != OperationCoordinator._metadata(value):
            files.fail("native_approved_action_changed")
    for action in actions:
        rendered = OperationCoordinator._metadata(action.to_dict())
        rendered["binding"] = OperationCoordinator._metadata(action_binding(action))
        rendered["classification"] = OperationCoordinator._classification(context, action,
            getattr(manual_actions.get(action.action_id), "root", None), "paseo")
        if approved.get(str(action.action_id)) != rendered:
            files.fail("execution_action_unapproved")
    with paseo_lifecycle.hold(evidence, census=census):
        token = _ticket.set((json.loads(json.dumps(evidence)), True))
        try:
            yield
        finally:
            _ticket.reset(token)


@dataclass(frozen=True)
class PaseoCleanupResult:
    deleted_ids: tuple[str, ...]
    status: str = "deleted"

    def to_dict(self):
        return {"status": self.status, "deleted_ids": list(self.deleted_ids)}


def execute(evidence, *, phase_callback):
    ticket = _ticket.get()
    if not ticket or not ticket[1] or ticket[0] != evidence or paseo_lifecycle.current_boundary(evidence["root"]) is None:
        files.fail("execution_ticket_required")
    if any(native_remaining(t) for t in evidence["native_targets"]):
        files.fail("native_dependency_remains")
    with ExitStack() as stack:
        prepared = []
        for desktop in evidence["desktop"]:
            for key, module in (("indexeddb", paseo_indexeddb), ("localstorage", paseo_localstorage)):
                family = stack.enter_context(module.prepared(desktop[key]))
                prepared.append((module, desktop[key], family))
        for module, item, family in prepared:
            module.install(item, family, phase_callback=phase_callback)
        files.apply(evidence["files"], phase_callback=phase_callback)
    if files.remaining(evidence["files"]) or any(paseo_indexeddb.remaining(d["indexeddb"]) + paseo_localstorage.remaining(d["localstorage"]) for d in evidence["desktop"]):
        files.fail("frontend_after_unverified")
    phase_callback("verified")
    return PaseoCleanupResult(tuple(evidence["selected_agent_ids"]))
