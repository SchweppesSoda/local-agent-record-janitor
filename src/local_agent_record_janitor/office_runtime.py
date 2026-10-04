"""Private bounded process checks for the two registered Office products."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .office_database import OfficeDatabaseError

_RUNTIMES = {"node", "node.exe", "bun", "bun.exe", "deno", "deno.exe", "electron", "electron.exe"}
_CLI = {"qoderclicn", "qoderclicn.exe", "qodercli", "qodercli.exe"}
_SHELLS = {"powershell.exe", "pwsh.exe", "cmd.exe", "bash", "sh", "zsh"}
_PRODUCT = re.compile(r"(?:qwenwork(?:cn)?|qoderwork(?:cn)?)(?:dev|canary)?(?:\.exe|\.app|\.appimage)?\Z", re.I)
_WORKER = re.compile(r"(?:^|[/\\\s\"'])qoder-worker-runtime(?:\.obf)?\.mjs(?:$|[\s\"'])", re.I)


def _fail():
    raise OfficeDatabaseError("office_writer_coverage_unknown")


def processes():
    deadline = time.monotonic() + 8
    if os.name == "nt":
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "$items=@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine); "
            "if ($items.Count -gt 10000) { throw 'bounded' }; ConvertTo-Json -InputObject $items -Depth 2 -Compress")
        try:
            value = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if value.returncode or len(value.stdout) > 8 * 1024 * 1024:
                _fail()
            rows = json.loads(value.stdout.decode("utf-8-sig"))
            if not isinstance(rows, list) or len(rows) > 10000:
                _fail()
            return [{"pid": row["ProcessId"], "parent": row["ParentProcessId"], "name": row["Name"],
                     "executable": row["ExecutablePath"], "command": row["CommandLine"]} for row in rows]
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            raise OfficeDatabaseError("office_writer_coverage_unknown") from exc
    if sys.platform.startswith("linux"):
        rows = []
        for entry in Path("/proc").iterdir():
            if time.monotonic() > deadline:
                _fail()
            if not entry.name.isdigit():
                continue
            if len(rows) >= 10000:
                _fail()
            try:
                raw = (entry / "stat").read_text()
                end = raw.rfind(")")
                fields = raw[end + 2:].split()
                name = raw[raw.index("(") + 1:end]
                parent = int(fields[1])
                executable = command = None
                try:
                    executable = os.readlink(entry / "exe")
                    with (entry / "cmdline").open("rb") as stream:
                        data = stream.read(128 * 1024 + 1)
                    if len(data) > 128 * 1024:
                        _fail()
                    command = data.replace(b"\0", b" ").decode("utf-8", "strict")
                except PermissionError:
                    pass
                rows.append({"pid": int(entry.name), "parent": parent, "name": name,
                             "executable": executable, "command": command})
            except (FileNotFoundError, ProcessLookupError):
                continue  # Exited during this read; next check takes a new census.
            except (OSError, ValueError, IndexError, UnicodeError) as exc:
                raise OfficeDatabaseError("office_writer_coverage_unknown") from exc
        return rows
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["/bin/ps", "-Aww", "-o", "pid=,ppid=,comm="], capture_output=True, timeout=8)
            if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
                _fail()
            rows = []
            for line in result.stdout.decode().splitlines():
                if time.monotonic() > deadline:
                    _fail()
                pid, parent, executable = line.strip().split(None, 2)
                name = Path(executable).name
                command = ""
                if name.casefold() in _RUNTIMES | _CLI:
                    detail = subprocess.run(["/bin/ps", "-ww", "-p", pid, "-o", "args="], capture_output=True,
                        timeout=max(0.001, min(2, deadline - time.monotonic())))
                    if detail.returncode or len(detail.stdout) > 128 * 1024:
                        _fail()
                    command = detail.stdout.decode().strip()
                rows.append({"pid": int(pid), "parent": int(parent), "name": name,
                             "executable": executable, "command": command})
                if len(rows) > 10000:
                    _fail()
            return rows
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise OfficeDatabaseError("office_writer_coverage_unknown") from exc
    _fail()


def related(rows, roots):
    selected, normalized = set(), []
    root_names = [str(root).replace("\\", "/").casefold() for root in roots]
    for row in rows:
        if not isinstance(row, dict) or type(row.get("pid")) is not int or type(row.get("parent")) is not int:
            _fail()
        name = str(row.get("name") or "").casefold()
        if not name:
            _fail()
        executable, command = row.get("executable"), row.get("command")
        if row["pid"] == os.getpid():
            continue
        if name in _RUNTIMES | _CLI and (not isinstance(executable, str) or not isinstance(command, str)):
            _fail()
        exe = str(executable or "").replace("\\", "/").casefold()
        cmd = str(command or "").replace("\\", "/").casefold()
        # Linux /proc/stat is a mutable, truncated thread name. Node 24 may
        # report MainThread while /proc/exe still proves the Node executable.
        executable_name = exe.rsplit("/", 1)[-1]
        runtime = name in _RUNTIMES | _CLI or executable_name in _RUNTIMES | _CLI
        cli = name in _CLI or executable_name in _CLI
        if runtime and (not isinstance(executable, str) or not isinstance(command, str)):
            _fail()
        parts = [re.sub(r"[ _-]", "", p) for p in exe.split("/")]
        owned = bool(_PRODUCT.fullmatch(re.sub(r"[ _-]", "", name)))
        owned |= name not in _SHELLS and any(_PRODUCT.fullmatch(p) for p in parts)
        owned |= runtime and (bool(_WORKER.search(cmd)) or "/@qoder-ai/qoder-agent-sdk/" in cmd
            or cli and bool(re.search(r"(?:^|\s)--sdk(?:\s|$)", cmd))
            or any(root in cmd for root in root_names) and any(flag in cmd for flag in (
                "qoder_config_dir=", "qodercn_config_dir=", "--config-dir ", "--user-data-dir=")))
        if owned:
            selected.add(row["pid"])
        normalized.append(row)
    changed = True
    while changed:
        before = len(selected)
        selected.update(row["pid"] for row in normalized if row["parent"] in selected)
        changed = len(selected) != before
    return sorted(selected)


def require_closed(client, roots, inspector=None):
    from .codex_desktop_state import running_related_clients
    if inspector is not None and inspector is not running_related_clients:
        from .frontend_session_cleanup import _inspect_owner_process
        if any(tuple(_inspect_owner_process(inspector, root, owner_client=client)) for root in roots):
            raise OfficeDatabaseError("office_writer_running")
        return
    if related(processes(), roots):
        raise OfficeDatabaseError("office_writer_running")
