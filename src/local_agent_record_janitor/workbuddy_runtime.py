"""Bounded, metadata-only writer checks for the independent WorkBuddy store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess


class WorkBuddyRuntimeError(RuntimeError):
    kind = "workbuddy_writer_coverage_unknown"


_WRITER_NAMES = {
    "workbuddy.exe", "workbuddy-cli.exe", "editor-sdk-supervisor.exe", "workbuddy-editor-sdk-supervisor.exe",
}
_VENDOR_NAMES = {"sandbox-center.exe", "sandbox-cli.exe", "sandbox-cli-gc.exe", "editor_sdk.exe", "genie.exe", "genie-cli.exe"}
_RUNTIME_NAMES = {"node.exe", "bun.exe", "deno.exe", "python.exe", "pythonw.exe"}
_INSTALL_COMPONENT = re.compile(r"(?:^|[/\\])workbuddy(?:[/\\]|$)", re.IGNORECASE)
_SHELL_NAMES = {"powershell.exe", "pwsh.exe", "cmd.exe", "conhost.exe"}


def _processes(root, *, timeout=8.0):
    if os.name != "nt":
        raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: Windows process inspection is required")
    standard = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    powershell = str(standard) if standard.is_file() else shutil.which("powershell.exe")
    if not powershell:
        raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: process inspection runtime is unavailable")
    # Candidate command lines stay private to this bounded probe. They are
    # never returned as inventory, exception text, or operation evidence.
    command = (
        "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$names=@('workbuddy.exe','workbuddy-cli.exe','editor-sdk-supervisor.exe','workbuddy-editor-sdk-supervisor.exe',"
        "'sandbox-center.exe','sandbox-cli.exe','sandbox-cli-gc.exe','editor_sdk.exe','genie.exe','genie-cli.exe','node.exe','bun.exe','deno.exe','python.exe','pythonw.exe'); "
        "$all=@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine); "
        "if ($all.Count -gt 10000) { throw 'Process inventory exceeds bound' }; "
        "$ids=[Collections.Generic.HashSet[uint32]]::new(); "
        "$candidates=@($all | Where-Object { "
        f"$_.ProcessId -ne $PID -and $_.ProcessId -ne {os.getpid()} -and ("
        "$names -contains $_.Name.ToLowerInvariant() -or "
        "([string]$_.ExecutablePath+' '+[string]$_.CommandLine).ToLowerInvariant().Contains('workbuddy') -or "
        "([string]$_.ExecutablePath+' '+[string]$_.CommandLine).ToLowerInvariant().Contains('editor-sdk-supervisor') -or "
        "([string]$_.ExecutablePath+' '+[string]$_.CommandLine).ToLowerInvariant().Contains($env:LARJ_WORKBUDDY_PROBE_ROOT.ToLowerInvariant())) }); "
        "foreach ($item in $candidates) { [void]$ids.Add([uint32]$item.ProcessId) }; "
        "$changed=$true; while ($changed) { $changed=$false; foreach ($item in $all) { "
        f"if ($item.ProcessId -ne $PID -and $item.ProcessId -ne {os.getpid()} -and "
        "$ids.Contains([uint32]$item.ParentProcessId) -and $ids.Add([uint32]$item.ProcessId)) { $changed=$true } } }; "
        "$items=@($all | Where-Object { $ids.Contains([uint32]$_.ProcessId) }); "
        "ConvertTo-Json -InputObject $items -Compress -Depth 3"
    )
    try:
        result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="strict",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
            env={**os.environ, "LARJ_WORKBUDDY_PROBE_ROOT": str(root)})
        if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
            raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: process inspection failed")
        rows = json.loads(result.stdout or "[]")
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
        raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: process inspection failed") from exc
    if not isinstance(rows, list) or len(rows) > 10000:
        raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: invalid process inventory")
    return rows


def probe(root: Path, *, collector=None):
    from .workbuddy_store import entries, plain_path, _strict_json
    rows = collector() if collector else _processes(root)
    registry = root / "sessions"
    registered = set()
    try:
        paths = entries(registry, optional=True)
        for path in paths:
            if path.suffix != ".json":
                raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: unknown runtime registry entry")
            plain_path(path, root, regular=True)
            if path.stat().st_size > 128 * 1024:
                raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: oversized runtime registry")
            value = _strict_json(path.read_bytes())
            pid = value.get("pid") if isinstance(value, dict) else None
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: invalid runtime registry")
            registered.add(pid)
    except (OSError, ValueError) as exc:
        raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: runtime registry is unreadable") from exc
    safe, related = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: invalid process metadata")
        pid, parent = row.get("ProcessId"), row.get("ParentProcessId")
        name = str(row.get("Name") or "").casefold()
        if not isinstance(pid, int) or not isinstance(parent, int) or not name:
            raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: invalid process metadata")
        executable, command = row.get("ExecutablePath"), row.get("CommandLine")
        if name in _RUNTIME_NAMES | _VENDOR_NAMES and (not isinstance(command, str) or not isinstance(executable, str)):
            raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: runtime command ownership is unreadable")
        executable_text = str(executable or "").replace("\\", "/")
        command_text = str(command or "").replace("\\", "/").casefold()
        root_text = str(root).replace("\\", "/").casefold()
        install_owned = bool(_INSTALL_COMPONENT.search(executable_text))
        runtime_owned = name in _RUNTIME_NAMES and (
            bool(_INSTALL_COMPONENT.search(command_text)) or "/workbuddy-host-cli/" in command_text
            or "/editor-sdk-supervisor/" in command_text or "workbuddy-editor-sdk-supervisor" in command_text
            or (root_text in command_text and any(flag in command_text for flag in
                ("--config ", "--config-dir ", "--workbuddy-root ", "workbuddy_config_dir="))))
        # Shell command text can merely mention the tool or profile during an
        # inventory. It does not establish a writer. Real WorkBuddy ancestors
        # and their descendants remain positive evidence below.
        owned = (name in _WRITER_NAMES or pid in registered
                 or (name not in _SHELL_NAMES and (install_owned or runtime_owned)))
        if owned:
            related.add(pid)
        safe.append({"pid": pid, "parent_pid": parent, "name": name,
                     "executable": str(executable) if executable else None, "related": owned})
    changed = True
    while changed:
        changed = False
        for row in safe:
            if row["parent_pid"] in related and row["pid"] not in related:
                related.add(row["pid"])
                row["related"] = True
                changed = True
    # This collector includes every candidate runtime. A live registry PID
    # outside that vocabulary is an unknown writer, not evidence of closure.
    missing = registered - {row["pid"] for row in safe}
    if missing:
        # Stale registries are common; check existence without reading process
        # command lines or sending a message to the application's endpoint.
        if os.name != "nt" or collector is not None:
            raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: registry PID is outside the proven process inventory")
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        for pid in missing:
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                kernel.CloseHandle(handle)
                raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: registry writer is still present")
            if ctypes.get_last_error() != 87:  # ERROR_INVALID_PARAMETER proves a missing PID.
                raise WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown: registry PID existence is unreadable")
    return {"owner_client": "workbuddy", "owner_process_root": str(root), "check_mode": "windows_process_metadata",
            "probe_complete": True, "coverage_complete": True, "clients_closed": not related,
            "coverage": {"scope": "known_workbuddy_desktop_vendor_cli_and_runtime_registry",
                         "remote_writers": "not_probed"},
            "processes": [row for row in safe if row["related"]], "errors": []}


def require_closed(root: Path, inspector=None):
    from .codex_desktop_state import running_related_clients
    if inspector is None or inspector is running_related_clients:
        observation = probe(root)
        if not observation["clients_closed"]:
            raise WorkBuddyRuntimeError("workbuddy_writer_running: close all related WorkBuddy writers")
        return
    # Explicit service injection is the same testing/integration seam as the
    # existing native writers, and is never a CLI bypass flag.
    from .frontend_session_cleanup import _inspect_owner_process
    if tuple(_inspect_owner_process(inspector, root, owner_client="workbuddy")):
        raise WorkBuddyRuntimeError("workbuddy_writer_running: close all related WorkBuddy writers")
