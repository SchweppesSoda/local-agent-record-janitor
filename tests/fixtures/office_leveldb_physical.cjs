'use strict'

// Independent, synthetic LevelDB fixtures. No imports from the validator under
// review, no real store discovery, and no filesystem mutation when imported.
// buildCase(name) -> { files: Map<string, Buffer>, table, manifest, wal, ... }.
// writeTempCase(name) creates a NEW os.tmpdir() directory; it never accepts an
// existing output path and never deletes, overwrites or follows store paths.
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const MAX_SEQUENCE = (1n << 56n) - 1n
const MAGIC = 0xdb4775248b80fb57n

const crcTable = new Uint32Array(256)
for (let i = 0; i < 256; i++) {
  let value = i
  for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ ((value & 1) ? 0x82f63b78 : 0)
  crcTable[i] = value >>> 0
}
function crc32c (bytes) {
  let value = 0xffffffff
  for (const byte of bytes) value = crcTable[(value ^ byte) & 255] ^ (value >>> 8)
  return (value ^ 0xffffffff) >>> 0
}
function maskedCrc (bytes) {
  const value = crc32c(bytes)
  return (((value >>> 15) | (value << 17)) + 0xa282ead8) >>> 0
}
function vi (value) {
  value = BigInt(value)
  if (value < 0n || value > (1n << 64n) - 1n) throw new Error('fixture_varint_invalid')
  const bytes = []
  do {
    let byte = Number(value & 127n)
    value >>= 7n
    if (value) byte |= 128
    bytes.push(byte)
  } while (value)
  return Buffer.from(bytes)
}
function str (value) {
  const bytes = Buffer.isBuffer(value) ? value : Buffer.from(value)
  return Buffer.concat([vi(bytes.length), bytes])
}
function internalKey (userKey, sequence = 1n, type = 1n) {
  sequence = BigInt(sequence); type = BigInt(type)
  const tail = Buffer.alloc(8)
  tail.writeBigUInt64LE((sequence << 8n) | type)
  return Buffer.concat([Buffer.isBuffer(userKey) ? userKey : Buffer.from(userKey), tail])
}
function rawBlock (entries) {
  const encoded = []
  const offsets = []
  let length = 0
  // Each entry is a restart. Compression is unnecessary for these probes.
  for (const { key, value } of entries) {
    offsets.push(length)
    const record = Buffer.concat([vi(0), vi(key.length), vi(value.length), key, value])
    encoded.push(record); length += record.length
  }
  if (!offsets.length) offsets.push(0)
  const restarts = Buffer.alloc(4 * (offsets.length + 1))
  offsets.forEach((offset, index) => restarts.writeUInt32LE(offset, 4 * index))
  restarts.writeUInt32LE(offsets.length, 4 * offsets.length)
  return Buffer.concat([...encoded, restarts])
}
function physicalBlock (bytes, compression = 0) {
  const tail = Buffer.alloc(5)
  tail[0] = compression
  tail.writeUInt32LE(maskedCrc(Buffer.concat([bytes, Buffer.from([compression])])), 1)
  return Buffer.concat([bytes, tail])
}
function blockHandle (offset, size) { return Buffer.concat([vi(offset), vi(size)]) }

function table (blocks, separators = null, bloom = null) {
  const encoded = [], handles = []
  let offset = 0
  for (const entries of blocks) {
    const raw = rawBlock(entries), physical = physicalBlock(raw)
    handles.push({ offset, size: raw.length }); encoded.push(physical); offset += physical.length
  }
  let filterHandle = null
  if (bloom) {
    const physical = physicalBlock(bloom)
    filterHandle = { offset, size: bloom.length }
    encoded.push(physical); offset += physical.length
  }
  const metaRaw = rawBlock(filterHandle ? [{
    key: Buffer.from('filter.leveldb.BuiltinBloomFilter2'),
    value: blockHandle(filterHandle.offset, filterHandle.size)
  }] : [])
  const metaHandle = { offset, size: metaRaw.length }, meta = physicalBlock(metaRaw)
  encoded.push(meta); offset += meta.length
  const indexRaw = rawBlock(blocks.map((entries, index) => ({
    key: separators ? separators[index] : entries[entries.length - 1].key,
    value: blockHandle(handles[index].offset, handles[index].size)
  })))
  const indexHandle = { offset, size: indexRaw.length }, index = physicalBlock(indexRaw)
  encoded.push(index)
  const footer = Buffer.alloc(48)
  Buffer.concat([blockHandle(metaHandle.offset, metaHandle.size),
    blockHandle(indexHandle.offset, indexHandle.size)]).copy(footer)
  footer.writeBigUInt64LE(MAGIC, 40)
  return { bytes: Buffer.concat([...encoded, footer]),
    firstKey: blocks[0][0].key, lastKey: blocks[blocks.length - 1].at(-1).key }
}
function oneFilter (allBitsSet = false) {
  // One filter covering data block offsets [0, 2048). Eight bitset bytes,
  // k=6, offset[0]=0, offset-array start=9, base_lg=11.
  const bytes = Buffer.alloc(18)
  if (allBitsSet) bytes.fill(255, 0, 8)
  bytes[8] = 6
  bytes.writeUInt32LE(0, 9)
  bytes.writeUInt32LE(9, 13)
  bytes[17] = 11
  return bytes
}
function logRecords (records) {
  const chunks = []
  let absolute = 0
  for (const record of records) {
    let at = 0, first = true
    do {
      let remaining = 32768 - (absolute % 32768)
      if (remaining < 7) {
        chunks.push(Buffer.alloc(remaining)); absolute += remaining; remaining = 32768
      }
      const length = Math.min(record.length - at, remaining - 7)
      const last = at + length === record.length
      const type = first ? (last ? 1 : 2) : (last ? 4 : 3)
      const payload = record.subarray(at, at + length), header = Buffer.alloc(7)
      header.writeUInt32LE(maskedCrc(Buffer.concat([Buffer.from([type]), payload])), 0)
      header.writeUInt16LE(length, 4); header[6] = type
      chunks.push(header, payload); absolute += 7 + length; at += length; first = false
    } while (at < record.length)
  }
  return Buffer.concat(chunks)
}
function writeBatch (sequence, operations, declaredCount = operations.length) {
  const header = Buffer.alloc(12)
  header.writeBigUInt64LE(BigInt(sequence), 0); header.writeUInt32LE(declaredCount, 8)
  return Buffer.concat([header, ...operations.map(operation => Buffer.concat([
    Buffer.from([operation.value === undefined ? 0 : 1]), str(operation.key),
    ...(operation.value === undefined ? [] : [str(operation.value)])
  ]))])
}
function manifestHead ({ log = 2n, next = 4n, sequence = 1n } = {}) {
  return Buffer.concat([vi(1), str('leveldb.BytewiseComparator'), vi(2), vi(log),
    vi(9), vi(0), vi(3), vi(next), vi(4), vi(sequence)])
}
function addFile (bytes, smallest, largest, level = 0n, number = 3n) {
  return Buffer.concat([vi(7), vi(level), vi(number), vi(bytes.length), str(smallest), str(largest)])
}
function deleteFile (level = 0n, number = 3n) { return Buffer.concat([vi(6), vi(level), vi(number)]) }

const names = Object.freeze([
  'valid',
  'sst_separator_before_last',
  'sst_separator_crosses_next_block',
  'sst_bloom_false_negative',
  'wal_sequence_overflow',
  'wal_zero_sequence_empty_count',
  'manifest_add_then_delete',
  'manifest_next_number_collision',
  'manifest_bounds_mismatch'
])
function buildCase (name) {
  if (!names.includes(name)) throw new Error('fixture_case_unknown')
  let blocks = [[{ key: internalKey('z'), value: Buffer.from('SYNTHETIC_VALUE') }]]
  let separators = null, bloom = null
  if (name === 'sst_separator_before_last') separators = [internalKey('a')]
  if (name === 'sst_separator_crosses_next_block') {
    blocks = [[{ key: internalKey('a'), value: Buffer.from('SYNTHETIC_A') }],
      [{ key: internalKey('m', 2n), value: Buffer.from('SYNTHETIC_M') }]]
    separators = [internalKey('z'), internalKey('zz', 2n)]
  }
  if (name === 'sst_bloom_false_negative') bloom = oneFilter(false)
  const builtTable = table(blocks, separators, bloom)
  const sequence = name === 'sst_separator_crosses_next_block' ? 2n : 1n
  const smallest = name === 'manifest_bounds_mismatch' ? internalKey('a') : builtTable.firstKey
  const largest = name === 'manifest_bounds_mismatch' ? internalKey('a') : builtTable.lastKey
  const addition = addFile(builtTable.bytes, smallest, largest)
  let logicalManifest = Buffer.concat([manifestHead({ sequence,
    next: name === 'manifest_next_number_collision' ? 3n : 4n }), addition])
  if (name === 'manifest_add_then_delete') {
    // Native DecodeFrom stores separate delete/add collections and Builder::Apply
    // processes deletes FIRST. A wire-order validator incorrectly sees zero files.
    logicalManifest = Buffer.concat([logicalManifest, deleteFile()])
  }
  let batch = null
  if (name === 'wal_sequence_overflow') batch = writeBatch(MAX_SEQUENCE, [
    { key: Buffer.from('a'), value: Buffer.from('SYNTHETIC_A') },
    { key: Buffer.from('b'), value: Buffer.from('SYNTHETIC_B') }
  ])
  if (name === 'wal_zero_sequence_empty_count') batch = writeBatch(0n, [])
  const wal = batch ? logRecords([batch]) : Buffer.alloc(0)
  const manifest = logRecords([logicalManifest])
  const files = new Map([
    ['CURRENT', Buffer.from('MANIFEST-000001\n')],
    ['MANIFEST-000001', manifest], ['000002.log', wal], ['000003.ldb', builtTable.bytes]
  ])
  return { name, files, table: builtTable.bytes, manifest, logicalManifest, wal, batch,
    expected: name === 'valid' ? 'accept' : 'reject',
    nativeLiveTableCount: 1,
    note: 'Only synthetic values and exact newly-created TEMP file names.' }
}
function writeTempCase (name) {
  const fixture = buildCase(name)
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'larj-leveldb-review-'))
  for (const [name, bytes] of fixture.files) fs.writeFileSync(path.join(root, name), bytes, { flag: 'wx' })
  return { root, name: fixture.name, expected: fixture.expected,
    files: [...fixture.files].map(([name, bytes]) => ({ name, size: bytes.length })) }
}

module.exports = { names, buildCase, writeTempCase, crc32c, maskedCrc, vi, str,
  internalKey, rawBlock, physicalBlock, table, oneFilter, logRecords, writeBatch,
  manifestHead, addFile, deleteFile, MAX_SEQUENCE }
if (require.main === module) {
  if (process.argv[2] === '--materialize' && process.argv.length === 4) {
    process.stdout.write(JSON.stringify(writeTempCase(process.argv[3])) + '\n')
  } else if (process.argv.length === 2 || process.argv[2] === '--list') {
    process.stdout.write(JSON.stringify({ schema: 'larj.leveldb-review-fixtures.v1', names,
      crc32cStandardVector: crc32c(Buffer.from('123456789')).toString(16) }) + '\n')
  } else {
    process.stderr.write('fixture_arguments_invalid\n'); process.exitCode = 1
  }
}
