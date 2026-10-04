'use strict'
// Pure synthetic, independently encoded counterexamples; no native store reads.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const f=require('./office_leveldb_physical.cjs');
const a=require('../../src/local_agent_record_janitor/leveldb_runtime/idb_audit.cjs');
const idb=require('../../src/local_agent_record_janitor/leveldb_runtime/idb_keys.cjs');
function text(value) { const raw=Buffer.from(value,'utf16le'); raw.swap16(); return Buffer.concat([f.vi(value.length),raw]); }
function user(value) { return Buffer.concat([Buffer.from([0,1,1,1,1]),text(value)]); }
function meta(tag) { return Buffer.from([0,0,0,0,tag]); }
function int(value) { value=BigInt(value); const bytes=[]; do { bytes.push(Number(value&255n)); value>>=8n; } while (value); return Buffer.from(bytes); }
const names=['valid','wrong_bytewise_order','separator_before_last','separator_crosses_next','bloom_false_negative','wal_bad_crc','wal_malformed_key_crc_correct','manifest_add_then_delete','pending_scope','format_16_with_runtime_15','format_17_with_runtime_16'];
function build(name) {
  if (!names.includes(name)) throw new Error('fixture_name_invalid');
  const aa={key:f.internalKey(user('aa'),1n),value:Buffer.from('SYNTHETIC_AA')};
  const b={key:f.internalKey(user('b'),2n),value:Buffer.from('SYNTHETIC_B')};
  let blocks=[[aa],[b]],separators=null,bloom=null;
  if (name==='wrong_bytewise_order') blocks=[[b],[aa]];
  if (name==='separator_before_last') { blocks=[[aa,b]]; separators=[f.internalKey(user('a'))]; }
  if (name==='separator_crosses_next') separators=[f.internalKey(user('z')),b.key];
  if (name==='bloom_false_negative') bloom=f.oneFilter(false);
  const table=f.table(blocks,separators,bloom);
  const operations=[{key:meta(0),value:int(5)}, {key:meta(2),value:int((BigInt(name==='format_17_with_runtime_16'?17:16)<<32n)|21n)},
    {key:Buffer.from([0,0,0,0,50,0]),value:Buffer.from([8,1])}];
  if (name==='pending_scope') operations.push({key:Buffer.from([0,0,0,0,50,1,0]),value:Buffer.alloc(0)});
  if (name==='wal_malformed_key_crc_correct') operations.push({key:Buffer.from([0,1,1,1,4,2,1,1,0,97]),value:Buffer.from('SYNTHETIC_INVALID')});
  const batch=f.writeBatch(3n,operations); let wal=f.logRecords([batch]);
  if (name==='wal_bad_crc') { wal=Buffer.from(wal); wal[0]^=1; }
  const head=Buffer.concat([f.vi(1),f.str('idb_cmp1'),f.vi(2),f.vi(2),f.vi(9),f.vi(0),f.vi(3),f.vi(4),f.vi(4),f.vi(2)]);
  let manifest=Buffer.concat([head,f.addFile(table.bytes,table.firstKey,table.lastKey)]);
  if (name==='manifest_add_then_delete') manifest=Buffer.concat([manifest,f.deleteFile()]);
  return {files:new Map([['CURRENT',Buffer.from('MANIFEST-000001\n')],['MANIFEST-000001',f.logRecords([manifest])],['000002.log',wal],['000003.ldb',table.bytes]]),
    runtime:{v8Version:name==='format_16_with_runtime_15'?15:16,blinkVersion:21}};
}
function run() {
  const results=[];
  for (const name of names) {
    const fixture=build(name),root=fs.mkdtempSync(path.join(os.tmpdir(),'larj-idb-audit-'));
    try {
      for (const [name,raw] of fixture.files) fs.writeFileSync(path.join(root,name),raw,{flag:'wx'});
      let actual='accept',code;
      try { a.audit(root,fixture.runtime); } catch(e) { actual='reject'; code=e.code??e.message; }
      results.push({name,actual,expected:name==='valid'?'accept':'reject',code});
    } finally { fs.rmSync(root,{recursive:true,force:true}); }
  }
  const comparison={rawBytesWrongForIDB:Buffer.compare(user('aa'),user('b'))>0,strictIDBOrder:idb.compare(user('aa'),user('b'))<0};
  return {schema:'larj.paseo-idb-audit-test.v1',comparison,results,passed:results.every(row=>row.actual===row.expected)&&Object.values(comparison).every(Boolean)};
}
module.exports={build,names,run};
if (require.main===module) { const result=run(); console.log(JSON.stringify(result)); if (!result.passed) process.exitCode=1; }
