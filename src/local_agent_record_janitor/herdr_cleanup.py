"""Qualified Herdr native children and exact frontend restoration closure."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
from pathlib import Path

from . import herdr_cleanup_files as files, herdr_cleanup_json as codec, herdr_lifecycle
from .herdr_bound_adapter import HerdrBoundAdapter, native_catalog
from .office_cleanup import digest
from .record_identity import canonical_path

KIND = "delete_herdr_frontend"
SCHEMA = "larj.herdr-cleanup.v1"
_ticket = ContextVar("herdr_cleanup_ticket", default=None)


def evidence_from_document(document):
    values = [a["impact"]["external_action_payload"]["herdr_evidence"] for a in document.get("actions", ()) if a.get("kind") == KIND]
    if len(values) > 1:
        codec.fail("multiple_profile_actions_unqualified")
    return values[0] if values else None


def action_id(evidence):
    return KIND + ":" + digest([canonical_path(evidence["root"]), evidence["files"]["bindings"]])[:32]


def frozen_pi_id(path):
    ticket = _ticket.get()
    if not ticket:
        return None
    values = {ref["native_id"] for ref in ticket[0]["references"] if ref["engine"] == "pi"
        and str(Path(ref["opaque_native_locator"])) == str(Path(path))}
    if len(values) > 1:
        codec.fail("pi_path_identity_ambiguous")
    return next(iter(values), None)


def pi_reference_matches_action(reference, path):
    native = reference["native_record"]
    try:
        Path(native["path"]).lstat()
    except FileNotFoundError:
        # Pi freezes its writer path with normcase. Once removed, Windows
        # cannot recover casing or 8.3 aliases. Both the raw reference and
        # its proven physical identity were frozen while present; accept
        # only those exact spellings in the writer's encoding.
        from .pi_sessions import _normalized_path
        return path in {_normalized_path(Path(native[key])) for key in ("path", "canonical_path")}
    return canonical_path(native["path"]) == canonical_path(path)


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
    if (client != "herdr" or not ticket or root is None or not ids
            or execution and (not ticket[1] or herdr_lifecycle.current_boundary(ticket[0]["root"]) is None)):
        return False
    evidence = ticket[0]
    if field == "frontend_session_delete":
        return canonical_path(root) == canonical_path(evidence["root"]) and set(ids) == {evidence["frontend_id"]}
    if field != "native_delete":
        return False
    return any(item["engine"] == engine and canonical_path(item["root"]) == canonical_path(root)
               and set(ids) <= set(item["ids"]) for item in evidence["native_targets"])


def permits_reference(reference, *, execution=False):
    ticket = _ticket.get()
    return bool(ticket and reference.client == "herdr" and reference.to_dict() in ticket[0]["references"]
        and (not execution or ticket[1] and herdr_lifecycle.current_boundary(ticket[0]["root"]) is not None))


def validate(evidence):
    if (not isinstance(evidence, dict) or evidence.get("schema_version") != SCHEMA
            or evidence.get("root") != evidence.get("files", {}).get("root")
            or not isinstance(evidence.get("native_targets"), list) or not evidence["native_targets"]):
        codec.fail("closure_invalid")
    from .herdr_bound_adapter import validate_manifest
    validate_manifest(evidence["binding_manifest"])
    if canonical_path(evidence["binding_manifest"]["profile_root"]) != canonical_path(evidence["root"]):
        codec.fail("closure_root_changed")
    files.validate(evidence["files"])
    manifest = evidence["binding_manifest"]["native_stores"]
    targets = evidence["native_targets"]
    keys = [(target["engine"], canonical_path(target["root"])) for target in targets]
    if len(set(keys)) != len(keys):
        codec.fail("native_target_duplicate")
    actions = evidence.get("native_actions")
    if not isinstance(actions, list) or len({a["action_id"] for a in actions}) != len(actions):
        codec.fail("native_action_evidence_invalid")
    for target in targets:
        if (target["binding"] not in manifest or target["engine"] != target["binding"]["engine"]
                or target["root"] != canonical_path(target["binding"]["root"]) or not target["ids"]
                or len(set(target["ids"])) != len(target["ids"])):
            codec.fail("native_target_binding_changed")
        covered = set()
        for action in actions:
            if action["action_id"] not in target["action_ids"]:
                continue
            kind = {"codex": "delete_conversation", "pi": "delete_pi_session", "claude": "delete_claude_session"}[target["engine"]]
            impact = action["impact"]
            payload = impact.get("external_action_payload") or {}
            actual_root = payload.get("session_root") if target["engine"] == "pi" else impact.get("external_storage_root")
            if actual_root is None and target["engine"] == "codex":
                actual_root = action.get("herdr_native_root")
            if action["kind"] != kind or actual_root is None or canonical_path(actual_root) != target["root"]:
                codec.fail("native_action_binding_changed")
            owners = [r for r in evidence["references"] if r["engine"] == target["engine"]
                and r["native_id"] == action["thread_id"] and r["native_record"]["store"]["canonical_path"] == target["root"]]
            if not owners or target["engine"] == "pi" and not any(
                    pi_reference_matches_action(r, payload["path"]) for r in owners):
                codec.fail("native_action_owner_unproven")
            covered.update((action["thread_id"], *action["affected_thread_ids"], *impact.get("descendant_thread_ids", ())))
            if target["engine"] == "pi" and canonical_path(payload["path"]) not in set(map(canonical_path, target["paths"])):
                codec.fail("pi_action_path_changed")
        refs = [r for r in evidence["references"] if r["engine"] == target["engine"]
                and r["native_record"]["store"]["canonical_path"] == target["root"]]
        covered.update(r["native_id"] for r in refs)
        if set(target["ids"]) != covered or set(target["action_ids"]) != {a["action_id"] for a in actions if a["action_id"] in target["action_ids"]}:
            codec.fail("native_target_scope_unproven")
    if {a["action_id"] for a in actions} != {aid for target in targets for aid in target["action_ids"]}:
        codec.fail("native_action_scope_unproven")
    owned = {(canonical_path(Path(evidence["root"]) / item["before"]["path"]),
              item["session"], f"workspaces/{wi}/tabs/{ti}/panes/{pid}/agent_session")
        for item in evidence["files"]["files"] for wi, ti, pid in item.get("owned", ())}
    observed = set()
    for ref in evidence["references"]:
        native = ref["native_record"]
        session = ref["frontend_id"].split("/", 1)[0]
        location = canonical_path(ref["source"]), session, ref["source_locator"]
        if (ref["client"] != "herdr" or ref["host"] != "local" or ref["path_namespace"] != "local"
                or ref["evidence_complete"] is not True or location not in owned
                or native["record_id"] != ref["native_id"] or native["store"]["backend"] != ref["engine"]
                or not any(t["engine"] == ref["engine"] and t["root"] == native["store"]["canonical_path"]
                    and ref["native_id"] in t["ids"] for t in targets)):
            codec.fail("reference_scope_changed")
        matches = [b for b in evidence["files"]["bindings"] if b["session"] == session and b["engine"] == ref["engine"]
                   and b["native_root"] == native["store"]["canonical_path"] and b["value"] == ref["opaque_native_locator"]]
        if len(matches) != 1 or not any(m["session"] == session and m["engine"] == ref["engine"]
                                      and canonical_path(m["root"]) == matches[0]["native_root"] for m in manifest):
            codec.fail("reference_root_unproven")
        if matches[0]["kind"] == "id" and matches[0]["value"] != ref["native_id"]:
            codec.fail("reference_id_changed")
        if matches[0]["kind"] == "path":
            path = Path(matches[0]["value"])
            if str(path) != str(Path(native["path"])):
                codec.fail("reference_path_changed")
            try:
                path.lstat()
            except FileNotFoundError:
                pass  # Only this exact frozen spelling may use its old ID.
            else:
                if canonical_path(path) != native["canonical_path"]:
                    codec.fail("reference_path_changed")
        observed.add(location)
    if observed != owned:
        codec.fail("owned_pane_reference_missing")


def bound_adapters(document, supplied=None):
    evidence = evidence_from_document(document)
    if evidence is None:
        return supplied
    from .client_contracts import describe_adapter
    adapter = HerdrBoundAdapter(evidence["binding_manifest"])
    adapter.frozen_references = evidence["references"]
    result = [adapter]
    for item in supplied or ():
        descriptor = describe_adapter(item)
        if descriptor.client == "herdr" and canonical_path(descriptor.profile_root) == canonical_path(adapter.profile_root):
            if not isinstance(item, HerdrBoundAdapter) or item.manifest != adapter.manifest:
                codec.fail("binding_manifest_changed")
            continue
        result.append(item)
    return tuple(result)


def native_remaining(target):
    engine, ids = target["engine"], set(target["ids"])
    if engine == "codex":
        from .orca_cleanup import _native_absent
        return not _native_absent(target["root"], ids, rollout_paths=target["paths"])
    catalog = native_catalog(target["binding"])
    if catalog.errors:
        codec.fail("native_catalog_incomplete")
    if engine == "pi":
        paths = set(map(canonical_path, target["paths"]))
        return any(canonical_path(record.path) in paths for record in catalog.records)
    if any(record.session_id in ids and record.transcript_paths for record in catalog.records):
        return True
    from . import claude_sessions as claude
    root = Path(target["root"])
    for sid in ids:
        if claude._discover_auxiliary_targets(root, sid):
            return True
    projects = root / "projects"
    try:
        projects.lstat()
    except FileNotFoundError:
        project_entries = ()
    else:
        claude._reject_reparse_components(projects, root)
        project_entries = claude._safe_iterdir(projects)
    for project in project_entries:
        for sid in ids:
            sidecar = project / sid
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            claude._reject_reparse_components(sidecar, root)
            return True
    return False


def residuals(document):
    evidence = evidence_from_document(document)
    validate(evidence)
    result = []
    for target in evidence["native_targets"]:
        if native_remaining(target):
            result.extend(target["action_ids"])
            if not target["action_ids"]:
                codec.fail("absent_native_target_reappeared")
    boundary = herdr_lifecycle.current_boundary(evidence["root"])
    if files.remaining(evidence["files"], reader=boundary.files.read if boundary else None):
        result.append(action_id(evidence))
    return list(dict.fromkeys(result))


def _references_for_targets(references, targets):
    result = []
    for ref in references:
        native = ref.native_record
        if native is not None and any(target["engine"] == native.store.backend
            and canonical_path(target["root"]) == native.store.canonical_path and native.record_id in target["ids"]
            and (native.store.backend != "pi" or native.canonical_path in set(map(canonical_path, target["paths"])))
            for target in targets):
            result.append(ref)
    return result


def build_context(coordinator, candidates, guards, selectors, engines, *, document=None):
    from .cleaner import ScanReport
    from .planning import CandidateAction, ActionKind, ActionImpact, TargetRef, StorageLocation, ScanStatus, RiskLevel, storage_id_for_path
    from .client_capability_guards import restrict_cleanup_context
    from .adapters import NativeIntegrityAdapter
    from .inventory import build_session_catalog
    from .manual_delete import build_manual_delete_plan
    selected = [a for a in candidates if isinstance(a, HerdrBoundAdapter)]
    if not selected:
        return None
    if len(selected) != 1 or not selectors:
        codec.fail("exact_single_profile_selection_required")
    adapter = selected[0]
    old = evidence_from_document(document or {})
    references = adapter.snapshot_references(refresh=True).references
    observations = adapter.observations()
    if old:
        validate(old)
        # Cold continuation retains the original action scopes even when a
        # native child has already removed its transcript and catalog row.
        desired = [(target["binding"], set(target["ids"]), set(target["paths"])) for target in old["native_targets"]]
    else:
        chosen, matched = [], set()
        for ref in references:
            aliases = {ref.frontend_id, "herdr:" + ref.frontend_id, ref.native_id, ref.binding_key, ref.opaque_native_locator}
            matches = set(selectors) & aliases
            if matches:
                if engines and ref.engine not in engines:
                    codec.fail("selector_engine_mismatch")
                matched.update(matches)
                chosen.append(ref)
        if matched != set(selectors) or not chosen:
            codec.fail("selector_not_found")
        # A raw ID may name the same logical record in many panes, but never
        # authorize different native roots merely because the ID was reused.
        for selector in selectors:
            identities = {(r.engine, r.native_record.store.canonical_path, r.native_record.record_id,
                           r.native_record.canonical_path) for r in chosen
                if selector in {r.frontend_id, "herdr:" + r.frontend_id, r.native_id, r.binding_key, r.opaque_native_locator}}
            if len(identities) > 1:
                codec.fail("selector_ambiguous")
        grouped = {}
        for ref in chosen:
            item = next(value for value in observations if adapter.profile_root / value["relative"] == ref.source
                        and value["frontend_id"] == ref.frontend_id)
            key = ref.engine, ref.native_record.store.canonical_path
            entry = grouped.setdefault(key, (item["binding"], set(), set()))
            if ref.engine == "pi" and canonical_path(entry[0]["agent_dir"]) != canonical_path(item["binding"]["agent_dir"]):
                codec.fail("pi_agent_binding_ambiguous")
            entry[1].add(ref.native_id)
            if ref.native_record.path:
                entry[2].add(str(ref.native_record.path))
        desired = list(grouped.values())
    base = coordinator.service.prepare_report(ScanReport(), active_adapters=(), platforms=("herdr",))
    native_contexts, actions, action_contexts, targets, manual_map = [], [], {}, [], {}
    manual_catalog = manual_plan = None
    for binding, ids, exact_paths in desired:
        engine, root = binding["engine"], Path(binding["root"])
        if engine == "codex":
            native = (NativeIntegrityAdapter(codex_home=root,
                codex_bin_hint=Path(binding["codex_binary"]) if binding.get("codex_binary") else None),)
            catalog = build_session_catalog(native)
            if catalog.errors:
                codec.fail("native_catalog_incomplete")
            if manual_catalog is not None:
                codec.fail("multiple_codex_roots_unqualified")
            plan = build_manual_delete_plan(catalog)
            result = coordinator._native_manual_context(native, catalog, plan)
            context, manual_catalog, manual_plan = result[0], catalog, plan
            chosen_actions = [a for a in context.plan.actions if a.target.thread_id in ids]
            chosen_actions = [a for a in chosen_actions if not any(a is not peer
                and a.target.thread_id in peer.impact.descendant_thread_ids for peer in chosen_actions)]
            affected = set(ids)
            for action in chosen_actions:
                affected.update(action.impact.affected_thread_ids)
                affected.update(action.impact.descendant_thread_ids)
                exact_paths.update(map(str, action.impact.rollout_paths))
            ids = affected
            manual_map.update({a.action_id: result[4][a.action_id] for a in chosen_actions if a.action_id in result[4]})
        else:
            catalog = native_catalog(binding)
            if catalog.errors:
                codec.fail("native_catalog_incomplete")
            context = coordinator.service.prepare_session_catalog(engine, catalog,
                catalog_builder=lambda b=binding: native_catalog(b), target_root=None)
            chosen_actions = [a for a in context.plan.actions if a.target.thread_id in ids and
                (engine != "pi" or canonical_path(a.impact.external_action_payload["path"]) in set(map(canonical_path, exact_paths)))]
            if engine == "pi" and chosen_actions:
                # Preserve the writer's exact frozen spelling after its file
                # becomes absent; Windows alias proofs are presence-dependent.
                exact_paths = {a.impact.external_action_payload["path"] for a in chosen_actions}
        for action in chosen_actions:
            action_contexts[str(action.action_id)] = context
        actions.extend(chosen_actions)
        native_contexts.append(context)
        target = {"engine": engine, "root": canonical_path(root), "ids": sorted(ids), "paths": sorted(exact_paths),
                  "binding": binding, "action_ids": sorted(str(a.action_id) for a in chosen_actions)}
        if old:
            previous = next(t for t in old["native_targets"] if t["engine"] == engine and t["root"] == canonical_path(root))
            if set(target["action_ids"]) - set(previous["action_ids"]) or not set(ids) <= set(previous["ids"]):
                codec.fail("native_scope_changed")
            target = previous
        if not chosen_actions and native_remaining(target):
            codec.fail("native_writer_missing")
        targets.append(target)
    if old:
        evidence = old
        files.remaining(evidence["files"], reader=adapter.read)
    else:
        approved_refs = _references_for_targets(references, targets)
        exact = []
        for item in observations:
            ref = next(r for r in references if r.source == adapter.profile_root / item["relative"] and r.frontend_id == item["frontend_id"])
            if ref in approved_refs:
                value = {key: item[key] for key in ("session", "engine", "kind", "value", "native_root")}
                if value not in exact:
                    exact.append(value)
        frozen = files.freeze(adapter.profile_root, exact)
        evidence = {"schema_version": SCHEMA, "root": str(adapter.profile_root), "binding_manifest": adapter.manifest,
            "files": frozen, "native_targets": targets, "references": [r.to_dict() for r in approved_refs],
            "selectors": list(selectors), "frontend_id": "herdr:" + digest(exact)[:24]}
        from .agent_operations import action_binding
        evidence["native_actions"] = [action_binding(action) for action in actions]
        for item in evidence["native_actions"]:
            if item["kind"] == "delete_conversation":
                item["herdr_native_root"] = next(target["root"] for target in targets if item["action_id"] in target["action_ids"])
        evidence["lifecycle"] = herdr_lifecycle.freeze(adapter.manifest, frozen)
    adapter.frozen_references = evidence["references"]
    base = coordinator._merge_cleanup_contexts(base, tuple(native_contexts)) if native_contexts else base
    sid = storage_id_for_path(adapter.profile_root)
    payload = {"herdr_evidence": evidence, "requires_action_ids": sorted(a for t in evidence["native_targets"] for a in t["action_ids"])}
    actions.append(CandidateAction(action_id(evidence), ActionKind(KIND), TargetRef(sid, evidence["frontend_id"]),
        RiskLevel.HIGH, True, None, ActionImpact(owner_client="herdr", owner_process_root=str(adapter.profile_root),
            external_engine=targets[0]["engine"], external_storage_root=str(adapter.profile_root), resource_path=str(adapter.profile_root),
            affected_thread_ids=(evidence["frontend_id"],), external_action_payload=payload, frontend_references_preserved=False),
        digest(evidence), resource_kind="herdr_session", requires_explicit_selection=True))
    storages = (*base.plan.storages, StorageLocation(sid, "Herdr frontend", adapter.profile_root, scan_status=ScanStatus.OK))
    plan = replace(base.plan, actions=tuple(actions), storages=storages,
                   plan_fingerprint="herdr-full:v1:" + digest([a.to_dict() for a in actions]))
    context = replace(base, plan=plan, actions=coordinator.service.typed_actions(plan))
    with planning_scope(evidence):
        context = restrict_cleanup_context(context, guards, coordinator.service.typed_actions)
        action_contexts = {key: restrict_cleanup_context(value, guards, coordinator.service.typed_actions) for key, value in action_contexts.items()}
    return context, context.active_adapters, manual_catalog, manual_plan, manual_map, action_contexts


@contextmanager
def execution_scope(document, actions, *, clients_closed, context, manual_actions, census=None):
    from .operation_store import plan_sha256
    evidence = evidence_from_document(document)
    validate(evidence)
    if clients_closed is not True or document.get("plan_sha256") != plan_sha256(document):
        codec.fail("approved_execution_required")
    approved = {a["action_id"]: a for a in document["actions"]}
    frontend = approved.get(action_id(evidence))
    if (frontend is None or set(frontend["impact"]["external_action_payload"].get("requires_action_ids", ()))
            != {a["action_id"] for a in evidence["native_actions"]}):
        codec.fail("native_dependencies_changed")
    for native in evidence["native_actions"]:
        value = {key: item for key, item in native.items() if key != "herdr_native_root"}
        from .operation_coordinator import OperationCoordinator
        if approved.get(native["action_id"], {}).get("binding") != OperationCoordinator._metadata(value):
            codec.fail("native_approved_action_changed")
    from .operation_coordinator import OperationCoordinator
    for action in actions:
        rendered = OperationCoordinator._metadata(action.to_dict())
        from .agent_operations import action_binding
        rendered["binding"] = OperationCoordinator._metadata(action_binding(action))
        rendered["classification"] = OperationCoordinator._classification(context, action,
            getattr(manual_actions.get(action.action_id), "root", None), "herdr")
        if approved.get(str(action.action_id)) != rendered:
            codec.fail("execution_action_unapproved")
    with herdr_lifecycle.hold(evidence, census=census):
        token = _ticket.set((json.loads(json.dumps(evidence)), True))
        try:
            yield
        finally:
            _ticket.reset(token)


@dataclass(frozen=True)
class HerdrCleanupResult:
    deleted_ids: tuple[str, ...]
    status: str = "deleted"

    def to_dict(self):
        return {"status": self.status, "deleted_ids": list(self.deleted_ids)}


def execute(evidence, *, phase_callback):
    ticket = _ticket.get()
    boundary = herdr_lifecycle.current_boundary(evidence["root"])
    if not ticket or not ticket[1] or ticket[0] != evidence or boundary is None:
        codec.fail("execution_ticket_required")
    if any(native_remaining(target) for target in evidence["native_targets"]):
        codec.fail("native_dependency_remains")
    boundary.apply(phase_callback=phase_callback)
    phase_callback("verified")
    return HerdrCleanupResult((evidence["frontend_id"],))
