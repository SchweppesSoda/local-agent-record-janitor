from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO


class CodexAppServerError(RuntimeError):
    """Base class for failures while talking to Codex app-server."""


class CodexAppServerStartupError(CodexAppServerError):
    """The app-server process could not be started or initialized."""


class CodexAppServerTimeout(CodexAppServerError):
    """The app-server did not answer before the configured deadline."""


class CodexAppServerProtocolError(CodexAppServerError):
    """The app-server emitted an invalid or unexpected protocol message."""


class CodexAppServerResponseError(CodexAppServerError):
    """The app-server returned a structured error for a request."""

    def __init__(self, method: str, error: object) -> None:
        self.method = method
        self.error = error
        try:
            rendered = json.dumps(error, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            rendered = repr(error)
        super().__init__(f"Codex app-server rejected {method}: {rendered}")


_EOF = object()
_UNSET = object()


def build_app_server_command(codex_binary: Path | str) -> list[str]:
    """Build a shell-free command for an executable or Windows command shim."""

    binary = Path(codex_binary).expanduser()
    arguments = [str(binary), "app-server", "--stdio"]
    if os.name == "nt" and binary.suffix.lower() in {".cmd", ".bat"}:
        # CreateProcess cannot execute a batch shim directly.  Pass one fully
        # quoted command line as cmd.exe's /c argument without shell=True.
        command_processor = os.environ.get("COMSPEC", "cmd.exe")
        command_line = subprocess.list2cmdline(arguments)
        return [command_processor, "/d", "/s", "/c", command_line]
    return arguments


class CodexAppServer:
    """Small synchronous JSONL client for the official Codex app-server."""

    def __init__(
        self,
        *,
        codex_home: Path,
        codex_binary: Path | str | None = None,
        command: Sequence[str] | None = None,
        timeout: float = 30.0,
        client_version: str = "0.1.0",
    ) -> None:
        if command is None and codex_binary is None:
            raise ValueError("codex_binary or command is required")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.codex_home = Path(codex_home).expanduser()
        self.codex_binary = (
            Path(codex_binary).expanduser() if codex_binary is not None else None
        )
        self.command = (
            list(command)
            if command is not None
            else build_app_server_command(self.codex_binary)  # type: ignore[arg-type]
        )
        if not self.command:
            raise ValueError("command must not be empty")
        self.timeout = float(timeout)
        self.client_version = client_version
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[object] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=50)
        self._request_id = 0
        self._initialized = False

    def __enter__(self) -> CodexAppServer:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def start(self) -> None:
        if self._process is not None:
            return
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.codex_home)
        popen_options: dict[str, Any] = {}
        if os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                **popen_options,
            )
        except (OSError, ValueError) as exc:
            self._process = None
            raise CodexAppServerStartupError(
                f"Could not start Codex app-server: {exc}"
            ) from exc

        assert self._process.stdout is not None
        assert self._process.stderr is not None
        threading.Thread(
            target=self._read_stdout,
            args=(self._process.stdout,),
            name="codex-app-server-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(self._process.stderr,),
            name="codex-app-server-stderr",
            daemon=True,
        ).start()

        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "local-agent-record-janitor",
                        "title": "Local Agent Record Janitor",
                        "version": self.client_version,
                    },
                    "capabilities": {},
                },
            )
            # The generated Codex protocol schema defines this notification
            # without params.
            self.notify("initialized")
            self._initialized = True
        except CodexAppServerError as exc:
            stderr = self.stderr_tail
            self.close()
            detail = f"\nCodex stderr:\n{stderr}" if stderr else ""
            raise CodexAppServerStartupError(
                f"Codex app-server initialization failed: {exc}{detail}"
            ) from exc

    def delete_thread(self, thread_id: str) -> Any:
        if not thread_id:
            raise ValueError("thread_id must not be empty")
        if not self._initialized:
            self.start()
        return self.request("thread/delete", {"threadId": thread_id})

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        if self._process is None:
            raise CodexAppServerProtocolError("Codex app-server is not running")
        effective_timeout = self.timeout if timeout is None else float(timeout)
        if effective_timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self._request_id += 1
        request_id = self._request_id
        self._write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + effective_timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerTimeout(
                    f"Timed out after {effective_timeout:g}s waiting for {method}"
                )
            try:
                item = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise CodexAppServerTimeout(
                    f"Timed out after {effective_timeout:g}s waiting for {method}"
                ) from exc
            if item is _EOF:
                exit_code = self._process.poll()
                stderr = self.stderr_tail
                detail = f"; stderr: {stderr}" if stderr else ""
                raise CodexAppServerProtocolError(
                    f"Codex app-server exited"
                    f"{f' with code {exit_code}' if exit_code is not None else ''}"
                    f" while waiting for {method}{detail}"
                )
            assert isinstance(item, str)
            try:
                message = json.loads(item)
            except json.JSONDecodeError as exc:
                raise CodexAppServerProtocolError(
                    f"Codex app-server emitted invalid JSON: {item[:200]!r}"
                ) from exc
            if not isinstance(message, dict):
                raise CodexAppServerProtocolError(
                    "Codex app-server emitted a non-object JSON message"
                )
            # Notifications and server-to-client requests may be interleaved
            # with the response.  No janitor operation requires handling them.
            if message.get("id") != request_id:
                continue
            if message.get("error") is not None:
                raise CodexAppServerResponseError(method, message["error"])
            if "result" not in message:
                raise CodexAppServerProtocolError(
                    f"Codex app-server response to {method} has no result"
                )
            return message["result"]

    def notify(
        self, method: str, params: dict[str, Any] | object = _UNSET
    ) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not _UNSET:
            message["params"] = params
        self._write(message)

    def close(self) -> None:
        process = self._process
        self._process = None
        self._initialized = False
        if process is None:
            return
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.terminate()
            process.wait(timeout=1.0)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerProtocolError("Codex app-server is not running")
        if process.poll() is not None:
            stderr = self.stderr_tail
            detail = f"; stderr: {stderr}" if stderr else ""
            raise CodexAppServerProtocolError(
                f"Codex app-server exited with code {process.returncode}{detail}"
            )
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            process.stdin.write(payload + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexAppServerProtocolError(
                f"Could not write to Codex app-server: {exc}"
            ) from exc

    def _read_stdout(self, stream: TextIO) -> None:
        try:
            for line in stream:
                stripped = line.strip()
                if stripped:
                    self._messages.put(stripped)
        finally:
            self._messages.put(_EOF)

    def _read_stderr(self, stream: TextIO) -> None:
        for line in stream:
            stripped = line.rstrip()
            if stripped:
                self._stderr.append(stripped)


__all__ = [
    "CodexAppServer",
    "CodexAppServerError",
    "CodexAppServerProtocolError",
    "CodexAppServerResponseError",
    "CodexAppServerStartupError",
    "CodexAppServerTimeout",
    "build_app_server_command",
]
