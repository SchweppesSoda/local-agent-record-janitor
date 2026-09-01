from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from local_agent_record_janitor.adapters import AionUIAdapter, CindyAdapter
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.client_inventory import (
    ClientInventory,
    ClientTarget,
    build_client_engine_contexts,
    build_client_inventory,
)
from local_agent_record_janitor.inventory import FrontendSessionRecord
from local_agent_record_janitor.record_identity import (
    EngineCapability,
    NATIVE_ROOT_UNVERIFIED,
    ProjectKey,
    ProjectSelectionError,
    RecordClassification,
    RecordKey,
    StoreKey,
    capability_for,
    resolve_project_selector,
)


@dataclass
class _FrontendAdapter:
    name: str
    codex_home: Path
    database: Path
    rows: tuple[FrontendSessionRecord, ...]

    @property
    def client(self) -> str:
        return self.name

    @property
    def engine(self) -> str:
        return "codex"

    def list_sessions(self) -> list[FrontendSessionRecord]:
        return list(self.rows)


def _write_pi_session(root: Path, session_id: str) -> Path:
    path = root / "sessions" / "--project--" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "session",
                "id": session_id,
                "version": 3,
                "cwd": str(root / "project"),
            }
        )
        + "\n"
        + json.dumps({"type": "message", "message": "metadata"}),
        encoding="utf-8",
    )
    return path


def _write_claude_session(root: Path, session_id: str) -> Path:
    path = root / "projects" / "project" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sessionId": session_id, "type": "user", "message": "metadata"})
        + "\n",
        encoding="utf-8",
    )
    return path


def _create_cindy_database(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                sdk_session_id TEXT,
                status TEXT,
                source TEXT,
                created_at INTEGER,
                updated_at INTEGER,
                parent_session_id TEXT,
                agent_kind TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO sessions
                (id, sdk_session_id, status, source, created_at, updated_at,
                 parent_session_id, agent_kind)
            VALUES (?, ?, ?, 'test', 1, 2, NULL, ?)
            """,
            rows,
        )
        connection.commit()


def _create_aionui_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations (id TEXT PRIMARY KEY);
            CREATE TABLE acp_session (
                conversation_id TEXT NOT NULL,
                session_id TEXT,
                agent_id TEXT,
                agent_source TEXT,
                session_status TEXT,
                last_active_at INTEGER
            );
            CREATE TABLE agent_metadata (
                agent_id TEXT PRIMARY KEY,
                backend TEXT
            );
            """
        )
        connection.execute("INSERT INTO conversations (id) VALUES ('aion-pi')")
        connection.execute(
            """
            INSERT INTO acp_session
                (conversation_id, session_id, agent_id, agent_source,
                 session_status, last_active_at)
            VALUES ('aion-pi', 'aion-pi-native', 'pi-agent', 'test', 'deleted', 1)
            """
        )
        connection.execute(
            "INSERT INTO agent_metadata (agent_id, backend) VALUES ('pi-agent', 'pi')"
        )
        connection.commit()


class ClientInventoryTests(unittest.TestCase):
    def test_same_name_project_paths_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "one" / "repo"
            second = root / "two" / "repo"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            projects = (
                ProjectKey.from_path("native", first, display_name="repo"),
                ProjectKey.from_path("native", second, display_name="repo"),
            )
            with self.assertRaises(ProjectSelectionError):
                resolve_project_selector(projects, "repo", client="native")

    def test_orphan_without_project_evidence_is_record_id_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            capability = capability_for("native", "codex")
            target = ClientTarget(
                client="native",
                engine="codex",
                record_key=RecordKey(StoreKey("codex", home), "orphan"),
                project_key=None,
                native_thread_id="orphan",
                frontend_reference_ids=(),
                classification=RecordClassification.ORPHAN_NATIVE,
                capability=capability,
                action_ids=("record:v1:orphan",),
            )
            inventory = ClientInventory(
                client="native",
                engines=("codex",),
                projects=(),
                records=(),
                frontend_sessions=(),
                unmapped_frontend_sessions=(),
                targets=(target,),
                capabilities={"codex": capability},
            )
            self.assertEqual(inventory.select(record_ids=("orphan",)).targets, (target,))
            with self.assertRaises(ValueError):
                inventory.select(all_projects=True)

    def test_duplicate_frontend_refs_produce_one_native_target_and_keep_both_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            thread = "same-native-thread"
            rows = tuple(
                FrontendSessionRecord(
                    platform="cindy",
                    platform_session_id=session_id,
                    thread_id=thread,
                    database=root / "cindy.db",
                    codex_home=home,
                    backend="codex",
                    status="deleted",
                    details={
                        "frontend_reference": {
                            "operation": "clear_session_sdk_session_id",
                            "exact": True,
                        }
                    },
                )
                for session_id in ("ref-one", "ref-two")
            )
            adapter = _FrontendAdapter("cindy", home, root / "cindy.db", rows)
            rollout = home / "sessions" / "thread.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": thread,
                            "cwd": str(root / "project"),
                            "originator": "cindy",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            inventory = build_client_inventory((adapter,), client="cindy")
            self.assertEqual(len(inventory.targets), 1)
            self.assertEqual(
                set(inventory.targets[0].frontend_reference_ids),
                {"cindy:ref-one", "cindy:ref-two"},
            )
            self.assertTrue(
                all(session.details["frontend_reference"] for session in inventory.frontend_sessions)
            )

    def test_cindy_all_backend_snapshot_and_pi_claude_native_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cindy_root = root / "cindy"
            database = cindy_root / "cindy.db"
            codex_home = cindy_root / "codex-home"
            codex_home.mkdir(parents=True)
            _create_cindy_database(
                database,
                [
                    ("cindy-pi", "pi-session", "deleted", "pi"),
                    (
                        "cindy-claude",
                        "11111111-1111-4111-8111-111111111111",
                        "deleted",
                        "cc",
                    ),
                ],
            )
            _write_pi_session(cindy_root / "pi-agent-home", "pi-session")
            _write_claude_session(
                cindy_root / "claude-home",
                "11111111-1111-4111-8111-111111111111",
            )
            adapter = CindyAdapter(
                database=database,
                codex_home=codex_home,
                cindy_root=cindy_root,
                codex_bin_hint=root / "codex",
            )
            first = adapter.snapshot_sessions()
            second = adapter.snapshot_sessions(all_backends=True)
            self.assertIs(first, second)
            self.assertEqual({row.backend for row in first.records}, {"pi", "claude"})
            contexts = build_client_engine_contexts((adapter,), client="cindy")
            by_engine = {context.engine: context for context in contexts}
            for engine, action_prefix, expected_root in (
                ("pi", "pi-session:v1:", cindy_root / "pi-agent-home" / "sessions"),
                ("claude", "claude-session:v1:", cindy_root / "claude-home"),
            ):
                context = by_engine[engine]
                self.assertEqual(len(context.native_records), 1)
                self.assertEqual(len(context.targets), 1)
                target = context.targets[0]
                self.assertTrue(any(item.startswith(action_prefix) for item in target.action_ids))
                self.assertEqual(target.record_key.store.path, expected_root.absolute())
                self.assertEqual(context.capability, target.capability)
                self.assertTrue(context.capability.native_delete)
                self.assertIn("frontend_reference", context.frontend_sessions[0].details)

    def test_aionui_pi_without_unique_root_is_structured_blocker_and_never_full(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "aionui.db"
            codex_home = root / "shared-codex"
            codex_home.mkdir()
            _create_aionui_database(database)
            adapter = AionUIAdapter(
                database=database,
                codex_home=codex_home,
                codex_bin_hint=root / "codex",
            )
            context = build_client_engine_contexts(
                (adapter,),
                client="aionui",
                engines=("pi",),
                writer_capabilities={
                    "pi": EngineCapability(
                        "aionui",
                        "pi",
                        native_delete=True,
                        frontend_session_delete=True,
                        frontend_reference_delete=True,
                        frontend_project_delete=True,
                    )
                },
            )[0]
            self.assertIn(NATIVE_ROOT_UNVERIFIED, context.capability.blocker_codes)
            self.assertNotEqual(context.capability.mode, "full")
            self.assertFalse(context.capability.native_delete)
            self.assertEqual(len(context.targets), 1)
            self.assertIn(NATIVE_ROOT_UNVERIFIED, context.targets[0].blocker_codes)
            self.assertNotIn("delete_pi_session", context.targets[0].action_ids)
            self.assertIn("frontend_reference", context.frontend_sessions[0].details)

    def test_aionui_unreferenced_project_item_is_exact_delete_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "aionui.db"
            codex_home = root / "shared-codex"
            codex_home.mkdir(parents=True)
            _create_aionui_database(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "INSERT INTO conversations (id) VALUES (?)",
                    ("project-only",),
                )
                connection.commit()
            adapter = AionUIAdapter(
                database=database,
                codex_home=codex_home,
                codex_bin_hint=root / "codex",
            )
            inventory = build_client_inventory((adapter,), client="aionui")
            targets = [
                target
                for target in inventory.targets
                if target.classification is RecordClassification.ORPHAN_PROJECT
            ]
            self.assertEqual(len(targets), 1)
            target = targets[0]
            self.assertIsNotNone(target.project_key)
            assert target.project_key is not None
            self.assertEqual(target.project_key.kind, "id")
            self.assertEqual(target.project_key.value, "project-only")
            self.assertEqual(len(target.action_ids), 1)
            self.assertTrue(target.action_ids[0].startswith("delete_project_item:"))
            self.assertTrue(target.capability.frontend_project_delete)
            self.assertNotIn("frontend_project_delete_unsupported", target.blocker_codes)
            self.assertEqual(target.project_row_evidence[0]["conversation_id"], "project-only")
            context_targets = tuple(
                target
                for context in build_client_engine_contexts(
                    (adapter,),
                    client="aionui",
                    inventory=inventory,
                )
                for target in context.targets
                if target.classification is RecordClassification.ORPHAN_PROJECT
            )
            self.assertEqual(len(context_targets), 1)
            self.assertEqual(context_targets[0].action_ids, target.action_ids)
            self.assertTrue(context_targets[0].capability.frontend_project_delete)
            self.assertEqual(len(inventory.to_dict()["project_items"]), 2)

    def test_aionui_project_item_without_supported_schema_is_inventory_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "aionui-unsupported.db"
            codex_home = root / "shared-codex"
            codex_home.mkdir(parents=True)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
                connection.execute(
                    "INSERT INTO conversations (id) VALUES (?)",
                    ("project-only",),
                )
                connection.commit()
            adapter = AionUIAdapter(
                database=database,
                codex_home=codex_home,
                codex_bin_hint=root / "codex",
            )
            inventory = build_client_inventory((adapter,), client="aionui")
            targets = [
                target
                for target in inventory.targets
                if target.classification is RecordClassification.ORPHAN_PROJECT
            ]
            self.assertEqual(len(targets), 1)
            target = targets[0]
            self.assertEqual(target.action_ids, ())
            self.assertFalse(target.capability.frontend_project_delete)
            self.assertIn("frontend_project_delete_unsupported", target.blocker_codes)
            self.assertEqual(target.project_row_evidence[0]["conversation_id"], "project-only")

    def test_records_client_pi_keeps_native_catalog_contract_without_frontend_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pi_root = root / "pi-agent"
            _write_pi_session(pi_root, "native-pi")
            output = StringIO()
            error = StringIO()
            result = main(
                [
                    "records",
                    "--client",
                    "pi",
                    "--pi-agent-dir",
                    str(pi_root),
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(payload["document_type"], "client_records")
            self.assertEqual(payload["client"], "pi")
            self.assertEqual(payload["count"], 1)
            self.assertEqual(
                set(payload["classifications"]),
                {
                    "healthy",
                    "orphan_native",
                    "orphan_frontend",
                    "orphan_project",
                    "broken_relation",
                    "stale_index",
                    "partial_remote",
                    "corrupt_unreadable",
                    "unknown_operation",
                },
            )


if __name__ == "__main__":
    unittest.main()
