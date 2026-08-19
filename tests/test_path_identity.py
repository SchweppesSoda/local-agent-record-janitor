from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent_record_janitor.path_identity import (
    canonical_existing_path_key,
)


@unittest.skipUnless(os.name == "nt", "Windows path alias behavior")
class ExistingPathIdentityTests(unittest.TestCase):
    def test_existing_extended_drive_path_matches_plain_path(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "rollout.jsonl"
            path.write_text("body\n", encoding="utf-8")
            extended = "\\\\?\\" + str(path)

            self.assertTrue(os.path.samefile(path, extended))
            self.assertEqual(
                canonical_existing_path_key(path),
                canonical_existing_path_key(extended),
            )

    def test_missing_extended_path_keeps_distinct_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plain = Path(root) / "missing.jsonl"
            extended = "\\\\?\\" + str(plain)

            self.assertNotEqual(
                canonical_existing_path_key(plain),
                canonical_existing_path_key(extended),
            )

    def test_existing_case_aliases_resolve_to_actual_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mixed = Path(root) / "MiXeD.jsonl"
            mixed.write_text("body\n", encoding="utf-8")
            alternate = Path(root) / "mixed.JSONL"

            self.assertTrue(os.path.samefile(mixed, alternate))
            self.assertEqual(
                canonical_existing_path_key(mixed),
                canonical_existing_path_key(alternate),
            )

    def test_existing_normal_alias_requires_samefile_proof(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mixed = Path(root) / "MiXeD.jsonl"
            mixed.write_text("body\n", encoding="utf-8")
            alternate = Path(root) / "mixed.JSONL"

            with mock.patch(
                "local_agent_record_janitor.path_identity.os.path.samefile",
                wraps=os.path.samefile,
            ) as samefile:
                self.assertEqual(
                    canonical_existing_path_key(mixed),
                    canonical_existing_path_key(alternate),
                )
            self.assertGreaterEqual(samefile.call_count, 2)

    def test_normal_alias_samefile_failure_preserves_lexical_identity(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mixed = Path(root) / "MiXeD.jsonl"
            mixed.write_text("body\n", encoding="utf-8")
            alternate = Path(root) / "mixed.JSONL"

            with mock.patch(
                "local_agent_record_janitor.path_identity.os.path.samefile",
                side_effect=OSError("identity unavailable"),
            ):
                self.assertNotEqual(
                    canonical_existing_path_key(mixed),
                    canonical_existing_path_key(alternate),
                )

    def test_missing_case_variants_stay_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            first = Path(root) / "Missing.jsonl"
            second = Path(root) / "missing.JSONL"

            self.assertNotEqual(
                canonical_existing_path_key(first),
                canonical_existing_path_key(second),
            )

    def test_missing_child_below_symlink_is_not_resolved_to_physical_parent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            physical = Path(root) / "physical"
            physical.mkdir()
            alias = Path(root) / "alias"
            try:
                alias.symlink_to(physical, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            self.assertNotEqual(
                canonical_existing_path_key(alias / "missing.jsonl"),
                canonical_existing_path_key(physical / "missing.jsonl"),
            )

    def test_samefile_error_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "rollout.jsonl"
            path.write_text("body\n", encoding="utf-8")
            extended = "\\\\?\\" + str(path)

            with mock.patch(
                "local_agent_record_janitor.path_identity.os.path.samefile",
                side_effect=OSError("identity unavailable"),
            ):
                self.assertNotEqual(
                    canonical_existing_path_key(path),
                    canonical_existing_path_key(extended),
                )

    def test_extended_unc_candidate_requires_samefile_proof(self) -> None:
        extended = r"\\?\UNC\server\share\rollout.jsonl"
        plain = r"\\server\share\rollout.jsonl"
        with (
            mock.patch(
                "local_agent_record_janitor.path_identity.Path.resolve",
                return_value=Path(extended),
            ),
            mock.patch(
                "local_agent_record_janitor.path_identity.Path.exists",
                return_value=True,
            ),
            mock.patch(
                "local_agent_record_janitor.path_identity.os.path.samefile",
                return_value=True,
            ) as samefile,
        ):
            self.assertEqual(
                canonical_existing_path_key(extended),
                os.path.normpath(os.path.abspath(plain)),
            )
        self.assertEqual(samefile.call_count, 2)
        self.assertEqual(samefile.call_args_list[-1].args[0], os.path.normpath(os.path.abspath(extended)))


if __name__ == "__main__":
    unittest.main()
