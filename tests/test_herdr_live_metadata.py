from __future__ import annotations

import unittest

from local_agent_record_janitor.herdr_live_metadata import HerdrLiveMetadataError, decode_response, parse_detached_daemon, parse_live_snapshot, parse_pong
from tests.herdr_live_support import live_snapshot, pong, wire
from tests.herdr_support import SENTINEL


class HerdrLiveMetadataTests(unittest.TestCase):
    def test_detached_startup_observation_accepts_only_bool_or_unknown(self):
        for value in (True, False, None):
            self.assertIs(parse_detached_daemon({"capabilities": {"detached_server_daemon": value}}), value)
        self.assertIsNone(parse_detached_daemon({}))
        self.assertIsNone(parse_detached_daemon({"capabilities": {}}))
        for value in (0, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(HerdrLiveMetadataError, "live_detached_daemon_invalid"):
                parse_detached_daemon({"capabilities": {"detached_server_daemon": value}})
        with self.assertRaisesRegex(HerdrLiveMetadataError, "live_capabilities_invalid"):
            parse_detached_daemon({"capabilities": []})

    def parse(self, value):
        return parse_live_snapshot(decode_response(wire(value), "snapshot"), "0.9.3")

    def test_all_tabs_are_read_and_duplicate_agent_observation_does_not_double_references(self):
        observed = self.parse(live_snapshot("snapshot"))
        self.assertEqual(len(observed.observations), 2)
        self.assertEqual(len(observed.pane_states), 2)
        self.assertEqual(observed.errors, ())
        self.assertNotIn(SENTINEL, repr(observed))
        self.assertEqual(self.parse(live_snapshot("snapshot", sessions=())).observations, ())
        self.assertEqual(parse_pong(decode_response(wire(pong("ping")), "ping")), "0.9.3")

    def test_missing_layout_leaf_and_coherent_partial_pane_counts_remain_incomplete(self):
        def leaf(value):
            value["layouts"][1]["panes"] = []
        def partial(value):
            for field in ("panes", "agents", "layouts"):
                value[field].pop()
        def counts(value):
            value["tabs"][1]["pane_count"] = 2
            value["workspaces"][0]["tab_count"] = 3
        for change in (leaf, partial, counts):
            with self.subTest(change=change.__name__):
                value = live_snapshot("snapshot")
                change(value["result"]["snapshot"])
                observed = self.parse(value)
                self.assertTrue(observed.errors)
                self.assertTrue(observed.observations)

    def test_conflicting_agent_value_is_preserved_separately_and_unknown_version_is_not_verified(self):
        value = live_snapshot("snapshot")
        value["result"]["snapshot"]["agents"][0]["agent_session"]["value"] = "other-id"
        result = self.parse(value)
        self.assertEqual(len(result.observations), 3)
        self.assertIn("live_agent_reference_conflict", result.errors)
        for version, incomplete in (("99.0.0", True), ("0.9.3-preview.1+build.2", False)):
            value = live_snapshot("snapshot", version=version)
            result = parse_live_snapshot(decode_response(wire(value), "snapshot"), version)
            self.assertEqual(len(result.observations), 2)
            self.assertEqual("live_version_unverified" in result.errors, incomplete)

    def test_plain_shell_pane_is_not_required_to_have_an_agent_info(self):
        value = live_snapshot("snapshot")
        snapshot = value["result"]["snapshot"]
        snapshot["panes"][-1].pop("agent_session")
        snapshot["panes"][-1]["agent_status"] = "unknown"
        snapshot["agents"].pop()
        result = self.parse(value)
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(result.errors, ())

    def test_malformed_private_errors_and_deep_json_produce_only_fixed_codes(self):
        negatives = [b"\xff", b'{"id":"snapshot","id":"x","result":{}}',
            b'{"id":"snapshot","result":NaN}', b"[" * 1500 + b"]" * 1500,
            b'{"id":"snapshot","result":' + b"9" * 5000 + b"}",
            wire({"id": "snapshot", "error": {"message": SENTINEL}})]
        for data in negatives:
            with self.subTest(size=len(data)):
                with self.assertRaises(HerdrLiveMetadataError) as caught:
                    decode_response(data, "snapshot")
                self.assertNotIn(SENTINEL, str(caught.exception))
        for field, bad in (("protocol", True), ("protocol", 23), ("version", "0.9.4")):
            value = live_snapshot("snapshot")
            value["result"]["snapshot"][field] = bad
            with self.assertRaises(HerdrLiveMetadataError):
                self.parse(value)
