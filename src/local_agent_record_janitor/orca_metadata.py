"""Read Orca journal metadata at upstream efbf651c7bb2eec778daf1844f8228e70809ec9f.

Only agent_session_records/tabs/store_meta are queried. Journal/chat rows,
operations, credentials and launch arguments are never returned or executed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client_contracts import SourceFailure
from .sqlite_utils import connect_readonly

UPSTREAM_REVISION = "efbf651c7bb2eec778daf1844f8228e70809ec9f"
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_LINK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MAX_RECORD_BYTES = 1024 * 1024


class OrcaMetadataError(ValueError):
    """Fixed diagnostic codes, never raw persisted JSON or parser excerpts."""


@dataclass(frozen=True)
class OrcaHandle:
    link_id: str
    provider: str
    native_id: str
    leaf_uuid: str | None
    origin: str


@dataclass(frozen=True)
class OrcaLease:
    """Minimum lifecycle/owner metadata; no spawn token or launch options."""

    claim_status: str
    unreconciled: bool
    runtime_kind: str
    handoff_stage: str | None
    owner_host: str | None
    owner_pid: int | None
    owner_start_time_ms: int | None
    death_kind: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"claim_status": self.claim_status, "unreconciled": self.unreconciled,
                "runtime_kind": self.runtime_kind, "handoff_stage": self.handoff_stage,
                "owner_host": self.owner_host, "owner_pid": self.owner_pid,
                "owner_start_time_ms": self.owner_start_time_ms, "death_kind": self.death_kind}


@dataclass(frozen=True)
class OrcaRecord:
    session_id: str
    provider: str
    host: str
    wsl_distro: str | None
    home_variable: str
    home_locator: str
    handles: tuple[OrcaHandle, ...]
    unsupported_recovery: bool
    lease: OrcaLease


def _text(value: Any, maximum: int = 512) -> bool:
    # Upstream bounds JavaScript UTF-16 code units, not Python code points.
    return (isinstance(value, str) and 0 < len(value.encode("utf-16-le", errors="surrogatepass")) // 2 <= maximum
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _integer(value: Any, minimum: int | None = None) -> bool:
    return type(value) is int and abs(value) <= 2**53 - 1 and (minimum is None or value >= minimum)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise OrcaMetadataError(code)


def _handle(value: Any) -> tuple[OrcaHandle, str, str]:
    _require(isinstance(value, dict), "provider_handle_invalid")
    handle = value.get("handle")
    _require(isinstance(handle, dict), "provider_handle_invalid")
    provider = handle.get("provider")
    native = handle.get("threadId") if provider == "codex" else handle.get("sessionId")
    leaf = handle.get("leafUuid") if provider == "claude" else None
    _require(provider in ("codex", "claude") and _text(native) and native == native.strip(), "provider_handle_invalid")
    _require(provider != "claude" or "leafUuid" in handle and (leaf is None or _text(leaf) and leaf == leaf.strip()), "provider_handle_invalid")
    _require(isinstance(value.get("linkId"), str) and bool(_LINK_ID.fullmatch(value["linkId"])), "provider_link_invalid")
    _require(value.get("origin") in {"created", "adopted", "resumed", "forked"}, "provider_link_invalid")
    _require(_integer(value.get("mintedAtFence"), 0) and _integer(value.get("observedAt")), "provider_link_invalid")
    key = provider + ":" + json.dumps([native, leaf] if provider == "claude" else native, ensure_ascii=False, separators=(",", ":"))
    root = provider + ":" + json.dumps(native, ensure_ascii=False, separators=(",", ":"))
    return OrcaHandle(value["linkId"], provider, native, leaf, value["origin"]), key, root


def parse_orca_record(row_id: Any, value: Any) -> OrcaRecord:
    _require(isinstance(value, dict) and type(value.get("schemaVersion")) is int and value["schemaVersion"] == 2, "record_schema_unsupported")
    session_id = value.get("sessionId")
    _require(isinstance(session_id, str) and bool(_SESSION_ID.fullmatch(session_id)) and row_id == session_id, "record_identity_invalid")
    location, home, lease = (value.get(key) for key in ("location", "accountHome", "lease"))
    _require(isinstance(location, dict) and _text(location.get("executionHostId"))
        and "wslDistro" in location and (location["wslDistro"] is None or _text(location["wslDistro"]))
        and _text(location.get("workspaceId")) and location.get("workspaceKind") in {"folder", "git-worktree"}, "record_location_invalid")
    provider = value.get("provider")
    _require(provider in {"codex", "claude"}, "record_provider_unsupported")
    _require(isinstance(home, dict) and home.get("variable") == ("CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR")
        and _text(home.get("path"), 4096), "record_account_home_invalid")
    chain = value.get("providerHandleChain")
    _require(isinstance(chain, list) and len(chain) <= 256, "provider_chain_invalid")
    handles: list[OrcaHandle] = []
    link_ids: set[str] = set()
    previous_key = previous_root = None
    previous_fence = -1
    for link in chain:
        handle, key, root = _handle(link)
        fence = link["mintedAtFence"]
        _require(handle.provider == provider and handle.link_id not in link_ids and fence >= previous_fence, "provider_chain_invalid")
        if not handles:
            _require(handle.origin in {"created", "adopted"}, "provider_chain_invalid")
        elif handle.origin == "resumed":
            _require(root == previous_root and not (key == previous_key and fence == previous_fence), "provider_chain_resume_invalid")
        elif handle.origin == "forked":
            fork_key = link.get("forkedFromKey")
            _require(root != previous_root and _text(fork_key) and fork_key == fork_key.strip()
                     and fork_key == previous_key, "provider_chain_fork_invalid")
        else:
            raise OrcaMetadataError("provider_chain_invalid")
        _require(handle.origin == "forked" or "forkedFromKey" not in link, "provider_chain_invalid")
        _require("supersedesKey" not in link or handle.origin == "created" and _text(link["supersedesKey"]), "provider_chain_invalid")
        handles.append(handle)
        link_ids.add(handle.link_id)
        previous_key, previous_root, previous_fence = key, root, fence
    _require(isinstance(lease, dict) and lease.get("sessionId") == session_id, "record_lease_invalid")
    _require(lease.get("runtimeKind") in {"native", "tui"} and _integer(lease.get("runtimeFence"), 0)
        and "handoffStage" in lease and lease["handoffStage"] in {None, "preparing", "old-owner-stopped", "new-owner-proving", "recovering", "manual-recovery"}, "record_lease_invalid")
    for key, bound in (("provenHandleLinkId", 128), ("reservedSpawnToken", 512), ("handoffOperationId", 512)):
        _require(key in lease and (lease[key] is None or _text(lease[key], bound)), "record_lease_invalid")
    _require(_integer(lease.get("leaseDeadlineAt")) and _integer(lease.get("lastRenewedAt"))
        and _text(lease.get("claimKeyId")) and lease.get("claimStatus") in {"reserved", "live", "conflicted", "released"}
        and type(lease.get("unreconciled")) is bool, "record_lease_invalid")
    owner = lease.get("ownerProcess")
    _require("ownerProcess" in lease and (owner is None or isinstance(owner, dict)
        and _text(owner.get("hostId")) and _integer(owner.get("pid"), 1)
        and "processStartTimeMs" in owner and (owner["processStartTimeMs"] is None or _integer(owner["processStartTimeMs"], 0))
        and _text(owner.get("spawnToken"))), "record_owner_invalid")
    checkpoint = lease.get("journalCheckpoint")
    _require("journalCheckpoint" in lease and (checkpoint is None or isinstance(checkpoint, dict)
        and _integer(checkpoint.get("epoch"), 0) and _integer(checkpoint.get("sequence"), 0)), "record_lease_invalid")
    death = lease.get("deathEvidence")
    _require("deathEvidence" in lease and (death is None or isinstance(death, dict)
        and death.get("kind") in {"exit-observed", "pid-absent", "identity-mismatch"} and _text(death.get("detail"))
        and _integer(death.get("observedAt"), 0)
        and ("ownerFence" not in death or _integer(death["ownerFence"], 0))
        and ("lastProvenAliveAt" not in death or _integer(death["lastProvenAliveAt"], 0)
             and death["lastProvenAliveAt"] <= death["observedAt"])), "record_lease_invalid")
    if lease["claimStatus"] == "live":
        _require(owner is not None and bool(handles) and lease["provenHandleLinkId"] == handles[-1].link_id
            and lease["runtimeFence"] == previous_fence, "record_live_lease_invalid")
    _require(_integer(value.get("createdAt")) and _integer(value.get("updatedAt")) and "launchEnv" not in value, "record_shape_invalid")
    if "options" in value:
        options = value["options"]
        _require(isinstance(options, dict) and len(options) <= 32
            and all(_text(k) and _text(v) for k, v in options.items()), "record_options_invalid")
    if "conversationName" in value:
        name = value["conversationName"]
        _require(_text(name, 200) and name == " ".join(name.split())
                 and not all(c in " \u200c\u200d" for c in name)
                 and not any(unicodedata.category(c) in {"Cc", "Zl", "Zp", "Cf"} and c not in "\u200c\u200d" for c in name),
                 "record_conversation_name_invalid")
    if "launchArgs" in value:
        args = value["launchArgs"]
        _require(isinstance(args, list) and len(args) <= 256 and all(isinstance(arg, str) and "\x00" not in arg for arg in args),
                 "record_launch_args_invalid")
        _require(len(json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 16384,
                 "record_launch_args_invalid")
    recovery = (lease["runtimeKind"] != "native" or lease["handoffStage"] in {"preparing", "old-owner-stopped", "manual-recovery"}
                or any(key in value for key in ("rewind", "conversationCommand"))
                or any("supersedesKey" in link for link in chain))
    return OrcaRecord(session_id, provider, location["executionHostId"], location["wslDistro"],
                      home["variable"], home["path"], tuple(handles), recovery,
                      OrcaLease(lease["claimStatus"], lease["unreconciled"], lease["runtimeKind"],
                                lease["handoffStage"], owner["hostId"] if owner else None,
                                owner["pid"] if owner else None,
                                owner["processStartTimeMs"] if owner else None,
                                death["kind"] if death else None))


def read_orca_journal(path: Path, profile_root: Path) -> tuple[tuple[OrcaRecord, ...], tuple[SourceFailure, ...]]:
    records: list[OrcaRecord] = []
    errors: list[SourceFailure] = []
    def failure(code: str) -> SourceFailure:
        return SourceFailure(str(path), code, profile_root=profile_root, database=path, error_type="OrcaInventoryIncomplete")
    try:
        with closing(connect_readonly(path)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] != 4:
                raise OrcaMetadataError("journal_schema_unsupported")
            for table, columns in {
                "agent_session_records": {"session_id": ("TEXT", True), "record_json": ("TEXT", False)},
                "agent_session_tabs": {"tab_id": ("TEXT", True), "session_id": ("TEXT", False), "position": ("INTEGER", False)},
                "agent_session_store_meta": {"key": ("TEXT", True), "value": ("TEXT", False)},
            }.items():
                schema = connection.execute("SELECT type, sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
                if schema is None or schema["type"] != "table" or str(schema["sql"]).lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
                    raise OrcaMetadataError("journal_metadata_schema_incomplete")
                actual = {row["name"]: row for row in connection.execute(f'PRAGMA table_info("{table}")')}
                if set(actual) != set(columns) or any(
                    str(actual[name]["type"]).upper() != kind
                    or (actual[name]["pk"] != 1 if primary else actual[name]["pk"] != 0 or actual[name]["notnull"] != 1)
                    for name, (kind, primary) in columns.items()
                ):
                    raise OrcaMetadataError("journal_metadata_schema_incomplete")
            for row in connection.execute("SELECT session_id, record_json FROM agent_session_records ORDER BY rowid"):
                try:
                    raw = row["record_json"]
                    _require(isinstance(raw, str) and len(raw.encode("utf-8")) <= _MAX_RECORD_BYTES, "record_size_unsupported")
                    records.append(parse_orca_record(row["session_id"], json.loads(raw)))
                except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
                    errors.append(failure(str(exc) if isinstance(exc, OrcaMetadataError) else "record_json_invalid"))
            ids = {record.session_id for record in records}
            tab_ids: set[str] = set()
            tab_sessions: set[str] = set()
            for row in connection.execute("SELECT tab_id, session_id, position FROM agent_session_tabs ORDER BY position"):
                tab, session = row["tab_id"], row["session_id"]
                if (not _text(tab) or ":" in tab or tab.startswith("web-terminal-") or session not in ids
                    or tab in tab_ids or session in tab_sessions or not _integer(row["position"], 0)):
                    errors.append(failure("session_tab_metadata_invalid"))
                    continue
                tab_ids.add(tab)
                tab_sessions.add(session)
            if connection.execute("SELECT 1 FROM agent_session_store_meta WHERE key = ?", ("session_tabs_recorded",)).fetchone() is None:
                errors.append(failure("session_tabs_coverage_unknown"))
    except (OSError, sqlite3.Error, OrcaMetadataError) as exc:
        errors.append(failure(str(exc) if isinstance(exc, OrcaMetadataError) else "journal_read_failed"))
    return tuple(records), tuple(dict.fromkeys(errors))
