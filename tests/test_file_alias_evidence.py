from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent_record_janitor.client_contracts import ClientContractError
from local_agent_record_janitor.file_alias_evidence import probe_file_aliases
from local_agent_record_janitor.path_identity import is_local_absolute_locator


class FileAliasEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.file = self.root / "known.jsonl"
        self.file.write_bytes(b"SENSITIVE_BODY_NOT_READ")

    def _link(self, target, link):
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"Temporary symlinks unavailable: {exc}")

    def test_hardlink_count_covers_only_supplied_directory_entries(self):
        second = self.root / "second.jsonl"
        os.link(self.file, second)
        with patch.object(Path, "open", side_effect=AssertionError("Contents must not be read")):
            one = probe_file_aliases((self.file,), roots=(self.root,))
            two = probe_file_aliases((self.file, second), roots=(self.root,))
        self.assertEqual(one.entries[0].nlink, 2)
        self.assertFalse(one.entries[0].hardlink_count_matches)
        self.assertTrue(all(entry.hardlink_count_matches for entry in two.entries))
        self.assertEqual(len(two.entries[0].known_hardlink_paths), 2)
        self.assertEqual(two.entries[0].file_id, two.entries[1].file_id)
        self.assertFalse(two.to_dict()["alias_coverage_complete"])
        self.assertNotIn("SENSITIVE_BODY", str(two.to_dict()))

    def test_copy_has_a_different_file_identity(self):
        second = self.root / "copy.jsonl"
        shutil.copyfile(self.file, second)
        result = probe_file_aliases((self.file, second), roots=(self.root,))
        self.assertNotEqual(result.entries[0].file_id, result.entries[1].file_id)
        self.assertTrue(all(len(entry.known_paths) == 1 for entry in result.entries))
        self.assertTrue(all(entry.hardlink_count_matches for entry in result.entries))

    def test_case_distinct_directories_keep_separate_identity_and_lexical_scope(self):
        first, second = self.root / "Store", self.root / "store"
        first.mkdir()
        second.mkdir(exist_ok=True)
        if os.path.samefile(first, second):
            self.skipTest("Temporary filesystem treats case-distinct directory names as aliases")
        files = (first / "known.txt", second / "known.txt")
        for file in files:
            file.write_bytes(b"metadata fixture")
        result = probe_file_aliases(files, roots=(first, second))
        self.assertTrue(all(entry.probe_complete for entry in result.entries), result.to_dict())
        self.assertNotEqual(result.entries[0].file_id, result.entries[1].file_id)
        excluded = probe_file_aliases((files[1],), roots=(first,))
        self.assertEqual(excluded.entries[0].errors, ("outside_known_roots",))

    @unittest.skipUnless(os.name == "nt", "Windows extended-path spelling only")
    def test_extended_spelling_does_not_count_as_a_second_hardlink(self):
        unlisted = self.root / "unlisted.jsonl"
        os.link(self.file, unlisted)
        result = probe_file_aliases((self.file, "\\\\?\\" + str(self.file)),
                                    roots=(self.root, "\\\\?\\" + str(self.root)))
        self.assertTrue(all(entry.probe_complete for entry in result.entries), result.to_dict())
        self.assertEqual(len(result.entries[0].known_paths), 2)
        self.assertEqual(len(result.entries[0].known_hardlink_paths), 1)
        self.assertTrue(all(entry.hardlink_count_matches is False for entry in result.entries))

    def test_internal_symlink_is_observed_without_claiming_alias_coverage(self):
        link = self.root / "link.jsonl"
        self._link(self.file, link)
        result = probe_file_aliases((self.file, link), roots=(self.root,))
        self.assertEqual(result.entries[1].kind, "symlink")
        self.assertEqual(result.entries[1].readlink, os.readlink(link))
        self.assertTrue(result.entries[1].probe_complete, result.to_dict())
        self.assertEqual(result.entries[1].file_id, result.entries[0].file_id)
        self.assertEqual(len(result.entries[0].known_hardlink_paths), 1)
        self.assertFalse(result.to_dict()["alias_coverage_complete"])

    def test_broken_and_external_symlinks_are_incomplete_without_following_external_target(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "unapproved.jsonl"
            external.write_bytes(b"UNAPPROVED")
            broken = self.root / "broken.jsonl"
            escape = self.root / "escape.jsonl"
            self._link(self.root / "missing.jsonl", broken)
            self._link(external, escape)
            actual_lstat = Path.lstat

            def bounded_lstat(path, *args, **kwargs):
                if path == external or str(path) == "\\\\?\\" + str(external):
                    raise AssertionError("Unapproved target must not be probed")
                return actual_lstat(path, *args, **kwargs)

            for omit_missing in (False, True):
                with patch.object(Path, "lstat", bounded_lstat):
                    result = probe_file_aliases((broken, escape), roots=(self.root,),
                                                omit_initially_missing=omit_missing)
                self.assertEqual(len(result.entries), 2)
                self.assertTrue(all(not entry.probe_complete for entry in result.entries))
                self.assertTrue(all(entry.readlink for entry in result.entries))
                self.assertIn("outside_known_roots", str(result.entries[1].errors))
                self.assertTrue(all(entry.file_id is None for entry in result.entries))

    def test_relative_leaf_symlink_preserves_its_raw_target(self):
        link = self.root / "relative.jsonl"
        self._link(self.file.name, link)
        result = probe_file_aliases((self.file, link), roots=(self.root,))
        self.assertTrue(result.entries[1].probe_complete, result.to_dict())
        self.assertEqual(result.entries[1].readlink, self.file.name)
        self.assertEqual(result.entries[1].file_id, result.entries[0].file_id)

    def test_remote_and_foreign_symlink_targets_are_rejected_before_probing(self):
        link = self.root / "opaque.jsonl"
        self._link(self.file, link)
        targets = ("\\\\server\\share\\file", "//server/share/file", "ssh://host/file",
                   "\\\\?\\UNC\\server\\share\\file", "\\\\.\\C:\\file", "C:relative")
        targets += (("/home/foreign/file", "\\foreign\\file") if os.name == "nt"
                    else ("C:\\foreign\\file", "\\\\?\\C:\\foreign\\file"))
        permitted = {str(path) for path in (link, self.root, *self.root.parents)}
        actual_lstat = Path.lstat

        def bounded_lstat(path, *args, **kwargs):
            self.assertIn(str(path), permitted, "Opaque target must not be probed")
            return actual_lstat(path, *args, **kwargs)

        for target in targets:
            with self.subTest(target=target), patch("os.readlink", return_value=target), \
                    patch.object(Path, "lstat", bounded_lstat):
                result = probe_file_aliases((link,), roots=(self.root,))
            self.assertEqual(result.entries[0].readlink, target)
            self.assertIn("opaque_link_target", str(result.entries[0].errors))
            self.assertIsNone(result.entries[0].file_id)

    @unittest.skipUnless(os.name == "nt", "Windows extended-path spelling only")
    def test_extended_root_spelling_requires_matching_directory_identity(self):
        # Win32 strips the trailing dot; the extended spelling names a
        # distinct directory. It must not silently extend the approved root.
        approved = self.root / "store."
        approved.mkdir()
        distinct = Path("\\\\?\\" + str(approved))
        distinct.mkdir()
        self.addCleanup(distinct.rmdir)
        external = distinct / "unapproved.jsonl"
        external.write_bytes(b"UNAPPROVED")
        self.addCleanup(external.unlink)
        self.assertFalse(os.path.samefile(approved, distinct))
        link = approved / "link.jsonl"
        self._link(external, link)
        actual_lstat = Path.lstat

        def bounded_lstat(path, *args, **kwargs):
            if path == external:
                raise AssertionError("Different root identity must not be probed")
            return actual_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", bounded_lstat):
            result = probe_file_aliases((link,), roots=(approved,))
        self.assertIn("outside_known_roots", str(result.entries[0].errors))
        self.assertIsNone(result.entries[0].file_id)

    @unittest.skipUnless(os.name == "nt", "Windows extended-path spelling only")
    def test_extended_spelling_cannot_hide_replacement_of_the_approved_root(self):
        actual_lstat = Path.lstat
        for omit_missing in (False, True):
            with self.subTest(omit_missing=omit_missing):
                approved = self.root / f"approved-{omit_missing}"
                approved.mkdir()
                extended = Path("\\\\?\\" + str(approved))
                swapped = False

                def replaced_root(path, *args, **kwargs):
                    nonlocal swapped
                    if path == extended and not swapped:
                        swapped = True
                        approved.rename(self.root / f"moved-{omit_missing}")
                        approved.mkdir()
                        if not omit_missing:
                            (approved / "known.jsonl").write_bytes(b"replacement")
                    return actual_lstat(path, *args, **kwargs)

                with patch.object(Path, "lstat", replaced_root):
                    result = probe_file_aliases((extended / "known.jsonl",), roots=(approved,),
                                                omit_initially_missing=omit_missing)
                self.assertTrue(swapped)
                self.assertEqual(len(result.entries), 1)
                self.assertIn("directory_identity_changed", str(result.entries[0].errors))
                self.assertFalse(result.entries[0].probe_complete)

    def test_probe_failure_and_changed_parent_keep_unknown_identity(self):
        actual_lstat = Path.lstat
        file_seen = False

        def changed_parent(path, *args, **kwargs):
            nonlocal file_seen
            if path == self.file:
                file_seen = True
            elif path == self.root and file_seen:
                raise PermissionError("Synthetic directory recheck failure")
            return actual_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", changed_parent):
            result = probe_file_aliases((self.file,), roots=(self.root,))
        self.assertFalse(result.to_dict()["probe_complete"])
        self.assertIsNone(result.entries[0].file_id)
        self.assertIn("recheck failure", str(result.entries[0].errors))
        with patch.object(Path, "lstat", side_effect=PermissionError("Synthetic root failure")):
            failed = probe_file_aliases((self.file,), roots=(self.root,))
        self.assertFalse(failed.to_dict()["probe_complete"])
        self.assertIn("root_probe_failed", str(failed.errors))

    def test_optional_absence_does_not_hide_parent_failure_or_observed_file_loss(self):
        missing = self.root / "optional-wal"
        result = probe_file_aliases((missing,), roots=(self.root,), omit_initially_missing=True)
        self.assertEqual(result.entries, ())
        self.assertTrue(result.to_dict()["probe_complete"])
        actual_lstat = Path.lstat
        missing_seen = False

        def failed_parent(path, *args, **kwargs):
            nonlocal missing_seen
            if path == missing:
                missing_seen = True
            elif path == self.root and missing_seen:
                raise FileNotFoundError("Temporary parent disappeared")
            return actual_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", failed_parent):
            changed = probe_file_aliases((missing,), roots=(self.root,), omit_initially_missing=True)
        self.assertEqual(len(changed.entries), 1)
        self.assertFalse(changed.entries[0].probe_complete)
        self.assertIn("parent disappeared", str(changed.entries[0].errors))
        calls = 0

        def observed_file_loss(path, *args, **kwargs):
            nonlocal calls
            if path == self.file:
                calls += 1
                if calls > 1:
                    raise FileNotFoundError("Previously observed file disappeared")
            return actual_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", observed_file_loss):
            lost = probe_file_aliases((self.file,), roots=(self.root,), omit_initially_missing=True)
        self.assertEqual(len(lost.entries), 1)
        self.assertFalse(lost.entries[0].probe_complete)
        self.assertIn("observed file disappeared", str(lost.entries[0].errors))
        failed = probe_file_aliases((missing,), roots=(self.root / "absent",), omit_initially_missing=True)
        self.assertFalse(failed.to_dict()["probe_complete"])
        self.assertTrue(failed.errors)

    def test_optional_dangling_symlink_remains_an_error(self):
        link = self.root / "optional-link"
        self._link(self.root / "missing", link)
        result = probe_file_aliases((link,), roots=(self.root,), omit_initially_missing=True)
        self.assertEqual(len(result.entries), 1)
        self.assertFalse(result.entries[0].probe_complete)
        self.assertTrue(result.entries[0].readlink)

    @unittest.skipUnless(os.name == "nt", "Windows Junction identity drift")
    def test_directory_moved_behind_junction_between_two_known_roots_is_incomplete(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if powershell is None:
            self.skipTest("No PowerShell available for temporary Junction fixture")
        first = self.root / "approved-A"
        second = self.root / "approved-B"
        first.mkdir()
        second.mkdir()
        file = first / "known.txt"
        file.write_bytes(b"metadata fixture")
        before = file.stat()
        moved = second / "moved-A"
        actual_lstat = Path.lstat
        replaced = False

        def replaced_directory(path, *args, **kwargs):
            nonlocal replaced
            if path == file and not replaced:
                replaced = True
                first.rename(moved)
                quoted_first = str(first).replace("'", "''")
                quoted_moved = str(moved).replace("'", "''")
                completed = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                    f"$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path '{quoted_first}' -Target '{quoted_moved}' | Out-Null"],
                    creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, check=False)
                if completed.returncode:
                    self.skipTest("Temporary Junction unavailable")
            return actual_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", replaced_directory):
            result = probe_file_aliases((file,), roots=(first, second))
        self.assertEqual((before.st_dev, before.st_ino),
                         ((moved / "known.txt").stat().st_dev, (moved / "known.txt").stat().st_ino))
        self.assertTrue(first.lstat().st_file_attributes & 0x400)
        self.assertFalse(result.entries[0].probe_complete)
        self.assertIn("directory_identity_changed", str(result.entries[0].errors))

    def test_foreign_remote_and_relative_locators_remain_opaque_before_path_or_syscalls(self):
        foreign = ("/home/remote/file.jsonl", "\\home\\remote\\file.jsonl") if os.name == "nt" else ("C:\\remote\\file.jsonl",)
        values = (*foreign, "\\\\server\\share\\file.jsonl", "//server/share/file.jsonl", "ssh://host/file", "relative.jsonl")
        with patch("local_agent_record_janitor.file_alias_evidence.Path", side_effect=AssertionError("Opaque locator became Path")):
            for value in values:
                with self.subTest(value=value):
                    result = probe_file_aliases((value,), roots=(value,))
                    self.assertEqual(result.entries[0].errors, ("opaque_locator",))
                    self.assertTrue(result.errors)
        self.assertFalse(is_local_absolute_locator("C:\\bad\0name"))

    def test_host_and_namespace_are_checked_before_pathlike_conversion(self):
        class PoisonPath:
            def __fspath__(self):
                raise AssertionError("Nonlocal locator converted")

        for location in ({"host": "remote"}, {"path_namespace": "wsl"}):
            with self.subTest(location=location), self.assertRaises(ClientContractError):
                probe_file_aliases((PoisonPath(),), roots=(PoisonPath(),), **location)


if __name__ == "__main__":
    unittest.main()
