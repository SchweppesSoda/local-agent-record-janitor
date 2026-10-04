"""Held Windows startup boundary for an explicitly bound Paseo deployment."""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket

from . import frozen_files, paseo_cleanup_files as files
from .record_identity import canonical_path
from .windows_held_files import HeldFiles

_active = ContextVar("paseo_lifecycle", default=None)


def current_boundary(root):
    value = _active.get()
    return value if value and canonical_path(value["root"]) == canonical_path(root) else None


def require_pid_absent(root):
    path = frozen_files.checked_path(root, "paseo.pid")
    try:
        path.lstat()
    except FileNotFoundError:
        return
    files.fail("daemon_pid_lock_present")


def freeze(manifest):
    if os.name != "nt":
        files.fail("held_lifecycle_platform_unqualified")
    root = Path(manifest["profile_root"])
    require_pid_absent(root)
    if os.environ.get("PASEO_SERVER_ID", manifest["server_id"]).strip() != manifest["server_id"]:
        files.fail("server_id_override_conflict")
    server, raw = frozen_files.read_file(root, "server-id", content=True, limit=256)
    if raw.decode("utf-8").strip() != manifest["server_id"]:
        files.fail("server_id_changed")
    binaries = list(manifest["runtime_binaries"])
    binaries.extend(binary for profile in manifest["desktop_profiles"] for binary in profile["runtime_binaries"])
    unique = {canonical_path(binary): Path(binary) for binary in binaries}
    return {"schema_version": "larj.paseo-lifecycle.windows.v1", "server_id_file": server,
        "binaries": [{"root": str(path.parent), "file": frozen_files.read_file(path.parent, path.name)[0]}
                     for _key, path in sorted(unique.items())]}


def require_closed(evidence, *, census=None):
    from .office_runtime import processes, _RUNTIMES
    rows = (census or processes)()
    if not isinstance(rows, (tuple, list)) or len(rows) > 10000:
        files.fail("writer_census_unverified")
    binary_paths = {canonical_path(Path(item["root"]) / item["file"]["path"]) for item in evidence["lifecycle"]["binaries"]}
    roots = [evidence["root"], *(p["root"] for p in evidence["binding_manifest"]["desktop_profiles"])]
    for row in rows:
        if not isinstance(row, dict) or type(row.get("pid")) is not int or type(row.get("parent")) is not int:
            files.fail("writer_census_unverified")
        if row["pid"] == os.getpid():
            continue
        name = str(row.get("name") or "").casefold()
        executable, command = row.get("executable"), row.get("command")
        if name.startswith("paseo") or executable and canonical_path(executable) in binary_paths:
            files.fail("writer_running")
        if name in _RUNTIMES:
            if not isinstance(executable, str) or not isinstance(command, str):
                files.fail("writer_census_unverified")
            command = command.replace("\\", "/").casefold()
            if "paseo" in command or any(str(root).replace("\\", "/").casefold() in command for root in roots):
                files.fail("writer_running")


@contextmanager
def pid_lease(root):
    """Standard Paseo startup rejects this live owner; refresh/rename is denied."""
    if os.name != "nt":
        files.fail("held_lifecycle_platform_unqualified")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    with frozen_files._parent_fence(root, "paseo.pid") as path:
        handle = kernel.CreateFileW(str(path), 0xC0010000, 1, None, 1, 0x00200080, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        os.set_inheritable(fd, False)
        try:
            body = json.dumps({"pid": os.getpid(), "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "hostname": socket.gethostname(), "uid": 0, "listen": None, "heartbeat": True}, separators=(",", ":")).encode()
            view = memoryview(body)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("paseo_pid_lease_short_write")
                view = view[count:]
            os.fsync(fd)
            expected = os.fstat(fd)
            if frozen_files._identity(expected) != frozen_files._identity(path.lstat()):
                files.fail("pid_lease_identity_changed")
            yield
        finally:
            try:
                disposition = wintypes.BOOL(True)
                if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                os.close(fd)


@contextmanager
def hold(evidence, *, census=None):
    root = Path(evidence["root"])
    if current_boundary(root) is not None or freeze(evidence["binding_manifest"]) != evidence["lifecycle"]:
        files.fail("held_lifecycle_evidence_changed")
    require_closed(evidence, census=census)
    with ExitStack() as stack:
        held = stack.enter_context(HeldFiles())
        for item in evidence["lifecycle"]["binaries"]:
            held.acquire(Path(item["root"]), item["file"])
        # Metadata readers retain read access to server-id; its exact frozen
        # bytes are checked before every stage instead of caching a descriptor.
        stack.enter_context(pid_lease(root))
        require_closed(evidence, census=census)
        token = _active.set(evidence)
        try:
            yield
        finally:
            _active.reset(token)
