from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.herdr_discovery import (
    default_herdr_roots, local_herdr_path, valid_recovery_name, valid_session_name,
)
from local_agent_record_janitor.herdr_metadata import parse_herdr_snapshot, read_herdr_snapshot
from tests.herdr_support import CODEX_ID, SENTINEL, agent_session, pane, recovery_name, snapshot, tab, write_snapshot


class HerdrMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.path = self.root / "session.json"

    def test_external_tag_layout_zero_and_every_persisted_pane_are_read(self):
        value = snapshot(self.root, (tab({0: pane(self.root, agent_session()), 1: pane(self.root)}),
                                    tab({2: pane(self.root, agent_session("claude", "claude-id"))})))
        # A pane need not be the currently visible layout leaf to retain a ref.
        value["workspaces"][0]["tabs"][0]["panes"]["3"] = pane(self.root, agent_session())
        observations, errors = parse_herdr_snapshot(value)
        self.assertEqual(errors, ())
        self.assertEqual(len(observations), 3)
        self.assertEqual({o.locator for o in observations}, {
            "workspaces/0/tabs/0/panes/0/agent_session", "workspaces/0/tabs/0/panes/3/agent_session",
            "workspaces/0/tabs/1/panes/2/agent_session"})

    def test_minimal_empty_current_is_valid_but_version_and_required_shape_are_strict(self):
        self.assertEqual(parse_herdr_snapshot(snapshot(self.root)), ((), ()))
        baseline = snapshot(self.root, (tab({0: pane(self.root, agent_session())}),))
        variants = []
        for version in (None, 2, 4, True):
            variants.append({**baseline, "version": version})
        for field in ("active", "selected", "workspaces"):
            item = copy.deepcopy(baseline); item.pop(field); variants.append(item)
        for bad_layout in ({"type": "pane", "pane_id": 0}, {"Pane": True}, {"Pane": -1}, {"Pane": 9},
                           {"Split": {"direction": [], "ratio": 0.5, "first": {"Pane": 0}, "second": {"Pane": 1}}}):
            item = copy.deepcopy(baseline); item["workspaces"][0]["tabs"][0]["layout"] = bad_layout; variants.append(item)
        for item in variants:
            with self.subTest(item=item):
                observations, errors = parse_herdr_snapshot(item)
                self.assertEqual(observations, ())
                self.assertTrue(errors)

    def test_invalid_independent_workspace_does_not_discard_good_metadata(self):
        value = snapshot(self.root, (tab({0: pane(self.root, agent_session())}),))
        value["workspaces"].append({"layout": {"Pane": 0}, "panes": {}, "zoomed": False})  # Legacy pre-tabs.
        observations, errors = parse_herdr_snapshot(value)
        self.assertEqual(len(observations), 1)
        self.assertTrue(errors)

    def test_known_pairs_and_local_pi_paths_never_infer_native_roots(self):
        foreign = "/foreign/pi.jsonl" if os.name == "nt" else r"C:\foreign\pi.jsonl"
        sessions = (agent_session(), agent_session("claude", "claude-id"), agent_session("pi", str(self.root / "pi.jsonl"), "path"),
                    agent_session("pi", "pi-id"), agent_session("codex", CODEX_ID, source="herdr:claude"),
                    agent_session("pi", foreign, "path"), agent_session("future-agent", "future-id"))
        value = snapshot(self.root, (tab({i: pane(self.root, item) for i, item in enumerate(sessions)}),))
        observations, errors = parse_herdr_snapshot(value)
        self.assertEqual([o.supported for o in observations], [True, True, True, True, False, False, False])
        self.assertIn("snapshot_agent_session_unsupported", errors)

    def test_ids_bound_utf8_and_controls_and_bad_kind_are_fixed_errors(self):
        for value in ("", "x" * 513, "😀" * 129, "\x1b" + SENTINEL, "\ud800"):
            document = snapshot(self.root, (tab({0: pane(self.root, agent_session(value=value))}),))
            observations, errors = parse_herdr_snapshot(document)
            self.assertFalse(observations)
            self.assertTrue(errors)
            self.assertNotIn(SENTINEL, repr(errors))
        document = snapshot(self.root, (tab({0: pane(self.root, agent_session(kind={"secret": SENTINEL}))}),))
        self.assertEqual(parse_herdr_snapshot(document)[1], ("snapshot_agent_session_kind_unsupported",))

    def test_corrupt_utf8_duplicate_and_large_integer_return_metadata_only_errors(self):
        blobs = (b"{broken " + SENTINEL.encode(), b"\xff" + SENTINEL.encode(),
                 b'{"version":3,"version":3}', b'{"version":' + b'9' * 5000 + b'}')
        for blob in blobs:
            self.path.write_bytes(blob)
            observations, errors = read_herdr_snapshot(self.path)
            self.assertFalse(observations)
            self.assertTrue(errors)
            self.assertNotIn(SENTINEL, repr(errors))

    def test_argv_is_not_executed_or_projected_and_restore_coverage_stays_incomplete(self):
        write_snapshot(self.path, snapshot(self.root, (tab({0: pane(self.root, agent_session(), launch_argv=[SENTINEL],
            agent_resume={"source": "herdr:codex", "agent": "codex", "argv": ["codex", "resume", CODEX_ID, SENTINEL]})}),)))
        observations, errors = read_herdr_snapshot(self.path)
        self.assertEqual(len(observations), 1)
        self.assertIn("snapshot_argv_restore_not_covered", errors)
        self.assertNotIn(SENTINEL, repr((observations, errors)))

    def test_host_namespace_and_foreign_os_gate_precede_path_or_syscalls(self):
        foreign = "/foreign/root" if os.name == "nt" else r"C:\foreign\root"
        with patch("local_agent_record_janitor.herdr_discovery.Path", side_effect=AssertionError("Path interpreted")):
            for args in ((str(self.root), {"host": "remote"}), (str(self.root), {"path_namespace": "wsl"}),
                         (foreign, {}), (r"\\remote\share\root", {}), ("relative", {})):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    local_herdr_path(args[0], **args[1])

    def test_default_roots_match_config_semantics_and_ignore_install_state_and_session_env(self):
        environment = {"XDG_CONFIG_HOME": str(self.root / "xdg"), "APPDATA": str(self.root / "appdata"),
            "HOME": str(self.root / "home"), "HERDR_HOME": str(self.root / "install"), "HERDR_CONFIG_PATH": str(self.path),
            "XDG_STATE_HOME": str(self.root / "state"), "HERDR_SESSION": "selected-server"}
        self.assertEqual(default_herdr_roots(environ=environment), tuple(self.root / "xdg" / p for p in ("herdr", "herdr-dev")))
        del environment["XDG_CONFIG_HOME"]
        expected = self.root / "appdata" if os.name == "nt" else self.root / "home" / ".config"
        self.assertEqual(default_herdr_roots(environ=environment)[0], expected / "herdr")
        for raw in ("", "relative"):
            with self.assertRaises(ValueError):
                default_herdr_roots(environ={"XDG_CONFIG_HOME": raw})
        with patch("local_agent_record_janitor.herdr_discovery.tempfile.gettempdir", return_value=str(self.root)):
            self.assertEqual(default_herdr_roots(environ={})[0], self.root / "herdr")

    def test_recovery_and_server_names_use_persisted_conventions(self):
        self.assertTrue(valid_recovery_name(recovery_name(timestamp=2**128 - 1)))
        for name in (recovery_name(timestamp=2**128), "session-1-1-0.json", recovery_name() + ".pending", "session-000-manual.json"):
            self.assertFalse(valid_recovery_name(name))
        self.assertTrue(valid_session_name("Research_1"))
        for name in ("default", ".", "..", "../outside", " bad", "a" * 65, "测试"):
            self.assertFalse(valid_session_name(name))
