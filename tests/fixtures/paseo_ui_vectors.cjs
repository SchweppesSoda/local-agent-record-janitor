'use strict'
// Pure-function vectors. No LevelDB opens and no live/user records.
const path = require('node:path')
const fs = require('node:fs')
const crypto = require('node:crypto')
const assert = require('node:assert/strict')
const dir = path.resolve(__dirname, '../../src/local_agent_record_janitor/leveldb_runtime')
const helper = require(path.join(dir, 'office_drafts.cjs'))
const cleaner = require(path.join(dir, 'paseo_ui.cjs'))({ ...helper, fail: code => { throw new Error(code) } })
const sourceHash = crypto.createHash('sha256').update(fs.readFileSync(path.join(dir, 'paseo_ui.cjs'))).digest('hex')
const id = '11111111-1111-4111-8111-111111111111'
const selected = new Set([id])
const key = `agent:server-a:${id}`
const draft = text => ({ input: { text, attachments: [] }, lifecycle: 'active', updatedAt: 1, version: 1 })
const tab = (tabId, target, state) => ({ tabId, target, createdAt: 1, ...(state === undefined ? {} : { state }) })
const layout = (tabs, tabIds = tabs.map(t => t.tabId), focusedTabId = tabIds.at(-1) ?? null) => ({
  root: { kind: 'pane', pane: { id: 'p', tabIds, focusedTabId, tabs } }, focusedPaneId: 'p'
})
const state = (local, extra = {}) => ({ version: 2, state: { layoutByWorkspace: { 'server-a:w': local }, ...extra } })
function transform(name, value, raw = false) {
  const before = helper.encodeString(raw ? value : JSON.stringify(value))
  const result = cleaner(name, selected, 'server-a')(before)
  return { before, ...result, value: JSON.parse(helper.decodeString(result.after)) }
}
const results = []
function vector(name, test) {
  try { test(); results.push({ name, passed: true }) }
  catch (error) { results.push({ name, passed: false, error: error.message.split('\n')[0] }) }
}
function blocked(name, value, raw = false) {
  assert.throws(() => transform(name, value, raw), /office_leveldb_paseo_(schema_unverified|attachment_closure_unverified)/)
}
const ownedTab = tab('owned', { kind: 'agent', agentId: id })
vector('trimmed typed tab/focus/parent-map removed together', () => {
  const input = layout([tab(' owned ', { kind: 'agent', agentId: ` ${id} ` }), tab('keep', { kind: 'file', path: 'foo' })], ['owned', 'keep'], ' owned ')
  input.parentTabIdByTabId = { ' owned ': 'keep', keep: ' owned ' }
  const after = transform('workspace-layout-state', state(input)).value.state.layoutByWorkspace['server-a:w']
  assert.deepEqual(after.root.pane.tabIds, ['keep'])
  assert.equal(after.root.pane.focusedTabId, 'keep')
  assert.equal(after.root.pane.tabs.length, 1)
  assert.deepEqual(after.parentTabIdByTabId, {})
})
vector('remote same agent ID preserved', () => {
  const remote = layout([tab('remote', { kind: 'agent', agentId: id }, { untouched: [1, true, null] })])
  const value = state(layout([ownedTab]))
  value.state.layoutByWorkspace['server-b:w'] = remote
  const after = transform('workspace-layout-state', value).value
  assert.deepEqual(after.state.layoutByWorkspace['server-b:w'], remote)
})
vector('legacy no-tabs agent-looking ID retained byte-for-byte', () => {
  const legacy = { root: { kind: 'pane', pane: { id: 'p', tabIds: [`agent_${id}`], focusedTabId: `agent_${id}` } }, focusedPaneId: 'p' }
  const result = transform('workspace-layout-state', state(legacy))
  assert.equal(result.pairs.length, 0)
  assert.deepEqual(result.after, result.before)
})
vector('normalized duplicate typed tab IDs blocked', () => {
  const input = layout([tab('x', { kind: 'agent', agentId: id }), tab(' x ', { kind: 'file', path: 'foo' })])
  blocked('workspace-layout-state', state(input))
})
vector('selected canonical text draft removed; remote/modal/draft IDs retained', () => {
  const value = { version: 5, state: { drafts: { [key]: draft('selected'), [`agent:server-b:${id}`]: draft('remote'), [`draft:server-a:${id}`]: draft('unowned') }, createModalDraft: draft('modal') } }
  const after = transform('paseo-drafts', value).value
  assert.equal(Object.hasOwn(after.state.drafts, key), false)
  assert.deepEqual(after.state.drafts[`agent:server-b:${id}`], value.state.drafts[`agent:server-b:${id}`])
  assert.deepEqual(after.state.drafts[`draft:server-a:${id}`], value.state.drafts[`draft:server-a:${id}`])
  assert.deepEqual(after.state.createModalDraft, value.state.createModalDraft)
})
vector('unknown retained draft field blocked', () => {
  const value = { version: 5, state: { drafts: { [key]: draft('selected'), retained: { ...draft('retained'), futureAgentBinding: id } } } }
  blocked('paseo-drafts', value)
})
vector('unknown retained target field blocked', () => {
  blocked('workspace-layout-state', state(layout([ownedTab, tab('file', { kind: 'file', path: 'foo', futureAgentBinding: id })])))
})
vector('unknown remote layout field blocked before any rewrite', () => {
  const value = state(layout([ownedTab]))
  value.state.layoutByWorkspace['server-b:w'] = { ...layout([tab('remote', { kind: 'file', path: 'foo' })]), futureAgentBinding: id }
  blocked('workspace-layout-state', value)
})
vector('wrong optional splitSizes map shape blocked', () => {
  blocked('workspace-layout-state', state(layout([ownedTab]), { splitSizesByWorkspace: 'wrong' }))
})
vector('present null optional drafts map blocked', () => {
  blocked('paseo-drafts', { version: 5, state: { drafts: null } })
})
vector('present null pinned map blocked', () => {
  blocked('workspace-layout-state', state(layout([ownedTab]), { pinnedAgentIdsByWorkspace: null }))
})
vector('present null parent map blocked', () => {
  const input = layout([ownedTab])
  input.parentTabIdByTabId = null
  blocked('workspace-layout-state', state(input))
})
vector('stale tabIds cannot resurrect a formerly invisible draft', () => {
  blocked('workspace-layout-state', state(layout([ownedTab], ['owned', 'orphan'], 'owned')))
})
vector('nonfinite unselected JSON blocked instead of reserialized as null', () => {
  const value = JSON.stringify(state(layout([ownedTab, tab('file', { kind: 'file', path: 'foo' }, { untouched: '__number__' })]))).replace('"__number__"', '1e400')
  blocked('workspace-layout-state', value, true)
})
vector('negative zero and unsafe integer retained values blocked', () => {
  const value = JSON.stringify(state(layout([ownedTab, tab('file', { kind: 'file', path: 'foo' }, { untouched: '__number__' })])))
  blocked('workspace-layout-state', value.replace('"__number__"', '-0'), true)
  blocked('workspace-layout-state', value.replace('"__number__"', '9007199254740993'), true)
})
vector('invalid selected subagent and plugin targets cannot delete legacy fallback drafts', () => {
  for (const target of [
    { kind: 'provider_subagent', parentAgentId: id, subagentId: '  ' },
    { kind: 'plugin', pluginId: '', panelId: 'panel', context: 'agent', agentId: id },
    { kind: 'plugin', pluginId: 'plugin', panelId: '\t', context: 'agent', agentId: id }
  ]) blocked('workspace-layout-state', state(layout([tab('legacy-fallback', target)])))
})
vector('invalid retained targets cannot resurrect drafts when last valid owned tab is removed', () => {
  for (const target of [
    { kind: 'draft', draftId: '  ' }, { kind: 'terminal', terminalId: '' },
    { kind: 'browser', browserId: '\t' }, { kind: 'setup', workspaceId: ' ' },
    { kind: 'commit_diff', sha: '' }, { kind: 'file', path: '  ' }
  ]) blocked('workspace-layout-state', state(layout([ownedTab, tab('invalid-retained', target)])))
})
vector('native discarded whitespace pane and group identities blocked', () => {
  const input = layout([ownedTab])
  input.root.pane.id = '  '
  blocked('workspace-layout-state', state(input))
  const value = state(layout([ownedTab]))
  value.state.layoutByWorkspace['server-a:w'].root = { kind: 'group', group: { id: '\t', direction: 'horizontal', sizes: [1], children: [layout([ownedTab]).root] } }
  blocked('workspace-layout-state', value)
})
vector('every legal retained target and unselected explicit parent preserved', () => {
  const targets = [
    { kind: 'new_tab' },
    { kind: 'draft', draftId: id, setup: { provider: 'codex', cwd: 'D:/project', modeId: null, model: 'model', thinkingOptionId: null, featureValues: { a: true, b: 's', c: null } } },
    { kind: 'provider_subagent', parentAgentId: 'different-parent', subagentId: id },
    { kind: 'terminal', terminalId: id }, { kind: 'browser', browserId: id },
    { kind: 'changes_tree' }, { kind: 'files' }, { kind: 'pull_request' },
    { kind: 'file', path: 'D:/file', lineStart: 2, lineEnd: 3 },
    { kind: 'working_diff', focusPath: 'file', focusRequestId: 1, mode: 'base', baseRef: 'main', ignoreWhitespace: false },
    { kind: 'setup', workspaceId: id }, { kind: 'commit_diff', sha: id },
    { kind: 'plugin', pluginId: id, panelId: id, context: 'workspace' },
    { kind: 'plugin', pluginId: 'plugin', panelId: 'panel', context: 'agent', agentId: 'different-agent' }
  ]
  const retained = targets.map((target, i) => tab(`retain-${i}`, target, { opaque: [true, null, { text: id }] }))
  const result = transform('workspace-layout-state', state(layout([ownedTab, ...retained])))
  assert.equal(result.pairs.length, 1)
  assert.deepEqual(result.value.state.layoutByWorkspace['server-a:w'].root.pane.tabs, retained)
})
vector('explicit empty tabs keep legacy draft IDs byte-for-byte', () => {
  const result = transform('workspace-layout-state', state(layout([], [`agent_${id}`])))
  assert.equal(result.pairs.length, 0)
  assert.deepEqual(result.after, result.before)
})
vector('legacy flat and nested incomplete drafts blocked rather than guessed', () => {
  for (const record of [{ text: 'legacy', attachments: [] }, { input: { text: 'legacy', attachments: [] } }]) {
    blocked('paseo-drafts', { version: 5, state: { drafts: { [key]: record } } })
  }
})
console.log(JSON.stringify({ sourceHash, total: results.length, passed: results.filter(r => r.passed).length, results }))
