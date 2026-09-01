from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.cli import (
    EXIT_ERROR,
    EXIT_GOAL_NOT_SATISFIED,
    EXIT_OK,
    _record_metadata_payload,
    _record_classification,
    build_parser,
    main,
)
from local_agent_record_janitor.cleaner import CleanupReport
from local_agent_record_janitor.inventory import FrontendSessionRecord, ManagedConversation
from local_agent_record_janitor.models import ConversationSummary


class FakeOperationCoordinator:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def plan_operation(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return dict(self.result)

    def apply_operation(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return dict(self.result)

    def status_operation(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return dict(self.result)

    def verify_operation(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return dict(self.result)


class OperationCliContractTests(unittest.TestCase):
    def test_parser_exposes_one_client_and_mutually_exclusive_scope(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            (
                "delete",
                "plan",
                "--client",
                "codex-native",
                "--project",
                "project-a",
                "--project",
                "project-b",
                "--engine",
                "codex",
                "--json",
            )
        )
        self.assertEqual(args.delete_action, "plan")
        self.assertEqual(args.client, "codex-native")
        self.assertEqual(args.project, ["project-a", "project-b"])
        self.assertEqual(args.engine, ["codex"])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                (
                    "delete",
                    "run",
                    "--client",
                    "native",
                    "--all-projects",
                    "--record-id",
                    "same-id",
                )
            )

    def test_operation_backend_receives_scope_and_never_emits_body(self) -> None:
        service = FakeOperationCoordinator(
            {
                "operation_id": "op-1",
                "goal_status": "ready",
                "plan_sha256": "sha-1",
                "chat_body": "secret chat text",
                "batches": [
                    {
                        "project": "project-a",
                        "engine": "codex",
                        "location": "store-a",
                        "content": "secret batch body",
                    }
                ],
            }
        )
        output = StringIO()
        status = main(
            (
                "delete",
                "plan",
                "--client",
                "cindy",
                "--project",
                "project-a",
                "--project",
                "project-b",
                "--engine",
                "codex",
                "--json",
            ),
            adapters=(),
            operation_coordinator=service,
            stdout=output,
            stderr=StringIO(),
            stdin=StringIO(),
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(service.calls[0]["client"], "cindy")
        self.assertEqual(service.calls[0]["projects"], ("project-a", "project-b"))
        self.assertFalse(service.calls[0]["all_projects"])
        self.assertEqual(service.calls[0]["record_ids"], ())
        self.assertEqual(service.calls[0]["engines"], ("codex",))
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret chat text", rendered)
        self.assertNotIn("secret batch body", rendered)
        self.assertEqual(payload["scope"]["client"], "cindy")

    def test_unknown_operation_is_reported_without_an_implicit_retry(self) -> None:
        calls: list[dict[str, object]] = []

        class UnknownService(FakeOperationCoordinator):
            def run_operation(self, **kwargs: object) -> dict[str, object]:
                calls.append(kwargs)
                return {
                    "operation_id": "op-unknown",
                    "goal_status": "unknown",
                    "mutation_started": True,
                    "blockers": [
                        {"blocker_code": "mutation_outcome_unknown"}
                    ],
                }

        service = UnknownService({})
        output = StringIO()
        status = main(
            (
                "delete",
                "run",
                "--client",
                "native",
                "--all-projects",
                "--clients-closed",
                "--json",
            ),
            adapters=(),
            operation_coordinator=service,
            stdout=output,
            stderr=StringIO(),
            stdin=StringIO(),
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_ERROR)
        self.assertEqual(len(calls), 1)
        self.assertEqual(payload["goal_status"], "unknown")
        self.assertTrue(payload["mutation_started"])

    def test_apply_forwards_explicit_plan_and_complete_scope(self) -> None:
        coordinator = FakeOperationCoordinator(
            {
                "operation_id": "op-apply",
                "goal_status": "complete",
                "modified": True,
            }
        )
        output = StringIO()
        status = main(
            (
                "delete",
                "apply",
                "--operation-id",
                "op-apply",
                "--plan",
                "C:/tmp/operation-plan.json",
                "--authorized-plan-sha256",
                "sha-apply",
                "--client",
                "cindy",
                "--project",
                "project-a",
                "--engine",
                "codex",
                "--clients-closed",
                "--json",
            ),
            adapters=(),
            operation_coordinator=coordinator,
            stdout=output,
            stderr=StringIO(),
            stdin=StringIO(),
        )
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(len(coordinator.calls), 1)
        call = coordinator.calls[0]
        self.assertEqual(call["operation_id"], "op-apply")
        self.assertEqual(call["plan_path"], Path("C:/tmp/operation-plan.json"))
        self.assertEqual(call["plan_sha256"], "sha-apply")
        self.assertTrue(call["clients_closed"])
        self.assertEqual(call["scope"], {
            "client": "cindy",
            "projects": ("project-a",),
            "all_projects": False,
            "record_ids": (),
            "engines": ("codex",),
        })

    def test_status_and_verify_forward_state_roots_by_name(self) -> None:
        for subcommand in ("status", "verify"):
            coordinator = FakeOperationCoordinator(
                {"operation_id": "op-query", "goal_status": "ready"}
            )
            output = StringIO()
            argv = [
                "operation",
                subcommand,
                "--operation-id",
                "op-query",
                "--operation-home",
                "C:/tmp/operation-state",
                "--codex-home",
                "C:/tmp/codex-home",
                "--plan",
                "C:/tmp/top-level-operation-plan.json",
                "--json",
            ]
            if subcommand == "verify":
                argv.extend(("--verify-timeout", "12"))
            status = main(
                argv,
                adapters=(),
                operation_coordinator=coordinator,
                stdout=output,
                stderr=StringIO(),
                stdin=StringIO(),
            )
            self.assertEqual(status, EXIT_OK)
            self.assertEqual(len(coordinator.calls), 1)
            call = coordinator.calls[0]
            self.assertEqual(
                call["plan_path"], Path("C:/tmp/top-level-operation-plan.json")
            )
            self.assertEqual(call["operation_home"], Path("C:/tmp/operation-state"))
            self.assertEqual(call["codex_home"], Path("C:/tmp/codex-home"))
            if subcommand == "verify":
                self.assertEqual(call["verify_timeout"], 12)

    def test_operation_query_without_core_hook_fails_closed_without_mutation(self) -> None:
        output = StringIO()
        with patch(
            "local_agent_record_janitor.cli._build_operation_coordinator",
            side_effect=ImportError("core coordinator not installed"),
        ):
            status = main(
                (
                    "operation",
                    "status",
                    "--operation-id",
                    "missing-operation",
                    "--json",
                ),
                adapters=(),
                stdout=output,
                stderr=StringIO(),
                stdin=StringIO(),
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_GOAL_NOT_SATISFIED)
        self.assertEqual(
            payload["blockers"][0]["blocker_code"],
            "operation_api_unavailable",
        )
        self.assertFalse(payload["modified"])
        self.assertFalse(payload["mutation_started"])

    def test_records_classification_covers_native_and_frontend_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            native = ManagedConversation(
                codex_home=home,
                thread_id="native-only",
                summary=ConversationSummary(
                    thread_id="native-only",
                    display_name="native",
                    cwd=str(home),
                ),
                indexed=True,
                artifact_present=True,
            )
            frontend = FrontendSessionRecord(
                platform="cindy",
                platform_session_id="front-only",
                thread_id="front-only",
                database=home / "cindy.db",
                codex_home=home,
                is_live=False,
            )
            frontend_only = ManagedConversation(
                codex_home=home,
                thread_id="front-only",
                summary=ConversationSummary(
                    thread_id="front-only",
                    display_name="frontend",
                    cwd=str(home),
                ),
                frontend_sessions=(frontend,),
            )
            self.assertEqual(
                _record_metadata_payload(native)["classification"],
                "orphan_native",
            )
            self.assertEqual(
                _record_metadata_payload(frontend_only)["classification"],
                "orphan_frontend",
            )
            self.assertNotIn(
                "messages",
                _record_metadata_payload(
                    {"thread_id": "x", "body": "must not leak"}
                ),
            )

    def test_records_classification_maps_known_anomalies(self) -> None:
        expected = {
            "index_missing_rollout": "stale_index",
            "duplicate_rollout": "orphan_native",
            "index_rollout_path_mismatch": "stale_index",
            "index_rollout_metadata_mismatch": "stale_index",
            "frontend_deleted_reference": "orphan_frontend",
        }
        for finding_type, classification in expected.items():
            record = SimpleNamespace(
                details={"finding_type": finding_type},
                blockers=(),
                indexed=False,
                artifact_present=False,
                rollouts=(),
                frontend_sessions=(),
                legacy_indexed=False,
            )
            with self.subTest(finding_type=finding_type):
                self.assertEqual(_record_classification(record), classification)

    def test_manual_delete_uses_one_apply_preflight_and_targeted_batch_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            action = SimpleNamespace(
                action_id="action-1",
                available=True,
                codex_home=home,
                thread_id="thread-1",
                affected_thread_ids=("thread-1",),
                frontend_sessions=(),
                plan_fingerprint="selected-fingerprint",
            )
            conversation = SimpleNamespace(
                codex_home=home,
                thread_id="thread-1",
                action_id="action-1",
            )
            catalog = SimpleNamespace(
                conversations=(conversation,),
                unmapped_frontend_sessions=(),
                failures=(),
            )

            class FakePlan:
                def __init__(self, selected: bool = False) -> None:
                    self.actions = (action,)
                    self.errors = ()
                    self.selected = selected
                    self.plan_fingerprint = (
                        "selected-fingerprint" if selected else None
                    )

                def with_selected_actions(self, _selectors: object) -> "FakePlan":
                    return FakePlan(True)

            builder_calls = 0

            def builder(*_args: object, **_kwargs: object) -> object:
                nonlocal builder_calls
                builder_calls += 1
                return catalog

            output = StringIO()
            errors = StringIO()
            with (
                patch(
                    "local_agent_record_janitor.inventory.build_session_catalog",
                    side_effect=builder,
                ),
                patch(
                    "local_agent_record_janitor.manual_delete.build_manual_delete_plan",
                    side_effect=lambda _catalog: FakePlan(False),
                ),
                patch(
                    "local_agent_record_janitor.manual_delete.execute_manual_delete",
                    return_value=CleanupReport(planned=[]),
                ) as execute,
            ):
                status = main(
                    (
                        "delete",
                        "--platform",
                        "native",
                        "--action-id",
                        "action-1",
                        "--yes",
                        "--clients-closed",
                        "--plan-fingerprint",
                        "selected-fingerprint",
                        "--json",
                    ),
                    adapters=(),
                    stdout=output,
                    stderr=errors,
                    stdin=StringIO(),
                )

            self.assertEqual(status, EXIT_OK, errors.getvalue() + output.getvalue())
            self.assertEqual(builder_calls, 2)
            kwargs = execute.call_args.kwargs
            self.assertTrue(kwargs["preflight_verified"])
            self.assertTrue(kwargs["targeted_guards_only"])
            self.assertTrue(callable(kwargs["targeted_guard"]))
            self.assertTrue(callable(kwargs["action_state_callback"]))


if __name__ == "__main__":
    unittest.main()
