"""Fixed isolated invocation validated through the public Orca v3 operation."""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .codex_app_server import CodexAppServer
from .orca_discovery import require_plain_directory, require_plain_file

RUNTIME_POLICY_SCHEMA = "larj.orca-runtime.v1"
PINNED_BINARY_SHA256 = "7d4588265a55adb1403f85d2e058b11dc971459842de84876c86ff02fb7771f2"
# The exact Windows binary/policy passed fresh public v3 plan, cold apply and
# cold verify in owned TEMP. Static brand capabilities remain closed.
RUNTIME_ACCEPTED = True
CONFIG_OVERRIDES = (
    'model="janitor-metadata-local"', 'model_provider="janitor_local"',
    'chatgpt_base_url="http://127.0.0.1:1/"', 'check_for_update_on_startup=false',
    'cli_auth_credentials_store="file"', 'project_doc_max_bytes=0', 'web_search="disabled"',
    'model_providers.janitor_local={name="Janitor isolation local metadata",base_url="http://127.0.0.1:1/v1",wire_api="responses",requires_openai_auth=false}',
    'analytics.enabled=false', 'feedback.enabled=false', 'otel.exporter="none"',
    'otel.trace_exporter="none"', 'otel.log_user_prompt=false',
)
DISABLED_FEATURES = ("plugins", "remote_plugin", "apps", "hooks", "skill_mcp_dependency_install", "skill_search",
                     "in_app_updates", "daemon_auto_start", "system_proxy_fallback", "code_mode_host")


def invocation_policy() -> dict:
    """Bind the actual fixed invocation and trusted implementation bytes."""
    files = (Path(__file__), Path(__file__).with_name("orca_runtime_launcher.py"),
             Path(__file__).with_name("codex_app_server.py"), Path(__file__).with_name("orca_codex_schema.py"),
             Path(__file__).with_name("orca_target_safety.py"), Path(sys.executable).absolute())
    return {"schema_version": RUNTIME_POLICY_SCHEMA, "binary_sha256": PINNED_BINARY_SHA256,
            "config_overrides": list(CONFIG_OVERRIDES), "disabled_features": list(DISABLED_FEATURES),
            "enabled_features": ["skip_host_skill_discovery"], "strict_config": True,
            "environment_policy": "complete_allowlist_isolated_homes_git_configs_loopback_proxy.v1",
            "cwd_policy": "fresh_owned_temp_empty_cwd", "job_policy": "parent_owned_named_job_zero_active.v1",
            "python_version": list(sys.version_info[:3]),
            "implementation": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                               for path in files]}


def runtime_host_identity() -> dict:
    if os.name != "nt":
        raise ValueError("Orca runtime requires Windows")
    import winreg
    from ctypes import wintypes
    session = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                       winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        guid = winreg.QueryValueEx(key, "MachineGuid")[0]
    if not isinstance(guid, str) or not guid:
        raise ValueError("Orca runtime machine identity unavailable")
    return {"machine_id_sha256": hashlib.sha256(guid.encode()).hexdigest(), "session_id": session.value}


def runtime_instance_stopped(value) -> bool:
    """Query only the exact durable job instance, without restarting anything."""
    if (os.name != "nt" or not isinstance(value, dict) or value.get("schema_version") != "larj.orca-runtime-instance.v1"
            or type(value.get("session_id")) is not int or value["session_id"] < 0
            or not isinstance(value.get("machine_id_sha256"), str) or len(value["machine_id_sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in value["machine_id_sha256"])
            or not isinstance(value.get("job_name"), str) or not value["job_name"].startswith("Local\\larj-orca-")
            or len(value["job_name"]) != len("Local\\larj-orca-") + 32
            or any(char not in "0123456789abcdef" for char in value["job_name"][-32:])
            or any(value.get(key) != expected for key, expected in runtime_host_identity().items())):
        raise ValueError("Orca runtime instance evidence is unavailable")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenJobObjectW(4, False, value["job_name"])
    if not handle:
        if ctypes.get_last_error() == 2:
            return True  # No handle/member can retain this named job object.
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # Even a currently empty existing job may be awaiting launcher join.
        # Only absence on this same machine/session proves cold teardown.
        return False
    finally:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


class _WindowsJob:
    """The parent retains a job handle until every known member has exited."""

    def __init__(self, name: str):
        from ctypes import wintypes
        from .orca_runtime_launcher import _ExtendedLimit

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        self.kernel.SetHandleInformation.restype = wintypes.BOOL
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateJobObject.restype = wintypes.BOOL
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.name = name
        self.handle = self.kernel.CreateJobObjectW(None, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.kernel.CloseHandle(self.handle)
            raise ValueError("Orca runtime job name was already used")
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if (not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not self.kernel.SetHandleInformation(self.handle, 1, 1)):
            self.kernel.CloseHandle(self.handle)
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        try:
            if not self.kernel.TerminateJobObject(handle, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            deadline = time.monotonic() + 5
            while True:
                # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: four LARGE_INTEGERs
                # followed by four DWORDs; ActiveProcesses is DWORD index 2.
                accounting = (ctypes.c_uint64 * 6)()
                if not self.kernel.QueryInformationJobObject(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                active = (accounting[5] & 0xFFFFFFFF)
                if active == 0:
                    break
                if time.monotonic() >= deadline:
                    raise ValueError("Orca runtime job still has active descendants")
                time.sleep(0.025)
        finally:
            if not self.kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


class JobCodexAppServer(CodexAppServer):
    def __init__(self, *, job_ready_callback=None, **kwargs):
        super().__init__(**kwargs)
        self.job_ready_callback = job_ready_callback

    def start(self):
        if self._process is not None:
            return
        if self.environment is None:
            raise ValueError("Job app-server requires an explicit child environment")
        self._job = _WindowsJob("Local\\larj-orca-" + uuid.uuid4().hex)
        self.environment["LARJ_JOB_HANDLE"] = str(int(self._job.handle))
        try:
            if self.job_ready_callback is not None:
                # Called before even the launcher can start. A production
                # caller durably records this exact instance and startup.
                self.job_ready_callback(self._job.name)
            super().start()
        except BaseException:
            self._job.close()
            raise

    def _popen_options(self):
        startup = __import__("subprocess").STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [int(self._job.handle)]}
        return {**super()._popen_options(), "startupinfo": startup, "close_fds": True}

    def close(self):
        try:
            super().close()
        finally:
            if getattr(self, "_job", None) is not None:
                self._job.close()


def _windows_directory() -> Path:
    if os.name != "nt":
        raise ValueError("Orca runtime requires Windows")
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
        raise ValueError("Windows directory identity unavailable")
    path = Path(buffer.value)
    require_plain_directory(path)
    return path


def isolated_environment(root: Path, codex_home: Path) -> tuple[dict[str, str], Path]:
    """A complete allowlist, including Git system/global/credential isolation."""
    root = root.absolute()
    require_plain_directory(root)
    require_plain_directory(codex_home)
    windows = _windows_directory()
    environment = {"SystemRoot": str(windows), "WINDIR": str(windows),
                   "SystemDrive": windows.drive,
                   "ComSpec": str(windows / "System32" / "cmd.exe"),
                   "PATH": str(windows / "System32"), "PATHEXT": ".EXE;.COM"}
    for name, directory in (("HOME", "home"), ("USERPROFILE", "userprofile"), ("APPDATA", "appdata"),
                            ("LOCALAPPDATA", "localappdata"), ("XDG_CONFIG_HOME", "xdg-config"),
                            ("XDG_DATA_HOME", "xdg-data"), ("XDG_STATE_HOME", "xdg-state"),
                            ("XDG_CACHE_HOME", "xdg-cache"), ("TMP", "tmp"), ("TEMP", "temp"),
                            ("ALLUSERSPROFILE", "allusers"), ("PROGRAMDATA", "programdata")):
        path = root / directory
        path.mkdir(exist_ok=True)
        require_plain_directory(path)
        environment[name] = str(path)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        environment[name] = "http://127.0.0.1:1"
    environment["NO_PROXY"] = "127.0.0.1,localhost,::1"
    environment["CODEX_HOME"] = str(codex_home)
    environment["HOMEDRIVE"] = root.drive
    environment["HOMEPATH"] = str(root / "userprofile")[len(root.drive):]
    git_config = root / "empty-git-config"
    with git_config.open("x", encoding="utf-8") as stream:
        stream.write("")
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": str(git_config),
                        "GIT_CONFIG_GLOBAL": str(git_config), "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
                        "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
                        "GIT_CONFIG_KEY_1": "http.proxy", "GIT_CONFIG_VALUE_1": "http://127.0.0.1:1"})
    cwd = root / "cwd"
    cwd.mkdir(exist_ok=True)
    require_plain_directory(cwd)
    return environment, cwd


class IsolatedCodexRuntime:
    """One owned scratch directory and job-contained app-server invocation."""

    def __init__(self, binary: Path, *, expected_sha256: str = PINNED_BINARY_SHA256, root: Path | None = None) -> None:
        if os.name != "nt":
            raise ValueError("Orca runtime requires Windows")
        self.binary = binary.absolute()
        info = require_plain_file(self.binary)
        if info.st_nlink != 1 or hashlib.sha256(self.binary.read_bytes()).hexdigest() != expected_sha256:
            raise ValueError("Orca runtime binary identity does not match its acceptance pin")
        self.temporary = tempfile.TemporaryDirectory(prefix="janitor-orca-runtime-") if root is None else None
        self.root = Path(self.temporary.name).resolve(strict=True) if self.temporary else root.absolute()
        require_plain_directory(self.root)

    def server(self, *, codex_home: Path, timeout: float = 30.0, job_ready_callback=None) -> CodexAppServer:
        environment, cwd = isolated_environment(self.root, codex_home)
        launcher = Path(__file__).with_name("orca_runtime_launcher.py")
        require_plain_file(launcher)
        command = [sys.executable, "-I", str(launcher), str(self.binary), "app-server", "--stdio", "--strict-config"]
        for override in CONFIG_OVERRIDES:
            command.extend(("-c", override))
        for feature in DISABLED_FEATURES:
            command.extend(("--disable", feature))
        command.extend(("--enable", "skip_host_skill_discovery"))
        return JobCodexAppServer(codex_home=codex_home, codex_binary=self.binary, command=command,
                              timeout=timeout, environment=environment, working_directory=cwd,
                              job_ready_callback=job_ready_callback)

    def close(self) -> None:
        if self.temporary is not None:
            self.temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
