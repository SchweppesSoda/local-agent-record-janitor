"""Bounded coordinator tickets; absent tickets keep every legacy writer closed."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json

from .record_identity import canonical_path


@dataclass(frozen=True)
class _Ticket:
    encoded: bytes
    execution: bool
    plan_sha256: str | None
    frontends: bytes = b"[]"


_current = ContextVar("orca_action_ticket", default=None)


@contextmanager
def coordinator_scope(evidence, *, execution=False, plan_sha256=None, frontend_closures=()):
    if execution or plan_sha256 is not None:
        raise ValueError("Execution requires an approved document and its exact batch")
    evidence = tuple(evidence)
    values = _qualified(evidence) if evidence or not frontend_closures else []
    frontends = tuple(frontend_closures) or tuple(value["frozen"]["frontend"] for value in values if "frontend" in value["frozen"])
    from .orca_frontend import validate
    for item in frontends:
        validate(item)
    token = _current.set(_Ticket(json.dumps(values, sort_keys=True).encode(), False, None, json.dumps(frontends).encode()))
    try:
        yield
    finally:
        _current.reset(token)


def _qualified(evidence):
    from .orca_target_safety import EVIDENCE_SCHEMA, FRONTEND_EVIDENCE_SCHEMA
    from .orca_runtime import RUNTIME_ACCEPTED, invocation_policy, PINNED_BINARY_SHA256
    values = list(evidence)
    if not RUNTIME_ACCEPTED or not values or any(
        value.get("schema_version") not in {EVIDENCE_SCHEMA, FRONTEND_EVIDENCE_SCHEMA} or value.get("preflight_complete") is not True
        or value.get("native_delete") is not True or value.get("api_boundary") != "validated_fixed_runtime"
        or value.get("frozen", {}).get("invocation_policy") != invocation_policy()
        or value.get("frozen", {}).get("binary", {}).get("sha256") != PINNED_BINARY_SHA256 for value in values):
        raise ValueError("Orca coordinator ticket lacks validated target evidence")
    return values


@contextmanager
def execution_scope(document, actions, *, clients_closed):
    from .operation_store import plan_sha256
    from .orca_target_safety import validate_document_targets, evidence_for_actions, recheck_document_targets
    validate_document_targets(document)
    sha = document.get("plan_sha256")
    if clients_closed is not True or sha != plan_sha256(document):
        raise ValueError("Orca execution requires the explicit ack and immutable approved plan")
    errors = recheck_document_targets(document)
    if errors:
        raise ValueError("; ".join(errors))
    from .orca_cleanup import frontend_evidence
    frontends = tuple(frontend_evidence(document).values())
    pending = evidence_for_actions(document, actions)
    values = _qualified(pending) if pending or not frontends else []
    token = _current.set(_Ticket(json.dumps(values, sort_keys=True).encode(), True, sha, json.dumps(frontends).encode()))
    try:
        yield
    finally:
        _current.reset(token)


def permits(home, target_ids, *, execution=False):
    ticket = _current.get()
    if ticket is None or execution and not ticket.execution or home is None or not target_ids:
        return False
    wanted = frozenset(str(value) for value in target_ids)
    scopes = [frozenset(value["frozen"]["affected_thread_ids"]) for value in json.loads(ticket.encoded)
              if canonical_path(value["frozen"]["home"]) == canonical_path(home)]
    if execution:
        # A writer receives one complete action closure, or the exact union
        # of the current batch. A subset never changes a frozen root action.
        return wanted in scopes or bool(scopes) and wanted == frozenset().union(*scopes)
    return any(wanted <= ids for ids in scopes)


def current_evidence():
    ticket = _current.get()
    return tuple(json.loads(ticket.encoded)) if ticket is not None else ()


def permits_frontend(root, ids, *, execution=False):
    ticket = _current.get()
    return bool(ticket is not None and (not execution or ticket.execution) and root is not None and ids
        and any(canonical_path(item["root"]) == canonical_path(root) and set(ids) <= set(item["session_ids"])
                for item in json.loads(ticket.frontends)))


def permits_frontend_evidence(evidence):
    ticket = _current.get()
    return bool(ticket is not None and ticket.execution and evidence in json.loads(ticket.frontends))


def permits_reference(reference, *, execution=False):
    ticket = _current.get()
    if ticket is None or execution and not ticket.execution:
        return False
    from .orca_cleanup import covers_reference
    approved = [item for proof in json.loads(ticket.encoded) for item in proof["frozen"].get("references", ())]
    return reference.to_dict() in approved and any(covers_reference(item, reference) for item in json.loads(ticket.frontends))


def permits_error(error):
    ticket = _current.get()
    if ticket is None:
        return False
    from .orca_cleanup import covers_error
    return any(covers_error(item, error) for item in json.loads(ticket.frontends))
