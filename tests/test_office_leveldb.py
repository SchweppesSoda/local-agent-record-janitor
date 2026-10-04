from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor import office_leveldb as drafts

PRIVATE = "PRIVATE_OFFICE_DRAFT_SENTINEL"
RELATIVE = "Partitions/main/Local Storage/leveldb"


class OfficeLevelDBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.node, cls.dependency, cls.helper = drafts.runtime()
        except drafts.OfficeLevelDBError:
            if os.environ.get("LARJ_REQUIRE_LEVELDB_TESTS") == "1":
                raise
            raise unittest.SkipTest("Pinned office LevelDB runtime is not installed")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.db = self.root / RELATIVE
        self.db.parent.mkdir(parents=True)

    def node_script(self, script, *arguments):
        result = subprocess.run([str(self.node), "-e", script, str(self.helper), str(self.db), *arguments],
            cwd=self.dependency, env=drafts._environment(self.dependency), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertNotIn(PRIVATE.encode(), result.stdout)
        return result.stdout

    def fixture(self, *, duplicate=False):
        self.node_script(r'''
const path=require('node:path');
const runtime=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const {ClassicLevel}=runtime('classic-level'), h=require(process.argv[1]);
const prefix=Buffer.from('_file://\0');
const key=n=>Buffer.concat([prefix,h.encodeString(n)]);
const raw=h.encodeString(JSON.stringify({'chat-1:child-1':{text:'PRIVATE_OFFICE_DRAFT_SENTINEL 中文😀'},'chat-2:child-2':{text:'KEEP 私密😀',n:123}}));
const db=new ClassicLevel(process.argv[2],{keyEncoding:'buffer',valueEncoding:'buffer',compression:true});
(async()=>{
await db.open(); await db.batch([
{type:'put',key:Buffer.from('VERSION'),value:Buffer.from('1')},
{type:'put',key:key('agent-drafts-global'),value:raw},
{type:'put',key:key('agent-input-history'),value:h.encodeString('["PRESERVE_HISTORY"]')},
{type:'put',key:key('opaque-auth'),value:Buffer.from('PRESERVE_CREDENTIAL')},
{type:'put',key:Buffer.from('META:file://'),value:Buffer.concat([Buffer.from([8]),h.varint(13000000000000000n),Buffer.from([16]),h.varint(BigInt(raw.length+1000))])}
],{sync:true});
if(process.argv[3]==='duplicate') await db.put(Buffer.concat([prefix,h.encodeString('agent-drafts-global',0)]),raw,{sync:true});
await db.compactRange(Buffer.from(''),Buffer.from([255]),{keyEncoding:'buffer'}); await db.close();
})().catch(e=>{console.error(e.code||'fixture_error');process.exitCode=1});
''', "duplicate" if duplicate else "normal")

    def hashes(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.db.iterdir()}

    def test_native_ui_migrations_exact_key_deletion_and_same_origin_metadata_batch(self):
        self.fixture()
        self.node_script(r'''
const path=require('node:path'), runtime=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const {ClassicLevel}=runtime('classic-level'),h=require(process.argv[1]);
const key=n=>Buffer.concat([Buffer.from('_file://\0'),h.encodeString(n)]);
const values={
 'agent-pinned-chats-account':['chat-1','chat-2'],
 'agent-pinned-chats-account2':['chat-1'],
 'agent-active-sub-chats-chat-1':'child-1',
 '1:agent-open-sub-chats-chat-1':['child-1'],
 'main:agent-pinned-sub-chats-chat-1':['child-1'],
 'main:agent-open-sub-chats-chat-2':['child-2'],
 'workbench-layout:child-1':{tabs:[]},'panel-layout:child-1':{legacy:true},
 'panel-layout:states':{'child-1':{old:true},'child-2':{keep:true}},
 'chatInput:contextSelections':{'child-1':{private:'PRIVATE_OFFICE_DRAFT_SENTINEL'},'child-2':{keep:true}},
 'agents:app-view':{type:'chat',chatId:'chat-1'},
 'agents:subChatUnseenChanges':['child-1','child-2'],
 'agents:previewPaths':{'chat-1':'PRESERVE_OPAQUE_OWNER'},
 'chatInput:modelSettings':{'chat-1':'PRESERVE_MODEL_SCOPE'}
};
const db=new ClassicLevel(process.argv[2],{keyEncoding:'buffer',valueEncoding:'buffer',createIfMissing:false});
(async()=>{await db.open();await db.batch(Object.entries(values).map(([n,v])=>({type:'put',key:key(n),value:h.encodeString(JSON.stringify(v))})),{sync:true});
await db.put(Buffer.from('META:file://'),Buffer.concat([Buffer.from([8]),h.varint(13000000000000000n),Buffer.from([16]),h.varint(100000n)]));await db.close()})().catch(()=>process.exitCode=1);
''')
        evidence = drafts.freeze(self.root, RELATIVE, ["chat-1"], sub_chat_ids=["child-1"])
        self.assertNotIn(PRIVATE, json.dumps(evidence))
        # Every same-origin edit binds ONE aggregated META after value.
        self.assertEqual(len({item["after_metadata_sha256"] for item in evidence["observation"]["edits"]}), 1)
        drafts.apply(evidence, phase_callback=lambda _: None)
        self.assertEqual(drafts.remaining(evidence), 0)
        self.node_script(r'''
const path=require('node:path'),a=require('node:assert/strict'),runtime=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const {ClassicLevel}=runtime('classic-level'),h=require(process.argv[1]);const key=n=>Buffer.concat([Buffer.from('_file://\0'),h.encodeString(n)]);
const db=new ClassicLevel(process.argv[2],{keyEncoding:'buffer',valueEncoding:'buffer',createIfMissing:false});
(async()=>{await db.open();for(const n of ['agent-active-sub-chats-chat-1','1:agent-open-sub-chats-chat-1','main:agent-pinned-sub-chats-chat-1','workbench-layout:child-1','panel-layout:child-1','agents:app-view'])a.equal(await db.get(key(n)),undefined);
const read=async n=>JSON.parse(h.decodeString(await db.get(key(n))));
a.deepEqual(await read('agent-pinned-chats-account'),['chat-2']);a.deepEqual(await read('agent-pinned-chats-account2'),[]);
a.deepEqual(await read('main:agent-open-sub-chats-chat-2'),['child-2']);a.deepEqual(await read('panel-layout:states'),{'child-2':{keep:true}});
a.deepEqual(await read('chatInput:contextSelections'),{'child-2':{keep:true}});a.deepEqual(await read('agents:subChatUnseenChanges'),['child-2']);
a.deepEqual(await read('agents:previewPaths'),{'chat-1':'PRESERVE_OPAQUE_OWNER'});a.deepEqual(await read('chatInput:modelSettings'),{'chat-1':'PRESERVE_MODEL_SCOPE'});
await db.close()})().catch(()=>process.exitCode=1);
''')

    def test_retained_raw_numbers_unicode_escapes_and_duplicate_keys(self):
        self.node_script(r'''
const h=require(process.argv[1]),a=require('node:assert/strict');
const raw=h.encodeString('{"chat-1:child-1":1,"chat-2:child-2":{"n":9007199254740993,"e":1e+09,"s":"\\u4e2d"}}');
const out=h.decodeString(h.cleanDraft(raw,new Set(['chat-1'])).after);
a.equal(out,'{"chat-2:child-2":{"n":9007199254740993,"e":1e+09,"s":"\\u4e2d"}}');
a.throws(()=>h.mapEntries('{"a":1,"\\u0061":2}'));
''')

    def test_non_draft_drift_after_selected_absence_is_unknown(self):
        self.fixture()
        evidence = drafts.freeze(self.root, RELATIVE, ["chat-1"])
        drafts.apply(evidence, phase_callback=lambda _: None)
        self.node_script(r'''
const path=require('node:path'),runtime=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const {ClassicLevel}=runtime('classic-level'),h=require(process.argv[1]);
const db=new ClassicLevel(process.argv[2],{keyEncoding:'buffer',valueEncoding:'buffer',createIfMissing:false});
(async()=>{await db.open();await db.put(Buffer.concat([Buffer.from('_file://\0'),h.encodeString('opaque-auth')]),Buffer.from('outside drift'));await db.close()})().catch(()=>process.exitCode=1);
''')
        with self.assertRaisesRegex(drafts.OfficeLevelDBError, "after_state_unverified"):
            drafts.remaining(evidence)
        self.assertEqual(drafts.remaining(evidence, terminal_verified=True), 0)

    def test_runtime_same_version_code_change_refuses_before_checkpoint(self):
        self.fixture()
        copy = self.root / "runtime"
        copy.mkdir()
        shutil.copytree(self.dependency / "node_modules", copy / "node_modules")
        shutil.copyfile(self.dependency / "package.json", copy / "package.json")
        with patch.dict(os.environ, {"LARJ_LEVELDB_RUNTIME": str(copy)}):
            evidence = drafts.freeze(self.root, RELATIVE, ["chat-1"])
            path = copy / "node_modules/classic-level/index.js"
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n// synthetic code drift, same package version\n")
            phases = []
            with self.assertRaisesRegex(drafts.OfficeLevelDBError, "runtime_changed"):
                drafts.apply(evidence, phase_callback=phases.append)
            self.assertEqual(phases, [])

    def test_malformed_helper_success_is_not_trusted(self):
        from types import SimpleNamespace
        value = {"schema_version": drafts.PROTOCOL, "status": "verified", "remaining_pairs": 0}
        with patch.object(drafts.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())):
            with patch.object(drafts, "runtime", return_value=(self.node, self.dependency, self.helper)):
                with self.assertRaisesRegex(drafts.OfficeLevelDBError, "response_invalid"):
                    drafts._call({"operation": "apply"})

    def test_crc_correct_but_semantically_corrupt_leveldb_never_reaches_native_open(self):
        fixture = Path(__file__).parent / "fixtures/office_leveldb_physical.cjs"
        self.node_script(r'''
const fs=require('node:fs'),path=require('node:path'),a=require('node:assert/strict');
const g=require(process.argv[3]),p=require(path.join(path.dirname(process.argv[1]),'physical.cjs'));
for(const name of g.names){const f=g.buildCase(name),root=path.join(path.dirname(process.argv[2]),name);fs.mkdirSync(root);
for(const [name,raw] of f.files)fs.writeFileSync(path.join(root,name),raw,{flag:'wx'});
if(name==='valid')p.validate(root);else a.throws(()=>p.validate(root));}
''', str(fixture.resolve()))

    def test_copy_only_plan_and_full_apply_preserve_unselected_keys(self):
        self.fixture()
        before = self.hashes()
        evidence = drafts.freeze(self.root, RELATIVE, ["chat-1"])
        self.assertEqual(self.hashes(), before)
        self.assertNotIn(PRIVATE, json.dumps(evidence))
        self.assertEqual(evidence["observation"]["count"]["removed_pairs"], 1)
        phases = []
        result = drafts.apply(evidence, phase_callback=phases.append)
        self.assertEqual(phases, ["mutation_started"])
        self.assertEqual(result["remaining_pairs"], 0)
        self.assertEqual(drafts.remaining(evidence), 0)
        self.node_script(r'''
const path=require('node:path'), assert=require('node:assert/strict');
const runtime=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const {ClassicLevel}=runtime('classic-level'),h=require(process.argv[1]);
const key=n=>Buffer.concat([Buffer.from('_file://\0'),h.encodeString(n)]);
const db=new ClassicLevel(process.argv[2],{keyEncoding:'buffer',valueEncoding:'buffer',createIfMissing:false});
(async()=>{await db.open();
const map=JSON.parse(h.decodeString(await db.get(key('agent-drafts-global'))));
assert.deepEqual(Object.keys(map),['chat-2:child-2']);assert.equal(map['chat-2:child-2'].text,'KEEP 私密😀');
assert.equal(h.decodeString(await db.get(key('agent-input-history'))),'["PRESERVE_HISTORY"]');
assert.equal((await db.get(key('opaque-auth'))).toString(),'PRESERVE_CREDENTIAL');
await db.close()})().catch(e=>{console.error(e.code||'verification_error');process.exitCode=1});
''')

    def test_hash_drift_never_emits_mutation_checkpoint(self):
        self.fixture()
        evidence = drafts.freeze(self.root, RELATIVE, ["chat-1"])
        (self.db / "LOG").write_bytes(b"outside writer drift")
        phases = []
        with self.assertRaises(drafts.OfficeLevelDBError):
            drafts.apply(evidence, phase_callback=phases.append)
        self.assertEqual(phases, [])

    @unittest.skipUnless(os.name == "nt", "Windows namespace fence")
    def test_live_open_keeps_profile_and_leveldb_directory_identity_locked(self):
        self.fixture()
        evidence = drafts.freeze(self.root, RELATIVE, ["chat-1"])
        attempts = []
        def checkpoint(phase):
            with self.assertRaises(OSError):
                self.db.rename(self.root / "outside")
            with self.assertRaises(OSError):
                (self.root / "Partitions").rename(self.root / "replaced-partitions")
            attempts.append(phase)
        result = drafts.apply(evidence, phase_callback=checkpoint)
        self.assertEqual(attempts, ["mutation_started"])
        self.assertEqual(result["remaining_pairs"], 0)

    def test_checksum_corruption_is_rejected_on_private_copy_without_changing_original(self):
        self.fixture()
        table = next(self.db.glob("*.ldb"))
        content = bytearray(table.read_bytes())
        content[10] ^= 1
        table.write_bytes(content)
        before = self.hashes()
        with self.assertRaises(drafts.OfficeLevelDBError):
            drafts.freeze(self.root, RELATIVE, ["chat-1"])
        self.assertEqual(self.hashes(), before)

    def test_duplicate_chromium_encoding_never_selects_one_winner(self):
        self.fixture(duplicate=True)
        with self.assertRaisesRegex(drafts.OfficeLevelDBError, "duplicate_draft_key"):
            drafts.freeze(self.root, RELATIVE, ["chat-1"])

    def test_absence_is_frozen_and_new_database_is_rejected(self):
        evidence = drafts.freeze(self.root, RELATIVE, ["chat-1"])
        self.assertIsNone(evidence["files"])
        self.assertEqual(drafts.remaining(evidence), 0)
        self.fixture()
        phases = []
        with self.assertRaises(drafts.OfficeLevelDBError):
            drafts.apply(evidence, phase_callback=phases.append)
        self.assertEqual(phases, [])

    def test_unknown_recovery_needs_exact_after_not_just_missing_selected_pairs(self):
        self.fixture()
        evidence = drafts.freeze(self.root, RELATIVE, ["chat-1"])
        drafts.apply(evidence, phase_callback=lambda phase: None)
        self.node_script(r'''
const path=require('node:path');const r=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const {ClassicLevel}=r('classic-level'),h=require(process.argv[1]);
const db=new ClassicLevel(process.argv[2],{keyEncoding:'buffer',valueEncoding:'buffer',createIfMissing:false});
(async()=>{await db.open();await db.put(Buffer.concat([Buffer.from('_file://\0'),h.encodeString('agent-drafts-global')]),h.encodeString('{}'),{sync:true});await db.close()})().catch(()=>{process.exitCode=1});
''')
        with self.assertRaisesRegex(drafts.OfficeLevelDBError, "after_state_unverified"):
            drafts.remaining(evidence)
        self.assertEqual(drafts.remaining(evidence, terminal_verified=True), 0)
