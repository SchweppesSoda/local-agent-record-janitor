"""Exact Orca structured-chat JSON projections, never whole-profile removal."""
from __future__ import annotations

from copy import deepcopy
import hashlib

from .orca_journal_cleanup import decode, encode, fail, operation, record_references, text

_WORKSPACE_FIELDS = set("""
activeRepoId activeWorkspaceKey activeWorkspaceExecutionHostId activeWorktreeId activeTabId
tabsByWorktree terminalLayoutsByTabId localOnlyScrollbackByTabId activeWorktreeIdsOnShutdown
openFilesByWorktree activeFileIdByWorktree markdownFrontmatterVisible browserTabsByWorktree
browserPagesByWorkspace activeBrowserTabIdByWorktree clientHostedBrowserPagesByWorktree
clientHostedBrowserCloseIntentsByEnvironment activeTabTypeByWorktree browserUrlHistory workspaceDocHistory
activeTabIdByWorktree unifiedTabs tabGroups tabGroupLayouts activeGroupIdByWorktree
activeConnectionIdsAtShutdown remoteSessionIdsByTabId lastVisitedAtByWorktreeId
defaultTerminalTabsAppliedByWorktreeId sleepingAgentSessionsByPaneKey terminalPtyIncarnationsByPaneKey
terminalTopologyRevisionByRepoId terminalSurfaceTombstonesByPaneKey closedTerminalTabTombstonesByTabId
""".split())


def mapping(value):
    if not isinstance(value, dict):
        fail("frontend_mapping_unverified")
    return value


def sequence(value):
    if not isinstance(value, list):
        fail("frontend_sequence_unverified")
    return value


def strings(value):
    result = sequence(value)
    if any(not text(item) for item in result):
        fail("frontend_identifiers_unverified")
    return result


def legacy_state(value, selected):
    value = deepcopy(mapping(value))
    allowed = {"schemaVersion", "hostId", "records", "operations", "retiredClaimKeys", "unusableRecords", "sessionTabs", "visibleSessionIds"}
    if value.get("schemaVersion") != 2 or not text(value.get("hostId")) or set(value) - allowed:
        fail("legacy_records_schema_unverified")
    for key in ("records", "unusableRecords"):
        value[key] = {sid: record for sid, record in mapping(value.get(key)).items() if sid not in selected}
        for sid, item in value[key].items():
            if key == "records":
                value[key][sid] = record_references(item, selected)
            else:
                item = mapping(item)
                value[key][sid] = {**item, "raw": record_references(item.get("raw"), selected)}
    value["operations"] = {key: operation(key, row, selected) for key, row in mapping(value.get("operations")).items()}
    sequence(value.get("retiredClaimKeys"))
    if "sessionTabs" in value:
        kept, seen_tabs, seen_sessions = [], set(), set()
        for row in sequence(value["sessionTabs"]):
            if (not isinstance(row, dict) or set(row) != {"tabId", "sessionId"}
                    or not text(row["tabId"]) or not text(row["sessionId"])
                    or row["tabId"] in seen_tabs or row["sessionId"] in seen_sessions):
                fail("legacy_tabs_unverified")
            seen_tabs.add(row["tabId"]); seen_sessions.add(row["sessionId"])
            if row["sessionId"] not in selected:
                kept.append(row)
        value["sessionTabs"] = kept
    if "visibleSessionIds" in value:
        value["visibleSessionIds"] = [sid for sid in strings(value["visibleSessionIds"]) if sid not in selected]
    return value


def _layout(value, removed, depth=0):
    value = mapping(value)
    if depth > 64:
        fail("group_layout_budget_exceeded")
    if value.get("type") == "leaf" and set(value) == {"type", "groupId"} and text(value["groupId"]):
        return None if value["groupId"] in removed else value
    if (value.get("type") != "split" or set(value) - {"type", "direction", "first", "second", "ratio"}
            or value.get("direction") not in {"horizontal", "vertical"}):
        fail("group_layout_unverified")
    if "ratio" in value and (type(value["ratio"]) not in {int, float} or not 0 < value["ratio"] < 1):
        fail("group_layout_unverified")
    first, second = _layout(value.get("first"), removed, depth + 1), _layout(value.get("second"), removed, depth + 1)
    if first is None:
        return second
    if second is None:
        return first
    return {**value, "first": first, "second": second}


def workspace(value, selected, known_tabs, native_ids):
    value = deepcopy(mapping(value))
    if set(value) - _WORKSPACE_FIELDS:
        fail("workspace_restore_fields_unverified")
    removed_tabs, kept_tabs = set(), set()
    worktree_tabs, worktree_groups = {}, {}
    for worktree, entries in mapping(value.get("unifiedTabs", {})).items():
        kept = []
        for tab in sequence(entries):
            tab = mapping(tab)
            if (not all(text(tab.get(key)) for key in ("id", "entityId", "contentType", "groupId", "worktreeId"))
                    or tab["worktreeId"] != worktree):
                fail("unified_tab_unverified")
            local = tab.get("executionHostId", "local") == "local"
            if local and tab["contentType"] == "agent-session" and tab["entityId"] in selected:
                removed_tabs.add(tab["id"])
                worktree_tabs.setdefault(worktree, set()).add(tab["id"])
            else:
                # A native title on a terminal is not proof that its resume
                # argv, buffers and provider home belong to this selection.
                title = tab.get("aiVaultTitle")
                if local and isinstance(title, dict) and title.get("agent") == "codex" and title.get("sessionId") in native_ids:
                    fail("terminal_restore_binding_unverified")
                kept.append(tab)
                kept_tabs.add(tab["id"])
        entries[:] = kept
    if removed_tabs & kept_tabs:
        fail("tab_identity_ambiguous")
    # Current entity membership wins over an old tab ID retained by /clear.
    removed_tabs.update(set(known_tabs) - kept_tabs)
    removed_groups = set()
    for worktree, groups in mapping(value.get("tabGroups", {})).items():
        kept_groups = []
        local_removed = set()
        for group in sequence(groups):
            group = mapping(group)
            if (not text(group.get("id")) or group.get("worktreeId") != worktree
                    or set(group) - {"id", "worktreeId", "activeTabId", "tabOrder", "recentTabIds"}):
                fail("tab_group_unverified")
            before = strings(group.get("tabOrder"))
            worktree_tabs.setdefault(worktree, set()).update(set(before) & removed_tabs)
            order = [sid for sid in before if sid not in removed_tabs]
            group["tabOrder"] = order
            if "recentTabIds" in group:
                group["recentTabIds"] = [sid for sid in strings(group["recentTabIds"]) if sid not in removed_tabs]
            if group.get("activeTabId") in removed_tabs:
                group["activeTabId"] = (group.get("recentTabIds") or order or [None])[-1]
            if before and not order:
                if any(tab["groupId"] == group["id"] for tab in value.get("unifiedTabs", {}).get(worktree, ())):
                    fail("tab_group_membership_inconsistent")
                local_removed.add(group["id"])
            else:
                kept_groups.append(group)
        groups[:] = kept_groups
        removed_groups.update(local_removed)
        worktree_groups[worktree] = local_removed
        layouts = mapping(value.get("tabGroupLayouts", {}))
        if worktree in layouts and local_removed:
            after = _layout(layouts[worktree], local_removed)
            if after is None:
                del layouts[worktree]
            else:
                layouts[worktree] = after
        active = mapping(value.get("activeGroupIdByWorktree", {}))
        if active.get(worktree) in local_removed:
            if kept_groups:
                active[worktree] = kept_groups[0]["id"]
            else:
                active.pop(worktree, None)
    if value.get("activeTabId") in removed_tabs:
        value["activeTabId"] = None
    for key in ("activeTabIdByWorktree",):
        for owner, identifier in mapping(value.get(key, {})).items():
            if identifier in worktree_tabs.get(owner, set()):
                value[key][owner] = None
    for record in mapping(value.get("sleepingAgentSessionsByPaneKey", {})).values():
        record = mapping(record)
        provider = mapping(record.get("providerSession"))
        if record.get("agent") == "codex" and provider.get("id") in native_ids:
            fail("sleeping_session_binding_unverified")
    # A structured session does not own terminal scrollback. Encountering such
    # a cross-model attachment requires the separate terminal binding contract.
    for key in ("terminalLayoutsByTabId", "localOnlyScrollbackByTabId", "remoteSessionIdsByTabId"):
        if set(mapping(value.get(key, {}))) & removed_tabs:
            fail("structured_tab_terminal_attachment_unverified")
    return value, worktree_tabs, worktree_groups


def ui_state(value, selected, known_tabs=(), native_ids=()):
    value = deepcopy(mapping(value))
    removed_tabs, removed_groups = {}, {}
    def merge(target, values):
        for key, items in values.items():
            target.setdefault(key, set()).update(items)
    pane_keys = set()
    def pane(tab, sid):
        digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]
        return f"{tab}:{digest[:8]}-{digest[8:12]}-4{digest[13:16]}-a{digest[17:20]}-{digest[20:]}"
    if isinstance(known_tabs, dict):
        pane_keys.update(pane(tab, sid) for tab, sids in known_tabs.items() for sid in sids if sid in selected)
    def clean_workspace(before):
        for entries in mapping(before.get("unifiedTabs", {})).values():
            for tab in sequence(entries):
                if (tab.get("executionHostId", "local") == "local" and tab.get("contentType") == "agent-session"
                        and tab.get("entityId") in selected):
                    pane_keys.add(pane(tab["id"], tab["entityId"]))
        return workspace(before, selected, known_tabs, set(native_ids))
    if "workspaceSession" in value:
        value["workspaceSession"], tabs, groups = clean_workspace(mapping(value["workspaceSession"]))
        merge(removed_tabs, tabs); merge(removed_groups, groups)
    if "workspaceSessionsByHostId" in value:
        hosts = mapping(value["workspaceSessionsByHostId"])
        if "local" in hosts:
            hosts["local"], tabs, groups = clean_workspace(mapping(hosts["local"]))
            merge(removed_tabs, tabs); merge(removed_groups, groups)
        # Remote namespaces, including same raw session IDs, are preserved.
    retained_panes, remote_tabs, remote_groups = set(), {}, {}
    def observe_retained(state, *, remote=False):
        for worktree, entries in mapping(mapping(state).get("unifiedTabs", {})).items():
            for tab in sequence(entries):
                tab = mapping(tab)
                if tab.get("contentType") == "agent-session" and text(tab.get("id")) and text(tab.get("entityId")):
                    retained_panes.add(pane(tab["id"], tab["entityId"]))
                if remote or tab.get("executionHostId", "local") != "local":
                    remote_tabs.setdefault(worktree, set()).add(tab.get("id"))
                    remote_groups.setdefault(worktree, set()).add(tab.get("groupId"))
    if "workspaceSession" in value:
        observe_retained(value["workspaceSession"])
    for host, state in mapping(value.get("workspaceSessionsByHostId", {})).items():
        observe_retained(state, remote=host != "local")
    for devices in mapping(value.get("mobileClientTabSelectionsByDeviceId", {})).values():
        for worktree, selection in mapping(devices).items():
            mapping(selection)
            if set(selection) != {"activeTabId", "activeGroupId", "activeTabIdByGroupId"}:
                fail("mobile_selection_unverified")
            local_tabs, local_groups = removed_tabs.get(worktree, set()), removed_groups.get(worktree, set())
            touched_tabs = ({selection["activeTabId"]} | set(mapping(selection["activeTabIdByGroupId"]).values())) & local_tabs
            touched_groups = ({selection["activeGroupId"]} | set(selection["activeTabIdByGroupId"])) & local_groups
            if touched_tabs & remote_tabs.get(worktree, set()) or touched_groups & remote_groups.get(worktree, set()):
                fail("mobile_selection_host_ambiguous")
            if selection["activeTabId"] in local_tabs:
                selection["activeTabId"] = None
            if selection["activeGroupId"] in local_groups:
                selection["activeGroupId"] = None
            selection["activeTabIdByGroupId"] = {gid: tid for gid, tid in mapping(selection["activeTabIdByGroupId"]).items()
                if gid not in local_groups and tid not in local_tabs}
    if "ui" in value:
        ui = mapping(value["ui"])
        for key in ("acknowledgedAgentsByPaneKey", "activityClearedAtByPaneKey", "manuallyUnreadTurnsByPaneKey"):
            if key in ui:
                if set(mapping(ui[key])) & pane_keys & retained_panes:
                    fail("ui_pane_owner_ambiguous")
                ui[key] = {key: item for key, item in mapping(ui[key]).items() if key not in pane_keys}
    return value


def transform(raw, *, family, selected, known_tabs=(), native_ids=()):
    value = decode(raw)
    if family == "legacy_records":
        after = legacy_state(value, selected)
    elif family == "profile_json":
        if mapping(value).get("schemaVersion") != 1:
            fail("profile_json_schema_unverified")
        after = ui_state(value, selected, known_tabs, native_ids)
    else:
        fail("json_family_unverified")
    return raw if after == value else (encode(after) + "\n").encode("utf-8")
