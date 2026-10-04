'use strict'

// Python owns path/operation admission, private copies and helper lifetime.
// Opening a live LevelDB can write recovery state, including before a batch.
const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')
const readline = require('node:readline')
const { createRequire } = require('node:module')
const runtimeRequire = process.env.LARJ_LEVELDB_RUNTIME
  ? createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME, 'package.json'))
  : require
const physical = require('./physical.cjs')
const sha = bytes => bytes === null ? null : crypto.createHash('sha256').update(bytes).digest('hex')
const fail = code => { const error = new Error(code); error.code = code; throw error }
const ID = /^[A-Za-z0-9_.-]{1,128}$/
const MAX_BYTES = 16 * 1024 * 1024

function decodeString (bytes) {
  if (!Buffer.isBuffer(bytes) || !bytes.length) fail('office_leveldb_string_invalid')
  if (bytes[0] === 1) return bytes.subarray(1).toString('latin1')
  if (bytes[0] === 0 && (bytes.length - 1) % 2 === 0) return bytes.subarray(1).toString('utf16le')
  fail('office_leveldb_encoding_unverified')
}

function encodeString (text, preferred = null) {
  const latin = [...text].every(char => char.codePointAt(0) <= 255)
  const format = preferred === 0 ? 0 : latin ? 1 : 0
  return Buffer.concat([Buffer.from([format]), Buffer.from(text, format === 1 ? 'latin1' : 'utf16le')])
}

// Strict recursive tokenizer. Values remain opaque raw slices, preserving all
// retained draft values (including numeric spelling/escapes) byte for byte in
// the decoded JSON rather than reserializing them with JSON.stringify.
function mapEntries (text) {
  if (text.length > MAX_BYTES) fail('office_leveldb_draft_budget_exceeded')
  let at = 0
  const ws = () => { while (at < text.length && /[ \r\n\t]/.test(text[at])) at++ }
  function string () {
    if (text[at] !== '"') fail('office_leveldb_json_invalid')
    const start = at++
    for (;;) {
      if (at >= text.length) fail('office_leveldb_json_invalid')
      const ch = text[at++]
      if (ch === '"') break
      if (ch.charCodeAt(0) < 32) fail('office_leveldb_json_invalid')
      if (ch === '\\') {
        const escape = text[at++]
        if (escape === 'u') {
          if (!/^[0-9a-fA-F]{4}$/.test(text.slice(at, at + 4))) fail('office_leveldb_json_invalid')
          at += 4
        } else if (!['"', '\\', '/', 'b', 'f', 'n', 'r', 't'].includes(escape)) fail('office_leveldb_json_invalid')
      }
    }
    return JSON.parse(text.slice(start, at))
  }
  function value (depth) {
    if (depth > 64) fail('office_leveldb_json_budget_exceeded')
    ws()
    if (text[at] === '"') { string(); return }
    if (text[at] === '{') { object(depth + 1, false); return }
    if (text[at] === '[') {
      at++; ws()
      if (text[at] === ']') { at++; return }
      for (;;) {
        value(depth + 1); ws()
        const delimiter = text[at++]
        if (delimiter === ']') return
        if (delimiter !== ',') fail('office_leveldb_json_invalid')
      }
    }
    const match = /^(?:null|true|false|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)/.exec(text.slice(at))
    if (!match) fail('office_leveldb_json_invalid')
    at += match[0].length
  }
  function object (depth, capture) {
    const entries = [], keys = new Set()
    if (text[at++] !== '{') fail('office_leveldb_json_invalid')
    ws()
    if (text[at] === '}') { at++; return entries }
    for (;;) {
      ws(); const start = at, key = string(); ws()
      if (keys.has(key)) fail('office_leveldb_json_duplicate_key')
      keys.add(key)
      if (keys.size > 20000) fail('office_leveldb_json_budget_exceeded')
      if (text[at++] !== ':') fail('office_leveldb_json_invalid')
      value(depth + 1)
      if (capture) entries.push({ key, raw: text.slice(start, at) })
      ws()
      const delimiter = text[at++]
      if (delimiter === '}') return entries
      if (delimiter !== ',') fail('office_leveldb_json_invalid')
    }
  }
  ws(); const entries = object(0, true); ws()
  if (at !== text.length) fail('office_leveldb_json_invalid')
  return entries
}

function cleanDraft (raw, chatIds) {
  const entries = mapEntries(decodeString(raw)), retained = [], pairs = []
  for (const entry of entries) {
    const components = entry.key.split(':')
    if (chatIds.has(components[0])) {
      if (components.length !== 2 || !ID.test(components[1])) fail('office_leveldb_draft_identity_unverified')
      pairs.push(components)
    } else retained.push(entry.raw)
  }
  pairs.sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)))
  return { after: encodeString('{' + retained.join(',') + '}', raw[0]), pairs }
}

function varint (number) {
  if (number < 0n || number > 0xffffffffffffffffn) fail('office_leveldb_metadata_invalid')
  const out = []
  do { let byte = Number(number & 127n); number >>= 7n; if (number) byte |= 128; out.push(byte) } while (number)
  return Buffer.from(out)
}

function metadata (raw, delta) {
  let at = 0
  function read () {
    let result = 0n, shift = 0n
    for (let count = 0; count < 10; count++) {
      if (at >= raw.length) fail('office_leveldb_metadata_invalid')
      const byte = raw[at++]; result |= BigInt(byte & 127) << shift
      if (!(byte & 128)) return result
      shift += 7n
    }
    fail('office_leveldb_metadata_invalid')
  }
  const values = new Map()
  while (at < raw.length) {
    const tag = read()
    if (tag !== 8n && tag !== 16n || values.has(tag)) fail('office_leveldb_metadata_schema_unverified')
    values.set(tag, read())
  }
  if (!values.has(8n) || !values.has(16n)) fail('office_leveldb_metadata_invalid')
  const size = values.get(16n) + BigInt(delta)
  // Keep the existing last_modified timestamp: deterministic before/after state
  // is frozen at plan time. Parent may choose to freeze a new known timestamp.
  return Buffer.concat([Buffer.from([8]), varint(values.get(8n)), Buffer.from([16]), varint(size)])
}

function dataKey (raw) {
  if (raw[0] !== 95) return null
  const sep = raw.indexOf(0, 1)
  if (sep < 0) fail('office_leveldb_key_schema_unverified')
  return { origin: raw.subarray(1, sep).toString('latin1'), name: decodeString(raw.subarray(sep + 1)), encodedSize: raw.length - sep - 1 }
}

const uiCleaner = require('./office_ui.cjs')({ decodeString, encodeString, mapEntries, cleanDraft, fail })

async function projection (db, request) {
  const version = await db.get(Buffer.from('VERSION'))
  if (!version || !Buffer.from(version).equals(Buffer.from('1'))) fail('office_leveldb_schema_unverified')
  const chatIds = new Set(request.chat_ids)
  if (!chatIds.size || chatIds.size !== request.chat_ids.length || [...chatIds].some(id => !ID.test(id))) fail('office_leveldb_chat_identity_invalid')
  const childIds = new Set(request.sub_chat_ids || [])
  if ([...childIds].some(id => !ID.test(id)) || childIds.size !== (request.sub_chat_ids || []).length) fail('office_leveldb_chat_identity_invalid')
  const edits = [], draftKeys = [], seenKeys = new Set(), origins = new Map(), count = { draft_maps: 0, removed_pairs: 0, keys: 0 }
  // Keys-only iteration does not read credential/input-history values.
  for await (const rawKey of db.keys({ fillCache: false, highWaterMarkBytes: 0 })) {
    if (++count.keys > 20000) fail('office_leveldb_key_budget_exceeded')
    const key = dataKey(rawKey)
    if (!key) continue
    const cleanValue = uiCleaner(key.name, chatIds, childIds)
    if (!cleanValue) continue
    if (!request.origins.includes(key.origin)) fail('office_leveldb_draft_origin_unverified')
    // UTF16 and Latin1 names can otherwise denote the same logical DOM key.
    const semantic = JSON.stringify([key.origin, key.name])
    if (seenKeys.has(semantic)) fail('office_leveldb_duplicate_draft_key')
    seenKeys.add(semantic)
    const before = await db.get(rawKey)
    if (!before || before.length > MAX_BYTES) fail('office_leveldb_draft_budget_exceeded')
    const clean = cleanValue(before)
    count.draft_maps++
    const metaKey = Buffer.from('META:' + key.origin)
    const beforeMeta = await db.get(metaKey)
    if (!beforeMeta) fail('office_leveldb_metadata_missing')
    // Parse even nonselected metadata to qualify the same exact local format.
    metadata(beforeMeta, 0)
    draftKeys.push({ key_sha256: sha(rawKey), value_sha256: sha(before),
      metadata_key_sha256: sha(metaKey), metadata_sha256: sha(beforeMeta) })
    if (!clean.pairs.length) continue
    const delta = clean.after === null ? -key.encodedSize - before.length : clean.after.length - before.length
    const origin = origins.get(key.origin) || { metaKey, beforeMeta, delta: 0 }
    origin.delta += delta; origins.set(key.origin, origin)
    edits.push({ key: rawKey, before, after: clean.after, metaKey, beforeMeta, origin: key.origin, pairs: clean.pairs })
    count.removed_pairs += clean.pairs.length
  }
  // Hash the complete logical store without exposing or interpreting other
  // values. A physical hash cannot survive legitimate WAL/compaction changes;
  // a drafts-only hash could miss loss of unrelated values during recovery.
  const replacements = new Map()
  for (const edit of edits) {
    const origin = origins.get(edit.origin)
    edit.afterMeta = metadata(origin.beforeMeta, origin.delta)
    replacements.set(edit.key.toString('hex'), edit.after)
    replacements.set(edit.metaKey.toString('hex'), edit.afterMeta)
  }
  const beforeHash = crypto.createHash('sha256'), afterHash = crypto.createHash('sha256')
  let total = 0, entries = 0
  function field (hash, bytes) {
    const length = Buffer.alloc(8); length.writeBigUInt64LE(BigInt(bytes.length))
    hash.update(length); hash.update(bytes)
  }
  for await (const [key, value] of db.iterator({ fillCache: false, highWaterMarkBytes: 0 })) {
    total += key.length + value.length
    if (++entries > 20000 || total > 512 * 1024 * 1024) fail('office_leveldb_logical_budget_exceeded')
    field(beforeHash, key); field(beforeHash, value)
    const replacement = replacements.get(key.toString('hex'))
    if (replacement !== null) { field(afterHash, key); field(afterHash, replacement === undefined ? value : replacement) }
  }
  return { edits, draft_keys: draftKeys, count,
    logical_before_sha256: beforeHash.digest('hex'), logical_after_sha256: afterHash.digest('hex') }
}

function publicProjection (value) {
  return {
    count: value.count,
    draft_keys: value.draft_keys,
    logical_before_sha256: value.logical_before_sha256,
    logical_after_sha256: value.logical_after_sha256,
    edits: value.edits.map(edit => ({ key_sha256: sha(edit.key), before_sha256: sha(edit.before),
      after_sha256: sha(edit.after), metadata_key_sha256: sha(edit.metaKey),
      before_metadata_sha256: sha(edit.beforeMeta), after_metadata_sha256: sha(edit.afterMeta),
      pairs: edit.pairs }))
  }
}

function digestProjection (value) { return sha(Buffer.from(JSON.stringify(publicProjection(value)))) }

async function run (request, hooks = {}) {
  if (!request || request.schema_version !== 'larj.office-leveldb-helper.v1' || !['inspect-copy', 'apply'].includes(request.operation)) fail('office_leveldb_request_invalid')
  if (runtimeRequire('classic-level/package.json').version !== '3.0.0') fail('office_leveldb_dependency_version_unverified')
  // Load only the pinned package's platform prebuild. node-gyp-build normally
  // falls back to build/Release or prebuilds beside process.execPath; neither
  // belongs to this installed dependency's approval.
  const packageRoot = path.dirname(runtimeRequire.resolve('classic-level/package.json'))
  const tuples = { 'win32-x64': ['win32-x64', 'classic-level.node'], 'win32-ia32': ['win32-ia32', 'classic-level.node'],
    'darwin-x64': ['darwin-x64+arm64', 'classic-level.node'], 'darwin-arm64': ['darwin-x64+arm64', 'classic-level.node'],
    'linux-x64': ['linux-x64', process.report.getReport().header.glibcVersionRuntime ? 'classic-level.node' : 'classic-level.musl.node'],
    'linux-arm64': ['linux-arm64', 'classic-level.armv8.node'] }
  const tuple = tuples[process.platform + '-' + process.arch]
  if (!tuple) fail('office_leveldb_platform_unverified')
  const binding = path.join(packageRoot, 'binding.js')
  require.cache[binding] = { id: binding, filename: binding, loaded: true, exports: require(path.join(packageRoot, 'prebuilds', ...tuple)) }
  const { ClassicLevel } = require(path.join(packageRoot, 'index.js'))
  if (!path.isAbsolute(request.path)) fail('office_leveldb_path_invalid')
  const info = fs.lstatSync(request.path)
  if (!info.isDirectory() || info.isSymbolicLink()) fail('office_leveldb_path_redirected')
  physical.validate(request.path)
  // Python validates all ancestors, descendant file identities, link counts,
  // reparse flags, allowed root and snapshot before invoking this draft.
  const db = new ClassicLevel(request.path, { keyEncoding: 'buffer', valueEncoding: 'buffer', createIfMissing: false, errorIfExists: false })
  let attempted = false
  try {
    // A classic-level open can rotate/recover WAL; parent journals the first
    // live open as mutation_started. inspect-copy ONLY accepts private copies.
    await db.open()
    const before = await projection(db, request), fingerprint = digestProjection(before)
    await hooks.opened?.(db, fingerprint)
    if (request.operation === 'inspect-copy') return { schema_version: request.schema_version, fingerprint, ...publicProjection(before) }
    if (fingerprint !== request.authorized_before_sha256) fail('office_leveldb_frozen_state_changed')
    if (sha(Buffer.from(JSON.stringify(before.edits.map(edit => ({ key: sha(edit.key), after: sha(edit.after), meta: sha(edit.afterMeta) }))))) !== request.authorized_after_sha256) fail('office_leveldb_after_authorization_mismatch')
    const operations = before.edits.map(edit => edit.after === null ? { type: 'del', key: edit.key } : { type: 'put', key: edit.key, value: edit.after })
    const metas = new Map(before.edits.map(edit => [edit.metaKey.toString('hex'), edit]))
    for (const edit of metas.values()) operations.push({ type: 'put', key: edit.metaKey, value: edit.afterMeta })
    attempted = true
    if (operations.length) await db.batch(operations, { sync: true })
    for (const edit of before.edits) {
      await db.compactRange(edit.key, edit.key, { keyEncoding: 'buffer' })
      const actual = await db.get(edit.key)
      if ((edit.after === null ? actual !== undefined : !actual || !Buffer.from(actual).equals(edit.after))
          || !Buffer.from(await db.get(edit.metaKey)).equals(edit.afterMeta)) fail('office_leveldb_after_state_unverified')
    }
    const after = await projection(db, request)
    if (after.count.removed_pairs || after.logical_before_sha256 !== before.logical_after_sha256) fail('office_leveldb_after_state_unverified')
    return { schema_version: request.schema_version, status: 'verified', mutation_started: true,
      removed_pairs: before.count.removed_pairs, remaining_pairs: 0,
      after_keys: before.edits.map(edit => ({ key_sha256: sha(edit.key), value_sha256: sha(edit.after), metadata_sha256: sha(edit.afterMeta) })) }
  } catch (error) {
    const code = /^office_leveldb_[a-z_]+$/.test(error.code || '') ? error.code : 'office_leveldb_engine_failure'
    const safe = new Error(code); safe.code = code; safe.unknown = attempted
    throw safe
  } finally {
    await db.close()
    // classic-level does not expose paranoidChecks or verifyChecksums. This
    // independent strict physical validator is the explicit integrity gate.
    physical.validate(request.path)
  }
}

module.exports = { encodeString, decodeString, mapEntries, cleanDraft, projection, publicProjection, digestProjection, run, sha, varint }
if (require.main === module) {
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity })
  lines.once('line', async line => {
    lines.close()
    try {
      if (line.length > 256 * 1024) fail('office_leveldb_request_budget_exceeded')
      const result = await run(JSON.parse(line))
      process.stdout.write(JSON.stringify(result) + '\n')
    } catch (error) {
      const code = /^office_leveldb_[a-z_]+$/.test(error.code || '') ? error.code : 'office_leveldb_request_invalid'
      process.stdout.write(JSON.stringify({ schema_version: 'larj.office-leveldb-helper.v1', status: 'blocked', blocker_code: code, unknown: error.unknown === true }) + '\n')
      process.exitCode = 1
    }
  })
}
