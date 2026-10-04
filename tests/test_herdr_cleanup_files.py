import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from local_agent_record_janitor import herdr_cleanup_files as cleanup
from local_agent_record_janitor.herdr_cleanup_json import fingerprint, HerdrCleanupError
from local_agent_record_janitor.record_identity import canonical_path
from tests.herdr_support import agent_session, pane, snapshot, tab, recovery_name, write_snapshot, CODEX_ID


class HerdrClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bindings = [{"session": "default", "engine": "codex", "kind": "id", "value": CODEX_ID,
                          "native_root": canonical_path(self.root / "native")}]
        self.value = snapshot(self.root / "project", (tab({0: pane(self.root, agent_session(), private="ERASE"),
            1: pane(self.root, agent_session(value="33333333-3333-4333-8333-333333333333"), private="KEEP")}),))
        self.history = {"version": 3, "layout_fingerprint": fingerprint(self.value), "workspaces": [{"tabs": [
            {"panes": {"0": {"ansi": "ERASE", "lines": 1}, "1": {"ansi": "KEEP", "lines": 2}}}]}]}

    def populated(self):
        write_snapshot(self.root / "session.json", self.value)
        write_snapshot(self.root / "session.json.tmp", self.value)
        write_snapshot(self.root / "session-history.json", self.history)
        write_snapshot(self.root / "session-history.json.tmp", self.history)
        write_snapshot(self.root / "session-snapshots" / recovery_name(), self.value)
        write_snapshot(self.root / "session-backups" / recovery_name(1).replace(".json", ".pending"), self.value)
        write_snapshot(self.root / "sessions" / "unselected" / "session.json", self.value)

    def test_all_recovery_copies_history_and_absence_are_frozen(self):
        self.populated()
        evidence, bodies = cleanup.materialize(self.root, self.bindings)
        self.assertEqual(len(evidence["files"]), 7)
        self.assertEqual(cleanup.remaining(evidence), 6)
        self.assertNotIn("ERASE", json.dumps(evidence))
        changes = cleanup.replacements(evidence)
        self.assertEqual(len(changes), 6)
        self.assertEqual(bodies["sessions/unselected/session.json"],
                         (self.root / "sessions/unselected/session.json").read_bytes())
        for relative, raw in changes.items():
            self.assertNotIn(b"ERASE", raw)
            self.assertIn(b"KEEP", raw)
            (self.root / relative).write_bytes(raw)
        self.assertEqual(cleanup.remaining(evidence), 0)
        (self.root / "session.json").write_text("{}")
        with self.assertRaisesRegex(HerdrCleanupError, "file_state_unknown"):
            cleanup.remaining(evidence)

    def test_history_can_bind_recovery_when_current_missing_or_different(self):
        write_snapshot(self.root / "session.json", snapshot(self.root))
        write_snapshot(self.root / "session-backups" / recovery_name(), self.value)
        write_snapshot(self.root / "session-history.json", self.history)
        evidence = cleanup.freeze(self.root, self.bindings)
        self.assertEqual(cleanup.remaining(evidence), 2)
        (self.root / "session.json").unlink()
        with self.assertRaisesRegex(HerdrCleanupError, "source_membership_changed"):
            cleanup.remaining(evidence)
        evidence = cleanup.freeze(self.root, self.bindings)
        self.assertEqual(cleanup.remaining(evidence), 2)

    def test_unmatched_history_new_copy_and_unknown_pending_are_rejected(self):
        self.populated()
        evidence = cleanup.freeze(self.root, self.bindings)
        write_snapshot(self.root / "session-backups" / recovery_name(5), self.value)
        with self.assertRaisesRegex(HerdrCleanupError, "source_membership_changed"):
            cleanup.remaining(evidence)
        write_snapshot(self.root / "session-history.json", {**self.history, "layout_fingerprint": "0" * 64})
        with self.assertRaisesRegex(HerdrCleanupError, "history_provenance_unverified"):
            cleanup.freeze(self.root, self.bindings)
        write_snapshot(self.root / "session-backups" / "unrecognized.pending", self.value)
        with self.assertRaisesRegex(HerdrCleanupError, "recovery_name_unverified"):
            cleanup.freeze(self.root, self.bindings)

    @unittest.skipUnless(os.name == "nt", "Windows sharing-mode primitive")
    def test_held_files_exclude_restore_save_and_rename_while_owner_commits(self):
        from local_agent_record_janitor.windows_held_files import HeldFiles
        self.populated()
        evidence = cleanup.freeze(self.root, self.bindings)
        with HeldFiles() as held:
            for item in evidence["files"]:
                held.acquire(self.root, item["before"], writable=True)
            for operation in (
                lambda: (self.root / "session.json").read_bytes(),
                lambda: (self.root / "session.json.tmp").write_bytes(b"UNAUTHORIZED"),
                lambda: (self.root / "session.json").rename(self.root / "renamed.json"),
            ):
                with self.assertRaises(OSError):
                    operation()
            replacements = cleanup.replacements(evidence, reader=held.read)
            for item in evidence["files"]:
                relative = item["before"]["path"]
                if relative in replacements:
                    held.replace(self.root, relative, replacements[relative], before=item["before"],
                                 after_sha256=item["after_sha256"])
            self.assertEqual(cleanup.remaining(evidence, reader=held.read), 0)
        self.assertEqual(cleanup.remaining(evidence), 0)
