from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from codex_session_janitor.legacy_index import (
    LegacyIndexInventoryError,
    LegacyIndexManifestError,
    LegacyIndexOperationError,
    LegacyIndexSafetyError,
    LegacyIndexSnapshotMismatch,
    inventory_legacy_index,
    repair_legacy_index,
    restore_legacy_index,
)


class LegacyIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.codex_home = Path(self.temporary_directory.name) / "codex-home"
        self.codex_home.mkdir()
        self._create_state(["live"])

    def _create_state(self, ids: list[str], *, with_threads: bool = True) -> None:
        path = self.codex_home / "state_5.sqlite"
        if path.exists():
            path.unlink()
        with closing(sqlite3.connect(path)) as connection:
            if with_threads:
                connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
                connection.executemany(
                    "INSERT INTO threads (id) VALUES (?)",
                    ((thread_id,) for thread_id in ids),
                )
            else:
                connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
            connection.commit()

    def _write_index(self, raw: bytes) -> Path:
        path = self.codex_home / "session_index.jsonl"
        path.write_bytes(raw)
        return path

    def _write_rollout(
        self,
        thread_id: str,
        *,
        archived: bool = False,
        raw: bytes | None = None,
    ) -> Path:
        root = "archived_sessions" if archived else "sessions"
        path = self.codex_home / root / "2026" / "07" / "31" / "rollout.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is None:
            raw = (
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": thread_id, "cwd": "D:\\project"},
                    }
                ).encode("utf-8")
                + b"\n{\"type\":\"event_msg\"}\n"
            )
        path.write_bytes(raw)
        return path

    def test_inventory_preserves_exact_bytes_and_reports_all_lines(self) -> None:
        self._write_rollout("rollout-live", archived=True)
        raw_lines = [
            b"\xef\xbb\xbf{\"id\":\"live\",\"thread_name\":\"one\"}\r\n",
            b"{\"id\":\"live\",\"thread_name\":\"two\"}\n",
            b"\xff\r",
            b"{\"thread_name\":\"no id\"}\n",
            b"{\"id\":\"gone\",\"thread_name\":\"gone one\"}\r\n",
            b"{\"id\":\"rollout-live\"}\n",
            b"{\"id\":\"gone\",\"thread_name\":\"gone two\"}",
        ]
        self._write_index(b"".join(raw_lines))

        inventory = inventory_legacy_index(self.codex_home)
        repeated = inventory_legacy_index(self.codex_home)

        self.assertEqual(inventory.snapshot_fingerprint, repeated.snapshot_fingerprint)
        self.assertEqual(inventory.residual_thread_ids, ("gone",))
        self.assertEqual(inventory.residual_line_count, 2)
        self.assertEqual(inventory.duplicate_entry_line_count, 2)
        self.assertEqual(inventory.duplicate_live_thread_ids, ("live",))
        self.assertEqual(inventory.duplicate_live_line_count, 2)
        self.assertEqual(inventory.malformed_line_count, 1)
        self.assertEqual(inventory.line_count, len(raw_lines))
        self.assertEqual(inventory.indexed_thread_count, 1)
        self.assertEqual(inventory.rollout_count, 1)
        self.assertEqual(inventory.live_thread_count, 2)
        self.assertTrue(inventory.needs_repair)
        self.assertEqual(
            [line.newline for line in inventory.lines],
            ["crlf", "lf", "cr", "lf", "crlf", "lf", "none"],
        )
        self.assertTrue(inventory.lines[0].has_bom)
        self.assertEqual(inventory.lines[2].parse_status, "invalid_utf8")
        self.assertEqual(inventory.lines[3].parse_status, "missing_id")
        for line, raw_line in zip(inventory.lines, raw_lines):
            self.assertEqual(line.sha256, hashlib.sha256(raw_line).hexdigest())

        expected = b"".join(raw_lines[:4] + [raw_lines[5]])
        self.assertEqual(
            inventory.expected_sha256, hashlib.sha256(expected).hexdigest()
        )
        payload = inventory.to_dict()
        self.assertEqual(payload["residual_thread_ids"], ["gone"])
        self.assertEqual(len(payload["lines"]), len(raw_lines))

    def test_repair_removes_only_approved_residual_lines_and_restores(self) -> None:
        raw = (
            b'{"id":"live","thread_name":"first"}\r\n'
            b"not json\n"
            b'{"id":"gone","thread_name":"old"}\n'
            b'{"id":"live","thread_name":"duplicate"}\r'
            b'{"id":"gone","thread_name":"old duplicate"}'
        )
        expected = (
            b'{"id":"live","thread_name":"first"}\r\n'
            b"not json\n"
            b'{"id":"live","thread_name":"duplicate"}\r'
        )
        index = self._write_index(raw)
        inventory = inventory_legacy_index(self.codex_home)

        result = repair_legacy_index(
            self.codex_home,
            approved_snapshot_fingerprint=inventory.snapshot_fingerprint,
        )

        self.assertEqual(index.read_bytes(), expected)
        self.assertEqual(result.removed_thread_ids, ("gone",))
        self.assertEqual(result.removed_line_count, 2)
        self.assertEqual(result.backup_path.read_bytes(), raw)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "prepared")
        self.assertEqual(manifest["operation"], "repair")
        self.assertEqual(manifest["original_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(manifest["new_sha256"], hashlib.sha256(expected).hexdigest())

        restored = restore_legacy_index(
            self.codex_home,
            backup_id=result.backup_id,
        )
        self.assertEqual(index.read_bytes(), raw)
        self.assertEqual(restored.source_backup_id, result.backup_id)
        self.assertEqual(restored.restore_backup_path.read_bytes(), expected)
        restore_manifest = json.loads(
            restored.restore_backup_path.with_name("manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(restore_manifest["operation"], "restore")
        self.assertEqual(restore_manifest["source_backup_id"], result.backup_id)

        # The restore operation's own pre-restore backup makes restore
        # reversible without weakening the current-hash guard.
        undo = restore_legacy_index(
            self.codex_home,
            backup_id=restored.restore_backup_id,
        )
        self.assertEqual(index.read_bytes(), expected)
        self.assertEqual(undo.restored_sha256, hashlib.sha256(expected).hexdigest())

    def test_snapshot_drift_blocks_before_backup_or_replacement(self) -> None:
        original = b'{"id":"gone"}\n'
        index = self._write_index(original)
        approved = inventory_legacy_index(self.codex_home)
        self._write_rollout("unrelated-new-live")

        with self.assertRaises(LegacyIndexSnapshotMismatch):
            repair_legacy_index(
                self.codex_home,
                approved_snapshot_fingerprint=approved.snapshot_fingerprint,
            )

        self.assertEqual(index.read_bytes(), original)
        backup_root = (
            self.codex_home
            / ".codex-session-janitor"
            / "legacy-index-backups"
        )
        self.assertEqual(list(backup_root.iterdir()), [])

    def test_missing_approved_snapshot_is_rejected(self) -> None:
        self._write_index(b'{"id":"gone"}\n')
        with self.assertRaises(LegacyIndexSnapshotMismatch):
            repair_legacy_index(
                self.codex_home,
                approved_snapshot_fingerprint="",
            )

    def test_restore_refuses_to_overwrite_subsequent_changes(self) -> None:
        self._write_index(b'{"id":"gone"}\n')
        approved = inventory_legacy_index(self.codex_home)
        repaired = repair_legacy_index(
            self.codex_home,
            approved_snapshot_fingerprint=approved.snapshot_fingerprint,
        )
        changed = b'{"id":"new-live"}\n'
        (self.codex_home / "session_index.jsonl").write_bytes(changed)

        with self.assertRaises(LegacyIndexSnapshotMismatch):
            restore_legacy_index(self.codex_home, backup_id=repaired.backup_id)

        self.assertEqual(
            (self.codex_home / "session_index.jsonl").read_bytes(), changed
        )

    def test_invalid_backup_id_cannot_escape_backup_root(self) -> None:
        with self.assertRaises(LegacyIndexManifestError):
            restore_legacy_index(self.codex_home, backup_id="../outside")

    def test_tampered_manifest_or_backup_is_rejected(self) -> None:
        self._write_index(b'{"id":"gone"}\n')
        approved = inventory_legacy_index(self.codex_home)
        repaired = repair_legacy_index(
            self.codex_home,
            approved_snapshot_fingerprint=approved.snapshot_fingerprint,
        )
        manifest_bytes = repaired.manifest_path.read_bytes()

        manifest = json.loads(manifest_bytes)
        manifest["storage_path"] = "different-storage"
        repaired.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(LegacyIndexManifestError):
            restore_legacy_index(self.codex_home, backup_id=repaired.backup_id)

        repaired.manifest_path.write_bytes(manifest_bytes)
        repaired.backup_path.write_bytes(b"tampered")
        with self.assertRaises(LegacyIndexManifestError):
            restore_legacy_index(self.codex_home, backup_id=repaired.backup_id)

    def test_all_rollout_first_line_failures_block_residual_proof(self) -> None:
        cases = {
            "invalid utf8": b"\xff\n",
            "invalid json": b"not json\n",
            "not object": b"[]\n",
            "wrong type": b'{"type":"event_msg","payload":{}}\n',
            "bad payload": b'{"type":"session_meta","payload":[]}\n',
            "missing id": b'{"type":"session_meta","payload":{}}\n',
        }
        for label, rollout_raw in cases.items():
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    home = Path(temporary_directory)
                    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
                        connection.commit()
                    (home / "session_index.jsonl").write_bytes(b'{"id":"gone"}\n')
                    rollout = home / "sessions" / "bad.jsonl"
                    rollout.parent.mkdir()
                    rollout.write_bytes(rollout_raw)
                    with self.assertRaises(LegacyIndexInventoryError):
                        inventory_legacy_index(home)

    def test_traversal_and_rollout_read_errors_are_not_softened(self) -> None:
        self._write_index(b'{"id":"gone"}\n')
        sessions = self.codex_home / "sessions"
        sessions.mkdir()
        with mock.patch(
            "codex_session_janitor.legacy_index.os.scandir",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaises(LegacyIndexInventoryError):
                inventory_legacy_index(self.codex_home)

        self._write_rollout("live-rollout")
        with mock.patch(
            "codex_session_janitor.legacy_index._read_stable_first_line",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaises(PermissionError):
                inventory_legacy_index(self.codex_home)

    def test_incompatible_or_corrupt_sqlite_blocks_inventory(self) -> None:
        self._write_index(b'{"id":"gone"}\n')
        self._create_state([], with_threads=False)
        with self.assertRaises(LegacyIndexInventoryError):
            inventory_legacy_index(self.codex_home)

        (self.codex_home / "state_5.sqlite").write_bytes(b"not sqlite")
        with self.assertRaises(LegacyIndexInventoryError):
            inventory_legacy_index(self.codex_home)

    def test_missing_state_database_is_an_explicit_empty_inventory(self) -> None:
        (self.codex_home / "state_5.sqlite").unlink()
        self._write_index(b'{"id":"gone"}\n')
        inventory = inventory_legacy_index(self.codex_home)
        self.assertEqual(inventory.indexed_thread_count, 0)
        self.assertEqual(inventory.residual_thread_ids, ("gone",))

    def test_hard_linked_index_is_rejected(self) -> None:
        source = self.codex_home / "source.jsonl"
        source.write_bytes(b'{"id":"gone"}\n')
        index = self.codex_home / "session_index.jsonl"
        try:
            os.link(source, index)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        with self.assertRaises(LegacyIndexSafetyError):
            inventory_legacy_index(self.codex_home)

    def test_symlinked_index_or_rollout_path_is_rejected(self) -> None:
        outside = self.codex_home / "outside.jsonl"
        outside.write_bytes(b'{"id":"gone"}\n')
        index = self.codex_home / "session_index.jsonl"
        try:
            index.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(LegacyIndexSafetyError):
            inventory_legacy_index(self.codex_home)

    def test_atomic_replace_failure_leaves_target_and_durable_backup(self) -> None:
        original = b'{"id":"gone"}\n'
        index = self._write_index(original)
        approved = inventory_legacy_index(self.codex_home)

        with mock.patch(
            "codex_session_janitor.legacy_index.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaises(LegacyIndexOperationError) as captured:
                repair_legacy_index(
                    self.codex_home,
                    approved_snapshot_fingerprint=approved.snapshot_fingerprint,
                )

        error = captured.exception
        self.assertEqual(error.state, "unchanged")
        self.assertIsNotNone(error.backup_id)
        self.assertEqual(index.read_bytes(), original)
        backup = (
            self.codex_home
            / ".codex-session-janitor"
            / "legacy-index-backups"
            / str(error.backup_id)
            / "session_index.jsonl.before"
        )
        self.assertEqual(backup.read_bytes(), original)

    def test_post_replace_flush_failure_is_reported_as_applied(self) -> None:
        original = b'{"id":"gone"}\n'
        index = self._write_index(original)
        approved = inventory_legacy_index(self.codex_home)
        expected = b""

        from codex_session_janitor import legacy_index as implementation

        real_fsync_directory = implementation._fsync_directory

        def fail_only_target_parent(path: Path) -> None:
            if path == self.codex_home:
                raise OSError("directory flush failed")
            real_fsync_directory(path)

        with mock.patch.object(
            implementation,
            "_fsync_directory",
            side_effect=fail_only_target_parent,
        ):
            with self.assertRaises(LegacyIndexOperationError) as captured:
                repair_legacy_index(
                    self.codex_home,
                    approved_snapshot_fingerprint=approved.snapshot_fingerprint,
                )

        self.assertEqual(captured.exception.state, "applied")
        self.assertEqual(index.read_bytes(), expected)
        self.assertEqual(
            captured.exception.current_sha256,
            hashlib.sha256(expected).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
