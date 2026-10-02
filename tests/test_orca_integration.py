from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent_record_janitor.adapters import NativeIntegrityAdapter, OrcaAdapter
from local_agent_record_janitor.claude_delete import build_claude_delete_plan, execute_claude_delete
from local_agent_record_janitor.claude_sessions import build_claude_session_catalog
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.client_inventory import build_client_engine_contexts
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.execution import ExecutionError
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.operation_guard_sources import validate_guard_sources
from local_agent_record_janitor.operation_store import plan_sha256
from local_agent_record_janitor.record_identity import canonical_path
from tests.orca_support import CURRENT_ID, HISTORY_ID, SENTINEL, create_profile, make_record, replace_record
from tests.support import create_thread_index, write_rollout


class OrcaIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        fake_home = patch("pathlib.Path.home", return_value=self.root / "user-home")
        fake_home.start()
        self.addCleanup(fake_home.stop)
        self.profile = self.root / "orca"
        create_profile(self.profile, accounts=0)
        environment = patch.dict(os.environ, {
            "ORCA_USER_DATA_PATH": "", "APPDATA": str(self.root / "appdata"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "PI_CODING_AGENT_DIR": str(self.root / "pi-agent"),
            "PI_CODING_AGENT_SESSION_DIR": str(self.root / "pi-agent" / "sessions"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.service = CleanupService(client_inspector=lambda *_: ())
        self.writer = Mock(side_effect=AssertionError("product writer boundary reached"))

    def native(self, home, native_id=CURRENT_ID):
        path = write_rollout(home, native_id, originator="codex_cli_rs", source="cli")
        if not (home / "state_5.sqlite").exists():
            create_thread_index(home, [{"id": native_id, "rollout_path": str(path), "source": "cli"}])
        return NativeIntegrityAdapter(codex_home=home, codex_bin_hint=Path("synthetic-codex"))

    def invoke(self, argv, **kwargs):
        output = StringIO()
        code = main(argv, stdout=output, stderr=StringIO(), cleanup_service=self.service,
                    app_server_factory=self.writer, binary_resolver=self.writer, **kwargs)
        rendered = output.getvalue().strip()
        document, end = json.JSONDecoder().raw_decode(rendered)
        self.assertEqual(rendered[end:].strip(), "")  # Exactly one JSON document.
        self.assertNotIn(SENTINEL, rendered)
        return code, document

    def write_claude(self, config):
        path = config / "projects" / "project" / f"{CURRENT_ID}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sessionId": CURRENT_ID, "message": SENTINEL}) + "\n", encoding="utf-8")
        return path

    def claude_reference(self, config):
        record = make_record(config, "orca_claude_fixture")
        record.update(provider="claude", accountHome={"variable": "CLAUDE_CONFIG_DIR", "path": str(config)})
        record["providerHandleChain"] = [{"linkId": "claude_current", "handle": {
            "provider": "claude", "sessionId": CURRENT_ID, "leafUuid": None},
            "origin": "created", "mintedAtFence": 2, "observedAt": 1700000000001}]
        replace_record(self.profile, record)

    def test_factory_records_explicit_env_default_and_readonly_operation_routes(self):
        homes = create_profile(self.root / "populated")
        for home in homes:
            self.native(home)
        for discovery in ("explicit", "env", "default"):
            profile = self.root / "populated"
            args = ["records", "--client", "orca", "--json"]
            with self.subTest(discovery=discovery):
                if discovery == "explicit":
                    args += ["--orca-root", str(profile)]
                elif discovery == "env":
                    os.environ["ORCA_USER_DATA_PATH"] = str(profile)
                else:
                    os.environ["ORCA_USER_DATA_PATH"] = ""
                    os.environ["APPDATA"] = str(self.root / "default-appdata")
                    from local_agent_record_janitor.orca_discovery import default_orca_root
                    profile = default_orca_root()
                    default_homes = create_profile(profile, accounts=1)
                    self.native(default_homes[0])
                code, payload = self.invoke(args)
                self.assertEqual(code, 0, payload)
                current = [t for t in payload["targets"] if t["native_thread_id"] == CURRENT_ID]
                self.assertEqual(len(current), 1 if discovery == "default" else 2)
                self.assertTrue(all(t["classification"] == "healthy" and len(t["references"]) == 1 for t in current))
                self.assertTrue(all(t["action_ids"] == [] for t in payload["targets"]))
                self.assertFalse(payload["capabilities"]["codex"]["verify"])
        for command in ("plan", "run"):
            code, result = self.invoke(["delete", command, "--client", "orca", "--record-id", CURRENT_ID,
                "--orca-root", str(self.root / "populated"), "--out", str(self.root / f"readonly-{command}.json"),
                "--clients-closed", "--json"])
            self.assertEqual(result["goal_status"], "blocked", result)
            self.assertFalse(result.get("mutation_started", False))
        self.writer.assert_not_called()

    def test_native_account_reverse_marker_protects_all_public_paths_and_other_store_is_independent(self):
        home = create_profile(self.root / "managed", accounts=1)[0]
        native = self.native(home)
        from local_agent_record_janitor.inventory import build_session_catalog
        from local_agent_record_janitor.manual_delete import build_manual_delete_plan
        catalog = build_session_catalog((native,))
        plan = build_manual_delete_plan(catalog)
        self.assertFalse(any(action.available for action in plan.actions))
        from local_agent_record_janitor.cleaner import clean_findings
        from local_agent_record_janitor.manual_delete import _synthetic_finding
        report = clean_findings((_synthetic_finding(plan.actions[0]),), app_server_factory=self.writer,
                                binary_resolver=self.writer, explicit_selection=True)
        self.assertEqual(report.results[0].status, "not_deleted")
        self.assertIn("client_capability_limit", report.results[0].error)
        code, result = self.invoke(["delete", "plan", "--client", "native", "--record-id", CURRENT_ID,
            "--codex-home", str(home), "--out", str(self.root / "reverse.json"), "--json"])
        self.assertEqual(result["goal_status"], "blocked", result)
        unrelated = self.native(self.root / "unrelated")
        approved = OperationCoordinator(self.service).plan_operation(client="native", record_ids=(CURRENT_ID,),
            adapters=(unrelated, OrcaAdapter(profile_root=self.root / "managed")), plan_path=self.root / "unrelated.json")
        self.assertEqual(approved["goal_status"], "ready", approved)
        self.writer.assert_not_called()

    def test_scoped_unproven_store_failure_does_not_disappear_or_poison_other_account(self):
        homes = create_profile(self.root / "scoped")
        (homes[0] / ".orca-managed-home").write_text("wrong-account", encoding="utf-8")
        self.native(homes[1])
        adapter = OrcaAdapter(profile_root=self.root / "scoped")
        binding = next(r.binding_key for r in adapter.snapshot_references().references
                       if r.frontend_id == "orca_fixture_01" and r.native_id == CURRENT_ID)
        code, result = self.invoke(["records", "--client", "orca", "--record-id", binding, "--json"], adapters=(adapter,))
        self.assertNotEqual(code, 0)
        self.assertEqual(result["goal_status"], "blocked")
        self.assertTrue(result["store_errors"])
        target = result["targets"][0]
        self.assertIsNone(target.get("record_key"))
        self.assertIsNone(target["references"][0].get("native_record"))
        qualified = next(t.record_key.value for t in build_client_engine_contexts((adapter,), client="orca")[0].targets
                         if t.record_key and t.native_thread_id == CURRENT_ID)
        code, result = self.invoke(["records", "--client", "orca", "--record-id", qualified, "--json"], adapters=(adapter,))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["store_errors"], [])

    def test_current_guard_profile_does_not_expand_explicit_orca_candidates(self):
        explicit_homes = create_profile(self.root / "explicit", accounts=1)
        other_homes = create_profile(self.root / "current-env", accounts=1)
        for home in (*explicit_homes, *other_homes):
            self.native(home)
        os.environ["ORCA_USER_DATA_PATH"] = str(self.root / "current-env")
        from local_agent_record_janitor.inventory import _read_rollouts_partial
        with patch("local_agent_record_janitor.inventory._read_rollouts_partial", wraps=_read_rollouts_partial) as walk:
            approved = OperationCoordinator(self.service).plan_operation(client="orca", record_ids=(CURRENT_ID,),
                adapters=(OrcaAdapter(profile_root=self.root / "explicit"),), orca_roots=(self.root / "explicit",),
                plan_path=self.root / "candidate-scope.json")
        self.assertEqual([call.args[0] for call in walk.call_args_list], list(explicit_homes))
        self.assertEqual({s["profile_root"] for s in approved["guard_sources"]},
                         {canonical_path(self.root / "explicit"), canonical_path(self.root / "current-env")})
        before = Path(approved["plan_path"]).read_bytes()
        with patch("local_agent_record_janitor.inventory._read_rollouts_partial", side_effect=AssertionError("blocked plan has no authorized native mutation")):
            for method in ("status_operation", "verify_operation"):
                result = getattr(OperationCoordinator(self.service), method)(operation_id=approved["operation_id"],
                    plan_path=Path(approved["plan_path"]))
                self.assertEqual(result["goal_status"], "blocked")
                self.assertFalse(result["goal_satisfied"])
                self.assertEqual(result["blockers"], approved["blockers"])
                self.assertEqual(result["plan_sha256"], approved["plan_sha256"])
        self.assertEqual(Path(approved["plan_path"]).read_bytes(), before)

    def test_frozen_and_current_guard_sources_survive_same_and_fresh_apply_with_new_selector(self):
        other = self.root / "current-env"
        create_profile(other, accounts=0)
        replacement = self.root / "replacement"
        create_profile(replacement, accounts=0)
        native = self.native(other / "codex-runtime-home" / "home")
        coordinator = OperationCoordinator(self.service)
        approved = coordinator.plan_operation(client="native", record_ids=(CURRENT_ID,), adapters=(native,),
            orca_roots=(self.profile,), plan_path=self.root / "frozen.json")
        self.assertEqual(approved["goal_status"], "ready", approved)
        self.assertEqual(approved["schema_version"], "larj.operation-plan.v2")
        self.assertEqual(validate_guard_sources(approved), (Path(canonical_path(self.profile)),))
        self.assertEqual(len(approved["storages"]), 1)
        before = Path(approved["plan_path"]).read_bytes()
        for changed in ("frozen", "current"):
            # Only a persisted local accountHome associates the runtime store.
            profile = self.profile if changed == "frozen" else other
            if changed == "frozen":
                home = self.profile / "codex-runtime-home" / "home"
                native = self.native(home)
                coordinator = OperationCoordinator(self.service)
                approved = coordinator.plan_operation(client="native", record_ids=(CURRENT_ID,), adapters=(native,),
                    orca_roots=(self.profile,), plan_path=self.root / "frozen-runtime.json")
                before = Path(approved["plan_path"]).read_bytes()
            else:
                native = self.native(other / "codex-runtime-home" / "home")
                coordinator = OperationCoordinator(self.service)
                approved = coordinator.plan_operation(client="native", record_ids=(CURRENT_ID,), adapters=(native,),
                    orca_roots=(self.profile,), plan_path=self.root / "current-runtime.json")
                before = Path(approved["plan_path"]).read_bytes()
                os.environ["ORCA_USER_DATA_PATH"] = str(other)
            replace_record(profile, make_record(native.codex_home, "orca_new_association"))
            for runner in (coordinator, OperationCoordinator(self.service)):
                result = runner.apply_operation(operation_id=approved["operation_id"], plan_path=Path(approved["plan_path"]),
                    scope=approved["scope"], plan_sha256=approved["plan_sha256"], adapters=(native,),
                    orca_roots=(replacement,), clients_closed=True, app_server_factory=self.writer, binary_resolver=self.writer)
                self.assertEqual(result["goal_status"], "blocked", result)
                self.assertFalse(result["mutation_started"])
                verified = runner.verify_operation(operation_id=approved["operation_id"], plan_path=Path(approved["plan_path"]),
                    adapters=(native,), orca_roots=(replacement,), verify_timeout=0)
                self.assertEqual(verified["goal_status"], "completed_with_residuals", verified)
                self.assertEqual(Path(approved["plan_path"]).read_bytes(), before)
        self.writer.assert_not_called()

    def test_supplied_native_account_marker_freezes_profile_for_an_unrelated_runtime_target(self):
        account = create_profile(self.root / "custom", accounts=1)[0]
        account_native = self.native(account, HISTORY_ID)
        runtime = self.native(self.root / "custom" / "codex-runtime-home" / "home")
        coordinator = OperationCoordinator(self.service)
        approved = coordinator.plan_operation(client="native", record_ids=(CURRENT_ID,),
            adapters=(account_native, runtime), plan_path=self.root / "reverse-marker.json")
        self.assertEqual(approved["goal_status"], "ready", approved)
        self.assertEqual(approved["schema_version"], "larj.operation-plan.v2")
        self.assertEqual(validate_guard_sources(approved), (Path(canonical_path(self.root / "custom")),))
        before = Path(approved["plan_path"]).read_bytes()
        replace_record(self.root / "custom", make_record(runtime.codex_home, "orca_runtime_association"))
        result = OperationCoordinator(self.service).apply_operation(operation_id=approved["operation_id"],
            plan_path=Path(approved["plan_path"]), scope=approved["scope"], plan_sha256=approved["plan_sha256"],
            adapters=(runtime,), clients_closed=True, app_server_factory=self.writer)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["mutation_started"])
        self.assertEqual(Path(approved["plan_path"]).read_bytes(), before)
        self.writer.assert_not_called()

    def test_missing_frozen_source_blocks_unattempted_apply_and_unknown_verify(self):
        native = self.native(self.root / "native")
        coordinator = OperationCoordinator(self.service)
        approved = coordinator.plan_operation(client="native", record_ids=(CURRENT_ID,), adapters=(native,),
            orca_roots=(self.profile,), plan_path=self.root / "missing-source.json")
        before = Path(approved["plan_path"]).read_bytes()
        (self.profile / "agent-session-journal.db").unlink()
        result = OperationCoordinator(self.service).apply_operation(operation_id=approved["operation_id"],
            plan_path=Path(approved["plan_path"]), scope=approved["scope"], plan_sha256=approved["plan_sha256"],
            adapters=(native,), clients_closed=True, app_server_factory=self.writer)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertIn("guard_source_incomplete", {b["blocker_code"] for b in result["blockers"]})
        verified = coordinator.verify_operation(operation_id=approved["operation_id"], plan_path=Path(approved["plan_path"]),
            adapters=(native,), verify_timeout=0)
        self.assertEqual(verified["goal_status"], "unknown")
        self.assertFalse(verified["mutation_started"])
        self.assertEqual(Path(approved["plan_path"]).read_bytes(), before)
        self.writer.assert_not_called()

    def test_new_current_profile_failure_cannot_become_an_empty_v2_guard(self):
        native = self.native(self.root / "native")
        coordinator = OperationCoordinator(self.service)
        approved = coordinator.plan_operation(client="native", record_ids=(CURRENT_ID,), adapters=(native,),
            orca_roots=(self.profile,), plan_path=self.root / "new-current-failure.json")
        os.environ["ORCA_USER_DATA_PATH"] = str(self.root / "missing-current-env")
        for runner in (coordinator, OperationCoordinator(self.service)):
            result = runner.apply_operation(operation_id=approved["operation_id"], plan_path=Path(approved["plan_path"]),
                scope=approved["scope"], plan_sha256=approved["plan_sha256"], adapters=(native,),
                clients_closed=True, app_server_factory=self.writer)
            self.assertEqual(result["goal_status"], "blocked", result)
            self.assertFalse(result["mutation_started"])
            self.assertIn("guard_source_incomplete", {b["blocker_code"] for b in result["blockers"]})
        self.writer.assert_not_called()

    def test_legacy_v1_new_source_requires_new_plan_without_changing_approval(self):
        native = self.native(self.root / "native")
        approved = OperationCoordinator(self.service).plan_operation(client="native", record_ids=(CURRENT_ID,),
            adapters=(native,), plan_path=self.root / "old-v1.json")
        self.assertEqual(approved["schema_version"], "larj.operation-plan.v1")
        before = Path(approved["plan_path"]).read_bytes()
        result = OperationCoordinator(self.service).apply_operation(operation_id=approved["operation_id"],
            plan_path=Path(approved["plan_path"]), scope=approved["scope"], plan_sha256=approved["plan_sha256"],
            adapters=(native,), orca_roots=(self.profile,), clients_closed=True, app_server_factory=self.writer)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertIn("missing_guard_source_evidence", {b["blocker_code"] for b in result["blockers"]})
        self.assertEqual(Path(approved["plan_path"]).read_bytes(), before)
        self.assertEqual(plan_sha256(approved), approved["plan_sha256"])
        self.writer.assert_not_called()

    def test_claude_exact_root_is_readonly_in_public_records_service_and_raw_writer(self):
        config = self.root / "claude"
        path = self.write_claude(config)
        self.claude_reference(config)
        os.environ["ORCA_USER_DATA_PATH"] = str(self.profile)
        code, result = self.invoke(["records", "--client", "claude", "--claude-config-dir", str(config), "--json"])
        self.assertFalse(result["targets"][0]["capability"]["native_delete"], result)
        self.assertEqual(result["targets"][0]["action_ids"], [])
        builder = lambda: build_claude_session_catalog(config_dir=config)
        context = self.service.prepare_session_catalog("claude", builder(), catalog_builder=builder)
        self.assertFalse(context.plan.actions[0].available)
        with self.assertRaises(ExecutionError):
            self.service.execute(context, context.plan.actions, timeout=0, app_server_factory=self.writer,
                binary_resolver=self.writer, session_executor=self.writer)
        selected = build_claude_delete_plan(builder()).with_selected_actions((CURRENT_ID,))
        result = execute_claude_delete(selected, catalog_builder=builder,
            approved_plan_fingerprint=selected.plan_fingerprint, clients_closed=True, unlink_fn=self.writer)
        self.assertEqual(result.results[0].status, "not_deleted")
        self.assertIn("client_capability_limit", result.results[0].error or "")
        self.assertTrue(path.is_file())
        self.writer.assert_not_called()

    def test_cached_claude_service_context_refreshes_new_same_profile_association(self):
        config = self.root / "claude"
        path = self.write_claude(config)
        reader = OrcaAdapter(profile_root=self.profile)
        builder = lambda: build_claude_session_catalog(config_dir=config)
        context = self.service.prepare_session_catalog("claude", builder(), catalog_builder=builder, active_adapters=(reader,))
        self.assertTrue(context.plan.actions[0].available)
        self.claude_reference(config)
        with self.assertRaises(ExecutionError) as caught:
            self.service.execute(context, context.plan.actions, timeout=0, app_server_factory=self.writer,
                binary_resolver=self.writer, session_executor=self.writer)
        self.assertEqual(caught.exception.kind, "client_capability_limit")
        self.assertTrue(path.is_file())
        self.writer.assert_not_called()

    def test_standalone_pi_claude_keep_catalogs_with_unrelated_env_and_explicit_guards(self):
        pi = self.root / "pi-agent" / "sessions" / "fixture.jsonl"
        pi.parent.mkdir(parents=True)
        pi.write_text(json.dumps({"type": "session", "version": 3, "id": "pi-fixture", "cwd": str(self.root)}) + "\n", encoding="utf-8")
        self.write_claude(self.root / "claude")
        for discovery in ("env", "explicit"):
            os.environ["ORCA_USER_DATA_PATH"] = str(self.profile) if discovery == "env" else ""
            for engine in ("pi", "claude"):
                args = ["records", "--client", engine, "--json"]
                if discovery == "explicit":
                    args += ["--orca-root", str(self.profile)]
                code, result = self.invoke(args)
                self.assertEqual(code, 0, result)
                self.assertEqual(len(result["targets"]), 1)
                self.assertTrue(result["targets"][0]["capability"]["native_delete"])

    def test_legacy_agent_does_not_drop_new_protection_sources_between_plan_and_apply(self):
        config = self.root / "claude"
        path = self.write_claude(config)
        plan_path = self.root / "agent-v1.json"
        code, summary = self.invoke(["agent", "plan", "--platform", "claude", "--claude-config-dir", str(config),
            "--session-id", CURRENT_ID, "--out", str(plan_path)])
        self.assertTrue(summary["authorization_required"], summary)
        before = plan_path.read_bytes()
        selected = json.loads(before)
        code, applied = self.invoke(["agent", "apply", "--plan", str(plan_path), "--authorized-plan-sha256",
            selected["plan_sha256"], "--clients-closed", "--orca-root", str(self.profile)])
        self.assertEqual(applied["goal_status"], "blocked", applied)
        self.assertIn("missing_guard_source_evidence", {b["blocker_code"] for b in applied["blockers"]})
        code, new_summary = self.invoke(["agent", "plan", "--platform", "claude", "--claude-config-dir", str(config),
            "--session-id", CURRENT_ID, "--orca-root", str(self.profile), "--out", str(self.root / "agent-with-guards.json")])
        self.assertFalse(new_summary["authorization_required"])
        self.assertIn("missing_guard_source_evidence", {b["blocker_code"] for b in new_summary["blockers"]})
        self.assertEqual(plan_path.read_bytes(), before)
        self.assertTrue(path.is_file())
        self.writer.assert_not_called()

    def test_guard_source_location_gate_precedes_path(self):
        base = {"schema_version": "larj.operation-plan.v2", "guard_sources": [{"client": "orca",
            "profile_root": str(self.profile), "host": "remote", "path_namespace": "local"}]}
        with patch("local_agent_record_janitor.orca_discovery.Path", side_effect=AssertionError("foreign Path")):
            with self.assertRaises(ValueError):
                validate_guard_sources(base)

    def test_reader_exception_after_complete_child_keeps_durable_mutation_facts(self):
        from local_agent_record_janitor.cleanup_service import partition_actions
        natives = (self.native(self.root / "one"), self.native(self.root / "two", HISTORY_ID))
        coordinator = OperationCoordinator(self.service)
        approved = coordinator.plan_operation(client="native", record_ids=(CURRENT_ID, HISTORY_ID), adapters=natives,
            orca_roots=(self.profile,), plan_path=self.root / "partial-rebind.json")
        self.assertEqual(approved["goal_status"], "ready", approved)
        live = coordinator._live[approved["operation_id"]]
        batch = partition_actions(live.candidates)[0]
        child_id = approved["child_batches"][0]["child_operation_id"]
        root = coordinator._storage_path(live.context, batch.storage_id, batch.actions[0])
        store, _ = coordinator._open_batch_store(live, batch, child_id, root, 1)
        store.append_event({"event": "mutation_started"}, state_updates={"phase": "executing",
            "mutation_started": True, "current_action_state": "mutation_started"})
        store.append_event({"event": "batch_finished", "goal_status": "complete"}, state_updates={
            "phase": "finished", "goal_status": "complete", "goal_satisfied": True,
            "modified": True, "mutation_started": True, "current_action_state": "verified"})
        before = Path(approved["plan_path"]).read_bytes()
        reader = OrcaAdapter(profile_root=self.profile)
        with patch.object(reader, "describe_client", side_effect=OSError("synthetic reader unavailable")):
            result = OperationCoordinator(self.service).apply_operation(operation_id=approved["operation_id"],
                plan_path=Path(approved["plan_path"]), scope=approved["scope"], plan_sha256=approved["plan_sha256"],
                adapters=(*natives, reader), clients_closed=True, app_server_factory=self.writer)
        self.assertEqual(result["goal_status"], "unknown", result)
        self.assertTrue(result["modified"])
        self.assertTrue(result["mutation_started"])
        self.assertEqual(result["batches"][0]["status"], "complete")
        self.assertEqual(Path(approved["plan_path"]).read_bytes(), before)
        self.writer.assert_not_called()
