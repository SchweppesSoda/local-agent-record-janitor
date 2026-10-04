from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent_record_janitor.adapters.native import NativeIntegrityAdapter
from local_agent_record_janitor.adapters.orca import OrcaAdapter
from local_agent_record_janitor.client_capability_guards import ClientCapabilityLimits
from local_agent_record_janitor.client_inventory import build_client_engine_contexts
from local_agent_record_janitor.record_identity import canonical_path
from tests.orca_support import CURRENT_ID, HISTORY_ID, SENTINEL, create_profile, make_record, replace_record
from tests.support import create_thread_index, write_rollout


class OrcaAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True) / "orca"
        self.homes = create_profile(self.root)

    def populate(self, home, *ids):
        rows = []
        for native_id in ids:
            path = write_rollout(home, native_id, originator="codex_cli_rs", source="cli")
            rows.append({"id": native_id, "rollout_path": str(path), "source": "cli"})
        create_thread_index(home, rows)

    def test_invalid_optional_default_keeps_reverse_marker_and_explicit_guards(self):
        from types import SimpleNamespace
        from local_agent_record_janitor.adapter_factory import discover_orca_guards
        from local_agent_record_janitor.orca_discovery import OrcaDiscoveryError
        args = SimpleNamespace(client="herdr", platform=("all",), codex_home=self.homes[0])
        with patch.dict(os.environ, {"ORCA_USER_DATA_PATH": ""}), patch(
            "local_agent_record_janitor.adapter_factory.default_orca_root",
            side_effect=OrcaDiscoveryError("orca_local_path_unproven"),
        ):
            guards = discover_orca_guards(args)
            self.assertEqual([guard.profile_root for guard in guards], [self.root])
            args.client = "orca"
            with self.assertRaises(OrcaDiscoveryError):
                discover_orca_guards(args)
            args.client = "herdr"
            with patch.dict(os.environ, {"ORCA_USER_DATA_PATH": "relative"}), self.assertRaises(OrcaDiscoveryError):
                discover_orca_guards(args)
            args.orca_root = (self.root,)
            self.assertEqual([guard.profile_root for guard in discover_orca_guards(args)], [self.root])

    def test_readonly_descriptor_and_snapshot_never_start_native_catalog(self):
        adapter = OrcaAdapter(profile_root=self.root)
        with patch("local_agent_record_janitor.inventory.build_session_catalog", side_effect=AssertionError("native scan")):
            snapshot = adapter.snapshot_references()
            descriptor = adapter.describe_client()
        self.assertEqual(len(snapshot.references), 4)
        self.assertEqual(len(descriptor.native_stores), 2)
        self.assertFalse(any(hasattr(adapter, field) for field in ("scan", "database", "codex_home", "snapshot_sessions")))
        for capability in descriptor.capability_limits:
            self.assertTrue(capability.inventory)
            for field in ("native_delete", "frontend_session_delete", "frontend_reference_delete", "frontend_project_delete", "remote_delete", "verify"):
                self.assertFalse(getattr(capability, field), field)
        self.assertNotIn(SENTINEL, json.dumps([r.to_dict() for r in snapshot.references]))

    def test_two_homes_same_ids_bind_once_and_missing_native_stays_visible(self):
        for home in self.homes:
            self.populate(home, CURRENT_ID)
        adapter = OrcaAdapter(profile_root=self.root)
        contexts = build_client_engine_contexts((adapter,), client="orca")
        targets = contexts[0].targets
        current = [t for t in targets if t.record_id == CURRENT_ID]
        missing = [t for t in targets if t.record_id == HISTORY_ID]
        self.assertEqual(len(current), 2)
        self.assertEqual({t.record_key.store.canonical_path for t in current}, {canonical_path(h) for h in self.homes})
        self.assertTrue(all(len(t.references) == 1 and t.classification.value == "healthy" for t in current))
        self.assertEqual(len(missing), 2)
        self.assertTrue(all(t.classification.value == "unverified" for t in missing))
        self.assertTrue(all(t.action_ids == () for t in targets))

    def test_provider_fork_history_does_not_invent_native_parent_and_normal_child_is_not_orphan(self):
        home = self.homes[0]
        child_id = "33333333-3333-4333-8333-333333333333"
        rows = []
        for native_id, source in ((HISTORY_ID, "cli"), (CURRENT_ID, "cli"),
                                  (child_id, {"subagent": {"thread_spawn": {"parent_thread_id": CURRENT_ID}}})):
            path = write_rollout(home, native_id, originator="codex_cli_rs", source=source)
            rows.append({"id": native_id, "rollout_path": str(path), "source": json.dumps(source)})
        create_thread_index(home, rows)
        contexts = build_client_engine_contexts((OrcaAdapter(profile_root=self.root),), client="orca")
        targets = {t.record_id: t for t in contexts[0].targets
                   if t.record_key is not None and t.record_key.store.canonical_path == canonical_path(home)}
        self.assertEqual(targets[CURRENT_ID].parent_thread_ids, ())
        self.assertEqual(targets[child_id].classification.value, "healthy")
        self.assertEqual(targets[child_id].parent_thread_ids, (CURRENT_ID,))
        self.assertEqual(targets[child_id].references, ())

    def test_multiple_profiles_share_one_catalog_pass_per_store(self):
        other = self.root.parent / "other-orca"
        homes = create_profile(other, accounts=1)
        for home in (*self.homes, *homes):
            self.populate(home, CURRENT_ID)
        from local_agent_record_janitor.inventory import _read_rollouts_partial
        with patch("local_agent_record_janitor.inventory._read_rollouts_partial", wraps=_read_rollouts_partial) as walk:
            contexts = build_client_engine_contexts((OrcaAdapter(profile_root=self.root), OrcaAdapter(profile_root=other)), client="orca")
        self.assertEqual(walk.call_count, 3)
        self.assertEqual(len([t for t in contexts[0].targets if t.record_id == CURRENT_ID]), 3)

    def test_remote_and_foreign_locators_are_opaque_and_never_catalog_default_home(self):
        for host, wsl, locator in (("ssh:remote", None, "/remote/home"), ("local", "Ubuntu", "/home/wsl"),
                                   ("local", None, "relative/home")):
            record = make_record(self.homes[0])
            record["location"].update(executionHostId=host, wslDistro=wsl)
            record["accountHome"]["path"] = locator
            replace_record(self.root, record)
            adapter = OrcaAdapter(profile_root=self.root)
            with self.subTest(host=host, wsl=wsl), patch("local_agent_record_janitor.adapters.orca.prove_runtime_home", side_effect=AssertionError("unexpected runtime proof")):
                snapshot = adapter.snapshot_references()
            refs = [r for r in snapshot.references if r.frontend_id == record["sessionId"]]
            self.assertTrue(all(r.native_record is None and r.opaque_native_locator == locator and r.evidence_complete is False for r in refs))
            self.assertIn("record_native_root_unproven", [e.message for e in snapshot.errors])

    def test_runtime_name_alone_is_not_a_store_but_persisted_local_record_is(self):
        runtime = self.root / "codex-runtime-home" / "home"
        (runtime / "sessions").mkdir(parents=True)
        adapter = OrcaAdapter(profile_root=self.root)
        self.assertNotIn(canonical_path(runtime), {s.canonical_path for s in adapter.describe_client().native_stores})
        record = make_record(runtime)
        replace_record(self.root, record)
        snapshot = adapter.snapshot_references(refresh=True)
        self.assertIn(canonical_path(runtime), {s.canonical_path for s in snapshot.descriptor.native_stores})
        self.assertTrue(all(r.native_record.store.canonical_path == canonical_path(runtime)
                            for r in snapshot.references if r.frontend_id == record["sessionId"]))

    def test_failed_runtime_proof_is_precise_and_account_errors_keep_journal_refs(self):
        runtime = self.root / "codex-runtime-home" / "home"
        runtime.mkdir(parents=True)  # Missing plain sessions directory.
        replace_record(self.root, make_record(runtime))
        adapter = OrcaAdapter(profile_root=self.root)
        snapshot = adapter.snapshot_references()
        self.assertNotIn(canonical_path(runtime), {s.canonical_path for s in snapshot.descriptor.native_stores})
        self.assertTrue(ClientCapabilityLimits.from_adapters((adapter,)).reasons("codex", "native_delete", native_root=runtime))
        from local_agent_record_janitor.adapters.orca import require_plain_directory
        def probe(path):
            if path == self.root / "codex-accounts":
                raise PermissionError("fixture")
            return require_plain_directory(path)
        with patch("local_agent_record_janitor.adapters.orca.require_plain_directory", side_effect=probe):
            snapshot = OrcaAdapter(profile_root=self.root).snapshot_references()
        self.assertEqual(len(snapshot.references), 4)
        self.assertIn("account_inventory_unavailable", [e.message for e in snapshot.errors])

    def test_failed_ownership_is_precise_blocker_without_declaring_owned_store(self):
        (self.homes[0] / ".orca-managed-home").write_text("wrong-account", encoding="utf-8")
        adapter = OrcaAdapter(profile_root=self.root)
        snapshot = adapter.snapshot_references()
        self.assertEqual({s.canonical_path for s in snapshot.descriptor.native_stores}, {canonical_path(self.homes[1])})
        limits = ClientCapabilityLimits.from_adapters((adapter,))
        self.assertIn("account_ownership_unproven", str(limits.reasons("codex", "native_delete", native_root=self.homes[0])))
        unrelated = self.root.parent / "unrelated"
        unrelated.mkdir()
        self.assertEqual(limits.reasons("codex", "native_delete", native_root=unrelated), ())
        self.populate(self.homes[1], CURRENT_ID)
        contexts = build_client_engine_contexts((adapter,), client="orca")
        self.assertTrue(any(t.record_key is not None and t.record_key.store.canonical_path == canonical_path(self.homes[1]) for t in contexts[0].targets))

    def test_legacy_and_restore_sources_are_reported_without_reading_bytes(self):
        source = self.root / "agent-sessions" / "agent-sessions.json"
        source.parent.mkdir()
        source.write_text(SENTINEL, encoding="utf-8")
        profile = self.root / "profiles" / "profile-1" / "profile-state.db"
        profile.parent.mkdir(parents=True)
        profile.write_text(SENTINEL, encoding="utf-8")
        original = Path.open
        def read(path, *args, **kwargs):
            if path in {source, profile}:
                self.fail("unsupported restore bytes were read")
            return original(path, *args, **kwargs)
        with patch("pathlib.Path.open", read):
            snapshot = OrcaAdapter(profile_root=self.root).snapshot_references()
        self.assertEqual(len(snapshot.references), 4)
        self.assertEqual({e.database for e in snapshot.errors}, {source, profile})
        self.assertNotIn(SENTINEL, repr(snapshot))

    def test_redirected_rollout_is_not_followed_by_native_catalog(self):
        outside = self.root.parent / "outside.jsonl"
        outside.write_text(SENTINEL, encoding="utf-8")
        linked = self.homes[0] / "sessions" / "linked.jsonl"
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Temporary symlink unavailable: {exc}")
        original = Path.open
        def read(path, *args, **kwargs):
            if path in {linked, outside}:
                self.fail("outside rollout read")
            return original(path, *args, **kwargs)
        with patch("pathlib.Path.open", read):
            catalog = OrcaAdapter(profile_root=self.root).native_catalog_for("codex")
        self.assertTrue(any(e.database == linked for e in catalog.errors))
        self.assertNotIn(SENTINEL, repr(catalog))

    def test_all_known_restore_families_are_presence_only(self):
        sources = [self.root / "agent-hooks" / "last-status.json", self.root / "orca-runtime.json"]
        for parent in (self.root, self.root / "profiles" / "profile-1"):
            sources.extend(parent / ("orca-data.json" + suffix) for suffix in ("", ".bak.1", ".bak.5"))
        sources.extend(self.root / "profiles" / "profile-1" / ("profile-state.db" + suffix)
                       for suffix in ("-wal", "-shm", "-journal"))
        for source in sources:
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(SENTINEL, encoding="utf-8")
        original = Path.open
        def read(path, *args, **kwargs):
            if path in sources:
                self.fail("unsupported restore/runtime bytes were read")
            return original(path, *args, **kwargs)
        with patch("pathlib.Path.open", read):
            snapshot = OrcaAdapter(profile_root=self.root).snapshot_references()
        self.assertEqual(len(snapshot.references), 4)
        self.assertEqual({e.database for e in snapshot.errors}, {*sources, self.root / "agent-hooks"})
        self.assertTrue(set(sources).issubset(snapshot.descriptor.sources))
        self.assertNotIn(SENTINEL, repr(snapshot))

    def test_nonregular_sqlite_sidecars_stop_before_connect(self):
        journal = self.root / "agent-session-journal.db"
        before = journal.read_bytes()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = journal.with_name(journal.name + suffix)
            sidecar.mkdir()
            with self.subTest(suffix=suffix), patch("local_agent_record_janitor.adapters.orca.read_orca_journal") as reader:
                snapshot = OrcaAdapter(profile_root=self.root).snapshot_references()
                reader.assert_not_called()
                self.assertIn("orca_metadata_file_redirected", [e.message for e in snapshot.errors])
            sidecar.rmdir()
        self.assertEqual(journal.read_bytes(), before)

    def test_active_wal_current_reference_is_visible_and_closed_wal_reader_may_create_lock_files(self):
        journal = self.root / "agent-session-journal.db"
        with closing(sqlite3.connect(journal)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            before = journal.read_bytes()
            record = make_record(self.homes[0], "orca_wal_fixture")
            connection.execute("INSERT INTO agent_session_records VALUES (?,?)", (record["sessionId"], json.dumps(record)))
            connection.commit()
            # TEMP-only immutable control demonstrates the new row is not in
            # the main DB. Production must not ignore committed WAL data.
            with closing(sqlite3.connect(journal.as_uri() + "?immutable=1", uri=True)) as base:
                self.assertEqual(base.execute("SELECT COUNT(*) FROM agent_session_records").fetchone()[0], 3)
            snapshot = OrcaAdapter(profile_root=self.root).snapshot_references()
            refs = [r for r in snapshot.references if r.frontend_id == record["sessionId"]]
            self.assertEqual(len(refs), 2)
            self.assertTrue(any(r.kind.value == "current" and r.native_record is not None for r in refs))
            self.assertEqual(snapshot.errors, ())
            self.assertEqual(journal.read_bytes(), before)
            self.assertNotIn(SENTINEL, repr(snapshot))
        before = journal.read_bytes()
        OrcaAdapter(profile_root=self.root).snapshot_references()
        self.assertEqual(journal.read_bytes(), before)
        # Read-only SQL can create WAL/SHM lock files. Never remove these
        # SQLite-owned files after inventory or claim filesystem zero-write.
        self.assertTrue(journal.with_name(journal.name + "-shm").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction fixture")
    def test_desktop_sqlite_junction_is_outside_the_orca_native_reader(self):
        outside = self.root.parent / "outside-desktop"
        outside.mkdir()
        database = outside / "codex.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE local_thread_catalog(host_id TEXT,thread_id TEXT,display_title TEXT)")
            connection.execute("INSERT INTO local_thread_catalog VALUES ('local',?,?)", (CURRENT_ID, SENTINEL))
            connection.commit()
        self.populate(self.homes[0], CURRENT_ID)
        link = self.homes[0] / "sqlite"
        def quote(path):
            return "'" + str(path).replace("'", "''") + "'"
        result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(outside)} | Out-Null"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            self.skipTest("Temporary Windows junction creation unavailable")
        before = database.read_bytes()
        with patch.object(NativeIntegrityAdapter, "list_sessions", side_effect=AssertionError("Desktop sources are outside this reader")):
            catalog = OrcaAdapter(profile_root=self.root).native_catalog_for("codex")
        self.assertEqual(database.read_bytes(), before)
        self.assertNotIn(SENTINEL, repr(catalog))
