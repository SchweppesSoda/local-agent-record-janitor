from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor.adapters import CindyAdapter
from local_agent_record_janitor.client_inventory import build_client_inventory
from local_agent_record_janitor.path_identity import (
    canonical_existing_path_key, inventory_path_identity_scope,
)
from tests.support import create_cindy_database, create_thread_index, write_rollout


@unittest.skipUnless(os.name == "nt", "Windows physical identity IO")
class InventoryIdentityCacheTests(unittest.TestCase):
    def test_only_proven_exact_spellings_are_reused_within_nested_read_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.jsonl"
            path.touch()
            extended = "\\\\?\\" + str(path)
            with patch("local_agent_record_janitor.path_identity.os.path.samefile",
                       wraps=os.path.samefile) as proof:
                with inventory_path_identity_scope():
                    expected = canonical_existing_path_key(path)
                    first_count = proof.call_count
                    with inventory_path_identity_scope():
                        self.assertEqual(canonical_existing_path_key(path), expected)
                    self.assertEqual(proof.call_count, first_count)
                    self.assertEqual(canonical_existing_path_key(extended), expected)
                    self.assertGreater(proof.call_count, first_count)
                last_count = proof.call_count
                canonical_existing_path_key(path)
                self.assertGreater(proof.call_count, last_count)

    def test_failed_identity_is_rechecked_and_exception_discards_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            with patch("local_agent_record_janitor.path_identity.os.path.samefile",
                       side_effect=[False, True, True]) as proof:
                with self.assertRaisesRegex(RuntimeError, "aborted"):
                    with inventory_path_identity_scope():
                        canonical_existing_path_key(path)
                        canonical_existing_path_key(path)
                        self.assertEqual(proof.call_count, 2)
                        raise RuntimeError("aborted")
                canonical_existing_path_key(path)
                self.assertEqual(proof.call_count, 3)

    def test_missing_path_is_not_cached_when_created_during_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "new.jsonl"
            extended = "\\\\?\\" + str(path)
            with inventory_path_identity_scope():
                self.assertNotEqual(canonical_existing_path_key(path),
                                    canonical_existing_path_key(extended))
                path.touch()
                self.assertEqual(canonical_existing_path_key(path),
                                 canonical_existing_path_key(extended))

    def test_cindy_shared_paths_are_proven_once_and_next_inventory_is_fresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            home.mkdir()
            database = root / "cindy.db"
            rows = []
            for index in range(25):
                record_id = f"record-{index}"
                rollout = write_rollout(home, record_id, originator="codex_cli_rs")
                rows.append({"id": record_id, "rollout_path": str(rollout)})
            create_thread_index(home, rows)
            create_cindy_database(database, [
                {"id": "ui-" + row["id"], "sdk_session_id": row["id"],
                 "agent_kind": "codex", "status": "active"} for row in rows
            ])
            def build():
                return build_client_inventory((CindyAdapter(
                    database=database, codex_home=home, cindy_root=root,
                    codex_bin_hint=root / "fake-codex"),), client="cindy")
            with patch("local_agent_record_janitor.path_identity.os.path.samefile",
                       wraps=os.path.samefile) as proof:
                first = build()
                home_calls = sum(str(call.args[0]) == str(home) for call in proof.call_args_list)
                self.assertEqual(home_calls, 1)
                proof.reset_mock()
                second = build()
                self.assertEqual(sum(str(call.args[0]) == str(home)
                                     for call in proof.call_args_list), 1)
            self.assertFalse(first.errors)
            self.assertEqual(len(first.records), 25)
            self.assertEqual(first.to_dict(), second.to_dict())


if __name__ == "__main__":
    unittest.main()
