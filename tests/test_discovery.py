from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_session_janitor.discovery import choose_codex_binary


class ChooseCodexBinaryTests(unittest.TestCase):
    @patch(
        "codex_session_janitor.discovery.discover_path_codex",
        return_value=Path("path-codex"),
    )
    def test_missing_explicit_hint_never_falls_back_to_path(
        self,
        discover,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "missing-codex"

            selected = choose_codex_binary(missing)

        self.assertIsNone(selected)
        discover.assert_not_called()

    @patch(
        "codex_session_janitor.discovery.discover_path_codex",
        return_value=Path("path-codex"),
    )
    def test_non_file_explicit_hint_never_falls_back_to_path(
        self,
        discover,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "codex-directory"
            directory.mkdir()

            selected = choose_codex_binary(directory)

        self.assertIsNone(selected)
        discover.assert_not_called()

    @patch(
        "codex_session_janitor.discovery.discover_path_codex",
        return_value=Path("path-codex"),
    )
    def test_no_hint_uses_path_discovery(self, discover) -> None:
        selected = choose_codex_binary(None)

        self.assertEqual(selected, Path("path-codex"))
        discover.assert_called_once_with()

    @patch(
        "codex_session_janitor.discovery.discover_path_codex",
        return_value=Path("path-codex"),
    )
    def test_valid_explicit_hint_returns_itself(self, discover) -> None:
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / "codex.exe"
            executable.touch()

            selected = choose_codex_binary(executable)

        self.assertEqual(selected, executable)
        discover.assert_not_called()

    @patch(
        "codex_session_janitor.discovery.discover_path_codex",
        return_value=Path("path-codex"),
    )
    def test_explicit_hint_inspection_error_fails_closed(
        self,
        discover,
    ) -> None:
        with patch.object(Path, "is_file", side_effect=OSError("denied")):
            selected = choose_codex_binary(Path("explicit-codex"))

        self.assertIsNone(selected)
        discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
