from __future__ import annotations

import gc
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from local_agent_record_janitor.herdr_discovery import local_herdr_path
from local_agent_record_janitor.herdr_live_metadata import decode_response
from local_agent_record_janitor.herdr_runtime import RuntimeBudget, probe_runtime
from local_agent_record_janitor.herdr_transport import HerdrTransportError, endpoint_identity, request_metadata
from tests.herdr_live_support import Endpoint, live_snapshot, pong, response, wire


class HerdrTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hj-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.locator = self.root / "herdr.sock"

    def query(self, locator=None, *, maximum=16384, seconds=0.35):
        locator = locator or self.locator
        return request_metadata(locator, "ping", "ping", deadline=time.monotonic() + seconds,
            maximum=maximum, identity=endpoint_identity(locator))

    def test_real_two_connection_metadata_framing_and_no_mutation_methods(self):
        with Endpoint(self.locator) as endpoint:
            result = probe_runtime(str(self.locator), RuntimeBudget())
            self.assertEqual(len(result.metadata.observations), 2)
            self.assertEqual(result.errors, ())
            self.assertTrue(result.detached_daemon_observed)
            self.assertEqual([r["method"] for r in endpoint.requests], ["ping", "session.snapshot"])
            self.assertEqual(endpoint.connections, 2)
            for r in endpoint.requests:
                self.assertEqual(r["params"], {})
            with self.assertRaisesRegex(HerdrTransportError, "live_method_forbidden"):
                request_metadata(str(self.locator), "server.stop", "no", deadline=time.monotonic() + 1,
                    maximum=1024, identity=endpoint_identity(self.locator))
            self.assertEqual(endpoint.connections, 2)

    def test_successful_ping_survives_failed_snapshot_without_claiming_complete_coverage(self):
        def failed_snapshot(request):
            return pong(request["id"]) if request["method"] == "ping" else {
                "id": request["id"], "error": {"message": "private server details"}}
        with Endpoint(self.locator, failed_snapshot):
            result = probe_runtime(str(self.locator), RuntimeBudget())
        self.assertIsNone(result.metadata)
        row = result.to_dict("default", str(self.locator))
        self.assertTrue(row["server_active"])
        self.assertTrue(row["detached_daemon_observed"])
        self.assertEqual(row["detached_observation_source"], "pong_startup_self_report")
        self.assertFalse(row["probe_complete"])
        self.assertFalse(row["generation_atomic"])
        self.assertEqual(row["errors"], ["live_api_error"])
        self.assertNotIn("private server details", repr(row))

    def test_real_oversize_truncation_and_two_frame_responses_are_bounded(self):
        cases = [(lambda r: b"x" * 100 + b"\n", 20, "live_response_limit_exceeded"),
                 (lambda r: b'{"id":"ping"', 1000, "live_"),
                 (lambda r: wire(pong(r["id"])) * 2, 1000, "live_framing_invalid")]
        for responder, maximum, error in cases:
            with self.subTest(error=error), Endpoint(self.locator, responder):
                with self.assertRaisesRegex(HerdrTransportError, error):
                    self.query(maximum=maximum)

    def test_real_stalled_reader_cancels_without_reader_threads_or_handle_growth(self):
        def stalled(request):
            time.sleep(0.18)
            return pong(request["id"])
        handles = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes as w
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = w.HANDLE
            kernel.GetProcessHandleCount.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
            def count():
                value = w.DWORD()
                self.assertTrue(kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value)))
                return value.value
            gc.collect(); handles = count()
        for _ in range(6):
            with Endpoint(self.locator, stalled):
                started = time.monotonic()
                with self.assertRaisesRegex(HerdrTransportError, "live_timeout"):
                    self.query(seconds=0.12)
                self.assertLess(time.monotonic() - started, 0.25)
        if handles is not None:
            from local_agent_record_janitor.herdr_transport import _retiring
            gc.collect()
            self.assertEqual(_retiring, [])
            self.assertEqual(count(), handles)

    def test_profile_deadline_and_byte_budget_stop_unqueried_endpoints(self):
        with Endpoint(self.locator) as endpoint:
            result = probe_runtime(str(self.locator), RuntimeBudget(deadline=time.monotonic() - 1))
            self.assertEqual(result.errors, ("live_profile_budget_exhausted",))
            result = probe_runtime(str(self.locator), RuntimeBudget(remaining_bytes=1))
            self.assertEqual(result.errors, ("live_profile_budget_exhausted",))
            self.assertEqual(endpoint.connections, 0)

    def test_host_namespace_and_foreign_os_are_rejected_before_local_syscalls(self):
        foreign = "/home/foreign/herdr.sock" if os.name == "nt" else "C:\\foreign\\herdr.sock"
        with patch("pathlib.Path.lstat", side_effect=AssertionError("local syscall")), patch("socket.socket", side_effect=AssertionError("transport")):
            for kwargs in ({"host": "ssh:remote"}, {"path_namespace": "wsl"}):
                with self.assertRaises(ValueError):
                    endpoint_identity(str(self.locator), **kwargs)
            with self.assertRaises(ValueError):
                local_herdr_path(foreign)

    @unittest.skipUnless(os.name == "nt", "Raw pipe namespace is Windows-specific")
    def test_raw_forward_slashes_reach_createfile_verbatim_and_real_endpoint(self):
        import _winapi
        raw = str(self.locator).replace("\\", "/")
        original = _winapi.CreateFile
        names = []
        def create(name, *args):
            names.append(name)
            return original(name, *args)
        with Endpoint(raw), patch("_winapi.CreateFile", create):
            self.assertEqual(decode_response(self.query(raw), "ping")["type"], "pong")
        self.assertEqual(names, ["\\\\.\\pipe\\" + raw])

    @unittest.skipUnless(os.name == "nt", "Windows marker and peer PID validation")
    def test_peer_pid_and_marker_drift_never_publish_snapshot_evidence(self):
        with Endpoint(self.locator) as endpoint:
            self.locator.write_text(f"{os.getpid() + 1}:1", encoding="ascii")
            with self.assertRaisesRegex(HerdrTransportError, "live_pipe_peer_unproven"):
                self.query()
            self.assertEqual(endpoint.requests, [])
        def changed(request):
            self.locator.write_text(f"{os.getpid()}:2", encoding="ascii")
            return pong(request["id"])
        with Endpoint(self.locator, changed):
            with self.assertRaisesRegex(HerdrTransportError, "live_endpoint_changed"):
                self.query()

    @unittest.skipUnless(os.name == "nt", "Windows OVERLAPPED retirement")
    def test_signalled_deferred_cancellation_is_finalized_before_retirement(self):
        import _winapi
        from local_agent_record_janitor.herdr_transport import _reap_cancelled, _retiring
        class Pending:
            event = 1
            completed = False
            result = None
            def GetOverlappedResult(self, wait):
                self.completed = True
                if self.result is None:
                    raise OSError("cancelled handle already closed")
                return 0, self.result
        self.addCleanup(_retiring.clear)
        for result in (None, 0, 995, 996):
            with self.subTest(result=result):
                operation = Pending()
                operation.result = result
                self.assertEqual(_retiring, [])
                _retiring.append(operation)
                with patch("_winapi.WaitForSingleObject", return_value=_winapi.WAIT_OBJECT_0):
                    if result == 996:
                        with self.assertRaisesRegex(HerdrTransportError, "live_pipe_cancellation_unconfirmed"):
                            _reap_cancelled()
                        self.assertEqual(_retiring, [operation])
                    else:
                        _reap_cancelled()
                        self.assertEqual(_retiring, [])
                self.assertTrue(operation.completed)
                _retiring.clear()

    def test_version_change_and_unknown_base_never_upgrade_complete_runtime_evidence(self):
        def changed(request):
            return pong(request["id"]) if request["method"] == "ping" else live_snapshot(request["id"], version="0.9.4")
        with Endpoint(self.locator, changed):
            result = probe_runtime(str(self.locator), RuntimeBudget())
            self.assertIsNone(result.metadata)
            self.assertEqual(result.errors, ("live_version_changed",))
        def future(request):
            return pong(request["id"], version="99.0.0") if request["method"] == "ping" else live_snapshot(request["id"], version="99.0.0")
        with Endpoint(self.locator, future):
            result = probe_runtime(str(self.locator), RuntimeBudget())
            self.assertEqual(len(result.metadata.observations), 2)
            self.assertIn("live_version_unverified", result.errors)
