from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator


_inventory_path_keys: ContextVar[dict[str, str] | None] = ContextVar(
    "inventory_path_keys", default=None
)


@contextmanager
def inventory_path_identity_scope() -> Iterator[None]:
    """Reuse proven identities while assembling one read-only inventory/context.

    Nested catalog builders share this scope. Nothing survives the outer
    builder, so deletion guards and subsequent inventories obtain fresh proof.
    Exact spellings are separate keys; missing/unproven aliases are never cached.
    """
    if _inventory_path_keys.get() is not None:
        yield
        return
    token = _inventory_path_keys.set({})
    try:
        yield
    finally:
        _inventory_path_keys.reset(token)


def is_local_absolute_locator(value: str) -> bool:
    """Reject remote and foreign-OS locators before constructing a Path."""
    if not value or "\0" in value or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return False
    if os.name == "nt":
        # Python 3.12 isabs also accepts /home and \home on Windows.
        # Require a drive identity, including the local extended form.
        return bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\\?\\[A-Za-z]:\\)", value))
    return value.startswith("/") and not value.startswith("//")


def canonical_existing_path_key(path: str | os.PathLike[str]) -> str:
    """Return a stable key while merging only proven Windows aliases.

    Windows extended paths can name the same existing object as their normal
    spelling.  Collapse those spellings only after ``samefile`` proves
    physical identity.  Missing paths and inspection failures stay distinct
    so planning remains fail closed.
    """

    raw = os.path.normpath(os.path.abspath(os.fspath(path)))
    if os.name != "nt":
        return os.path.normcase(raw)

    cache = _inventory_path_keys.get()
    if cache is not None and raw in cache:
        return cache[raw]

    def proven(value: str) -> str:
        if cache is not None:
            cache[raw] = value
        return value

    try:
        resolved = os.path.normpath(os.fspath(Path(raw).resolve(strict=True)))
        if not os.path.samefile(raw, resolved):
            return raw
    except (OSError, RuntimeError):
        # Do not collapse case or extended-prefix spellings for missing or
        # uninspectable paths.  They have no proven physical identity.
        return raw

    lowered = resolved.lower()
    candidate: str | None = None
    if lowered.startswith("\\\\?\\unc\\"):
        candidate = "\\\\" + resolved[8:]
    elif lowered.startswith("\\\\?\\"):
        tail = resolved[4:]
        if len(tail) >= 3 and tail[1] == ":" and tail[2] in "\\/":
            candidate = tail
    if candidate is None:
        return proven(resolved)

    candidate_key = os.path.normpath(os.path.abspath(candidate))
    try:
        if os.path.samefile(raw, candidate_key):
            return proven(candidate_key)
    except OSError:
        pass
    return raw


__all__ = ["canonical_existing_path_key", "is_local_absolute_locator", "inventory_path_identity_scope"]
