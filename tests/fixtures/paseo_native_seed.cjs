'use strict';
// TEMP test-only: direct native IDB; never imports or starts Paseo.
// getpaseo/paseo 05b074764dd1be4b7b04c7ab403ebeb88393aee3:
// packages/app/src/runtime/replica-cache/row-store.web.ts (v1 schema),
// index.ts StoredAgentSchema/StoredTimelineSchema/serializeDirectoryCheckpoint.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const SCHEMA = 'larj.paseo-native-fixture.v1';
const ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const digest = value => crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
function buildRows({serverId='srv_local',selectedId='selected',keepId='keep',otherServerId='srv_remote'}={}) {
  if (![serverId,selectedId,keepId,otherServerId].every(value=>typeof value==='string' && ID.test(value)) || serverId===otherServerId || selectedId===keepId) throw new Error('fixture_scope_invalid');
  const timestamp='2026-10-05T00:00:00.000Z';
  const storedAgent=(id,label)=>({
    snapshot:{id,provider:'codex',cwd:'D:/TEMP/paseo-fixture',model:null,thinkingOptionId:null,
      createdAt:timestamp,updatedAt:timestamp,lastUserMessageAt:timestamp,status:'idle',
      capabilities:{supportsStreaming:true,supportsSessionPersistence:true,supportsDynamicModes:true,
        supportsMcpServers:true,supportsReasoningStream:true,supportsToolInvocations:true},
      currentModeId:null,availableModes:[],pendingPermissions:[],persistence:null,title:label,labels:{}},
    turn:{phase:'idle'},projectPlacement:null,lastActivityAt:timestamp,
  });
  const storedTimeline=(id,label)=>({agentId:id,
    items:[{id:'message-1',kind:'assistant_message',timestamp,
      timelineCursor:{epoch:'synthetic-epoch',seq:1},text:label}],
    range:{epoch:'synthetic-epoch',startSeq:1,endSeq:1},hasOlder:false,
  });
  const rows=[];
  for (const [server,id,label] of [[serverId,selectedId,'TEMP_SELECTED'],[serverId,keepId,'TEMP_KEEP'],[otherServerId,selectedId,'TEMP_REMOTE_SAME_ID']]) {
    rows.push({serverId:server,kind:'agent',id,payload:JSON.stringify(storedAgent(id,label))});
    rows.push({serverId:server,kind:'timeline',id,payload:JSON.stringify(storedTimeline(id,label+'_TIMELINE'))});
  }
  rows.push({serverId,kind:'checkpoint',id:'singleton',payload:JSON.stringify({version:1,cursors:{agents:{generation:'synthetic-generation',afterSeq:1}}})});
  return rows;
}
function validateRequest(request) {
  if (request.schema_version!==SCHEMA || !['seed','inspect'].includes(request.mode)
      || !['normal','auto-increment','legacy','blob'].includes(request.variant??'normal')) throw new Error('fixture_request_invalid');
  const root=path.resolve(request.fixtureRoot);
  if (!path.basename(root).startsWith('larj-paseo-fixture-') || fs.lstatSync(root).isSymbolicLink() || !fs.lstatSync(root).isDirectory()) throw new Error('fixture_root_invalid');
  const rows=buildRows(request);
  return {root,profile:path.join(root,'session'),rows};
}
async function run(request) {
  request={serverId:'srv_local',selectedId:'selected',keepId:'keep',otherServerId:'srv_remote',...request};
  const {root,profile,rows}=validateRequest(request),owner=path.join(root,'fixture-owner.json');
  if (request.mode==='seed') {
    fs.mkdirSync(profile); // Must be absent: cannot overwrite any existing origin.
    fs.writeFileSync(owner,JSON.stringify({schema_version:SCHEMA,rows_sha256:digest(rows)}),{flag:'wx'});
  } else if (JSON.parse(fs.readFileSync(owner,'utf8')).rows_sha256!==digest(rows)) throw new Error('fixture_owner_changed');
  const control=path.join(root,'control-'+crypto.randomUUID());fs.mkdirSync(control);
  const {app,BrowserWindow,protocol,session}=require('electron');
  if (process.versions.electron!=='44.2.0' || process.versions.chrome!=='152.0.7977.76') throw new Error('fixture_runtime_unverified');
  app.setPath('userData',control);
  app.commandLine.appendSwitch('disable-background-networking');
  app.commandLine.appendSwitch('disable-component-update');
  app.commandLine.appendSwitch('disable-sync');
  protocol.registerSchemesAsPrivileged([{scheme:'paseo',privileges:{standard:true,secure:true,supportFetchAPI:true}}]);
  await app.whenReady();
  const storage=session.fromPath(profile,{cache:false});
  storage.webRequest.onBeforeRequest((details,callback)=>callback({cancel:!details.url.startsWith('paseo://app/')}));
  await storage.protocol.handle('paseo',()=>new Response('<!doctype html><title>Synthetic storage</title>',{headers:{'content-type':'text/html','content-security-policy':"default-src 'none'"}}));
  const window=new BrowserWindow({show:false,webPreferences:{session:storage,nodeIntegration:false,contextIsolation:true,sandbox:true}});
  window.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  await window.loadURL('paseo://app/');
  if (request.mode==='seed') await window.webContents.executeJavaScript(`(async()=>{
    const open=(name,upgrade)=>new Promise((resolve,reject)=>{const r=indexedDB.open(name,1);r.onupgradeneeded=()=>upgrade(r.result);r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(new Error('fixture_open_failed'));});
    const write=(db,names,body)=>new Promise((resolve,reject)=>{const tx=db.transaction(names,'readwrite',{durability:'strict'});tx.oncomplete=resolve;tx.onabort=()=>reject(new Error('fixture_write_failed'));body(tx);});
    const db=await open('paseo-replica-row-store',db=>{db.createObjectStore('rows',{keyPath:['serverId','kind','id']});db.createObjectStore('meta');});
    await write(db,['rows','meta'],tx=>{tx.objectStore('meta').put(1,'schema_version');for(const row of ${JSON.stringify(rows)})tx.objectStore('rows').put(row);});db.close();
    const other=await open('unrelated-vault',db=>db.createObjectStore('keep',{keyPath:'id'}));
    await write(other,'keep',tx=>tx.objectStore('keep').put({id:'unselected',private:'TEMP_UNSELECTED_OTHER_DATABASE'}));other.close();
    if (${JSON.stringify(request.variant??'normal')}==='auto-increment') {const extra=await open('unselected-auto',db=>db.createObjectStore('items',{autoIncrement:true}));extra.close();}
    if (${JSON.stringify(request.variant??'normal')}==='legacy') {const extra=await open('paseo-replica-cache',db=>db.createObjectStore('legacy'));await write(extra,'legacy',tx=>tx.objectStore('legacy').put({private:'TEMP_LEGACY'},'legacy'));extra.close();}
    if (${JSON.stringify(request.variant??'normal')}==='blob') {const extra=await open('unselected-blob',db=>db.createObjectStore('blob'));await write(extra,'blob',tx=>tx.objectStore('blob').put(new Blob(['TEMP_BLOB']), 'blob'));extra.close();}
    return true;
  })()`);
  const snapshot=await window.webContents.executeJavaScript(`(async()=>{
    const databases=[];
    for(const definition of (await indexedDB.databases()).sort((a,b)=>a.name<b.name?-1:a.name>b.name?1:0)) {
      const r=indexedDB.open(definition.name);r.onupgradeneeded=()=>r.transaction.abort();
      const db=await new Promise((resolve,reject)=>{r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(new Error('fixture_read_failed'));});
      const stores=[];
      for(const name of [...db.objectStoreNames].sort()) {
        const store=db.transaction(name,'readonly').objectStore(name),records=[];
        const r=store.openCursor();await new Promise((resolve,reject)=>{r.onerror=()=>reject(new Error('fixture_read_failed'));r.onsuccess=()=>{const c=r.result;if(!c)return resolve();records.push({key:c.key,value:c.value});c.continue();};});
        stores.push({name,keyPath:store.keyPath,autoIncrement:store.autoIncrement,
          indexes:[...store.indexNames].sort().map(name=>{const i=store.index(name);return{name,keyPath:i.keyPath,unique:i.unique,multiEntry:i.multiEntry};}),records});
      }
      databases.push({name:db.name,version:db.version,stores});db.close();
    }
    return databases;
  })()`);
  const records=snapshot.find(db=>db.name==='paseo-replica-row-store').stores.find(store=>store.name==='rows').records;
  const selected=row=>row.key[0]===request.serverId && ['agent','timeline'].includes(row.key[1]) && row.key[2]===request.selectedId;
  const unselected=records.filter(row=>!selected(row));
  const schemas=snapshot.map(db=>({...db,stores:db.stores.map(({records,...store})=>store)}));
  const other=snapshot.find(db=>db.name==='unrelated-vault').stores[0].records;
  console.log(JSON.stringify({schema_version:SCHEMA,status:'verified',mode:request.mode,
    runtime:{electron:process.versions.electron,chrome:process.versions.chrome,node:process.versions.node},
    selectedRecords:records.filter(selected).length,unselectedRecords:unselected.length,
    remoteSameIdRecords:records.filter(row=>row.key[0]===request.otherServerId && row.key[2]===request.selectedId).length,
    unselectedRowsSha256:digest(unselected),otherDatabaseSha256:digest(other),schemaSha256:digest(schemas)}));
  await new Promise(resolve=>setTimeout(resolve,300));window.destroy();app.quit();
}
module.exports={buildRows,SCHEMA};
if(process.versions.electron || require.main===module) {
  let request;
  try {request=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));}
  catch(error){console.log(JSON.stringify({status:'blocked',code:'fixture_request_invalid'}));process.exit(1);}
  run(request).catch(error=>{console.log(JSON.stringify({status:'blocked',code:error.message}));require('electron').app.exit(1);});
}
