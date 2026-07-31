"""Terminal-safe rendering helpers for untrusted identity metadata.

Conversation titles, working directories, and agent names can originate from
rollout files or databases.  They must therefore be treated as untrusted when
printed in an interactive cleanup prompt.  This module keeps ordinary Unicode
text intact while making terminal control characters visible and ensuring the
result occupies one logical line.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator


DEFAULT_MAX_WIDTH = 160
TRUNCATION_MARKER = "…"

_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,  # ARABIC LETTER MARK
        0x200E,  # LEFT-TO-RIGHT MARK
        0x200F,  # RIGHT-TO-LEFT MARK
        0x202A,  # LEFT-TO-RIGHT EMBEDDING
        0x202B,  # RIGHT-TO-LEFT EMBEDDING
        0x202C,  # POP DIRECTIONAL FORMATTING
        0x202D,  # LEFT-TO-RIGHT OVERRIDE
        0x202E,  # RIGHT-TO-LEFT OVERRIDE
        0x2066,  # LEFT-TO-RIGHT ISOLATE
        0x2067,  # RIGHT-TO-LEFT ISOLATE
        0x2068,  # FIRST STRONG ISOLATE
        0x2069,  # POP DIRECTIONAL ISOLATE
    }
)
_LINE_SEPARATOR_CODEPOINTS = frozenset({0x2028, 0x2029})


def safe_single_line(
    value: object | None,
    max_width: int | None = DEFAULT_MAX_WIDTH,
) -> str:
    """Return a terminal-safe, optionally truncated, single-line string.

    ``None`` becomes an empty string so that the caller can choose an
    appropriate placeholder.  Other values are converted with ``str``;
    ordinary whitespace is deliberately not stripped.

    ``max_width`` is measured in approximate terminal columns rather than
    Python code points.  Wide CJK characters and common emoji sequences are
    therefore not cut in half.  Pass ``None`` to disable truncation.  When
    truncation is needed, the returned text ends with ``TRUNCATION_MARKER``.
    """

    if max_width is not None:
        if (
            isinstance(max_width, bool)
            or not isinstance(max_width, int)
            or max_width < 1
        ):
            raise ValueError("max_width must be a positive integer or None")

    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""

    units = tuple(_safe_display_units(text))
    rendered = "".join(unit for unit, _width in units)
    if max_width is None:
        return rendered

    rendered_width = sum(width for _unit, width in units)
    if rendered_width <= max_width:
        return rendered

    marker_width = terminal_display_width(TRUNCATION_MARKER)
    available_width = max_width - marker_width
    kept: list[str] = []
    kept_width = 0
    for unit, unit_width in units:
        if kept_width + unit_width > available_width:
            break
        kept.append(unit)
        kept_width += unit_width
    return "".join(kept) + TRUNCATION_MARKER


def escape_terminal_controls(value: object | None) -> str:
    """Escape terminal controls and Unicode line/bidi controls without truncating."""

    if value is None:
        return ""
    return "".join(
        escaped
        for escaped, _width in _safe_display_units(str(value))
    )


def terminal_display_width(value: str) -> int:
    """Return an approximate terminal-column width for already-safe text.

    This intentionally has no locale or third-party-library dependency.  It
    handles combining marks, East Asian wide characters, regional-indicator
    flags, variation selectors, emoji modifiers, and common ZWJ sequences.
    """

    return sum(_cluster_display_width(cluster) for cluster in _grapheme_clusters(value))


def _safe_display_units(value: str) -> Iterator[tuple[str, int]]:
    """Yield indivisible safe strings and their rendered terminal widths."""

    for cluster in _grapheme_clusters(value):
        escaped = "".join(_escape_character(character) for character in cluster)
        yield escaped, terminal_display_width(escaped)


def _escape_character(character: str) -> str:
    codepoint = ord(character)
    if character == "\r":
        return r"\r"
    if character == "\n":
        return r"\n"
    if character == "\t":
        return r"\t"
    if codepoint <= 0x1F or 0x7F <= codepoint <= 0x9F:
        return f"\\x{codepoint:02x}"
    if codepoint in _BIDI_CONTROL_CODEPOINTS:
        return f"\\u{codepoint:04x}"
    if codepoint in _LINE_SEPARATOR_CODEPOINTS:
        return f"\\u{codepoint:04x}"
    if unicodedata.category(character) == "Cs":
        # Unpaired surrogates cannot be encoded by a normal UTF-8 terminal.
        return f"\\u{codepoint:04x}"
    return character


def _grapheme_clusters(value: str) -> Iterator[str]:
    """Yield conservative, dependency-free approximations of grapheme clusters."""

    index = 0
    length = len(value)
    while index < length:
        start = index
        first = value[index]
        index += 1

        if _is_regional_indicator(first) and index < length:
            if _is_regional_indicator(value[index]):
                index += 1

        while index < length:
            character = value[index]
            if _is_grapheme_extension(character):
                index += 1
                continue
            if character == "\u200d" and index + 1 < length:
                # Keep the joiner and the character it joins to in one unit.
                index += 2
                continue
            break
        yield value[start:index]


def _is_grapheme_extension(character: str) -> bool:
    codepoint = ord(character)
    return (
        unicodedata.category(character).startswith("M")
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0100 <= codepoint <= 0xE01EF
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or 0xE0020 <= codepoint <= 0xE007F
    )


def _is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _cluster_display_width(cluster: str) -> int:
    if not cluster:
        return 0
    if any(_is_regional_indicator(character) for character in cluster):
        return 2
    if "\ufe0f" in cluster:
        return 2
    widths = tuple(_character_display_width(character) for character in cluster)
    if "\u200d" in cluster:
        return max(widths, default=0)
    return max(widths, default=0)


def _character_display_width(character: str) -> int:
    codepoint = ord(character)
    category = unicodedata.category(character)
    if (
        codepoint <= 0x1F
        or 0x7F <= codepoint <= 0x9F
        or category in {"Mn", "Me", "Cf", "Cs"}
    ):
        return 0
    if unicodedata.east_asian_width(character) in {"W", "F"}:
        return 2
    return 1


__all__ = [
    "DEFAULT_MAX_WIDTH",
    "TRUNCATION_MARKER",
    "escape_terminal_controls",
    "safe_single_line",
    "terminal_display_width",
]
