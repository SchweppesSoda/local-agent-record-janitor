from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.adapters import AionUIAdapter, CindyAdapter, NativeIntegrityAdapter
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.claude_sessions import build_claude_session_catalog, resolve_claude_paths
from local_agent_record_janitor.pi_sessions import build_pi_session_catalog
from local_agent_record_janitor.client_inventory import (
    ClientInventory,
    ClientTarget,
    build_client_engine_contexts,
    build_client_inventory,
    build_native_client_inventory,
    collect_client_file_aliases,
)
from local_agent_record_janitor.inventory import FrontendSessionRecord, build_session_catalog
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
from tests.support import create_thread_index, write_rollout
from tests.test_cindy_references import create_database


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
    def _engine_profile(self, root, engine, session_id, *, status="active", frontend_id="same-ui-id"):
        home = root / "codex-home"
        home.mkdir(parents=True)
        database = root / "cindy.db"
        create_database(database, [(frontend_id, session_id, status, "cc" if engine == "claude" else engine)])
        if engine == "pi":
            _write_pi_session(root / "pi-agent-home", session_id)
        else:
            _write_claude_session(root / "claude-home", session_id)
        return CindyAdapter(database=database, codex_home=home, cindy_root=root)

    def test_all_profiles_and_exact_bindings_survive_engine_projection_and_selection(self):
        for engine in ("pi", "claude"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sid = "11111111-1111-4111-8111-111111111111"
                adapters = tuple(self._engine_profile(root / name, engine, sid) for name in ("one", "two"))
                context = build_client_engine_contexts(adapters, client="cindy", engines=(engine,))[0]
                self.assertEqual(len(context.native_records), 2)
                self.assertEqual(len(context.targets), 2)
                self.assertEqual(context.inventory.errors, ())
                self.assertEqual(context.inventory.unmapped_frontend_sessions, ())
                self.assertEqual(len({t.frontend_binding_keys for t in context.targets}), 2)
                for target in context.targets:
                    self.assertEqual(len(target.frontend_binding_keys), 1)
                    self.assertFalse(target.to_dict()["cleanup_eligible"])
                    self.assertIn("native_record_blocked", target.blocker_codes)
                output = StringIO()
                result = main(["records", "--client", "cindy", "--engine", engine, "--record-id",
                               context.targets[1].record_key.value, "--json"], adapters=adapters,
                              stdout=output, stderr=StringIO())
                self.assertEqual(result, 0, output.getvalue())
                data = json.loads(output.getvalue())
                self.assertEqual(data["count"], 1)
                self.assertEqual(data["records"][0]["record_key"]["value"], context.targets[1].record_key.value)

    def test_multi_profile_plan_and_revalidation_retain_every_store(self):
        for engine in ("pi", "claude"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                ids = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
                adapters = tuple(self._engine_profile(root / str(i), engine, sid, status="deleted")
                                 for i, sid in enumerate(ids))
                coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
                plan = coordinator.plan_operation(client="cindy", engines=(engine,), record_ids=ids,
                    adapters=adapters, plan_path=root / "plan.json")
                self.assertIn("actions", plan, plan)
                actions = [a for a in plan["actions"] if a["kind"] == f"delete_{engine}_session"]
                self.assertEqual(len(actions), 2, plan)
                self.assertEqual(len({a["target"]["storage_id"] for a in actions}), 2)
                live = coordinator._live[plan["operation_id"]]
                for action in actions:
                    context = live.action_contexts[action["action_id"]]
                    fresh = context.session_catalog_builder()
                    self.assertEqual({r.session_id for r in fresh.records}, set(ids))

    def test_default_inventory_finds_native_sessions_without_frontend_rows(self):
        for engine in ("pi", "claude"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temporary:
                adapter = self._engine_profile(Path(temporary), engine, "11111111-1111-4111-8111-111111111111")
                with closing(sqlite3.connect(adapter.database)) as db:
                    db.execute("DELETE FROM sessions")
                    db.commit()
                contexts = build_client_engine_contexts((adapter,), client="cindy")
                targets = [t for context in contexts for t in context.targets]
                self.assertEqual(len(targets), 1)
                self.assertEqual(targets[0].engine, engine)
                self.assertEqual(targets[0].classification, RecordClassification.ORPHAN_NATIVE)

    def test_codex_and_pi_same_id_never_share_native_identity_or_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = self._engine_profile(Path(temporary), "pi", "same-id")
            path = write_rollout(adapter.codex_home, "same-id", originator="cindy")
            create_thread_index(adapter.codex_home, [{"id": "same-id", "rollout_path": str(path)}])
            with closing(sqlite3.connect(adapter.database)) as db:
                db.execute("INSERT INTO sessions(id,sdk_session_id,status,agent_kind) VALUES('codex-ui','same-id','active','codex')")
                db.commit()
            contexts = build_client_engine_contexts((adapter,), client="cindy")
            targets = [t for context in contexts for t in context.targets]
            self.assertEqual(len(targets), 2)
            self.assertEqual({t.engine for t in targets}, {"codex", "pi"})
            for target in targets:
                self.assertEqual(target.engine, target.capability.engine)
                self.assertEqual(len(target.frontend_binding_keys), 1)

    def test_missing_current_binding_is_not_hidden_by_existing_historical_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            home.mkdir()
            database = root / "cindy.db"
            old = _write_pi_session(root / "pi-agent-home", "old")
            create_database(database, [("ui", "missing", "active", "pi")], [
                ("switch", "ui", {"fromAgentKind": "pi", "fromSdkSessionId": str(old)}, 1, None),
            ])
            adapter = CindyAdapter(database=database, codex_home=home, cindy_root=root)
            context = build_client_engine_contexts((adapter,), client="cindy", engines=("pi",))[0]
            self.assertEqual(len(context.targets), 2)
            by_id = {t.record_id: t for t in context.targets}
            self.assertEqual(by_id["missing"].classification, RecordClassification.ORPHAN_FRONTEND)
            self.assertEqual(by_id["old"].classification, RecordClassification.HEALTHY)
            self.assertFalse(by_id["old"].to_dict()["cleanup_eligible"])
            self.assertNotEqual(by_id["missing"].frontend_binding_keys, by_id["old"].frontend_binding_keys)
            old.unlink()
            missing = build_client_engine_contexts((adapter,), client="cindy", engines=("pi",))[0]
            selected = missing.inventory.select(record_ids=("missing", str(old)))
            self.assertEqual(len(selected.targets), 2)

    def test_native_catalog_failures_are_visible_and_cannot_report_success(self):
        for engine in ("pi", "claude"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sid = "11111111-1111-4111-8111-111111111111"
                adapter = self._engine_profile(root, engine, sid, status="deleted")
                bad = (root / "pi-agent-home" / "sessions" / "bad.jsonl" if engine == "pi"
                       else root / "claude-home" / "projects" / "project" / "bad.jsonl")
                bad.write_text("not a session", encoding="utf-8")
                output = StringIO()
                status = main(["records", "--client", "cindy", "--engine", engine, "--json"],
                              adapters=(adapter,), stdout=output, stderr=StringIO())
                data = json.loads(output.getvalue())
                self.assertNotEqual(status, 0)
                self.assertEqual(data["goal_status"], "blocked")
                self.assertTrue(data["errors"])
                self.assertFalse(any(r["cleanup_eligible"] for r in data["records"]))

    def test_builder_exception_is_not_an_empty_successful_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = self._engine_profile(Path(temporary), "pi", "pi-id")
            output = StringIO()
            with patch.object(adapter, "native_catalog_for", side_effect=OSError("fixture")):
                status = main(["records", "--client", "cindy", "--engine", "pi", "--json"],
                              adapters=(adapter,), stdout=output, stderr=StringIO())
            self.assertNotEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue())["goal_status"], "blocked")

    def test_shared_claude_root_keeps_all_profile_references_and_excludes_standalone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "shared-claude"
            sid = "11111111-1111-4111-8111-111111111111"
            other = "22222222-2222-4222-8222-222222222222"
            _write_claude_session(shared, sid)
            _write_claude_session(shared, other)
            adapters = []
            for name in ("Cindy", "CindyGlobal"):
                profile = root / name
                (profile / "codex-home").mkdir(parents=True)
                _create_cindy_database(profile / "cindy.db", [("same-ui", sid, "active", "cc")])
                adapters.append(CindyAdapter(database=profile / "cindy.db", codex_home=profile / "codex-home", cindy_root=profile))
            effective = resolve_claude_paths(environ={"CLAUDE_CONFIG_DIR": str(shared)}, home=root)
            with patch("local_agent_record_janitor.claude_sessions.resolve_claude_paths", return_value=effective):
                context = build_client_engine_contexts(adapters, client="cindy", engines=("claude",))[0]
            self.assertEqual(context.inventory.errors, ())
            self.assertEqual(len(context.native_records), 1)
            self.assertEqual(context.native_records[0].session_id, sid)
            self.assertEqual(len(context.targets), 1)
            self.assertEqual(len(context.targets[0].frontend_binding_keys), 2)
            self.assertEqual({r.source for r in context.targets[0].references}, {a.database for a in adapters})
            self.assertTrue(all(not any(s.backend == "claude" for s in a.describe_client().native_stores) for a in adapters))
            self.assertEqual({r.native_record.store.path for r in context.targets[0].references}, {shared})
            self.assertFalse(context.targets[0].to_dict()["cleanup_eligible"])

    def test_standalone_pi_claude_inventory_and_plan_classification_are_healthy(self):
        for engine in ("pi", "claude"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sid = "11111111-1111-4111-8111-111111111111"
                if engine == "pi":
                    _write_pi_session(root, sid)
                    catalog = build_pi_session_catalog(agent_dir=root, session_root=root / "sessions")
                else:
                    _write_claude_session(root, sid)
                    catalog = build_claude_session_catalog(config_dir=root)
                output = StringIO()
                with patch(f"local_agent_record_janitor.cli._build_{engine}_catalog", return_value=catalog):
                    status = main(["records", "--client", engine, "--json"], adapters=(),
                                  stdout=output, stderr=StringIO(), **{f"{engine}_catalog_builder": lambda **_: catalog})
                self.assertEqual(status, 0, output.getvalue())
                self.assertEqual(json.loads(output.getvalue())["records"][0]["classification"], "healthy")
                coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
                with patch(f"local_agent_record_janitor.session_catalog_factory.build_{engine}_catalog", return_value=catalog):
                    plan = coordinator.plan_operation(client=engine, record_ids=(sid,), plan_path=root / "plan.json")
                self.assertEqual(plan["actions"][0]["classification"], "healthy")

    def test_records_alias_projection_is_scoped_and_excluded_from_snapshot_and_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            first = _write_pi_session(root, "selected-pi")
            _write_pi_session(root, "other-pi")
            catalog = build_pi_session_catalog(agent_dir=root, session_root=root / "sessions")
            inventory, contexts = build_native_client_inventory(client="pi", engine="pi", catalog=catalog)
            frozen_targets = [target.to_dict() for target in inventory.targets]
            approval = [record.approval_payload() for record in catalog.records]
            results = []
            for _ in range(2):
                output = StringIO()
                with patch("local_agent_record_janitor.cli._build_pi_catalog", return_value=catalog), patch(
                        "local_agent_record_janitor.codex_desktop_state._running_related_process_records",
                        side_effect=AssertionError("No declared owner may be probed")):
                    status = main(["records", "--client", "pi", "--record-id", "selected-pi",
                                   "--inspect-clients", "--json"], adapters=(), stdout=output, stderr=StringIO(),
                                  pi_catalog_builder=lambda **_: catalog)
                self.assertEqual(status, 0, output.getvalue())
                results.append(json.loads(output.getvalue()))
            aliases = results[0]["file_aliases"]
            self.assertEqual([entry["lexical_path"] for entry in aliases["entries"]], [str(first)])
            self.assertTrue(aliases["probe_complete"], aliases)
            self.assertFalse(aliases["alias_coverage_complete"])
            self.assertEqual(results[0]["snapshot_id"], results[1]["snapshot_id"])
            expected = "inventory:v1:" + hashlib.sha256(json.dumps(
                results[0]["records"], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            self.assertEqual(results[0]["snapshot_id"], expected)
            self.assertNotIn("file_aliases", str(frozen_targets))
            self.assertEqual([target.to_dict() for target in inventory.targets], frozen_targets)
            self.assertEqual([record.approval_payload() for record in catalog.records], approval)
            owner = results[0]["client_ownership"][0]
            self.assertIsNone(owner["owner_process_root"])
            self.assertIsNone(owner["clients_closed"])
            self.assertFalse(owner["coverage_complete"])

    def test_observed_hardlinks_do_not_merge_logical_native_stores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            sid = "11111111-1111-4111-8111-111111111111"
            homes = (root / "one", root / "two")
            files = [write_rollout(home, sid, originator="codex_cli_rs", source="cli") for home in homes]
            files[1].unlink()
            os.link(files[0], files[1])
            for home, file in zip(homes, files):
                create_thread_index(home, [{"id": sid, "rollout_path": str(file), "source": "cli"}])
            adapters = tuple(NativeIntegrityAdapter(codex_home=home) for home in homes)
            catalog = build_session_catalog(adapters)
            inventory, contexts = build_native_client_inventory(client="native", engine="codex", catalog=catalog,
                                                                adapters=adapters)
            aliases = collect_client_file_aliases(contexts, inventory.targets)
            self.assertEqual(len(inventory.targets), 2)
            self.assertEqual(len({target.record_key.store.value for target in inventory.targets}), 2)
            self.assertEqual(aliases.entries[0].file_id, aliases.entries[1].file_id)
            self.assertTrue(all(entry.hardlink_count_matches for entry in aliases.entries), aliases.to_dict())
            selected = collect_client_file_aliases(contexts, inventory.targets[:1])
            self.assertEqual(len(selected.entries), 1)
            self.assertFalse(selected.entries[0].hardlink_count_matches)

    def test_unknown_cindy_engines_are_visible_and_never_have_delete_actions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "codex-home").mkdir()
            create_database(root / "cindy.db", [("active", None, "active", "gemini"),
                                                ("deleted", "unknown-id", "deleted", "gemini"),
                                                ("alias-0", None, "deleted", "codex-cli"),
                                                ("alias-1", None, "active", "codex_native"),
                                                ("alias-2", None, "deleted", "claude-code")], [
                ("switch", "active", {"fromAgentKind": "future-engine", "fromSdkSessionId": "old"}, 1, None),
            ])
            adapter = CindyAdapter(database=root / "cindy.db", codex_home=root / "codex-home", cindy_root=root)
            output = StringIO()
            status = main(["records", "--client", "cindy", "--json"], adapters=(adapter,), stdout=output, stderr=StringIO())
            self.assertEqual(status, 0, output.getvalue())
            records = json.loads(output.getvalue())["records"]
            self.assertEqual(len(records), 6)
            self.assertTrue(all(r["classification"] == "unverified" and not r["action_ids"] for r in records), records)
            for selector in ("active", "unknown-id", "alias-0", "alias-1", "alias-2"):
                coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
                plan = coordinator.plan_operation(client="cindy", record_ids=(selector,), adapters=(adapter,),
                    plan_path=root / f"{selector}.json")
                self.assertEqual(plan["actions"], [])

    def test_unimplemented_clients_do_not_inherit_native_capabilities(self):
        for client in ("chatgpt", "chatgpt-desktop", "orca", "herdr"):
            with self.subTest(client=client):
                capability = capability_for(client, "codex")
                self.assertFalse(capability.native_delete)
                self.assertFalse(capability.frontend_session_delete)

    def test_cindy_client_inspection_uses_owner_root_for_frontend_only_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "codex-home").mkdir()
            _create_cindy_database(root / "cindy.db", [("ui", "missing", "deleted", "pi")])
            adapter = CindyAdapter(database=root / "cindy.db", codex_home=root / "codex-home", cindy_root=root)
            output = StringIO()
            with patch("local_agent_record_janitor.codex_desktop_state.inspect_client_ownership",
                       return_value={"clients_closed": False}) as inspect:
                status = main(["records", "--client", "cindy", "--engine", "pi", "--inspect-clients", "--json"],
                              adapters=(adapter,), stdout=output, stderr=StringIO())
            self.assertEqual(status, 0, output.getvalue())
            inspect.assert_called_once_with(root, owner_client="cindy", engines=("pi",))
            self.assertFalse(json.loads(output.getvalue())["client_ownership"][0]["clients_closed"])

    def _lineage_fixture(self, root, parents, *, references=("parent",)):
        home = root / "codex-home"
        rows = []
        for record_id, parent in parents.items():
            source = (
                {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
                if parent else "app-server"
            )
            path = write_rollout(home, record_id, originator="cindy", source=source)
            rows.append({"id": record_id, "rollout_path": str(path),
                         "source": json.dumps(source)})
        create_thread_index(home, rows)
        with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
            db.execute("ALTER TABLE threads ADD COLUMN title TEXT")
            db.execute("UPDATE threads SET title='A record title'")
            db.commit()
        database = root / "cindy.db"
        _create_cindy_database(database, [
            (f"ui-{record_id}", record_id, "active", "codex")
            for record_id in references
        ])
        return CindyAdapter(database=database, codex_home=home, cindy_root=root)

    def test_cindy_children_keep_lineage_and_are_not_unreferenced_orphans(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = self._lineage_fixture(Path(temporary), {
                "parent": None, "child": "parent", "grandchild": "child",
            })
            inventory = build_client_inventory((adapter,), client="cindy")
            targets = {target.record_id: target for target in inventory.targets}
            self.assertEqual(targets["parent"].descendant_thread_ids, ("child", "grandchild"))
            for record_id, parent_id in (("child", "parent"), ("grandchild", "child")):
                child = targets[record_id]
                self.assertEqual(child.classification, RecordClassification.HEALTHY)
                self.assertTrue(child.is_subagent)
                self.assertEqual(child.parent_thread_ids, (parent_id,))
                self.assertEqual(child.frontend_reference_ids, ())
                self.assertEqual(child.to_dict()["lineage_status"], "known")
                self.assertEqual(child.display_name, "A record title")
            # Both CLI projections use the complete catalog before narrowing display.
            for selection in (
                ["--client", "cindy"],
                ["--client", "cindy", "--record-id", "grandchild"],
                ["--platform", "cindy"],
            ):
                output = StringIO()
                status = main(["records", *selection, "--json"], adapters=(adapter,),
                              stdout=output, stderr=StringIO())
                self.assertEqual(status, 0, output.getvalue())
                payload = json.loads(output.getvalue())
                self.assertTrue(all(r["classification"] == "healthy" for r in payload["records"]))
                if "--client" in selection:
                    self.assertEqual(payload["classifications"]["orphan_native"], 0)
                    child = next(r for r in payload["records"] if r["record_id"] == "grandchild")
                    self.assertEqual(child["parent_thread_ids"], ["child"])
                    self.assertTrue(child["is_subagent"])

    def test_parent_placeholder_and_other_store_do_not_hide_missing_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._lineage_fixture(root / "one", {"child": "parent"})
            second = self._lineage_fixture(root / "two", {"parent": None})
            inventory = build_client_inventory((first, second), client="cindy")
            child = next(target for target in inventory.targets if target.record_id == "child")
            self.assertEqual(child.classification, RecordClassification.ORPHAN_NATIVE)
            self.assertEqual(child.parent_thread_ids, ("parent",))
            self.assertEqual(child.to_dict()["lineage_status"], "missing_parent")
            placeholder = next(record for record in inventory.records
                               if record.codex_home == first.codex_home and record.thread_id == "parent")
            self.assertFalse(placeholder.artifact_present)

    def test_non_lineage_metadata_conflict_does_not_break_parent_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = self._lineage_fixture(Path(temporary), {"parent": None, "child": "parent"})
            with closing(sqlite3.connect(adapter.codex_home / "state_5.sqlite")) as db:
                db.execute("UPDATE threads SET archived=1 WHERE id='parent'")
                db.commit()
            inventory = build_client_inventory((adapter,), client="cindy")
            parent = next(r for r in inventory.records if r.thread_id == "parent")
            self.assertTrue(parent.summary.metadata_conflicts)
            self.assertTrue(all(t.classification == RecordClassification.HEALTHY
                                for t in inventory.targets))

    def test_injected_native_catalog_binds_frontend_references_in_the_same_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._lineage_fixture(root / "one", {"parent": None, "child": "parent"})
            second = self._lineage_fixture(root / "two", {"parent": None}, references=())
            inventory = build_client_inventory((first, second), client="cindy")
            catalog = replace(inventory.catalog, records=tuple(
                replace(record, frontend_sessions=()) for record in inventory.records
            ))
            contexts = build_client_engine_contexts(
                (first, second), client="cindy", inventory=inventory,
                native_catalogs={"codex": catalog},
            )
            parents = [t for c in contexts for t in c.targets if t.record_id == "parent"]
            bound = next(t for t in parents if t.record_key.store.path == first.codex_home)
            unbound = next(t for t in parents if t.record_key.store.path == second.codex_home)
            self.assertEqual(bound.frontend_reference_ids, ("cindy:ui-parent",))
            self.assertEqual(bound.classification, RecordClassification.HEALTHY)
            self.assertEqual(unbound.frontend_reference_ids, ())
            self.assertEqual(unbound.classification, RecordClassification.ORPHAN_NATIVE)

    def test_index_only_and_rollout_only_parents_prove_child_relationship(self):
        for parent_source in ("index", "rollout"):
            with self.subTest(parent_source=parent_source), tempfile.TemporaryDirectory() as temporary:
                adapter = self._lineage_fixture(
                    Path(temporary), {"parent": None, "child": "parent"}, references=(),
                )
                catalog = build_session_catalog((adapter,))
                parent = next(r for r in catalog.records if r.thread_id == "parent")
                if parent_source == "index":
                    for rollout in parent.rollouts:
                        rollout.path.unlink()
                else:
                    with closing(sqlite3.connect(adapter.codex_home / "state_5.sqlite")) as db:
                        db.execute("DELETE FROM threads WHERE id='parent'")
                        db.commit()
                inventory = build_client_inventory((adapter,), client="cindy")
                targets = {t.record_id: t for t in inventory.targets}
                self.assertEqual(targets["parent"].classification, RecordClassification.ORPHAN_NATIVE)
                self.assertEqual(targets["child"].classification, RecordClassification.HEALTHY)
                self.assertEqual(targets["child"].lineage_status, "known")

    def test_incomplete_lineage_scan_does_not_prove_a_child_is_healthy(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = self._lineage_fixture(Path(temporary), {"parent": None, "child": "parent"})
            with patch("local_agent_record_janitor.inventory.read_native_lineage",
                       side_effect=OSError("fixture incomplete graph")):
                inventory = build_client_inventory((adapter,), client="cindy")
            child = next(t for t in inventory.targets if t.record_id == "child")
            self.assertEqual(child.classification, RecordClassification.BROKEN_RELATION)
            self.assertEqual(child.lineage_status, "unknown")
            self.assertFalse(child.to_dict()["cleanup_eligible"])

    def test_lineage_conflicts_survive_engine_context_projection(self):
        for parents in (
            {"parent": "child", "child": "parent"},
            {"parent": None, "child": "child"},
        ):
            with self.subTest(parents=parents), tempfile.TemporaryDirectory() as temporary:
                adapter = self._lineage_fixture(Path(temporary), parents)
                contexts = build_client_engine_contexts((adapter,), client="cindy")
                child = next(t for c in contexts for t in c.targets if t.record_id == "child")
                self.assertEqual(child.classification, RecordClassification.BROKEN_RELATION)
                self.assertIn("lineage_conflict", child.blocker_codes)
                self.assertTrue(child.blockers)
                self.assertEqual(child.to_dict()["lineage_status"], "conflict")
                self.assertFalse(child.to_dict()["cleanup_eligible"])

    def test_child_with_unknown_parent_is_not_reported_as_a_confirmed_orphan(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = self._lineage_fixture(Path(temporary), {"child": None}, references=())
            source = {"subagent": {"other": "guardian"}}
            write_rollout(adapter.codex_home, "child", originator="cindy", source=source)
            with closing(sqlite3.connect(adapter.codex_home / "state_5.sqlite")) as db:
                db.execute("UPDATE threads SET source=?", (json.dumps(source),))
                db.commit()
            inventory = build_client_inventory((adapter,), client="cindy")
            child = next(t for t in inventory.targets if t.record_id == "child")
            self.assertTrue(child.is_subagent)
            self.assertEqual(child.to_dict()["lineage_status"], "unknown")
            self.assertEqual(child.classification, RecordClassification.BROKEN_RELATION)
            self.assertFalse(child.to_dict()["cleanup_eligible"])

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
            cindy_root = root / "CindyDev"
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
            output = StringIO()
            with patch("local_agent_record_janitor.codex_desktop_state.inspect_client_ownership",
                       return_value={"clients_closed": None}) as inspect:
                status = main(["records", "--client", "aionui", "--project", "project-only",
                               "--inspect-clients", "--json"], adapters=(adapter,),
                              stdout=output, stderr=StringIO())
            self.assertEqual(status, 0, output.getvalue())
            inspect.assert_called_once_with(adapter.describe_client().owner_process_root,
                                           owner_client="aionui", engines=("codex",))

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
                    "unverified",
                },
            )


if __name__ == "__main__":
    unittest.main()
