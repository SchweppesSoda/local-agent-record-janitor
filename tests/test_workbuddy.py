from __future__ import annotations

from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from local_agent_record_janitor import workbuddy_store as store
from local_agent_record_janitor.workbuddy_store import WorkBuddyAdapter, WorkBuddyStoreError
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.client_inventory import build_client_inventory, build_client_engine_contexts
from local_agent_record_janitor.operation_store import OperationStore
from local_agent_record_janitor import workbuddy_runtime as runtime

SID = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
ORPHAN = "00000000-0000-4000-8000-000000000003"
ORPHAN_TWO = "00000000-0000-4000-8000-000000000004"
USER = "00000000-0000-4000-8000-000000000010"
SECRET = "PRIVATE transcript prompt and title should never appear in evidence"
FIXTURE = Path(__file__).parent / "fixtures/workbuddy_562.sql"


def create_store(root, *, wal=False, session_ids=(SID, OTHER)):
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "workbuddy.db")) as db:
        db.executescript(FIXTURE.read_text(encoding="utf-8"))
        if wal:
            db.execute("PRAGMA journal_mode=WAL")
        for sid in session_ids:
            db.execute("""INSERT INTO sessions(id,cwd,user_id,title,custom_title,status,created_at,updated_at,
                deleted_at,transport) VALUES (?, 'D:/work/project', ?, ?, ?, 'completed', 1, 2, 3, 'local')""",
                (sid, USER, SECRET, SECRET))
            db.execute("INSERT INTO session_usage VALUES (?, 5, 10, 2, ?)", (sid, SECRET))
        db.execute("""INSERT INTO automations(id,name,prompt,status,created_at,updated_at)
                      VALUES ('keep-automation', 'Definition', ?, 'enabled', 1, 2)""", (SECRET,))
        db.execute("INSERT INTO workspaces VALUES ('D:/work/project', 1)")
        db.commit()
    project = root / "projects" / "d-Users-fangjun-Documents-project"
    project.mkdir(parents=True)
    for sid in session_ids:
        (project / (sid + ".jsonl")).write_text(SECRET, encoding="utf-8")
        (project / (sid + ".meta.json")).write_text(json.dumps({"sessionId": sid, "title": SECRET}), encoding="utf-8")
        for directory in ("artifact-index", "file-tree-manifests", "media-index"):
            path = root / directory
            path.mkdir(exist_ok=True)
            (path / (sid + ".json")).write_text(json.dumps({"sessionId": sid}), encoding="utf-8")
        (project / (sid + ".quickask")).write_bytes(b"")
    (root / USER).mkdir()
    sidebar = {"version": 1, "savedAt": 123, "items": [{"id": sid, "title": SECRET, "state": "idle",
        "transport": "local", "kind": "task", "lastActivityAt": 2, "updatedAt": 2, "conversationOrigin": "local"}
        for sid in session_ids]}
    (root / USER / "sidebar-list-snapshot.json").write_text(json.dumps(sidebar), encoding="utf-8")
    for suffix in (f"user-{USER}-personal/global", f"user-{USER}-personal", f"user-{USER}"):
        directory = root / "storage" / suffix
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "conversations.json").write_text(json.dumps({"pinned": [{"id": sid, "groupKey": ""}
            for sid in session_ids]}), encoding="utf-8")
    (root / "settings.json").write_text(SECRET, encoding="utf-8")
    workspace = root / "workspace/sessions" / SID
    workspace.mkdir(parents=True)
    (workspace / "user-result.txt").write_text(SECRET, encoding="utf-8")
    return root


class WorkBuddyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = create_store(self.base / "profile")

    def evidence(self, ids=(SID,)):
        return store.freeze(self.root, ids)

    def ids(self, root=None):
        with closing(sqlite3.connect((root or self.root) / "workbuddy.db")) as db:
            return [row[0] for row in db.execute("SELECT id FROM sessions ORDER BY id")]

    def coordinator(self, inspector=None):
        return OperationCoordinator(CleanupService(client_inspector=inspector or (lambda _: ())))

    def adapter(self, root=None):
        return WorkBuddyAdapter(profile_root=root or self.root)

    def plan(self, *, coordinator=None, adapters=None, path=None, **scope):
        return (coordinator or self.coordinator()).plan_operation(client="workbuddy",
            adapters=adapters or (self.adapter(),), plan_path=path or self.base / "plan.json", **(scope or {"record_ids": (SID,)}))

    def add_pinned_only(self, ids=(ORPHAN,), *, user=USER, environment="personal", legacy=True):
        roots = [self.root / "storage" / f"user-{user}-{environment}" / "global"]
        if legacy:
            roots.extend((roots[0].parent, self.root / "storage" / f"user-{user}"))
        before = {}
        for directory in roots:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "conversations.json"
            value = json.loads(path.read_text()) if path.exists() else {"pinned": []}
            before[str(path)] = list(value["pinned"])
            value["pinned"][1:1] = [{"id": sid, "groupKey": "private-group-to-preserve"} for sid in ids]
            path.write_text(json.dumps(value))
        return before

    def test_metadata_inventory_has_independent_identity_and_no_body(self):
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        self.assertEqual(inventory.engines, ("workbuddy",))
        self.assertEqual(len(inventory.targets), 2)
        self.assertEqual(inventory.targets[0].record_key.store.backend, "workbuddy")
        self.assertNotIn(SECRET, json.dumps(inventory.to_dict()))
        self.assertTrue(all(target.capability.native_delete for target in inventory.targets))
        contexts = build_client_engine_contexts((self.adapter(),), client="workbuddy", inventory=inventory)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].native_catalog, None)
        self.assertNotIn("delete_native", contexts[0].targets[0].action_ids)

    def test_discovery_bound_and_invalid_timestamp_fail_closed(self):
        with patch.object(store, "MAX_ENTRIES", 1):
            inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        self.assertEqual(inventory.errors[0].message, "workbuddy_discovery_limit_exceeded")
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("UPDATE sessions SET created_at=-1 WHERE id=?", (SID,)); db.commit()
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        self.assertEqual(inventory.errors[0].message, "workbuddy_session_metadata_unknown")

    def test_sync_reference_and_automation_dependency_block_only_related_ids(self):
        with closing(sqlite3.connect(self.root / "edge-sync-mapping.db")) as db:
            db.executescript("""CREATE TABLE edge_sync_mapping(session_id TEXT PRIMARY KEY,conversation_id TEXT,msg_channel TEXT,created_at INTEGER);
                CREATE TABLE edge_sync_image_mapping(blob_id TEXT PRIMARY KEY,cos_uri TEXT,session_id TEXT,created_at INTEGER);
                CREATE TABLE edge_sync_artifact_cache(file_path TEXT PRIMARY KEY,mtime_ms INTEGER,size INTEGER,file_hash TEXT,download_url TEXT,
                    smh_path TEXT,content_type TEXT,expires_at INTEGER,uploaded_at INTEGER);""")
            db.execute("INSERT INTO edge_sync_mapping VALUES (?, 'cloud-id', 'local', 1)", (SID,)); db.commit()
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        by_id = {target.record_id: target for target in inventory.targets}
        self.assertIn("workbuddy_remote_sync_reference_unproven", by_id[SID].blocker_codes)
        self.assertTrue(by_id[OTHER].capability.native_delete)
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("INSERT INTO automation_runtime_state(automation_id,running_conversation_id) VALUES ('keep-automation',?)", (OTHER,)); db.commit()
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        self.assertIn("workbuddy_session_dependency_unproven", {target.record_id: target for target in inventory.targets}[OTHER].blocker_codes)

    def test_local_complete_closure_and_settings_other_sessions_preserved(self):
        evidence = self.evidence()
        self.assertNotIn(SECRET, json.dumps(evidence))
        result = store.execute(evidence, client_inspector=lambda _: ())
        self.assertEqual(result.deleted_ids, (SID,))
        self.assertEqual(result.deleted_session_count, 1)
        self.assertEqual(result.deleted_usage_count, 1)
        self.assertEqual(result.deleted_artifact_count, 6)
        self.assertEqual(result.removed_ui_reference_count, 4)
        self.assertEqual(self.ids(), [OTHER])
        self.assertEqual(store.remaining(evidence), [])
        self.assertEqual((self.root / "settings.json").read_text(), SECRET)
        self.assertEqual((self.root / "workspace/sessions" / SID / "user-result.txt").read_text(), SECRET)
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            self.assertEqual(db.execute("SELECT prompt FROM automations").fetchall(), [(SECRET,)])
            self.assertEqual(db.execute("SELECT path FROM workspaces").fetchall(), [("D:/work/project",)])
            self.assertEqual(db.execute("SELECT session_id FROM session_usage").fetchall(), [(OTHER,)])
        self.assertFalse(list(self.root.glob(".larj-workbuddy-sessions-*")))

    def test_shared_store_multiple_sessions_one_batch(self):
        coordinator = self.coordinator()
        plan = self.plan(coordinator=coordinator, record_ids=(SID, OTHER))
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual(len(plan["child_batches"]), 1)
        self.assertEqual(plan["child_batches"][0]["mutation_family"], store.KIND)
        self.assertNotIn(SECRET, json.dumps(plan))
        result = coordinator.apply_operation(plan_path=self.base / "plan.json", clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertEqual(self.ids(), [])

    def test_profiles_with_same_id_remain_isolated(self):
        other_root = create_store(self.base / "other-profile", session_ids=(SID,))
        inventory = build_client_inventory((self.adapter(), self.adapter(other_root)), client="workbuddy")
        self.assertEqual(len([target for target in inventory.targets if target.record_id == SID]), 2)
        keys = {target.record_key.value for target in inventory.targets if target.record_id == SID}
        self.assertEqual(len(keys), 2)
        store.execute(self.evidence(), client_inspector=lambda _: ())
        self.assertEqual(self.ids(other_root), [SID])

    def test_wal_canonical_snapshots_and_read_marks(self):
        root = create_store(self.base / "wal-profile", wal=True)
        evidence = store.freeze(root, (SID,))
        for _ in range(2):
            store.snapshot(root)
        result = store.execute(evidence, client_inspector=lambda _: ())
        self.assertEqual(result.deleted_ids, (SID,))
        self.assertEqual(store.remaining(evidence), [])

    def test_cloud_unknown_transport_and_invalid_rollback_sidecar_inventory_only(self):
        for index, transport in enumerate(("cloud", "mystery")):
            with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
                db.execute("UPDATE sessions SET transport=? WHERE id=?", (transport, SID)); db.commit()
            plan = self.plan(path=self.base / f"blocked-{index}.json", record_ids=(SID,))
            self.assertEqual(plan["goal_status"], "blocked", plan)
            self.assertIn("workbuddy_remote_or_unknown_transport", [item["blocker_code"] for item in plan["blockers"]])
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("UPDATE sessions SET transport='local' WHERE id=?", (SID,)); db.commit()
        path = self.root / "projects/d-Users-fangjun-Documents-project" / (SID + ".file-rollback.ndjson")
        path.write_text('{"v":2,"requestId":"x","commitSeq":1}\n')
        plan = self.plan(path=self.base / "blocked-sidecar.json", record_ids=(SID,))
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertIn("workbuddy_rollback_metadata_unknown", [item["blocker_code"] for item in plan["blockers"]])
        self.assertTrue(path.exists())

    def test_proven_v1_rollback_sidecar_has_body_free_evidence_and_is_deleted(self):
        path = self.root / "projects/d-Users-fangjun-Documents-project" / (SID + ".file-rollback.ndjson")
        private_request = "private-request-id-not-in-evidence"
        path.write_text(json.dumps({"v": 1, "requestId": private_request, "commitSeq": 1}) + "\n")
        evidence = self.evidence()
        self.assertNotIn(private_request, json.dumps(evidence))
        self.assertEqual(evidence[0]["rollback_sidecars"][str(path)], {"format": store.ROLLBACK_FORMAT, "record_count": 1})
        result = store.execute(evidence, client_inspector=lambda _: ())
        self.assertEqual(result.deleted_artifact_count, 7)
        self.assertFalse(path.exists())
        self.assertEqual(result.deleted_session_ids, (SID,))
        self.assertEqual(result.removed_ui_only_ids, ())

    def test_rollback_malformed_duplicate_extra_nested_and_orphan_formats_block(self):
        path = self.root / "projects/d-Users-fangjun-Documents-project" / (SID + ".file-rollback.ndjson")
        cases = ('not-json', '{"v":1,"v":1,"requestId":"x","commitSeq":1}',
            '{"v":true,"requestId":"x","commitSeq":1}', '{"v":1,"requestId":"x","commitSeq":-1}',
            '{"v":1,"requestId":{"body":"private"},"commitSeq":1}', '{"v":1,"requestId":"x","commitSeq":1,"parentSessionId":"x"}')
        for value in cases:
            with self.subTest(value=value):
                path.write_text(value + "\n")
                target = next(target for target in build_client_inventory((self.adapter(),), client="workbuddy").targets if target.record_id == SID)
                self.assertIn("workbuddy_rollback_metadata_unknown", target.blocker_codes)
                self.assertFalse(target.capability.native_delete)
        path.write_text('{"v":1,"requestId":"x","commitSeq":1}\n')
        path.with_name(SID + ".jsonl").unlink()
        self.assertIn("workbuddy_rollback_transcript_identity_unproven", store.snapshot(self.root)["records"][SID]["blocker_codes"])
        self.assertTrue(path.exists())

    def test_exact_pinned_only_cleanup_preserves_sessions_and_all_migration_sources(self):
        before = self.add_pinned_only((ORPHAN, ORPHAN_TWO))
        plan = self.plan(record_ids=(ORPHAN, ORPHAN_TWO))
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual(len(plan["child_batches"]), 1)
        self.assertEqual(plan["child_batches"][0]["mutation_family"], store.UI_KIND)
        self.assertEqual({action["resource"]["kind"] for action in plan["actions"]}, {"workbuddy_ui_reference"})
        self.assertFalse(plan["capabilities"]["workbuddy"]["remote_delete"])
        evidence = [action["impact"]["external_action_payload"]["workbuddy_session_evidence"] for action in plan["actions"]]
        original_sha = evidence[0]["database_sha256"]
        self.assertEqual(original_sha, evidence[0]["after_database_sha256"])
        statements = []
        original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        with patch.object(store.sqlite3, "connect", side_effect=connect):
            result = store.execute(evidence, client_inspector=lambda _: ())
        self.assertEqual(result.deleted_session_ids, ())
        self.assertFalse(any(statement.lstrip().upper().startswith("DELETE") for statement in statements))
        self.assertEqual(result.removed_ui_only_ids, (ORPHAN, ORPHAN_TWO))
        self.assertEqual(result.deleted_session_count, 0)
        self.assertEqual(result.deleted_usage_count, 0)
        self.assertEqual(result.deleted_artifact_count, 0)
        self.assertEqual(result.removed_ui_reference_count, 6)
        self.assertEqual(store.snapshot(self.root)["database_snapshot"]["database_sha256"], original_sha)
        self.assertEqual(self.ids(), [SID, OTHER])
        for source, expected in before.items():
            self.assertEqual(json.loads(Path(source).read_text())["pinned"], expected)
        self.assertEqual(store.remaining(evidence), [])
        self.assertFalse(list(self.root.glob(".larj-workbuddy-sessions-*")))

    def test_mixed_session_and_ui_selection_is_blocked(self):
        self.add_pinned_only()
        plan = self.plan(record_ids=(SID, ORPHAN))
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertEqual(plan["actions"], [])
        self.assertIn("workbuddy_mixed_mutation_families", [blocker["blocker_code"] for blocker in plan["blockers"]])

    def test_pinned_only_cross_user_environment_and_sidebar_are_blocked(self):
        self.add_pinned_only()
        self.add_pinned_only(user=ORPHAN_TWO, legacy=False)
        target = store.snapshot(self.root)["records"][ORPHAN]
        self.assertIn("workbuddy_ui_reference_scope_unproven", target["blocker_codes"])
        for path in (self.root / "storage" / f"user-{ORPHAN_TWO}-personal/global").iterdir():
            path.unlink()
        self.add_pinned_only(environment="another-environment", legacy=False)
        self.assertIn("workbuddy_ui_reference_scope_unproven", store.snapshot(self.root)["records"][ORPHAN]["blocker_codes"])
        path = self.root / "storage" / f"user-{USER}-another-environment/global/conversations.json"
        path.unlink()
        sidebar = self.root / USER / "sidebar-list-snapshot.json"
        value = json.loads(sidebar.read_text()); value["items"].append({"id": ORPHAN, "transport": "cloud"})
        sidebar.write_text(json.dumps(value))
        plan = self.plan(record_ids=(ORPHAN,))
        self.assertEqual(plan["goal_status"], "blocked", plan)

    def test_new_native_row_or_artifact_blocks_frozen_ui_plan(self):
        self.add_pinned_only()
        path = self.base / "ui-old.json"
        self.plan(path=path, record_ids=(ORPHAN,))
        artifact = self.root / "projects/d-Users-fangjun-Documents-project" / (ORPHAN + ".jsonl")
        artifact.write_text(SECRET)
        result = self.coordinator().apply_operation(plan_path=path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "blocked", result)
        artifact.unlink()
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("INSERT INTO sessions(id,cwd,user_id,status,created_at,updated_at,transport) VALUES (?,'D:/work/project',?,'completed',1,2,'local')", (ORPHAN, USER)); db.commit()
        result = self.coordinator().apply_operation(plan_path=path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertIn(ORPHAN, self.ids())

    def test_ui_unknown_blocks_other_session_and_recovers_readonly(self):
        self.add_pinned_only()
        path = self.base / "ui-unknown.json"
        self.plan(path=path, record_ids=(ORPHAN,))
        with patch.object(store, "remaining", side_effect=RuntimeError("verification interrupted")):
            result = self.coordinator().apply_operation(plan_path=path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "unknown", result)
        sibling = self.base / "native-sibling.json"
        self.plan(path=sibling, record_ids=(SID,))
        result = self.coordinator().apply_operation(plan_path=sibling, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "blocked", result)
        with patch.object(store, "execute", side_effect=AssertionError("must not resend")):
            recovered = self.coordinator().verify_operation(plan_path=path, adapters=(self.adapter(),))
        self.assertEqual(recovered["goal_status"], "complete", recovered)
        self.assertTrue(recovered["modified"])
        self.assertEqual(self.ids(), [SID, OTHER])
        result = self.coordinator().apply_operation(plan_path=sibling, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "complete", result)
        verified = self.coordinator().verify_operation(plan_path=path, adapters=(self.adapter(),))
        self.assertEqual(verified["goal_status"], "complete", verified)

    def test_ui_unknown_before_first_write_does_not_claim_modified(self):
        self.add_pinned_only()
        path = self.base / "ui-before.json"
        self.plan(path=path, record_ids=(ORPHAN,))
        with patch.object(store, "_atomic_bytes", side_effect=OSError("first cache write denied")):
            result = self.coordinator().apply_operation(plan_path=path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "unknown", result)
        self.assertFalse(result["modified"])
        with patch.object(store, "execute", side_effect=AssertionError("must not resend")):
            recovered = self.coordinator().verify_operation(plan_path=path, adapters=(self.adapter(),))
        self.assertEqual(recovered["goal_status"], "completed_with_residuals", recovered)
        self.assertFalse(recovered["modified"])
        self.assertIn(ORPHAN, store.snapshot(self.root)["records"])
        self.assertEqual(self.ids(), [SID, OTHER])

    def test_ui_partial_shared_json_write_remains_unknown(self):
        self.add_pinned_only()
        evidence = store.freeze(self.root, (ORPHAN,))
        original = store._atomic_bytes
        calls = 0

        def write(path, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("second cache write failed")
            return original(path, data)

        with patch.object(store, "_atomic_bytes", side_effect=write):
            with self.assertRaises(WorkBuddyStoreError) as caught:
                store.execute(evidence, client_inspector=lambda _: ())
        self.assertTrue(caught.exception.outcome_unknown)
        with self.assertRaises(WorkBuddyStoreError):
            store.remaining(evidence)
        self.assertEqual(self.ids(), [SID, OTHER])
        self.assertTrue(store.recovery_directory(evidence).exists())

    def test_ui_new_dependency_sync_and_frozen_path_tamper_fail_closed(self):
        self.add_pinned_only()
        evidence = store.freeze(self.root, (ORPHAN,))
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("INSERT INTO automation_runtime_state(automation_id,running_conversation_id) VALUES ('keep-automation',?)", (ORPHAN,)); db.commit()
        with self.assertRaisesRegex(WorkBuddyStoreError, "frozen_state_changed"):
            store.execute(evidence, client_inspector=lambda _: ())
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("DELETE FROM automation_runtime_state"); db.commit()
        evidence = store.freeze(self.root, (ORPHAN,))
        evidence[0]["ui_scope"]["user_id"] = ORPHAN_TWO
        with self.assertRaisesRegex(WorkBuddyStoreError, "frozen_ui_identity_invalid"):
            store.execute(evidence, client_inspector=lambda _: ())

    def test_missing_store_unknown_schema_and_trigger_fail_closed(self):
        missing = self.adapter(self.base / "missing")
        inventory = build_client_inventory((missing,), client="workbuddy")
        self.assertTrue(inventory.errors)
        self.assertFalse(inventory.targets)
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("CREATE TRIGGER unsafe AFTER DELETE ON sessions BEGIN DELETE FROM automations; END"); db.commit()
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        self.assertEqual(inventory.errors[0].message, "workbuddy_schema_unknown")
        plan = self.plan(record_ids=(SID,))
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertEqual(self.ids(), [SID, OTHER])

    def test_unknown_orphan_ui_reference_not_hidden_and_exact_local_independent(self):
        pinned = self.root / "storage" / f"user-{USER}" / "conversations.json"
        value = json.loads(pinned.read_text()); value["pinned"].append({"id": ORPHAN, "groupKey": ""})
        pinned.write_text(json.dumps(value))
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        orphan = next(target for target in inventory.targets if target.record_id == ORPHAN)
        self.assertFalse(orphan.capability.native_delete)
        plan = self.plan(record_ids=(SID,))
        self.assertEqual(plan["goal_status"], "ready", plan)
        all_plan = self.plan(path=self.base / "all.json", all_projects=True)
        self.assertEqual(all_plan["goal_status"], "blocked", all_plan)
        self.assertIn("workbuddy_inventory_coverage_unproven", [item["blocker_code"] for item in all_plan["blockers"]])

    def test_orphan_files_and_subagent_only_directory_never_empty_success(self):
        path = self.root / "projects/d-Users-fangjun-Documents-project" / ORPHAN
        path.mkdir()
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        target = next(target for target in inventory.targets if target.record_id == ORPHAN)
        self.assertIn("workbuddy_subagent_closure_unproven", target.blocker_codes)
        self.assertEqual(target.record_metadata["artifact_directories"][0]["path"], str(path))
        self.assertEqual(self.plan(record_ids=(ORPHAN,))["goal_status"], "blocked")

    def test_shared_unknown_artifact_sources_block(self):
        (self.root / "media-index-workspace").mkdir()
        (self.root / "media-index-workspace/legacy.json").write_text('{"records": []}')
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        self.assertEqual(inventory.errors[0].message, "workbuddy_shared_media_index_unproven")
        (self.root / "media-index-workspace/legacy.json").unlink()
        (self.root / "session-artifacts.json").write_text('{}')
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        self.assertEqual(inventory.errors[0].message, "workbuddy_session_artifacts_source_unproven")

    def test_default_projects_soft_deleted_only_and_exact_ids_not_prefix(self):
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("UPDATE sessions SET deleted_at=NULL WHERE id=?", (SID,)); db.commit()
        plan = self.plan(all_projects=True)
        self.assertEqual([action["target"]["thread_id"] for action in plan["actions"]], [OTHER])
        exact = self.plan(path=self.base / "exact.json", record_ids=(SID,))
        self.assertEqual(exact["goal_status"], "ready", exact)
        prefix = self.plan(path=self.base / "prefix.json", record_ids=(SID[:10],))
        self.assertEqual(prefix["goal_status"], "blocked")

    def test_file_and_unselected_body_drift_blocks_without_deleting(self):
        evidence = self.evidence()
        path = Path(evidence[0]["artifacts"][0]["path"])
        path.write_text("changed")
        with self.assertRaisesRegex(WorkBuddyStoreError, "frozen_state_changed"):
            store.execute(evidence, client_inspector=lambda _: ())
        self.assertEqual(self.ids(), [SID, OTHER])
        evidence = self.evidence()
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("UPDATE sessions SET title='changed title' WHERE id=?", (OTHER,)); db.commit()
        with self.assertRaises(WorkBuddyStoreError):
            store.execute(evidence, client_inspector=lambda _: ())
        self.assertEqual(self.ids(), [SID, OTHER])

    def test_writer_guard_and_unreadable_runtime_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "workbuddy_writer_running"):
            store.execute(self.evidence(), client_inspector=lambda _: ("WorkBuddy.exe",))
        with patch.object(runtime, "probe", side_effect=runtime.WorkBuddyRuntimeError("workbuddy_writer_coverage_unknown")):
            with self.assertRaisesRegex(runtime.WorkBuddyRuntimeError, "coverage_unknown"):
                store.execute(self.evidence())
        self.assertEqual(self.ids(), [SID, OTHER])

    def test_duplicate_keys_and_nested_metadata_do_not_leak(self):
        sidebar = self.root / USER / "sidebar-list-snapshot.json"
        sidebar.write_text('{"version":1,"savedAt":0,"items":[],"items":[]}')
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        self.assertEqual(inventory.errors[0].message, "workbuddy_json_duplicate_key")
        sidebar.write_text(json.dumps({"version": 1, "savedAt": 0, "items": [{"id": SID, "state": {"body": SECRET}}]}))
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        self.assertEqual(inventory.errors[0].message, "workbuddy_ui_metadata_unknown")
        self.assertNotIn(SECRET, json.dumps(inventory.to_dict()))

    def test_corrupt_metadata_values_become_structured_errors(self):
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("UPDATE sessions SET created_at='bad timestamp' WHERE id=?", (SID,)); db.commit()
        inventory = build_client_inventory((self.adapter(),), client="workbuddy")
        self.assertEqual(inventory.errors[0].message, "workbuddy_session_metadata_unknown")

    def test_symlink_escape_and_frozen_path_escape_block(self):
        evidence = self.evidence()
        evidence[0]["artifacts"][0]["path"] = str(self.base / "outside.json")
        with self.assertRaises(WorkBuddyStoreError):
            store.execute(evidence, client_inspector=lambda _: ())
        evidence = self.evidence()
        evidence[0]["artifacts"][0]["path"] = str(self.root / "settings.json")
        with self.assertRaisesRegex(WorkBuddyStoreError, "frozen_artifact_path_invalid"):
            store.execute(evidence, client_inspector=lambda _: ())
        link = self.base / "linked-parent"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("The test account cannot create directory symlinks")
        inventory = build_client_inventory((self.adapter(link),), client="workbuddy")
        self.assertTrue(inventory.errors)

    def test_intermediate_ancestor_symlink_is_blocked(self):
        link = self.base / "ancestor-link"
        try:
            link.symlink_to(self.base, target_is_directory=True)
        except OSError:
            self.skipTest("The test account cannot create directory symlinks")
        inventory = build_client_inventory((self.adapter(link / "profile"),), client="workbuddy")
        self.assertTrue(inventory.errors)
        self.assertFalse(inventory.targets)

    def test_drift_between_fresh_snapshot_and_writer_lock_blocks(self):
        evidence = self.evidence()
        original = store.snapshot
        calls = 0

        def changed(root):
            nonlocal calls
            value = original(root)
            calls += 1
            if calls == 2:
                with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
                    db.execute("UPDATE sessions SET title='outside writer changed' WHERE id=?", (OTHER,)); db.commit()
            return value

        with patch.object(store, "snapshot", side_effect=changed):
            with self.assertRaisesRegex(WorkBuddyStoreError, "database_changed_under_lock") as caught:
                store.execute(evidence, client_inspector=lambda _: ())
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(self.ids(), [SID, OTHER])

    def test_unknown_different_root_is_independent(self):
        other_root = create_store(self.base / "independent", session_ids=(SID,))
        path = self.base / "unknown-independent.json"
        coordinator = self.coordinator()
        self.plan(coordinator=coordinator, path=path, record_ids=(SID,))
        with patch.object(store, "remaining", side_effect=RuntimeError("verification interrupted")):
            result = coordinator.apply_operation(plan_path=path, clients_closed=True)
        self.assertEqual(result["goal_status"], "unknown")
        sibling_path = self.base / "independent-plan.json"
        coordinator = self.coordinator()
        plan = self.plan(coordinator=coordinator, adapters=(self.adapter(other_root),), path=sibling_path, record_ids=(SID,))
        self.assertEqual(plan["goal_status"], "ready", plan)
        result = coordinator.apply_operation(plan_path=sibling_path, clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertEqual(self.ids(other_root), [])

    def test_frozen_unknown_recovery_detects_new_copies_and_subagent_directory(self):
        evidence = self.evidence()
        with patch.object(store, "remaining", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(WorkBuddyStoreError):
                store.execute(evidence, client_inspector=lambda _: ())
        duplicate = self.root / "projects/another-project"
        duplicate.mkdir()
        path = duplicate / (SID + ".jsonl")
        path.write_text(SECRET)
        with self.assertRaisesRegex(WorkBuddyStoreError, "artifacts_changed"):
            store.remaining(evidence)
        path.unlink()
        (duplicate / SID).mkdir()
        with self.assertRaisesRegex(WorkBuddyStoreError, "record_boundary_unproven"):
            store.remaining(evidence)
        self.assertTrue(store.recovery_directory(evidence).exists())

    def test_postcommit_unknown_recovers_readonly_and_clears_rollback(self):
        evidence = self.evidence()
        with patch.object(store, "remaining", side_effect=RuntimeError("verification interrupted")):
            with self.assertRaises(WorkBuddyStoreError) as caught:
                store.execute(evidence, client_inspector=lambda _: ())
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertTrue(store.recovery_directory(evidence).exists())
        with patch.object(store, "execute", side_effect=AssertionError("must not resend")):
            self.assertEqual(store.remaining(evidence), [])
        self.assertFalse(store.recovery_directory(evidence).exists())

    def test_preparation_manifest_write_failure_clears_owned_copies(self):
        evidence = self.evidence()
        original = store._write_private

        def write(path, data, identities):
            if path.name == "manifest.json":
                original(path, data[:20], identities)
                raise OSError("manifest write failed")
            return original(path, data, identities)

        with patch.object(store, "_write_private", side_effect=write):
            with self.assertRaises(WorkBuddyStoreError) as caught:
                store.execute(evidence, client_inspector=lambda _: ())
        self.assertTrue(caught.exception.outcome_known_rolled_back)
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(self.ids(), [SID, OTHER])
        self.assertFalse(store.recovery_directory(evidence).exists())
        self.assertEqual(store.execute(evidence, client_inspector=lambda _: ()).deleted_ids, (SID,))

    def test_preparation_ui_drift_clears_owned_copies(self):
        evidence = self.evidence()
        original = store._expected_after

        def after(backup, items):
            result = original(backup, items)
            path = self.root / USER / "sidebar-list-snapshot.json"
            value = json.loads(path.read_text()); value["savedAt"] = 124
            path.write_text(json.dumps(value))
            return result

        with patch.object(store, "_expected_after", side_effect=after):
            with self.assertRaisesRegex(WorkBuddyStoreError, "ui_reference_count_changed") as caught:
                store.execute(evidence, client_inspector=lambda _: ())
        self.assertTrue(caught.exception.outcome_known_rolled_back)
        self.assertEqual(self.ids(), [SID, OTHER])
        self.assertFalse(store.recovery_directory(evidence).exists())

    def test_preparation_cleanup_failure_keeps_unknown_shared_occupancy(self):
        path = self.base / "preparation.json"
        self.plan(path=path, record_ids=(SID,))
        original = store._write_private

        def write(target, data, identities):
            if target.name == "manifest.json":
                raise OSError("manifest failed")
            return original(target, data, identities)

        with patch.object(store, "_write_private", side_effect=write), patch.object(store, "_discard_preparation", side_effect=OSError("cleanup denied")):
            result = self.coordinator().apply_operation(plan_path=path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "unknown", result)
        self.assertEqual(self.coordinator().status_operation(plan_path=path)["goal_status"], "unknown")
        sibling = self.base / "prep-sibling.json"
        self.plan(path=sibling, record_ids=(OTHER,))
        result = self.coordinator().apply_operation(plan_path=sibling, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertEqual(self.ids(), [SID, OTHER])
        self.assertTrue(list(self.root.glob(".larj-workbuddy-sessions-*")))

    def test_completed_preparation_later_drift_cleanup_failure_is_unknown(self):
        path = self.base / "late-preflight.json"
        self.plan(path=path, record_ids=(SID,))
        original = store._prepare_recovery

        def prepare(items):
            value = original(items)
            with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
                db.execute("UPDATE automations SET prompt='outside writer changed'"); db.commit()
            return value

        with patch.object(store, "_prepare_recovery", side_effect=prepare), patch.object(store, "_discard_preparation", side_effect=OSError("cleanup denied")):
            result = self.coordinator().apply_operation(plan_path=path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "unknown", result)
        self.assertEqual(self.coordinator().status_operation(plan_path=path)["goal_status"], "unknown")
        self.assertTrue(list(self.root.glob(".larj-workbuddy-sessions-*")))
        sibling = self.base / "late-sibling.json"
        self.plan(path=sibling, record_ids=(OTHER,))
        result = self.coordinator().apply_operation(plan_path=sibling, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertEqual(self.ids(), [SID, OTHER])

    def test_partial_artifact_delete_and_json_write_failures_stay_unknown(self):
        evidence = self.evidence()
        failing_path = Path(evidence[0]["artifacts"][1]["path"])
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == failing_path:
                raise OSError("simulated unlink failure")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            with self.assertRaises(WorkBuddyStoreError) as caught:
                store.execute(evidence, client_inspector=lambda _: ())
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertFalse(caught.exception.outcome_known_rolled_back)
        with self.assertRaisesRegex(WorkBuddyStoreError, "mixed_or_changed_state"):
            store.remaining(evidence)
        self.assertTrue(store.recovery_directory(evidence).exists())

    def test_db_commit_then_json_failure_preserves_unknown_recovery(self):
        evidence = self.evidence()
        with patch.object(store, "_atomic_bytes", side_effect=OSError("JSON write failed")):
            with self.assertRaises(WorkBuddyStoreError) as caught:
                store.execute(evidence, client_inspector=lambda _: ())
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(self.ids(), [OTHER])
        with self.assertRaises(WorkBuddyStoreError):
            store.remaining(evidence)
        self.assertTrue(store.recovery_directory(evidence).exists())

    def test_missing_backup_and_changed_unselected_state_cannot_be_completed(self):
        evidence = self.evidence()
        with patch.object(store, "remaining", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(WorkBuddyStoreError):
                store.execute(evidence, client_inspector=lambda _: ())
        directory = store.recovery_directory(evidence)
        (directory / "database.sqlite").unlink()
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("UPDATE automations SET prompt='changed definition'"); db.commit()
        with self.assertRaisesRegex(WorkBuddyStoreError, "mixed_or_changed_state"):
            store.remaining(evidence)
        self.assertTrue(directory.exists())

    def test_forged_manifest_after_digest_does_not_override_plan(self):
        evidence = self.evidence()
        with patch.object(store, "remaining", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(WorkBuddyStoreError):
                store.execute(evidence, client_inspector=lambda _: ())
        directory = store.recovery_directory(evidence)
        manifest = directory / "manifest.json"
        value = json.loads(manifest.read_text()); value["after_database_sha256"] = "a" * 64
        manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(WorkBuddyStoreError, "after_unproven"):
            store.remaining(evidence)
        self.assertTrue(directory.exists())

    def test_cleanup_interruption_recovers_from_frozen_after_proof(self):
        evidence = self.evidence()
        with patch.object(store, "_discard_recovery", side_effect=OSError("cleanup interrupted")):
            with self.assertRaises(WorkBuddyStoreError):
                store.execute(evidence, client_inspector=lambda _: ())
        directory = store.recovery_directory(evidence)
        (directory / "database.sqlite").unlink()
        self.assertEqual(store.remaining(evidence), [])
        self.assertFalse(directory.exists())

    def test_protocol_unknown_same_db_other_id_is_blocked_across_instances(self):
        coordinator = self.coordinator()
        path = self.base / "unknown.json"
        plan = self.plan(coordinator=coordinator, path=path, record_ids=(SID,))
        with patch.object(store, "remaining", side_effect=RuntimeError("verification interrupted")):
            result = coordinator.apply_operation(plan_path=path, clients_closed=True)
        self.assertEqual(result["goal_status"], "unknown", result)
        self.assertEqual(self.coordinator().status_operation(plan_path=path)["goal_status"], "unknown")
        with patch.object(store, "execute", side_effect=AssertionError("must not resend")):
            repeat = self.coordinator().apply_operation(plan_path=path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(repeat["goal_status"], "unknown")
        sibling_path = self.base / "sibling.json"
        sibling = self.plan(path=sibling_path, record_ids=(OTHER,))
        self.assertEqual(sibling["goal_status"], "ready", sibling)
        result = self.coordinator().apply_operation(plan_path=sibling_path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertEqual(self.ids(), [OTHER])
        recovered = self.coordinator().verify_operation(plan_path=path, adapters=(self.adapter(),))
        self.assertEqual(recovered["goal_status"], "complete", recovered)
        self.assertFalse(store.recovery_directory([action["impact"]["external_action_payload"]["workbuddy_session_evidence"]
                                                 for action in plan["actions"]]).exists())
        result = self.coordinator().apply_operation(plan_path=sibling_path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "complete", result)

    def test_plan_hash_scope_and_frozen_root_binding(self):
        coordinator = self.coordinator()
        plan = self.plan(coordinator=coordinator, record_ids=(SID,))
        path = self.base / "plan.json"
        blocked = coordinator.apply_operation(plan_path=path, clients_closed=True, plan_sha256="0" * 64)
        self.assertEqual(blocked["goal_status"], "blocked")
        blocked = coordinator.apply_operation(plan_path=path, clients_closed=True, scope={"client": "workbuddy", "record_ids": (OTHER,)})
        self.assertEqual(blocked["goal_status"], "blocked")
        blocked = coordinator.apply_operation(plan_path=path, clients_closed=True, workbuddy_roots=(self.base,))
        self.assertEqual(blocked["goal_status"], "blocked")
        self.assertEqual(self.ids(), [SID, OTHER])
        result = self.coordinator().apply_operation(plan_path=path, clients_closed=True, plan_sha256=plan["plan_sha256"],
                                                   adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "complete", result)

    def test_verified_operation_survives_later_other_session_delete(self):
        path = self.base / "finished.json"
        first = self.plan(path=path, record_ids=(SID,))
        result = self.coordinator().apply_operation(plan_path=path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertTrue(OperationCoordinator._workbuddy_terminal_verified(first, str(self.root)))
        other_path = self.base / "later.json"
        self.plan(path=other_path, record_ids=(OTHER,))
        result = self.coordinator().apply_operation(plan_path=other_path, clients_closed=True, adapters=(self.adapter(),))
        self.assertEqual(result["goal_status"], "complete", result)
        verified = self.coordinator().verify_operation(plan_path=path, adapters=(self.adapter(),))
        self.assertEqual(verified["goal_status"], "complete", verified)
        child = first["child_batches"][0]["child_operation_id"]
        operation_store = OperationStore(self.root, child)
        operation_store.lock_path.write_text("unproven lock")
        self.assertFalse(OperationCoordinator._workbuddy_terminal_verified(first, str(self.root)))
        operation_store.lock_path.unlink()

    def test_cli_records_and_operation_root_options(self):
        from local_agent_record_janitor.cli import main, build_parser
        args = build_parser().parse_args(["delete", "plan", "--client", "workbuddy", "--workbuddy-root", str(self.root),
                                        "--record-id", SID, "--out", str(self.base / "cli.json"), "--json"])
        self.assertEqual(args.workbuddy_root, [self.root])
        repeated = build_parser().parse_args(["records", "--client", "workbuddy", "--workbuddy-root", str(self.root),
                                             "--workbuddy-root", str(self.base / "other")])
        self.assertEqual(repeated.workbuddy_root, [self.root, self.base / "other"])
        stdout = io.StringIO()
        code = main(["records", "--client", "workbuddy", "--workbuddy-root", str(self.root), "--json"],
                    stdout=stdout, stderr=io.StringIO(), adapters=(self.adapter(),))
        self.assertEqual(code, 0, stdout.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["count"], 2)
        self.assertNotIn(SECRET, stdout.getvalue())

    def test_cli_native_plan_without_usage_survives_fresh_apply_and_verify(self):
        from local_agent_record_janitor.cli import main
        with closing(sqlite3.connect(self.root / "workbuddy.db")) as db:
            db.execute("DELETE FROM session_usage WHERE session_id=?", (SID,)); db.commit()
        path = self.base / "cli-native-no-usage.json"
        output = io.StringIO()
        code = main(["delete", "plan", "--client", "workbuddy", "--workbuddy-root", str(self.root),
            "--record-id", SID, "--out", str(path), "--json"], stdout=output, stderr=io.StringIO(),
            client_inspector=lambda _: ())
        self.assertEqual(code, 0, output.getvalue())
        document = json.loads(path.read_text(encoding="utf-8"))
        evidence = document["actions"][0]["impact"]["external_action_payload"]["workbuddy_session_evidence"]
        self.assertIn("usage", evidence)
        self.assertIsNone(evidence["usage"])
        self.assertIsNone(evidence["row"]["conversation_origin"])
        self.assertTrue(all(value is None for value in evidence["database_sidecars"].values()))
        result = self.coordinator().apply_operation(plan_path=path, clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertTrue(result["modified"])
        verified = self.coordinator().verify_operation(plan_path=path)
        self.assertEqual(verified["goal_status"], "complete", verified)
        self.assertEqual(self.ids(), [OTHER])

    def test_cli_ui_only_plan_survives_fresh_apply_and_verify(self):
        from local_agent_record_janitor.cli import main
        self.add_pinned_only((ORPHAN, ORPHAN_TWO))
        path = self.base / "cli-ui-only.json"
        output = io.StringIO()
        code = main(["delete", "plan", "--client", "workbuddy", "--workbuddy-root", str(self.root),
            "--record-id", ORPHAN, "--record-id", ORPHAN_TWO, "--out", str(path), "--json"],
            stdout=output, stderr=io.StringIO(), client_inspector=lambda _: ())
        self.assertEqual(code, 0, output.getvalue())
        document = json.loads(path.read_text(encoding="utf-8"))
        for action in document["actions"]:
            evidence = action["impact"]["external_action_payload"]["workbuddy_session_evidence"]
            self.assertIn("row", evidence)
            self.assertIsNone(evidence["row"])
            self.assertIsNone(evidence["usage"])
        result = self.coordinator().apply_operation(plan_path=path, clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertTrue(result["modified"])
        verified = self.coordinator().verify_operation(plan_path=path)
        self.assertEqual(verified["goal_status"], "complete", verified)
        self.assertEqual(self.ids(), [SID, OTHER])
        self.assertNotIn(ORPHAN, store.snapshot(self.root)["records"])
        self.assertNotIn(ORPHAN_TWO, store.snapshot(self.root)["records"])

    def test_only_workbuddy_evidence_preserves_nulls_and_still_filters_body_keys(self):
        payload = {"ordinary": {"nullable": None}, "workbuddy_session_evidence": {
            "row": None, "usage": None, "nested": [{"nullable": None, "prompt": SECRET, "body": None}]}}
        cleaned = OperationCoordinator._metadata(payload)
        self.assertEqual(cleaned["ordinary"], {})
        self.assertEqual(cleaned["workbuddy_session_evidence"], {
            "row": None, "usage": None, "nested": [{"nullable": None}]})
        self.assertNotIn(SECRET, json.dumps(cleaned))


class WorkBuddyRuntimeTests(unittest.TestCase):
    def row(self, name, *, command="", executable="D:/Apps/WorkBuddy/bin/runtime.exe", pid=100, parent=10):
        return {"Name": name, "ProcessId": pid, "ParentProcessId": parent,
                "ExecutablePath": executable, "CommandLine": command}

    def test_all_known_real_writers_and_root_based_generic_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in sorted(runtime._WRITER_NAMES | runtime._VENDOR_NAMES):
                result = runtime.probe(root, collector=lambda: [self.row(name)])
                self.assertFalse(result["clients_closed"], name)
            result = runtime.probe(root, collector=lambda: [self.row("python.exe", executable="D:/Python/python.exe",
                                                                command="worker --config " + str(root))])
            self.assertFalse(result["clients_closed"])
            self.assertNotIn("CommandLine", json.dumps(result))
            result = runtime.probe(root, collector=lambda: [self.row("sandbox-cli.exe", executable="D:/other-app/sandbox-cli.exe")])
            self.assertTrue(result["clients_closed"])

    def test_empty_invalid_and_failed_process_probes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(runtime.probe(root, collector=lambda: [])["clients_closed"])
            with self.assertRaises(runtime.WorkBuddyRuntimeError):
                runtime.probe(root, collector=lambda: [{"Name": "node.exe"}])
            with self.assertRaises(runtime.WorkBuddyRuntimeError):
                runtime.probe(root, collector=lambda: [self.row("node.exe", command=None)])
            adapter = WorkBuddyAdapter(profile_root=root)
            with patch.object(runtime, "_processes", side_effect=RuntimeError("timeout")):
                self.assertFalse(adapter.inspect_runtime()["probe_complete"])

    def test_shell_mentions_are_not_writers_and_real_parent_still_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shell = self.row("pwsh.exe", executable="C:/Program Files/PowerShell/7/pwsh.exe",
                command="python -c inspect WorkBuddy --workbuddy-root " + str(root), pid=90)
            self.assertTrue(runtime.probe(root, collector=lambda: [shell])["clients_closed"])
            parent = self.row("WorkBuddy.exe", pid=80)
            shell["ParentProcessId"] = 80
            result = runtime.probe(root, collector=lambda: [shell, parent])
            self.assertFalse(result["clients_closed"])
            self.assertEqual({row["pid"] for row in result["processes"]}, {80, 90})
            grandchild = self.row("generic-helper.exe", executable="D:/Apps/helper.exe", pid=91, parent=90)
            result = runtime.probe(root, collector=lambda: [grandchild, shell, parent])
            self.assertEqual({row["pid"] for row in result["processes"]}, {80, 90, 91})


if __name__ == "__main__":
    unittest.main()
