from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from codex_session_janitor.claude_sessions import (
    build_claude_multi_root_catalog,
    build_claude_session_catalog,
)
from codex_session_janitor.claude_delete import build_claude_delete_plan
from codex_session_janitor.cli import (
    EXIT_CONFIRMATION_REQUIRED,
    EXIT_ERROR,
    EXIT_OK,
    build_parser,
    main,
)


ONE = "11111111-1111-4111-8111-111111111111"
TWO = "22222222-2222-4222-8222-222222222222"


class TTYStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def create_cindy_database(
    path: Path,
    sessions: list[tuple[str, str, str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                sdk_session_id TEXT,
                status TEXT,
                agent_kind TEXT,
                working_dir TEXT,
                updated_at INTEGER
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                created_at INTEGER,
                rewind_at INTEGER
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO sessions
                (id, sdk_session_id, status, agent_kind, working_dir, updated_at)
            VALUES (?, ?, ?, ?, '/work', 100)
            """,
            sessions,
        )
        connection.commit()


def write_claude_session(config: Path, session_id: str, secret: str) -> Path:
    transcript = config / "projects" / "project" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps({"sessionId": session_id, "message": secret}) + "\n",
        encoding="utf-8",
    )
    return transcript


def write_pi_session(root: Path, session_id: str, secret: str) -> Path:
    path = root / "--project--" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session", "id": session_id, "version": 3})
        + "\n"
        + json.dumps({"type": "message", "message": secret})
        + "\n",
        encoding="utf-8",
    )
    return path


class MultiEngineCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / ".claude"
        self.transcript = write_claude_session(self.config, ONE, "claude secret body")

    def claude_builder(self, **_kwargs: object):
        return build_claude_session_catalog(config_dir=self.config)

    def test_parser_publicly_documents_claude_platform_and_config_root(self) -> None:
        parser = build_parser()
        command_action = next(action for action in parser._actions if action.dest == "command")
        records_help = command_action.choices["records"].format_help()
        delete_help = command_action.choices["delete"].format_help()
        self.assertIn("--claude-config-dir PATH", records_help)
        self.assertIn("Claude Code", records_help)
        self.assertIn("--platform", delete_help)
        self.assertIn("--session-id ID", delete_help)
        self.assertIn("Codex thread 或 Pi/Claude Code session", delete_help)
        self.assertIn("逐项选择存储身份明确的目标", delete_help)

    def test_claude_records_json_and_human_are_metadata_only_and_all_totals_are_separate(self) -> None:
        output = StringIO()
        status = main(
            ["records", "--platform", "claude", "--json"],
            adapters=(), stdout=output, stderr=StringIO(), stdin=StringIO(),
            claude_catalog_builder=self.claude_builder,
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(payload["records"], [])
        self.assertEqual(payload["claude_count"], 1)
        self.assertEqual(payload["total_count"], 1)
        self.assertNotIn("claude secret body", output.getvalue())

        human = StringIO()
        status = main(
            ["records", "--platform", "claude"],
            adapters=(), stdout=human, stderr=StringIO(), stdin=StringIO(),
            claude_catalog_builder=self.claude_builder,
        )
        self.assertEqual(status, EXIT_OK)
        rendered = human.getvalue()
        self.assertIn("Cindy 使用情况：未发现 Cindy 正在使用这个会话", rendered)
        self.assertIn("删除资格：可以选择删除", rendered)
        self.assertIn("本地会话文件：1 份", rendered)
        self.assertIn("不显示正文", rendered)
        self.assertNotIn("claude secret body", rendered)
        self.assertNotIn("unreferenced", rendered)
        self.assertNotIn("classification", rendered)
        self.assertNotIn("storage", rendered)

        mixed = StringIO()
        status = main(
            ["records", "--json"], adapters=(), stdout=mixed,
            stderr=StringIO(), stdin=StringIO(),
            pi_catalog_builder=lambda **_kwargs: type(
                "PiCatalog", (), {"records": (), "errors": (), "to_dict": lambda self: {"records": [], "errors": []}}
            )(),
            claude_catalog_builder=self.claude_builder,
        )
        payload = json.loads(mixed.getvalue())
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(
            payload["total_count"],
            payload["count"] + payload["pi_count"] + payload["claude_count"],
        )

    def test_claude_preview_execution_fingerprint_and_shared_file_preservation(self) -> None:
        shared = self.config / "settings.json"
        shared.write_bytes(b"shared settings")
        preview_output = StringIO()
        status = main(
            ["delete", "--platform", "claude", "--session-id", ONE, "--json"],
            stdout=preview_output, stderr=StringIO(), stdin=StringIO(),
            claude_catalog_builder=self.claude_builder,
        )
        preview = json.loads(preview_output.getvalue())
        self.assertEqual(status, EXIT_CONFIRMATION_REQUIRED)
        self.assertTrue(self.transcript.exists())
        self.assertEqual(preview["platform"], "claude")
        self.assertTrue(preview["shared_records_preserved"])

        missing_clients = StringIO()
        status = main(
            [
                "delete", "--platform", "claude", "--session-id", ONE,
                "--plan-fingerprint", preview["plan_fingerprint"], "--yes", "--json",
            ],
            stdout=missing_clients, stderr=StringIO(), stdin=StringIO(),
            claude_catalog_builder=self.claude_builder,
        )
        self.assertEqual(status, EXIT_ERROR)
        self.assertTrue(self.transcript.exists())

        missing_fingerprint = StringIO()
        status = main(
            [
                "delete", "--platform", "claude", "--session-id", ONE,
                "--clients-closed", "--yes", "--json",
            ],
            stdout=missing_fingerprint, stderr=StringIO(), stdin=StringIO(),
            claude_catalog_builder=self.claude_builder,
        )
        self.assertEqual(status, EXIT_ERROR)
        self.assertTrue(self.transcript.exists())

        wrong = StringIO()
        status = main(
            [
                "delete", "--platform", "claude", "--session-id", ONE,
                "--plan-fingerprint", "sha256:wrong", "--clients-closed",
                "--yes", "--json",
            ],
            stdout=wrong, stderr=StringIO(), stdin=StringIO(),
            claude_catalog_builder=self.claude_builder,
        )
        self.assertEqual(status, EXIT_ERROR)
        self.assertTrue(self.transcript.exists())

        executed = StringIO()
        status = main(
            [
                "delete", "--platform", "claude", "--session-id", ONE,
                "--plan-fingerprint", preview["plan_fingerprint"],
                "--clients-closed", "--yes", "--json",
            ],
            stdout=executed, stderr=StringIO(), stdin=StringIO(),
            claude_catalog_builder=self.claude_builder,
        )
        result = json.loads(executed.getvalue())
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(result["results"][0]["status"], "deleted")
        self.assertFalse(self.transcript.exists())
        self.assertEqual(shared.read_bytes(), b"shared settings")

    def test_claude_tty_number_and_backend_specific_confirmation(self) -> None:
        input_stream = TTYStringIO(
            "1\nClaude Code 客户端已关闭并确认永久删除\n"
        )
        output = TTYStringIO()
        status = main(
            ["delete", "--platform", "claude"],
            stdin=input_stream, stdout=output, stderr=StringIO(),
            claude_catalog_builder=self.claude_builder,
        )
        self.assertEqual(status, EXIT_OK)
        rendered = output.getvalue()
        self.assertIn(f"1. 会话 ID：{ONE}", rendered)
        self.assertIn("共享历史记录和索引", rendered)
        self.assertIn(f"已删除：会话 ID {ONE}", rendered)
        self.assertNotIn("shared history/index", rendered)
        self.assertNotIn(" deleted ", rendered)
        self.assertFalse(self.transcript.exists())

    def test_claude_non_tty_missing_inputs_and_mixed_selectors_are_rejected(self) -> None:
        commands = (
            ["delete", "--platform", "claude", "--json"],
            ["delete", "--platform", "claude", "--session-id", "all", "--json"],
            ["delete", "--platform", "claude", "--thread-id", ONE, "--json"],
            [
                "delete", "--platform", "claude", "--session-id", ONE,
                "--action-id", "anything", "--json",
            ],
            [
                "delete", "--platform", "claude", "--platform", "native",
                "--session-id", ONE, "--json",
            ],
        )
        for command in commands:
            with self.subTest(command=command):
                output = StringIO()
                status = main(
                    command, stdout=output, stderr=StringIO(), stdin=StringIO(),
                    claude_catalog_builder=self.claude_builder,
                )
                self.assertEqual(status, EXIT_ERROR)
                self.assertIn("error", json.loads(output.getvalue()))
                self.assertTrue(self.transcript.exists())

    def test_default_claude_discovery_decorates_production_cindy_and_shows_frontend_only(self) -> None:
        home = self.root / "home"
        config = home / ".claude"
        write_claude_session(config, ONE, "live body")
        write_claude_session(config, TWO, "deleted body")
        appdata = self.root / "AppData"
        database = appdata / "Cindy" / "cindy-local-v1.db"
        # A stale production dedicated directory is inventory-worthy but must
        # not steal refs from the shared default Claude root.
        (database.parent / "claude-home").mkdir(parents=True)
        missing = "33333333-3333-4333-8333-333333333333"
        create_cindy_database(
            database,
            [
                ("live", ONE, "active", "cc"),
                ("deleted", TWO, "deleted", "cc"),
                ("missing", missing, "active", "cc"),
            ],
        )
        output = StringIO()
        with patch("pathlib.Path.home", return_value=home):
            status = main(
                ["records", "--platform", "claude", "--appdata", str(appdata), "--json"],
                stdout=output, stderr=StringIO(), stdin=StringIO(),
            )
        payload = json.loads(output.getvalue())
        by_id = {item["session_id"]: item for item in payload["claude_sessions"]}
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(by_id[ONE]["classification"], "live_current_reference")
        self.assertEqual(by_id[TWO]["classification"], "deleted_frontend_reference")
        self.assertEqual(by_id[missing]["classification"], "frontend_only")
        self.assertNotIn("live body", output.getvalue())
        self.assertNotIn("deleted body", output.getvalue())

    def test_explicit_cindy_claude_db_keeps_live_sibling_namespace_guard(self) -> None:
        appdata = self.root / "SiblingClaudeAppData"
        cindy_root = appdata / "CustomCindy"
        config = cindy_root / "claude-home"
        write_claude_session(config, ONE, "sibling guarded body")
        local = cindy_root / "cindy-local-v1.db"
        owner = cindy_root / "cindy-owner-fixture.db"
        create_cindy_database(local, [("local-deleted", ONE, "deleted", "cc")])
        create_cindy_database(owner, [("owner-live", ONE, "active", "cc")])
        output = StringIO()

        status = main(
            [
                "records", "--platform", "claude", "--json",
                "--appdata", str(appdata),
                "--claude-config-dir", str(config),
                "--cindy-root", str(cindy_root),
                "--cindy-db", str(local),
            ],
            stdout=output,
            stderr=StringIO(),
            stdin=StringIO(),
        )
        payload = json.loads(output.getvalue())
        record = next(item for item in payload["claude_sessions"] if item["session_id"] == ONE)

        self.assertEqual(status, EXIT_OK)
        self.assertEqual(record["classification"], "live_current_reference")
        self.assertFalse(record["deletable"])
        self.assertEqual(len(record["frontend_reference_snapshot"]), 2)
        self.assertNotIn("sibling guarded body", output.getvalue())

    def test_production_cindy_refs_follow_effective_environment_config_root(self) -> None:
        home = self.root / "env-home"
        env_config = self.root / "env-claude"
        write_claude_session(env_config, ONE, "environment body")
        appdata = self.root / "EnvAppData"
        database = appdata / "CindyGlobal" / "cindy-local-v1.db"
        create_cindy_database(database, [("live", ONE, "active", "cc")])
        output = StringIO()
        with patch("pathlib.Path.home", return_value=home), patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(env_config)}, clear=True
        ):
            status = main(
                ["records", "--platform", "claude", "--appdata", str(appdata), "--json"],
                stdout=output, stderr=StringIO(), stdin=StringIO(),
            )
        payload = json.loads(output.getvalue())
        record = next(item for item in payload["claude_sessions"] if item["session_id"] == ONE)
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(
            os.path.normcase(record["config_dir"]),
            os.path.normcase(str(env_config)),
        )
        self.assertEqual(record["classification"], "live_current_reference")
        self.assertFalse(record["deletable"])

    def test_explicit_custom_root_with_unattributed_production_ref_is_fail_closed(self) -> None:
        custom = self.root / "explicit-custom"
        write_claude_session(custom, ONE, "custom body")
        appdata = self.root / "ExplicitAppData"
        database = appdata / "Cindy" / "cindy-local-v1.db"
        create_cindy_database(database, [("live", ONE, "active", "cc")])
        output = StringIO()
        status = main(
            [
                "records", "--platform", "claude", "--claude-config-dir", str(custom),
                "--appdata", str(appdata), "--json",
            ],
            stdout=output, stderr=StringIO(), stdin=StringIO(),
        )
        payload = json.loads(output.getvalue())
        record = next(item for item in payload["claude_sessions"] if item["session_id"] == ONE)
        self.assertEqual(status, EXIT_ERROR)
        self.assertEqual(record["classification"], "inventory_incomplete")
        self.assertFalse(record["deletable"])
        self.assertTrue(payload["claude_failures"])

    def test_surviving_cindy_dev_claude_home_is_discovered_without_database(self) -> None:
        home = self.root / "orphan-home"
        appdata = self.root / "OrphanAppData"
        dedicated = appdata / "CindyDev" / "claude-home"
        write_claude_session(dedicated, TWO, "dedicated body")
        output = StringIO()
        with patch("pathlib.Path.home", return_value=home), patch.dict(
            os.environ, {}, clear=True
        ):
            status = main(
                ["records", "--platform", "claude", "--appdata", str(appdata), "--json"],
                stdout=output, stderr=StringIO(), stdin=StringIO(),
            )
        payload = json.loads(output.getvalue())
        record = next(item for item in payload["claude_sessions"] if item["session_id"] == TWO)
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(os.path.normcase(record["config_dir"]), os.path.normcase(str(dedicated)))
        self.assertEqual(record["classification"], "unreferenced")

    def test_default_pi_inventory_keeps_standalone_and_cindy_storage_distinct(self) -> None:
        home = self.root / "pi-home"
        write_pi_session(home / ".pi" / "agent" / "sessions", "standalone", "standalone secret")
        appdata = self.root / "PiAppData"
        cindy_root = appdata / "Cindy"
        write_pi_session(cindy_root / "pi-agent-home" / "sessions", "cindy-live", "cindy secret")
        create_cindy_database(
            cindy_root / "cindy-local-v1.db",
            [("maker", "cindy-live", "active", "pi")],
        )
        output = StringIO()
        with patch("pathlib.Path.home", return_value=home), patch.dict(
            os.environ, {}, clear=True
        ):
            status = main(
                ["records", "--platform", "pi", "--appdata", str(appdata), "--json"],
                stdout=output, stderr=StringIO(), stdin=StringIO(),
            )
        payload = json.loads(output.getvalue())
        by_id = {item["session_id"]: item for item in payload["pi_sessions"]}
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(by_id["standalone"]["storage_kind"], "standalone")
        self.assertEqual(by_id["cindy-live"]["storage_kind"], "cindy")
        self.assertEqual(
            by_id["cindy-live"]["reference_classification"],
            "live_current_reference",
        )
        self.assertFalse(by_id["cindy-live"]["deletable"])
        self.assertNotIn("standalone secret", output.getvalue())
        self.assertNotIn("cindy secret", output.getvalue())

    def test_explicit_cindy_pi_db_keeps_live_sibling_namespace_guard(self) -> None:
        appdata = self.root / "SiblingPiAppData"
        cindy_root = appdata / "CustomCindy"
        sessions = cindy_root / "pi-agent-home" / "sessions"
        write_pi_session(sessions, "shared-pi", "sibling guarded pi body")
        local = cindy_root / "cindy-local-v1.db"
        owner = cindy_root / "cindy-owner-fixture.db"
        create_cindy_database(
            local,
            [("local-deleted", "shared-pi", "deleted", "pi")],
        )
        create_cindy_database(
            owner,
            [("owner-live", "shared-pi", "active", "pi")],
        )
        output = StringIO()

        status = main(
            [
                "records", "--platform", "pi", "--json",
                "--appdata", str(appdata),
                "--cindy-root", str(cindy_root),
                "--cindy-db", str(local),
            ],
            stdout=output,
            stderr=StringIO(),
            stdin=StringIO(),
        )
        payload = json.loads(output.getvalue())
        record = next(item for item in payload["pi_sessions"] if item["session_id"] == "shared-pi")

        self.assertEqual(status, EXIT_OK)
        self.assertEqual(record["reference_classification"], "live_current_reference")
        self.assertFalse(record["deletable"])
        self.assertEqual(len(record["cindy_references"]), 2)
        self.assertNotIn("sibling guarded pi body", output.getvalue())

    def test_surviving_known_cindy_pi_store_without_database_is_listed_once(self) -> None:
        home = self.root / "orphan-pi-home"
        appdata = self.root / "OrphanPiAppData"
        cindy_sessions = appdata / "CindyDev" / "pi-agent-home" / "sessions"
        write_pi_session(cindy_sessions, "orphan-cindy", "orphan secret")
        write_pi_session(
            appdata / "UnknownBrand" / "pi-agent-home" / "sessions",
            "unknown-brand",
            "unknown secret",
        )

        with patch("pathlib.Path.home", return_value=home), patch.dict(
            os.environ, {}, clear=True
        ):
            output = StringIO()
            status = main(
                ["records", "--platform", "pi", "--appdata", str(appdata), "--json"],
                stdout=output,
                stderr=StringIO(),
                stdin=StringIO(),
            )
        payload = json.loads(output.getvalue())

        self.assertEqual(status, EXIT_OK)
        self.assertEqual(payload["pi_count"], 1)
        record = payload["pi_sessions"][0]
        self.assertEqual(record["session_id"], "orphan-cindy")
        self.assertEqual(record["storage_kind"], "cindy")
        self.assertEqual(record["reference_classification"], "unreferenced")
        self.assertTrue(record["deletable"])
        self.assertNotIn("unknown-brand", output.getvalue())

    def test_explicit_cindy_pi_agent_or_session_dir_retains_live_guards(self) -> None:
        appdata = self.root / "ExplicitPiAppData"
        cindy_root = appdata / "Cindy"
        agent_home = cindy_root / "pi-agent-home"
        sessions = agent_home / "sessions"
        write_pi_session(sessions, "cindy-current", "current secret")
        write_pi_session(sessions, "cindy-parked", "parked secret")
        database = cindy_root / "cindy-local-v1.db"
        create_cindy_database(
            database,
            [
                ("current-maker", "cindy-current", "active", "pi"),
                ("parked-maker", "codex-current", "active", "codex"),
            ],
        )
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                """
                INSERT INTO messages
                    (id, session_id, role, content, created_at, rewind_at)
                VALUES ('switch', 'parked-maker', 'agent_switch', ?, 50, NULL)
                """,
                (
                    json.dumps(
                        {
                            "fromAgentKind": "pi",
                            "fromSdkSessionId": "cindy-parked",
                        }
                    ),
                ),
            )
            connection.commit()

        choices = (
            ["--pi-agent-dir", str(agent_home)],
            ["--pi-session-dir", str(sessions)],
        )
        for explicit in choices:
            with self.subTest(explicit=explicit):
                output = StringIO()
                status = main(
                    [
                        "records", "--platform", "pi", "--appdata", str(appdata),
                        *explicit, "--json",
                    ],
                    stdout=output,
                    stderr=StringIO(),
                    stdin=StringIO(),
                )
                payload = json.loads(output.getvalue())
                by_id = {item["session_id"]: item for item in payload["pi_sessions"]}
                self.assertEqual(status, EXIT_OK)
                self.assertEqual(payload["pi_count"], 2)
                self.assertEqual(by_id["cindy-current"]["storage_kind"], "cindy")
                self.assertEqual(
                    by_id["cindy-current"]["reference_classification"],
                    "live_current_reference",
                )
                self.assertFalse(by_id["cindy-current"]["deletable"])
                self.assertEqual(
                    by_id["cindy-parked"]["reference_classification"],
                    "live_historical_reference",
                )
                self.assertFalse(by_id["cindy-parked"]["deletable"])
                self.assertNotIn("current secret", output.getvalue())
                self.assertNotIn("parked secret", output.getvalue())

    def test_explicit_cindy_pi_deleted_reference_remains_individually_eligible(self) -> None:
        appdata = self.root / "DeletedExplicitPiAppData"
        cindy_root = appdata / "CindyGlobal"
        agent_home = cindy_root / "pi-agent-home"
        sessions = agent_home / "sessions"
        write_pi_session(sessions, "deleted-pi", "deleted secret")
        create_cindy_database(
            cindy_root / "cindy-local-v1.db",
            [("deleted-maker", "deleted-pi", "deleted", "pi")],
        )

        output = StringIO()
        status = main(
            [
                "records", "--platform", "pi", "--appdata", str(appdata),
                "--pi-agent-dir", str(agent_home), "--json",
            ],
            stdout=output,
            stderr=StringIO(),
            stdin=StringIO(),
        )
        payload = json.loads(output.getvalue())
        record = payload["pi_sessions"][0]

        self.assertEqual(status, EXIT_OK)
        self.assertEqual(record["storage_kind"], "cindy")
        self.assertEqual(
            record["reference_classification"], "deleted_frontend_reference"
        )
        self.assertTrue(record["deletable"])

    def test_explicit_cindy_pi_database_failure_blocks_only_that_selected_root(self) -> None:
        appdata = self.root / "BrokenExplicitPiAppData"
        cindy_root = appdata / "CindyDev"
        agent_home = cindy_root / "pi-agent-home"
        sessions = agent_home / "sessions"
        write_pi_session(sessions, "candidate", "candidate secret")
        database = cindy_root / "cindy-local-v1.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE sessions (id TEXT)")
            connection.commit()

        output = StringIO()
        status = main(
            [
                "records", "--platform", "pi", "--appdata", str(appdata),
                "--pi-session-dir", str(sessions), "--json",
            ],
            stdout=output,
            stderr=StringIO(),
            stdin=StringIO(),
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(status, EXIT_ERROR)
        self.assertEqual(payload["pi_count"], 1)
        self.assertFalse(payload["pi_sessions"][0]["deletable"])
        self.assertTrue(payload["pi_failures"])
        self.assertEqual(payload["pi_failures"][0]["source"], "cindy-reference")

    def test_environment_pi_dirs_matching_cindy_are_deduplicated_and_guarded(self) -> None:
        home = self.root / "env-pi-home"
        appdata = self.root / "EnvPiAppData"
        cindy_root = appdata / "Cindy"
        agent_home = cindy_root / "pi-agent-home"
        sessions = agent_home / "sessions"
        active_path = write_pi_session(sessions, "env-live", "environment secret")
        create_cindy_database(
            cindy_root / "cindy-local-v1.db",
            [("env-maker", "env-live", "active", "pi")],
        )

        environments = (
            {
                "PI_CODING_AGENT_DIR": str(agent_home),
                "PI_SESSION_FILE": str(active_path),
            },
            {
                "PI_CODING_AGENT_SESSION_DIR": str(sessions),
                "PI_SESSION_FILE": str(active_path),
            },
        )
        for environment in environments:
            with self.subTest(environment=environment), patch(
                "pathlib.Path.home", return_value=home
            ), patch.dict(os.environ, environment, clear=True):
                output = StringIO()
                status = main(
                    ["records", "--platform", "pi", "--appdata", str(appdata), "--json"],
                    stdout=output,
                    stderr=StringIO(),
                    stdin=StringIO(),
                )
                payload = json.loads(output.getvalue())
                self.assertEqual(status, EXIT_OK)
                self.assertEqual(payload["pi_count"], 1)
                record = payload["pi_sessions"][0]
                self.assertEqual(record["storage_kind"], "cindy")
                self.assertEqual(
                    record["reference_classification"], "live_current_reference"
                )
                self.assertTrue(record["active"])
                self.assertFalse(record["deletable"])

    def test_invalid_active_marker_survives_environment_cindy_root_deduplication(self) -> None:
        home = self.root / "invalid-active-home"
        appdata = self.root / "InvalidActiveAppData"
        cindy_root = appdata / "Cindy"
        agent_home = cindy_root / "pi-agent-home"
        sessions = agent_home / "sessions"
        write_pi_session(sessions, "candidate", "candidate secret")
        create_cindy_database(
            cindy_root / "cindy-local-v1.db",
            [("maker", "candidate", "deleted", "pi")],
        )

        with patch("pathlib.Path.home", return_value=home), patch.dict(
            os.environ,
            {
                "PI_CODING_AGENT_DIR": str(agent_home),
                "PI_SESSION_FILE": "relative-active.jsonl",
            },
            clear=True,
        ):
            output = StringIO()
            status = main(
                ["records", "--platform", "pi", "--appdata", str(appdata), "--json"],
                stdout=output,
                stderr=StringIO(),
                stdin=StringIO(),
            )
        payload = json.loads(output.getvalue())

        self.assertEqual(status, EXIT_ERROR)
        self.assertEqual(payload["pi_count"], 1)
        self.assertFalse(payload["pi_sessions"][0]["deletable"])
        self.assertTrue(
            any(
                failure["source"] == "pi-active-session"
                for failure in payload["pi_failures"]
            )
        )

    def test_cindy_pi_parent_alias_is_physically_qualified_and_uses_real_root(self) -> None:
        appdata = self.root / "AliasPiAppData"
        home = self.root / "alias-home"
        cindy_root = appdata / "CindyGlobal"
        real_agent = cindy_root / "pi-agent-home"
        real_sessions = real_agent / "sessions"
        real_file = write_pi_session(
            real_sessions, "header-not-sdk-id", "alias secret"
        )
        alias_root = self.root / "cindy-profile-alias"
        try:
            alias_root.symlink_to(cindy_root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        alias_agent = alias_root / "pi-agent-home"
        alias_sessions = alias_agent / "sessions"
        alias_file = alias_sessions / real_file.relative_to(real_sessions)
        create_cindy_database(
            cindy_root / "cindy-local-v1.db",
            [("maker", str(alias_file), "active", "pi")],
        )

        explicit_choices = (
            ["--pi-agent-dir", str(alias_agent)],
            ["--pi-session-dir", str(alias_sessions)],
        )
        for explicit in explicit_choices:
            with self.subTest(explicit=explicit):
                output = StringIO()
                status = main(
                    [
                        "records", "--platform", "pi", "--appdata", str(appdata),
                        *explicit, "--json",
                    ],
                    stdout=output,
                    stderr=StringIO(),
                    stdin=StringIO(),
                )
                payload = json.loads(output.getvalue())
                self.assertEqual(status, EXIT_OK)
                self.assertEqual(payload["pi_count"], 1)
                record = payload["pi_sessions"][0]
                self.assertEqual(record["storage_kind"], "cindy")
                self.assertEqual(
                    record["reference_classification"], "live_current_reference"
                )
                self.assertFalse(record["deletable"])
                self.assertEqual(
                    os.path.normcase(record["file"]["path"]),
                    os.path.normcase(str(real_file)),
                )

        environments = (
            {"PI_CODING_AGENT_DIR": str(alias_agent)},
            {"PI_CODING_AGENT_SESSION_DIR": str(alias_sessions)},
        )
        for environment in environments:
            with self.subTest(environment=environment), patch(
                "pathlib.Path.home", return_value=home
            ), patch.dict(os.environ, environment, clear=True):
                output = StringIO()
                status = main(
                    ["records", "--platform", "pi", "--appdata", str(appdata), "--json"],
                    stdout=output,
                    stderr=StringIO(),
                    stdin=StringIO(),
                )
                payload = json.loads(output.getvalue())
                self.assertEqual(status, EXIT_OK)
                self.assertEqual(payload["pi_count"], 1)
                record = payload["pi_sessions"][0]
                self.assertEqual(record["storage_kind"], "cindy")
                self.assertFalse(record["deletable"])

    def test_scan_clean_claude_are_explicit_errors_but_all_keeps_codex_behavior(self) -> None:
        for command in ("scan", "clean"):
            output = StringIO()
            status = main(
                [command, "--platform", "claude", "--json"],
                adapters=(), stdout=output, stderr=StringIO(), stdin=StringIO(),
            )
            self.assertEqual(status, EXIT_ERROR)
            self.assertIn("error", json.loads(output.getvalue()))
        output = StringIO()
        status = main(
            ["scan", "--platform", "all", "--json"],
            adapters=(), stdout=output, stderr=StringIO(), stdin=StringIO(),
        )
        self.assertEqual(status, EXIT_OK)

    def test_injected_empty_adapters_never_trigger_native_engine_builders(self) -> None:
        with patch(
            "codex_session_janitor.pi_sessions.build_pi_session_inventory",
            side_effect=AssertionError("Pi home touched"),
        ), patch(
            "codex_session_janitor.claude_sessions.resolve_claude_paths",
            side_effect=AssertionError("Claude home touched"),
        ):
            output = StringIO()
            status = main(
                ["records", "--json"], adapters=(), stdout=output,
                stderr=StringIO(), stdin=StringIO(),
            )
        self.assertEqual(status, EXIT_OK)
        payload = json.loads(output.getvalue())
        self.assertNotIn("pi_sessions", payload)
        self.assertNotIn("claude_sessions", payload)

        for platform in ("pi", "claude"):
            with self.subTest(platform=platform):
                exact_output = StringIO()
                exact_status = main(
                    ["records", "--platform", platform, "--json"],
                    adapters=(), stdout=exact_output, stderr=StringIO(), stdin=StringIO(),
                )
                exact_payload = json.loads(exact_output.getvalue())
                self.assertEqual(exact_status, EXIT_ERROR)
                self.assertIn("catalog builder", exact_payload["error"]["message"])

    def test_claude_human_records_explain_live_and_frontend_only_non_deletable_states(self) -> None:
        missing = "33333333-3333-4333-8333-333333333333"
        references = (
            {
                "backend": "claude", "native_session_id": ONE,
                "session_status": "active", "reference_kind": "current",
                "claude_config_dir": str(self.config),
            },
            {
                "backend": "claude", "native_session_id": missing,
                "session_status": "active", "reference_kind": "current",
                "claude_config_dir": str(self.config),
            },
        )
        catalog = build_claude_session_catalog(
            config_dir=self.config, frontend_references=references
        )
        output = StringIO()
        status = main(
            ["records", "--platform", "claude"], adapters=(),
            stdout=output, stderr=StringIO(), stdin=StringIO(),
            claude_catalog_builder=lambda **_kwargs: catalog,
        )
        rendered = output.getvalue()
        self.assertEqual(status, EXIT_OK)
        self.assertIn("Cindy 使用情况：仍被活跃 Cindy 会话当前使用", rendered)
        self.assertIn("删除资格：当前不能选择删除", rendered)
        self.assertIn("Cindy 使用情况：Cindy 仍有引用，但本地会话文件已不存在", rendered)
        self.assertIn("删除资格：没有文件可删", rendered)
        self.assertNotIn("live_current_reference", rendered)
        self.assertNotIn("frontend_only", rendered)
        self.assertNotIn("classification", rendered)
        self.assertNotIn("storage", rendered)
        self.assertNotIn("descendant", rendered)
        self.assertNotIn("后代", rendered)
        self.assertNotIn("无后代", rendered)
        self.assertNotIn("删除操作 ID", rendered)

    def test_root_scoped_failure_does_not_pollute_other_claude_config(self) -> None:
        other = self.root / "other-claude"
        write_claude_session(other, TWO, "other body")
        catalog = build_claude_multi_root_catalog(
            (self.config, other),
            reference_errors=(
                {
                    "config_dir": str(other),
                    "message": "dev Cindy database is unreadable",
                },
            ),
        )
        by_root = {item.config_dir: item for item in catalog.catalogs}
        self.assertEqual(by_root[self.config].records[0].classification, "unreferenced")
        self.assertTrue(by_root[self.config].records[0].deletable)
        self.assertEqual(by_root[other].records[0].classification, "inventory_incomplete")
        self.assertFalse(by_root[other].records[0].deletable)
        plan = build_claude_delete_plan(catalog)
        by_action_root = {action.config_dir: action for action in plan.actions}
        self.assertTrue(by_action_root[self.config].available)
        self.assertFalse(by_action_root[other].available)
        self.assertFalse(by_action_root[self.config].catalog_blocking_failures)
        self.assertTrue(by_action_root[other].catalog_blocking_failures)

    def test_unqualified_reference_blocks_same_uuid_and_cross_root_selector_is_ambiguous(self) -> None:
        other = self.root / "other-claude"
        other_transcript = write_claude_session(other, ONE, "other same uuid")
        ambiguous_reference = {
            "backend": "claude",
            "native_session_id": ONE,
            "session_status": "active",
            "reference_kind": "current",
        }
        blocked = build_claude_multi_root_catalog(
            (self.config, other), frontend_references=(ambiguous_reference,)
        )
        self.assertTrue(all(not record.deletable for record in blocked.records))
        self.assertTrue(all(record.classification == "inventory_incomplete" for record in blocked.records))

        safe = build_claude_multi_root_catalog((self.config, other))
        builder = lambda **_kwargs: safe
        ambiguous_output = StringIO()
        status = main(
            ["delete", "--platform", "claude", "--session-id", ONE, "--json"],
            stdout=ambiguous_output, stderr=StringIO(), stdin=StringIO(),
            claude_catalog_builder=builder,
        )
        self.assertEqual(status, EXIT_ERROR)
        self.assertIn("ambiguous", json.loads(ambiguous_output.getvalue())["error"]["message"])

        selected = next(record for record in safe.records if record.config_dir == self.config)
        preview_output = StringIO()
        status = main(
            ["delete", "--platform", "claude", "--action-id", selected.action_id, "--json"],
            stdout=preview_output, stderr=StringIO(), stdin=StringIO(),
            claude_catalog_builder=builder,
        )
        self.assertEqual(status, EXIT_CONFIRMATION_REQUIRED)
        self.assertTrue(self.transcript.exists())
        self.assertTrue(other_transcript.exists())


if __name__ == "__main__":
    unittest.main()
