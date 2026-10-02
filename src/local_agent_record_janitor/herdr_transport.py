"""Bounded local JSON API requests; never start, attach, or restore Herdr."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time

from .herdr_discovery import HerdrDiscoveryError, local_herdr_path, require_plain_directory, require_plain_file

ALLOWED_METHODS = {"ping", "session.snapshot"}
MAX_REQUEST_BYTES = 512
CANCEL_RESERVE = 0.05


class HerdrTransportError(ValueError):
    pass


@dataclass(frozen=True)
class HerdrEndpointIdentity:
    parents: tuple[tuple[str, int, int, int, int], ...]
    file_identity: tuple[int, int, int, int]
    marker: bytes = b""

    @property
    def generation_key(self) -> str:
        return hashlib.sha256(repr((self.parents, self.file_identity, self.marker)).encode()).hexdigest()


def endpoint_identity(locator: str | Path, *, host: str = "local", path_namespace: str = "local") -> HerdrEndpointIdentity:
    path = local_herdr_path(locator, host=host, path_namespace=path_namespace)
    if path.name != "herdr.sock":
        raise HerdrTransportError("live_endpoint_name_unproven")
    require_plain_directory(path.parent)
    parents = []
    for directory in (*reversed(path.parent.parents), path.parent):
        info = directory.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or attributes & 0x400:
            raise HerdrTransportError("live_endpoint_changed")
        parents.append((str(directory), info.st_dev, info.st_ino, info.st_mode, attributes))
    before = path.lstat()
    marker = b""
    if os.name == "nt":
        before = require_plain_file(path)
        if before.st_size > 128:
            raise HerdrTransportError("live_marker_invalid")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise HerdrTransportError("live_endpoint_changed")
            marker = stream.read(129)
        match = re.fullmatch(rb"([0-9]+):([0-9]+)", marker)
        if not match or not 0 < int(match[1]) <= 2**32 - 1 or int(match[2]) > 2**128 - 1:
            raise HerdrTransportError("live_marker_invalid")
        after = require_plain_file(path)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise HerdrTransportError("live_endpoint_changed")
    elif not stat.S_ISSOCK(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise HerdrTransportError("live_endpoint_not_socket")
    elif hasattr(os, "geteuid") and before.st_uid != os.geteuid():
        raise HerdrTransportError("live_endpoint_owner_unproven")
    return HerdrEndpointIdentity(tuple(parents), (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns), marker)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HerdrTransportError("live_timeout")
    return remaining


def _line(chunks: bytearray, data: bytes, maximum: int) -> bytes | None:
    chunks.extend(data)
    if len(chunks) > maximum:
        raise HerdrTransportError("live_response_limit_exceeded")
    if b"\n" in chunks:
        first, rest = bytes(chunks).split(b"\n", 1)
        if rest or not first.strip():
            raise HerdrTransportError("live_framing_invalid")
        return first
    if not data:
        raise HerdrTransportError("live_response_truncated")
    return None


def _unix_request(path: Path, request: bytes, deadline: float, maximum: int) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(_remaining(deadline))
        stream.connect(str(path))
        stream.settimeout(_remaining(deadline))
        stream.sendall(request)
        chunks = bytearray()
        while True:
            stream.settimeout(_remaining(deadline))
            result = _line(chunks, stream.recv(min(16384, maximum + 1 - len(chunks))), maximum)
            if result is not None:
                return result


# Only a pathological OS cancellation failure needs deferred retirement. Keep
# the native OVERLAPPED storage alive until its event signals, with no reader
# thread/process and at most one retained operation. Ordinary local NPFS
# cancellation completes during the reserved deadline and retains nothing.
_retiring: list[object] = []
_pipe_io_lock = threading.Lock()


def _reap_cancelled() -> None:
    import _winapi
    pending = []
    for operation in _retiring:
        if _winapi.WaitForSingleObject(operation.event, 0) != _winapi.WAIT_OBJECT_0:
            pending.append(operation)
            continue
        try:
            _, error = operation.GetOverlappedResult(False)
            if error == 996:  # ERROR_IO_INCOMPLETE is not exported by _winapi.
                pending.append(operation)
        except OSError:
            pass  # CPython clears its pending flag on this terminal error.
    _retiring[:] = pending
    if _retiring:
        raise HerdrTransportError("live_pipe_cancellation_unconfirmed")


def _overlapped(operation: object, error: int, deadline: float, io_deadline: float) -> int:
    import _winapi
    try:
        if error == _winapi.ERROR_IO_PENDING:
            wait = _winapi.WaitForSingleObject(operation.event, max(1, math.ceil(_remaining(io_deadline) * 1000)))
            if wait != _winapi.WAIT_OBJECT_0:
                raise HerdrTransportError("live_timeout")
        count, error = operation.GetOverlappedResult(False)
        if error:
            raise HerdrTransportError("live_pipe_io_failed")
        return count
    except BaseException:
        try:
            operation.cancel()
        except OSError:
            pass  # Still retain its storage unless completion is confirmed.
        wait_ms = max(0, math.ceil((deadline - time.monotonic()) * 1000))
        if _winapi.WaitForSingleObject(operation.event, wait_ms) == _winapi.WAIT_OBJECT_0:
            try:
                operation.GetOverlappedResult(False)
            except OSError:
                pass
        else:
            _retiring.append(operation)
        raise


def _windows_request(raw_locator: str, request: bytes, deadline: float, maximum: int, identity: HerdrEndpointIdentity) -> bytes:
    if not _pipe_io_lock.acquire(blocking=False):
        raise HerdrTransportError("live_pipe_probe_busy")
    try:
        return _windows_request_locked(raw_locator, request, deadline, maximum, identity)
    finally:
        _pipe_io_lock.release()


def _windows_request_locked(raw_locator: str, request: bytes, deadline: float, maximum: int, identity: HerdrEndpointIdentity) -> bytes:
    import _winapi
    import ctypes
    from ctypes import wintypes
    _reap_cancelled()
    io_deadline = deadline - CANCEL_RESERVE
    # Pinned interprocess 2.4.2 preserves the endpoint spelling verbatim.
    name = "\\\\.\\pipe\\" + raw_locator
    while True:
        _remaining(io_deadline)
        try:
            handle = _winapi.CreateFile(name, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                0, 0, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, 0)
            break
        except OSError as exc:
            if exc.winerror != _winapi.ERROR_PIPE_BUSY:
                raise
            time.sleep(min(0.002, _remaining(io_deadline)))
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        peer_pid = wintypes.ULONG()
        getter = kernel.GetNamedPipeServerProcessId
        getter.argtypes, getter.restype = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)], wintypes.BOOL
        if not getter(handle, ctypes.byref(peer_pid)) or peer_pid.value != int(identity.marker.split(b":", 1)[0]):
            raise HerdrTransportError("live_pipe_peer_unproven")
        operation, error = _winapi.WriteFile(handle, request, True)
        if _overlapped(operation, error, deadline, io_deadline) != len(request):
            raise HerdrTransportError("live_request_truncated")
        chunks = bytearray()
        while True:
            _remaining(io_deadline)
            operation, error = _winapi.ReadFile(handle, min(16384, maximum + 1 - len(chunks)), True)
            _overlapped(operation, error, deadline, io_deadline)
            result = _line(chunks, operation.getbuffer(), maximum)
            if result is not None:
                return result
    finally:
        _winapi.CloseHandle(handle)


def request_metadata(locator: str | Path, method: str, request_id: str, *, deadline: float,
                     maximum: int, identity: HerdrEndpointIdentity, host: str = "local", path_namespace: str = "local") -> bytes:
    path = local_herdr_path(locator, host=host, path_namespace=path_namespace)
    raw_locator = os.fspath(locator)
    if method not in ALLOWED_METHODS:
        raise HerdrTransportError("live_method_forbidden")
    request = json.dumps({"id": request_id, "method": method, "params": {}}, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(request) > MAX_REQUEST_BYTES or maximum <= 0:
        raise HerdrTransportError("live_request_invalid")
    try:
        if endpoint_identity(path) != identity:
            raise HerdrTransportError("live_endpoint_changed")
        result = (_windows_request(raw_locator, request, deadline, maximum, identity) if os.name == "nt" else
                  _unix_request(path, request, deadline, maximum))
        if endpoint_identity(path) != identity:
            raise HerdrTransportError("live_endpoint_changed")
        return result
    except HerdrTransportError:
        raise
    except (TimeoutError, socket.timeout):
        raise HerdrTransportError("live_timeout") from None
    except (OSError, HerdrDiscoveryError, ValueError):
        raise HerdrTransportError("live_endpoint_unavailable") from None
