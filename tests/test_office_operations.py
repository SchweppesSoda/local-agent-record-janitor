from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.office_store import OfficeAdapter
from local_agent_record_janitor import office_cleanup, office_files, office_leveldb

SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
SECRET = "PRIVATE_OFFICE_BODY_MUST_NOT_ESCAPE"


class OfficeOperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.profile, self.sdk = self.base / "profile", self.base / "sdk"
        self.path = self.base / "plan.json"
        self.client = "qwenwork"
        (self.profile / "data").mkdir(parents=True)
        with closing(sqlite3.connect(self.profile / "data/agents.db")) as db:
            schema = (Path(__file__).parent / "fixtures/qwenwork_cn_schema.sql").read_text(encoding="utf-8")
            db.executescript("BEGIN;\n" + schema + "\nCOMMIT;")
            db.execute("INSERT INTO projects(id,name,path) VALUES ('project',?,?)", (SECRET, str(self.base / "documents")))
            for name, sdk in (("selected", SID), ("other", OTHER)):
                db.execute("INSERT INTO chats(id,project_id,name) VALUES (?,'project',?)", (name, SECRET))
                db.execute("INSERT INTO sub_chats(id,chat_id,session_id) VALUES (?,?,?)", (name + "-child", name, sdk))
                db.execute("INSERT INTO messages(id,message_id,chat_id,sub_chat_id,sequence,role,parts,searchable_text) VALUES (?,?,?,?,1,'user',?,?)",
                    ("message-" + name, "native-" + name, name, name + "-child", SECRET, SECRET))
                transcript = self.sdk / "projects/bucket" / (sdk + ".jsonl")
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text(SECRET, encoding="utf-8")
            db.commit()
        for patcher in (patch("pathlib.Path.home", return_value=self.base), patch.dict(os.environ,
            {"APPDATA": str(self.base / "config"), "XDG_CONFIG_HOME": str(self.base / "config"), "ORCA_USER_DATA_PATH": ""})):
            patcher.start(); self.addCleanup(patcher.stop)

    def coordinator(self, inspector=None):
        return OperationCoordinator(CleanupService(client_inspector=inspector or (lambda _: ())))

    def plan(self):
        adapter = OfficeAdapter(client=self.client, profile_root=self.profile, sdk_root=self.sdk)
        result = self.coordinator().plan_operation(client=self.client, adapters=(adapter,), record_ids=("selected",), plan_path=self.path)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["goal_status"], "ready", result)
        return result

    def ids(self):
        with closing(sqlite3.connect(self.profile / "data/agents.db")) as db:
            return [row[0] for row in db.execute("SELECT id FROM chats ORDER BY id")]

    def test_plan_cold_apply_status_verify_complete_across_profile_and_sdk_roots(self):
        plan = self.plan()
        self.assertEqual([b["mutation_family"] for b in plan["child_batches"]], ["delete_office_artifacts", "delete_office_frontend"])
        result = self.coordinator().apply_operation(plan_path=self.path, clients_closed=True, plan_sha256=plan["plan_sha256"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertEqual(self.ids(), ["other"])
        self.assertFalse((self.sdk / f"projects/bucket/{SID}.jsonl").exists())
        self.assertTrue((self.sdk / f"projects/bucket/{OTHER}.jsonl").exists())
        for verb in ("status_operation", "verify_operation"):
            value = getattr(self.coordinator(), verb)(operation_id=plan["operation_id"], plan_path=self.path)
            self.assertEqual(value["goal_status"], "complete", value)

    def test_sdk_failure_does_not_release_profile_and_is_not_retried(self):
        plan = self.plan()
        original = office_files.frozen_files.apply_remove
        def fail(root, item):
            if root == self.sdk:
                raise OSError("synthetic SDK interruption")
            return original(root, item)
        with patch.object(office_files.frozen_files, "apply_remove", fail):
            result = self.coordinator().apply_operation(plan_path=self.path, clients_closed=True, plan_sha256=plan["plan_sha256"])
        self.assertEqual(result["goal_status"], "unknown", result)
        self.assertEqual(self.ids(), ["other", "selected"])
        with patch.object(office_cleanup, "execute", side_effect=AssertionError("must not retry")):
            result = self.coordinator().apply_operation(plan_path=self.path, clients_closed=True, plan_sha256=plan["plan_sha256"])
        self.assertEqual(result["goal_status"], "unknown", result)

    def test_running_client_refuses_before_removing_native_files(self):
        plan = self.plan()
        result = self.coordinator(lambda _: ("Office process",)).apply_operation(plan_path=self.path,
            clients_closed=True, plan_sha256=plan["plan_sha256"])
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["mutation_started"])
        self.assertEqual(self.ids(), ["other", "selected"])
        self.assertTrue((self.sdk / f"projects/bucket/{SID}.jsonl").exists())

    def test_unknown_profile_operation_blocks_before_sdk_mutation(self):
        from tests.test_mutation_guard import basic_action, write_journal
        plan = self.plan()
        write_journal(self.profile, "earlier-unknown", basic_action(self.profile))
        result = self.coordinator().apply_operation(plan_path=self.path,
            clients_closed=True, plan_sha256=plan["plan_sha256"])
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["mutation_started"])
        self.assertTrue((self.sdk / f"projects/bucket/{SID}.jsonl").exists())
        self.assertEqual(self.ids(), ["other", "selected"])

    def test_conflicting_sdk_bindings_do_not_silently_select_one_root(self):
        adapters = tuple(OfficeAdapter(client=self.client, profile_root=self.profile, sdk_root=root)
            for root in (self.sdk, self.base / "other-sdk"))
        result = self.coordinator().plan_operation(client=self.client, adapters=adapters,
            record_ids=("selected",), plan_path=self.path)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertIn("office_profile_sdk_binding_conflict", json.dumps(result))

    def peer(self, sdk_id, child_id="peer-child"):
        profile = self.base / "custom-peer"
        (profile / "data").mkdir(parents=True)
        with closing(sqlite3.connect(profile / "data/agents.db")) as db:
            schema = (Path(__file__).parent / "fixtures/qwenwork_cn_schema.sql").read_text(encoding="utf-8")
            db.executescript("BEGIN;\n" + schema + "\nCOMMIT;")
            db.execute("INSERT INTO projects(id,name,path) VALUES ('project','project',?)", (str(self.base / "documents"),))
            db.execute("INSERT INTO chats(id,project_id,name) VALUES ('peer','project',?)", (SECRET,))
            db.execute("INSERT INTO sub_chats(id,chat_id,session_id) VALUES (?,'peer',?)", (child_id, sdk_id))
            db.commit()
        (self.sdk / f"projects/bucket/{sdk_id}.jsonl").write_text(SECRET)
        return profile

    def test_two_custom_profiles_share_sdk_and_accept_only_verified_peer_transition(self):
        self.two_profiles()

    def test_cold_resume_skips_verified_peer_and_sdk_after_pre_mutation_block(self):
        self.two_profiles(transient=True)

    def two_profiles(self, transient=False):
        peer_id = "33333333-3333-4333-8333-333333333333"
        profile = self.peer(peer_id)
        adapters = tuple(OfficeAdapter(client=self.client, profile_root=p, sdk_root=self.sdk)
                         for p in (self.profile, profile))
        plan = self.coordinator().plan_operation(client=self.client, adapters=adapters,
            record_ids=("selected", "peer"), plan_path=self.path)
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual(len(plan["child_batches"]), 4)
        if transient:
            from local_agent_record_janitor.office_database import OfficeDatabaseError
            root = next(s["path"] for s in plan["storages"] if s["storage_id"] == plan["child_batches"][-1]["storage_id"])
            original = office_cleanup.execute
            def block_last(evidence, **kwargs):
                if evidence["role"] == "profile" and evidence["profile_root"] == root:
                    raise OfficeDatabaseError("office_writer_running")
                return original(evidence, **kwargs)
            with patch.object(office_cleanup, "execute", block_last):
                value = self.coordinator().apply_operation(plan_path=self.path,
                    clients_closed=True, plan_sha256=plan["plan_sha256"])
            self.assertEqual(value["goal_status"], "blocked", value)
            self.assertTrue(value["mutation_started"])
        result = self.coordinator().apply_operation(plan_path=self.path,
            clients_closed=True, plan_sha256=plan["plan_sha256"])
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertEqual(self.ids(), ["other"])
        with closing(sqlite3.connect(profile / "data/agents.db")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM chats").fetchone()[0], 0)
        self.assertFalse((self.sdk / f"projects/bucket/{peer_id}.jsonl").exists())
        result = self.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=self.path)
        self.assertEqual(result["goal_status"], "complete", result)

    def test_shared_sdk_uuid_in_custom_peer_is_blocked_before_any_delete(self):
        profile = self.peer(SID)
        adapters = tuple(OfficeAdapter(client=self.client, profile_root=p, sdk_root=self.sdk)
                         for p in (self.profile, profile))
        plan = self.coordinator().plan_operation(client=self.client, adapters=adapters,
            record_ids=("selected",), plan_path=self.path)
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertIn("sdk_session_shared_across_profiles", json.dumps(plan))
        self.assertTrue((self.sdk / f"projects/bucket/{SID}.jsonl").exists())

    def test_shared_sanitized_child_cache_in_custom_peer_is_blocked(self):
        profile = self.peer("33333333-3333-4333-8333-333333333333", child_id="SELECTED-CHILD")
        adapters = tuple(OfficeAdapter(client=self.client, profile_root=p, sdk_root=self.sdk)
                         for p in (self.profile, profile))
        plan = self.coordinator().plan_operation(client=self.client, adapters=adapters,
            record_ids=("selected",), plan_path=self.path)
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertIn("session_cache_shared_across_profiles", json.dumps(plan))

    def test_nonempty_leveldb_and_database_cold_apply_then_verify(self):
        try:
            node, dependency, helper = office_leveldb.runtime()
        except office_leveldb.OfficeLevelDBError:
            if os.environ.get("LARJ_REQUIRE_LEVELDB_TESTS") == "1":
                raise
            self.skipTest("Pinned Office LevelDB runtime is not installed")
        leveldb = self.profile / "Partitions/main/Local Storage/leveldb"
        leveldb.parent.mkdir(parents=True)
        script = r'''
const path=require('node:path'),assert=require('node:assert/strict');
const runtime=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const {ClassicLevel}=runtime('classic-level'),h=require(process.argv[1]);
const key=n=>Buffer.concat([Buffer.from('_file://\0'),h.encodeString(n)]);
const db=new ClassicLevel(process.argv[2],{keyEncoding:'buffer',valueEncoding:'buffer'});
(async()=>{await db.open();
if(process.argv[3]==='write')await db.batch([
{type:'put',key:Buffer.from('VERSION'),value:Buffer.from('1')},
{type:'put',key:key('agent-drafts-global'),value:h.encodeString(JSON.stringify({'selected:selected-child':{text:'PRIVATE_OFFICE_BODY_MUST_NOT_ESCAPE'},'other:other-child':{text:'keep'}}))},
{type:'put',key:key('agent-pinned-chats-account'),value:h.encodeString('["selected","other"]')},
{type:'put',key:key('opaque-auth'),value:Buffer.from('PRESERVE_CREDENTIAL')},
{type:'put',key:Buffer.from('META:file://'),value:Buffer.concat([Buffer.from([8]),h.varint(13000000000000000n),Buffer.from([16]),h.varint(100000n)])}
],{sync:true});
else {const read=async n=>JSON.parse(h.decodeString(await db.get(key(n))));
assert.deepEqual(await read('agent-drafts-global'),{'other:other-child':{text:'keep'}});
assert.deepEqual(await read('agent-pinned-chats-account'),['other']);
assert.equal((await db.get(key('opaque-auth'))).toString(),'PRESERVE_CREDENTIAL');}
await db.close()})().catch(()=>process.exitCode=1);
'''
        def run(mode):
            value = subprocess.run([str(node), "-e", script, str(helper), str(leveldb), mode],
                cwd=dependency, env=office_leveldb._environment(dependency), capture_output=True, timeout=30)
            self.assertEqual(value.returncode, 0, value.stderr.decode(errors="replace"))
        run("write")
        before = {p.name: p.read_bytes() for p in leveldb.iterdir()}
        plan = self.plan()
        self.assertEqual({p.name: p.read_bytes() for p in leveldb.iterdir()}, before)
        result = self.coordinator().apply_operation(plan_path=self.path,
            clients_closed=True, plan_sha256=plan["plan_sha256"])
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertEqual(self.ids(), ["other"])
        self.assertFalse((self.sdk / f"projects/bucket/{SID}.jsonl").exists())
        self.assertTrue((self.sdk / f"projects/bucket/{OTHER}.jsonl").exists())
        for verb in ("status_operation", "verify_operation"):
            value = getattr(self.coordinator(), verb)(operation_id=plan["operation_id"], plan_path=self.path)
            self.assertEqual(value["goal_status"], "complete", value)
            self.assertNotIn(SECRET, json.dumps(value))
        run("read")


if __name__ == "__main__":
    unittest.main()
