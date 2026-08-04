from __future__ import annotations

import unittest
from pathlib import Path

from local_agent_record_janitor.rendering import (
    DEFAULT_MAX_WIDTH,
    escape_terminal_controls,
    safe_single_line,
    terminal_display_width,
)


class SafeSingleLineTests(unittest.TestCase):
    def test_preserves_plain_chinese_and_windows_paths(self) -> None:
        value = r"D:\GitRepo\高考志愿填报\VPS-Toolkit"

        self.assertEqual(safe_single_line(value, max_width=None), value)
        self.assertEqual(safe_single_line(Path(value), max_width=None), value)

    def test_escapes_cr_lf_and_tab_explicitly(self) -> None:
        self.assertEqual(
            safe_single_line("title\r\nnext\tcolumn", max_width=None),
            r"title\r\nnext\tcolumn",
        )

    def test_neutralizes_ansi_escape_sequences(self) -> None:
        value = "safe\x1b[31mred\x1b[0m\x1b]0;forged title\x07"
        rendered = safe_single_line(value, max_width=None)

        self.assertEqual(
            rendered,
            r"safe\x1b[31mred\x1b[0m\x1b]0;forged title\x07",
        )
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)

    def test_escapes_all_c0_del_and_c1_examples(self) -> None:
        value = "\x00\x07\x1f\x7f\x80\x85\x9b\x9f"
        self.assertEqual(
            escape_terminal_controls(value),
            r"\x00\x07\x1f\x7f\x80\x85\x9b\x9f",
        )

    def test_escapes_all_unicode_bidi_controls(self) -> None:
        codepoints = (
            0x061C,
            0x200E,
            0x200F,
            0x202A,
            0x202B,
            0x202C,
            0x202D,
            0x202E,
            0x2066,
            0x2067,
            0x2068,
            0x2069,
        )
        value = "name" + "".join(chr(codepoint) for codepoint in codepoints) + "txt"
        expected = (
            "name"
            + "".join(f"\\u{codepoint:04x}" for codepoint in codepoints)
            + "txt"
        )

        rendered = safe_single_line(value, max_width=None)

        self.assertEqual(rendered, expected)
        for codepoint in codepoints:
            self.assertNotIn(chr(codepoint), rendered)

    def test_escapes_unicode_line_and_paragraph_separators(self) -> None:
        self.assertEqual(
            safe_single_line("one\u2028two\u2029three", max_width=None),
            r"one\u2028two\u2029three",
        )

    def test_none_and_blank_values_do_not_gain_a_placeholder(self) -> None:
        self.assertEqual(safe_single_line(None), "")
        self.assertEqual(safe_single_line(""), "")
        self.assertEqual(safe_single_line("   "), "   ")

    def test_long_chinese_is_stably_truncated_by_terminal_width(self) -> None:
        value = "中文测试会话名称"

        first = safe_single_line(value, max_width=10)
        second = safe_single_line(value, max_width=10)

        self.assertEqual(first, "中文测试…")
        self.assertEqual(second, first)
        self.assertLessEqual(terminal_display_width(first), 10)

    def test_emoji_clusters_are_not_split_during_truncation(self) -> None:
        cases = (
            "👩\u200d💻abc",
            "👍🏽abc",
            "🇨🇳abc",
            "1\ufe0f\u20e3abc",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    safe_single_line(value, max_width=3),
                    value[:-3] + "…",
                )

    def test_truncation_does_not_split_an_escape_token(self) -> None:
        self.assertEqual(safe_single_line("\x1bX", max_width=4), "…")
        self.assertEqual(
            safe_single_line("\x1bX", max_width=5),
            r"\x1bX",
        )

    def test_default_width_is_bounded_and_marked(self) -> None:
        rendered = safe_single_line("x" * (DEFAULT_MAX_WIDTH + 20))

        self.assertTrue(rendered.endswith("…"))
        self.assertEqual(terminal_display_width(rendered), DEFAULT_MAX_WIDTH)

    def test_rejects_invalid_max_width(self) -> None:
        for value in (0, -1, True, 1.5, "10"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safe_single_line("name", max_width=value)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
