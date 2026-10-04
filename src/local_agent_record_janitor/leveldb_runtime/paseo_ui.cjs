'use strict'
// Fixed Paseo 05b0747 persistence schemas. Never infer ownership from tab IDs.
module.exports = ({ decodeString, encodeString, mapEntries, fail }) => {
  const bad = () => fail('office_leveldb_paseo_schema_unverified')
  const object = (value, allowed, required = []) => {
    if (!value || Array.isArray(value) || typeof value !== 'object' ||
        Object.keys(value).some(key => !allowed.includes(key)) || required.some(key => !(key in value))) bad()
  }
  const text = value => { if (typeof value !== 'string') bad(); return value }
  const identity = value => { const result = text(value).trim(); if (!result) bad(); return result }
  const map = value => { if (!value || Array.isArray(value) || typeof value !== 'object') bad(); return value }
  function parse (raw, version) {
    const source = decodeString(raw)
    mapEntries(source) // Reject duplicate fields, invalid tokens and excessive depth.
    const value = JSON.parse(source)
    let nodes = 0
    function safe (item) {
      if (++nodes > 200000) bad()
      if (typeof item === 'number' && (!Number.isFinite(item) || Object.is(item, -0) ||
          Number.isInteger(item) && !Number.isSafeInteger(item))) bad()
      if (item && typeof item === 'object') for (const child of Object.values(item)) safe(child)
    }
    safe(value)
    object(value, ['state', 'version'], ['state', 'version'])
    if (value.version !== version) bad()
    map(value.state)
    return value
  }
  const fields = ['pinnedAgentIdsByWorkspace', 'layoutByWorkspace', 'splitSizesByWorkspace',
    'explorerSidebarWidthByWorkspace', 'explorerSidebarRatioByWorkspace', 'sidePanelRatioByWorkspace',
    'explorerPaneIdByWorkspace', 'explorerSidebarPaneIdByWorkspace', 'sidePaneIdByWorkspace',
    'pullRequestTabAutoOpenedByWorkspace', 'acknowledgedPullRequestByWorkspace']
  return (name, ids, server) => {
    if (['@paseo:replica-cache', 'paseo-replica-cache', 'agent-timelines'].includes(name)) {
      return () => fail('office_leveldb_paseo_legacy_cache_unverified')
    }
    if (!['paseo-drafts', 'workspace-layout-state'].includes(name)) return null
    return raw => {
      const value = parse(raw, name === 'paseo-drafts' ? 5 : 2), pairs = []
      if (name === 'paseo-drafts') {
        object(value.state, ['drafts', 'createModalDraft'])
        function validateRecord (record) {
          object(record, ['input', 'lifecycle', 'updatedAt', 'version'], ['input', 'lifecycle', 'updatedAt', 'version'])
          object(record.input, ['text', 'attachments', 'cwd'], ['text', 'attachments'])
          text(record.input.text)
          if (!['active', 'abandoned', 'sent'].includes(record.lifecycle) || typeof record.updatedAt !== 'number' ||
              !Number.isSafeInteger(record.version) || record.version < 1 || !Array.isArray(record.input.attachments)) bad()
          if ('cwd' in record.input) text(record.input.cwd)
          // Until the complete external-file reference graph is qualified,
          // admission is deliberately limited to canonical text drafts.
          if (record.input.attachments.length) fail('office_leveldb_paseo_attachment_closure_unverified')
        }
        if (value.state.createModalDraft != null) validateRecord(value.state.createModalDraft)
        for (const [key, record] of Object.entries(map('drafts' in value.state ? value.state.drafts : {}))) {
          validateRecord(record)
          const match = [...ids].find(id => key === `agent:${server}:${id}`)
          if (!match) continue
          // External attachment files/Blob stores need a complete ownership
          // closure; deleting the reference alone cannot claim their removal.
          if (!Array.isArray(record.input.attachments) || record.input.attachments.length) {
            fail('office_leveldb_paseo_attachment_closure_unverified')
          }
          delete value.state.drafts[key]
          pairs.push([match])
        }
      } else {
        object(value.state, fields, ['layoutByWorkspace'])
        for (const field of fields) {
          if (!(field in value.state) || ['layoutByWorkspace', 'pinnedAgentIdsByWorkspace'].includes(field)) continue
          for (const item of Object.values(map(value.state[field]))) {
            if (field === 'splitSizesByWorkspace') {
              for (const sizes of Object.values(map(item))) if (!Array.isArray(sizes) || sizes.some(v => typeof v !== 'number')) bad()
            } else if (['explorerSidebarWidthByWorkspace', 'explorerSidebarRatioByWorkspace', 'sidePanelRatioByWorkspace'].includes(field)) {
              if (typeof item !== 'number') bad()
            } else if (['explorerPaneIdByWorkspace', 'explorerSidebarPaneIdByWorkspace', 'sidePaneIdByWorkspace'].includes(field)) {
              if (item !== null) text(item)
            } else if (field === 'pullRequestTabAutoOpenedByWorkspace') {
              if (item !== true) bad()
            } else text(item)
          }
        }
        function owner (target) {
          if (!target || typeof target.kind !== 'string') bad()
          const types = ['new_tab', 'draft', 'agent', 'provider_subagent', 'terminal', 'browser', 'changes_tree',
            'files', 'pull_request', 'file', 'working_diff', 'setup', 'commit_diff', 'plugin']
          if (!types.includes(target.kind)) bad()
          if (target.kind === 'agent') { object(target, ['kind', 'agentId'], ['agentId']); return identity(target.agentId) }
          if (target.kind === 'provider_subagent') {
            object(target, ['kind', 'parentAgentId', 'subagentId'], ['parentAgentId', 'subagentId'])
            identity(target.subagentId); return identity(target.parentAgentId)
          }
          if (target.kind === 'plugin') {
            object(target, ['kind', 'pluginId', 'panelId', 'context', 'agentId'], ['pluginId', 'panelId', 'context'])
            if (!['agent', 'workspace'].includes(target.context)) bad()
            identity(target.pluginId); identity(target.panelId)
            if (target.context === 'workspace' && 'agentId' in target) bad()
            return target.context === 'agent' ? identity(target.agentId) : null
          }
          const schema = {
            new_tab: [[], []], changes_tree: [[], []], files: [[], []], pull_request: [[], []],
            draft: [['draftId', 'setup'], ['draftId']], terminal: [['terminalId'], ['terminalId']],
            browser: [['browserId'], ['browserId']], file: [['path', 'lineStart', 'lineEnd'], ['path']],
            working_diff: [['focusPath', 'focusRequestId', 'mode', 'baseRef', 'ignoreWhitespace'], []],
            setup: [['workspaceId'], ['workspaceId']], commit_diff: [['sha'], ['sha']]
          }[target.kind]
          object(target, ['kind', ...schema[0]], schema[1])
          for (const key of ['draftId', 'terminalId', 'browserId', 'path', 'workspaceId', 'sha']) {
            if (key in target) identity(target[key])
          }
          if ('focusPath' in target) text(target.focusPath)
          for (const key of ['lineStart', 'lineEnd']) if (key in target && (!Number.isSafeInteger(target[key]) || target[key] <= 0)) bad()
          if ('focusRequestId' in target && typeof target.focusRequestId !== 'number' ||
              'mode' in target && !['uncommitted', 'base'].includes(target.mode) ||
              'baseRef' in target && target.baseRef !== null && typeof target.baseRef !== 'string' ||
              'ignoreWhitespace' in target && typeof target.ignoreWhitespace !== 'boolean') bad()
          if ('setup' in target) {
            const setup = target.setup
            const names = ['provider', 'cwd', 'modeId', 'model', 'thinkingOptionId', 'featureValues']
            object(setup, names, names)
            text(setup.provider); text(setup.cwd)
            for (const key of ['modeId', 'model', 'thinkingOptionId']) if (setup[key] !== null) text(setup[key])
            for (const value of Object.values(map(setup.featureValues))) if (value !== null && !['string', 'boolean'].includes(typeof value)) bad()
          }
          return null
        }
        let nodes = 0
        function walk (node, removed, seen, active, depth = 0) {
          if (++nodes > 20000 || depth > 64) bad()
          object(node, ['kind', node.kind === 'pane' ? 'pane' : 'group'], ['kind'])
          if (node.kind === 'group') {
            const group = node.group
            object(group, ['id', 'direction', 'children', 'sizes'], ['id', 'direction', 'children', 'sizes'])
            identity(group.id)
            if (!['horizontal', 'vertical'].includes(group.direction) || !Array.isArray(group.children) ||
                !Array.isArray(group.sizes) || group.sizes.some(v => typeof v !== 'number')) bad()
            for (const child of group.children) walk(child, removed, seen, active, depth + 1)
          } else if (node.kind === 'pane') {
            const pane = node.pane
            object(pane, ['id', 'tabIds', 'focusedTabId', 'tabs', 'hidden'], ['id', 'tabIds', 'focusedTabId'])
            identity(pane.id)
            if (pane.focusedTabId !== null) text(pane.focusedTabId)
            if ('hidden' in pane && typeof pane.hidden !== 'boolean') bad()
            if (!Array.isArray(pane.tabIds) || pane.tabIds.some(i => typeof i !== 'string')) bad()
            if ('tabs' in pane && !Array.isArray(pane.tabs)) bad()
            if (!pane.tabs?.length) {
              for (const id of pane.tabIds.map(id => id.trim())) {
                if (!id || seen.has(id)) bad()
                seen.add(id)
              }
              return // Upstream restores legacy IDs as draft tabs.
            }
            if (JSON.stringify(pane.tabIds.map(id => id.trim())) !== JSON.stringify(pane.tabs.map(tab => text(tab.tabId).trim()))) bad()
            const local = new Set()
            for (const tab of pane.tabs) {
              object(tab, ['tabId', 'target', 'createdAt', 'state'], ['tabId', 'target', 'createdAt'])
              if (typeof tab.createdAt !== 'number') bad()
              const tabId = text(tab.tabId).trim()
              if (!tabId || seen.has(tabId)) bad()
              seen.add(tabId)
              const id = owner(tab.target)
              if (active && id && ids.has(id)) { local.add(tabId); removed.add(tabId); pairs.push([id]) }
            }
            if (!local.size) return
            pane.tabs = pane.tabs.filter(tab => !local.has(tab.tabId.trim()))
            pane.tabIds = pane.tabIds.filter(id => !local.has(id.trim()))
            if (pane.focusedTabId !== null && local.has(text(pane.focusedTabId).trim())) pane.focusedTabId = pane.tabIds.at(-1) ?? null
          } else bad()
        }
        for (const [key, layout] of Object.entries(map(value.state.layoutByWorkspace))) {
          object(layout, ['root', 'focusedPaneId', 'parentTabIdByTabId'], ['root', 'focusedPaneId'])
          if (layout.focusedPaneId !== null) text(layout.focusedPaneId)
          const removed = new Set()
          walk(layout.root, removed, new Set(), key.startsWith(server + ':'))
          if ('parentTabIdByTabId' in layout) for (const [child, parent] of Object.entries(map(layout.parentTabIdByTabId))) {
            text(parent)
            if (removed.has(child.trim()) || removed.has(text(parent).trim())) delete layout.parentTabIdByTabId[child]
          }
        }
        for (const [key, pinned] of Object.entries(map('pinnedAgentIdsByWorkspace' in value.state ? value.state.pinnedAgentIdsByWorkspace : {}))) {
          if (!Array.isArray(pinned) || pinned.some(i => typeof i !== 'string')) bad()
          if (!key.startsWith(server + ':')) continue
          value.state.pinnedAgentIdsByWorkspace[key] = pinned.filter(id => {
            if (!ids.has(id.trim())) return true
            pairs.push([id.trim()]); return false
          })
        }
      }
      return { after: pairs.length ? encodeString(JSON.stringify(value), raw[0]) : raw, pairs }
    }
  }
}
