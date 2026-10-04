"""Paseo draft/layout rewrites using pinned LevelDB on private copies only."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import tempfile

from . import office_leveldb as level
from .paseo_indexeddb import _copy, fail, install_family, require_no_sealed
from .paseo_cleanup_files import ID
from . import frozen_files

SCHEMA = "larj.paseo-local-storage.v1"
RELATIVE = "Local Storage/leveldb"


def _call(path, evidence, *, apply=False):
    request = {"schema_version": level.PROTOCOL, "operation": "apply" if apply else "inspect-copy",
        "path": str(path), "chat_ids": evidence["agent_ids"], "sub_chat_ids": [],
        "origins": ["paseo://app"], "paseo_server_id": evidence["server_id"]}
    if apply:
        observed = evidence["observation"]
        edits = [{"key": i["key_sha256"], "after": i["after_sha256"], "meta": i["after_metadata_sha256"]}
                 for i in observed["edits"]]
        request.update(authorized_before_sha256=observed["fingerprint"],
                       authorized_after_sha256=hashlib.sha256(level._json(edits)).hexdigest())
    return level._call(request)


def _validate(evidence):
    ids = evidence.get("agent_ids")
    if (evidence.get("schema_version") != SCHEMA or evidence.get("relative") != RELATIVE
            or not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
            or ids != sorted(set(ids)) or not isinstance(evidence.get("server_id"), str) or not ID.fullmatch(evidence["server_id"])):
        fail("local_storage_scope_invalid")
    frozen_files.checked_path(Path(evidence["root"]), RELATIVE)
    return Path(evidence["root"])


def freeze(root, server_id, ids):
    root = Path(root)
    require_no_sealed(root)
    if (not isinstance(ids, (list, tuple)) or not ids or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
            or not isinstance(server_id, str) or not ID.fullmatch(server_id)):
        fail("local_storage_selection_invalid")
    evidence = {"schema_version": SCHEMA, "root": str(root), "relative": RELATIVE,
        "agent_ids": sorted(set(ids)), "server_id": server_id, "files": level._snapshot(root, RELATIVE),
        "runtime": None, "observation": None}
    if evidence["files"]:
        evidence["runtime"] = level.runtime_fingerprint()
        with tempfile.TemporaryDirectory(prefix="larj-paseo-local-") as raw:
            stage = Path(raw).resolve(strict=True) / "stage"
            _copy(root, evidence["files"], stage, relative=RELATIVE)
            evidence["observation"] = _call(stage / RELATIVE, evidence)
        if level.runtime_fingerprint() != evidence["runtime"]:
            fail("local_storage_runtime_changed")
    return evidence


@contextmanager
def prepared(evidence):
    root = _validate(evidence)
    require_no_sealed(root)
    if level._snapshot(root, RELATIVE) != evidence["files"]:
        if remaining(evidence) == 0:
            yield None
            return
        fail("local_storage_source_changed")
    if evidence["files"] is None:
        yield None
        return
    if level.runtime_fingerprint() != evidence["runtime"]:
        fail("local_storage_runtime_changed")
    with tempfile.TemporaryDirectory(prefix="larj-paseo-local-apply-") as raw:
        temporary = Path(raw).resolve(strict=True)
        stage = temporary / "stage"
        _copy(root, evidence["files"], stage, relative=RELATIVE)
        observed = _call(stage / RELATIVE, evidence)
        if observed != evidence["observation"]:
            fail("local_storage_frozen_state_changed")
        changes = observed["count"]["removed_pairs"]
        if changes:
            _call(stage / RELATIVE, evidence, apply=True)
        snapshot = level._snapshot(stage, RELATIVE)
        cold = temporary / "cold"
        _copy(stage, snapshot, cold, relative=RELATIVE)
        result = _call(cold / RELATIVE, evidence)
        if (result["logical_before_sha256"] != observed["logical_after_sha256"]
                or result["count"]["removed_pairs"]):
            fail("local_storage_cold_after_unverified")
        if level.runtime_fingerprint() != evidence["runtime"]:
            fail("local_storage_runtime_changed")
        yield (stage, snapshot) if changes else None


def install(evidence, family, *, phase_callback):
    root = _validate(evidence)
    if family is None:
        if remaining(evidence):
            fail("local_storage_after_unverified")
        return
    install_family(root, RELATIVE, evidence["files"], family, phase_callback=phase_callback,
                   verify_after=lambda: _verify_installed(evidence))


def _verify_installed(evidence):
    root = Path(evidence["root"])
    with tempfile.TemporaryDirectory(prefix="larj-paseo-local-installed-") as raw:
        stage = Path(raw).resolve(strict=True) / "stage"
        _copy(root, level._snapshot(root, RELATIVE), stage, relative=RELATIVE)
        value = _call(stage / RELATIVE, evidence)
    if (value["logical_before_sha256"] != evidence["observation"]["logical_after_sha256"]
            or value["count"]["removed_pairs"]):
        fail("local_storage_installed_after_unverified")


def remaining(evidence):
    root = _validate(evidence)
    require_no_sealed(root)
    current = level._snapshot(root, RELATIVE)
    if current is None:
        if evidence["files"] is not None:
            fail("local_storage_shared_family_missing")
        return 0
    if evidence["files"] is None:
        fail("local_storage_unexpected_family")
    if level.runtime_fingerprint() != evidence["runtime"]:
        fail("local_storage_runtime_changed")
    with tempfile.TemporaryDirectory(prefix="larj-paseo-local-verify-") as raw:
        stage = Path(raw).resolve(strict=True) / "stage"
        _copy(root, current, stage, relative=RELATIVE)
        value = _call(stage / RELATIVE, evidence)
    expected = evidence["observation"]
    if value["logical_before_sha256"] == expected["logical_after_sha256"] and not value["count"]["removed_pairs"]:
        return 0
    if current == evidence["files"] and value == expected:
        return value["count"]["removed_pairs"]
    fail("local_storage_state_unknown")
