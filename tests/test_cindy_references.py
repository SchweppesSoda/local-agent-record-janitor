from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.adapters import CindyAdapter, NativeIntegrityAdapter
from local_agent_record_janitor.cindy_references import build_cindy_reference_catalog
from local_agent_record_janitor.inventory import build_session_catalog
from local_agent_record_janitor.discovery import CindyProfile
from local_agent_record_janitor.pi_sessions import PiInventoryError, build_pi_session_inventory
from local_agent_record_janitor.pi_delete import build_pi_delete_plan

from tests.support import create_thread_index, write_rollout


def create_database(
    path: Path,
    sessions: list[tuple[str, str | None, str, str]],
    switches: list[tuple[str, str, object, int, int | None]] = [],
) -> None:
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
            VALUES (?, ?, ?, ?, '/project', 200)
            """,
            sessions,
        )
        connection.executemany(
            """
            INSERT INTO messages
                (id, session_id, role, content, created_at, rewind_at)
            VALUES (?, ?, 'agent_switch', ?, ?, ?)
            """,
            (
                (
                    boundary_id,
                    session_id,
                    json.dumps(content) if not isinstance(content, str) else content,
                    created_at,
                    rewind_at,
                )
                for boundary_id, session_id, content, created_at, rewind_at in switches
            ),
        )
        connection.commit()


class CindyReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.database = self.root / "cindy.db"

    def test_current_and_all_parked_references_are_preserved(self) -> None:
        create_database(
            self.database,
            [("maker", "pi-current", "active", "pi")],
            [
                (
                    "switch-1",
                    "maker",
                    {"fromAgentKind": "codex", "fromSdkSessionId": "codex-old"},
                    10,
                    None,
                ),
                (
                    "switch-2",
                    "maker",
                    {"fromAgentKind": "cc", "fromSdkSessionId": "claude-old"},
                    20,
                    30,
                ),
                (
                    "switch-3",
                    "maker",
                    {"fromAgentKind": "codex", "fromSdkSessionId": "codex-older"},
                    40,
                    None,
                ),
            ],
        )

        catalog = build_cindy_reference_catalog(self.database)

        self.assertEqual(catalog.failures, ())
        self.assertEqual(
            [(ref.backend, ref.native_session_id, ref.reference_kind) for ref in catalog.references],
            [
                ("pi", "pi-current", "current"),
                ("codex", "codex-old", "agent_switch"),
                ("claude", "claude-old", "agent_switch"),
                ("codex", "codex-older", "agent_switch"),
            ],
        )
        claude = catalog.for_backend("claude")[0]
        self.assertEqual(claude.boundary_id, "switch-2")
        self.assertEqual(claude.boundary_created_at_ms, 20)
        self.assertEqual(claude.boundary_rewind_at_ms, 30)
        self.assertTrue(claude.is_live)

    def test_legacy_database_without_messages_or_optional_columns_is_current_only(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    sdk_session_id TEXT,
                    status TEXT,
                    agent_kind TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO sessions VALUES ('legacy', 'thread', 'archived', 'codex')"
            )
            connection.commit()

        catalog = build_cindy_reference_catalog(self.database)

        self.assertEqual(catalog.failures, ())
        self.assertEqual(catalog.references[0].native_session_id, "thread")
        self.assertIsNone(catalog.references[0].working_dir)

    def test_malformed_switch_json_and_incompatible_message_schema_fail_closed(self) -> None:
        create_database(
            self.database,
            [("maker", "pi", "active", "pi")],
            [("bad", "maker", "{not json", 10, None)],
        )
        malformed = build_cindy_reference_catalog(self.database)
        self.assertEqual(malformed.references, ())
        self.assertTrue(malformed.failures[0].blocks_delete)
        self.assertNotIn("not json", malformed.failures[0].message)

        self.database.unlink()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "CREATE TABLE sessions (id TEXT, sdk_session_id TEXT, status TEXT, agent_kind TEXT)"
            )
            connection.execute("CREATE TABLE messages (id TEXT, role TEXT, content TEXT)")
            connection.commit()
        incompatible = build_cindy_reference_catalog(self.database)
        self.assertEqual(incompatible.references, ())
        self.assertTrue(incompatible.failures)


class CindyCodexProtectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.database = self.root / "cindy.db"
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()

    def adapter(self) -> CindyAdapter:
        return CindyAdapter(
            database=self.database,
            codex_home=self.codex_home,
            cindy_root=self.root,
            codex_bin_hint=self.root / "codex",
        )

    def test_live_parked_reference_is_visible_and_blocks_physical_delete(self) -> None:
        create_database(
            self.database,
            [("maker", "pi-current", "active", "pi")],
            [
                (
                    "switch",
                    "maker",
                    {"fromAgentKind": "codex", "fromSdkSessionId": "parked-thread"},
                    10,
                    None,
                )
            ],
        )
        rollout = write_rollout(self.codex_home, "parked-thread", originator="cindy")
        create_thread_index(
            self.codex_home,
            [{"id": "parked-thread", "rollout_path": str(rollout)}],
        )

        adapter = self.adapter()
        sessions = adapter.list_sessions()
        catalog = build_session_catalog(
            [NativeIntegrityAdapter(codex_home=self.codex_home), adapter]
        )
        record = next(record for record in catalog.records if record.thread_id == "parked-thread")

        self.assertEqual(sessions[0].details["reference_kind"], "agent_switch")
        self.assertTrue(sessions[0].is_live)
        adapter.scan()
        self.assertIn("parked-thread", adapter.live_thread_ids)
        self.assertFalse(record.deletable)
        self.assertTrue(any("historical" in blocker for blocker in record.blockers))

    def test_deleted_parked_reference_is_a_scan_candidate(self) -> None:
        create_database(
            self.database,
            [("maker", "pi-current", "deleted", "pi")],
            [
                (
                    "switch",
                    "maker",
                    {"fromAgentKind": "codex", "fromSdkSessionId": "parked-thread"},
                    10,
                    None,
                )
            ],
        )
        rollout = write_rollout(self.codex_home, "parked-thread", originator="cindy")
        create_thread_index(
            self.codex_home,
            [{"id": "parked-thread", "rollout_path": str(rollout)}],
        )

        finding = self.adapter().scan()[0]

        self.assertEqual(finding.thread_id, "parked-thread")
        self.assertEqual(finding.details["reference_kind"], "agent_switch")
        self.assertTrue(finding.details["cleanable"])


class PiCindyInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.standalone_agent = self.root / "standalone-agent"
        self.standalone_sessions = self.standalone_agent / "sessions"
        self.cindy_root = self.root / "Cindy"
        self.cindy_database = self.cindy_root / "cindy-user.db"

    def write_pi(self, root: Path, name: str, session_id: str) -> Path:
        path = root / "--project--" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "type": "session",
                    "version": 3,
                    "id": session_id,
                    "timestamp": "2026-08-03T00:00:00Z",
                    "cwd": str(self.project),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def profile(self) -> CindyProfile:
        return CindyProfile(
            root=self.cindy_root,
            database=self.cindy_database,
            codex_home=self.cindy_root / "codex-home",
        )

    def inventory(self):
        return build_pi_session_inventory(
            standalone_options={
                "environ": {},
                "cwd": self.project,
                "home": self.root,
                "agent_dir": self.standalone_agent,
                "session_root": self.standalone_sessions,
            },
            cindy_profiles=[self.profile()],
        )

    def test_standalone_and_cindy_roots_remain_distinct_and_live_current_blocks(self) -> None:
        self.write_pi(self.standalone_sessions, "standalone.jsonl", "standalone")
        cindy_sessions = self.cindy_root / "pi-agent-home" / "sessions"
        self.write_pi(cindy_sessions, "current.jsonl", "pi-live")
        self.cindy_root.mkdir(exist_ok=True)
        create_database(
            self.cindy_database,
            [("maker", "pi-live", "active", "pi")],
        )

        inventory = self.inventory()
        by_id = {record.session_id: record for record in inventory.records}

        self.assertEqual(len(inventory.catalogs), 2)
        self.assertEqual(by_id["standalone"].storage_kind, "standalone")
        self.assertTrue(by_id["standalone"].deletable)
        self.assertEqual(by_id["pi-live"].storage_kind, "cindy")
        self.assertEqual(by_id["pi-live"].reference_classification, "live_current_reference")
        self.assertFalse(by_id["pi-live"].deletable)

    def test_live_historical_blocks_but_deleted_reference_is_eligible(self) -> None:
        cindy_sessions = self.cindy_root / "pi-agent-home" / "sessions"
        self.write_pi(cindy_sessions, "parked.jsonl", "pi-parked")
        self.write_pi(cindy_sessions, "deleted.jsonl", "pi-deleted")
        self.cindy_root.mkdir(exist_ok=True)
        create_database(
            self.cindy_database,
            [
                ("live-maker", "codex-current", "active", "codex"),
                ("deleted-maker", "pi-deleted", "deleted", "pi"),
            ],
            [
                (
                    "switch",
                    "live-maker",
                    {"fromAgentKind": "pi", "fromSdkSessionId": "pi-parked"},
                    10,
                    None,
                )
            ],
        )

        by_id = {record.session_id: record for record in self.inventory().records}

        self.assertEqual(by_id["pi-parked"].reference_classification, "live_historical_reference")
        self.assertFalse(by_id["pi-parked"].deletable)
        self.assertEqual(by_id["pi-deleted"].reference_classification, "deleted_frontend_reference")
        self.assertTrue(by_id["pi-deleted"].deletable)

    def test_live_and_deleted_missing_native_files_are_frontend_only(self) -> None:
        self.cindy_root.mkdir()
        create_database(
            self.cindy_database,
            [
                ("live", "missing-live", "active", "pi"),
                ("deleted", "missing-deleted", "deleted", "pi"),
            ],
        )

        inventory = self.inventory()

        self.assertEqual(
            {
                (reference.native_session_id, reference.is_live)
                for reference in inventory.frontend_only_references
            },
            {("missing-live", True), ("missing-deleted", False)},
        )

    def test_incompatible_existing_cindy_schema_blocks_the_profile_root(self) -> None:
        self.write_pi(self.standalone_sessions, "standalone.jsonl", "standalone")
        cindy_sessions = self.cindy_root / "pi-agent-home" / "sessions"
        self.write_pi(cindy_sessions, "candidate.jsonl", "candidate")
        with closing(sqlite3.connect(self.cindy_database)) as connection:
            connection.execute("CREATE TABLE sessions (id TEXT)")
            connection.commit()

        inventory = self.inventory()
        candidate = next(record for record in inventory.records if record.session_id == "candidate")
        standalone = next(record for record in inventory.records if record.session_id == "standalone")
        plan = build_pi_delete_plan(inventory)
        by_action = {action.action_id: action for action in plan.actions}

        self.assertFalse(candidate.deletable)
        self.assertTrue(standalone.deletable)
        self.assertTrue(any(error.source == "cindy-reference" for error in inventory.errors))
        self.assertFalse(by_action[candidate.action_id].available)
        self.assertTrue(by_action[standalone.action_id].available)

    def test_unreadable_cindy_native_path_identity_is_inventory_incomplete(self) -> None:
        cindy_sessions = self.cindy_root / "pi-agent-home" / "sessions"
        path = self.write_pi(cindy_sessions, "candidate.jsonl", "header-id")
        self.cindy_root.mkdir(exist_ok=True)
        create_database(
            self.cindy_database,
            [("maker", str(path), "active", "pi")],
        )

        with patch(
            "local_agent_record_janitor.pi_sessions._physical_file_identity",
            side_effect=PiInventoryError("denied"),
        ):
            inventory = self.inventory()
        candidate = next(record for record in inventory.records if record.session_id == "header-id")

        self.assertEqual(candidate.reference_classification, "inventory_incomplete")
        self.assertFalse(candidate.deletable)
        self.assertTrue(
            any(error.source == "cindy-reference-path" for error in inventory.errors)
        )


if __name__ == "__main__":
    unittest.main()
