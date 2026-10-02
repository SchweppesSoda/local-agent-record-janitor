from __future__ import annotations

from contextlib import ExitStack
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from local_agent_record_janitor.adapters import HerdrAdapter
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.herdr_discovery import raw_herdr_join
from tests.herdr_live_support import Endpoint, live_snapshot, pong
from tests.herdr_support import CODEX_ID, CLAUDE_ID, SENTINEL, agent_session, pane, tab, snapshot, write_snapshot, recovery_name
from tests.support import create_thread_index, write_rollout


class HerdrLiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hl-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.profile = self.root / "herdr"
        self.profile.mkdir()
        home = patch("pathlib.Path.home", return_value=self.root / "user")
        home.start(); self.addCleanup(home.stop)
        env = patch.dict(os.environ, {"ORCA_USER_DATA_PATH": "", "XDG_CONFIG_HOME": str(self.root / "config"),
            "APPDATA": str(self.root / "appdata"), "HOME": str(self.root / "user"), "CODEX_HOME": str(self.root / "native"),
            "PI_CODING_AGENT_DIR": str(self.root / "pi"), "CLAUDE_CONFIG_DIR": str(self.root / "claude")})
        env.start(); self.addCleanup(env.stop)
        old_override = os.environ.pop("HERDR_SOCKET_PATH", None)
        if old_override is not None:
            self.addCleanup(os.environ.__setitem__, "HERDR_SOCKET_PATH", old_override)
        self.writer = Mock(side_effect=AssertionError("writer reached"))
        self.current = snapshot(self.root, (tab({100: pane(self.root, agent_session()), 101: pane(self.root, agent_session())}),))
        write_snapshot(self.profile / "session.json", self.current)
        write_snapshot(self.profile / "session-backups" / recovery_name(), snapshot(self.root,
            (tab({0: pane(self.root, agent_session())}),)))

    def invoke(self, *extra, client="herdr", root=None):
        args = ["records", "--client", client, "--json"]
        if client == "herdr" and root is not False:
            args += ["--herdr-root", os.fspath(root or self.profile)]
        args += list(extra)
        out = StringIO()
        code = main(args, stdout=out, stderr=StringIO(), app_server_factory=self.writer, binary_resolver=self.writer)
        text = out.getvalue().strip()
        value, end = json.JSONDecoder().raw_decode(text)
        self.assertEqual(text[end:].strip(), "")
        self.assertNotIn(SENTINEL, text)
        self.writer.assert_not_called()
        return code, value

    def test_real_factory_cli_combines_live_current_restore_and_uses_one_cached_observation(self):
        named = self.profile / "sessions" / "work"
        write_snapshot(named / "session.json", snapshot(self.root, (tab({1: pane(self.root, agent_session("claude", CLAUDE_ID))}),)))
        def different(request):
            return pong(request["id"]) if request["method"] == "ping" else live_snapshot(request["id"], sessions=("different-id",))
        with Endpoint(self.profile / "herdr.sock") as default, Endpoint(named / "herdr.sock", different) as work:
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.profile.rglob("*") if p.is_file()}
            with patch("local_agent_record_janitor.codex_desktop_state.inspect_client_ownership", side_effect=AssertionError("wrong inspector")):
                code, result = self.invoke("--inspect-clients")
            after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.profile.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertNotEqual(code, 0)
            self.assertEqual(len(result["targets"]), 7)
            refs = [r for t in result["targets"] for r in t["references"]]
            self.assertEqual(sum(r["lifecycle"] == "live" for r in refs), 3)
            self.assertEqual(sum(r["kind"] == "restore" for r in refs), 1)
            self.assertEqual(len({r["binding_key"] for r in refs}), 7)
            self.assertTrue(all(r.get("native_record") is None for r in refs))
            self.assertTrue(all(not t["action_ids"] and not t["capability"]["verify"] for t in result["targets"]))
            owner = result["client_ownership"][0]
            self.assertFalse(owner["clients_closed"])
            self.assertFalse(owner["coverage_complete"])
            self.assertEqual(owner["coverage"]["scope"], "profile_sessions")
            sessions = {r["session_name"]: r for r in owner["sessions"]}
            self.assertTrue(sessions["default"]["reference_values_match"])
            self.assertFalse(sessions["work"]["reference_values_match"])
            self.assertEqual(sessions["default"]["pane_identity_mapping"], "unproven")
            self.assertNotIn("detached", owner)
            self.assertIn("live_persisted_reference_values_differ", [e["message"] for e in result["store_errors"]])
            self.assertEqual(default.connections, 2)
            self.assertEqual(work.connections, 2)
            expected = "inventory:v1:" + hashlib.sha256(json.dumps(result["targets"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            self.assertEqual(result["snapshot_id"], expected)  # runtime projection is outside this hash.

    def test_explicit_live_scope_keeps_runtime_profile_failure_without_other_profile_contamination(self):
        other = self.root / "other"
        write_snapshot(other / "session.json", snapshot(self.root))
        restore = next(r for r in HerdrAdapter(profile_root=self.profile).snapshot_references().references if r.kind.value == "restore")
        with Endpoint(self.profile / "herdr.sock") as endpoint:
            code, result = self.invoke("--inspect-clients", "--herdr-root", str(other), "--record-id", restore.binding_key)
            self.assertNotEqual(code, 0)
            self.assertEqual(len(result["targets"]), 1)
            self.assertEqual({e["profile_root"] for e in result["store_errors"]}, {str(self.profile)})
            self.assertEqual(len(result["client_ownership"]), 1)
            self.assertEqual(result["client_ownership"][0]["coverage"]["scope"], "profile_sessions")
            self.assertEqual(endpoint.connections, 2)

    def test_absence_override_and_unknown_version_keep_references_without_closed_proof(self):
        code, absent = self.invoke("--inspect-clients")
        self.assertEqual(len(absent["targets"]), 3)
        self.assertIsNone(absent["client_ownership"][0]["clients_closed"])
        self.assertFalse(absent["client_ownership"][0]["probe_complete"])
        with Endpoint(self.profile / "herdr.sock") as endpoint, patch.dict(os.environ, {"HERDR_SOCKET_PATH": SENTINEL}):
            code, override = self.invoke("--inspect-clients")
            self.assertEqual(endpoint.connections, 0)
            self.assertIn("live_socket_override_not_covered", [e["message"] for e in override["store_errors"]])
        def future(request):
            return pong(request["id"], version="99.0.0") if request["method"] == "ping" else live_snapshot(request["id"], version="99.0.0")
        with Endpoint(self.profile / "herdr.sock", future):
            code, result = self.invoke("--inspect-clients")
            self.assertEqual(len(result["targets"]), 5)
            self.assertIn("live_version_unverified", [e["message"] for e in result["store_errors"]])
            self.assertFalse(result["client_ownership"][0]["probe_complete"])
            self.assertFalse(result["client_ownership"][0]["coverage_complete"])

    def test_plain_records_and_native_inspection_never_query_herdr_endpoints(self):
        # Default discovery may retain a Herdr metadata guard, but it is not a
        # selected runtime inspector for a native-store command.
        os.environ["XDG_CONFIG_HOME"] = str(self.root)
        with Endpoint(self.profile / "herdr.sock") as endpoint:
            code, plain = self.invoke()
            self.assertEqual(len(plain["targets"]), 3)
            self.assertEqual(endpoint.connections, 0)
            home = self.root / "native"
            rollout = write_rollout(home, CODEX_ID, originator="codex_cli_rs", source="cli")
            create_thread_index(home, [{"id": CODEX_ID, "rollout_path": str(rollout), "source": "cli"}])
            with patch("local_agent_record_janitor.codex_desktop_state._running_related_process_records", return_value=()):
                code, native = self.invoke("--inspect-clients", "--codex-home", str(home), client="native")
            self.assertEqual(code, 0)
            self.assertEqual(len(native["targets"]), 1)
            self.assertEqual(endpoint.connections, 0)

    def test_real_multi_session_stalls_have_a_single_profile_budget_and_no_empty_success(self):
        def stalled(request):
            time.sleep(0.19)
            return pong(request["id"]) if request["method"] == "ping" else live_snapshot(request["id"])
        with ExitStack() as stack:
            endpoints = [stack.enter_context(Endpoint(self.profile / "herdr.sock", stalled))]
            for i in range(8):
                directory = self.profile / "sessions" / str(i)
                write_snapshot(directory / "session.json", snapshot(self.root))
                endpoints.append(stack.enter_context(Endpoint(directory / "herdr.sock", stalled)))
            start = time.monotonic()
            code, result = self.invoke("--inspect-clients")
            self.assertLess(time.monotonic() - start, 2.5)
            self.assertNotEqual(code, 0)
            self.assertEqual(len(result["targets"]), 3)
            self.assertIn("live_profile_budget_exhausted", [e["message"] for e in result["store_errors"]])
            self.assertTrue(any(endpoint.connections == 0 for endpoint in endpoints))
            self.assertIsNone(result["client_ownership"][0]["clients_closed"])

    @unittest.skipUnless(os.name == "nt", "Windows default raw namespace spelling")
    def test_actual_factory_preserves_raw_explicit_pipe_names(self):
        import _winapi
        original = _winapi.CreateFile
        raw = str(self.profile).replace("\\", "/") + "/"
        endpoint = raw_herdr_join(raw, "herdr.sock")
        names = []
        def create(name, *args):
            names.append(name)
            return original(name, *args)
        with Endpoint(endpoint), patch("_winapi.CreateFile", create):
            code, result = self.invoke("--inspect-clients", root=raw)
        self.assertEqual(names, ["\\\\.\\pipe\\" + endpoint] * 2)
        self.assertEqual(len(result["targets"]), 5)
        self.assertEqual(result["client_ownership"][0]["sessions"][0]["endpoint"], endpoint)

    @unittest.skipUnless(os.name == "nt", "Windows HOME raw namespace join")
    def test_actual_default_home_join_keeps_embedded_slash_and_reaches_that_endpoint(self):
        import _winapi
        for key in ("XDG_CONFIG_HOME", "APPDATA", "USERPROFILE"):
            os.environ.pop(key, None)
        raw_home = str(self.root / "home").replace("\\", "/")
        os.environ["HOME"] = raw_home
        default = raw_herdr_join(raw_home, ".config/herdr")
        write_snapshot(Path(default) / "session.json", self.current)
        endpoint = raw_herdr_join(default, "herdr.sock")
        names, original = [], _winapi.CreateFile
        def create(name, *args):
            names.append(name)
            return original(name, *args)
        with Endpoint(endpoint), patch("_winapi.CreateFile", create):
            code, result = self.invoke("--inspect-clients", root=False)
        self.assertEqual(names, ["\\\\.\\pipe\\" + endpoint] * 2)
        self.assertEqual(len(result["targets"]), 4)
