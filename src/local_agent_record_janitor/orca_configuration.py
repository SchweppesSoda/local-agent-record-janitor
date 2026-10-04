"""Bounded configuration allowlist for the isolated metadata-only runtime.

Configuration values and credentials never enter plans or diagnostics. The
caller freezes file identities and hashes, and rechecks them before startup.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys

from .path_identity import is_local_absolute_locator

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

MAX_CONFIG_BYTES = 256 * 1024
_ENUMS = {
    "model_reasoning_effort": {"none", "minimal", "low", "medium", "high", "xhigh"},
    "model_reasoning_summary": {"auto", "concise", "detailed", "none"},
    "model_verbosity": {"low", "medium", "high"},
    "approval_policy": {"never", "on-request", "on-failure", "untrusted"},
    "sandbox_mode": {"read-only", "workspace-write", "danger-full-access"},
    "cli_auth_credentials_store": {"file", "keyring", "auto", "ephemeral"},
}
_NAMES = {"model", "review_model", "model_provider"}


def validate_configuration(path: Path, identity: dict) -> None:
    if identity.get("absent"):
        return
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES or hashlib.sha256(data).hexdigest() != identity["sha256"]:
            raise ValueError
        value = tomllib.loads(data.decode("utf-8"))
        if not set(value) <= _NAMES | set(_ENUMS) | {"check_for_update_on_startup", "projects"}:
            raise ValueError
        for name, item in value.items():
            if name in _NAMES:
                if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,256}", item):
                    raise ValueError
            elif name in _ENUMS:
                if not isinstance(item, str) or item not in _ENUMS[name]:
                    raise ValueError
            elif name == "check_for_update_on_startup":
                if type(item) is not bool:
                    raise ValueError
            elif name == "projects":
                if not isinstance(item, dict) or len(item) > 256:
                    raise ValueError
                for locator, settings in item.items():
                    if (not isinstance(locator, str) or len(locator) > 4096
                            or not is_local_absolute_locator(locator)
                            or not isinstance(settings, dict) or set(settings) != {"trust_level"}
                            or settings["trust_level"] not in ("trusted", "untrusted")):
                        raise ValueError
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        # TOML parse exceptions can contain private configuration values.
        raise ValueError("orca_storage_configuration_unverified") from None


def package_manifest_paths(binary: Path) -> tuple[Path, ...]:
    """Known install-context probes for the qualified standalone executable."""
    parts = tuple(part.casefold() for part in binary.parts)
    if any(parts[index:index + 3] == ("packages", "standalone", "releases") for index in range(len(parts) - 2)):
        raise ValueError("orca_package_context_unverified")
    paths = [binary.parent / "codex-package.json"]
    if binary.parent.name.casefold() in {"bin", "codex-resources"}:
        paths.append(binary.parent.parent / "codex-package.json")
    return tuple(paths)
