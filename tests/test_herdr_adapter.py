from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.adapters import HerdrAdapter
from local_agent_record_janitor.client_inventory import build_client_engine_contexts
from local_agent_record_janitor.herdr_discovery import bounded_entries
from tests.herdr_support import CODEX_ID, SENTINEL, agent_session, create_profile, pane, recovery_name, snapshot, tab, write_snapshot


class HerdrAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.outer = Path(temporary.name).resolve(strict=True)
        self.root = self.outer / "herdr"
        self.project = create_profile(self.root)

    def test_current_empty_keeps_every_restore_and_session_identity_and_all_caps_are_readonly(self):
        adapter = HerdrAdapter(profile_root=self.root)
        inventory = adapter.snapshot_references()
        self.assertEqual(len(inventory.references), 7)
        self.assertEqual(sum(r.kind.value == "restore" for r in inventory.references), 3)
        self.assertEqual(len({r.binding_key for r in inventory.references}), 7)
        same = [r for r in inventory.references if r.native_id == CODEX_ID]
        self.assertGreater(len(same), 1)
        self.assertTrue(all(r.native_record is None for r in same))
        self.assertFalse(inventory.descriptor.native_stores)
        self.assertEqual(inventory.descriptor.owner_process_root, self.root)
        self.assertEqual({r.lifecycle.value for r in inventory.references if r.kind.value == "current"}, {"unknown"})
        self.assertEqual({r.lifecycle.value for r in inventory.references if r.kind.value == "restore"}, {"restorable"})
        self.assertTrue(all(error.store is None for error in inventory.errors))
        self.assertIn("live_metadata_not_probed", [e.message for e in inventory.errors])
        for capability in inventory.descriptor.capability_limits:
            self.assertTrue(capability.inventory)
            self.assertFalse(any(getattr(capability, field) for field in (
                "native_delete", "frontend_session_delete", "frontend_reference_delete", "frontend_project_delete", "remote_delete", "verify")))
        for engine in inventory.descriptor.inventory_engines:
            self.assertIsNone(adapter.native_catalog_for(engine))

    def test_unknown_spelling_survives_public_contexts_with_verify_false(self):
        write_snapshot(self.root / "sessions" / "unknown" / "session.json", snapshot(self.project,
            (tab({1: pane(self.project, agent_session("Future_Agent"))}),)))
        adapter = HerdrAdapter(profile_root=self.root)
        with patch("local_agent_record_janitor.inventory._read_rollouts_partial", side_effect=AssertionError("native walk")):
            contexts = build_client_engine_contexts((adapter,), client="herdr")
        targets = tuple(t for context in contexts for t in context.targets)
        unknown = next(t for t in targets if t.engine == "unsupported:future-agent")
        self.assertEqual(unknown.references[0].raw_backend, "Future_Agent")
        self.assertFalse(unknown.capability.verify)
        self.assertEqual(unknown.action_ids, ())
        self.assertTrue(all(not t.capability.verify for t in targets))
        self.assertEqual(len(targets), 8)

    def test_bad_sources_preserve_good_references_and_never_read_history_or_start_runtime(self):
        mixed = self.outer / "mixed"
        create_profile(mixed, mixed=True)
        original = Path.open
        def opened(path, *args, **kwargs):
            if path.name in {"session-history.json", "herdr.sock", "herdr-client.sock"}:
                self.fail("history/socket was opened")
            return original(path, *args, **kwargs)
        before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in mixed.rglob("*") if p.is_file()}
        with patch("pathlib.Path.open", opened), patch("subprocess.Popen", side_effect=AssertionError("runtime started")), patch("socket.socket", side_effect=AssertionError("socket connected")):
            result = HerdrAdapter(profile_root=mixed).snapshot_references()
        self.assertEqual(len(result.references), 8)
        self.assertIn("snapshot_version_unsupported", [e.message for e in result.errors])
        self.assertIn("snapshot_argv_restore_not_covered", [e.message for e in result.errors])
        self.assertGreaterEqual(len(result.errors), 6)
        self.assertNotIn(SENTINEL, repr(result))
        after = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in mixed.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_unpublished_pending_is_not_read_and_manual_recovery_name_is_incomplete(self):
        recovery = self.root / "session-backups"
        pending = recovery / (recovery_name(8) + ".pending")
        manual = recovery / "session-000-manual.json"
        for path in (pending, manual):
            path.write_text(SENTINEL, encoding="utf-8")
        original = Path.open
        def opened(path, *args, **kwargs):
            if path in {pending, manual}:
                self.fail("unsupported recovery bytes were read")
            return original(path, *args, **kwargs)
        with patch("pathlib.Path.open", opened):
            result = HerdrAdapter(profile_root=self.root).snapshot_references()
        self.assertEqual(len(result.references), 7)
        self.assertTrue(any(e.database == manual and e.message == "recovery_filename_not_covered" for e in result.errors))
        self.assertFalse(any(e.database == pending for e in result.errors))

    def test_source_permission_failure_and_directory_limit_are_not_empty_success(self):
        original = Path.open
        bad = self.root / "session-backups" / recovery_name(1)
        def opened(path, *args, **kwargs):
            if path == bad:
                raise PermissionError(SENTINEL)
            return original(path, *args, **kwargs)
        with patch("pathlib.Path.open", opened):
            result = HerdrAdapter(profile_root=self.root).snapshot_references()
        self.assertEqual(len(result.references), 6)
        self.assertTrue(any(e.database == bad for e in result.errors))
        self.assertNotIn(SENTINEL, repr(result))
        with self.assertRaises(ValueError):
            bounded_entries(self.root / "sessions", maximum=1)

    def test_fresh_snapshot_observes_an_independent_new_recovery_binding(self):
        adapter = HerdrAdapter(profile_root=self.root)
        before = adapter.snapshot_references()
        write_snapshot(self.root / "session-backups" / recovery_name(9), snapshot(self.project,
            (tab({0: pane(self.project, agent_session())}),)))
        self.assertIs(adapter.snapshot_references(), before)
        after = adapter.snapshot_references(refresh=True)
        self.assertEqual(len(after.references), len(before.references) + 1)
        self.assertEqual(len({r.binding_key for r in after.references}), len(after.references))

    @unittest.skipUnless(os.name == "nt", "Windows junction validation requires Windows")
    def test_named_session_junction_is_not_followed_and_good_sessions_remain_visible(self):
        outside = self.outer / "outside"
        outside.mkdir()
        payload = outside / "session.json"
        payload.write_text(SENTINEL, encoding="utf-8")
        linked = self.root / "sessions" / "redirected"
        quoted = lambda path: "'" + str(path).replace("'", "''") + "'"
        process = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            f"New-Item -ItemType Junction -Path {quoted(linked)} -Target {quoted(outside)} | Out-Null"],
            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode:
            self.skipTest("Temporary Windows junction unavailable")
        self.addCleanup(linked.rmdir)
        original = Path.open
        def opened(path, *args, **kwargs):
            if path == payload or path == linked / "session.json":
                self.fail("outside metadata was read")
            return original(path, *args, **kwargs)
        with patch("pathlib.Path.open", opened):
            result = HerdrAdapter(profile_root=self.root).snapshot_references()
        self.assertEqual(len(result.references), 7)
        self.assertTrue(any(e.database == linked and e.message == "herdr_directory_redirected" for e in result.errors))
        self.assertNotIn(SENTINEL, repr(result))
