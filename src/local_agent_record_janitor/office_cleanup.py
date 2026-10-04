"""Office full record closures split into SDK and profile child operations."""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path

from . import office_database as database, office_files, office_leveldb, frozen_files
from .office_store import OfficeAdapter, PROFILES, default_profile_roots, read_database
from .record_identity import canonical_path, ProjectKey, resolve_project_selector, ProjectSelectionError
from .sqlite_utils import connect_readonly

SCHEMA = "larj.office-cleanup.v1"
KINDS = {"profile": "delete_office_frontend", "sdk": "delete_office_artifacts"}
_operation = ContextVar("office_operation", default=None)


@contextmanager
def operation_scope(document):
    """Make exact same-operation sibling receipts available to the writer."""
    token = _operation.set(document)
    try:
        yield
    finally:
        _operation.reset(token)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def action_id(client, profile, identifier, role):
    return KINDS[role] + ":" + digest([client, canonical_path(profile), identifier])[:32]


def _siblings(client, profile, sdk_ids, profile_roots=(), child_ids=()):
    sdk_folded = {i.casefold() for i in sdk_ids}
    cache_keys = {limit: {(office_files.sanitize(cid, limit) or "").casefold() for cid in child_ids}
                  for limit in (96, 120)}
    candidates = {canonical_path(path): path for path in (*profile_roots, *default_profile_roots(client),
        *(profile.parent / name for name in PROFILES[client])) if canonical_path(path) != canonical_path(profile)}
    result = []
    for _, root in sorted(candidates.items()):
        path = root / database.RELATIVE
        try:
            path.lstat()
        except FileNotFoundError:
            result.append({"root": str(root), "absent": True})
            continue
        database._path(root)
        with closing(connect_readonly(path)) as db:
            db.execute("BEGIN")
            database.qualify(db, client)
            for row in database._rows(db, "SELECT id,session_id,ext FROM sub_chats"):
                ext = database._json(row["ext"]) if row["ext"] else {}
                if not isinstance(ext, dict):
                    database._fail("sibling_metadata_unverified")
                if (row["session_id"] or "").casefold() in sdk_folded or str(ext.get("forkSourceSessionId") or "").casefold() in sdk_folded:
                    database._fail("sdk_session_shared_across_profiles")
                if client == "qwenwork" and any(
                    (office_files.sanitize(row["id"], limit) or "").casefold() in cache_keys[limit]
                    for limit in (96, 120)):
                    database._fail("session_cache_shared_across_profiles")
            result.append({"root": str(root), "identity": frozen_files._identity(path.stat()),
                           "database_sha256": database.logical_hash(db)})
    return result


def _validate_siblings(evidence):
    for item in evidence["siblings"]:
        root = Path(item["root"])
        path = root / database.RELATIVE
        if item.get("absent"):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            database._fail("sibling_profile_appeared")
        database._path(root)
        with closing(connect_readonly(path)) as db:
            db.execute("BEGIN")
            database.qualify(db, evidence["client"])
            observed_hash = database.logical_hash(db)
            after_verified = False
            if observed_hash != item["database_sha256"] and observed_hash == item.get("approved_after_sha256"):
                from .operation_coordinator import OperationCoordinator
                document = _operation.get()
                after_verified = document is not None and OperationCoordinator._workbuddy_terminal_verified(
                    document, str(root), KINDS["profile"])
            if frozen_files._identity(path.stat()) != item["identity"] or (observed_hash != item["database_sha256"] and not after_verified):
                database._fail("sibling_profile_changed")


def freeze(adapter, chat_ids, *, timestamp=None, roles=("sdk", "profile"), profile_roots=()):
    profile, sdk = adapter.profile_root, adapter.sdk_root
    db = database.freeze(adapter.client, profile, chat_ids, timestamp=timestamp)
    siblings = _siblings(adapter.client, profile, set(db["sdk_ids"]), profile_roots,
                         [row["id"] for row in db["sub_chats"]])
    observed = read_database(adapter.client, profile)
    children = db["sub_chats"]
    all_ids = [row["id"] for row in observed["sub_chats"]]
    common = {"schema_version": SCHEMA, "client": adapter.client, "profile_root": str(profile),
              "sdk_root": str(sdk), "database": db, "siblings": siblings}
    result = {}
    try:
        sdk.lstat()
        sdk_exists = True
    except FileNotFoundError:
        sdk_exists = False
    for role in roles:
        if role == "sdk":
            if not sdk_exists:
                continue
            files = office_files.freeze(sdk, client=adapter.client, role=role, children=children, all_child_ids=all_ids)
            result[role] = {**common, "role": role, "files": files}
        elif role == "profile":
            files = office_files.freeze(profile, client=adapter.client, role=role, children=children, all_child_ids=all_ids)
            ui = [office_leveldb.freeze(profile, path, chat_ids, sub_chat_ids=[row["id"] for row in children])
                  for path in ("Local Storage/leveldb", "Partitions/main/Local Storage/leveldb")]
            result[role] = {**common, "role": role, "files": files, "ui": ui, "sdk_absent": not sdk_exists}
        else:
            database._fail("cleanup_role_unverified")
    return result


def build_context(adapters, service, *, engines=(), refresh=False):
    from .cleaner import ScanReport
    from .planning import ActionImpact, ActionKind, CandidateAction, RiskLevel, ScanStatus, StorageLocation, TargetRef, storage_id_for_path
    selected = tuple(a for a in adapters if isinstance(a, OfficeAdapter))
    client = selected[0].client if selected else "qwenwork"
    profiles = sorted(({"profile_root": str(a.profile_root), "sdk_root": str(a.sdk_root)} for a in selected),
                      key=lambda value: (value["profile_root"], value["sdk_root"]))
    context = service.prepare_report(ScanReport(), active_adapters=selected, platforms=(client,))
    actions, storages, errors, seen = [], {}, [], {}
    for adapter in selected:
        key = canonical_path(adapter.profile_root)
        sdk_key = canonical_path(adapter.sdk_root)
        if key in seen:
            if seen[key] != sdk_key:
                errors.append("office_profile_sdk_binding_conflict")
            continue
        seen[key] = sdk_key
        snapshot = adapter.snapshot_references(refresh=refresh)
        errors.extend(error.message for error in snapshot.errors if error.blocks_inventory)
        observation = adapter.observation
        for role, root in (("profile", adapter.profile_root), ("sdk", adapter.sdk_root)):
            if role == "sdk" and not root.exists():
                continue
            sid = storage_id_for_path(root)
            storages[str(sid)] = StorageLocation(sid, client + " " + role, root,
                scan_status=ScanStatus.OK if observation else ScanStatus.FAILED)
            if observation is None or engines and client not in engines:
                continue
            for row in observation["chats"].values():
                cwd = row["worktree_path"] or observation["projects"][row["project_id"]]["path"]
                payload = {"cwd": cwd, "office_binding": {"client": client, "profile_root": str(adapter.profile_root),
                    "sdk_root": str(adapter.sdk_root), "role": role, "profiles": profiles}, "deleted_at": row["deleted_at"]}
                actions.append(CandidateAction(action_id(client, adapter.profile_root, row["id"], role),
                    ActionKind(KINDS[role]), TargetRef(sid, row["id"]), RiskLevel.HIGH, True, None,
                    ActionImpact(owner_client=client, owner_process_root=str(adapter.profile_root), external_engine=client,
                        external_storage_root=str(root), resource_path=str(root), affected_thread_ids=(row["id"],),
                        external_action_payload=payload, frontend_references_preserved=False), digest(payload),
                    resource_kind="office_session", requires_explicit_selection=row["deleted_at"] is None))
    plan = replace(context.plan, actions=tuple(actions), storages=tuple(storages.values()), errors=tuple(dict.fromkeys(errors)),
                   plan_fingerprint="office:v1:" + digest([a.to_dict() for a in actions]))
    return replace(context, plan=plan, actions=service.typed_actions(plan),
                   frontend_scan_coverage=tuple((canonical_path(a.profile_root / database.RELATIVE), "office_chats") for a in selected if a.observation))


def freeze_actions(actions, *, frozen=()):
    groups, result, closures = {}, {}, {}
    previous = {str(a["action_id"]): a for a in frozen}
    for action in actions:
        binding = action.impact.external_action_payload["office_binding"]
        groups.setdefault((binding["client"], binding["profile_root"], binding["sdk_root"]), []).append(action)
    for (client, profile, sdk), group in groups.items():
        roles = {a.impact.external_action_payload["office_binding"]["role"] for a in group}
        ids = sorted({a.target.thread_id for a in group})
        old = next((previous[str(a.action_id)] for a in group if str(a.action_id) in previous), None)
        timestamp = old["impact"]["external_action_payload"]["office_session_evidence"]["database"]["timestamp"] if old else None
        evidence = freeze(OfficeAdapter(client=client, profile_root=Path(profile), sdk_root=Path(sdk)), ids,
                          timestamp=timestamp, roles=tuple(sorted(roles)),
                          profile_roots=tuple(Path(p["profile_root"]) for action in group
                              for p in action.impact.external_action_payload["office_binding"]["profiles"]))
        if old:
            old_evidence = old["impact"]["external_action_payload"]["office_session_evidence"]
            _validate_siblings(old_evidence)
            old_roots = {canonical_path(Path(item["root"])) for item in old_evidence["siblings"]}
            for item in evidence.values():
                if {canonical_path(Path(s["root"])) for s in item["siblings"]} != old_roots:
                    database._fail("sibling_profile_scope_changed")
                # Preserve the immutable approval across a resumed operation;
                # current exact after states were just tied to child receipts.
                item["siblings"] = [dict(s) for s in old_evidence["siblings"]]
        closures[(client, canonical_path(profile))] = evidence
    # A prior sibling child may legitimately change its database within this
    # approval. Bind that exact after projection, never arbitrary future data;
    # execute accepts it only with the same operation's durable verified receipt.
    for evidence in closures.values():
        for item in evidence.values():
            for sibling in item["siblings"]:
                other = closures.get((item["client"], canonical_path(Path(sibling["root"]))), {}).get("profile")
                if other:
                    if sibling.get("database_sha256") != other["database"]["before_sha256"]:
                        database._fail("sibling_profile_changed")
                    sibling["approved_after_sha256"] = other["database"]["after_sha256"]
    for (client, profile, sdk), group in groups.items():
        evidence = closures[(client, canonical_path(profile))]
        ids = sorted({a.target.thread_id for a in group})
        for action in group:
            role = action.impact.external_action_payload["office_binding"]["role"]
            if role not in evidence:
                database._fail("sdk_root_disappeared")
            item = evidence[role]
            payload = {**action.impact.external_action_payload, "office_session_evidence": item}
            if role == "profile" and not item["sdk_absent"]:
                payload["requires_action_ids"] = [action_id(client, Path(profile), sid, "sdk") for sid in ids]
            impact = replace(action.impact, external_action_payload=payload)
            result[action.action_id] = replace(action, impact=impact, snapshot_fingerprint=digest(item))
    return tuple(result[a.action_id] for a in actions)


def select_candidates(context, scope, blocker):
    errors = [blocker(code, code, scope="office_inventory") for code in context.plan.errors]
    actions = tuple(context.plan.actions)
    wanted = set(scope.get("record_ids", ()))
    if wanted:
        selected = tuple(a for a in actions if a.target.thread_id in wanted)
        found = {a.target.thread_id for a in selected}
        errors.extend(blocker("record_not_found", "Office selection requires exact chat IDs", scope="selection") for _ in wanted - found)
    else:
        projects = {p.stable_id: p for a in actions if (cwd := a.impact.external_action_payload.get("cwd"))
                    for p in (ProjectKey.from_path(scope["client"], cwd),)}
        chosen = set(projects) if scope.get("all_projects") else set()
        for value in scope.get("projects", ()):
            try:
                chosen.add(resolve_project_selector(projects.values(), value, client=scope["client"]).stable_id)
            except ProjectSelectionError as exc:
                errors.append(blocker("project_not_found", str(exc), scope="selection"))
        selected = tuple(a for a in actions if not a.requires_explicit_selection
            and (cwd := a.impact.external_action_payload.get("cwd"))
            and ProjectKey.from_path(scope["client"], cwd).stable_id in chosen)
    if not selected and not errors:
        errors.append(blocker("empty_scope", "No eligible Office conversations matched this scope", scope="selection"))
    if errors:
        return (), errors
    try:
        return freeze_actions(selected), []
    except Exception as exc:
        code = getattr(exc, "kind", "office_closure_unverified")
        return (), [blocker(code, code, scope="office_closure")]


def _validate(evidence):
    if not isinstance(evidence, dict) or evidence.get("schema_version") != SCHEMA or evidence.get("role") not in KINDS:
        database._fail("cleanup_evidence_invalid")
    profile, sdk = Path(evidence["profile_root"]), Path(evidence["sdk_root"])
    db = evidence["database"]
    if db["client"] != evidence["client"] or Path(db["root"]) != profile:
        database._fail("cleanup_evidence_invalid")
    if Path(evidence["files"]["root"]) != (sdk if evidence["role"] == "sdk" else profile):
        database._fail("cleanup_evidence_invalid")
    return profile, sdk


@dataclass(frozen=True)
class OfficeCleanupResult:
    deleted_ids: tuple[str, ...]
    role: str
    status: str = "deleted"

    def to_dict(self):
        return {"status": self.status, "deleted_ids": list(self.deleted_ids), "role": self.role}


def execute(evidence, *, client_inspector=None, phase_callback):
    from .office_runtime import require_closed
    profile, sdk = _validate(evidence)
    check = lambda: require_closed(evidence["client"], (profile, sdk), client_inspector)
    check()
    _validate_siblings(evidence)
    fresh_db = database.freeze(evidence["client"], profile, evidence["database"]["chat_ids"],
                               timestamp=evidence["database"]["timestamp"])
    if fresh_db != evidence["database"] or office_files.fresh(evidence["files"]) != evidence["files"]:
        database._fail("cleanup_evidence_changed")
    if evidence["role"] == "profile":
        if evidence["sdk_absent"] and sdk.exists():
            database._fail("sdk_root_appeared")
        for item in evidence["ui"]:
            if office_leveldb.freeze(profile, item["relative"], item["chat_ids"], sub_chat_ids=item["sub_chat_ids"]) != item:
                database._fail("ui_evidence_changed")
    check()
    def checkpoint(phase):
        if phase == "mutation_started":
            check()
        phase_callback(phase)
    if evidence["role"] == "sdk":
        office_files.apply(evidence["files"], phase_callback=checkpoint)
    else:
        # Profile is released only after all its SDK child actions succeed.
        # Each store has an independent durable checkpoint; partial execution
        # stays unknown and is never silently repeated.
        for item in evidence["ui"]:
            office_leveldb.apply(item, phase_callback=checkpoint)
            if office_leveldb.remaining(item):
                database._fail("ui_targets_remain")
        office_files.apply(evidence["files"], phase_callback=checkpoint)
        database.apply(evidence["database"], phase_callback=checkpoint)
    check()
    if remaining(evidence):
        database._fail("cleanup_targets_remain")
    phase_callback("verified")
    return OfficeCleanupResult(tuple(evidence["database"]["chat_ids"]), evidence["role"])


def remaining(evidence, *, terminal_verified=False):
    _, sdk = _validate(evidence)
    count = office_files.remaining(evidence["files"])
    if evidence["role"] == "profile":
        if evidence["sdk_absent"] and sdk.exists():
            database._fail("sdk_root_appeared")
        count += database.remaining(evidence["database"], terminal_verified=terminal_verified)
        count += sum(office_leveldb.remaining(item, terminal_verified=terminal_verified) for item in evidence["ui"])
    return count


def bound_adapters(document, supplied=None):
    if document.get("scope", {}).get("client") not in {"qwenwork", "qoderwork"}:
        return supplied
    values = {}
    for action in document.get("actions", ()):
        binding = action.get("impact", {}).get("external_action_payload", {}).get("office_binding")
        if binding:
            for profile in binding["profiles"]:
                key = (binding["client"], profile["profile_root"])
                if key in values and values[key] != profile["sdk_root"]:
                    database._fail("frozen_profile_roots_mismatch")
                values[key] = profile["sdk_root"]
    if not values:
        return supplied  # Old blocked read-only plans retain their old result.
    if supplied is not None:
        actual = {(a.client, str(a.profile_root)): str(a.sdk_root) for a in supplied if isinstance(a, OfficeAdapter)}
        if actual != values:
            database._fail("frozen_profile_roots_mismatch")
    return tuple(OfficeAdapter(client=client, profile_root=Path(root), sdk_root=Path(sdk))
                 for (client, root), sdk in sorted(values.items()))
