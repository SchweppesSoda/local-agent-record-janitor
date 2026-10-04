from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor import frozen_files as files


class FrozenFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)

    def write(self, name, data=b"PRIVATE_BODY"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_tree_includes_empty_directories_preserves_neighbors_and_has_no_bodies(self):
        self.write("selected/subagent/one.jsonl")
        (self.root / "selected/empty").mkdir()
        other = self.write("neighbor/one.jsonl")
        frozen = files.freeze_remove(self.root, "selected")
        self.assertNotIn("PRIVATE_BODY", json.dumps(frozen))
        self.assertFalse(files.satisfied(self.root, frozen))
        files.apply_remove(self.root, frozen)
        self.assertTrue(files.satisfied(self.root, frozen))
        self.assertEqual(other.read_bytes(), b"PRIVATE_BODY")

    def test_executable_suffix_has_consistent_path_and_handle_identity(self):
        self.write("runtime/node.exe", b"SYNTHETIC_EXECUTABLE")
        frozen = files.freeze_remove(self.root, "runtime")
        files.apply_remove(self.root, frozen)
        self.assertTrue(files.satisfied(self.root, frozen))

    def test_modified_or_added_member_refuses_before_any_unlink(self):
        selected = self.write("selected/one.jsonl")
        frozen = files.freeze_remove(self.root, "selected")
        added = self.write("selected/two.jsonl")
        with self.assertRaises(files.FrozenFilesError):
            files.apply_remove(self.root, frozen)
        self.assertTrue(selected.exists())
        added.unlink()
        selected.write_bytes(b"UPDATED_BODY")
        with self.assertRaises(files.FrozenFilesError):
            files.apply_remove(self.root, frozen)
        self.assertTrue(selected.exists())

    def test_partial_unlink_failure_is_readable_but_never_implicitly_retried(self):
        first = self.write("selected/a.jsonl")
        second = self.write("selected/b.jsonl")
        frozen = files.freeze_remove(self.root, "selected")
        original = files._delete_frozen
        def unlink(root, before, **kwargs):
            if root / before["path"] == second:
                raise PermissionError("fixture locked member")
            return original(root, before, **kwargs)
        with patch.object(files, "_delete_frozen", unlink), self.assertRaises(PermissionError):
            files.apply_remove(self.root, frozen)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertFalse(files.satisfied(self.root, frozen))
        with self.assertRaises(files.FrozenFilesError):
            files.apply_remove(self.root, frozen)
        self.assertTrue(second.exists())

    def test_added_member_during_unlink_survives_nonrecursive_directory_removal(self):
        selected = self.write("selected/a.jsonl")
        frozen = files.freeze_remove(self.root, "selected")
        original = files._delete_frozen
        added = self.root / "selected/new.jsonl"
        def unlink(root, before, **kwargs):
            result = original(root, before, **kwargs)
            if root / before["path"] == selected:
                added.write_bytes(b"NEW_OUTSIDE_APPROVAL")
            return result
        with patch.object(files, "_delete_frozen", unlink), self.assertRaises(OSError):
            files.apply_remove(self.root, frozen)
        self.assertEqual(added.read_bytes(), b"NEW_OUTSIDE_APPROVAL")
        self.assertFalse(files.satisfied(self.root, frozen))

    def test_exact_rewrite_binds_both_before_and_after_and_preserves_other_values(self):
        source = self.write("state.json", b'{"selected":"PRIVATE_BODY","other":42}')
        def transform(raw):
            value = json.loads(raw)
            del value["selected"]
            return json.dumps(value).encode()
        frozen = files.freeze_rewrite(self.root, "state.json", transform)
        self.assertNotIn("PRIVATE_BODY", json.dumps(frozen))
        self.assertFalse(files.satisfied(self.root, frozen))
        with self.assertRaises(files.FrozenFilesError):
            files.apply_rewrite(self.root, frozen, lambda raw: b"{}")
        self.assertIn(b"PRIVATE_BODY", source.read_bytes())
        files.apply_rewrite(self.root, frozen, transform)
        self.assertTrue(files.satisfied(self.root, frozen))
        self.assertEqual(json.loads(source.read_bytes()), {"other": 42})
        self.assertEqual(list(self.root.glob(".larj-write-*")), [])

    def test_replaced_file_with_same_bytes_is_not_original_authorization(self):
        source = self.write("state.json", b"{}")
        frozen = files.freeze_rewrite(self.root, "state.json", lambda raw: b"[]")
        replacement = self.write("replacement", b"{}")
        os.replace(replacement, source)
        with self.assertRaises(files.FrozenFilesError):
            files.apply_rewrite(self.root, frozen, lambda raw: b"[]")
        self.assertEqual(source.read_bytes(), b"{}")

    def test_hardlinks_and_path_escape_are_rejected(self):
        self.write("source")
        os.link(self.root / "source", self.root / "alias")
        with self.assertRaises(files.FrozenFilesError):
            files.freeze_remove(self.root, "source")
        for relative in ("../outside", "/absolute", "C:/absolute", "sub/../source", "sub\\source", "./source"):
            with self.subTest(relative=relative), self.assertRaises(files.FrozenFilesError):
                files.checked_path(self.root, relative)

    def test_bounds_apply_to_directory_enumeration_and_file_reads(self):
        self.write("selected/a")
        self.write("selected/b")
        with patch.object(files, "MAX_ENTRIES", 2), self.assertRaises(files.FrozenFilesError):
            files.freeze_remove(self.root, "selected")
        with self.assertRaises(files.FrozenFilesError):
            files.read_file(self.root, "selected/a", limit=1)
        with patch.object(files, "MAX_ENTRIES", 3):
            frozen = files.freeze_remove(self.root, "selected")
        self.assertEqual(len(frozen["files"]) + len(frozen["directories"]), 3)

    @unittest.skipUnless(os.name == "nt", "Windows object deletion handles")
    def test_delete_handle_prevents_last_moment_replacement_or_write(self):
        source = self.write("selected.jsonl")
        replacement = self.write("replacement.jsonl", b"OUTSIDE_APPROVAL")
        frozen = files.freeze_remove(self.root, "selected.jsonl")
        original = files._windows_fd
        attempts = []
        def opened(path, **kwargs):
            fd = original(path, **kwargs)
            if path == source and kwargs.get("delete"):
                with self.assertRaises(OSError):
                    os.replace(replacement, source)
                with self.assertRaises(OSError):
                    source.write_bytes(b"OUTSIDE_APPROVAL")
                attempts.append(path)
            return fd
        with patch.object(files, "_windows_fd", opened):
            files.apply_remove(self.root, frozen)
        self.assertEqual(attempts, [source])
        self.assertFalse(source.exists())
        self.assertEqual(replacement.read_bytes(), b"OUTSIDE_APPROVAL")

    def test_rewrite_cannot_report_success_after_temporary_file_tampering(self):
        self.write("state.json", b"{}")
        frozen = files.freeze_rewrite(self.root, "state.json", lambda raw: b"[]")
        original = os.replace
        def replace(source, destination):
            Path(source).write_bytes(b"UNAPPROVED_TEMP_BYTES")
            return original(source, destination)
        with patch.object(os, "replace", replace), self.assertRaises(files.FrozenFilesError):
            files.apply_rewrite(self.root, frozen, lambda raw: b"[]")
        self.assertFalse(files.satisfied(self.root, frozen))

    def test_windows_lexical_aliases_are_rejected_on_all_platforms(self):
        for relative in ("source.", "source ", "sub./source", "sub /source", "NUL", "con.txt", "COM1", "LPT9.json"):
            with self.subTest(relative=relative), self.assertRaises(files.FrozenFilesError):
                files.checked_path(self.root, relative)

    @unittest.skipUnless(os.name == "posix", "POSIX symbolic links")
    def test_symbolic_link_in_frozen_parent_is_rejected_in_recovery(self):
        self.write("selected/a")
        frozen = files.freeze_remove(self.root, "selected/a")
        (self.root / "selected").rename(self.root / "moved")
        (self.root / "selected").symlink_to(self.root / "moved", target_is_directory=True)
        with self.assertRaises(files.FrozenFilesError):
            files.satisfied(self.root, frozen)
