import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from local_agent_record_janitor import office_runtime as runtime, office_leveldb


class OfficeRuntimeTests(unittest.TestCase):
    def test_known_desktop_workers_helpers_cli_and_descendants_are_detected(self):
        root = Path("C:/synthetic/sdk")
        rows = [
            {"pid": 100001, "parent": 1, "name": "QwenWorkCN Helper (Renderer)",
             "executable": "/Applications/QwenWorkCN.app/Contents/Frameworks/Helper.app/Contents/MacOS/Helper", "command": "helper"},
            {"pid": 100002, "parent": 1, "name": "QoderWorkCN.AppI", "executable": "/opt/QoderWorkCN.AppImage", "command": "desktop"},
            {"pid": 100003, "parent": 1, "name": "qoderclicn", "executable": "/bin/qoderclicn", "command": "qoderclicn --sdk --config-dir C:/synthetic/sdk"},
            {"pid": 100004, "parent": 100003, "name": "helper", "executable": "/bin/helper", "command": "helper"},
            {"pid": 100005, "parent": 1, "name": "powershell.exe", "executable": "C:/Windows/powershell.exe", "command": "read docs mentioning qoder-worker-runtime.mjs"},
        ]
        self.assertEqual(runtime.related(rows, [root]), [100001, 100002, 100003, 100004])
        with self.assertRaisesRegex(runtime.OfficeDatabaseError, "coverage_unknown"):
            runtime.related([{"pid": 100006, "parent": 1, "name": "node.exe", "executable": None, "command": None}], [root])

    def test_real_host_census_finds_a_synthetic_sdk_worker(self):
        try:
            node = office_leveldb._node()
        except office_leveldb.OfficeLevelDBError:
            if os.environ.get("LARJ_REQUIRE_LEVELDB_TESTS") == "1":
                raise
            self.skipTest("Node runtime not visible for synthetic process fixture")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            script = root / "qoder-worker-runtime.mjs"
            script.write_text("process.stdout.write('ready\\n'); setInterval(()=>{},1000)", encoding="utf-8")
            worker = subprocess.Popen([str(node), str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                self.assertEqual(worker.stdout.readline(), b"ready\n")
                rows = [row for row in runtime.processes() if row["pid"] == worker.pid]
                self.assertEqual(runtime.related(rows, [root]), [worker.pid])
            finally:
                worker.terminate()
                worker.wait(timeout=10)
                worker.stdout.close(); worker.stderr.close()

    def test_linux_thread_name_does_not_hide_executable_identity(self):
        row = {"pid": 100007, "parent": 1, "name": "MainThread",
               "executable": "/opt/node/bin/node", "command": "node /tmp/qoder-worker-runtime.mjs"}
        self.assertEqual(runtime.related([row], [Path("/tmp")]), [100007])
        with self.assertRaisesRegex(runtime.OfficeDatabaseError, "coverage_unknown"):
            runtime.related([{**row, "command": None}], [Path("/tmp")])


if __name__ == "__main__":
    unittest.main()
