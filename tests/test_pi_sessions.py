from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from codex_session_janitor.pi_sessions import (
    PiSelectionError,
    build_pi_session_catalog,
    resolve_pi_paths,
    select_pi_sessions,
)


class PiSessionCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.agent = self.home / ".pi" / "agent"
        self.sessions = self.agent / "sessions"
        self.project = self.root / "project"
        self.agent.mkdir(parents=True)
        self.project.mkdir()

    def _env(self, **extra: str) -> dict[str, str]:
        return {"PI_CODING_AGENT_DIR": str(self.agent), **extra}

    def _write_session(
        self,
        relative: str,
        *,
        session_id: str = "session-1",
        version: int | None = 3,
        parent_session: str | None = None,
        entries: list[dict[str, object]] | None = None,
    ) -> Path:
        path = self.sessions / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        header: dict[str, object] = {
            "type": "session",
            "id": session_id,
            "timestamp": "2026-08-01T00:00:00.000Z",
            "cwd": str(self.project),
        }
        if version is not None:
            header["version"] = version
        if parent_session is not None:
            header["parentSession"] = parent_session
        payload = [header, *(entries or [])]
        path.write_text("\n".join(json.dumps(item) for item in payload) + "\n", encoding="utf-8")
        return path

    def test_resolves_pi_precedence_and_relative_paths_like_current_pi(self) -> None:
        (self.agent / "settings.json").write_text(json.dumps({"sessionDir": "global-sessions"}), encoding="utf-8")
        project_settings = self.project / ".pi"
        project_settings.mkdir()
        (project_settings / "settings.json").write_text(json.dumps({"sessionDir": "project-sessions"}), encoding="utf-8")

        project = resolve_pi_paths(environ=self._env(), cwd=self.project, home=self.home)
        env = resolve_pi_paths(environ=self._env(PI_CODING_AGENT_SESSION_DIR="env-sessions"), cwd=self.project, home=self.home)
        argument = resolve_pi_paths(environ=self._env(), cwd=self.project, home=self.home, session_root="argument-sessions")

        self.assertEqual(project.session_root, self.project / "project-sessions")
        self.assertEqual(project.session_root_source, "project_settings")
        self.assertEqual(env.session_root, self.project / "env-sessions")
        self.assertEqual(argument.session_root, self.project / "argument-sessions")

    def test_parses_v1_v2_v3_and_metadata_without_exposing_bodies(self) -> None:
        self._write_session("--one--/v1.jsonl", session_id="v1", version=None)
        self._write_session("--two--/v2.jsonl", session_id="v2", version=2)
        self._write_session(
            "--three--/v3.jsonl",
            session_id="v3",
            entries=[
                {"type": "message", "id": "a", "parentId": None, "timestamp": "x", "message": {"role": "user", "content": "secret user body"}},
                {"type": "model_change", "id": "b", "parentId": "a", "timestamp": "x", "provider": "openai-codex", "modelId": "gpt-5.6-codex"},
                {"type": "session_info", "id": "c", "parentId": "b", "timestamp": "x", "name": "Named session"},
            ],
        )

        catalog = build_pi_session_catalog(environ=self._env(), cwd=self.project, home=self.home)
        by_id = {record.session_id: record for record in catalog.records}

        self.assertEqual({key: item.version for key, item in by_id.items()}, {"v1": None, "v2": 2, "v3": 3})
        self.assertTrue(by_id["v3"].used_openai_codex)
        self.assertEqual(by_id["v3"].provider, "openai-codex")
        self.assertEqual(by_id["v3"].model, "gpt-5.6-codex")
        self.assertEqual(by_id["v3"].session_name, "Named session")
        self.assertNotIn("secret user body", json.dumps(catalog.to_dict()))
        approval_file = by_id["v3"].approval_payload()["file"]
        self.assertIn("sha256", approval_file)
        self.assertEqual(
            approval_file["st_file_attributes"],
            getattr(os.lstat(by_id["v3"].path), "st_file_attributes", 0),
        )

    def test_malformed_file_is_visible_and_blocks_every_record(self) -> None:
        self._write_session("--project--/good.jsonl", session_id="good")
        malformed = self.sessions / "--project--/bad.jsonl"
        malformed.write_text("{not valid json}\n", encoding="utf-8")

        catalog = build_pi_session_catalog(environ=self._env(), cwd=self.project, home=self.home)

        self.assertEqual([record.session_id for record in catalog.records], ["good"])
        self.assertFalse(catalog.records[0].deletable)
        self.assertTrue(catalog.errors)
        self.assertNotIn("not valid json", json.dumps(catalog.to_dict()))

    def test_active_duplicate_and_child_reference_are_conservative(self) -> None:
        parent = self._write_session("--project--/parent.jsonl", session_id="same")
        child = self._write_session("--project--/child.jsonl", session_id="child", parent_session=str(parent))
        self._write_session("--other--/duplicate.jsonl", session_id="same")

        catalog = build_pi_session_catalog(
            environ=self._env(PI_SESSION_FILE=str(child)), cwd=self.project, home=self.home
        )
        by_path = {record.path: record for record in catalog.records}

        self.assertTrue(by_path[child].active)
        self.assertFalse(by_path[child].deletable)
        self.assertIn(child, by_path[parent].child_paths)
        self.assertFalse(by_path[parent].deletable)
        self.assertGreaterEqual(sum(error.error_type == "DuplicateSessionId" for error in catalog.errors), 2)
        with self.assertRaises(PiSelectionError):
            select_pi_sessions(catalog, ["same"])
        self.assertEqual(select_pi_sessions(catalog, [by_path[child].action_id]), (by_path[child],))

    def test_missing_root_is_empty_but_deeper_layout_is_a_failure(self) -> None:
        missing = build_pi_session_catalog(environ=self._env(), cwd=self.project, home=self.home)
        self.assertEqual(missing.records, ())
        self.assertEqual(missing.errors, ())

        self.sessions.mkdir(parents=True)
        nested = self.sessions / "--project--" / "too-deep"
        nested.mkdir(parents=True)
        (nested / "hidden.jsonl").write_text("{}\n", encoding="utf-8")
        catalog = build_pi_session_catalog(environ=self._env(), cwd=self.project, home=self.home)
        self.assertTrue(catalog.errors)
        self.assertIn("deeper", catalog.errors[0].message)

    def test_existing_non_directory_session_root_is_a_failure(self) -> None:
        self.sessions.write_text("not a directory", encoding="utf-8")

        catalog = build_pi_session_catalog(environ=self._env(), cwd=self.project, home=self.home)

        self.assertTrue(catalog.errors)
        self.assertIn("not a directory", catalog.errors[0].message)

    @unittest.skipUnless(hasattr(os, "symlink"), "platform lacks symlink support")
    def test_symlink_is_explicitly_rejected(self) -> None:
        self.sessions.mkdir(parents=True)
        target = self.root / "outside.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        link = self.sessions / "linked.jsonl"
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")

        catalog = build_pi_session_catalog(environ=self._env(), cwd=self.project, home=self.home)
        self.assertTrue(catalog.errors)
        self.assertIn("link", catalog.errors[0].message)


if __name__ == "__main__":
    unittest.main()
