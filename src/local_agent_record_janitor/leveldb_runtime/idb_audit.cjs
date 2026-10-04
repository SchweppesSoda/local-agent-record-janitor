'use strict'
// Read-only physical audit. Native IDB is never opened; values are only hashed.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const physical=require('./physical.cjs').indexedDB(),idb=require('./idb_keys.cjs');
function fail(code) { const e=new Error(code); e.code=code; throw e; }
function readLogical(root) {
  const summary=physical.validate(root);
  const current=fs.readFileSync(path.join(root,'CURRENT'),'latin1').trim();
  const manifest=physical.manifest(physical.records(fs.readFileSync(path.join(root,current))));
  const entries=[];
  for (const file of manifest.files.values()) {
    const names=fs.readdirSync(root).filter(name=>/^\d+\.(?:ldb|sst)$/.test(name) && BigInt(name.split('.')[0])===file.number);
    if (names.length!==1) fail('idb_live_table_identity_ambiguous');
    for (const entry of physical.sst(fs.readFileSync(path.join(root,names[0]))).entries) {
      entries.push({key:entry.key.subarray(0,-8),sequence:entry.key.readBigUInt64LE(entry.key.length-8)>>8n,
        type:entry.key[entry.key.length-8],value:entry.value});
    }
  }
  for (const name of fs.readdirSync(root).filter(name=>/^\d+\.log$/.test(name))) {
    const number=BigInt(name.slice(0,-4));
    if (number<manifest.log && number!==manifest.prevLog) continue;
    for (const raw of physical.records(fs.readFileSync(path.join(root,name)))) {
      physical.writeBatch(raw);
      const sequence=raw.readBigUInt64LE(0),c=physical.cursor(raw.subarray(12)); let position=0n;
      while (!c.end()) {
        const type=c.byte(),key=c.str(),value=type===1?c.str():Buffer.alloc(0);
        entries.push({sequence:sequence+position++,type,key,value});
      }
    }
  }
  if (entries.length>200000) fail('idb_logical_budget_exceeded');
  entries.sort((a,b)=>idb.compare(a.key,b.key) || (a.sequence===b.sequence?0:a.sequence>b.sequence?-1:1));
  const logical=[];
  for (let i=0;i<entries.length;) {
    const entry=entries[i]; let next=i+1;
    while (next<entries.length && idb.compare(entry.key,entries[next].key)===0) {
      const other=entries[next];
      if (!entry.key.equals(other.key)) fail('idb_comparator_equivalent_noncanonical_keys');
      if (entry.sequence===other.sequence && (entry.type!==other.type || !entry.value.equals(other.value))) fail('idb_sequence_identity_ambiguous');
      next++;
    }
    if (entry.type===1) logical.push({key:entry.key,value:entry.value});
    i=next;
  }
  return {summary,logical};
}
function unsignedInt(raw) {
  if (!raw || !raw.length || raw.length>8 || raw.length>1 && raw.at(-1)===0) fail('idb_metadata_integer_unverified');
  let value=0n; for (let i=0;i<raw.length;i++) value |= BigInt(raw[i])<<BigInt(i*8);
  if (value>0x7fffffffffffffffn) fail('idb_metadata_integer_unverified'); return value;
}
function logicalDigest(logical) {
  const hash=crypto.createHash('sha256'),size=Buffer.alloc(8);
  for (const row of logical) {
    size.writeBigUInt64LE(BigInt(row.key.length)); hash.update(size).update(row.key);
    size.writeBigUInt64LE(BigInt(row.value.length)); hash.update(size).update(row.value);
  }
  return hash.digest('hex');
}
function audit(root,{v8Version=16,blinkVersion=21}={}) {
  const {summary,logical}=readLogical(root),prefix=Buffer.from([0,0,0,0]);
  const metadata=tag=>logical.find(row=>row.key.equals(Buffer.concat([prefix,Buffer.from([tag])])));
  if (unsignedInt(metadata(0)?.value)!==5n) fail('idb_storage_schema_unverified');
  const data=unsignedInt(metadata(2)?.value),v8=Number(data>>32n),blink=Number(data&0xffffffffn);
  if (v8!==v8Version || blink!==blinkVersion) fail('idb_runtime_data_format_incompatible');
  const scopesPrefix=Buffer.concat([prefix,Buffer.from([50])]);
  let global=false;
  for (const row of logical) {
    const key=idb.decode(row.key);
    if (key.prefix[0]===0n && key.suffix[0]===50) {
      if (row.key.equals(Buffer.concat([scopesPrefix,Buffer.from([0])])) && row.value.equals(Buffer.from([8,1]))) global=true;
      else fail('idb_pending_scope_recovery_unverified');
    }
    if (key.prefix[2]===3n || key.prefix[0]===0n && [3,4].includes(key.suffix[0]) && row.value.length) fail('idb_blob_closure_unverified');
  }
  if (!global) fail('idb_scopes_schema_unverified');
  const preserved=logical.filter(row=>!([2,5,6].some(tag=>row.key.equals(Buffer.concat([prefix,Buffer.from([tag])])))));
  const dataRecords=logical.filter(row=>{ const key=idb.decode(row.key); return key.prefix[0]!==0n && key.prefix[1]!==0n && key.prefix[2]===1n; }).length;
  return {...summary,schema_version:'larj.paseo-idb-audit.v1',logicalEntries:logical.length,
    rawLogicalSha256:logicalDigest(logical),preservationSha256:logicalDigest(preserved),dataRecords,
    storageSchema:5,dataFormat:{v8,blink},pendingRecovery:false,blobEntries:false};
}
module.exports={audit,readLogical};
if (require.main===module) {
  try { process.stdout.write(JSON.stringify(audit(process.argv[2]))+'\n'); }
  catch(e) { process.stdout.write(JSON.stringify({status:'blocked',code:e.code??'idb_audit_failed'})+'\n'); process.exitCode=1; }
}
