from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from codex_session_janitor.adapters import AionUIAdapter, CindyAdapter, NativeIntegrityAdapter
from codex_session_janitor.discovery import (
    discover_aionui_databases,
    discover_cindy_profiles,
)
from codex_session_janitor.inventory import (
    InventorySelectionError,
    build_session_catalog,
    select_managed_conversations,
)

from tests.support import (
    create_cindy_database,
    create_thread_index,
    write_rollout,
)


class SessionCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    @staticmethod
    def _cindy_row(
        session_id: str,
        thread_id: str | None,
        *,
        status: str = "active",
    ) -> dict[str, object]:
        return {
            "id": session_id,
            "sdk_session_id": thread_id,
            "status": status,
            "source": "desktop",
            "created_at": 1,
            "updated_at": 2,
            "parent_session_id": None,
            "agent_kind": "codex",
        }

    def test_union_lists_normal_orphan_legacy_frontend_only_and_unmapped(self) -> None:
        home = self.root / "codex-home"
        home.mkdir()
        normal_rollout = write_rollout(home, "normal", originator="cindy")
        write_rollout(home, "rollout-only", originator="codex_cli_rs")
        create_thread_index(
            home,
            [
                {"id": "normal", "rollout_path": str(normal_rollout)},
                {"id": "index-only", "rollout_path": "missing.jsonl"},
            ],
        )
        (home / "session_index.jsonl").write_text(
            json.dumps({"id": "legacy-only", "thread_name": "Legacy"}) + "\n",
            encoding="utf-8",
        )
        database = self.root / "cindy.db"
        create_cindy_database(
            database,
            [
                self._cindy_row("mapped", "normal"),
                self._cindy_row("frontend-only", "gone-thread", status="deleted"),
                self._cindy_row("unassigned", None),
            ],
        )
        adapters = [
            NativeIntegrityAdapter(codex_home=home),
            CindyAdapter(database=database, codex_home=home),
        ]

        catalog = build_session_catalog(adapters)
        by_id = {record.thread_id: record for record in catalog.records}

        self.assertEqual(
            set(by_id),
            {"normal", "index-only", "rollout-only", "legacy-only", "gone-thread"},
        )
        self.assertEqual(len(by_id["normal"].frontend_sessions), 1)
        self.assertTrue(by_id["normal"].deletable)
        self.assertTrue(by_id["rollout-only"].deletable)
        self.assertTrue(by_id["index-only"].deletable)
        self.assertFalse(by_id["legacy-only"].artifact_present)
        self.assertFalse(by_id["legacy-only"].deletable)
        self.assertFalse(by_id["gone-thread"].deletable)
        self.assertEqual(
            [record.platform_session_id for record in catalog.unmapped_frontend_sessions],
            ["unassigned"],
        )
        self.assertEqual(catalog.errors, ())

    def test_same_thread_id_in_different_homes_is_not_merged(self) -> None:
        adapters = []
        for name in ("one", "two"):
            home = self.root / name / "codex-home"
            home.mkdir(parents=True)
            rollout = write_rollout(home, "same-id", originator="cindy")
            create_thread_index(home, [{"id": "same-id", "rollout_path": str(rollout)}])
            database = self.root / name / "cindy.db"
            create_cindy_database(database, [self._cindy_row(name, "same-id")])
            adapters.append(CindyAdapter(database=database, codex_home=home))

        catalog = build_session_catalog(adapters)

        self.assertEqual(len(catalog.records), 2)
        self.assertEqual(len({record.action_id for record in catalog.records}), 2)
        with self.assertRaises(InventorySelectionError):
            select_managed_conversations(catalog, ["same-id"])
        self.assertEqual(
            select_managed_conversations(catalog, [catalog.records[0].action_id]),
            (catalog.records[0],),
        )

    def test_malformed_rollout_and_missing_cascade_schema_fail_closed(self) -> None:
        home = self.root / "codex-home"
        home.mkdir()
        rollout = write_rollout(home, "healthy", originator="codex_cli_rs")
        create_thread_index(
            home,
            [{"id": "healthy", "rollout_path": str(rollout)}],
            include_spawn_edges_table=False,
        )
        malformed = home / "sessions" / "bad.jsonl"
        malformed.write_text("not-json\n", encoding="utf-8")

        catalog = build_session_catalog([NativeIntegrityAdapter(codex_home=home)])
        record = next(record for record in catalog.records if record.thread_id == "healthy")

        self.assertFalse(record.deletable)
        self.assertTrue(record.cascade_unknown)
        self.assertEqual(
            {error.source for error in catalog.errors},
            {"codex-cascade", "codex-rollouts"},
        )
        self.assertTrue(all(error.blocks_delete for error in catalog.errors))

    def test_malformed_legacy_line_keeps_valid_preview_but_blocks_home(self) -> None:
        home = self.root / "codex-home"
        home.mkdir()
        rollout = write_rollout(home, "healthy", originator="codex_cli_rs")
        create_thread_index(home, [{"id": "healthy", "rollout_path": str(rollout)}])
        (home / "session_index.jsonl").write_text(
            json.dumps({"id": "legacy", "thread_name": "Old"}) + "\n{" + "\n",
            encoding="utf-8",
        )

        catalog = build_session_catalog([NativeIntegrityAdapter(codex_home=home)])

        self.assertIn("legacy", {record.thread_id for record in catalog.records})
        self.assertTrue(any(error.source == "legacy-index" for error in catalog.errors))
        self.assertTrue(all(not record.deletable for record in catalog.records))

    def test_frontend_schema_failure_blocks_otherwise_healthy_shared_home(self) -> None:
        home = self.root / "codex-home"
        home.mkdir()
        rollout = write_rollout(home, "healthy", originator="codex_cli_rs")
        create_thread_index(home, [{"id": "healthy", "rollout_path": str(rollout)}])
        corrupt_frontend = self.root / "cindy.db"
        corrupt_frontend.write_text("not sqlite", encoding="utf-8")

        catalog = build_session_catalog(
            [
                NativeIntegrityAdapter(codex_home=home),
                CindyAdapter(database=corrupt_frontend, codex_home=home),
            ]
        )

        self.assertTrue(any(error.source == "frontend:cindy" for error in catalog.errors))
        self.assertFalse(catalog.records[0].deletable)
        self.assertTrue(
            any("frontend:cindy" in blocker for blocker in catalog.records[0].blockers)
        )

    def test_invalid_state_path_is_not_silently_treated_as_absent(self) -> None:
        home = self.root / "codex-home"
        home.mkdir()
        write_rollout(home, "healthy", originator="codex_cli_rs")
        (home / "state_5.sqlite").mkdir()

        catalog = build_session_catalog([NativeIntegrityAdapter(codex_home=home)])

        self.assertTrue(any(error.source == "codex-state" for error in catalog.errors))
        self.assertFalse(catalog.records[0].deletable)
        self.assertTrue(catalog.records[0].cascade_unknown)

    def test_rollout_walk_error_blocks_the_home(self) -> None:
        home = self.root / "codex-home"
        home.mkdir()
        rollout = write_rollout(home, "healthy", originator="codex_cli_rs")
        create_thread_index(home, [{"id": "healthy", "rollout_path": str(rollout)}])

        def failed_walk(root: Path, *, onerror, followlinks: bool):
            del followlinks
            onerror(PermissionError(13, "denied", str(Path(root) / "private")))
            return iter(())

        with patch("codex_session_janitor.inventory.os.walk", side_effect=failed_walk):
            catalog = build_session_catalog([NativeIntegrityAdapter(codex_home=home)])

        self.assertTrue(any(error.source == "codex-rollouts" for error in catalog.errors))
        self.assertFalse(catalog.records[0].deletable)


class CurrentAionSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "aionui.db"
        self.home = self.root / "codex-home"
        self.home.mkdir()

    def _create_current_database(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT);
                CREATE TABLE acp_session (
                    conversation_id TEXT PRIMARY KEY,
                    agent_backend TEXT NOT NULL,
                    agent_source TEXT,
                    agent_id TEXT,
                    session_id TEXT,
                    session_status TEXT,
                    last_active_at INTEGER
                );
                CREATE TABLE agent_metadata (id TEXT PRIMARY KEY, backend TEXT);
                INSERT INTO agent_metadata (id, backend) VALUES ('codex-agent', 'codex');
                INSERT INTO acp_session (
                    conversation_id, agent_backend, agent_source, agent_id,
                    session_id, session_status, last_active_at
                ) VALUES ('orphan', 'codex', 'builtin', 'codex-agent',
                          'codex-thread', 'idle', 123);
                """
            )
            connection.commit()

    def test_list_and_existing_scan_support_direct_backend_and_metadata_id(self) -> None:
        self._create_current_database()
        rollout = write_rollout(self.home, "codex-thread", originator="aionui-session")
        create_thread_index(
            self.home,
            [{"id": "codex-thread", "rollout_path": str(rollout)}],
        )
        adapter = AionUIAdapter(database=self.database, codex_home=self.home)

        sessions = adapter.list_sessions()
        findings = adapter.scan()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].backend, "codex")
        self.assertFalse(sessions[0].is_live)
        self.assertEqual([finding.thread_id for finding in findings], ["codex-thread"])
        self.assertTrue(findings[0].details["cleanable"])


class LegacyCindyOwnershipTests(unittest.TestCase):
    def test_xdt_maker_originator_is_accepted_for_legacy_cindy_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "codex-home"
            home.mkdir()
            database = root / "xdt-maker-user.db"
            create_cindy_database(
                database,
                [SessionCatalogTests._cindy_row("deleted", "legacy", status="deleted")],
            )
            rollout = write_rollout(home, "legacy", originator="xdt-maker")
            create_thread_index(home, [{"id": "legacy", "rollout_path": str(rollout)}])

            finding = CindyAdapter(database=database, codex_home=home).scan()[0]

            self.assertEqual(finding.details["ownership_status"], "confirmed")
            self.assertTrue(finding.details["cleanable"])


class FrontendDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.appdata = Path(self.temporary_directory.name)

    def test_discovers_current_and_old_aion_databases_in_priority_order(self) -> None:
        root = self.appdata / "AionUi" / "aionui"
        root.mkdir(parents=True)
        current = root / "aionui.db"
        legacy = root / "aionui-backend.db"
        current.touch()
        legacy.touch()

        self.assertEqual(discover_aionui_databases(self.appdata), (current, legacy))

    def test_invalid_known_database_path_is_not_treated_as_not_installed(self) -> None:
        invalid = self.appdata / "AionUi" / "aionui" / "aionui.db"
        invalid.mkdir(parents=True)

        with self.assertRaises(RuntimeError):
            discover_aionui_databases(self.appdata)

    def test_discovers_cindy_profiles_and_excludes_backup_files(self) -> None:
        global_root = self.appdata / "CindyGlobal"
        cn_root = self.appdata / "Cindy"
        global_root.mkdir()
        cn_root.mkdir()
        current = global_root / "cindy-user.db"
        old = cn_root / "cindy-local-v1.db"
        current.touch()
        old.touch()
        (global_root / "cindy-user-backup.db").touch()

        profiles = discover_cindy_profiles(self.appdata)

        self.assertEqual([profile.database for profile in profiles], [current, old])
        self.assertEqual(profiles[0].codex_home, global_root / "codex-home")

    def test_discovers_surviving_cindy_codex_home_without_frontend_database(
        self,
    ) -> None:
        root = self.appdata / "CindyDev"
        codex_home = root / "codex-home"
        codex_home.mkdir(parents=True)

        profiles = discover_cindy_profiles(self.appdata)

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].root, root)
        self.assertEqual(profiles[0].codex_home, codex_home)
        self.assertEqual(profiles[0].database, root / "cindy-local-v1.db")


if __name__ == "__main__":
    unittest.main()
