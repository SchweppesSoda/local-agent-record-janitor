"""A held Windows boundary for one frozen Herdr runtime/profile closure."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import re

from . import frozen_files, herdr_cleanup_files, herdr_cleanup_json as codec
from .herdr_discovery import raw_herdr_join
from .record_identity import canonical_path
from .windows_held_files import HeldFiles, pipe_lease

_active = ContextVar("herdr_held_boundary", default=None)


def current_boundary(root):
    value = _active.get()
    return value if value and canonical_path(value.root) == canonical_path(root) else None


def freeze(manifest, closure):
    if os.name != "nt":
        codec.fail("held_lifecycle_platform_unqualified")
    if "HERDR_SOCKET_PATH" in os.environ:
        codec.fail("custom_socket_namespace_unqualified")
    binaries = []
    for value in manifest["runtime_binaries"]:
        path = Path(value)
        evidence, _ = frozen_files.read_file(path.parent, path.name)
        binaries.append({"root": str(path.parent), "file": evidence})
    sessions = {"default", *(item["session"] for item in closure["files"])}
    sessions.update(item["session"] for item in manifest["native_stores"])
    markers, endpoints = [], []
    root = Path(closure["root"])
    for session in sorted(sessions):
        relative = "herdr.sock" if session == "default" else "sessions/" + session + "/herdr.sock"
        raw = manifest["profile_root"] if session == "default" else raw_herdr_join(
            raw_herdr_join(manifest["profile_root"], "sessions"), session)
        endpoints.append("\\\\.\\pipe\\" + raw_herdr_join(raw, "herdr.sock"))
        try:
            evidence, data = frozen_files.read_file(root, relative, content=True, limit=128)
        except FileNotFoundError:
            markers.append({"path": relative, "absent": True})
            continue
        try:
            value = data.decode("ascii").strip()
            match = re.fullmatch(r"([0-9]+):([0-9]+)", value, re.ASCII)
            if not match or not 0 < int(match[1]) <= 2**32 - 1 or int(match[2]) > 2**128 - 1:
                codec.fail("runtime_marker_unverified")
        except UnicodeError:
            codec.fail("runtime_marker_unverified")
        markers.append({"file": evidence, "pid": int(match[1])})
    return {"schema_version": "larj.herdr-lifecycle.windows.v1", "binaries": binaries,
            "pipe_names": endpoints, "markers": markers}


def require_closed(evidence, root, *, census=None):
    from .office_runtime import processes, _RUNTIMES
    rows = (census or processes)()
    if not isinstance(rows, (tuple, list)) or len(rows) > 10000:
        codec.fail("writer_census_unverified")
    declared = {item["pid"] for item in evidence["markers"] if "pid" in item}
    binary_paths = {canonical_path(Path(item["root"]) / item["file"]["path"]) for item in evidence["binaries"]}
    root_name = str(root).replace("\\", "/").casefold()
    for row in rows:
        if not isinstance(row, dict) or type(row.get("pid")) is not int or type(row.get("parent")) is not int:
            codec.fail("writer_census_unverified")
        if row["pid"] == os.getpid():
            continue
        name = str(row.get("name") or "").casefold()
        executable, command = row.get("executable"), row.get("command")
        if (name in {"herdr", "herdr.exe", "herdr-dev", "herdr-dev.exe"} or row["pid"] in declared
                or executable and canonical_path(executable) in binary_paths):
            codec.fail("writer_running")
        if name in _RUNTIMES:
            if not isinstance(executable, str) or not isinstance(command, str):
                codec.fail("writer_census_unverified")
            if root_name in command.replace("\\", "/").casefold():
                codec.fail("writer_running")


@dataclass
class Boundary:
    root: Path
    files: HeldFiles
    evidence: dict
    applied: bool = False

    def apply(self, *, phase_callback):
        closure = self.evidence["files"]
        bodies = herdr_cleanup_files.replacements(closure, reader=self.files.read)
        phase_callback("mutation_started")
        for item in closure["files"]:
            relative = item["before"]["path"]
            if relative in bodies:
                self.files.replace(self.root, relative, bodies[relative], before=item["before"],
                                   after_sha256=item["after_sha256"])
        self.applied = True
        if herdr_cleanup_files.remaining(closure, reader=self.files.read):
            codec.fail("frontend_after_unverified")


@contextmanager
def hold(evidence, *, census=None):
    if os.name != "nt":
        codec.fail("held_lifecycle_platform_unqualified")
    root, lifecycle = Path(evidence["root"]), evidence["lifecycle"]
    if lifecycle.get("schema_version") != "larj.herdr-lifecycle.windows.v1":
        codec.fail("held_lifecycle_schema_invalid")
    if freeze(evidence["binding_manifest"], evidence["files"]) != lifecycle:
        codec.fail("held_lifecycle_evidence_changed")
    if current_boundary(root):
        codec.fail("held_lifecycle_reentry")
    require_closed(lifecycle, root, census=census)
    with ExitStack() as stack:
        files = stack.enter_context(HeldFiles())
        for binary in lifecycle["binaries"]:
            files.acquire(Path(binary["root"]), binary["file"])
        for name in lifecycle["pipe_names"]:
            stack.enter_context(pipe_lease(name))
        for marker in lifecycle["markers"]:
            if marker.get("absent"):
                if frozen_files.checked_path(root, marker["path"]).exists():
                    codec.fail("runtime_marker_changed")
            else:
                files.acquire(root, marker["file"])
        for item in evidence["files"]["files"]:
            files.acquire(root, item["before"], writable=True)
        require_closed(lifecycle, root, census=census)
        boundary = Boundary(root, files, evidence)
        # All frozen bytes, absences and memberships are rechecked after the
        # executable/namespace/file gates, before the first native child.
        herdr_cleanup_files.replacements(evidence["files"], reader=files.read)
        token = _active.set(boundary)
        try:
            yield boundary
        finally:
            _active.reset(token)
