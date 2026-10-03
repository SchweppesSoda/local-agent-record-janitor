from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import subprocess
import time
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from local_agent_record_janitor.codex_app_server import CodexAppServer
from local_agent_record_janitor.orca_runtime import (JobCodexAppServer, isolated_environment,
    _WindowsJob, runtime_host_identity, runtime_instance_stopped)


class IsolatedCodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.home = self.root / "codex-home"
        self.home.mkdir()

    def protocol_script(self, *, descendant=False):
        path = self.root / "synthetic-protocol.py"
        path.write_text(
            "import json, os, pathlib, subprocess, sys\n"
            + ("child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'])\n"
               "pathlib.Path(os.environ['CHILD_PID_PATH']).write_text(str(child.pid))\n" if descendant else "")
            + "for line in sys.stdin:\n"
              "    message = json.loads(line)\n"
              "    if 'id' not in message: continue\n"
              "    result = {'environment': dict(os.environ), 'cwd': os.getcwd()} if message['method'] == 'inspect-isolation' else {}\n"
              "    print(json.dumps({'id': message['id'], 'result': result}), flush=True)\n", encoding="utf-8")
        return path

    def test_explicit_empty_environment_and_cwd_do_not_inherit_credentials_or_config(self):
        script = self.protocol_script()
        environment = {"JANITOR_EXPLICIT": "safe"}
        server = CodexAppServer(codex_home=self.home, command=[sys.executable, "-I", str(script)],
                                environment=environment, working_directory=self.root, timeout=10)
        environment["LATE_MUTATION"] = "must not leak"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "PRIVATE_TEST_SENTINEL", "CODEX_SQLITE_HOME": "outside"}):
            with server:
                result = server.request("inspect-isolation", {})
        self.assertEqual(result["environment"].get("JANITOR_EXPLICIT"), "safe")
        self.assertEqual(result["environment"]["CODEX_HOME"], str(self.home))
        for name in ("OPENAI_API_KEY", "CODEX_SQLITE_HOME", "LATE_MUTATION"):
            self.assertNotIn(name, result["environment"])
        self.assertEqual(Path(result["cwd"]), self.root)
        with CodexAppServer(codex_home=self.home, command=[sys.executable, "-I", str(script)], environment={}, timeout=10) as empty:
            self.assertNotIn("JANITOR_EXPLICIT", empty.request("inspect-isolation", {})["environment"])

    def test_runtime_allowlist_isolates_all_homes_git_credentials_and_configuration(self):
        with patch("local_agent_record_janitor.orca_runtime._windows_directory", return_value=self.root), patch.dict(
                os.environ, {"OPENAI_API_KEY": "PRIVATE_TEST_SENTINEL", "SSH_AUTH_SOCK": "outside",
                             "GIT_CONFIG_COUNT": "99", "CODEX_SQLITE_HOME": "outside"}):
            environment, cwd = isolated_environment(self.root, self.home)
        for name in ("OPENAI_API_KEY", "SSH_AUTH_SOCK", "CODEX_SQLITE_HOME"):
            self.assertNotIn(name, environment)
        for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                     "XDG_STATE_HOME", "XDG_CACHE_HOME", "TMP", "TEMP", "PROGRAMDATA", "ALLUSERSPROFILE"):
            self.assertTrue(Path(environment[name]).is_relative_to(self.root))
        self.assertEqual(cwd, self.root / "cwd")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(Path(environment["GIT_CONFIG_GLOBAL"]).read_bytes(), b"")

    @unittest.skipUnless(os.name == "nt", "Binary acceptance wrapper uses Windows isolation")
    def test_acceptance_wrapper_enters_fresh_isolated_worker_before_discovery(self):
        from tests.orca_binary_acceptance import run_isolated
        wrapper = self.root / "wrapper"
        wrapper.mkdir()
        with patch("tests.orca_binary_acceptance.tempfile.mkdtemp", return_value=str(wrapper)), patch.dict(
                os.environ, {"OPENAI_API_KEY": "PRIVATE_TEST_SENTINEL", "CODEX_SQLITE_HOME": "outside"}), patch(
                "tests.orca_binary_acceptance.subprocess.run", return_value=SimpleNamespace(
                    returncode=0, stdout="TEMP/acceptance-result.json\n", stderr="")) as child:
            self.assertEqual(run_isolated(self.root / "never-launched.exe"), "TEMP/acceptance-result.json")
        positional, arguments = child.call_args
        self.assertEqual(positional[0][:3], [sys.executable, "-I", "-B"])
        environment = arguments["env"]
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("CODEX_SQLITE_HOME", environment)
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "TEMP", "TMP"):
            self.assertTrue(Path(environment[key]).is_relative_to(wrapper))
        self.assertTrue(arguments["cwd"].is_relative_to(wrapper))
        self.assertEqual(arguments["creationflags"], subprocess.CREATE_NO_WINDOW)

    @unittest.skipUnless(os.name == "nt", "Windows job behavior requires Windows")
    def test_job_launcher_contains_unbranded_descendants_before_protocol_start(self):
        import local_agent_record_janitor.orca_runtime_launcher as launcher
        from ctypes import wintypes

        script = self.protocol_script(descendant=True)
        pid_path = self.root / "child.pid"
        environment, cwd = isolated_environment(self.root, self.home)
        environment["CHILD_PID_PATH"] = str(pid_path)
        server = JobCodexAppServer(codex_home=self.home, command=[sys.executable, "-I", launcher.__file__,
            sys.executable, "-I", str(script)], environment=environment, working_directory=cwd, timeout=10)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = None
        try:
            with server:
                child_pid = int(pid_path.read_text())
                handle = kernel.OpenProcess(0x00100000, False, child_pid)  # SYNCHRONIZE
                self.assertTrue(handle)
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0x102)
                self.assertEqual(Path(server.request("inspect-isolation", {})["cwd"]), cwd)
            self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
        finally:
            server.close()
            if handle:
                kernel.CloseHandle(handle)

    @unittest.skipUnless(os.name == "nt", "Windows job behavior requires Windows")
    def test_empty_existing_job_is_not_cold_recovery_teardown_proof(self):
        job = _WindowsJob("Local\\larj-orca-" + uuid.uuid4().hex)
        instance = {"schema_version": "larj.orca-runtime-instance.v1", "job_name": job.name,
                    **runtime_host_identity()}
        try:
            self.assertFalse(runtime_instance_stopped(instance))
        finally:
            job.close()
        self.assertTrue(runtime_instance_stopped(instance))
        with self.assertRaises(ValueError):
            runtime_instance_stopped({**instance, "session_id": instance["session_id"] + 1})
        for field, invalid in (("session_id", True), ("session_id", -1), ("machine_id_sha256", "x" * 64),
                               ("job_name", "Local\\larj-orca-" + "z" * 32)):
            with self.assertRaises(ValueError):
                runtime_instance_stopped({**instance, field: invalid})

    @unittest.skipUnless(os.name == "nt", "Windows parent crash behavior requires Windows")
    def test_parent_crash_terminates_launcher_server_and_unbranded_child(self):
        from ctypes import wintypes
        import local_agent_record_janitor.orca_runtime_launcher as launcher
        script = self.protocol_script(descendant=True)
        ready = self.root / "ready.json"
        pid_path = self.root / "child.pid"
        worker = self.root / "worker.py"
        source = Path(__file__).resolve().parents[1] / "src"
        worker.write_text("import ctypes,json,pathlib,sys,time\nfrom ctypes import wintypes as W\n"
            + "sys.path.insert(0," + repr(str(source)) + ")\n"
            + "from local_agent_record_janitor.orca_runtime import JobCodexAppServer\n"
            + "server=JobCodexAppServer(codex_home=pathlib.Path(sys.argv[1]), command=[sys.executable,'-I',sys.argv[2],sys.executable,'-I',sys.argv[3]],environment=dict(__import__('os').environ), timeout=10)\n"
            + "server.start()\n"
            + "class Pids(ctypes.Structure):\n _fields_=[('assigned',W.DWORD),('listed',W.DWORD),('ids',ctypes.c_size_t*128)]\n"
            + "values=Pids()\njob=server._job\n"
            + "if not job.kernel.QueryInformationJobObject(job.handle,3,ctypes.byref(values),ctypes.sizeof(values),None):raise OSError('job PID query failed')\n"
            + "pathlib.Path(sys.argv[4]).write_text(json.dumps(list(values.ids[:values.listed])))\n"
            + "time.sleep(120)\n", encoding="utf-8")
        environment, cwd = isolated_environment(self.root, self.home)
        environment["CHILD_PID_PATH"] = str(pid_path)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handles = []
        with subprocess.Popen([sys.executable, "-I", str(worker), str(self.home), launcher.__file__, str(script), str(ready)],
                env=environment, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW) as parent:
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "contained synthetic parent failed to initialize")
                for pid in json.loads(ready.read_text()):
                    handle = kernel.OpenProcess(0x00100000, False, pid)
                    self.assertTrue(handle)
                    handles.append(handle)
                self.assertGreaterEqual(len(handles), 3)
                parent.kill()
                parent.wait(timeout=5)
                for handle in handles:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                for handle in handles:
                    kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
