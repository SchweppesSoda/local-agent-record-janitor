from __future__ import annotations

from io import StringIO
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from local_agent_record_janitor.cli import main
from local_agent_record_janitor.client_inventory import build_client_inventory, build_client_engine_contexts
from local_agent_record_janitor.paseo_store import PaseoAdapter, build_inventory, default_root

PRIVATE = "PASEO_PRIVATE_PROMPT_CREDENTIAL_AND_BODY_SENTINEL"
STAMP = "2026-10-04T00:00:00.000Z"


class PaseoStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.home = self.root / ".paseo"
        self.home.mkdir()
        (self.home / "agents").mkdir()
        environment = {"APPDATA": str(self.root / "appdata"), "XDG_CONFIG_HOME": str(self.root / "appdata"),
            "PASEO_HOME": str(self.home), "ORCA_USER_DATA_PATH": "", "HERDR_CONFIG_DIR": str(self.root / "absent-herdr"),
            "CODEX_HOME": str(self.root / "absent-codex"), "CLAUDE_CONFIG_DIR": str(self.root / "absent-claude"),
            "PI_CODING_AGENT_DIR": str(self.root / "absent-pi")}
        for patcher in (patch("pathlib.Path.home", return_value=self.root), patch.dict(os.environ, environment)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.writer = Mock(side_effect=AssertionError("No native writer may run for Paseo"))

    def record(self, identifier="agent-1", provider="codex", **changes):
        return {"id": identifier, "provider": provider, "cwd": str(self.root / "workspace"),
            "createdAt": STAMP, "updatedAt": STAMP, "title": PRIVATE,
            "labels": {"private": PRIVATE}, "config": {"systemPrompt": PRIVATE, "mcpServers": {"private": PRIVATE}},
            "lastError": PRIVATE, "lastStatus": "closed", "persistence": {
                "provider": provider, "sessionId": "native-1", "nativeHandle": "native-1", "metadata": {"private": PRIVATE}},
            "runtimeInfo": {"provider": provider, "sessionId": "native-1", "extra": {"private": PRIVATE}}, **changes}

    def write(self, value=None, *, relative="project/agent-1.json", home=None):
        path = (home or self.home) / "agents" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value or self.record()), encoding="utf-8")
        return path

    def invoke(self, *args):
        out, errors = StringIO(), StringIO()
        code = main(list(args), stdout=out, stderr=errors, app_server_factory=self.writer, binary_resolver=self.writer)
        value = json.loads(out.getvalue())
        self.assertNotIn(PRIVATE, out.getvalue() + errors.getvalue())
        self.writer.assert_not_called()
        return code, value

    def test_current_archived_legacy_and_unbound_agents_are_visible_without_private_fields(self):
        paths = [self.write(), self.write(self.record("archived", "claude", archivedAt=STAMP), relative="other/archived.json"),
            self.write(self.record("legacy", persistence=None, runtimeInfo={"provider": "codex", "sessionId": None}), relative="legacy.json"),
            self.write(self.record("custom", "my-provider", persistence={"provider": "my-provider", "sessionId": "custom-session",
                "nativeHandle": {"prompt": PRIVATE}}), relative="project/custom.json")]
        transcript = self.home / "agents" / "project" / "native-1.jsonl"
        transcript.write_text(PRIVATE, encoding="utf-8")
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (*paths, transcript)}
        original_open = Path.open
        def metadata_only(path, *args, **kwargs):
            self.assertNotEqual(path, transcript)
            return original_open(path, *args, **kwargs)
        with patch("pathlib.Path.open", metadata_only):
            code, payload = self.invoke("records", "--client", "paseo", "--json", "--inspect-clients")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["inventory_scope"], "persisted_agent_registry")
        self.assertEqual(payload["count"], 4)
        targets = {row["record_id"]: row for row in payload["targets"]}
        self.assertEqual(targets["archived"]["record_metadata"]["snapshots"][0]["archivedAt"], STAMP)
        self.assertEqual(targets["custom"]["engine"], "unsupported:my-provider")
        self.assertIsNone(targets["agent-1"].get("native_thread_id"))
        self.assertEqual(len(targets["agent-1"]["references"]), 3)
        self.assertIsNone(payload["client_ownership"][0]["clients_closed"])
        for row in targets.values():
            self.assertEqual(row["record_key"]["store"]["backend"], "paseo")
            self.assertFalse(row["capability"]["verify"])
            self.assertFalse(row["capability"]["native_delete"])
            self.assertFalse(row["cleanup_eligible"])
            self.assertEqual(row["action_ids"], [])
            self.assertTrue(all(ref.get("native_record") is None for ref in row.get("references", ())))
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})

    def test_native_generated_registry_fixture_matches_public_inventory(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "paseo_agent_registry_0110.json").read_text(encoding="utf-8"))
        for sample in fixture["records"]:
            record = dict(sample["record"], cwd=str(self.root / "workspace"))
            prefix = "" if sample["layout"] == "legacy" else "project/"
            self.write(record, relative=prefix + record["id"] + ".json")
        code, result = self.invoke("records", "--client", "paseo", "--json")
        self.assertEqual(code, 0, result)
        self.assertEqual({row["record_id"] for row in result["targets"]}, {sample["record"]["id"] for sample in fixture["records"]})
        self.assertTrue(all(row["record_metadata"]["upstream_commit"] == fixture["upstream_commit"] for row in result["targets"]))

    def test_exact_parent_label_and_pi_path_are_observations_not_native_ownership(self):
        transcript = self.root / "foreign-provider" / "session.jsonl"
        transcript.parent.mkdir()
        transcript.write_text(PRIVATE, encoding="utf-8")
        self.write(self.record(provider="pi", labels={"private": PRIVATE, "paseo.parent-agent-id": "parent"},
            persistence={"provider": "pi", "sessionId": "pi-session", "nativeHandle": str(transcript)}))
        adapter = PaseoAdapter(profile_root=self.home)
        inventory = build_client_inventory((adapter,), client="paseo")
        self.assertEqual(inventory.targets[0].record_metadata["snapshots"][0]["parent_agent_id"], "parent")
        self.assertTrue(inventory.targets[0].is_subagent)
        self.assertEqual(inventory.targets[0].lineage_status, "unknown")
        self.assertEqual(inventory.targets[0].parent_thread_ids, ())
        paths = [ref for ref in inventory.references if ref.source_locator == "persistence/nativeHandle"]
        self.assertEqual(paths[0].opaque_native_locator, str(transcript))
        self.assertIsNone(paths[0].native_record)
        self.assertIsNone(paths[0].native_id)
        self.assertEqual(adapter.describe_client().native_stores, ())
        contexts = build_client_engine_contexts((adapter,), client="paseo", inventory=inventory)
        self.assertIsNone(contexts[0].native_catalog)

    def test_custom_provider_spelling_survives_engine_context_normalization(self):
        self.write(self.record(provider="Corp_Model"))
        code, result = self.invoke("records", "--client", "paseo", "--json")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["targets"][0]["engine"], "unsupported:corp-model")
        self.assertEqual(result["targets"][0]["record_metadata"]["snapshots"][0]["provider"], "Corp_Model")
        code, filtered = self.invoke("records", "--client", "paseo", "--engine", "unsupported:corp-model", "--json")
        self.assertEqual(code, 0, filtered)
        self.assertEqual(filtered["count"], 1)

    def test_same_id_multiple_profiles_isolated_and_case_alias_deduplicated(self):
        self.write()
        second = self.root / "second"
        self.write(home=second)
        alias = Path(str(self.home).upper()) if os.name == "nt" else self.home
        code, result = self.invoke("records", "--client", "paseo", "--paseo-root", str(self.home),
            "--paseo-root", str(alias), "--paseo-root", str(second), "--json")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["count"], 2)
        self.assertEqual(len({row["record_key"]["value"] for row in result["targets"]}), 2)

    def test_duplicates_preserve_every_source_and_provider_reference_without_choosing_winner(self):
        self.write()
        self.write(self.record(cwd=str(self.root / "elsewhere"), persistence={"provider": "claude", "sessionId": "other-native"}),
            relative="legacy.json")
        code, result = self.invoke("records", "--client", "paseo", "--json")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["count"], 1)
        row = result["targets"][0]
        self.assertEqual(row["record_metadata"]["snapshot_count"], 2)
        self.assertIsNone(row.get("project_key"))
        self.assertEqual({ref["native_id"] for ref in row["references"]}, {"native-1", "other-native"})
        codes = {error["message"] for error in result["errors"]}
        self.assertIn("paseo_duplicate_agent_snapshots", codes)
        self.assertIn("paseo_provider_reference_conflict", codes)

    def test_corrupt_shape_and_private_json_errors_never_become_empty_success(self):
        healthy = self.write(self.record("healthy"), relative="healthy.json")
        bad = self.home / "agents" / "bad.json"
        cases = [PRIVATE, '{"id":"' + PRIVATE + '",', '{"id":"x","id":"' + PRIVATE + '"}',
            json.dumps(self.record(id=[])), json.dumps(self.record(lastStatus="future-state")),
            json.dumps(self.record(runtimeInfo={"provider": "codex"})),
            json.dumps(self.record(runtimeInfo=None)), json.dumps(self.record(schemaVersion=2)),
            json.dumps(self.record(persistence={"provider": "codex", "sessionId": None}))]
        for data in cases:
            with self.subTest(data=data[:30]):
                bad.write_text(data, encoding="utf-8")
                code, payload = self.invoke("records", "--client", "paseo", "--json")
                self.assertNotEqual(code, 0)
                self.assertEqual(payload["goal_status"], "blocked")
                self.assertEqual([row["record_id"] for row in payload["targets"]], ["healthy"])
        self.assertTrue(healthy.exists())

    def test_missing_empty_and_unknown_nested_registry_have_distinct_results(self):
        code, result = self.invoke("records", "--client", "paseo", "--json")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["count"], 0)
        code, result = self.invoke("records", "--client", "paseo", "--paseo-root", str(self.root / "missing"), "--json")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["goal_status"], "blocked")
        self.write(relative="nested/deeper/hidden.json")
        code, result = self.invoke("records", "--client", "paseo", "--json")
        self.assertNotEqual(code, 0)
        self.assertIn("paseo_nested_registry_not_covered", {row["message"] for row in result["errors"]})

    def test_unpublished_temp_files_ignored_and_budget_limits_reported(self):
        path = self.write()
        path.with_suffix(".json.tmp").write_text(PRIVATE, encoding="utf-8")
        code, result = self.invoke("records", "--client", "paseo", "--json")
        self.assertEqual(code, 0, result)
        for setting, limit in (("MAX_FILE_BYTES", 20), ("MAX_TOTAL_BYTES", 20), ("MAX_ENTRIES", 1)):
            with self.subTest(setting=setting), patch("local_agent_record_janitor.paseo_store." + setting, limit):
                code, result = self.invoke("records", "--client", "paseo", "--json")
                self.assertNotEqual(code, 0)
                self.assertIn("paseo_inventory_budget_exceeded", {row["message"] for row in result["errors"]})

    def test_growing_failed_reads_still_consume_aggregate_byte_budget(self):
        import local_agent_record_janitor.paseo_store as store
        path = self.write()
        size = path.stat().st_size
        original_open = Path.open
        class GrowingReader:
            def __enter__(inner):
                inner.file = original_open(path, "rb")
                return inner
            def __exit__(inner, *args):
                inner.file.close()
            def fileno(inner):
                return inner.file.fileno()
            def read(inner, count):
                self.assertLessEqual(count, size + 11)
                with original_open(path, "ab") as writer:
                    writer.write(b" " * 100)
                return inner.file.read(count)
        budget = [0, 0]
        with patch.object(store, "MAX_TOTAL_BYTES", size + 10), patch("pathlib.Path.open", return_value=GrowingReader()):
            with self.assertRaisesRegex(ValueError, "paseo_inventory_budget_exceeded"):
                store._read(path, budget)
        self.assertEqual(budget[0], size + 11)

    def test_selected_client_only_env_override_and_explicit_precedence(self):
        self.write()
        self.assertEqual(default_root(), self.home)
        with patch.dict(os.environ, {"PASEO_HOME": "relative-paseo-home"}):
            self.assertEqual(default_root(), Path("relative-paseo-home").absolute())
        with patch.dict(os.environ, {"PASEO_HOME": "https://remote.invalid/store"}):
            code, result = self.invoke("records", "--client", "paseo", "--paseo-root", str(self.home), "--json")
            self.assertEqual(code, 0, result)
            with self.assertRaisesRegex(ValueError, "paseo_environment_root_unproven"):
                default_root()
        with patch("local_agent_record_janitor.paseo_store.PaseoAdapter.snapshot_references",
                   side_effect=AssertionError("unselected default must not be read")):
            self.invoke("records", "--client", "native", "--json")

    def test_fstat_ctime_difference_does_not_misreport_stable_windows_file(self):
        from types import SimpleNamespace
        import local_agent_record_janitor.paseo_store as store
        path = self.write()
        original = os.fstat
        def alternate_ctime(fd):
            info = original(fd)
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns + 10000)
        with patch.object(store.os, "fstat", alternate_ctime):
            row = store._read(path, [0, 0])
        self.assertEqual(row["id"], "agent-1")

    def test_linked_roots_and_records_rejected_before_open(self):
        path = self.write()
        link = self.root / "linked"
        try:
            link.symlink_to(self.home, target_is_directory=True)
        except OSError:
            # Windows junctions are tested without developer-mode symlink privileges.
            if os.name != "nt":
                raise
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.home)], check=True, capture_output=True)
        self.addCleanup(lambda: link.rmdir() if link.exists() and os.name == "nt" and not link.is_symlink() else link.unlink(missing_ok=True))
        code, result = self.invoke("records", "--client", "paseo", "--paseo-root", str(link), "--json")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["count"], 0)
        self.assertIn("paseo_path_redirected_or_special", {row["message"] for row in result["errors"]})
        self.assertTrue(path.exists())

    def test_source_drift_and_permission_failure_reported_without_private_details(self):
        path = self.write()
        import local_agent_record_janitor.paseo_store as store
        original = store._plain
        calls = 0
        def changed(candidate, **kwargs):
            nonlocal calls
            info = original(candidate, **kwargs)
            if candidate == path:
                calls += 1
                if calls == 2:
                    raise store.PaseoInventoryError("paseo_snapshot_changed")
            return info
        with patch.object(store, "_plain", changed):
            code, result = self.invoke("records", "--client", "paseo", "--json")
        self.assertNotEqual(code, 0)
        self.assertIn("paseo_snapshot_changed", {row["message"] for row in result["errors"]})
        with patch.object(store, "_entries", side_effect=PermissionError(PRIVATE)):
            code, result = self.invoke("records", "--client", "paseo", "--json")
        self.assertNotEqual(code, 0)
        self.assertIn("paseo_registry_unreadable", {row["message"] for row in result["errors"]})

    def test_plan_run_apply_status_verify_remain_blocked_and_sources_unchanged(self):
        source = self.write()
        before = source.read_bytes()
        plan_path = self.root / "paseo-plan.json"
        code, plan = self.invoke("delete", "plan", "--client", "paseo", "--paseo-root", str(self.home),
            "--record-id", "agent-1", "--out", str(plan_path), "--json")
        self.assertNotEqual(code, 0)
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertEqual(plan["actions"], [])
        self.assertIn("client_capability_limit", {row["blocker_code"] for row in plan["blockers"]})
        frozen = plan_path.read_bytes()
        for verb in ("status", "verify"):
            _, result = self.invoke("operation", verb, "--operation-id", plan["operation_id"], "--plan", str(plan_path), "--json")
            self.assertEqual(result["goal_status"], "blocked", result)
        _, applied = self.invoke("delete", "apply", "--operation-id", plan["operation_id"], "--plan", str(plan_path),
            "--authorized-plan-sha256", plan["plan_sha256"], "--clients-closed", "--json")
        self.assertEqual(applied["goal_status"], "blocked")
        self.assertFalse(applied["mutation_started"])
        self.assertEqual(plan_path.read_bytes(), frozen)
        _, run = self.invoke("delete", "run", "--client", "paseo", "--paseo-root", str(self.home), "--record-id", "agent-1",
            "--clients-closed", "--out", str(self.root / "run.json"), "--json")
        self.assertEqual(run["goal_status"], "blocked")
        self.assertEqual(run["actions"], [])
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
