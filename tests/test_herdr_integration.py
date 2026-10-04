from __future__ import annotations

import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent_record_janitor.adapters import HerdrAdapter, NativeIntegrityAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from tests.herdr_support import CODEX_ID, SENTINEL, create_profile
from tests.support import create_thread_index, write_rollout


class HerdrIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        home = patch("pathlib.Path.home", return_value=self.root / "user-home")
        home.start(); self.addCleanup(home.stop)
        env = patch.dict(os.environ, {"ORCA_USER_DATA_PATH": "", "XDG_CONFIG_HOME": str(self.root / "config"),
            "APPDATA": str(self.root / "appdata"), "HOME": str(self.root / "user-home"),
            "CODEX_HOME": str(self.root / "native"), "PI_CODING_AGENT_DIR": str(self.root / "pi"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude")})
        env.start(); self.addCleanup(env.stop)
        self.profile = self.root / "explicit-profile"
        create_profile(self.profile)
        self.service = CleanupService(client_inspector=lambda *_: ())
        self.writer = Mock(side_effect=AssertionError("writer boundary reached"))

    def invoke(self, args, **kwargs):
        out = StringIO()
        code = main(args, stdout=out, stderr=StringIO(), cleanup_service=self.service,
            app_server_factory=self.writer, binary_resolver=self.writer, **kwargs)
        output = out.getvalue().strip()
        document, end = json.JSONDecoder().raw_decode(output)
        self.assertEqual(output[end:].strip(), "")
        self.assertNotIn(SENTINEL, output)
        return code, document

    def test_real_factory_default_dev_explicit_and_multi_root_candidates(self):
        release, development = self.root / "config" / "herdr", self.root / "config" / "herdr-dev"
        for root in (release, development):
            create_profile(root)
        code, default = self.invoke(["records", "--client", "herdr", "--json"])
        self.assertEqual(default["goal_status"], "complete")  # Requested persisted metadata only.
        self.assertEqual(code, 0)
        self.assertEqual(default["inventory_scope"], "persisted_metadata")
        self.assertTrue(all(error["blocks_delete"] for error in default["store_errors"]))
        self.assertTrue(all(not error["blocks_inventory"] for error in default["store_errors"]))
        self.assertEqual(len(default["targets"]), 14)
        code, explicit = self.invoke(["records", "--client", "herdr", "--herdr-root", str(self.profile), "--json"])
        self.assertEqual(len(explicit["targets"]), 7)
        self.assertEqual(explicit["shared_store_file_aliases"]["stores"], [])
        sources = {reference["source"] for target in explicit["targets"] for reference in target["references"]}
        self.assertTrue(all(Path(source).is_relative_to(self.profile) for source in sources))
        code, multiple = self.invoke(["records", "--client", "herdr", "--herdr-root", str(self.profile),
            "--herdr-root", str(development), "--json"])
        self.assertEqual(len(multiple["targets"]), 14)
        self.assertEqual(len({key for target in multiple["targets"] for key in target["frontend_binding_keys"]}), 14)
        self.writer.assert_not_called()

    def test_exact_restore_scope_keeps_profile_failures_and_other_profile_is_independent(self):
        broken = self.root / "broken-profile"
        create_profile(broken, mixed=True)
        first = HerdrAdapter(profile_root=self.profile)
        restore = next(r for r in first.snapshot_references().references if r.kind.value == "restore")
        code, selected = self.invoke(["records", "--client", "herdr", "--herdr-root", str(self.profile),
            "--herdr-root", str(broken), "--record-id", restore.binding_key, "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(selected["goal_satisfied"])
        self.assertEqual(len(selected["targets"]), 1)
        self.assertEqual(selected["targets"][0]["references"][0]["kind"], "restore")
        self.assertTrue(selected["store_errors"])
        self.assertEqual({e["profile_root"] for e in selected["store_errors"]}, {str(self.profile)})
        self.assertIn("live_metadata_not_probed", [e["message"] for e in selected["store_errors"]])
        self.assertIsNone(selected["targets"][0].get("record_key"))
        self.assertFalse(selected["targets"][0]["capability"]["verify"])

    def test_missing_bad_and_invalid_environment_sources_never_report_complete_empty(self):
        for root in (self.root / "missing", self.root / "not-a-directory"):
            if root.name == "not-a-directory":
                root.write_text(SENTINEL, encoding="utf-8")
            code, result = self.invoke(["records", "--client", "herdr", "--herdr-root", str(root), "--json"])
            self.assertNotEqual(code, 0)
            self.assertEqual(result["goal_status"], "blocked")
            self.assertTrue(result["store_errors"])
        os.environ["XDG_CONFIG_HOME"] = "relative"
        code, result = self.invoke(["records", "--client", "herdr", "--json"])
        self.assertNotEqual(code, 0)
        self.assertEqual(result["goal_status"], "blocked")
        self.assertIn("herdr_environment_root_unproven", [e["message"] for e in result["store_errors"]])

    def test_plan_run_and_fresh_queries_preserve_readonly_blocked_without_source_rebinding(self):
        plan_path = self.root / "herdr-plan.json"
        code, plan = self.invoke(["delete", "plan", "--client", "herdr", "--herdr-root", str(self.profile),
            "--record-id", CODEX_ID, "--out", str(plan_path), "--json"])
        self.assertNotEqual(code, 0)
        self.assertEqual(plan["goal_status"], "blocked")
        self.assertIn("client_capability_limit", {b["blocker_code"] for b in plan["blockers"]})
        self.assertEqual(plan["actions"], [])
        self.assertEqual(plan["child_batches"], [])
        self.assertEqual(plan["schema_version"], "larj.operation-plan.v1")
        self.assertNotIn("guard_sources", plan)
        before = plan_path.read_bytes()
        with patch.object(HerdrAdapter, "snapshot_references", side_effect=AssertionError("no authorized mutation to rebind")):
            for verb in ("status", "verify"):
                code, queried = self.invoke(["operation", verb, "--operation-id", plan["operation_id"], "--plan", str(plan_path), "--json"])
                self.assertEqual(queried["goal_status"], "blocked")
                self.assertEqual(queried["blockers"], plan["blockers"])
                self.assertEqual(queried["plan_sha256"], plan["plan_sha256"])
                self.assertFalse(queried["goal_satisfied"])
            code, applied = self.invoke(["delete", "apply", "--operation-id", plan["operation_id"], "--plan", str(plan_path),
                "--authorized-plan-sha256", plan["plan_sha256"], "--clients-closed", "--json"])
            self.assertEqual(applied["goal_status"], "blocked")
            self.assertFalse(applied["modified"])
            self.assertFalse(applied["mutation_started"])
        code, run = self.invoke(["delete", "run", "--client", "herdr", "--herdr-root", str(self.profile),
            "--record-id", CODEX_ID, "--clients-closed", "--out", str(self.root / "herdr-run.json"), "--json"])
        self.assertEqual(run["goal_status"], "blocked")
        self.assertEqual(plan_path.read_bytes(), before)
        self.writer.assert_not_called()

    def test_default_rootless_herdr_same_id_never_expands_or_blocks_native_candidates(self):
        default = self.root / "config" / "herdr"
        create_profile(default, mixed=True)
        home = self.root / "native"
        rollout = write_rollout(home, CODEX_ID, originator="codex_cli_rs", source="cli")
        create_thread_index(home, [{"id": CODEX_ID, "rollout_path": str(rollout), "source": "cli"}])
        code, records = self.invoke(["records", "--client", "native", "--codex-home", str(home), "--json"])
        self.assertEqual(code, 0, records)
        self.assertEqual(len(records["targets"]), 1)
        self.assertEqual(records["records"][0]["classification"], "healthy")
        self.assertTrue(records["targets"][0]["capability"]["native_delete"])
        native = NativeIntegrityAdapter(codex_home=home, codex_bin_hint=Path("synthetic-codex"))
        plan = OperationCoordinator(self.service).plan_operation(client="native", record_ids=(CODEX_ID,),
            adapters=(native, HerdrAdapter(profile_root=default)), plan_path=self.root / "native-plan.json")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual(len(plan["actions"]), 1)
        self.assertEqual(plan["schema_version"], "larj.operation-plan.v1")
        self.assertNotIn("guard_sources", plan)
        self.writer.assert_not_called()

    def test_empty_process_snapshot_is_unknown_for_real_herdr_owner_projection(self):
        with patch("local_agent_record_janitor.codex_desktop_state._running_related_process_records", return_value=()):
            code, result = self.invoke(["records", "--client", "herdr", "--herdr-root", str(self.profile), "--inspect-clients", "--json"])
        owner = result["client_ownership"][0]
        self.assertEqual(owner["owner_process_root"], str(self.profile))
        self.assertIsNone(owner["clients_closed"])
        self.assertFalse(owner["coverage_complete"])

    def test_public_coordinator_honors_explicit_root_without_adapter_or_default_profile(self):
        plan = OperationCoordinator(self.service).plan_operation(client="herdr", herdr_roots=(self.profile,),
            record_ids=(CODEX_ID,), plan_path=self.root / "direct-plan.json")
        self.assertEqual(plan["goal_status"], "blocked")
        self.assertIn("client_capability_limit", {b["blocker_code"] for b in plan["blockers"]})
        self.assertNotIn("record_not_found", {b["blocker_code"] for b in plan["blockers"]})
        self.writer.assert_not_called()

    def test_text_inventory_success_retains_visible_cleanup_restrictions(self):
        out, errors = StringIO(), StringIO()
        code = main(["records", "--client", "herdr", "--herdr-root", str(self.profile)],
                    stdout=out, stderr=errors, cleanup_service=self.service,
                    app_server_factory=self.writer, binary_resolver=self.writer)
        self.assertEqual(code, 0)
        self.assertIn("清理限制", errors.getvalue())
        self.assertIn("live_metadata_not_probed", errors.getvalue())
        self.writer.assert_not_called()
