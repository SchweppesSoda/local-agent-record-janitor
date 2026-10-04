'use strict'
// Strict Chromium 152 idb_cmp1 decoding and ordering. Unknown keys fail closed.
// Does not decode stored values or perform filesystem operations.
const MAX_PART = 1000000, MAX_DEPTH = 64, MAX_ITEMS = 200000;
function bad() { const e = new Error('idb_key_schema_unverified'); e.code = e.message; throw e; }
const cmp = (a,b) => a < b ? -1 : a > b ? 1 : 0;
function cursor(bytes) {
  let at = 0, count = 0;
  function take(n) { if (!Number.isSafeInteger(n) || n < 0 || at + n > bytes.length) bad(); const result = bytes.subarray(at,at+n); at += n; return result; }
  function vi() {
    let n = 0n, shift = 0n;
    for (let i=0; i<10; i++) {
      const b=take(1)[0]; if (i===9 && b>1) bad(); n |= BigInt(b&127)<<shift;
      if (!(b&128)) { if (i>0 && b===0 || n>0x7fffffffffffffffn) bad(); return n; }
      shift+=7n;
    }
    bad();
  }
  function length(multiplier=1) { const n=vi(); if (n>BigInt(MAX_PART)) bad(); return Number(n)*multiplier; }
  function text() { return take(length(2)); }
  function encoded(depth=0) {
    if (depth>MAX_DEPTH || ++count>MAX_ITEMS) bad();
    const type=take(1)[0];
    if (type===0 || type===5) return {type};
    if (type===1) return {type,data:text()};
    if (type===6) return {type,data:take(length())};
    if (type===2 || type===3) { const data=take(8).readDoubleLE(); if (Number.isNaN(data) || Object.is(data,-0) || type===2 && !Number.isFinite(data)) bad(); return {type,data}; }
    if (type===4) { const n=length(); if (n>MAX_ITEMS) bad(); const data=[]; for (let i=0;i<n;i++) data.push(encoded(depth+1)); return {type,data}; }
    bad();
  }
  return { take,vi,text,encoded,remaining:()=>bytes.length-at,end:()=>at===bytes.length };
}
const typeRanks = {5:0,3:1,2:2,1:3,6:4,4:5,0:6};
function compareIDB(a,b) {
  const first=cmp(typeRanks[a.type],typeRanks[b.type]); if (first) return first;
  if (a.type===0 || a.type===5) return 0;
  if (a.type===2 || a.type===3) return cmp(a.data,b.data);
  if (a.type===1 || a.type===6) return Buffer.compare(a.data,b.data);
  for (let i=0;i<Math.min(a.data.length,b.data.length);i++) { const result=compareIDB(a.data[i],b.data[i]); if (result) return result; }
  return cmp(a.data.length,b.data.length);
}
function decode(bytes) {
  const c=cursor(bytes), header=c.take(1)[0];
  function intLE(n) {
    const raw=c.take(n); if (n>1 && raw.at(-1)===0) bad();
    let value=0n; for (let i=0;i<n;i++) value |= BigInt(raw[i])<<BigInt(i*8);
    if (value>0x7fffffffffffffffn) bad(); return value;
  }
  const prefix=[intLE(((header>>5)&7)+1),intLE(((header>>2)&7)+1),intLE((header&3)+1)];
  const [db,store,index]=prefix; if (index>0x7fffffffn) bad(); let suffix;
  const positive=()=>{ const n=c.vi(); if (n===0n) bad(); return n; };
  if (db===0n) {
    if (store!==0n || index!==0n) bad();
    const tag=c.take(1)[0];
    if (tag<=6) suffix=[tag];
    else if (tag===50) { const data=c.take(c.remaining()); if (!data.length) bad(); suffix=[tag,data]; }
    else if (tag===100) suffix=[tag,positive()];
    else if (tag===201) suffix=[tag,c.text(),c.text()];
    else bad();
  } else if (store===0n) {
    if (index!==0n) bad();
    const tag=c.take(1)[0];
    if (tag<=5) suffix=[tag];
    else if (tag===50) { const store=positive(),field=c.take(1)[0]; if (field>7) bad(); suffix=[tag,store,field]; }
    else if (tag===100) { const store=positive(),index=positive(),field=c.take(1)[0]; if (index<30n || field>3) bad(); suffix=[tag,store,index,field]; }
    else if (tag===150) suffix=[tag,positive()];
    else if (tag===151) { const store=positive(),index=positive(); if (index<30n) bad(); suffix=[tag,store,index]; }
    else if (tag===200) suffix=[tag,c.text()];
    else if (tag===201) suffix=[tag,positive(),c.text()];
    else bad();
  } else if (index===1n || index===2n || index===3n) suffix=[c.encoded()];
  else if (index>=30n && index<0x7fffffffn) {
    const first=c.encoded(); let sequence=-1n,primary=null;
    if (!c.end()) sequence=c.vi();
    if (!c.end()) primary=c.encoded();
    suffix=[first,primary,sequence];
  } else bad();
  if (!c.end()) bad();
  return {prefix,suffix};
}
function compare(a,b) {
  const aa=decode(a),bb=decode(b);
  for (let i=0;i<3;i++) { const result=cmp(aa.prefix[i],bb.prefix[i]); if (result) return result; }
  if (aa.prefix[1]!==0n) {
    let result=compareIDB(aa.suffix[0],bb.suffix[0]); if (result) return result;
    if (aa.prefix[2]<30n) return 0;
    if (!aa.suffix[1] || !bb.suffix[1]) return cmp(Number(!!aa.suffix[1]),Number(!!bb.suffix[1]));
    result=compareIDB(aa.suffix[1],bb.suffix[1]); return result || cmp(aa.suffix[2],bb.suffix[2]);
  }
  for (let i=0;i<aa.suffix.length;i++) {
    const a=aa.suffix[i],b=bb.suffix[i];
    const result=Buffer.isBuffer(a) ? Buffer.compare(a,b) : cmp(a,b); if (result) return result;
  }
  return 0;
}
module.exports={compare,decode,compareIDB,cursor};
