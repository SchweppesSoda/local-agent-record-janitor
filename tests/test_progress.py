from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.adapters import NativeIntegrityAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.cli import EXIT_GOAL_NOT_SATISFIED, main
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.progress import ProgressReporter

from tests.support import create_thread_index, write_rollout


class ProgressTests(unittest.TestCase):
    def test_cli_verify_progress_reaches_public_coordinator_and_stays_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            path = write_rollout(home, "retained", originator="codex_cli_rs")
            create_thread_index(home, [{"id": "retained", "rollout_path": str(path)}])
            adapter = NativeIntegrityAdapter(codex_home=home)
            coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
            plan_path = root / "plan.json"
            plan = coordinator.plan_operation(client="native", record_ids=("retained",),
                adapters=(adapter,), plan_path=plan_path)
            self.assertEqual(plan["goal_status"], "ready", plan)
            output, errors = StringIO(), StringIO()
            status = main(("operation", "verify", "--operation-id", plan["operation_id"],
                           "--plan", str(plan_path), "--progress", "--json"),
                adapters=(adapter,), operation_coordinator=coordinator,
                stdout=output, stderr=errors, stdin=StringIO())
            self.assertEqual(status, EXIT_GOAL_NOT_SATISFIED, output.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(result["goal_status"], "completed_with_residuals")
            self.assertFalse(result["mutation_started"])
            self.assertTrue(path.is_file())
            events = [json.loads(line) for line in errors.getvalue().splitlines()]
            self.assertEqual([event["status"] for event in events if event["stage"] == "verify"],
                             ["started", "completed"])
            self.assertTrue(all(event["event"] == "progress" for event in events))
            self.assertNotIn('"event": "progress"', output.getvalue())

    def test_native_batch_reports_each_verified_action_with_total(self) -> None:
        """A real temporary native batch exposes live per-action progress."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            home.mkdir()
            thread_ids = tuple(f"native-{index:03d}" for index in range(100))
            rollout_paths: dict[str, Path] = {}
            rows: list[dict[str, str]] = []
            for thread_id in thread_ids:
                path = write_rollout(home, thread_id, originator="codex_cli_rs")
                rollout_paths[thread_id] = path
                rows.append({"id": thread_id, "rollout_path": str(path)})
            create_thread_index(home, rows)

            class DeletingServer:
                def __enter__(self) -> "DeletingServer":
                    return self

                def __exit__(self, *_exc_info: object) -> None:
                    return None

                def delete_thread(self, thread_id: str) -> None:
                    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                        connection.execute(
                            "DELETE FROM threads WHERE id = ?", (thread_id,)
                        )
                        connection.commit()
                    rollout_paths[thread_id].unlink(missing_ok=True)

            events: list[dict[str, object]] = []
            result = OperationCoordinator(
                CleanupService(client_inspector=lambda *_: ())
            ).run_operation(
                client="native",
                record_ids=thread_ids,
                adapters=(NativeIntegrityAdapter(codex_home=home),),
                plan_path=root / "operation-plan.json",
                clients_closed=True,
                timeout=5,
                app_server_factory=lambda **_kwargs: DeletingServer(),
                binary_resolver=lambda _hint: Path("codex"),
                progress_callback=events.append,
            )

            self.assertEqual(result.get("goal_status"), "complete", result)
            action_events = [
                event
                for event in events
                if event.get("stage") == "apply"
                and event.get("status") == "action_completed"
            ]
            self.assertEqual(len(action_events), len(thread_ids))
            self.assertEqual(
                [event["counts"]["completed_action_count"] for event in action_events],
                list(range(1, len(thread_ids) + 1)),
            )
            self.assertTrue(
                all(
                    event["counts"]["total_action_count"] == len(thread_ids)
                    for event in action_events
                )
            )

    def test_reporter_clock_or_stream_failures_are_best_effort(self) -> None:
        class BrokenStream(StringIO):
            def write(self, _value: str) -> int:
                raise OSError("closed")

        reporter = ProgressReporter(BrokenStream())
        reporter({"stage": "inventory", "status": "running"})
        reporter({"stage": "inventory", "status": "completed"})

        def broken_clock() -> float:
            raise RuntimeError("clock")

        reporter = ProgressReporter(StringIO(), clock=broken_clock)
        reporter({"stage": "inventory", "status": "running"})

    def test_unknown_batch_reports_recovery_without_verify_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            action = SimpleNamespace(
                action_id="unknown-action",
                kind="delete_pi_session",
                available=True,
                target=SimpleNamespace(
                    storage_id="pi-store",
                    thread_id="unknown-session",
                ),
                impact=SimpleNamespace(
                    external_engine="pi",
                    external_storage_root=str(home),
                    external_action_payload={},
                ),
            )
            context = SimpleNamespace(
                actions=(action,),
                plan=SimpleNamespace(
                    actions=(action,),
                    storages=(SimpleNamespace(storage_id="pi-store", path=home),),
                ),
            )

            from tests.test_mutation_guard import write_journal
            store = write_journal(home, "unknown-operation-1", {
                "action_id": "unknown-action", "kind": "delete_pi_session",
                "storage_id": "pi-store", "thread_id": "unknown-session",
                "impact": {"external_storage_root": str(home), "external_action_payload": {}},
            }, started=False)

            class UnknownService:
                def execute(self, _context: object, selected: object, **kwargs: object):
                    current = tuple(selected)[0]
                    callback = kwargs["action_state_callback"]
                    callback("mutation_started", current, None)
                    callback(
                        "verified",
                        current,
                        SimpleNamespace(status="unknown"),
                    )
                    return SimpleNamespace(
                        modified=True,
                        session_cleanup=SimpleNamespace(
                            results=(SimpleNamespace(status="unknown"),)
                        ),
                    )

            coordinator = OperationCoordinator(UnknownService())
            live = SimpleNamespace(
                operation_id="unknown-operation",
                document={
                    "operation_id": "unknown-operation",
                    "plan_sha256": "a" * 64,
                    "scope": {},
                },
                context=context,
                candidates=(action,),
                client="pi",
                adapters=(),
                result=None,
            )
            events: list[dict[str, object]] = []
            with (
                patch.object(
                    coordinator,
                    "_open_batch_store",
                    return_value=(store, store.read_state()),
                ),
                patch.object(
                    coordinator,
                    "_terminal_context",
                    side_effect=AssertionError("unknown must skip terminal scan"),
                ),
            ):
                result = coordinator._execute_live(
                    live,
                    timeout=1,
                    app_server_factory=lambda **_kwargs: None,
                    binary_resolver=lambda _hint: None,
                    progress_callback=events.append,
                )

            self.assertEqual(result["goal_status"], "unknown")
            self.assertEqual(
                [
                    event["status"]
                    for event in events
                    if event.get("stage") == "apply"
                    and event.get("status") == "action_completed"
                ],
                [],
            )
            checked = [
                event
                for event in events
                if event.get("stage") == "apply"
                and event.get("status") == "action_checked"
            ]
            self.assertEqual(len(checked), 1)
            self.assertEqual(checked[0]["result_status"], "unknown")
            verify_statuses = [
                event["status"]
                for event in events
                if event.get("stage") == "verify"
            ]
            self.assertEqual(verify_statuses, ["skipped"])


if __name__ == "__main__":
    unittest.main()
