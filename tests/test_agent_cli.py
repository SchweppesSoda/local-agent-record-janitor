from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.adapters.native import NativeIntegrityAdapter
from local_agent_record_janitor.agent_cli import _verify_with_retry
from local_agent_record_janitor.agent_operations import plan_counts
from local_agent_record_janitor.cli import build_parser, main
from local_agent_record_janitor.codex_desktop_state import DesktopStateError
from local_agent_record_janitor.operation_store import (
    OperationStore,
    OperationStoreError,
    plan_sha256,
)

from tests.support import create_thread_index, write_rollout


class _NoTTYInput(StringIO):
    def isatty(self) -> bool:
        raise AssertionError("Agent mode must not inspect TTY state")

    def readline(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("Agent mode must not read stdin")


class _MutatingServer:
    def __init__(self, callback: object) -> None:
        self.callback = callback
        self.deleted_thread_ids: list[str] = []

    def __enter__(self) -> _MutatingServer:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        self.deleted_thread_ids.append(thread_id)
        self.callback(thread_id)


class AgentCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        create_thread_index(self.codex_home, [])
        self.adapter = NativeIntegrityAdapter(codex_home=self.codex_home)

    def invoke(
        self,
        argv: tuple[str, ...],
        *,
        server: _MutatingServer | None = None,
        client_probe: tuple[str, ...] | BaseException = (),
    ) -> tuple[int, dict[str, object], str]:
        output = StringIO()
        errors = StringIO()
        client_patch = (
            {"side_effect": client_probe}
            if isinstance(client_probe, BaseException)
            else {"return_value": client_probe}
        )
        with patch(
            "local_agent_record_janitor.agent_cli.running_related_clients",
            **client_patch,
        ):
            code = main(
                argv,
                adapters=(self.adapter,),
                stdin=_NoTTYInput(),
                stdout=output,
                stderr=errors,
                app_server_factory=(
                    (lambda **_kwargs: server)
                    if server is not None
                    else (lambda **_kwargs: self.fail("app server was not expected"))
                ),
                binary_resolver=lambda _hint: Path("codex"),
            )
        rendered = output.getvalue()
        self.assertEqual(errors.getvalue(), "")
        lines = [line for line in rendered.splitlines() if line]
        self.assertEqual(len(lines), 1)
        return code, json.loads(lines[0]), rendered

    def make_plan(self, thread_id: str = "agent-residual") -> tuple[Path, dict[str, object], Path]:
        rollout = write_rollout(
            self.codex_home,
            thread_id,
            originator="test",
        )
        with rollout.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"body": "SECRET_CHAT_BODY_7f9030"},
                    }
                )
                + "\n"
            )
        plan_path = self.root / "plan.json"
        code, summary, rendered = self.invoke(
            (
                "agent",
                "plan",
                "--operation",
                "purge",
                "--platform",
                "native",
                "--codex-home",
                str(self.codex_home),
                "--out",
                str(plan_path),
            )
        )
        self.assertEqual(code, 0)
        document = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(document["plan_sha256"], plan_sha256(document))
        self.assertEqual(summary["plan_sha256"], document["plan_sha256"])
        self.assertTrue(summary["authorization_required"])
        self.assertNotIn("SECRET_CHAT_BODY_7f9030", rendered)
        self.assertNotIn("SECRET_CHAT_BODY_7f9030", plan_path.read_text(encoding="utf-8"))
        return plan_path, document, rollout

    def operation_directory(self, document: dict[str, object]) -> Path:
        return (
            self.codex_home
            / ".local-agent-record-janitor"
            / "operations"
            / str(document["operation_id"])
        )

    def install_desktop_orphan(self, thread_id: str) -> None:
        database = self.codex_home / "sqlite" / "codex-dev.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(
                """
                CREATE TABLE local_thread_catalog (
                    host_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    display_title TEXT,
                    missing_candidate INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (host_id, thread_id)
                );
                CREATE TABLE local_thread_catalog_metadata (
                    id INTEGER PRIMARY KEY,
                    catalog_revision INTEGER NOT NULL
                );
                INSERT INTO local_thread_catalog_metadata VALUES (1, 7);
                """
            )
            connection.execute(
                "INSERT INTO local_thread_catalog "
                "(host_id, thread_id, display_title) VALUES ('local', ?, ?)",
                (thread_id, "Desktop orphan"),
            )
            connection.commit()
        (self.codex_home / ".codex-global-state.json").write_text(
            json.dumps(
                {
                    "projectless-thread-ids": [thread_id],
                    f"thread-permissions-{thread_id}": {"mode": "workspace"},
                }
            ),
            encoding="utf-8",
        )

    def test_doctor_and_empty_plan_are_noninteractive_and_read_only(self) -> None:
        before = {
            path.relative_to(self.codex_home): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.codex_home.rglob("*")
            if path.is_file()
        }
        code, doctor, _ = self.invoke(
            (
                "agent",
                "doctor",
                "--platform",
                "native",
                "--codex-home",
                str(self.codex_home),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(doctor["status"], "ready")

        plan_path = self.root / "empty-plan.json"
        code, summary, _ = self.invoke(
            (
                "agent",
                "plan",
                "--platform",
                "native",
                "--codex-home",
                str(self.codex_home),
                "--out",
                str(plan_path),
            )
        )
        self.assertEqual(code, 0)
        self.assertFalse(summary["authorization_required"])
        self.assertFalse(
            (self.codex_home / ".local-agent-record-janitor").exists()
        )
        after = {
            path.relative_to(self.codex_home): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.codex_home.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_empty_plan_is_rescanned_before_it_can_complete(self) -> None:
        plan_path = self.root / "empty-race-plan.json"
        code, summary, _ = self.invoke(
            (
                "agent", "plan", "--platform", "native", "--codex-home",
                str(self.codex_home), "--out", str(plan_path),
            )
        )
        self.assertEqual(code, 0)
        self.assertFalse(summary["authorization_required"])
        document = json.loads(plan_path.read_text(encoding="utf-8"))
        raced_rollout = write_rollout(
            self.codex_home,
            "appeared-after-empty-plan",
            originator="test",
        )

        code, result, _ = self.invoke(
            (
                "agent", "apply", "--plan", str(plan_path),
                "--authorized-plan-sha256", str(document["plan_sha256"]),
                "--clients-closed", "--verify-timeout", "0",
            )
        )

        self.assertEqual(code, 3)
        self.assertEqual(result["goal_status"], "blocked")
        self.assertFalse(result["goal_satisfied"])
        self.assertFalse(result["mutation_started"])
        self.assertTrue(raced_rollout.is_file())
        self.assertIn(
            "plan_changed",
            {item["blocker_code"] for item in result["blockers"]},
        )

    def test_unchanged_empty_plan_completes_with_full_verification(self) -> None:
        plan_path = self.root / "empty-complete-plan.json"
        code, summary, _ = self.invoke(
            (
                "agent", "plan", "--platform", "native", "--codex-home",
                str(self.codex_home), "--out", str(plan_path),
            )
        )
        self.assertEqual(code, 0)

        code, result, _ = self.invoke(
            (
                "agent", "apply", "--plan", str(plan_path),
                "--authorized-plan-sha256", str(summary["plan_sha256"]),
                "--clients-closed", "--verify-timeout", "0",
            )
        )

        self.assertEqual(code, 0)
        self.assertEqual(result["goal_status"], "complete")
        self.assertFalse(result["mutation_started"])
        self.assertFalse(result["modified"])
        self.assertTrue(result["verification"]["all_satisfied"])
        self.assertTrue(result["final_scope_verification"]["all_satisfied"])

    def test_empty_plan_scan_failure_is_unknown_not_complete(self) -> None:
        plan_path = self.root / "empty-scan-error-plan.json"
        code, summary, _ = self.invoke(
            (
                "agent", "plan", "--platform", "native", "--codex-home",
                str(self.codex_home), "--out", str(plan_path),
            )
        )
        self.assertEqual(code, 0)
        document = json.loads(plan_path.read_text(encoding="utf-8"))
        with patch.object(
            self.adapter,
            "scan",
            side_effect=OSError("scan temporarily unavailable"),
        ):
            code, result, _ = self.invoke(
                (
                    "agent", "apply", "--plan", str(plan_path),
                    "--authorized-plan-sha256", str(summary["plan_sha256"]),
                    "--clients-closed", "--verify-timeout", "0",
                )
            )

        self.assertEqual(code, 1)
        self.assertEqual(result["goal_status"], "unknown")
        self.assertFalse(result["goal_satisfied"])
        self.assertFalse(result["mutation_started"])
        self.assertIn(
            "scan_incomplete",
            {item["blocker_code"] for item in result["blockers"]},
        )
        self.assertTrue(self.operation_directory(document).is_dir())

    def test_doctor_reports_running_client_and_probe_failure_by_code(self) -> None:
        argv = (
            "agent",
            "doctor",
            "--platform",
            "native",
            "--codex-home",
            str(self.codex_home),
        )
        code, running, _ = self.invoke(
            argv,
            client_probe=("ChatGPT.exe",),
        )
        self.assertEqual(code, 1)
        self.assertEqual(running["status"], "unknown")
        self.assertEqual(
            running["blockers"][0]["blocker_code"],
            "target_client_running",
        )

        code, failed, _ = self.invoke(
            argv,
            client_probe=DesktopStateError("CIM unavailable"),
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            failed["blockers"][0]["blocker_code"],
            "client_check_failed",
        )

    def test_documented_agent_command_shapes_parse(self) -> None:
        parser = build_parser()
        examples = (
            ("agent", "doctor", "--platform", "native", "--codex-home", "C:\\exact\\.codex"),
            (
                "agent", "plan", "--operation", "purge", "--platform", "native",
                "--codex-home", "C:\\exact\\.codex", "--out", "plan.json",
            ),
            (
                "agent", "apply", "--plan", "plan.json",
                "--authorized-plan-sha256", "a" * 64, "--clients-closed",
            ),
            (
                "agent", "status", "--operation-id", "purge-example",
                "--codex-home", "C:\\exact\\.codex",
            ),
            (
                "agent", "verify", "--operation-id", "purge-example",
                "--codex-home", "C:\\exact\\.codex", "--verify-timeout", "180",
            ),
        )
        for example in examples:
            with self.subTest(command=example[1]):
                parsed = parser.parse_args(example)
                self.assertEqual(parsed.command, "agent")

    def test_apply_requires_hash_and_clients_closed_without_mutation(self) -> None:
        plan_path, document, rollout = self.make_plan()

        code, missing_hash, _ = self.invoke(
            ("agent", "apply", "--plan", str(plan_path), "--clients-closed")
        )
        self.assertEqual(code, 3)
        self.assertEqual(
            missing_hash["blockers"][0]["blocker_code"],
            "authorization_hash_required",
        )
        self.assertTrue(rollout.exists())

        code, missing_ack, _ = self.invoke(
            (
                "agent",
                "apply",
                "--plan",
                str(plan_path),
                "--authorized-plan-sha256",
                str(document["plan_sha256"]),
            )
        )
        self.assertEqual(code, 3)
        self.assertEqual(
            missing_ack["blockers"][0]["blocker_code"],
            "clients_closed_ack_required",
        )
        self.assertTrue(rollout.exists())

    def test_tampered_plan_is_rejected_before_operation_directory(self) -> None:
        plan_path, document, rollout = self.make_plan()
        document["counts"]["artifact_count"] += 1
        plan_path.write_text(json.dumps(document), encoding="utf-8")

        code, result, _ = self.invoke(
            (
                "agent",
                "apply",
                "--plan",
                str(plan_path),
                "--authorized-plan-sha256",
                str(document["plan_sha256"]),
                "--clients-closed",
            )
        )
        self.assertEqual(code, 3)
        self.assertEqual(
            result["blockers"][0]["blocker_code"],
            "plan_integrity_failed",
        )
        self.assertTrue(rollout.exists())
        self.assertFalse(
            (self.codex_home / ".local-agent-record-janitor").exists()
        )

    def test_apply_is_persisted_verified_and_never_repeated(self) -> None:
        thread_id = "agent-success"
        plan_path, document, rollout = self.make_plan(thread_id)
        server = _MutatingServer(lambda _thread_id: rollout.unlink())
        apply_argv = (
            "agent",
            "apply",
            "--plan",
            str(plan_path),
            "--authorized-plan-sha256",
            str(document["plan_sha256"]),
            "--clients-closed",
            "--verify-timeout",
            "0",
        )

        code, result, _ = self.invoke(apply_argv, server=server)
        self.assertEqual(code, 0)
        self.assertEqual(result["goal_status"], "complete")
        self.assertTrue(result["goal_satisfied"])
        self.assertTrue(result["mutation_started"])
        self.assertTrue(result["modified"])
        self.assertEqual(server.deleted_thread_ids, [thread_id])

        code, repeated, _ = self.invoke(apply_argv, server=server)
        self.assertEqual(code, 0)
        self.assertEqual(repeated["goal_status"], "complete")
        self.assertEqual(server.deleted_thread_ids, [thread_id])

        operation_id = str(document["operation_id"])
        code, status, _ = self.invoke(
            (
                "agent",
                "status",
                "--operation-id",
                operation_id,
                "--codex-home",
                str(self.codex_home),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(status["goal_status"], "complete")

        operation_dir = (
            self.codex_home
            / ".local-agent-record-janitor"
            / "operations"
            / operation_id
        )
        for name in ("plan.json", "events.jsonl", "state.json", "result.json"):
            self.assertTrue((operation_dir / name).is_file())
            self.assertNotIn(
                "SECRET_CHAT_BODY_7f9030",
                (operation_dir / name).read_text(encoding="utf-8"),
            )
        sequences = [
            json.loads(line)["sequence"]
            for line in (operation_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

    def test_verify_resolves_unknown_without_resending_mutation(self) -> None:
        thread_id = "agent-unknown"
        plan_path, document, rollout = self.make_plan(thread_id)

        class TimeoutServer(_MutatingServer):
            def delete_thread(self, current_id: str) -> None:
                self.deleted_thread_ids.append(current_id)
                raise TimeoutError("ambiguous request outcome")

        server = TimeoutServer(lambda _thread_id: None)
        argv = (
            "agent",
            "apply",
            "--plan",
            str(plan_path),
            "--authorized-plan-sha256",
            str(document["plan_sha256"]),
            "--clients-closed",
            "--verify-timeout",
            "0",
        )
        code, result, _ = self.invoke(argv, server=server)
        self.assertEqual(code, 1)
        self.assertEqual(result["goal_status"], "unknown")
        self.assertEqual(server.deleted_thread_ids, [thread_id])

        code, repeated, _ = self.invoke(argv, server=server)
        self.assertEqual(code, 1)
        self.assertEqual(repeated["goal_status"], "unknown")
        self.assertEqual(server.deleted_thread_ids, [thread_id])

        rollout.unlink()
        code, verified, _ = self.invoke(
            (
                "agent",
                "verify",
                "--operation-id",
                str(document["operation_id"]),
                "--codex-home",
                str(self.codex_home),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(verified["goal_status"], "complete")
        self.assertEqual(server.deleted_thread_ids, [thread_id])

    def test_agent_argument_errors_are_one_json_document_and_never_exit_two(
        self,
    ) -> None:
        code, result, _ = self.invoke(("agent", "apply"))

        self.assertEqual(code, 1)
        self.assertNotEqual(code, 2)
        self.assertEqual(result["goal_status"], "unknown")
        self.assertEqual(
            result["blockers"][0]["blocker_code"],
            "invalid_agent_arguments",
        )

    def test_mixed_mutation_family_finishes_with_counted_residuals(self) -> None:
        legacy_id = "legacy-only-after-delete"
        (self.codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": legacy_id}) + "\n",
            encoding="utf-8",
        )
        plan_path, document, rollout = self.make_plan("delete-first")
        self.assertEqual(
            document["authorization"]["mutation_kind"],
            "delete_conversation",
        )
        server = _MutatingServer(lambda _thread_id: rollout.unlink())

        code, result, _ = self.invoke(
            (
                "agent",
                "apply",
                "--plan",
                str(plan_path),
                "--authorized-plan-sha256",
                str(document["plan_sha256"]),
                "--clients-closed",
                "--verify-timeout",
                "0",
            ),
            server=server,
        )

        self.assertEqual(code, 3)
        self.assertEqual(result["goal_status"], "completed_with_residuals")
        remaining = result["final_scope_verification"]
        self.assertFalse(remaining["all_satisfied"])
        self.assertEqual(remaining["counts"]["root_action_count"], 1)
        self.assertEqual(remaining["counts"]["affected_thread_count"], 1)
        self.assertEqual(remaining["counts"]["artifact_count"], 1)
        self.assertEqual(
            remaining["counts"]["legacy_residual_line_count"],
            1,
        )

    def test_legacy_agent_result_and_event_retain_backup_evidence(self) -> None:
        (self.codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": "legacy-backup-target"}) + "\n",
            encoding="utf-8",
        )
        plan_path = self.root / "legacy-plan.json"
        code, summary, _ = self.invoke(
            (
                "agent",
                "plan",
                "--platform",
                "native",
                "--codex-home",
                str(self.codex_home),
                "--out",
                str(plan_path),
            )
        )
        self.assertEqual(code, 0)
        document = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(
            document["authorization"]["mutation_kind"],
            "repair_legacy_index",
        )

        code, result, _ = self.invoke(
            (
                "agent",
                "apply",
                "--plan",
                str(plan_path),
                "--authorized-plan-sha256",
                str(summary["plan_sha256"]),
                "--clients-closed",
                "--verify-timeout",
                "0",
            )
        )

        self.assertEqual(code, 0)
        repair = result["execution_result"]["repair"]
        self.assertTrue(repair["backup_id"])
        self.assertTrue(Path(repair["backup_path"]).is_file())
        events = [
            json.loads(line)
            for line in (
                self.operation_directory(document) / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            events[-1]["execution_result"]["repair"]["backup_id"],
            repair["backup_id"],
        )

    def test_desktop_agent_result_and_event_retain_backup_evidence(self) -> None:
        thread_id = "desktop-backup-target"
        self.install_desktop_orphan(thread_id)
        plan_path = self.root / "desktop-plan.json"
        code, summary, _ = self.invoke(
            (
                "agent", "plan", "--platform", "native", "--codex-home",
                str(self.codex_home), "--out", str(plan_path),
            )
        )
        self.assertEqual(code, 0)
        document = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(
            document["authorization"]["mutation_kind"],
            "remove_desktop_state",
        )

        code, result, _ = self.invoke(
            (
                "agent", "apply", "--plan", str(plan_path),
                "--authorized-plan-sha256", str(summary["plan_sha256"]),
                "--clients-closed", "--verify-timeout", "0",
            )
        )

        self.assertEqual(code, 0)
        cleanup = result["execution_result"]["result"]
        self.assertTrue(cleanup["backup_id"])
        self.assertTrue(Path(cleanup["backup_directory"]).is_dir())
        events = [
            json.loads(line)
            for line in (
                self.operation_directory(document) / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            events[-1]["execution_result"]["result"]["backup_id"],
            cleanup["backup_id"],
        )

    def test_forged_result_cannot_make_status_or_apply_complete(self) -> None:
        plan_path, document, rollout = self.make_plan("forged-result")
        server = _MutatingServer(lambda _thread_id: rollout.unlink())
        apply_argv = (
            "agent",
            "apply",
            "--plan",
            str(plan_path),
            "--authorized-plan-sha256",
            str(document["plan_sha256"]),
            "--clients-closed",
            "--verify-timeout",
            "0",
        )
        code, _result, _ = self.invoke(apply_argv, server=server)
        self.assertEqual(code, 0)
        result_path = self.operation_directory(document) / "result.json"
        result_path.write_text(
            json.dumps({"goal_status": "complete"}),
            encoding="utf-8",
        )

        code, status, _ = self.invoke(
            (
                "agent",
                "status",
                "--operation-id",
                str(document["operation_id"]),
                "--codex-home",
                str(self.codex_home),
            )
        )
        self.assertEqual(code, 1)
        self.assertEqual(status["goal_status"], "unknown")
        code, repeated, _ = self.invoke(apply_argv, server=server)
        self.assertNotEqual(code, 0)
        self.assertNotEqual(repeated["goal_status"], "complete")
        self.assertEqual(server.deleted_thread_ids, ["forged-result"])

    def test_state_cannot_contradict_durable_mutation_event(self) -> None:
        plan_path, document, rollout = self.make_plan("forged-state")
        server = _MutatingServer(lambda _thread_id: rollout.unlink())
        apply_argv = (
            "agent", "apply", "--plan", str(plan_path),
            "--authorized-plan-sha256", str(document["plan_sha256"]),
            "--clients-closed", "--verify-timeout", "0",
        )
        code, _result, _ = self.invoke(apply_argv, server=server)
        self.assertEqual(code, 0)
        operation_directory = self.operation_directory(document)
        (operation_directory / "result.json").unlink()
        state_path = operation_directory / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["mutation_started"] = False
        state["modified"] = False
        state_path.write_text(json.dumps(state), encoding="utf-8")

        code, status, _ = self.invoke(
            (
                "agent", "status", "--operation-id", str(document["operation_id"]),
                "--codex-home", str(self.codex_home),
            )
        )
        self.assertEqual(code, 1)
        self.assertEqual(status["goal_status"], "unknown")
        code, repeated, _ = self.invoke(apply_argv, server=server)
        self.assertEqual(code, 1)
        self.assertEqual(repeated["goal_status"], "unknown")
        self.assertTrue(repeated["mutation_started"])
        self.assertEqual(server.deleted_thread_ids, ["forged-state"])

    def test_status_and_verify_never_trust_result_while_lock_exists(self) -> None:
        plan_path, document, rollout = self.make_plan("locked-result")
        server = _MutatingServer(lambda _thread_id: rollout.unlink())
        code, _result, _ = self.invoke(
            (
                "agent", "apply", "--plan", str(plan_path),
                "--authorized-plan-sha256", str(document["plan_sha256"]),
                "--clients-closed", "--verify-timeout", "0",
            ),
            server=server,
        )
        self.assertEqual(code, 0)
        operation_directory = self.operation_directory(document)
        result_path = operation_directory / "result.json"
        trusted_result = result_path.read_bytes()
        (operation_directory / "apply.lock").write_text("{}\n", encoding="utf-8")

        code, status, _ = self.invoke(
            (
                "agent", "status", "--operation-id", str(document["operation_id"]),
                "--codex-home", str(self.codex_home),
            )
        )
        self.assertEqual(code, 1)
        self.assertEqual(status["goal_status"], "unknown")
        self.assertEqual(status["blockers"][0]["blocker_code"], "operation_locked")

        code, verified, _ = self.invoke(
            (
                "agent", "verify", "--operation-id", str(document["operation_id"]),
                "--codex-home", str(self.codex_home), "--verify-timeout", "0",
            )
        )
        self.assertEqual(code, 1)
        self.assertEqual(verified["goal_status"], "unknown")
        self.assertEqual(result_path.read_bytes(), trusted_result)

    def test_lock_replacement_is_not_unlinked_as_if_still_owned(self) -> None:
        _plan_path, document, _rollout = self.make_plan("lock-replaced")
        store = OperationStore(
            self.codex_home,
            str(document["operation_id"]),
        )
        store.accept_plan(document)
        replacement = store.directory / "replacement.lock"

        with self.assertRaises(OperationStoreError):
            with store.mutation_lock():
                replacement.write_text("replacement\n", encoding="utf-8")
                os.replace(replacement, store.lock_path)

        self.assertTrue(store.lock_path.is_file())
        self.assertEqual(
            store.lock_path.read_text(encoding="utf-8"),
            "replacement\n",
        )

    def test_full_scope_read_failure_after_mutation_stays_unknown(self) -> None:
        plan_path, document, rollout = self.make_plan("full-scan-error")
        server = _MutatingServer(lambda _thread_id: rollout.unlink())
        argv = (
            "agent", "apply", "--plan", str(plan_path),
            "--authorized-plan-sha256", str(document["plan_sha256"]),
            "--clients-closed", "--verify-timeout", "0",
        )
        with patch(
            "local_agent_record_janitor.agent_cli._verify_full_target_scope",
            side_effect=OSError("full target scan failed"),
        ):
            code, result, _ = self.invoke(argv, server=server)
        self.assertEqual(code, 1)
        self.assertEqual(result["goal_status"], "unknown")
        self.assertTrue(result["mutation_started"])
        self.assertEqual(server.deleted_thread_ids, ["full-scan-error"])

        code, repeated, _ = self.invoke(argv, server=server)
        self.assertEqual(code, 1)
        self.assertEqual(repeated["goal_status"], "unknown")
        self.assertEqual(server.deleted_thread_ids, ["full-scan-error"])

    def test_mutation_gate_flush_failure_never_calls_modifier_or_retries(self) -> None:
        plan_path, document, _rollout = self.make_plan("durability-gate")
        operation_directory = self.operation_directory(document)
        calls = 0

        def fail_mutation_state_directory_flush(path: Path) -> None:
            nonlocal calls
            if path == operation_directory:
                calls += 1
                if calls == 6:
                    raise OSError("simulated durable gate failure")

        server = _MutatingServer(lambda _thread_id: self.fail("must not mutate"))
        argv = (
            "agent",
            "apply",
            "--plan",
            str(plan_path),
            "--authorized-plan-sha256",
            str(document["plan_sha256"]),
            "--clients-closed",
            "--verify-timeout",
            "0",
        )
        with patch(
            "local_agent_record_janitor.operation_store._fsync_directory",
            side_effect=fail_mutation_state_directory_flush,
        ):
            code, result, _ = self.invoke(argv, server=server)

        self.assertEqual(code, 1)
        self.assertEqual(result["goal_status"], "unknown")
        self.assertTrue(result["mutation_started"])
        self.assertEqual(server.deleted_thread_ids, [])
        code, repeated, _ = self.invoke(argv, server=server)
        self.assertEqual(code, 1)
        self.assertEqual(repeated["goal_status"], "unknown")
        self.assertEqual(server.deleted_thread_ids, [])

    def test_operation_symlink_is_rejected_before_any_mutation(self) -> None:
        plan_path, document, rollout = self.make_plan("symlink-guard")
        outside = self.root / "outside-operation-store"
        outside.mkdir()
        journal = self.codex_home / ".local-agent-record-janitor"
        try:
            os.symlink(outside, journal, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        server = _MutatingServer(lambda _thread_id: self.fail("must not mutate"))

        code, result, _ = self.invoke(
            (
                "agent",
                "apply",
                "--plan",
                str(plan_path),
                "--authorized-plan-sha256",
                str(document["plan_sha256"]),
                "--clients-closed",
                "--verify-timeout",
                "0",
            ),
            server=server,
        )

        self.assertEqual(code, 3)
        self.assertEqual(result["goal_status"], "blocked")
        self.assertTrue(rollout.is_file())
        self.assertEqual(server.deleted_thread_ids, [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_plan_storage_identity_is_recomputed_not_trusted(self) -> None:
        plan_path, document, rollout = self.make_plan("wrong-storage")
        document["target"]["storage_id"] = "forged-storage-id"
        document["plan_sha256"] = plan_sha256(document)
        plan_path.write_text(json.dumps(document), encoding="utf-8")

        code, result, _ = self.invoke(
            (
                "agent",
                "apply",
                "--plan",
                str(plan_path),
                "--authorized-plan-sha256",
                str(document["plan_sha256"]),
                "--clients-closed",
            )
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            result["blockers"][0]["blocker_code"],
            "plan_schema_invalid",
        )
        self.assertTrue(rollout.is_file())

    def test_verification_retries_transient_read_errors(self) -> None:
        plan = {"target": {}, "authorization": {}}
        exact = {"all_satisfied": True, "verified_action_ids": []}
        full = {"all_satisfied": True, "scan_complete": True}
        with (
            patch(
                "local_agent_record_janitor.agent_cli.verify_frozen_actions",
                side_effect=[OSError("transient"), exact],
            ),
            patch(
                "local_agent_record_janitor.agent_cli._verify_full_target_scope",
                side_effect=[OSError("transient"), full],
            ),
            patch("local_agent_record_janitor.agent_cli.time.sleep"),
        ):
            verification = _verify_with_retry(
                plan,
                None,
                total_timeout=1,
                retry_pending=True,
            )

        self.assertEqual(verification[0], exact)
        self.assertEqual(verification[1], full)
        self.assertEqual(verification[4], 2)

    def test_five_roots_and_132_descendants_count_as_137_threads(self) -> None:
        descendant_counts = (27, 27, 26, 26, 26)
        actions = []
        for root_number, descendant_count in enumerate(descendant_counts):
            root_id = f"root-{root_number}"
            affected = (root_id,) + tuple(
                f"child-{root_number}-{index}"
                for index in range(descendant_count)
            )
            actions.append(
                SimpleNamespace(
                    action_id=f"action-{root_number}",
                    kind="delete_conversation",
                    executable=True,
                    target=SimpleNamespace(
                        storage_id="storage",
                        thread_id=root_id,
                    ),
                    impact=SimpleNamespace(
                        affected_thread_ids=affected,
                        index_record_count=len(affected),
                        rollout_file_count=len(affected),
                        frontend_reference_count=100,
                        frontend_residual_count=100,
                        desktop_catalog_record_count=0,
                        desktop_global_state_reference_count=0,
                        legacy_residual_line_count=0,
                        legacy_residual_thread_ids=(),
                    ),
                )
            )

        counts = plan_counts(
            findings=(),
            actions=actions,
            root_actions=actions,
        )

        self.assertEqual(counts["root_action_count"], 5)
        self.assertEqual(counts["affected_thread_count"], 137)
        self.assertEqual(counts["artifact_count"], 274)


if __name__ == "__main__":
    unittest.main()
