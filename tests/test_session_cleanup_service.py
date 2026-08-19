from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.cli import main
from local_agent_record_janitor.operation_store import plan_sha256


ONE = "11111111-1111-4111-8111-111111111111"
TWO = "22222222-2222-4222-8222-222222222222"


class SessionCleanupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.appdata = self.root / "appdata"
        self.appdata.mkdir()

    def invoke(self, argv: list[str]) -> tuple[int, dict[str, object], str]:
        output = StringIO()
        errors = StringIO()
        code = main(
            argv,
            stdin=StringIO(),
            stdout=output,
            stderr=errors,
            app_server_factory=lambda **_kwargs: self.fail(
                "Pi/Claude cleanup must not start a Codex app-server"
            ),
            binary_resolver=lambda _hint: self.fail(
                "Pi/Claude cleanup must not resolve a Codex binary"
            ),
        )
        self.assertEqual(errors.getvalue(), "")
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        return code, json.loads(lines[0]), output.getvalue()

    def write_pi(self, root: Path, session_id: str, secret: str) -> Path:
        path = root / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "type": "session",
                    "id": session_id,
                    "version": 3,
                    "cwd": str(self.root / "project"),
                }
            )
            + "\n"
            + json.dumps({"type": "message", "message": secret})
            + "\n",
            encoding="utf-8",
        )
        return path

    def write_claude(self, config: Path, session_id: str, secret: str) -> Path:
        path = config / "projects" / "project" / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"sessionId": session_id, "message": secret}) + "\n",
            encoding="utf-8",
        )
        return path

    def test_agent_pi_requires_explicit_selection(self) -> None:
        agent_dir = self.root / "pi-agent"
        sessions = agent_dir / "sessions"
        transcript = self.write_pi(sessions, "pi-one", "SECRET_PI_BODY")
        plan_path = self.root / "pi-empty-plan.json"

        code, summary, rendered = self.invoke(
            [
                "agent",
                "plan",
                "--platform",
                "pi",
                "--appdata",
                str(self.appdata),
                "--pi-agent-dir",
                str(agent_dir),
                "--pi-session-dir",
                str(sessions),
                "--out",
                str(plan_path),
            ]
        )

        self.assertEqual(code, 0)
        self.assertFalse(summary["authorization_required"])
        self.assertIn(
            "explicit_session_selection_required",
            {item["blocker_code"] for item in summary["blockers"]},
        )
        self.assertTrue(transcript.is_file())
        self.assertNotIn("SECRET_PI_BODY", rendered)
        self.assertNotIn("SECRET_PI_BODY", plan_path.read_text(encoding="utf-8"))

    def test_agent_pi_apply_uses_two_full_scans_and_keeps_unrelated_session(self) -> None:
        agent_dir = self.root / "pi-agent"
        sessions = agent_dir / "sessions"
        selected = self.write_pi(sessions, "pi-one", "SECRET_SELECTED_PI")
        unrelated = self.write_pi(sessions, "pi-two", "SECRET_OTHER_PI")
        plan_path = self.root / "pi-plan.json"
        common = [
            "--platform",
            "pi",
            "--appdata",
            str(self.appdata),
            "--pi-agent-dir",
            str(agent_dir),
            "--pi-session-dir",
            str(sessions),
        ]
        code, summary, _ = self.invoke(
            [
                "agent",
                "plan",
                *common,
                "--session-id",
                "pi-one",
                "--out",
                str(plan_path),
            ]
        )
        self.assertEqual(code, 0)
        document = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(document["plan_sha256"], plan_sha256(document))
        self.assertEqual(
            document["authorization"]["mutation_kind"],
            "delete_pi_session",
        )
        self.assertEqual(document["counts"]["artifact_count"], 1)
        self.assertNotIn("SECRET_SELECTED_PI", plan_path.read_text(encoding="utf-8"))

        from local_agent_record_janitor import session_catalog_factory

        original_builder = session_catalog_factory.build_pi_catalog
        full_scans = 0

        def counted_builder(*args: object, **kwargs: object) -> object:
            nonlocal full_scans
            full_scans += 1
            return original_builder(*args, **kwargs)

        with patch.object(
            session_catalog_factory,
            "build_pi_catalog",
            side_effect=counted_builder,
        ):
            code, result, rendered = self.invoke(
                [
                    "agent",
                    "apply",
                    "--plan",
                    str(plan_path),
                    "--authorized-plan-sha256",
                    str(summary["plan_sha256"]),
                    "--clients-closed",
                    "--verify-timeout",
                    "0",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(result["goal_status"], "complete")
        self.assertTrue(result["mutation_started"])
        self.assertEqual(full_scans, 2)
        self.assertFalse(selected.exists())
        self.assertTrue(unrelated.is_file())
        self.assertNotIn("SECRET_SELECTED_PI", rendered)
        self.assertNotIn("SECRET_OTHER_PI", rendered)
        operation_id = str(document["operation_id"])
        operation = (
            agent_dir
            / ".local-agent-record-janitor"
            / "operations"
            / operation_id
        )
        self.assertEqual(
            {path.name for path in operation.iterdir()},
            {"receipt.json"},
        )
        self.assertFalse((sessions / ".local-agent-record-janitor").exists())
        for path in operation.iterdir():
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("SECRET_SELECTED_PI", text)
                self.assertNotIn("SECRET_OTHER_PI", text)

        status_code, status, _ = self.invoke(
            [
                "agent",
                "status",
                "--operation-id",
                operation_id,
                "--operation-home",
                str(agent_dir),
            ]
        )
        self.assertEqual(status_code, 0)
        self.assertEqual(status["goal_status"], "complete")

    def test_agent_claude_apply_preserves_other_config_data(self) -> None:
        config = self.root / "claude"
        transcript = self.write_claude(config, ONE, "SECRET_CLAUDE_BODY")
        other = self.write_claude(config, TWO, "SECRET_OTHER_CLAUDE")
        settings = config / "settings.json"
        settings.write_bytes(b"preserve settings")
        plan_path = self.root / "claude-plan.json"
        code, summary, _ = self.invoke(
            [
                "agent",
                "plan",
                "--platform",
                "claude",
                "--appdata",
                str(self.appdata),
                "--claude-config-dir",
                str(config),
                "--session-id",
                ONE,
                "--out",
                str(plan_path),
            ]
        )
        self.assertEqual(code, 0)
        document = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(
            document["authorization"]["mutation_kind"],
            "delete_claude_session",
        )

        code, result, rendered = self.invoke(
            [
                "agent",
                "apply",
                "--plan",
                str(plan_path),
                "--authorized-plan-sha256",
                str(summary["plan_sha256"]),
                "--clients-closed",
                "--verify-timeout",
                "0",
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(result["goal_status"], "complete")
        self.assertFalse(transcript.exists())
        self.assertTrue(other.is_file())
        self.assertEqual(settings.read_bytes(), b"preserve settings")
        self.assertNotIn("SECRET_CLAUDE_BODY", rendered)
        self.assertNotIn("SECRET_OTHER_CLAUDE", rendered)


if __name__ == "__main__":
    unittest.main()
