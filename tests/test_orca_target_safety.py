from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent_record_janitor.adapters import NativeIntegrityAdapter, OrcaAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.orca_metadata import parse_orca_record
from local_agent_record_janitor.orca_target_safety import (
    _absent_configuration, _optional_identity, freeze_orca_target,
    inspect_orca_target_processes, recheck_orca_target, validate_document_targets,
)
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.operation_store import plan_sha256
from tests.orca_support import CURRENT_ID, SENTINEL, create_profile, make_record, replace_record
from tests.support import create_thread_index, write_rollout

TARGET_ID = "33333333-3333-4333-8333-333333333333"
CHILD_ID = "44444444-4444-4444-8444-444444444444"


@unittest.skipUnless(os.name == "nt", "Orca P5 preflight is a Windows-only combination")
class OrcaTargetSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.profile = self.root / "orca"
        self.home = create_profile(self.profile, accounts=1)[0]
        self.rollout = write_rollout(self.home, TARGET_ID, originator="codex_cli_rs", source="cli")
        self.rollout = self.rollout.rename(self.rollout.with_name("rollout-2026-10-03T00-00-00-" + TARGET_ID + ".jsonl"))
        create_thread_index(self.home, [{"id": TARGET_ID, "rollout_path": str(self.rollout), "source": "cli"}])
        self.binary = self.root / "synthetic-codex.exe"
        self.binary.write_bytes(b"synthetic binary identity, never launched")
        self.adapter = OrcaAdapter(profile_root=self.profile, codex_bin_hint=self.binary)
        for patcher in (
            patch("local_agent_record_janitor.orca_runtime.RUNTIME_ACCEPTED", False),
            patch("local_agent_record_janitor.orca_target_safety._schema_metadata", return_value={}),
            patch("local_agent_record_janitor.orca_target_safety._startup_control", return_value={}),
            patch("local_agent_record_janitor.orca_runtime.PINNED_BINARY_SHA256", hashlib.sha256(self.binary.read_bytes()).hexdigest()),
            patch("local_agent_record_janitor.orca_target_safety._windows_process_snapshot", return_value=()),
            patch("local_agent_record_janitor.orca_target_safety._system_configuration_root", return_value=self.root / "system-config"),
            patch.dict(os.environ, {"APPDATA": str(self.root / "appdata"), "ORCA_USER_DATA_PATH": "",
                                   "LOCALAPPDATA": str(self.root / "localappdata"),
                                   "CODEX_HOME": str(self.root / "unrelated-native")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def freeze(self, **kwargs):
        return freeze_orca_target(self.adapter, self.home, TARGET_ID, affected_ids=(TARGET_ID,),
                                  rollout_paths=(self.rollout,), binary=self.binary, **kwargs)

    def test_preflight_is_body_free_and_never_grants_writer(self):
        evidence = self.freeze()
        self.assertTrue(evidence["preflight_complete"], evidence)
        self.assertFalse(evidence["native_delete"])
        self.assertEqual(evidence["api_boundary"], "not_validated")
        self.assertNotIn(SENTINEL, json.dumps(evidence))
        self.assertNotIn("spawnToken", json.dumps(evidence))
        self.assertEqual(recheck_orca_target(evidence, self.adapter), ())

    def test_native_filename_and_compressed_copies_block_before_start(self):
        from local_agent_record_janitor.orca_target_safety import _rollout_boundary
        original = self.rollout
        for name in ("rollout-" + TARGET_ID + ".jsonl",
                     "rollout-2026-02-30T00-00-00-" + TARGET_ID + ".jsonl",
                     "rollout-2026-10-03T00-00-00-" + CHILD_ID + ".jsonl"):
            with self.subTest(name=name):
                self.rollout = self.rollout.rename(original.with_name(name))
                self.assertIn("orca_rollout_filename_unverified", self.freeze()["blocker_codes"])
        self.rollout = self.rollout.rename(original)
        evidence = self.freeze()
        self.assertTrue(evidence["preflight_complete"], evidence)
        for compressed in (Path(str(self.rollout) + ".zst"),
                           self.home / "archived_sessions" / "compressed-only.jsonl.zst"):
            compressed.parent.mkdir(exist_ok=True)
            compressed.write_bytes(b"synthetic compressed bytes, never decompressed")
            try:
                self.assertIn("orca_compressed_rollout_scope_unverified", self.freeze()["blocker_codes"])
                self.assertIn("orca_compressed_rollout_scope_unverified", recheck_orca_target(evidence, self.adapter))
            finally:
                compressed.unlink()
        independent = str(self.rollout.with_name("rollout-2026-10-03T00-00-00-" + TARGET_ID + "_" + CHILD_ID + ".jsonl"))
        self.assertTrue(_rollout_boundary(self.home, (independent,), (TARGET_ID,))["compressed_rollouts_absent"])

    def test_unregistered_optional_database_families_are_never_opened(self):
        evidence = self.freeze()
        for name in ("thread_history_1.sqlite", "memories_v2_1.sqlite", "agent_message_board_1.sqlite"):
            for suffix in ("", "-wal", "-shm", "-journal"):
                with self.subTest(name=name, suffix=suffix):
                    path = self.home / (name + suffix)
                    path.write_bytes(b"private unsupported database sentinel, never open")
                    try:
                        self.assertIn("orca_system_configuration_unverified", self.freeze()["blocker_codes"])
                        self.assertTrue(recheck_orca_target(evidence, self.adapter))
                    finally:
                        path.unlink()

    def test_unrelated_native_logs_block_startup_without_reading_log_body(self):
        import sqlite3
        from contextlib import closing
        from tests.orca_native_support import create_native_schema
        (self.home / "state_5.sqlite").unlink()
        create_native_schema(self.home)
        evidence = self.freeze()
        self.assertTrue(evidence["preflight_complete"], evidence)
        with closing(sqlite3.connect(self.home / "logs_2.sqlite")) as connection:
            connection.execute("INSERT INTO logs(ts,ts_nanos,level,target,thread_id,feedback_log_body) VALUES(0,0,'INFO','test',?,?)",
                (CHILD_ID, SENTINEL))
            connection.commit()
        self.assertIn("orca_startup_logs_cleanup_unverified", self.freeze()["blocker_codes"])
        self.assertIn("orca_startup_logs_cleanup_unverified", recheck_orca_target(evidence, self.adapter))
        self.assertNotIn(SENTINEL, json.dumps(self.freeze()))
        # Read-only diagnosis does not perform the native startup maintenance.
        self.assertEqual(recheck_orca_target(evidence, self.adapter, phase="readonly_recovery"), ())

    def test_native_maintenance_lock_requires_plain_empty_exclusive_file(self):
        lock = self.home / ".sqlite-maintenance.lock"
        lock.write_bytes(b"")
        evidence = self.freeze()
        self.assertTrue(evidence["preflight_complete"], evidence)
        lock.write_bytes(b"unapproved lock content")
        self.assertIn("orca_startup_content_unverified", self.freeze()["blocker_codes"])
        lock.write_bytes(b"")
        alias = self.root / "maintenance-alias"
        os.link(lock, alias)
        self.assertIn("orca_startup_artifact_linked", self.freeze()["blocker_codes"])

    def test_selected_memory_consolidation_blocks_without_restricting_other_threads(self):
        import sqlite3
        from contextlib import closing
        from tests.orca_native_support import create_native_schema
        (self.home / "state_5.sqlite").unlink()
        create_native_schema(self.home)
        with closing(sqlite3.connect(self.home / "memories_1.sqlite")) as connection:
            for identifier, selected in ((TARGET_ID, 0), (CHILD_ID, 1)):
                connection.execute("INSERT INTO stage1_outputs(thread_id,source_updated_at,raw_memory,rollout_summary,generated_at,selected_for_phase2) VALUES (?,1,?,?,1,?)",
                    (identifier, SENTINEL, SENTINEL, selected))
            connection.commit()
        evidence = self.freeze()
        self.assertTrue(evidence["preflight_complete"], evidence)
        with closing(sqlite3.connect(self.home / "memories_1.sqlite")) as connection:
            connection.execute("UPDATE stage1_outputs SET selected_for_phase2=1 WHERE thread_id=?", (TARGET_ID,))
            connection.commit()
        self.assertIn("orca_global_memory_job_scope_unverified", self.freeze()["blocker_codes"])
        self.assertIn("orca_global_memory_job_scope_unverified", recheck_orca_target(evidence, self.adapter))
        self.assertNotIn(SENTINEL, json.dumps(self.freeze()))

    def test_preexisting_native_index_replacement_leaf_is_not_authorized(self):
        evidence = self.freeze()
        temporary_index = self.home / "session_index.jsonl.tmp"
        os.link(self.rollout, temporary_index)
        self.assertTrue(recheck_orca_target(evidence, self.adapter))
        temporary_index.unlink()
        temporary_index.write_bytes(b"unapproved ordinary temporary leaf")
        self.assertIn("orca_system_configuration_unverified", self.freeze()["blocker_codes"])

    def test_registered_exact_native_target_has_ready_v3_but_no_ack_stays_closed(self):
        from tests.orca_native_support import create_native_schema, add_native_record
        (self.home / "state_5.sqlite").unlink()
        self.rollout.unlink()
        create_native_schema(self.home)
        self.rollout = add_native_record(self.home, TARGET_ID)
        with patch("local_agent_record_janitor.orca_runtime.RUNTIME_ACCEPTED", True):
            coordinator = OperationCoordinator(CleanupService())
            plan = coordinator.plan_operation(client="orca", record_ids=(TARGET_ID,), engines=("codex",),
                adapters=(self.adapter,), plan_path=self.root / "ready.json", operation_id="orca-ready")
            self.assertEqual(plan.get("goal_status"), "ready", plan.get("blockers", plan))
            self.assertEqual(plan["schema_version"], "larj.operation-plan.v3")
            self.assertEqual(len(plan["actions"]), 1)
            self.assertEqual(plan["target_safety_evidence"][0]["action_id"], plan["actions"][0]["action_id"])
            no_ack = coordinator.apply_operation(client="orca", record_ids=(TARGET_ID,), engines=("codex",),
                operation_id="orca-ready", plan_path=self.root / "ready.json", plan_sha256=plan["plan_sha256"])
            self.assertEqual(no_ack["blockers"][0]["blocker_code"], "clients_closed_ack_required")

    def test_cold_unknown_verification_reads_frozen_artifacts_and_original_job_when_capability_closed(self):
        import sqlite3
        import uuid
        from contextlib import closing
        from local_agent_record_janitor.cleanup_service import partition_actions
        from local_agent_record_janitor.orca_runtime import _WindowsJob, runtime_host_identity, runtime_instance_stopped
        from tests.orca_native_support import create_native_schema, add_native_record
        (self.home / "state_5.sqlite").unlink()
        self.rollout.unlink()
        create_native_schema(self.home)
        self.rollout = add_native_record(self.home, TARGET_ID)
        path = self.root / "cold.json"
        unrelated = self.root / "unrelated-native"
        unrelated.mkdir()
        (unrelated / "state_5.sqlite").write_bytes(b"unrelated invalid database")
        unrelated_adapter = NativeIntegrityAdapter(codex_home=unrelated)
        coordinator = OperationCoordinator(CleanupService())
        with patch("local_agent_record_janitor.orca_runtime.RUNTIME_ACCEPTED", True):
            plan = coordinator.plan_operation(client="orca", record_ids=(TARGET_ID,), engines=("codex",),
                adapters=(self.adapter,), plan_path=path, operation_id="orca-cold-unknown")
        self.assertEqual(plan["goal_status"], "ready", plan)
        live = coordinator._live[plan["operation_id"]]
        batch = partition_actions(live.candidates)[0]
        child_id = plan["child_batches"][0]["child_operation_id"]
        store, _ = coordinator._open_batch_store(live, batch, child_id, self.home, 1)
        job = _WindowsJob("Local\\larj-orca-" + uuid.uuid4().hex)
        instance = {"schema_version": "larj.orca-runtime-instance.v1", "job_name": job.name, **runtime_host_identity()}
        store.append_event({"event": "orca_runtime_startup", "mutation_started": True, "runtime_instance": instance},
            state_updates={"mutation_started": True, "runtime_instance": instance, "current_action_state": "mutation_started"})
        args = {"operation_id": plan["operation_id"], "plan_path": path}
        try:
            running = OperationCoordinator(CleanupService()).verify_operation(**args)
            self.assertEqual(running["goal_status"], "unknown", running)
            self.assertTrue(store.state_path.exists())
        finally:
            job.close()
        index = self.home / "session_index.jsonl"
        index.write_text("{malformed\n", encoding="utf-8")
        malformed = OperationCoordinator(CleanupService()).verify_operation(**args)
        self.assertEqual(malformed["goal_status"], "unknown", malformed)
        self.assertTrue(malformed["mutation_started"])
        self.assertTrue(malformed["batches"])
        self.assertTrue(store.state_path.exists())
        index.unlink()
        original_build = OperationCoordinator._build_context_sources

        def bounded_build(runner, client, adapters, **kwargs):
            self.assertEqual(client, "native")
            self.assertEqual([adapter.codex_home for adapter in adapters], [self.home])
            return original_build(runner, client, adapters, **kwargs)

        bounded = patch.object(OperationCoordinator, "_build_context_sources", bounded_build)
        bounded.start()
        self.addCleanup(bounded.stop)
        # Narrowing inventory must retain newly discovered protection sources.
        with patch.dict(os.environ, {"ORCA_USER_DATA_PATH": str(self.root / "missing-current-profile")}):
            guarded = OperationCoordinator(CleanupService()).verify_operation(**args, adapters=(unrelated_adapter,))
        self.assertEqual(guarded["goal_status"], "unknown", guarded)
        self.assertIn("guard_source_incomplete", {item["blocker_code"] for item in guarded["blockers"]})
        self.assertTrue(guarded["mutation_started"])
        self.assertTrue(guarded["batches"])
        self.assertTrue(store.state_path.exists())
        for supplied in (None, (), (unrelated_adapter,)):
            output = StringIO()
            exit_code = main(["operation", "verify", "--operation-id", plan["operation_id"], "--plan", str(path), "--json"],
                             adapters=supplied, stdout=output, stderr=StringIO(), cleanup_service=CleanupService())
            public = json.loads(output.getvalue())
            self.assertEqual(exit_code, 3, public)
            self.assertEqual(public["goal_status"], "completed_with_residuals", public)
            self.assertTrue(public["mutation_started"])
        with patch("local_agent_record_janitor.orca_runtime.runtime_instance_stopped", wraps=runtime_instance_stopped) as stopped:
            residual = coordinator.verify_operation(**args, adapters=(self.adapter, unrelated_adapter))
            self.assertEqual(residual["goal_status"], "completed_with_residuals", residual)
            self.assertTrue(residual["mutation_started"])
            stopped.assert_called_once_with(instance)
        self.assertTrue(self.rollout.exists())
        self.assertEqual(store.read_result()["runtime_instance"], instance)
        self.rollout.unlink()
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("DELETE FROM threads WHERE id=?", (TARGET_ID,))
            connection.commit()
        # A sidebar-only residual is still a frozen native artifact.
        index.write_text(json.dumps({"id": TARGET_ID}) + "\n", encoding="utf-8")
        self.assertEqual(OperationCoordinator(CleanupService()).verify_operation(**args)["goal_status"], "completed_with_residuals")
        index.write_text("", encoding="utf-8")
        with patch("local_agent_record_janitor.orca_runtime.runtime_instance_stopped", wraps=runtime_instance_stopped) as stopped:
            complete = OperationCoordinator(CleanupService()).verify_operation(**args)
            self.assertEqual(complete["goal_status"], "complete", complete)
            stopped.assert_called_once_with(instance)

    def test_readonly_recovery_does_not_require_current_invocation_or_binary(self):
        evidence = self.freeze()
        self.binary.unlink()
        with patch("local_agent_record_janitor.orca_runtime.invocation_policy", return_value={"changed": True}):
            self.assertEqual(recheck_orca_target(evidence, self.adapter, phase="readonly_recovery"), ())
            self.assertTrue(recheck_orca_target(evidence, self.adapter, phase="before_start"))

    def test_known_sync_lock_and_wrong_arg0_kinds_never_pass_after_start(self):
        from local_agent_record_janitor.orca_target_safety import _startup_manifest
        evidence = self.freeze()
        lock = self.home / "plugins.sync.lock"
        lock.write_bytes(b"")
        self.assertTrue(recheck_orca_target(evidence, self.adapter, phase="post_start"))
        lock.unlink()
        bucket = self.home / "tmp" / "arg0" / "codex-arg0abc123"
        bucket.parent.mkdir(parents=True)
        bucket.write_bytes(b"")
        with self.assertRaises(ValueError):
            _startup_manifest(self.home, after_start=True)
        bucket.unlink()
        (bucket / ".lock").mkdir(parents=True)
        with self.assertRaises(ValueError):
            _startup_manifest(self.home, after_start=True)
        (bucket / ".lock").rmdir()
        (bucket / ".lock").write_bytes(b"")
        self.assertTrue(_startup_manifest(self.home, after_start=True))

    def test_runtime_held_empty_arg0_lock_uses_identity_without_reading_locked_bytes(self):
        import msvcrt
        from local_agent_record_janitor.orca_target_safety import _startup_manifest
        lock = self.home / "tmp" / "arg0" / "codex-arg0abc123" / ".lock"
        lock.parent.mkdir(parents=True)
        lock.write_bytes(b"")
        with lock.open("r+b") as stream:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                entries = _startup_manifest(self.home, after_start=True)
                entry = next(value for value in entries if value["path"] == str(lock))
                self.assertEqual(entry["size"], 0)
                self.assertNotIn("sha256", entry)
                self.assertEqual(entry["file_id"], lock.stat().st_ino)
                with self.assertRaisesRegex(ValueError, "orca_existing_startup_artifact_unverified"):
                    _startup_manifest(self.home, after_start=False)
            finally:
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        lock.write_bytes(b"unexpected")
        with self.assertRaisesRegex(ValueError, "orca_startup_content_unverified"):
            _startup_manifest(self.home, after_start=True)

    def test_known_orca_owner_and_unbranded_child_block_but_unrelated_process_does_not(self):
        lease = parse_orca_record("orca_fixture_01", make_record(self.home)).lease
        unrelated = {"process_id": 10, "parent_process_id": 0, "name": "unrelated.exe", "start_time_ms": 1}
        self.assertTrue(inspect_orca_target_processes((lease,), records=(unrelated,))["clients_closed"])
        main_process = {"process_id": 20, "parent_process_id": 0, "name": "Orca.exe", "start_time_ms": 2}
        child = {"process_id": 30, "parent_process_id": 20, "name": "python.exe", "start_time_ms": 3}
        result = inspect_orca_target_processes((lease,), records=(unrelated, main_process, child))
        self.assertEqual(result["blocking_process_ids"], [20, 30])
        owner = replace(lease, owner_host="local", owner_pid=20, owner_start_time_ms=2)
        result = inspect_orca_target_processes((owner,), records=(unrelated, child))
        self.assertEqual(result["blocking_process_ids"], [30])  # Absent owner's child is still live.

    def test_lease_identity_reuse_missing_metadata_and_probe_failure_are_not_closed(self):
        lease = parse_orca_record("orca_fixture_01", make_record(self.home)).lease
        owner = replace(lease, owner_host="local", owner_pid=20, owner_start_time_ms=2)
        result = inspect_orca_target_processes((owner,), records=({"process_id": 20,
            "parent_process_id": 0, "name": "codex.exe", "start_time_ms": 99},))
        self.assertFalse(result["clients_closed"])
        self.assertIn("orca_lease_owner_identity_changed", result["errors"])
        self.assertIsNone(inspect_orca_target_processes((lease,), records=({"process_id": "bad"},))["clients_closed"])
        with patch("local_agent_record_janitor.orca_target_safety._windows_process_snapshot", side_effect=OSError):
            self.assertIsNone(inspect_orca_target_processes((lease,))["clients_closed"])
        self.assertIsNone(inspect_orca_target_processes((replace(lease, claim_status="live"),), records=())["clients_closed"])

    def test_plan_running_observation_can_become_closed_without_identity_drift(self):
        evidence = self.freeze(process_records=({"process_id": 20, "parent_process_id": 0,
            "name": "Orca.exe", "start_time_ms": 2},))
        self.assertTrue(evidence["preflight_complete"])
        self.assertFalse(evidence["runtime_observation"]["clients_closed"])
        self.assertEqual(recheck_orca_target(evidence, self.adapter, process_records=()), ())

    def test_reference_to_required_descendant_protects_parent(self):
        evidence = freeze_orca_target(self.adapter, self.home, TARGET_ID,
            affected_ids=(TARGET_ID, CURRENT_ID), rollout_paths=(self.rollout,), binary=self.binary)
        self.assertIn("orca_target_retains_reference", evidence["blocker_codes"])

    def test_new_reference_new_hardlink_marker_and_binary_replacement_drift(self):
        for change in ("reference", "hardlink", "marker", "binary"):
            with self.subTest(change=change):
                evidence = self.freeze()
                if change == "reference":
                    record = make_record(self.home, "orca_fixture_new", empty=True)
                    record["providerHandleChain"] = [{"linkId": "target", "handle": {"provider": "codex", "threadId": TARGET_ID},
                        "origin": "created", "mintedAtFence": 2, "observedAt": 1700000000001}]
                    replace_record(self.profile, record)
                    self.assertIn("orca_target_retains_reference", recheck_orca_target(evidence, self.adapter))
                    record["providerHandleChain"] = []
                    replace_record(self.profile, record)
                elif change == "hardlink":
                    alias = self.root / "outside-link"
                    os.link(self.home / "state_5.sqlite", alias)
                    self.assertIn("orca_file_alias_unproven", recheck_orca_target(evidence, self.adapter))
                    alias.unlink()
                else:
                    path = self.binary if change == "binary" else self.home / ".orca-managed-home"
                    data = path.read_bytes()
                    saved = path.with_name(path.name + ".saved")
                    path.rename(saved)
                    path.write_bytes(data)
                    self.assertIn("orca_target_boundary_changed", recheck_orca_target(evidence, self.adapter))
                    path.unlink()
                    saved.rename(path)

    def test_shared_index_and_rollout_links_are_rejected(self):
        for path in (self.rollout, self.home / "session_index.jsonl"):
            path.touch(exist_ok=True)
            alias = self.root / "shared-file"
            os.link(path, alias)
            self.assertIn("orca_file_alias_unproven", self.freeze()["blocker_codes"])
            alias.unlink()

    def test_configuration_and_system_requirements_remain_blocked_without_reading_system(self):
        (self.home / "config.toml").write_text('sqlite_home="outside"', encoding="utf-8")
        self.assertIn("orca_storage_configuration_unverified", self.freeze()["blocker_codes"])
        system = self.root / "system-config"
        system.mkdir()
        path = system / "requirements.toml"
        path.write_text("PRIVATE_SYSTEM_SETTINGS", encoding="utf-8")
        with patch.object(Path, "open", side_effect=AssertionError("system config must not be read")):
            with self.assertRaises(ValueError):
                _absent_configuration(path)

    def test_configured_home_freezes_opaque_credentials_and_valid_startup_content(self):
        (self.home / "config.toml").write_text('model="example"\nmodel_reasoning_effort="high"', encoding="utf-8")
        (self.home / "installation_id").write_text("12345678-1234-4234-8234-123456789abc", encoding="ascii")
        for name in ("auth.json", ".credentials.json", "credentials.json"):
            (self.home / name).write_bytes(b"PRIVATE_CREDENTIAL_SENTINEL: deliberately invalid JSON")
        evidence = self.freeze()
        self.assertTrue(evidence["preflight_complete"], evidence)
        self.assertEqual(evidence["schema_version"], "larj.orca-target-safety.v2")
        self.assertNotIn("PRIVATE_CREDENTIAL_SENTINEL", json.dumps(evidence))
        self.assertEqual(recheck_orca_target(evidence, self.adapter), ())
        (self.home / "auth.json").write_bytes(b"different")
        self.assertIn("orca_target_boundary_changed", recheck_orca_target(evidence, self.adapter))

    def test_preexisting_startup_and_package_drift_fail_before_runtime_start(self):
        installation = self.home / "installation_id"
        installation.write_bytes(b"bad")
        self.assertIn("orca_installation_id_unverified", self.freeze()["blocker_codes"])
        installation.write_bytes(b"12345678-1234-4234-8234-123456789abc")
        evidence = self.freeze()
        installation.write_bytes(b"12345678-1234-4234-8234-123456789abd")
        self.assertIn("orca_target_boundary_changed", recheck_orca_target(evidence, self.adapter))
        self.assertTrue(recheck_orca_target(evidence, self.adapter, phase="post_start"))
        marker = self.home / "skills" / ".system" / ".codex-system-skills.marker"
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"unregistered bundled content")
        self.assertIn("orca_startup_content_unverified", self.freeze()["blocker_codes"])
        marker.unlink()
        (self.binary.parent / "codex-package.json").write_bytes(b"PRIVATE_PACKAGE_SENTINEL")
        self.assertIn("orca_system_configuration_unverified", self.freeze()["blocker_codes"])
        (self.binary.parent / "codex-package.json").unlink()
        (self.home / "environments.toml").write_bytes(b"PRIVATE_ENVIRONMENT_SENTINEL")
        self.assertIn("orca_system_configuration_unverified", self.freeze()["blocker_codes"])

    def test_legacy_evidence_remains_readonly_without_rehash_or_new_invocation(self):
        evidence = self.freeze(evidence_schema="larj.orca-target-safety.v1")
        before = json.dumps(evidence, sort_keys=True)
        self.assertEqual(recheck_orca_target(evidence, self.adapter), ("orca_runtime_policy_replan_required",))
        self.binary.unlink()
        self.assertEqual(recheck_orca_target(evidence, self.adapter, phase="readonly_recovery"), ())
        self.assertEqual(json.dumps(evidence, sort_keys=True), before)

    def test_legacy_unknown_child_retains_root_occupancy_and_cold_readonly_recovery(self):
        import uuid
        from local_agent_record_janitor.cleanup_service import partition_actions
        from local_agent_record_janitor.mutation_guard import scopes_for_frozen_plan
        from local_agent_record_janitor.orca_runtime import _WindowsJob, runtime_host_identity
        from tests.orca_native_support import create_native_schema, add_native_record
        (self.home / "state_5.sqlite").unlink()
        self.rollout.unlink()
        create_native_schema(self.home)
        self.rollout = add_native_record(self.home, TARGET_ID)
        coordinator = OperationCoordinator(CleanupService())
        path = self.root / "legacy-unknown.json"
        def legacy_freeze(*args, **kwargs):
            return freeze_orca_target(*args, **kwargs, evidence_schema="larj.orca-target-safety.v1")
        with patch("local_agent_record_janitor.orca_runtime.RUNTIME_ACCEPTED", True), \
                patch("local_agent_record_janitor.orca_target_safety.EVIDENCE_SCHEMA", "larj.orca-target-safety.v1"), \
                patch("local_agent_record_janitor.orca_target_safety.freeze_orca_target", side_effect=legacy_freeze):
            plan = coordinator.plan_operation(client="orca", record_ids=(TARGET_ID,), engines=("codex",),
                adapters=(self.adapter,), plan_path=path, operation_id="legacy-unknown")
        self.assertEqual(plan["goal_status"], "ready", plan)
        before = path.read_bytes()
        live = coordinator._live[plan["operation_id"]]
        batch = partition_actions(live.candidates)[0]
        child_id = plan["child_batches"][0]["child_operation_id"]
        store, _ = coordinator._open_batch_store(live, batch, child_id, self.home, 1)
        self.assertTrue(scopes_for_frozen_plan(store.read_plan()))
        job = _WindowsJob("Local\\larj-orca-" + uuid.uuid4().hex)
        instance = {"schema_version": "larj.orca-runtime-instance.v1", "job_name": job.name, **runtime_host_identity()}
        job.close()
        store.append_event({"event": "orca_runtime_startup", "mutation_started": True, "runtime_instance": instance},
            state_updates={"mutation_started": True, "runtime_instance": instance, "current_action_state": "mutation_started"})
        from local_agent_record_janitor.orca_target_safety import _startup_manifest
        # The legacy contract froze startup identity, without v2 content rules.
        (self.home / "installation_id").write_bytes(b"legacy fixture: v2 UUID validation must not be applied")
        snapshot = _startup_manifest(self.home, after_start=True, evidence_schema="larj.orca-target-safety.v1")
        store.append_event({"event": "orca_runtime_boundary_observed", "startup_artifacts": snapshot},
            state_updates={"startup_artifacts": snapshot})
        self.binary.unlink()
        result = OperationCoordinator(CleanupService()).verify_operation(operation_id=plan["operation_id"], plan_path=path)
        self.assertEqual(result["goal_status"], "completed_with_residuals", result)
        self.assertTrue(result["mutation_started"])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.rollout.exists())

    def test_disappearance_after_initial_observation_is_not_optional_absence(self):
        with patch("local_agent_record_janitor.orca_target_safety._identity", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                _optional_identity(self.binary, digest=True)

    def test_readonly_recovery_allows_approved_absence_and_sidecar_lifecycle_but_not_survivor_link(self):
        evidence = self.freeze()
        self.rollout.unlink()
        (self.home / "state_5.sqlite-wal").write_bytes(b"synthetic sidecar")
        self.assertEqual(recheck_orca_target(evidence, self.adapter, phase="readonly_recovery"), ())
        alias = self.root / "linked-db"
        os.link(self.home / "state_5.sqlite", alias)
        self.assertIn("orca_file_alias_unproven", recheck_orca_target(evidence, self.adapter, phase="readonly_recovery"))

    def test_real_cli_plan_freezes_selected_scope_hash_and_apply_requires_ack_then_rechecks(self):
        service = CleanupService(client_inspector=lambda *_: ())
        writer = Mock(side_effect=AssertionError("new closed capability reached writer"))
        output = StringIO()
        path = self.root / "plan.json"
        main(["delete", "plan", "--client", "orca", "--record-id", TARGET_ID,
              "--orca-root", str(self.profile), "--out", str(path), "--json"],
             stdout=output, stderr=StringIO(), cleanup_service=service, adapters=(self.adapter,))
        self.assertNotIn(SENTINEL, output.getvalue())
        plan = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(plan["schema_version"], "larj.operation-plan.v3", plan)
        self.assertEqual(plan["plan_sha256"], plan_sha256(plan))
        evidence = plan["target_safety_evidence"][0]
        self.assertEqual(evidence["frozen"]["rollout_paths"], [str(self.rollout)])
        self.assertEqual(evidence["frozen"]["affected_thread_ids"], [TARGET_ID])
        before = path.read_bytes()
        args = {"operation_id": plan["operation_id"], "plan_path": path, "app_server_factory": writer,
                "binary_resolver": writer, "adapters": (self.adapter,)}
        coordinator = OperationCoordinator(service)
        self.assertIn("clients_closed_ack_required", {item["blocker_code"]
            for item in coordinator.apply_operation(**args)["blockers"]})
        os.link(self.home / "state_5.sqlite", self.root / "new-link")
        result = coordinator.apply_operation(**args, clients_closed=True)
        self.assertIn("orca_file_alias_unproven", {item["blocker_code"] for item in result["blockers"]})
        self.assertFalse(result["mutation_started"])
        self.assertEqual(path.read_bytes(), before)
        writer.assert_not_called()

    def test_unrelated_native_plan_retains_v2_and_old_schema_cannot_carry_new_evidence(self):
        home = self.root / "independent"
        rollout = write_rollout(home, TARGET_ID, originator="codex_cli_rs", source="cli")
        create_thread_index(home, [{"id": TARGET_ID, "rollout_path": str(rollout), "source": "cli"}])
        plan = OperationCoordinator(CleanupService(client_inspector=lambda *_: ())).plan_operation(
            client="native", record_ids=(TARGET_ID,), adapters=(NativeIntegrityAdapter(codex_home=home), self.adapter),
            plan_path=self.root / "unrelated.json")
        self.assertEqual(plan["schema_version"], "larj.operation-plan.v2")
        self.assertNotIn("target_safety_evidence", plan)
        with self.assertRaises(ValueError):
            validate_document_targets({"schema_version": "larj.operation-plan.v2", "target_safety_evidence": [self.freeze()]})


if __name__ == "__main__":
    unittest.main()
