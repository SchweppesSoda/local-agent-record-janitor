"""Windows sharing-mode leases for a frozen, explicitly owned file set.

Unlike a sidecar lock, these handles prevent ordinary readers, writers,
renames and launches. Missing files are never created by this primitive.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path

from . import frozen_files


def exclusive_fd(path, *, writable=False):
    if os.name != "nt":
        raise frozen_files.FrozenFilesError("windows_file_lease_unavailable")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x80000000 | (0x40000000 if writable else 0),
                                0, None, 3, 0x200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, (os.O_RDWR if writable else os.O_RDONLY) | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    os.set_inheritable(fd, False)
    return fd


@contextmanager
def pipe_lease(name):
    if os.name != "nt" or not name.startswith("\\\\.\\pipe\\"):
        raise frozen_files.FrozenFilesError("windows_pipe_lease_unavailable")
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    kernel.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    # First instance plus maxInstances=1 excludes competing servers, including
    # ones that do not request FILE_FLAG_FIRST_PIPE_INSTANCE themselves.
    handle = kernel.CreateNamedPipeW(name, 3 | 0x80000, 0, 1, 4096, 4096, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


class HeldFiles:
    def __init__(self):
        self.stack = ExitStack()
        self.handles = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def acquire(self, root, evidence, *, writable=False):
        root = Path(root)
        key = (str(root), evidence["path"])
        if key in self.handles:
            raise frozen_files.FrozenFilesError("duplicate_file_lease")
        path = self.stack.enter_context(frozen_files._parent_fence(root, evidence["path"]))
        fd = exclusive_fd(path, writable=writable)
        self.stack.callback(os.close, fd)
        self.handles[key] = (fd, writable)
        current, _ = self.read(root, evidence["path"])
        if current != evidence:
            raise frozen_files.FrozenFilesError("held_file_evidence_changed")

    def read(self, root, relative, *, content=False, limit=frozen_files.MAX_BYTES):
        fd, _ = self.handles[(str(root), relative)]
        path = frozen_files.checked_path(Path(root), relative)
        before = os.fstat(fd)
        frozen_files._plain(before)
        if (frozen_files._revision(path.lstat()) != frozen_files._revision(before)
                or before.st_size > limit):
            raise frozen_files.FrozenFilesError("held_file_evidence_changed")
        os.lseek(fd, 0, os.SEEK_SET)
        chunks, digest, size = [], hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise frozen_files.FrozenFilesError("held_file_budget_exceeded")
            digest.update(chunk)
            if content:
                chunks.append(chunk)
        if frozen_files._revision(os.fstat(fd)) != frozen_files._revision(before):
            raise frozen_files.FrozenFilesError("held_file_evidence_changed")
        return {"path": relative, "identity": frozen_files._identity(before), "size": size,
                "mode": frozen_files._mode(before), "mtime_ns": before.st_mtime_ns,
                "sha256": digest.hexdigest()}, b"".join(chunks) if content else None

    def replace(self, root, relative, body, *, before, after_sha256):
        fd, writable = self.handles[(str(root), relative)]
        current, _ = self.read(root, relative)
        if not writable or current != before or hashlib.sha256(body).hexdigest() != after_sha256:
            raise frozen_files.FrozenFilesError("held_file_approval_changed")
        os.lseek(fd, 0, os.SEEK_SET)
        remaining = memoryview(body)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError("held_file_short_write")
            remaining = remaining[count:]
        os.ftruncate(fd, len(body))
        os.fsync(fd)
        observed, _ = self.read(root, relative)
        if observed["sha256"] != after_sha256 or observed["size"] != len(body):
            raise frozen_files.FrozenFilesError("held_file_after_unverified")
