from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from local_agent_record_janitor.codex_state import (
    CodexStateReadError,
    find_thread_rollouts,
    read_spawn_descendants,
    read_thread_index,
    read_thread_metadata,
    rollout_state_fingerprint,
    scan_rollouts,
)

from tests.support import create_thread_index, write_rollout


class RolloutScanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()

    def test_reads_active_and_archived_session_metadata(self) -> None:
        active_path = write_rollout(
            self.codex_home,
            "active-thread",
            originator="aionui-session",
            source={"subagent": None},
        )
        archived_path = write_rollout(
            self.codex_home,
            "archived-thread",
            originator="cindy",
            archived=True,
            use_session_id=True,
        )

        records = scan_rollouts(self.codex_home)

        self.assertEqual(set(records), {"active-thread", "archived-thread"})
        self.assertEqual(records["active-thread"].path, active_path)
        self.assertEqual(records["active-thread"].originator, "aionui-session")
        self.assertEqual(records["active-thread"].source, {"subagent": None})
        self.assertFalse(records["active-thread"].archived)
        self.assertEqual(records["archived-thread"].path, archived_path)
        self.assertTrue(records["archived-thread"].archived)

    def test_ignores_invalid_non_meta_and_empty_thread_records(self) -> None:
        invalid_json = self.codex_home / "sessions" / "invalid.jsonl"
        invalid_json.parent.mkdir()
        invalid_json.write_text("{not-json}\n", encoding="utf-8")
        write_rollout(
            self.codex_home,
            "wrong-record",
            originator="cindy",
            first_record_type="event_msg",
        )
        empty_id = self.codex_home / "sessions" / "empty.jsonl"
        empty_id.write_text(
            '{"type":"session_meta","payload":{"id":""}}\n',
            encoding="utf-8",
        )
        non_object_payload = self.codex_home / "sessions" / "list.jsonl"
        non_object_payload.write_text(
            '{"type":"session_meta","payload":[]}\n',
            encoding="utf-8",
        )
        missing_file = self.codex_home / "sessions" / "missing.jsonl"
        missing_file.touch()

        self.assertEqual(scan_rollouts(self.codex_home), {})

    def test_absent_rollout_directories_are_safe(self) -> None:
        self.assertEqual(scan_rollouts(self.codex_home), {})

    def test_rollout_state_fingerprint_is_stable_and_tracks_file_stat(
        self,
    ) -> None:
        path = write_rollout(
            self.codex_home,
            "fingerprinted",
            originator="aionui-session",
            source={"nested": {"value": 1}},
        )
        record = find_thread_rollouts(
            self.codex_home,
            "fingerprinted",
        )[0]

        initial = rollout_state_fingerprint(record)
        repeated = rollout_state_fingerprint(record)
        path.write_text(
            path.read_text(encoding="utf-8")
            + '{"type":"event_msg","payload":{"new":true}}\n',
            encoding="utf-8",
        )
        changed = rollout_state_fingerprint(record)

        self.assertRegex(initial, r"^v1:[0-9a-f]{64}$")
        self.assertEqual(repeated, initial)
        self.assertNotEqual(changed, initial)

    def test_rollout_state_fingerprint_fails_if_file_state_is_unreadable(
        self,
    ) -> None:
        path = write_rollout(
            self.codex_home,
            "missing-fingerprint",
            originator="test",
        )
        record = find_thread_rollouts(
            self.codex_home,
            "missing-fingerprint",
        )[0]
        path.unlink()

        with self.assertRaises(OSError):
            rollout_state_fingerprint(record)


class ThreadIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()

    def test_reads_only_requested_threads_and_deduplicates_ids(self) -> None:
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": "requested",
                    "rollout_path": "/tmp/requested.jsonl",
                    "archived": 1,
                    "created_at": 10,
                    "updated_at": 20,
                },
                {"id": "not-requested"},
            ],
        )

        rows = read_thread_index(
            self.codex_home, ["requested", "requested", "missing"]
        )

        self.assertEqual(set(rows), {"requested"})
        self.assertEqual(rows["requested"]["rollout_path"], "/tmp/requested.jsonl")
        self.assertEqual(rows["requested"]["archived"], 1)
        self.assertEqual(rows["requested"]["created_at"], 10)
        self.assertEqual(rows["requested"]["updated_at"], 20)

    def test_special_characters_are_bound_as_values(self) -> None:
        unusual_id = "thread'); DROP TABLE threads; --"
        create_thread_index(self.codex_home, [{"id": unusual_id}])

        rows = read_thread_index(self.codex_home, [unusual_id])

        self.assertEqual(set(rows), {unusual_id})
        with closing(
            sqlite3.connect(self.codex_home / "state_5.sqlite")
        ) as connection:
            count = connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        self.assertEqual(count, 1)

    def test_missing_database_table_or_empty_ids_are_safe(self) -> None:
        self.assertEqual(read_thread_index(self.codex_home, ["thread"]), {})
        create_thread_index(
            self.codex_home, [], include_threads_table=False
        )
        self.assertEqual(read_thread_index(self.codex_home, ["thread"]), {})
        self.assertEqual(read_thread_index(self.codex_home, []), {})

    def test_metadata_reader_preserves_optional_column_presence(self) -> None:
        state_db = self.codex_home / "state_5.sqlite"
        with closing(sqlite3.connect(state_db)) as connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT)"
            )
            connection.executemany(
                "INSERT INTO threads (id, name) VALUES (?, ?)",
                (("null-name", None), ("named", "Readable name")),
            )
            connection.commit()

        rows = read_thread_metadata(
            self.codex_home,
            ["named", "null-name", "missing", "named"],
        )

        self.assertEqual(set(rows), {"named", "null-name"})
        self.assertEqual(rows["named"]["name"], "Readable name")
        self.assertIn("name", rows["null-name"])
        self.assertIsNone(rows["null-name"]["name"])
        self.assertNotIn("title", rows["null-name"])
        self.assertNotIn("cwd", rows["null-name"])

    def test_metadata_reader_fails_closed_only_for_existing_bad_state(self) -> None:
        self.assertEqual(
            read_thread_metadata(self.codex_home, ["rollout-only"]),
            {},
        )
        state_db = self.codex_home / "state_5.sqlite"
        with closing(sqlite3.connect(state_db)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()

        with self.assertRaisesRegex(
            CodexStateReadError,
            r"required table 'threads' is missing",
        ):
            read_thread_metadata(self.codex_home, ["thread"], strict=True)

        self.assertEqual(
            read_thread_metadata(self.codex_home, ["thread"], strict=False),
            {},
        )


class SpawnDescendantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()

    def test_reads_transitive_descendant_closure(self) -> None:
        create_thread_index(
            self.codex_home,
            [{"id": "root"}, {"id": "child"}, {"id": "grandchild"}],
            spawn_edges=[
                {
                    "parent_thread_id": "root",
                    "child_thread_id": "child",
                },
                {
                    "parent_thread_id": "child",
                    "child_thread_id": "grandchild",
                },
            ],
        )

        descendants = read_spawn_descendants(self.codex_home, ["root", "child"])

        self.assertEqual(descendants["root"], {"child", "grandchild"})
        self.assertEqual(descendants["child"], {"grandchild"})

    def test_existing_incompatible_spawn_table_fails_closed_in_strict_mode(
        self,
    ) -> None:
        state_db = self.codex_home / "state_5.sqlite"
        with closing(sqlite3.connect(state_db)) as connection:
            connection.execute(
                """
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            connection.commit()

        with self.assertRaisesRegex(
            CodexStateReadError,
            r"thread_spawn_edges.*child_thread_id",
        ):
            read_spawn_descendants(
                self.codex_home,
                ["root"],
                strict=True,
            )

        self.assertEqual(
            read_spawn_descendants(
                self.codex_home,
                ["root"],
                strict=False,
            ),
            {"root": set()},
        )


if __name__ == "__main__":
    unittest.main()
