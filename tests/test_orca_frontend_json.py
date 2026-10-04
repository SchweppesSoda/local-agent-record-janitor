from copy import deepcopy
import json
import unittest

from local_agent_record_janitor import orca_frontend_json as frontend
from local_agent_record_janitor.orca_journal_cleanup import OrcaFrontendError


def fixture():
    def tab(sid, group):
        return {"id": "tab-" + sid, "entityId": sid, "contentType": "agent-session", "groupId": group,
                "worktreeId": "worktree", "label": "PRIVATE_" + sid, "customLabel": None,
                "color": None, "sortOrder": 0, "createdAt": 1, "agentSessionAgent": "codex"}
    def group(sid, gid):
        return {"id": gid, "worktreeId": "worktree", "activeTabId": "tab-" + sid,
                "tabOrder": ["tab-" + sid], "recentTabIds": ["tab-" + sid]}
    workspace = {"activeRepoId": None, "activeWorktreeId": "worktree", "activeTabId": "tab-selected",
        "tabsByWorktree": {}, "terminalLayoutsByTabId": {},
        "unifiedTabs": {"worktree": [tab("selected", "g1"), tab("other", "g2")]},
        "tabGroups": {"worktree": [group("selected", "g1"), group("other", "g2")]},
        "tabGroupLayouts": {"worktree": {"type": "split", "direction": "horizontal", "ratio": 0.4,
            "first": {"type": "leaf", "groupId": "g1"}, "second": {"type": "leaf", "groupId": "g2"}}},
        "activeGroupIdByWorktree": {"worktree": "g1"}, "activeTabIdByWorktree": {"worktree": "tab-selected"}}
    remote = json.loads(json.dumps(workspace).replace('"worktree"', '"remote-worktree"'))
    return {"schemaVersion": 1, "settings": {"auth": "PRESERVE_SECRET"}, "workspaceSession": workspace,
        "workspaceSessionsByHostId": {"ssh:remote": remote},
        "mobileClientTabSelectionsByDeviceId": {"phone": {"worktree": {"activeTabId": "tab-selected", "activeGroupId": "g1",
            "activeTabIdByGroupId": {"g1": "tab-selected", "g2": "tab-other"}}}}}


class OrcaFrontendJsonTests(unittest.TestCase):
    def test_remove_only_local_selected_tab_and_collapse_group_preserving_remote(self):
        value = fixture()
        raw = (json.dumps(value) + "\n").encode()
        result = frontend.transform(raw, family="profile_json", selected={"selected"}, known_tabs={"tab-selected"})
        after = json.loads(result)
        workspace = after["workspaceSession"]
        self.assertEqual(workspace["unifiedTabs"]["worktree"], value["workspaceSession"]["unifiedTabs"]["worktree"][1:])
        self.assertEqual(workspace["tabGroupLayouts"]["worktree"], {"type": "leaf", "groupId": "g2"})
        self.assertEqual(workspace["activeGroupIdByWorktree"]["worktree"], "g2")
        self.assertIsNone(workspace["activeTabId"])
        self.assertEqual(after["workspaceSessionsByHostId"], value["workspaceSessionsByHostId"])
        self.assertEqual(after["settings"], value["settings"])
        self.assertEqual(frontend.transform(result, family="profile_json", selected={"selected"}, known_tabs={"tab-selected"}), result)

    def test_clear_keeps_a_reused_tab_now_showing_an_unselected_session(self):
        value = fixture()
        value["workspaceSession"]["unifiedTabs"]["worktree"][0]["entityId"] = "replacement"
        after = frontend.ui_state(value, {"selected"}, {"tab-selected"})
        self.assertEqual(after, value)

    def test_sleeping_native_without_a_proven_root_never_claims_an_empty_closure(self):
        value = fixture()
        value["workspaceSession"]["sleepingAgentSessionsByPaneKey"] = {
            "pane": {"agent": "codex", "providerSession": {"key": "session_id", "id": "native-selected"}}}
        with self.assertRaisesRegex(OrcaFrontendError, "sleeping_session_binding_unverified"):
            frontend.ui_state(value, {"selected"}, native_ids={"native-selected"})

    def test_legacy_foreign_host_header_cannot_leave_local_selected_restore_record(self):
        value = {"schemaVersion": 2, "hostId": "old-host-identifier", "records": {"selected": {"private": "remove"}, "other": {"private": "keep"}},
            "unusableRecords": {"selected": {"reason": "shape", "raw": "remove"}}, "operations": {}, "retiredClaimKeys": [{"keyId": "retain", "retiredAt": 1}],
            "sessionTabs": [{"tabId": "old", "sessionId": "selected"}, {"tabId": "keep", "sessionId": "other"}], "visibleSessionIds": ["selected", "other"]}
        result = frontend.legacy_state(value, {"selected"})
        self.assertEqual(result["records"], {"other": {"private": "keep"}})
        self.assertEqual(result["unusableRecords"], {})
        self.assertEqual(result["retiredClaimKeys"], value["retiredClaimKeys"])
        self.assertEqual(result["sessionTabs"], value["sessionTabs"][1:])
        self.assertEqual(result["visibleSessionIds"], ["other"])

    def test_mobile_same_group_id_in_another_worktree_is_preserved(self):
        value = fixture()
        other = {"activeTabId": "tab-neighbor", "activeGroupId": "g1", "activeTabIdByGroupId": {"g1": "tab-neighbor"}}
        value["mobileClientTabSelectionsByDeviceId"]["phone"]["other-worktree"] = other
        result = frontend.ui_state(value, {"selected"})
        self.assertEqual(result["mobileClientTabSelectionsByDeviceId"]["phone"]["other-worktree"], other)
        self.assertIsNone(result["mobileClientTabSelectionsByDeviceId"]["phone"]["worktree"]["activeGroupId"])

    def test_inconsistent_group_cannot_drop_a_kept_tabs_layout(self):
        value = fixture()
        value["workspaceSession"]["unifiedTabs"]["worktree"][1]["groupId"] = "g1"
        with self.assertRaisesRegex(OrcaFrontendError, "membership_inconsistent"):
            frontend.ui_state(value, {"selected"})

    def test_ui_pane_keys_derive_from_logical_session_and_actual_tab_id(self):
        import hashlib
        value = fixture()
        digest = hashlib.sha256(b"selected").hexdigest()[:32]
        key = f"tab-selected:{digest[:8]}-{digest[8:12]}-4{digest[13:16]}-a{digest[17:20]}-{digest[20:]}"
        value["ui"] = {"acknowledgedAgentsByPaneKey": {key: 1, "unrelated": 2}}
        with self.assertRaisesRegex(OrcaFrontendError, "ui_pane_owner_ambiguous"):
            frontend.ui_state(value, {"selected"})
        value["workspaceSessionsByHostId"] = {}
        self.assertEqual(frontend.ui_state(value, {"selected"})["ui"], {"acknowledgedAgentsByPaneKey": {"unrelated": 2}})

    def test_shared_hostless_mobile_identity_refuses_before_deletion(self):
        value = fixture()
        value["workspaceSessionsByHostId"]["ssh:remote"] = deepcopy(value["workspaceSession"])
        with self.assertRaisesRegex(OrcaFrontendError, "mobile_selection_host_ambiguous"):
            frontend.ui_state(value, {"selected"})


if __name__ == "__main__":
    unittest.main()
