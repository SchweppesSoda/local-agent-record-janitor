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


_current = ContextVar("orca_action_ticket", default=None)


@contextmanager
def coordinator_scope(evidence, *, execution=False, plan_sha256=None):
    if execution or plan_sha256 is not None:
        raise ValueError("Execution requires an approved document and its exact batch")
    values = _qualified(evidence)
    token = _current.set(_Ticket(json.dumps(values, sort_keys=True).encode(), False, None))
    try:
        yield
    finally:
        _current.reset(token)


def _qualified(evidence):
    from .orca_target_safety import EVIDENCE_SCHEMA
    from .orca_runtime import RUNTIME_ACCEPTED, invocation_policy, PINNED_BINARY_SHA256
    values = list(evidence)
    if not RUNTIME_ACCEPTED or not values or any(
        value.get("schema_version") != EVIDENCE_SCHEMA or value.get("preflight_complete") is not True
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
    values = _qualified(evidence_for_actions(document, actions))
    token = _current.set(_Ticket(json.dumps(values, sort_keys=True).encode(), True, sha))
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
