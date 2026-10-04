'use strict'

// Static format validator for the official LevelDB 1.x log/table format.
// No values, filenames, payloads or native error text leave this module.
const fs = require('node:fs')
const path = require('node:path')
const MAX_FILES = 4096, MAX_FILE = 128 * 1024 * 1024, MAX_TOTAL = 512 * 1024 * 1024
const MAX_RECORD = 32 * 1024 * 1024, MAX_BLOCK = 16 * 1024 * 1024, MAX_ENTRIES = 200000
function fail (code) { const error = new Error(code); error.code = code; throw error }
const bad = () => fail('office_leveldb_physical_corruption')
const table = new Uint32Array(256)
for (let i = 0; i < 256; i++) {
  let crc = i
  for (let j = 0; j < 8; j++) crc = (crc >>> 1) ^ ((crc & 1) ? 0x82f63b78 : 0)
  table[i] = crc >>> 0
}
function crc32c (bytes) {
  let crc = 0xffffffff
  for (const byte of bytes) crc = table[(crc ^ byte) & 255] ^ (crc >>> 8)
  return (crc ^ 0xffffffff) >>> 0
}
function mask (crc) { return (((crc >>> 15) | (crc << 17)) + 0xa282ead8) >>> 0 }
function checksum (bytes, expected) { if (mask(crc32c(bytes)) !== expected) bad() }
function cursor (bytes) {
  let at = 0
  function vi () {
    let value = 0n, shift = 0n
    for (let i = 0; i < 10; i++) {
      if (at >= bytes.length) bad()
      const byte = bytes[at++]
      if (i === 9 && byte > 1) bad()
      value |= BigInt(byte & 127) << shift
      if (!(byte & 128)) return value
      shift += 7n
    }
    bad()
  }
  function integer () { const value = vi(); if (value > BigInt(Number.MAX_SAFE_INTEGER)) bad(); return Number(value) }
  function slice (length) {
    if (!Number.isSafeInteger(length) || length < 0 || at + length > bytes.length) bad()
    const result = bytes.subarray(at, at + length); at += length; return result
  }
  return { vi, integer, slice, str: () => slice(integer()), end: () => at === bytes.length, at: () => at,
    byte: () => { if (at >= bytes.length) bad(); return bytes[at++] } }
}

function snappy (input) {
  const c = cursor(input), size = c.integer()
  if (size > MAX_BLOCK) fail('office_leveldb_physical_budget_exceeded')
  const output = Buffer.allocUnsafe(size)
  let at = 0
  while (!c.end()) {
    const tag = c.byte(), type = tag & 3
    let length, offset
    if (type === 0) {
      length = tag >>> 2
      if (length < 60) length++
      else {
        const encoded = c.slice(length - 59)
        length = 1
        for (let i = 0; i < encoded.length; i++) length += encoded[i] * (2 ** (8 * i))
      }
      if (at + length > size) bad()
      c.slice(length).copy(output, at); at += length
    } else {
      if (type === 1) { length = 4 + ((tag >>> 2) & 7); offset = ((tag & 224) << 3) | c.byte() }
      else { length = 1 + (tag >>> 2); const bytes = c.slice(type === 2 ? 2 : 4); offset = type === 2 ? bytes.readUInt16LE() : bytes.readUInt32LE() }
      if (!offset || offset > at || at + length > size) bad()
      for (let i = 0; i < length; i++) output[at + i] = output[at + i - offset]
      at += length
    }
  }
  if (at !== size) bad()
  return output
}

function records (bytes) {
  const logical = []
  let at = 0, parts = null, partSize = 0
  while (at < bytes.length) {
    const blockEnd = Math.min(bytes.length, (Math.floor(at / 32768) + 1) * 32768)
    if (blockEnd - at < 7) {
      if (bytes.subarray(at, blockEnd).some(byte => byte !== 0)) bad()
      at = blockEnd; continue
    }
    const expected = bytes.readUInt32LE(at), length = bytes.readUInt16LE(at + 4), type = bytes[at + 6]
    if (!expected && !length && !type) {
      if (bytes.subarray(at, blockEnd).some(byte => byte !== 0)) bad()
      at = blockEnd; continue
    }
    if (length > blockEnd - at - 7 || type < 1 || type > 4) bad()
    checksum(bytes.subarray(at + 6, at + 7 + length), expected)
    const data = bytes.subarray(at + 7, at + 7 + length); at += 7 + length
    if (type === 1) {
      if (parts !== null) bad()
      logical.push(data)
    } else if (type === 2) {
      if (parts !== null) bad()
      parts = [data]; partSize = data.length
    } else {
      if (parts === null) bad()
      parts.push(data); partSize += data.length
      if (partSize > MAX_RECORD) fail('office_leveldb_physical_budget_exceeded')
      if (type === 4) { logical.push(Buffer.concat(parts)); parts = null; partSize = 0 }
    }
    if (logical.length > MAX_ENTRIES) fail('office_leveldb_physical_budget_exceeded')
  }
  if (parts !== null) bad()
  return logical
}

function internalKey (key) {
  if (key.length < 8 || (key[key.length - 8] !== 0 && key[key.length - 8] !== 1)) bad()
}
function compareInternal (a, b) {
  const compare = Buffer.compare(a.subarray(0, -8), b.subarray(0, -8))
  if (compare) return compare
  const aa = a.readBigUInt64LE(a.length - 8), bb = b.readBigUInt64LE(b.length - 8)
  return aa === bb ? 0 : aa > bb ? -1 : 1
}
function writeBatch (bytes) {
  if (bytes.length < 12) bad()
  const seq = bytes.readBigUInt64LE(0), count = bytes.readUInt32LE(8)
  if (seq < 1n || seq > 0xffffffffffffffn || count > MAX_ENTRIES
      || count > 0 && seq + BigInt(count) - 1n > 0xffffffffffffffn) bad()
  const c = cursor(bytes.subarray(12))
  let found = 0
  while (!c.end()) {
    const type = c.byte()
    if (type !== 0 && type !== 1) fail('office_leveldb_record_schema_unverified')
    c.str(); if (type === 1) c.str()
    if (++found > MAX_ENTRIES) fail('office_leveldb_physical_budget_exceeded')
  }
  if (found !== count) bad()
}

function manifest (logical) {
  const state = { files: new Map(), comparator: null, log: null, prevLog: 0n, next: null, sequence: null }
  for (const record of logical) {
    const c = cursor(record), single = new Set(), changedFiles = new Set()
    let lastRank = -1
    while (!c.end()) {
      const tag = c.integer()
      // Accept the canonical VersionEdit::EncodeTo order only. Applying wire
      // order differs from native Builder::Apply for a noncanonical add/delete
      // pair and can otherwise hide a live table from the integrity check.
      const rank = [1, 2, 9, 3, 4, 5, 6, 7].indexOf(tag)
      if (rank < 0) fail('office_leveldb_manifest_schema_unverified')
      if (rank < lastRank) bad()
      lastRank = rank
      if ([1, 2, 3, 4, 9].includes(tag)) { if (single.has(tag)) bad(); single.add(tag) }
      if (tag === 1) { state.comparator = c.str().toString('latin1'); if (state.comparator !== 'leveldb.BytewiseComparator') fail('office_leveldb_comparator_unverified') }
      else if (tag === 2) state.log = c.vi()
      else if (tag === 9) state.prevLog = c.vi()
      else if (tag === 3) state.next = c.vi()
      else if (tag === 4) { state.sequence = c.vi(); if (state.sequence > 0xffffffffffffffn) bad() }
      else if (tag === 5) { const level = c.integer(); if (level >= 7) bad(); internalKey(c.str()) }
      else if (tag === 6) {
        const level = c.integer(), number = c.vi(), identity = level + ':' + number
        if (level >= 7 || !number || changedFiles.has(identity)) bad()
        changedFiles.add(identity); state.files.delete(identity)
      }
      else if (tag === 7) {
        const level = c.integer(), number = c.vi(), size = c.integer(), smallest = c.str(), largest = c.str()
        const identity = level + ':' + number
        if (level >= 7 || !number || size > MAX_FILE || changedFiles.has(identity)) bad()
        changedFiles.add(identity)
        internalKey(smallest); internalKey(largest)
        if (compareInternal(smallest, largest) > 0) bad()
        state.files.set(identity, { level, number, size, smallest, largest })
      } else fail('office_leveldb_manifest_schema_unverified')
    }
  }
  if (state.comparator === null || state.log === null || state.next === null || state.sequence === null) bad()
  return state
}

function blockEntries (bytes, internal) {
  if (bytes.length < 4) bad()
  const restarts = bytes.readUInt32LE(bytes.length - 4)
  if (!restarts || restarts > MAX_ENTRIES || 4 + restarts * 4 > bytes.length) bad()
  const end = bytes.length - 4 - restarts * 4, restartOffsets = []
  for (let i = 0; i < restarts; i++) {
    const offset = bytes.readUInt32LE(end + i * 4)
    if (offset > end || (i === 0 && offset !== 0) || (i > 0 && offset <= restartOffsets[i - 1])) bad()
    restartOffsets.push(offset)
  }
  const c = cursor(bytes.subarray(0, end)), result = []
  let previous = Buffer.alloc(0), restartIndex = 0
  while (!c.end()) {
    const offset = c.at(), shared = c.integer(), nonshared = c.integer(), length = c.integer()
    if (shared > previous.length) bad()
    if (restartIndex < restarts && offset === restartOffsets[restartIndex]) {
      if (shared !== 0) bad()
      restartIndex++
    } else if (restartIndex < restarts && offset > restartOffsets[restartIndex]) bad()
    const key = Buffer.concat([previous.subarray(0, shared), c.slice(nonshared)]), value = c.slice(length)
    if (internal) internalKey(key)
    if (result.length && (internal ? compareInternal(previous, key) : Buffer.compare(previous, key)) >= 0) bad()
    result.push({ key, value }); previous = key
    if (result.length > MAX_ENTRIES) fail('office_leveldb_physical_budget_exceeded')
  }
  // Empty blocks have a single restart offset of zero.
  if (result.length && restartIndex !== restarts || !result.length && (restarts !== 1 || end !== 0)) bad()
  return result
}
function handle (raw) {
  const c = cursor(raw), offset = c.integer(), size = c.integer()
  if (!c.end() || size > MAX_BLOCK) bad()
  return { offset, size }
}
function filter (bytes) {
  if (bytes.length < 5 || bytes[bytes.length - 1] !== 11) bad()
  const start = bytes.readUInt32LE(bytes.length - 5)
  if (start > bytes.length - 5 || (bytes.length - 5 - start) % 4) bad()
  let previous = 0
  for (let at = start; at < bytes.length - 5; at += 4) {
    const offset = bytes.readUInt32LE(at)
    if (offset < previous || offset > start || at === start && offset !== 0) bad()
    previous = offset
  }
  return (offset, key) => {
    const index = Math.floor(offset / 2048), count = (bytes.length - 5 - start) / 4
    // Native treats a missing filter as a possible match.
    if (index >= count) return
    const begin = bytes.readUInt32LE(start + index * 4)
    const end = index + 1 === count ? start : bytes.readUInt32LE(start + (index + 1) * 4)
    const data = bytes.subarray(begin, end)
    if (data.length < 2) bad()
    const bits = (data.length - 1) * 8, probes = data[data.length - 1]
    if (probes > 30) return // Reserved native encoding means possible match.
    const m = 0xc6a4a793
    let h = (0xbc9f1d34 ^ Math.imul(key.length, m)) >>> 0, at = 0
    while (at + 4 <= key.length) {
      h = Math.imul((h + key.readUInt32LE(at)) >>> 0, m) >>> 0
      h ^= h >>> 16; at += 4
    }
    if (key.length - at >= 3) h = (h + (key[at + 2] << 16)) >>> 0
    if (key.length - at >= 2) h = (h + (key[at + 1] << 8)) >>> 0
    if (key.length - at >= 1) { h = Math.imul((h + key[at]) >>> 0, m) >>> 0; h ^= h >>> 24 }
    const delta = ((h >>> 17) | (h << 15)) >>> 0
    for (let i = 0; i < probes; i++) {
      const bit = (h >>> 0) % bits
      if (!(data[Math.floor(bit / 8)] & (1 << (bit % 8)))) bad()
      h = (h + delta) >>> 0
    }
  }
}

function sst (bytes) {
  if (bytes.length < 48 || bytes.readBigUInt64LE(bytes.length - 8) !== 0xdb4775248b80fb57n) bad()
  const footerOffset = bytes.length - 48, footer = cursor(bytes.subarray(footerOffset, bytes.length - 8))
  const meta = { offset: footer.integer(), size: footer.integer() }, index = { offset: footer.integer(), size: footer.integer() }
  if (footer.slice(40 - footer.at()).some(byte => byte !== 0)) bad()
  const seen = new Map()
  function read (h) {
    if (h.size > MAX_BLOCK || h.offset + h.size + 5 > footerOffset || seen.has(h.offset)) bad()
    const compression = bytes[h.offset + h.size]
    checksum(bytes.subarray(h.offset, h.offset + h.size + 1), bytes.readUInt32LE(h.offset + h.size + 1))
    const raw = bytes.subarray(h.offset, h.offset + h.size)
    if (compression > 1) fail('office_leveldb_compression_unverified')
    const decoded = compression ? snappy(raw) : raw
    seen.set(h.offset, { size: h.size, compression })
    return decoded
  }
  const indexEntries = blockEntries(read(index), true), metaEntries = blockEntries(read(meta), false)
  let firstKey = null, lastKey = null, previousSeparator = null
  const dataBlocks = []
  for (const entry of indexEntries) {
    const location = handle(entry.value), entries = blockEntries(read(location), true)
    if (!entries.length || lastKey && compareInternal(lastKey, entries[0].key) >= 0) bad()
    if (previousSeparator && compareInternal(previousSeparator, entries[0].key) >= 0) bad()
    if (compareInternal(entries[entries.length - 1].key, entry.key) > 0) bad()
    if (firstKey === null) firstKey = entries[0].key
    lastKey = entries[entries.length - 1].key
    previousSeparator = entry.key
    dataBlocks.push({ offset: location.offset, entries })
  }
  for (const entry of metaEntries) {
    const name = entry.key.toString('latin1')
    if (!['filter.leveldb.BuiltinBloomFilter2', 'filter.leveldb.BuiltinBloomFilter'].includes(name)) fail('office_leveldb_meta_block_schema_unverified')
    const mayMatch = filter(read(handle(entry.value)))
    for (const block of dataBlocks) for (const item of block.entries) mayMatch(block.offset, item.key.subarray(0, -8))
  }
  let end = 0
  for (const [offset, h] of [...seen].sort((a, b) => a[0] - b[0])) {
    if (offset !== end) bad()
    end += h.size + 5
  }
  if (end !== footerOffset) bad()
  return { blocks: seen.size, compressed_blocks: [...seen.values()].filter(h => h.compression === 1).length,
    first_key: firstKey, last_key: lastKey }
}

function validate (root) {
  const info = fs.lstatSync(root)
  if (!info.isDirectory() || info.isSymbolicLink()) fail('office_leveldb_path_redirected')
  const names = fs.readdirSync(root)
  if (names.length > MAX_FILES) fail('office_leveldb_physical_budget_exceeded')
  const files = new Map(), manifests = new Map(), tables = new Map(), logs = new Map()
  let total = 0, blocks = 0, compressed = 0
  for (const name of names) {
    if (!/^(?:CURRENT|LOCK|LOG(?:\.old)?|MANIFEST-[0-9]+|[0-9]+\.(?:ldb|sst|log))$/.test(name)) fail('office_leveldb_file_schema_unverified')
    const location = path.join(root, name), stat = fs.lstatSync(location)
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) fail('office_leveldb_path_redirected')
    if (stat.size > MAX_FILE || (total += stat.size) > MAX_TOTAL) fail('office_leveldb_physical_budget_exceeded')
    const raw = fs.readFileSync(location)
    if (raw.length !== stat.size) fail('office_leveldb_physical_changed')
    files.set(name, raw)
    if (/^MANIFEST-/.test(name)) manifests.set(name, manifest(records(raw)))
    else if (/\.log$/.test(name)) { for (const record of records(raw)) writeBatch(record); logs.set(BigInt(name.slice(0, -4)), raw) }
    else if (/\.(?:ldb|sst)$/.test(name)) {
      const number = BigInt(name.split('.')[0])
      if (tables.has(number)) fail('office_leveldb_file_number_ambiguous')
      const result = sst(raw); blocks += result.blocks; compressed += result.compressed_blocks
      tables.set(number, { raw, result })
    }
  }
  const current = files.get('CURRENT')
  if (!current || !/^MANIFEST-[0-9]+\n$/.test(current.toString('latin1'))) bad()
  const state = manifests.get(current.toString('latin1').trim())
  if (!state) bad()
  const liveNumbers = new Set()
  for (const file of state.files.values()) {
    if (liveNumbers.has(file.number)) bad()
    liveNumbers.add(file.number)
    if (!tables.has(file.number) || tables.get(file.number).raw.length !== file.size) fail('office_leveldb_manifest_reference_missing')
    const actual = tables.get(file.number).result
    if (!actual.first_key || !file.smallest.equals(actual.first_key) || !file.largest.equals(actual.last_key)
        || file.number >= state.next) bad()
  }
  for (let level = 1; level < 7; level++) {
    const members = [...state.files.values()].filter(file => file.level === level)
      .sort((a, b) => compareInternal(a.smallest, b.smallest))
    for (let i = 1; i < members.length; i++) {
      if (compareInternal(members[i - 1].largest, members[i].smallest) >= 0) bad()
    }
  }
  if (state.prevLog && !logs.has(state.prevLog)) fail('office_leveldb_manifest_reference_missing')
  // LevelDB recovers every log >= active log number, not only that exact file.
  if (![...logs.keys()].some(number => number >= state.log)) fail('office_leveldb_manifest_reference_missing')
  return { schema_version: 'larj.leveldb-physical.v1', files: files.size, bytes: total,
    tables: tables.size, live_tables: liveNumbers.size, manifests: manifests.size,
    logs: logs.size, blocks, compressed_blocks: compressed }
}

module.exports = { validate, crc32c, mask, cursor, records, sst, snappy, blockEntries, manifest, writeBatch }
