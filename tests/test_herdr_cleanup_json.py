import copy
import json
from pathlib import Path
import unittest

from local_agent_record_janitor.herdr_cleanup_json import (
    HerdrCleanupError, fingerprint, normalized, prune_snapshot, prune_history,
)
from tests.herdr_support import agent_session, pane, snapshot, tab


class HerdrCleanupJsonTests(unittest.TestCase):
    def fixture(self):
        cwd = Path("/synthetic/project")
        selected = pane(cwd, agent_session(), agent_resume={"source": "herdr:codex", "agent": "codex",
            "argv": ["codex", "resume", "PRIVATE_RESUME"]}, embedded="PRIVATE_EMBEDDED")
        survivor = pane(cwd, agent_session(value="33333333-3333-4333-8333-333333333333"), unknown="KEEP_UNKNOWN")
        value = snapshot(cwd, (tab({0: selected, 1: survivor}), tab({2: copy.deepcopy(selected)})))
        value["workspaces"][0]["active_tab"] = 1
        return value

    def test_owned_panes_and_embedded_recovery_erased_survivor_numbers_preserved(self):
        before = self.fixture()
        after, coordinates = prune_snapshot(before, {(0, 0, 0), (0, 1, 2)})
        workspace = after["workspaces"][0]
        self.assertEqual(len(workspace["tabs"]), 1)
        self.assertEqual(workspace["tabs"][0]["panes"], {"1": before["workspaces"][0]["tabs"][0]["panes"]["1"]})
        self.assertEqual(workspace["tabs"][0]["layout"], {"Pane": 1})
        self.assertEqual(workspace["public_pane_numbers"], {"1": 2})
        self.assertEqual(workspace["next_public_pane_number"], 4)
        self.assertEqual(workspace["public_tab_numbers"], [1])
        self.assertEqual(workspace["next_public_tab_number"], 3)
        self.assertEqual(workspace["active_tab"], 0)
        self.assertNotIn("PRIVATE", json.dumps(after))
        self.assertEqual(coordinates, [(0, [(0, {1})])])

    def test_history_uses_exact_old_coordinates_and_new_structural_fingerprint(self):
        before = self.fixture()
        after, coordinates = prune_snapshot(before, {(0, 0, 0), (0, 1, 2)})
        history = {"version": 3, "layout_fingerprint": fingerprint(before), "workspaces": [{"tabs": [
            {"panes": {"0": {"ansi": "PRIVATE", "lines": 1}, "1": {"ansi": "\u001b[31mKEEP", "lines": 2}}},
            {"panes": {"2": {"ansi": "PRIVATE", "lines": 1}}}]}]}
        result = prune_history(history, before, after, coordinates)
        self.assertEqual(result["layout_fingerprint"], fingerprint(after))
        self.assertEqual(result["workspaces"][0]["tabs"], [{"panes": {"1": {"ansi": "\u001b[31mKEEP", "lines": 2}}}])
        with self.assertRaisesRegex(HerdrCleanupError, "history_provenance_unverified"):
            prune_history({**history, "layout_fingerprint": "0" * 64}, before, after, coordinates)

    def test_unaffected_snapshot_is_identical_and_empty_selection_has_valid_defaults(self):
        before = self.fixture()
        unchanged, _ = prune_snapshot(before, set())
        self.assertEqual(unchanged, before)
        after, _ = prune_snapshot(before, {(0, 0, 0), (0, 0, 1), (0, 1, 2)})
        self.assertEqual(after["workspaces"], [])
        self.assertIsNone(after["active"])
        self.assertEqual(after["selected"], 0)

    def test_hash_projection_includes_defaults_and_normalizes_f32_and_sets(self):
        value = self.fixture()
        value["sidebar_section_split"] = 0.1
        value["collapsed_space_keys"] = ["z", "a", "z"]
        projection = normalized(value)
        self.assertEqual(projection["sidebar_section_split"], 0.10000000149011612)
        self.assertEqual(projection["collapsed_space_keys"], ["a", "z"])
        self.assertIsNone(projection["sidebar_width"])
        self.assertNotIn("unknown", projection["workspaces"][0]["tabs"][0]["panes"]["1"])
        self.assertEqual(fingerprint(value), fingerprint({**value, "unknown": "not in typed schema"}))

    def test_duplicate_numeric_keys_and_invalid_resume_are_rejected(self):
        value = self.fixture()
        value["workspaces"][0]["tabs"][0]["panes"]["00"] = pane(Path("/synthetic"))
        with self.assertRaises(HerdrCleanupError):
            normalized(value)
        value = self.fixture()
        value["workspaces"][0]["tabs"][0]["panes"]["0"]["agent_resume"]["argv"] = "not-an-array"
        with self.assertRaises(HerdrCleanupError):
            normalized(value)


if __name__ == "__main__":
    unittest.main()
