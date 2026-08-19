from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .codex_state import parse_thread_source, read_thread_metadata
from .models import ConversationSummary, RolloutRecord, ThreadSourceInfo
from .path_identity import canonical_existing_path_key


__all__ = [
    "parse_thread_source",
    "project_label_from_cwd",
    "read_conversation_summaries",
    "read_legacy_thread_names",
]


def project_label_from_cwd(cwd: str | None) -> str | None:
    """Return a cross-platform basename while preserving ``cwd`` elsewhere."""

    if cwd is None:
        return None
    stripped = cwd.rstrip("/\\")
    if not stripped:
        return None
    label = re.split(r"[/\\]", stripped)[-1]
    if not label or label.endswith(":"):
        return None
    return label


def read_legacy_thread_names(
    codex_home: Path,
    thread_ids: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """Best-effort read of legacy ``session_index.jsonl`` display names.

    These names are presentation hints only, never conversation ownership
    evidence.  Malformed records and read errors are softened so any names
    already decoded remain usable by a read-only preview.
    """

    requested = {
        thread_id
        for thread_id in thread_ids
        if isinstance(thread_id, str) and thread_id
    }
    if not requested:
        return {}
    collected: dict[str, set[str]] = {}
    try:
        with (codex_home / "session_index.jsonl").open(
            "r", encoding="utf-8"
        ) as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(raw, dict):
                    continue
                thread_id = _nonempty_string(raw.get("id"))
                name = _nonempty_string(raw.get("thread_name"))
                if thread_id in requested and name is not None:
                    collected.setdefault(thread_id, set()).add(name)
    except (OSError, UnicodeError):
        pass
    return {
        thread_id: tuple(sorted(names))
        for thread_id, names in sorted(collected.items())
    }


def read_conversation_summaries(
    codex_home: Path,
    thread_ids: Iterable[str],
    *,
    rollout_records_by_thread: Mapping[
        str, RolloutRecord | Iterable[RolloutRecord]
    ]
    | None = None,
    legacy_names: Mapping[str, str | Iterable[str]] | None = None,
    strict: bool = True,
) -> dict[str, ConversationSummary]:
    """Merge SQLite, supplied rollout metadata and legacy index names.

    This function intentionally accepts already-read rollout records so the
    caller controls inventory strictness and avoids rereading chat content.
    Only fields present in :class:`RolloutRecord` (which originate from the
    first ``session_meta`` line) are inspected.
    """

    ids = sorted(
        {
            thread_id
            for thread_id in thread_ids
            if isinstance(thread_id, str) and thread_id
        }
    )
    if not ids:
        return {}
    rows = read_thread_metadata(codex_home, ids, strict=strict)
    rollout_mapping = rollout_records_by_thread or {}
    legacy_mapping = legacy_names or {}
    summaries: dict[str, ConversationSummary] = {}
    for thread_id in ids:
        row = rows.get(thread_id)
        records = _rollout_records(rollout_mapping.get(thread_id))
        legacy_values = _text_values(legacy_mapping.get(thread_id))
        summaries[thread_id] = _merge_summary(
            thread_id,
            row=row,
            records=records,
            legacy_names=legacy_values,
        )
    return summaries


def _merge_summary(
    thread_id: str,
    *,
    row: Mapping[str, Any] | None,
    records: tuple[RolloutRecord, ...],
    legacy_names: tuple[str, ...],
) -> ConversationSummary:
    legacy_names = tuple(sorted(set(legacy_names)))
    sources: set[str] = set()
    conflicts: set[str] = set()
    indexed = row is not None
    evidence_fingerprints: list[str] = []
    if row is not None:
        evidence_fingerprints.append(
            "threads=" + _evidence_sha256(dict(row))
        )
    for record in records:
        evidence_fingerprints.append(
            "session_meta="
            + _evidence_sha256(
                {
                    "thread_id": record.thread_id,
                    "path": canonical_existing_path_key(record.path),
                    "originator": record.originator,
                    "source": record.source,
                    "cwd": record.cwd,
                    "timestamp": record.timestamp,
                    "archived": record.archived,
                }
            )
        )
    if legacy_names:
        evidence_fingerprints.append(
            "session_index.thread_name="
            + _evidence_sha256(list(legacy_names))
        )

    name = _row_text(row, "name", sources)
    title = _row_text(row, "title", sources)
    git_origin_url = _row_text(row, "git_origin_url", sources)
    db_cwd = _row_text(row, "cwd", sources)
    db_originator = _row_text(row, "originator", sources)

    source_infos: list[ThreadSourceInfo] = []
    if row is not None:
        thread_source = _row_text(row, "thread_source", sources)
        if thread_source is not None:
            info = parse_thread_source(
                thread_source,
                source_label="threads.thread_source",
            )
            if info.is_subagent:
                source_infos.append(info)
        if "source" in row:
            sources.add("threads.source")
            source_infos.append(
                parse_thread_source(
                    row.get("source"),
                    source_label="threads.source",
                )
            )

    rollout_cwds: list[str] = []
    rollout_originators: list[str] = []
    rollout_archived: list[bool] = []
    for record in records:
        sources.add("session_meta")
        _append_text(rollout_cwds, record.cwd)
        _append_text(rollout_originators, record.originator)
        rollout_archived.append(record.archived)
        source_infos.append(
            parse_thread_source(
                record.source,
                source_label="session_meta.source",
            )
        )

    cwd = _prefer_database_value(
        db_cwd,
        rollout_cwds,
        "cwd",
        conflicts,
    )
    originator = _prefer_database_value(
        db_originator,
        rollout_originators,
        "originator",
        conflicts,
    )

    db_archived = _row_bool(row, "archived", sources)
    archived = _merge_archived(db_archived, rollout_archived, conflicts)

    db_nickname = _row_text(row, "agent_nickname", sources)
    db_role = _row_text(row, "agent_role", sources)
    db_path = _row_text(row, "agent_path", sources)
    nickname = _prefer_database_value(
        db_nickname,
        [info.agent_nickname for info in source_infos],
        "agent_nickname",
        conflicts,
    )
    role = _prefer_database_value(
        db_role,
        [info.agent_role for info in source_infos],
        "agent_role",
        conflicts,
    )
    agent_path = _prefer_database_value(
        db_path,
        [info.agent_path for info in source_infos],
        "agent_path",
        conflicts,
    )

    is_subagent = any(info.is_subagent for info in source_infos)
    if row is not None and "thread_source" in row:
        raw_thread_source = _nonempty_string(row.get("thread_source"))
        if (
            raw_thread_source is not None
            and raw_thread_source.lower() != "subagent"
            and is_subagent
        ):
            conflicts.add(
                "thread_source conflicts with subagent source metadata: "
                f"{_values_json([raw_thread_source])}"
            )
    parent_sets = {
        info.parent_thread_ids
        for info in source_infos
        if info.parent_thread_ids
    }
    if len(parent_sets) > 1:
        conflicts.add("parent_thread_ids have conflicting source values")
    parents = tuple(
        sorted(
            {
                parent
                for info in source_infos
                for parent in info.parent_thread_ids
            }
        )
    )
    for info in source_infos:
        if info.is_subagent:
            sources.update(info.metadata_sources)
        conflicts.update(info.metadata_conflicts)

    if legacy_names:
        sources.add("session_index.thread_name")
    if len(set(legacy_names)) > 1:
        conflicts.add(
            "legacy thread_name has conflicting values: "
            f"{_values_json(legacy_names)}"
        )
    legacy_name = legacy_names[0] if legacy_names else None
    display_name: str | None
    display_name_source: str | None
    if name is not None:
        display_name = name
        display_name_source = "threads.name"
    elif title is not None:
        display_name = title
        display_name_source = "threads.title"
    elif legacy_name is not None:
        display_name = legacy_name
        display_name_source = "session_index.thread_name"
    else:
        display_name = None
        display_name_source = None

    return ConversationSummary(
        thread_id=thread_id,
        name=name,
        title=title,
        display_name=display_name,
        display_name_source=display_name_source,
        cwd=cwd,
        project_label=project_label_from_cwd(cwd),
        git_origin_url=git_origin_url,
        is_subagent=is_subagent,
        agent_nickname=nickname,
        agent_role=role,
        agent_path=agent_path,
        parent_thread_ids=parents,
        archived=archived,
        indexed=indexed,
        originator=originator,
        metadata_sources=tuple(sorted(sources)),
        metadata_conflicts=tuple(sorted(conflicts)),
        metadata_evidence_fingerprints=tuple(
            sorted(evidence_fingerprints)
        ),
    )


def _rollout_records(
    value: RolloutRecord | Iterable[RolloutRecord] | None,
) -> tuple[RolloutRecord, ...]:
    if isinstance(value, RolloutRecord):
        return (value,)
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return ()
    try:
        return tuple(
            record for record in value if isinstance(record, RolloutRecord)
        )
    except TypeError:
        return ()


def _text_values(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if isinstance(value, str):
        text = _nonempty_string(value)
        return (text,) if text is not None else ()
    if value is None or isinstance(value, (bytes, Mapping)):
        return ()
    try:
        return tuple(
            text
            for item in value
            if (text := _nonempty_string(item)) is not None
        )
    except TypeError:
        return ()


def _row_text(
    row: Mapping[str, Any] | None,
    field: str,
    sources: set[str],
) -> str | None:
    if row is None or field not in row:
        return None
    sources.add(f"threads.{field}")
    return _nonempty_string(row.get(field))


def _row_bool(
    row: Mapping[str, Any] | None,
    field: str,
    sources: set[str],
) -> bool | None:
    if row is None or field not in row:
        return None
    sources.add(f"threads.{field}")
    value = row.get(field)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _merge_archived(
    database_value: bool | None,
    rollout_values: Iterable[bool],
    conflicts: set[str],
) -> bool | None:
    distinct = set(rollout_values)
    if len(distinct) > 1:
        conflicts.add("archived has conflicting rollout values")
    rollout_value = next(iter(distinct)) if len(distinct) == 1 else None
    if (
        database_value is not None
        and rollout_value is not None
        and database_value != rollout_value
    ):
        conflicts.add("archived differs between threads and rollout metadata")
    return database_value if database_value is not None else rollout_value


def _prefer_database_value(
    database_value: str | None,
    other_values: Iterable[str | None],
    field: str,
    conflicts: set[str],
) -> str | None:
    values = [
        value
        for item in other_values
        if (value := _nonempty_string(item)) is not None
    ]
    distinct = set(values)
    comparison_keys = {
        _metadata_comparison_key(field, value) for value in values
    }
    if len(comparison_keys) > 1:
        conflicts.add(
            f"{field} has conflicting non-database values: "
            f"{_values_json(distinct)}"
        )
    if database_value is not None:
        database_key = _metadata_comparison_key(field, database_value)
        if any(
            _metadata_comparison_key(field, value) != database_key
            for value in distinct
        ):
            conflicts.add(
                f"{field} differs between threads and other metadata: "
                f"threads={_values_json([database_value])},"
                f"other={_values_json(distinct)}"
            )
        return database_value
    if len(comparison_keys) == 1 and values:
        return min(
            distinct,
            key=lambda item: (_metadata_comparison_key(field, item), item),
        )
    return None


def _metadata_comparison_key(field: str, value: str) -> str:
    """Return a comparison key without changing the displayed metadata value.

    Codex Desktop can persist the same existing Windows directory with and
    without the extended-length prefix. Collapse those spellings only after
    filesystem identity is proven; missing paths remain distinct.
    Other metadata fields retain exact string comparison semantics.
    """

    if field != "cwd":
        return value
    if os.name == "nt" and ntpath.isabs(value):
        return canonical_existing_path_key(value)
    return value


def _append_text(values: list[str], value: object) -> None:
    text = _nonempty_string(value)
    if text is not None:
        values.append(text)


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _values_json(values: Iterable[str]) -> str:
    return json.dumps(
        sorted(set(values)),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _evidence_sha256(value: object) -> str:
    encoded = json.dumps(
        _evidence_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "sha256": hashlib.sha256(value).hexdigest(),
            "size": len(value),
        }
    if isinstance(value, Mapping):
        return {
            str(key): _evidence_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_evidence_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_evidence_json_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return {
        "type": type(value).__name__,
        "repr": repr(value),
    }
