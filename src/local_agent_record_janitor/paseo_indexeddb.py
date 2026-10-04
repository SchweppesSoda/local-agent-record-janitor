"""Qualified Chromium IndexedDB cleanup on verified private copies.

The original is never opened by Chromium. A durable mutation checkpoint precedes
installation of the cold-verified family. Interrupted installation is unknown;
only an exact frozen logical after state can subsequently verify completion.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import frozen_files
from .office_leveldb import _environment, _node, _snapshot

SCHEMA = "larj.paseo-indexeddb.v1"
RELATIVE = "IndexedDB/paseo_app_0.indexeddb.leveldb"
CODE = Path(__file__).with_name("leveldb_runtime")


class PaseoIndexedDBError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.kind = code


def fail(code):
    raise PaseoIndexedDBError("paseo_" + code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def runtime(root):
    """Only the complete official Electron 44.2.0 Windows distribution."""
    if os.name != "nt":
        fail("browser_platform_unqualified")
    root = Path(root)
    expected = json.loads((CODE / "paseo_electron.json").read_text(encoding="utf-8"))
    found = frozen_files.freeze_remove(root.parent, root.name)
    expected_names = {item["path"] for item in expected["files"]}
    if {item["path"][len(root.name) + 1:] for item in found["files"]} != expected_names:
        fail("browser_runtime_layout_changed")
    by_name = {item["path"][len(root.name) + 1:]: item for item in found["files"]}
    for item in expected["files"]:
        actual = by_name[item["path"]]
        if (actual["sha256"], actual["size"]) != (item["sha256"], item["size"]):
            fail("browser_runtime_unverified")
    helpers = [frozen_files.read_file(CODE, name)[0] for name in (
        "paseo_browser.cjs", "physical.cjs", "idb_keys.cjs", "idb_audit.cjs")]
    node = _node().resolve(strict=True)
    return {"root": str(root), "distribution": found, "helpers": helpers,
            "node": str(node), "node_file": frozen_files.read_file(node.parent, node.name)[0]}


def _run(command, *, cwd=None, timeout=60):
    try:
        result = subprocess.run(list(map(str, command)), capture_output=True, timeout=timeout,
            cwd=cwd, env=_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PaseoIndexedDBError("paseo_browser_helper_failed") from exc
    if len(result.stdout) > 1024 * 1024:
        fail("browser_output_budget_exceeded")
    try:
        lines = [line for line in result.stdout.splitlines() if line.startswith(b"{")]
        if len(lines) != 1:
            raise ValueError()
        value = json.loads(lines[0])
    except (ValueError, UnicodeError) as exc:
        raise PaseoIndexedDBError("paseo_browser_response_invalid") from exc
    if result.returncode or value.get("status") in {"blocked", "unknown"}:
        fail("browser_copy_unverified")
    return value


def audit(path, state):
    value = _run([state["node"], CODE / "idb_audit.cjs", path])
    if value.get("schema_version") != "larj.paseo-idb-audit.v1":
        fail("browser_physical_audit_invalid")
    return value


def _browser(session, temporary, state, server_id, ids, *, before=None, after=None):
    control = Path(tempfile.mkdtemp(prefix="control-", dir=temporary))
    request = {"mode": "inspect" if before is None else "apply", "sessionPath": str(session),
               "controlPath": str(control), "serverId": server_id, "agentIds": ids,
               "beforeSha256": before, "afterSha256": after}
    path = control / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    value = _run([Path(state["root"]) / "electron.exe", CODE / "paseo_browser.cjs", path], cwd=control)
    if (value.get("status") != "verified" or value.get("origin") != "paseo://app"
            or value.get("valueCodec") != "json-safe.v1"
            or value.get("runtime") != {"electron": "44.2.0", "chrome": "152.0.7977.76", "node": "24.20.0"}):
        fail("browser_logical_audit_invalid")
    for key in ("beforeSha256", "expectedAfterSha256", "afterSha256"):
        import re
        if not isinstance(value.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", value[key]):
            fail("browser_logical_audit_invalid")
    return value


def _copy(root, snapshot, stage, *, relative=RELATIVE):
    destination = stage / relative
    stage.mkdir(parents=True, exist_ok=True)
    with ExitStack() as fences:
        fences.enter_context(frozen_files._parent_fence(stage, ".larj-parent-fence"))
        prefix = ""
        for component in relative.split("/"):
            prefix = prefix + "/" + component if prefix else component
            path = frozen_files.checked_path(stage, prefix)
            path.mkdir(exist_ok=True)
            fences.enter_context(frozen_files._parent_fence(stage, prefix + "/.larj-parent-fence"))
        for item in snapshot["files"]:
            evidence, body = frozen_files.read_file(root, item["path"], content=True)
            if evidence != item:
                fail("browser_source_changed")
            with (destination / Path(item["path"]).name).open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
    if _snapshot(root, relative) != snapshot:
        fail("browser_source_changed")


def _observe(stage, temporary, state, server_id, ids):
    raw = audit(stage / RELATIVE, state)
    value = _browser(stage, temporary, state, server_id, ids)
    opened = audit(stage / RELATIVE, state)
    if (opened["preservationSha256"] != raw["preservationSha256"]
            or opened["dataRecords"] != raw["dataRecords"]
            or value["totalRecords"] != raw["dataRecords"]
            or value["beforeSha256"] != value["afterSha256"]):
        fail("browser_native_open_changed_records")
    return {"before_sha256": value["beforeSha256"], "after_sha256": value["expectedAfterSha256"],
            "selected": value["selectedBefore"], "raw_preservation_sha256": raw["preservationSha256"],
            "data_records": raw["dataRecords"]}


def require_no_blob(root):
    blob = frozen_files.checked_path(root, RELATIVE.replace(".leveldb", ".blob"))
    try:
        blob.lstat()
    except FileNotFoundError:
        return
    fail("browser_blob_family_unqualified")


def freeze(root, electron_root, server_id, ids):
    root = Path(root)
    require_no_sealed(root)
    from .paseo_cleanup_files import ID
    if (not isinstance(ids, (list, tuple)) or not ids or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
            or not isinstance(server_id, str) or not ID.fullmatch(server_id)):
        fail("browser_selection_invalid")
    ids = sorted(set(ids))
    snapshot = _snapshot(root, RELATIVE)
    # A sibling .blob family needs a separately qualified external-blob writer.
    require_no_blob(root)
    state = runtime(electron_root) if snapshot else None
    observation = None
    if snapshot:
        with tempfile.TemporaryDirectory(prefix="larj-paseo-idb-") as raw:
            temporary = Path(raw).resolve(strict=True)
            stage = temporary / "session"
            _copy(root, snapshot, stage)
            observation = _observe(stage, temporary, state, server_id, ids)
        if runtime(electron_root) != state:
            fail("browser_runtime_changed")
    return {"schema_version": SCHEMA, "root": str(root), "relative": RELATIVE, "server_id": server_id,
            "agent_ids": ids, "files": snapshot, "runtime": state, "observation": observation}


def _validate(evidence):
    from .paseo_cleanup_files import ID
    ids = evidence.get("agent_ids")
    if (evidence.get("schema_version") != SCHEMA or evidence.get("relative") != RELATIVE
            or not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
            or ids != sorted(set(ids)) or not isinstance(evidence.get("server_id"), str) or not ID.fullmatch(evidence["server_id"])):
        fail("browser_frozen_scope_invalid")
    frozen_files.checked_path(Path(evidence["root"]), RELATIVE)
    return Path(evidence["root"])


@contextmanager
def prepared(evidence):
    """All native opens and after checks complete before original mutation."""
    root = _validate(evidence)
    require_no_blob(root)
    require_no_sealed(root)
    if _snapshot(root, RELATIVE) != evidence["files"]:
        if remaining(evidence) == 0:
            yield None
            return
        fail("browser_source_changed")
    if evidence["files"] is None:
        yield None
        return
    state = evidence["runtime"]
    if runtime(state["root"]) != state:
        fail("browser_runtime_changed")
    with tempfile.TemporaryDirectory(prefix="larj-paseo-idb-apply-") as raw:
        temporary = Path(raw).resolve(strict=True)
        stage = temporary / "stage"
        _copy(root, evidence["files"], stage)
        observation = _observe(stage, temporary, state, evidence["server_id"], evidence["agent_ids"])
        if observation != evidence["observation"]:
            fail("browser_frozen_state_changed")
        if observation["selected"]:
            result = _browser(stage, temporary, state, evidence["server_id"], evidence["agent_ids"],
                before=observation["before_sha256"], after=observation["after_sha256"])
            if result["afterSha256"] != observation["after_sha256"] or result["selectedAfter"]:
                fail("browser_after_unverified")
        # The exact staged family is reopened only on another private copy.
        snapshot = _snapshot(stage, RELATIVE)
        cold = temporary / "cold"
        _copy(stage, snapshot, cold)
        observed = _observe(cold, temporary, state, evidence["server_id"], evidence["agent_ids"])
        if observed["before_sha256"] != observation["after_sha256"] or observed["selected"]:
            fail("browser_cold_after_unverified")
        if runtime(state["root"]) != state:
            fail("browser_runtime_changed")
        yield (stage, snapshot) if observation["selected"] else None


def install(evidence, prepared_family, *, phase_callback):
    """Caller holds the product lifecycle; no implicit retries or backups."""
    root = _validate(evidence)
    if prepared_family is None:
        if remaining(evidence):
            fail("browser_after_unverified")
        return
    install_family(root, RELATIVE, evidence["files"], prepared_family, phase_callback=phase_callback,
                   verify_after=lambda: _verify_installed(evidence))


def require_no_sealed(root):
    for path in Path(root).iterdir():
        if path.name.casefold().startswith(".larj-paseo-sealed-"):
            fail("cache_install_recovery_required")


def install_family(root, relative, before, prepared_family, *, phase_callback, verify_after):
    if relative not in {RELATIVE, "Local Storage/leveldb"}:
        fail("browser_family_unqualified")
    if relative == RELATIVE:
        require_no_blob(root)
    if _snapshot(root, relative) != before:
        fail("browser_source_changed")
    if prepared_family is None:
        return
    require_no_sealed(root)
    stage, snapshot = prepared_family
    if _snapshot(stage, relative) != snapshot:
        fail("browser_preparation_changed")
    sealed_relative = ".larj-paseo-sealed-" + _digest([str(root), relative, before])[:32]
    sealed = frozen_files.checked_path(root, sealed_relative)
    # A complete durable after copy survives any later interrupted install.
    # No cleanup in finally: partial/unknown seals are recovery evidence.
    phase_callback("mutation_started")
    with frozen_files._parent_fence(root, sealed_relative):
        sealed.mkdir(mode=0o700)
        with frozen_files._parent_fence(root, sealed_relative + "/owner.json"):
            _copy(stage, snapshot, sealed, relative=relative)
            with frozen_files._parent_fence(sealed, relative + "/CURRENT"):
                sealed_snapshot = _snapshot(sealed, relative)
                if [(i["path"], i["size"], i["sha256"]) for i in sealed_snapshot["files"]] != [(i["path"], i["size"], i["sha256"]) for i in snapshot["files"]]:
                    fail("browser_sealed_copy_changed")
                manifest = {"schema_version": "larj.paseo-sealed-after.v1", "root": str(root), "relative": relative,
                            "before_sha256": _digest(before), "files": sealed_snapshot}
                with (sealed / "owner.json").open("x", encoding="utf-8") as stream:
                    json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                owned_seal = frozen_files.freeze_remove(root, sealed_relative)
    # Keep the directory itself pinned while replacing only frozen members.
    # Every removal uses a verified handle; a newly appeared member survives
    # and makes the operation unknown rather than being recursively removed.
    with frozen_files._parent_fence(root, relative + "/CURRENT"):
        if relative == RELATIVE:
            require_no_blob(root)
        if _snapshot(root, relative) != before or frozen_files.freeze_remove(root, sealed_relative) != owned_seal:
            fail("browser_source_changed")
        for item in before["files"]:
            frozen_files._delete_frozen(root, item)
        for item in sorted(snapshot["files"], key=lambda i: Path(i["path"]).name == "CURRENT"):
            proof, body = frozen_files.read_file(stage, item["path"], content=True)
            if proof != item:
                fail("browser_preparation_changed")
            path = frozen_files.checked_path(root, item["path"])
            with path.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        current = _snapshot(root, relative)
        def contents(value):
            return [(i["path"], i["sha256"], i["size"]) for i in value["files"]]
        if contents(current) != contents(snapshot):
            fail("browser_install_unverified")
        verify_after()
        frozen_files.apply_remove(root, owned_seal)


def _verify_installed(evidence):
    root, state = Path(evidence["root"]), evidence["runtime"]
    require_no_blob(root)
    with tempfile.TemporaryDirectory(prefix="larj-paseo-idb-installed-") as raw:
        temporary = Path(raw).resolve(strict=True)
        stage = temporary / "session"
        _copy(root, _snapshot(root, RELATIVE), stage)
        value = _observe(stage, temporary, state, evidence["server_id"], evidence["agent_ids"])
    if value["before_sha256"] != evidence["observation"]["after_sha256"] or value["selected"]:
        fail("browser_installed_after_unverified")


def remaining(evidence):
    root = _validate(evidence)
    require_no_blob(root)
    require_no_sealed(root)
    snapshot = _snapshot(root, RELATIVE)
    if snapshot is None:
        if evidence["files"] is not None:
            fail("browser_shared_family_missing")
        return 0
    if evidence["files"] is None:
        fail("browser_unexpected_family")
    state = evidence["runtime"]
    if runtime(state["root"]) != state:
        fail("browser_runtime_changed")
    with tempfile.TemporaryDirectory(prefix="larj-paseo-idb-verify-") as raw:
        temporary = Path(raw).resolve(strict=True)
        stage = temporary / "session"
        _copy(root, snapshot, stage)
        value = _observe(stage, temporary, state, evidence["server_id"], evidence["agent_ids"])
    expected = evidence["observation"]
    if value["before_sha256"] == expected["after_sha256"] and not value["selected"]:
        return 0
    if snapshot == evidence["files"] and value == expected:
        return value["selected"]
    fail("browser_state_unknown")
