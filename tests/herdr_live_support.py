"""Temporary local endpoints and synthetic protocol22 metadata, never Herdr."""

from __future__ import annotations

import ctypes
import copy
import json
import os
from pathlib import Path
import socket
import threading
import time

from tests.herdr_support import CODEX_ID, SENTINEL, agent_session


def pong(request_id, *, version="0.9.3"):
    return {"id": request_id, "result": {"type": "pong", "version": version, "protocol": 22,
        "capabilities": {"detached_server_daemon": True}}}


def live_snapshot(request_id, *, sessions=(CODEX_ID, CODEX_ID), version="0.9.3"):
    value = {"version": version, "protocol": 22, "workspaces": [], "tabs": [], "panes": [], "agents": [], "layouts": []}
    if sessions:
        value["workspaces"] = [{"workspace_id": "w1", "number": 1, "label": SENTINEL, "focused": True,
            "pane_count": len(sessions), "tab_count": len(sessions), "active_tab_id": "w1:t1", "agent_status": "working"}]
        value.update(focused_workspace_id="w1", focused_tab_id="w1:t1", focused_pane_id="w1:p1")
    for number, identifier in enumerate(sessions, 1):
        tid, pid = f"w1:t{number}", f"w1:p{number}"
        row = {"pane_id": pid, "terminal_id": f"00000000-0000-4000-8000-{number:012x}",
            "workspace_id": "w1", "tab_id": tid, "focused": number == 1, "agent_status": "working",
            "revision": 1, "agent_session": agent_session(value=identifier), "title": SENTINEL,
            "restore_error": SENTINEL, "tokens": {"label": SENTINEL}}
        value["panes"].append(row)
        value["agents"].append({**copy.deepcopy(row), "state_change_seq": 1, "name": SENTINEL})
        value["tabs"].append({"tab_id": tid, "workspace_id": "w1", "number": number, "label": SENTINEL,
            "focused": number == 1, "pane_count": 1, "agent_status": "working"})
        rect = {"x": 0, "y": 0, "width": 80, "height": 24}
        value["layouts"].append({"workspace_id": "w1", "tab_id": tid, "zoomed": False, "area": rect,
            "focused_pane_id": pid, "panes": [{"pane_id": pid, "focused": True, "rect": rect}], "splits": []})
    return {"id": request_id, "result": {"type": "session_snapshot", "snapshot": value}}


def response(request):
    return pong(request["id"]) if request["method"] == "ping" else live_snapshot(request["id"])


def wire(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


class Endpoint:
    """A real temporary socket/pipe, polling only in the test server thread."""
    def __init__(self, locator, responder=response):
        self.raw_locator = os.fspath(locator)
        self.path = Path(locator)
        self.responder = responder
        self.requests, self.violations = [], []
        self.connections = 0
        self.ready, self.stop = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._serve, name="herdr-temp-endpoint")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            self.path.write_text(f"{os.getpid()}:{time.time_ns()}", encoding="ascii")
        self.thread.start()
        if not self.ready.wait(2):
            self.stop.set(); self.thread.join(2)
            raise AssertionError("Temporary endpoint did not become ready")
        if self.violations:
            raise AssertionError(self.violations)
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError("Temporary endpoint thread survived cleanup")
        self.path.unlink(missing_ok=True)
        if self.violations:
            raise AssertionError(self.violations)

    def _reply(self, data):
        request = json.loads(data)
        if (set(request) != {"id", "method", "params"} or request["params"] != {}
                or request["method"] not in {"ping", "session.snapshot"}):
            raise AssertionError("Non-metadata request")
        self.requests.append(request)
        result = self.responder(request)
        return result if isinstance(result, bytes) else wire(result)

    def _serve(self):
        try:
            self._windows() if os.name == "nt" else self._unix()
        except Exception as exc:
            self.violations.append(type(exc).__name__)
            self.ready.set()

    def _unix(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(self.raw_locator)
            listener.listen(4); listener.settimeout(0.02)
            self.ready.set()
            while not self.stop.is_set():
                try:
                    stream, _ = listener.accept()
                except TimeoutError:
                    continue
                with stream:
                    self.connections += 1
                    stream.settimeout(0.02)
                    data = bytearray()
                    while b"\n" not in data and not self.stop.is_set():
                        try:
                            chunk = stream.recv(512)
                        except TimeoutError:
                            continue
                        if not chunk:
                            break
                        data.extend(chunk)
                    if data:
                        result = self._reply(data)
                        try:
                            stream.sendall(result)
                        except (OSError, TimeoutError):
                            pass  # Client deliberately timed out or hit its cap.

    def _windows(self):
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateNamedPipeW
        create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.LPVOID]
        create.restype = w.HANDLE
        connect = kernel.ConnectNamedPipe
        connect.argtypes, connect.restype = [w.HANDLE, w.LPVOID], w.BOOL
        read, write, close = kernel.ReadFile, kernel.WriteFile, kernel.CloseHandle
        for operation in (read, write):
            operation.argtypes, operation.restype = [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID], w.BOOL
        close.argtypes, close.restype = [w.HANDLE], w.BOOL
        while not self.stop.is_set():
            handle = create("\\\\.\\pipe\\" + self.raw_locator, 3, 1, 1, 65536, 65536, 0, None)
            if handle == ctypes.c_void_p(-1).value:
                raise OSError(ctypes.get_last_error())
            try:
                self.ready.set()
                while not self.stop.is_set():
                    if connect(handle, None) or ctypes.get_last_error() == 535:
                        break
                    if ctypes.get_last_error() != 536:
                        raise OSError(ctypes.get_last_error())
                    time.sleep(0.002)
                if self.stop.is_set():
                    break
                self.connections += 1
                data, count = bytearray(), w.DWORD()
                while b"\n" not in data and not self.stop.is_set():
                    buffer = ctypes.create_string_buffer(512)
                    if read(handle, buffer, 512, ctypes.byref(count), None):
                        data.extend(buffer.raw[:count.value])
                    elif ctypes.get_last_error() in {232, 109}:
                        if ctypes.get_last_error() == 109:
                            break
                        time.sleep(0.002)
                    else:
                        raise OSError(ctypes.get_last_error())
                if data:
                    result = self._reply(data)
                    offset = 0
                    while offset < len(result) and not self.stop.is_set():
                        chunk = ctypes.create_string_buffer(result[offset:offset + 16384])
                        if not write(handle, chunk, len(chunk) - 1, ctypes.byref(count), None):
                            if ctypes.get_last_error() in {109, 232, 233}:
                                break
                            raise OSError(ctypes.get_last_error())
                        if count.value == 0:
                            time.sleep(0.002)
                        offset += count.value
            finally:
                close(handle)
