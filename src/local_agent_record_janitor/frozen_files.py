"""Exact, content-free file evidence for closed-client cleanup writers.

Callers own lifecycle exclusion, durable mutation checkpoints and the product's
path/record ownership rules. This module never discovers a deletion target or
retries a partially executed manifest. It only applies the already frozen file
changes, and can inspect their results without writing after a crash.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable

from .legacy_index import _fsync_directory
from .path_identity import is_local_absolute_locator

MAX_ENTRIES = 20_000
MAX_BYTES = 512 * 1024 * 1024
MAX_REWRITE_BYTES = 16 * 1024 * 1024


class FrozenFilesError(RuntimeError):
    kind = "client_file_evidence_changed"


def _identity(info):
    return [info.st_dev, info.st_ino]


def _plain(info, *, directory=False):
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink != 1):
        raise FrozenFilesError("client_file_link_or_special")


def checked_path(root: Path, relative: str) -> Path:
    root = Path(root).expanduser()
    if (not is_local_absolute_locator(str(root)) or root != Path(os.path.abspath(root))
            or not isinstance(relative, str) or not relative or "\\" in relative
            or ":" in relative or any(ord(char) < 32 for char in relative)
            or relative.startswith("/") or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise FrozenFilesError("client_file_path_invalid")
    if any(part.endswith((".", " ")) or part.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))
    } for part in relative.split("/")):
        raise FrozenFilesError("client_file_path_alias")
    for parent in (*reversed(root.parents), root):
        _plain(parent.lstat(), directory=True)
    path = root.joinpath(*relative.split("/"))
    path.relative_to(root)
    # Do not resolve a descendant: following a new link would change approval.
    for parent in reversed(path.parents):
        if parent == root or root in parent.parents:
            try:
                _plain(parent.lstat(), directory=True)
            except FileNotFoundError:
                break
    return path


def _mode(info):
    # Windows lstat synthesizes execute bits from a .exe/.bat filename; fstat
    # has no filename and cannot synthesize them. They are not NTFS access bits.
    return stat.S_IMODE(info.st_mode) & (~0o111 if os.name == "nt" else 0o7777)


def _revision(info):
    # Windows 3.12 lstat/fstat can disagree about ctime immediately after file
    # creation. Identity, size, mtime and a stable read/hash still bind the file.
    change_time = () if os.name == "nt" else (info.st_ctime_ns,)
    return (*_identity(info), stat.S_IFMT(info.st_mode), _mode(info), info.st_nlink, info.st_size, info.st_mtime_ns, *change_time)


def read_file(root: Path, relative: str, *, content=False, limit=MAX_BYTES):
    """Read a stable ordinary single-link file; bodies never enter evidence."""
    path = checked_path(root, relative)
    before = path.lstat()
    _plain(before)
    if before.st_size > limit:
        raise FrozenFilesError("client_file_budget_exceeded")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        _plain(opened)
        if _revision(opened) != _revision(before):
            raise FrozenFilesError("client_file_replaced")
        digest, size, chunks = hashlib.sha256(), 0, []
        while True:
            data = os.read(fd, min(1024 * 1024, limit - size + 1))
            if not data:
                break
            size += len(data)
            if size > limit:
                raise FrozenFilesError("client_file_budget_exceeded")
            digest.update(data)
            if content:
                chunks.append(data)
        if _revision(os.fstat(fd)) != _revision(before):
            raise FrozenFilesError("client_file_changed")
    finally:
        os.close(fd)
    if _revision(checked_path(root, relative).lstat()) != _revision(before):
        raise FrozenFilesError("client_file_changed")
    evidence = {"path": relative, "identity": _identity(before), "size": size, "mode": _mode(before),
                "mtime_ns": before.st_mtime_ns, "sha256": digest.hexdigest()}
    return evidence, b"".join(chunks) if content else None


def freeze_rewrite(root: Path, relative: str, transform: Callable[[bytes], bytes]):
    before, original = read_file(root, relative, content=True, limit=MAX_REWRITE_BYTES)
    after = transform(original)
    if not isinstance(after, bytes) or len(after) > MAX_REWRITE_BYTES:
        raise FrozenFilesError("client_file_transform_invalid")
    return {"kind": "rewrite", "before": before, "after_sha256": hashlib.sha256(after).hexdigest(),
            "after_size": len(after)}


def freeze_remove(root: Path, relative: str):
    """Freeze one owned file/tree including empty directories and all members."""
    files, directories, total = [], [], 0

    def names_of(path):
        names = []
        for entry in path.iterdir():
            if len(names) >= MAX_ENTRIES:
                raise FrozenFilesError("client_file_budget_exceeded")
            names.append(entry.name)
        return sorted(names)

    def visit(name, depth=0):
        nonlocal total
        if depth > 128 or len(files) + len(directories) >= MAX_ENTRIES:
            raise FrozenFilesError("client_file_budget_exceeded")
        path = checked_path(root, name)
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            _plain(info, directory=True)
            names = names_of(path)
            directories.append({"path": name, "identity": _identity(info), "entries": names})
            for child in names:
                visit(name + "/" + child, depth + 1)
            if (_identity(checked_path(root, name).lstat()) != _identity(info)
                    or names != names_of(path)):
                raise FrozenFilesError("client_file_tree_changed")
        else:
            evidence, _ = read_file(root, name, limit=MAX_BYTES - total)
            files.append(evidence)
            total += evidence["size"]

    visit(relative)
    return {"kind": "remove", "path": relative, "files": files, "directories": directories}


def _check_remove(root, change):
    if freeze_remove(root, change["path"]) != change:
        raise FrozenFilesError("client_file_tree_changed")


def _windows_fd(path, *, directory=False, delete=False):
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    # Attribute-only directory handles do not enforce Windows sharing checks
    # against rename. GENERIC_READ is needed for a real namespace fence.
    access = 0x80000000 | (0x10000 if delete else 0)
    # A directory fence permits child I/O but denies moving its namespace.
    sharing = 1 if delete else 3
    handle = kernel.CreateFileW(str(path), access, sharing, None, 3,
                                0x200000 | (0x2000000 if directory else 0), None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    os.set_inheritable(fd, False)
    return fd


@contextmanager
def _parent_fence(root, relative):
    path = checked_path(root, relative)
    with ExitStack() as stack:
        if os.name == "nt":
            for parent in reversed(path.parents):
                before = parent.lstat()
                _plain(before, directory=True)
                fd = _windows_fd(parent, directory=True)
                stack.callback(os.close, fd)
                opened = os.fstat(fd)
                _plain(opened, directory=True)
                if _identity(opened) != _identity(before):
                    raise FrozenFilesError("client_file_parent_replaced")
        yield path


def _delete_frozen(root, before, *, directory=False):
    with _parent_fence(root, before["path"]) as path:
        if os.name != "nt":
            # POSIX callers must hold the product's lifecycle exclusion; an
            # ordinary flock is not a lock against arbitrary pathname writes.
            if directory:
                if _identity(path.lstat()) != before["identity"]:
                    raise FrozenFilesError("client_file_directory_replaced")
                path.rmdir()
            else:
                current, _ = read_file(root, before["path"])
                if current != before:
                    raise FrozenFilesError("client_file_changed_before_unlink")
                path.unlink()
            return
        import ctypes
        from ctypes import wintypes
        import msvcrt
        fd = _windows_fd(path, directory=directory, delete=True)
        try:
            info = os.fstat(fd)
            _plain(info, directory=directory)
            if _identity(info) != before["identity"]:
                raise FrozenFilesError("client_file_changed_before_unlink")
            if not directory:
                digest, size = hashlib.sha256(), 0
                while chunk := os.read(fd, min(1024 * 1024, before["size"] - size + 1)):
                    size += len(chunk)
                    if size > before["size"]:
                        raise FrozenFilesError("client_file_changed_before_unlink")
                    digest.update(chunk)
                if (size != before["size"] or digest.hexdigest() != before["sha256"]
                        or info.st_mtime_ns != before["mtime_ns"] or _mode(info) != before["mode"]):
                    raise FrozenFilesError("client_file_changed_before_unlink")
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            kernel.SetFileInformationByHandle.restype = wintypes.BOOL
            disposition = wintypes.BOOL(True)
            if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(fd), 4,
                                                     ctypes.byref(disposition), ctypes.sizeof(disposition)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            os.close(fd)


def apply_rewrite(root: Path, change, transform: Callable[[bytes], bytes]):
    if change.get("kind") != "rewrite":
        raise FrozenFilesError("client_file_change_invalid")
    before, original = read_file(root, change["before"]["path"], content=True, limit=MAX_REWRITE_BYTES)
    if before != change["before"]:
        raise FrozenFilesError("client_file_changed_before_write")
    after = transform(original)
    if (not isinstance(after, bytes) or len(after) != change["after_size"]
            or hashlib.sha256(after).hexdigest() != change["after_sha256"]):
        raise FrozenFilesError("client_file_transform_changed")
    path = checked_path(root, before["path"])
    fd, raw = tempfile.mkstemp(prefix=".larj-write-", dir=path.parent)
    temporary = Path(raw)
    temporary_identity = _identity(os.fstat(fd))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(after)
            stream.flush()
            if os.name != "nt":
                os.fchmod(stream.fileno(), before["mode"])
            os.fsync(stream.fileno())
        fresh, _ = read_file(root, before["path"])
        if fresh != before:
            raise FrozenFilesError("client_file_changed_before_write")
        temporary_relative = temporary.relative_to(root).as_posix()
        temporary_evidence, _ = read_file(root, temporary_relative, limit=MAX_REWRITE_BYTES)
        if (temporary_evidence["identity"] != temporary_identity
                or temporary_evidence["sha256"] != change["after_sha256"]
                or temporary_evidence["size"] != change["after_size"]):
            raise FrozenFilesError("client_preparation_file_replaced")
        with _parent_fence(root, before["path"]):
            os.replace(temporary, checked_path(root, before["path"]))
        _fsync_directory(path.parent)
        if not satisfied(root, change):
            raise FrozenFilesError("client_file_after_state_unverified")
    finally:
        # Only our uniquely created preparation file is eligible for removal.
        try:
            info = temporary.lstat()
        except FileNotFoundError:
            pass
        else:
            _plain(info)
            if _identity(info) != temporary_identity:
                raise FrozenFilesError("client_preparation_file_replaced")
            prepared, _ = read_file(root, temporary.relative_to(root).as_posix(), limit=MAX_REWRITE_BYTES)
            if (prepared["identity"] != temporary_identity or prepared["size"] != change["after_size"]
                    or prepared["sha256"] != change["after_sha256"]):
                raise FrozenFilesError("client_preparation_file_replaced")
            _delete_frozen(root, prepared)


def apply_remove(root: Path, change):
    if change.get("kind") != "remove":
        raise FrozenFilesError("client_file_change_invalid")
    _check_remove(root, change)
    for before in change["files"]:
        current, _ = read_file(root, before["path"])
        if current != before:
            raise FrozenFilesError("client_file_changed_before_unlink")
        path = checked_path(root, before["path"])
        _delete_frozen(root, before)
        _fsync_directory(path.parent)
    for directory in reversed(change["directories"]):
        path = checked_path(root, directory["path"])
        info = path.lstat()
        _plain(info, directory=True)
        if _identity(info) != directory["identity"]:
            raise FrozenFilesError("client_file_directory_replaced")
        # rmdir is deliberately non-recursive: a newly added file is never
        # removed merely because its parent directory was approved earlier.
        _delete_frozen(root, directory, directory=True)
        _fsync_directory(path.parent)


def satisfied(root: Path, change) -> bool:
    """Read-only recovery; absence never authorizes retrying a mutation."""
    if change.get("kind") == "remove":
        path = checked_path(root, change["path"])
        try:
            info = path.lstat()
        except FileNotFoundError:
            return True
        _plain(info, directory=stat.S_ISDIR(info.st_mode))
        return False
    if change.get("kind") == "rewrite":
        after, _ = read_file(root, change["before"]["path"], limit=MAX_REWRITE_BYTES)
        return after["sha256"] == change["after_sha256"] and after["size"] == change["after_size"]
    raise FrozenFilesError("client_file_change_invalid")
