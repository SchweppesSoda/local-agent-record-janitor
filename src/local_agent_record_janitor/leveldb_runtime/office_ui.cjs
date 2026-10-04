'use strict'

// Exact native UI keys in QwenWork CN 1.2.5 / QoderWork CN 0.9.18.
// Values never become paths or execution instructions.
module.exports = function operations ({ decodeString, encodeString, mapEntries, cleanDraft, fail }) {
  return function cleaner (name, chats, children) {
    const both = new Set([...chats, ...children])
    function strict (raw) {
      const text = decodeString(raw)
      mapEntries('{"value":' + text + '}') // Strict duplicates/depth/budget gate.
      return { text, value: JSON.parse(text) }
    }
    function map (ids) {
      return raw => {
        const entries = mapEntries(decodeString(raw)), removed = entries.filter(item => ids.has(item.key))
        return { after: encodeString('{' + entries.filter(item => !ids.has(item.key)).map(item => item.raw).join(',') + '}', raw[0]),
          pairs: removed.map(item => [item.key]) }
      }
    }
    function array (ids) {
      return raw => {
        const { text, value } = strict(raw)
        if (!Array.isArray(value) || value.length > 20000 || value.some(item => typeof item !== 'string')) fail('office_leveldb_ui_schema_unverified')
        const tokens = text.match(/"(?:[^"\\]|\\.)*"/g) || []
        if (tokens.length !== value.length) fail('office_leveldb_ui_schema_unverified')
        return { after: encodeString('[' + tokens.filter((_, i) => !ids.has(value[i])).join(',') + ']', raw[0]),
          pairs: value.filter(item => ids.has(item)).map(item => [item]) }
      }
    }
    function whole (owner, kind) {
      return raw => {
        const { value } = strict(raw)
        const valid = kind === 'array' ? Array.isArray(value) && value.every(item => typeof item === 'string')
          : kind === 'active' ? value === null || typeof value === 'string'
            : value && typeof value === 'object' && !Array.isArray(value)
        if (!valid) fail('office_leveldb_ui_schema_unverified')
        return { after: null, pairs: [[owner]] }
      }
    }
    if (name === 'agent-drafts-global') return raw => cleanDraft(raw, chats)
    if (/^agent-pinned-chats-.{1,256}$/.test(name)) return array(chats)
    const sub = /^(?:[A-Za-z0-9_-]{1,128}:)?agent-(active|open|pinned)-sub-chats-(.+)$/.exec(name)
    if (sub) return chats.has(sub[2]) ? whole(sub[2], sub[1] === 'active' ? 'active' : 'array') : raw => ({ after: raw, pairs: [] })
    const layout = /^(?:workbench-layout|panel-layout):(.+)$/.exec(name)
    if (layout && name !== 'panel-layout:states') return children.has(layout[1]) ? whole(layout[1], 'object') : raw => ({ after: raw, pairs: [] })
    if (/^(?:[A-Za-z0-9_-]{1,128}:)?agents:app-view$/.test(name)) return raw => {
      const { value } = strict(raw)
      if (!value || typeof value !== 'object' || Array.isArray(value) || typeof value.type !== 'string') fail('office_leveldb_ui_schema_unverified')
      return value.type === 'chat' && chats.has(value.chatId) ? { after: null, pairs: [[value.chatId]] } : { after: raw, pairs: [] }
    }
    if (['chatInput:contextSelections', 'legokit:sessionContexts'].includes(name)) return map(both)
    if (['agents:userSelectedDirectories', 'agents:chatLastVisitedAt'].includes(name)) return map(chats)
    if (['panel-layout:states', 'agents:subChatModes', 'agents:subChatModelLevels', 'agents:memoryChanges', 'agents:prunedArtifactPaths', 'agents:rightSidebarPreferencePerSubChat'].includes(name)) return map(children)
    if (name === 'agents:subChatUnseenChanges') return array(children)
    if (name === 'agents:unseenChanges') return array(chats)
    return null
  }
}
