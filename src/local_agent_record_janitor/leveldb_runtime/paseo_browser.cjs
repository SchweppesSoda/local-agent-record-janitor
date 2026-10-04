// Isolated private-copy browser helper; never imports or boots Paseo.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { app, BrowserWindow, protocol, session } = require('electron');
const request = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
if (!['inspect', 'apply'].includes(request.mode)) throw new Error('paseo_browser_mode_invalid');
if (process.versions.electron !== '44.2.0' || process.versions.chrome !== '152.0.7977.76') throw new Error('paseo_browser_runtime_unverified');
let mutationStarted = false;
app.setPath('userData', request.controlPath);
app.commandLine.appendSwitch('disable-background-networking');
app.commandLine.appendSwitch('disable-component-update');
app.commandLine.appendSwitch('disable-sync');
protocol.registerSchemesAsPrivileged([{ scheme: 'paseo', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
const fullSnapshot = async function () {
  let items=0;
  const checkJson = (value,seen=new Set(),depth=0) => {
    if (++items>200000 || depth>64) throw new Error('idb_snapshot_budget_exceeded');
    if (value===null || typeof value==='string' || typeof value==='boolean') return;
    if (typeof value==='number') { if (!Number.isFinite(value) || Object.is(value,-0)) throw new Error('idb_value_codec_unverified'); return; }
    if (!value || typeof value!=='object' || seen.has(value)) throw new Error('idb_value_codec_unverified');
    if (!Array.isArray(value) && Object.getPrototypeOf(value)!==Object.prototype) throw new Error('idb_value_codec_unverified');
    seen.add(value);
    if (Array.isArray(value)) {
      if (Object.keys(value).length!==value.length) throw new Error('idb_value_codec_unverified');
      for (let i=0;i<value.length;i++) { if (!(i in value)) throw new Error('idb_value_codec_unverified'); checkJson(value[i],seen,depth+1); }
    } else for (const key of Object.keys(value)) checkJson(value[key],seen,depth+1);
    // Plain JSON trees have neither cycles nor repeated object references.
    // Retain seen nodes so JSON.stringify cannot erase shared-reference identity.
  };
  const requestResult = request => new Promise((resolve,reject) => { request.onsuccess = () => resolve(request.result); request.onerror = () => reject(new Error('idb_request_failed')); });
  const definitions = (await indexedDB.databases()).sort((a,b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  const databases = [];
  for (const definition of definitions) {
    const opening = indexedDB.open(definition.name);
    opening.onupgradeneeded = () => opening.transaction.abort();
    const db = await requestResult(opening), stores = [];
    for (const name of [...db.objectStoreNames].sort()) {
      const tx = db.transaction(name, 'readonly'), store = tx.objectStore(name);
      if (store.autoIncrement) throw new Error('idb_key_generator_unqualified');
      const indexes = [...store.indexNames].sort().map(name => { const index = store.index(name); return { name, keyPath: index.keyPath, unique: index.unique, multiEntry: index.multiEntry }; });
      const records = [];
      const cursor = store.openCursor();
      await new Promise((resolve,reject) => { cursor.onerror = () => reject(new Error('idb_cursor_failed')); cursor.onsuccess = () => { const item = cursor.result; if (!item) return resolve(); try { checkJson(item.key); checkJson(item.value); records.push({ key: item.key, value: item.value }); item.continue(); } catch(error) { reject(error); } }; });
      stores.push({ name, keyPath: store.keyPath, autoIncrement: store.autoIncrement, indexes, records });
    }
    databases.push({ name: db.name, version: db.version, stores }); db.close();
  }
  return databases;
};
function qualify(content) {
  if (content.some(db => db.name === 'paseo-replica-cache')) throw new Error('idb_legacy_cache_unverified');
  const db = content.find(db => db.name === 'paseo-replica-row-store');
  if (!db) return [];
  if (db.version !== 1 || db.stores.map(store => store.name).join(',') !== 'meta,rows') throw new Error('idb_schema_unverified');
  const rows = db.stores.find(store => store.name === 'rows'), meta = db.stores.find(store => store.name === 'meta');
  if (JSON.stringify(rows.keyPath) !== '["serverId","kind","id"]' || rows.autoIncrement || rows.indexes.length || meta.keyPath !== null || meta.autoIncrement || meta.indexes.length) throw new Error('idb_schema_unverified');
  if (meta.records.length !== 1 || meta.records[0].key !== 'schema_version' || meta.records[0].value !== 1) throw new Error('idb_schema_version_unverified');
  for (const record of rows.records) {
    const value = record.value;
    if (!Array.isArray(record.key) || record.key.length !== 3 || !value || typeof value !== 'object' || Object.keys(value).sort().join(',') !== 'id,kind,payload,serverId' || typeof value.serverId !== 'string' || typeof value.id !== 'string' || typeof value.payload !== 'string' || !['agent','workspace','project','timeline','checkpoint'].includes(value.kind) || JSON.stringify(record.key) !== JSON.stringify([value.serverId,value.kind,value.id])) throw new Error('idb_row_schema_unverified');
  }
  return rows.records;
}
const digest = value => crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
app.whenReady().then(async () => {
  const storage = session.fromPath(request.sessionPath, { cache: false });
  storage.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !details.url.startsWith('paseo://app/') }));
  await storage.protocol.handle('paseo', () => new Response('<!doctype html><title>TEMP storage helper</title>', { headers: { 'content-type': 'text/html', 'content-security-policy': "default-src 'none'" } }));
  const window = new BrowserWindow({ show: false, webPreferences: { session: storage, nodeIntegration: false, contextIsolation: true, sandbox: true } });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  await window.loadURL('paseo://app/');
  const evaluate = expression => window.webContents.executeJavaScript(expression);
  const before = await evaluate(`(${fullSnapshot.toString()})()`), rowRecords = qualify(before);
  const expected = structuredClone(before);
  const rows = expected.find(db => db.name === 'paseo-replica-row-store')?.stores.find(store => store.name === 'rows');
  const selected = record => record.key[0] === request.serverId && ['agent','timeline'].includes(record.key[1]) && request.agentIds.includes(record.key[2]);
  if (rows) rows.records = rows.records.filter(record => !selected(record));
  const beforeHash = digest(before), expectedAfterHash = digest(expected);
  if (request.mode === 'apply') {
    if (beforeHash !== request.beforeSha256 || expectedAfterHash !== request.afterSha256) throw new Error('idb_frozen_logical_state_changed');
    mutationStarted = true;
    if (rows) await evaluate(`(async () => {
      const opening = indexedDB.open('paseo-replica-row-store');
      opening.onupgradeneeded = () => opening.transaction.abort();
      const db = await new Promise((resolve,reject) => { opening.onsuccess = () => resolve(opening.result); opening.onerror = () => reject(new Error('idb_open_failed')); });
      const tx = db.transaction(['rows','meta'], 'readwrite', { durability: 'strict' });
      const completed = new Promise((resolve,reject) => { tx.oncomplete = resolve; tx.onabort = () => reject(new Error('idb_transaction_aborted')); tx.onerror = () => reject(new Error('idb_transaction_failed')); });
      const meta = tx.objectStore('meta').get('schema_version');
      meta.onsuccess = () => {
        if (meta.result !== 1) return tx.abort();
        for (const id of ${JSON.stringify(request.agentIds)}) for (const kind of ['agent','timeline']) tx.objectStore('rows').delete([${JSON.stringify(request.serverId)},kind,id]);
      };
      await completed; db.close(); return true;
    })()`);
  }
  const after = await evaluate(`(${fullSnapshot.toString()})()`), afterRows = qualify(after);
  if (request.mode === 'apply' && digest(after) !== expectedAfterHash) throw new Error('idb_after_state_unverified');
  console.log(JSON.stringify({ status: 'verified', mode: request.mode, origin: await evaluate('location.origin'),
    runtime: { electron: process.versions.electron, chrome: process.versions.chrome, node: process.versions.node },
    beforeSha256: beforeHash, expectedAfterSha256: expectedAfterHash, afterSha256: digest(after),
    rowsBefore: rowRecords.length, rowsAfter: afterRows.length, selectedBefore: rowRecords.filter(selected).length,
    selectedAfter: afterRows.filter(selected).length, databases: after.length,
    totalRecords:after.reduce((sum,db)=>sum+db.stores.reduce((sum,store)=>sum+store.records.length,0),0),valueCodec:'json-safe.v1',mutationStarted }));
  await new Promise(resolve=>setTimeout(resolve,300));
  window.destroy(); app.quit();
}).catch(error => {
  console.log(JSON.stringify({ status: mutationStarted ? 'unknown' : 'blocked', code: error.message, mutationStarted }));
  app.exit(1);
});
