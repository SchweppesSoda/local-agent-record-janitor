"""Frozen v1 wire formats, independently of the current plan generator."""

from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.adapters import NativeIntegrityAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.operation_store import (
    OperationStore,
    _receipt_sha256,
    plan_sha256,
)
from local_agent_record_janitor.planning import storage_id_for_path
from tests.support import create_thread_index


FIXTURE = Path(__file__).parent / "fixtures" / "operation_v1.json"


class PersistedV1CompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.samples = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "store"
        self.home.mkdir()
        create_thread_index(self.home, [])

    def relocate(self, name: str) -> dict:
        # Only this fixture harness relocates paths/bindings. Production
        # readers must return the persisted document and its hash unchanged.
        original = self.samples[name]
        self.assertEqual(plan_sha256(original), original["plan_sha256"])
        text = json.dumps(original)
        text = text.replace("$STORE_ROOT", self.home.as_posix())
        text = text.replace("$FIXTURE_ROOT", self.root.as_posix())
        old_store_id = original.get("target", {}).get("storage_id")
        if old_store_id:
            text = text.replace(old_store_id, storage_id_for_path(self.home))
        result = json.loads(text)
        result["plan_sha256"] = plan_sha256(result)
        return result

    def invoke(self, argv: tuple[str, ...]) -> dict:
        output = StringIO()
        with patch(
            "local_agent_record_janitor.agent_cli.running_related_clients",
            return_value=(),
        ):
            main(
                argv,
                adapters=(NativeIntegrityAdapter(codex_home=self.home),),
                cleanup_service=CleanupService(client_inspector=lambda *_: ()),
                stdout=output,
                stderr=StringIO(),
                app_server_factory=lambda **_: self.fail("old mutation must not be replayed"),
                binary_resolver=lambda _: Path("synthetic-codex"),
            )
        return json.loads(output.getvalue())

    def test_fixed_plans_read_without_enrichment_or_hash_rewrite(self) -> None:
        plan = self.relocate("agent_plan")
        store = OperationStore(self.home, plan["operation_id"])
        store.accept_plan(plan)
        before = store.plan_path.read_bytes()
        self.assertEqual(store.read_plan(), plan)
        self.assertEqual(store.plan_path.read_bytes(), before)

        operation = self.relocate("operation_plan")
        path = self.root / "operation.json"
        path.write_text(json.dumps(operation), encoding="utf-8")
        coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        before = path.read_bytes()
        self.assertEqual(
            coordinator._load_plan(operation["operation_id"], path, operation["plan_sha256"]),
            operation,
        )
        self.assertEqual(path.read_bytes(), before)

    def test_fixed_states_remain_diagnostic_and_started_mutations_are_not_replayed(self) -> None:
        for name in ("unstarted", "mutation_started", "unknown", "partial"):
            with self.subTest(state=name):
                plan = self.relocate("agent_plan")
                plan["operation_id"] += "-" + name
                plan["plan_sha256"] = plan_sha256(plan)
                store = OperationStore(self.home, plan["operation_id"])
                store.accept_plan(plan)
                state = {**self.samples["states"][name], "operation_id": plan["operation_id"],
                         "plan_sha256": plan["plan_sha256"]}
                events = [
                    {**event, "operation_id": plan["operation_id"], "plan_sha256": plan["plan_sha256"]}
                    for event in self.samples["events"][:state["next_event_sequence"] - 1]
                ]
                if name == "partial":
                    events[-1]["goal_status"] = "completed_with_residuals"
                store.state_path.write_text(json.dumps(state), encoding="utf-8")
                store.events_path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
                self.assertEqual(store.read_state(), state)
                before = store.plan_path.read_bytes()
                status = self.invoke(("agent", "status", "--operation-id", plan["operation_id"],
                                      "--codex-home", str(self.home)))
                self.assertEqual(status["goal_status"], state["goal_status"])
                self.assertEqual(status["mutation_started"], state["mutation_started"])
                if state["mutation_started"]:
                    applied = self.invoke(("agent", "apply", "--plan", str(store.plan_path),
                        "--authorized-plan-sha256", plan["plan_sha256"], "--clients-closed"))
                    self.assertEqual(applied["goal_status"], "unknown")
                    self.assertTrue(applied["mutation_started"])
                self.assertEqual(store.plan_path.read_bytes(), before)

    def test_fixed_unknown_can_be_verified_without_reissuing_mutation(self) -> None:
        plan = self.relocate("agent_plan")
        store = OperationStore(self.home, plan["operation_id"])
        store.accept_plan(plan)
        state = {**self.samples["states"]["unknown"], "plan_sha256": plan["plan_sha256"]}
        store.state_path.write_text(json.dumps(state), encoding="utf-8")
        events = [{**e, "plan_sha256": plan["plan_sha256"]} for e in self.samples["events"]]
        store.events_path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        result = self.invoke(("agent", "verify", "--operation-id", plan["operation_id"],
                              "--codex-home", str(self.home)))
        self.assertEqual(result["goal_status"], "complete")
        self.assertEqual(result["plan_sha256"], plan["plan_sha256"])

    def test_fixed_terminal_receipt_read_has_no_catalog_or_writer_dependency(self) -> None:
        receipt = self.samples["receipt"]
        self.assertEqual(_receipt_sha256(receipt), receipt["receipt_sha256"])
        store = OperationStore(self.home, receipt["operation_id"])
        store.directory.mkdir(parents=True)
        store.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        before = store.receipt_path.read_bytes()
        for command in ("status", "verify"):
            result = self.invoke(("agent", command, "--operation-id", receipt["operation_id"],
                                  "--codex-home", str(self.home)))
            self.assertEqual(result["goal_status"], "complete")
            self.assertEqual(result["plan_sha256"], receipt["plan_sha256"])
            self.assertTrue(result["compacted"])
        self.assertEqual(store.receipt_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
