"""Private-copy planning and exact-key execution for office Chromium drafts.

Opening LevelDB is a write operation even for a get. Inventory therefore opens
only a verified private copy; execution requires a durable checkpoint before
opening the original. Runtime installation is always an explicit operation.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from . import frozen_files

SCHEMA = "larj.office-drafts.v1"
PROTOCOL = "larj.office-leveldb-helper.v1"
_FILE = re.compile(r"(?:CURRENT|LOCK|LOG(?:\.old)?|MANIFEST-\d+|\d+\.(?:ldb|sst|log))\Z")
_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


class OfficeLevelDBError(RuntimeError):
    def __init__(self, code, *, unknown=False):
        super().__init__(code)
        self.kind, self.unknown = code, unknown


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _runtime_directory():
    if configured := os.environ.get("LARJ_LEVELDB_RUNTIME"):
        return Path(configured).expanduser().absolute()
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache")
    return base / "local-agent-record-janitor" / "leveldb-v1"


def _node():
    candidates = [os.environ.get("LARJ_NODE")]
    if os.name == "nt":
        candidates.extend(str(Path(os.environ.get(name, default)) / "nodejs/node.exe")
                          for name, default in (("ProgramFiles", r"C:\Program Files"),
                                                ("LOCALAPPDATA", str(Path.home() / "AppData/Local"))))
    candidates.append(shutil.which("node"))
    for candidate in dict.fromkeys(value for value in candidates if value):
        path = Path(candidate).expanduser().absolute()
        if not path.is_file():
            continue
        try:
            result = subprocess.run([str(path), "--version"], capture_output=True, timeout=10,
                env=_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            match = re.fullmatch(rb"v(\d+)\.\d+\.\d+\s*", result.stdout)
            if result.returncode == 0 and match and int(match[1]) >= 18:
                return path
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise OfficeLevelDBError("office_leveldb_node_runtime_unavailable")


def _environment(runtime=None):
    # NODE_OPTIONS/NODE_PATH and loader injection must not run during cleanup.
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "TMPDIR",
               "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if runtime is not None:
        env["LARJ_LEVELDB_RUNTIME"] = str(runtime)
    return env


def runtime():
    root = _runtime_directory()
    try:
        package = root / "node_modules/classic-level/package.json"
        frozen_files.checked_path(root, "node_modules/classic-level/package.json")
        value = json.loads(package.read_text(encoding="utf-8"))
        if value.get("version") != "3.0.0":
            raise ValueError("version")
    except (OSError, ValueError, frozen_files.FrozenFilesError) as exc:
        raise OfficeLevelDBError("office_leveldb_dependency_unavailable") from exc
    helper = Path(str(files(__package__).joinpath("leveldb_runtime/office_drafts.cjs")))
    return _node(), root, helper


def runtime_fingerprint():
    """Bind the executable and installed loader/native code, not just a version."""
    node, root, helper = runtime()
    manifest, total = [], 0
    pending = [root / "node_modules"]
    while pending:
        directory = pending.pop()
        frozen_files.checked_path(root, directory.relative_to(root).as_posix())
        frozen_files._plain(directory.lstat(), directory=True)
        for entry in sorted(directory.iterdir()):
            if entry.name == ".bin":  # npm command shims are never executed.
                continue
            info = entry.lstat()
            frozen_files._plain(info, directory=stat.S_ISDIR(info.st_mode))
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry)
                continue
            if entry.suffix not in {".js", ".cjs", ".mjs", ".json", ".node"}:
                continue
            if len(manifest) >= 4096 or (total := total + info.st_size) > 512 * 1024 * 1024:
                raise OfficeLevelDBError("office_leveldb_runtime_budget_exceeded")
            value, _ = frozen_files.read_file(root, entry.relative_to(root).as_posix())
            manifest.append(value)
            if entry.name == "package.json":
                package = json.loads(entry.read_text(encoding="utf-8"))
                main = package.get("main", "index.js")
                if not isinstance(main, str) or Path(main).is_absolute() or ".." in Path(main).parts:
                    raise OfficeLevelDBError("office_leveldb_dependency_redirected")
    manifest.sort(key=lambda value: value["path"])
    node = node.resolve(strict=True)
    executable, _ = frozen_files.read_file(node.parent, node.name)
    helpers = [frozen_files.read_file(helper.parent, name)[0]
               for name in ("office_drafts.cjs", "office_ui.cjs", "paseo_ui.cjs", "physical.cjs")]
    return {"root": str(root), "node": str(node), "executable": executable,
            "code_sha256": hashlib.sha256(_json(manifest)).hexdigest(), "helpers": helpers}


def _validate_response(response, operation):
    def sha(value, *, nullable=False):
        return value is None and nullable or isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
    def integer(value):
        return type(value) is int and 0 <= value <= 20000
    if not isinstance(response, dict) or response.get("schema_version") != PROTOCOL:
        raise ValueError("protocol")
    if response.get("status") == "blocked":
        return
    if operation == "apply":
        if (response.get("status") != "verified" or response.get("mutation_started") is not True
                or not integer(response.get("removed_pairs")) or response.get("remaining_pairs") != 0
                or type(response.get("remaining_pairs")) is not int):
            raise ValueError("status")
        items = response.get("after_keys")
        if not isinstance(items, list) or len(items) > 20000 or any(not isinstance(item, dict)
                or set(item) != {"key_sha256", "value_sha256", "metadata_sha256"}
                or not sha(item["key_sha256"]) or not sha(item["value_sha256"], nullable=True)
                or not sha(item["metadata_sha256"]) for item in items):
            raise ValueError("after_keys")
        return
    if any(not sha(response.get(key)) for key in ("fingerprint", "logical_before_sha256", "logical_after_sha256")):
        raise ValueError("hash")
    count = response.get("count")
    if not isinstance(count, dict) or set(count) != {"draft_maps", "removed_pairs", "keys"} or not all(integer(i) for i in count.values()):
        raise ValueError("count")
    keys, edits = response.get("draft_keys"), response.get("edits")
    key_fields = {"key_sha256", "value_sha256", "metadata_key_sha256", "metadata_sha256"}
    edit_fields = {"key_sha256", "before_sha256", "after_sha256", "metadata_key_sha256", "before_metadata_sha256", "after_metadata_sha256", "pairs"}
    if not isinstance(keys, list) or len(keys) != count["draft_maps"] or any(not isinstance(item, dict)
            or set(item) != key_fields or not all(sha(i) for i in item.values()) for item in keys):
        raise ValueError("keys")
    if not isinstance(edits, list) or len(edits) > len(keys):
        raise ValueError("edits")
    found = {item["key_sha256"] for item in keys}
    if len(found) != len(keys):
        raise ValueError("duplicate_key")
    total = 0
    for item in edits:
        if not isinstance(item, dict) or set(item) != edit_fields or item["key_sha256"] not in found:
            raise ValueError("edit")
        found.remove(item["key_sha256"])
        if any(not sha(value, nullable=key == "after_sha256") for key, value in item.items() if key != "pairs"):
            raise ValueError("edit_hash")
        pairs = item["pairs"]
        if not isinstance(pairs, list) or not pairs or any(not isinstance(pair, list) or len(pair) not in (1, 2)
                or any(not isinstance(i, str) or not _ID.fullmatch(i) for i in pair) for pair in pairs):
            raise ValueError("pairs")
        total += len(pairs)
    if total != count["removed_pairs"]:
        raise ValueError("count")


def _call(request, *, timeout=30):
    node, dependency, helper = runtime()
    try:
        result = subprocess.run([str(node), str(helper)], input=_json(request) + b"\n",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=dependency,
            env=_environment(dependency), timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OfficeLevelDBError("office_leveldb_helper_failed",
                                unknown=request["operation"] == "apply") from exc
    if len(result.stdout) > 8 * 1024 * 1024:
        raise OfficeLevelDBError("office_leveldb_response_budget_exceeded", unknown=request["operation"] == "apply")
    try:
        response = json.loads(result.stdout)
        _validate_response(response, request["operation"])
    except (ValueError, AttributeError) as exc:
        raise OfficeLevelDBError("office_leveldb_response_invalid", unknown=request["operation"] == "apply") from exc
    if result.returncode or response.get("status") == "blocked":
        code = response.get("blocker_code", "")
        if not isinstance(code, str) or not re.fullmatch(r"office_leveldb_[a-z_]+", code):
            code = "office_leveldb_helper_failed"
        raise OfficeLevelDBError(code, unknown=request["operation"] == "apply")
    return response


def _snapshot(root, relative):
    path = frozen_files.checked_path(root, relative)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    value = frozen_files.freeze_remove(root, relative)
    if (len(value["directories"]) != 1 or any(not _FILE.fullmatch(Path(item["path"]).name)
                                              for item in value["files"])
            or not any(Path(item["path"]).name == "CURRENT" for item in value["files"])):
        raise OfficeLevelDBError("office_leveldb_file_layout_unverified")
    return value


def _inspect_copy(root, relative, chat_ids, snapshot, sub_chat_ids=()):
    with tempfile.TemporaryDirectory(prefix="larj-office-drafts-") as temporary:
        copy = Path(temporary).resolve(strict=True) / "leveldb"
        copy.mkdir(mode=0o700)
        for item in snapshot["files"]:
            evidence, raw = frozen_files.read_file(root, item["path"], content=True)
            if evidence != item:
                raise OfficeLevelDBError("office_leveldb_snapshot_changed")
            destination = copy / Path(item["path"]).name
            with destination.open("xb") as stream:
                stream.write(raw)
        if _snapshot(root, relative) != snapshot:
            raise OfficeLevelDBError("office_leveldb_snapshot_changed")
        return _call({"schema_version": PROTOCOL, "operation": "inspect-copy", "path": str(copy),
                      "chat_ids": chat_ids, "sub_chat_ids": list(sub_chat_ids), "origins": ["file://"]})


def freeze(root: Path, relative: str, chat_ids, *, sub_chat_ids=()):
    ids = sorted(set(chat_ids))
    if not ids or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in ids):
        raise OfficeLevelDBError("office_leveldb_chat_identity_invalid")
    children = sorted(set(sub_chat_ids))
    if any(not isinstance(value, str) or not _ID.fullmatch(value) for value in children):
        raise OfficeLevelDBError("office_leveldb_chat_identity_invalid")
    snapshot = _snapshot(root, relative)
    runtime_state = runtime_fingerprint() if snapshot else None
    observation = _inspect_copy(root, relative, ids, snapshot, children) if snapshot else None
    if snapshot and runtime_fingerprint() != runtime_state:
        raise OfficeLevelDBError("office_leveldb_runtime_changed")
    return {"schema_version": SCHEMA, "root": str(root), "relative": relative, "chat_ids": ids, "sub_chat_ids": children,
            "files": snapshot, "observation": observation, "runtime": runtime_state}


def _validated(evidence):
    if evidence.get("schema_version") != SCHEMA or evidence.get("relative") not in {
        "Local Storage/leveldb", "Partitions/main/Local Storage/leveldb"
    }:
        raise OfficeLevelDBError("office_leveldb_frozen_scope_invalid")
    ids = evidence.get("chat_ids")
    if (not isinstance(ids, list) or not ids or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in ids)
            or ids != sorted(set(ids))):
        raise OfficeLevelDBError("office_leveldb_frozen_scope_invalid")
    root = Path(evidence["root"])
    children = evidence.get("sub_chat_ids")
    if not isinstance(children, list) or children != sorted(set(children)) or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in children):
        raise OfficeLevelDBError("office_leveldb_frozen_scope_invalid")
    frozen_files.checked_path(root, evidence["relative"])
    return root


def apply(evidence, *, phase_callback):
    root = _validated(evidence)
    if _snapshot(root, evidence["relative"]) != evidence["files"]:
        raise OfficeLevelDBError("office_leveldb_snapshot_changed")
    if evidence["files"] is None:
        return {"removed_pairs": 0, "remaining_pairs": 0}
    if runtime_fingerprint() != evidence["runtime"]:
        raise OfficeLevelDBError("office_leveldb_runtime_changed")
    observed = _inspect_copy(root, evidence["relative"], evidence["chat_ids"], evidence["files"], evidence["sub_chat_ids"])
    if observed != evidence["observation"]:
        raise OfficeLevelDBError("office_leveldb_frozen_state_changed")
    runtime()  # Missing dependencies are a pre-mutation blocker.
    edits = [{"key": item["key_sha256"], "after": item["after_sha256"], "meta": item["after_metadata_sha256"]}
             for item in observed["edits"]]
    with frozen_files._parent_fence(root, evidence["relative"] + "/CURRENT"):
        if _snapshot(root, evidence["relative"]) != evidence["files"]:
            raise OfficeLevelDBError("office_leveldb_snapshot_changed")
        phase_callback("mutation_started")  # Native open may rotate/recover WAL.
        if _snapshot(root, evidence["relative"]) != evidence["files"]:
            raise OfficeLevelDBError("office_leveldb_snapshot_changed", unknown=True)
        if runtime_fingerprint() != evidence["runtime"]:
            raise OfficeLevelDBError("office_leveldb_runtime_changed", unknown=True)
        result = _call({"schema_version": PROTOCOL, "operation": "apply",
                      "path": str(root / evidence["relative"]), "chat_ids": evidence["chat_ids"], "sub_chat_ids": evidence["sub_chat_ids"],
                      "origins": ["file://"], "authorized_before_sha256": observed["fingerprint"],
                      "authorized_after_sha256": hashlib.sha256(_json(edits)).hexdigest()})
        if runtime_fingerprint() != evidence["runtime"]:
            raise OfficeLevelDBError("office_leveldb_runtime_changed", unknown=True)
        return result


def remaining(evidence, *, terminal_verified=False):
    root = _validated(evidence)
    snapshot = _snapshot(root, evidence["relative"])
    if snapshot is None:
        if evidence["files"] is not None:
            raise OfficeLevelDBError("office_leveldb_shared_store_missing")
        return 0
    value = _inspect_copy(root, evidence["relative"], evidence["chat_ids"], snapshot, evidence["sub_chat_ids"])
    if not terminal_verified and not value["count"]["removed_pairs"]:
        expected = [dict(item) for item in (evidence.get("observation") or {}).get("draft_keys", ())]
        edits = {item["key_sha256"]: item for item in (evidence.get("observation") or {}).get("edits", ())}
        metadata = {item["metadata_key_sha256"]: item["after_metadata_sha256"] for item in edits.values()}
        expected = [item for item in expected if item["key_sha256"] not in edits or edits[item["key_sha256"]]["after_sha256"] is not None]
        for item in expected:
            if edit := edits.get(item["key_sha256"]):
                item["value_sha256"] = edit["after_sha256"]
            if item["metadata_key_sha256"] in metadata:
                item["metadata_sha256"] = metadata[item["metadata_key_sha256"]]
        if value["draft_keys"] != expected:
            raise OfficeLevelDBError("office_leveldb_after_state_unverified")
        if value["logical_before_sha256"] != (evidence.get("observation") or {}).get("logical_after_sha256"):
            raise OfficeLevelDBError("office_leveldb_after_state_unverified")
    return value["count"]["removed_pairs"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install the pinned office draft database helper explicitly.")
    parser.add_argument("--install", action="store_true", required=True)
    parser.add_argument("--runtime-dir", type=Path, default=_runtime_directory())
    args = parser.parse_args(argv)
    root = args.runtime_dir.expanduser().absolute()
    if root.exists() and any(root.iterdir()):
        raise OfficeLevelDBError("office_leveldb_install_directory_not_empty")
    node = _node()
    npm_candidates = [node.parent / "node_modules/npm/bin/npm-cli.js",
                      node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"]
    if os.name != "nt" and (found := shutil.which("npm")):
        npm_candidates.append(Path(found).resolve(strict=True))
    npm = next((path for path in npm_candidates if path.is_file()), None)
    if npm is None:
        raise OfficeLevelDBError("office_leveldb_npm_runtime_unavailable")
    root.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        (root / name).write_bytes(files(__package__).joinpath("leveldb_runtime/" + name).read_bytes())
    result = subprocess.run([str(node), str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                            cwd=root, env=_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise OfficeLevelDBError("office_leveldb_dependency_install_failed")
    print(str(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
