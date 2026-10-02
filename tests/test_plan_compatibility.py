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
        original = (self.samples["child_recovery"]["plan"] if name == "child_plan"
                    else self.samples[name])
        self.assertEqual(plan_sha256(original), original["plan_sha256"])
        text = json.dumps(original)
        text = text.replace("$STORE_ROOT", self.home.as_posix())
        text = text.replace("$FIXTURE_ROOT", self.root.as_posix())
        storages = [*original.get("storages", ()), original.get("target", {})]
        for storage in storages:
            if storage.get("storage_id"):
                text = text.replace(storage["storage_id"], storage_id_for_path(self.home))
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

    def install_child(self, *, receipt: bool) -> tuple[dict, dict, OperationStore]:
        top = self.relocate("operation_plan")
        child = self.relocate("child_plan")
        self.assertEqual(child["actions"], top["actions"])
        self.assertEqual(child["target"]["storage_id"], top["storages"][0]["storage_id"])
        self.assertEqual(top["child_batches"][0]["storage_id"], child["target"]["storage_id"])
        Path(top["plan_path"]).write_text(json.dumps(top), encoding="utf-8")
        store = OperationStore(self.home, child["operation_id"])
        fixture = self.samples["child_recovery"]
        if receipt:
            value = {**fixture["receipt"], "plan_sha256": child["plan_sha256"]}
            value["receipt_sha256"] = _receipt_sha256(value)
            store.directory.mkdir(parents=True)
            store.receipt_path.write_text(json.dumps(value), encoding="utf-8")
        else:
            store.accept_plan(child)
            state = {**fixture["state"], "plan_sha256": child["plan_sha256"]}
            events = [{**event, "plan_sha256": child["plan_sha256"]} for event in fixture["events"]]
            store.state_path.write_text(json.dumps(state), encoding="utf-8")
            store.events_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        return top, child, store

    def test_fixed_child_unknown_status_verify_and_apply_refusal_use_frozen_bindings(self) -> None:
        top, child, store = self.install_child(receipt=False)
        before = store.plan_path.read_bytes()
        coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        args = {"operation_id": top["operation_id"], "plan_path": Path(top["plan_path"])}
        status = coordinator.status_operation(**args)
        self.assertEqual(status["goal_status"], "unknown")
        self.assertTrue(status["mutation_started"])
        applied = coordinator.apply_operation(**args, scope=top["scope"], plan_sha256=top["plan_sha256"],
            clients_closed=True, app_server_factory=lambda **_: self.fail("fixed child must not replay"))
        self.assertEqual(applied["goal_status"], "unknown")
        self.assertEqual(store.plan_path.read_bytes(), before)
        self.assertEqual(store.read_plan()["plan_sha256"], child["plan_sha256"])
        verified = coordinator.verify_operation(**args, adapters=(NativeIntegrityAdapter(codex_home=self.home),),
            verify_timeout=0)
        self.assertEqual(verified["goal_status"], "complete")
        self.assertTrue(store.receipt_path.exists())
        fresh = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        result = fresh.apply_operation(**args, scope=top["scope"], plan_sha256=top["plan_sha256"],
            clients_closed=True, app_server_factory=lambda **_: self.fail("verified fixed child must not replay"))
        self.assertEqual(result["goal_status"], "complete")

    def test_fixed_child_receipt_reopens_in_new_coordinator_without_replaying(self) -> None:
        top, _, store = self.install_child(receipt=True)
        before = store.receipt_path.read_bytes()
        args = {"operation_id": top["operation_id"], "plan_path": Path(top["plan_path"])}
        for method in ("status_operation", "verify_operation", "apply_operation"):
            with self.subTest(method=method):
                coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
                extras = ({"scope": top["scope"], "plan_sha256": top["plan_sha256"], "clients_closed": True,
                           "app_server_factory": lambda **_: self.fail("fixed receipt must not replay")}
                          if method == "apply_operation" else
                          {"adapters": (NativeIntegrityAdapter(codex_home=self.home),), "verify_timeout": 0}
                          if method == "verify_operation" else {})
                result = getattr(coordinator, method)(**args, **extras)
                self.assertEqual(result["goal_status"], "complete")
                self.assertTrue(result["mutation_started"])
        self.assertEqual(store.receipt_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
