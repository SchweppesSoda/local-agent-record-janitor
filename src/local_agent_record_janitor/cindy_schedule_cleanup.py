"""Exact Cindy schedule-run deletion; schedules and conversations are preserved.

Only explicit ``schedule-run:<full-id>`` selectors enter this family. Bodies,
prompts, hook output and run result text are never read into evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from .codex_desktop_state import running_related_clients
from .frontend_session_cleanup import (
    _require_client_closed, _validate_database,
)
from .sqlite_utils import connect_readonly

KIND = "delete_schedule_run"
PREFIX = "schedule-run:"
TERMINAL = {"success", "failed", "skipped", "aborted", "interrupted"}
RUN_COLUMNS = {
    "id", "schedule_id", "session_id", "fired_at", "finished_at", "status",
    "error_msg", "read_at", "result_text", "heartbeat_at", "pre_run_hook_result",
    "cost_usd", "estimated_value_usd", "cost_attribution", "cost_amount",
    "estimated_value_amount", "cost_currency", "cost_is_approximate",
}
META = ("id", "schedule_id", "session_id", "fired_at", "finished_at", "status",
        "read_at", "heartbeat_at", "cost_usd", "estimated_value_usd",
        "cost_attribution", "cost_amount", "estimated_value_amount",
        "cost_currency", "cost_is_approximate")

# Installed Cindy schema observed 2026-09-29. Match complete definitions, not
# trigger names. Unknown versions fail closed and remain inventory-only.
TRIGGERS = {
    "schedule_session_latest_run_insert": """CREATE TRIGGER schedule_session_latest_run_insert
    AFTER INSERT ON schedule_runs WHEN NEW.session_id IS NOT NULL BEGIN
      INSERT INTO schedule_session_latest_runs (session_id, run_id, fired_at)
      VALUES (NEW.session_id, NEW.id, NEW.fired_at)
      ON CONFLICT(session_id) DO UPDATE SET run_id = excluded.run_id, fired_at = excluded.fired_at
      WHERE excluded.fired_at > schedule_session_latest_runs.fired_at
        OR (excluded.fired_at = schedule_session_latest_runs.fired_at
          AND excluded.run_id > schedule_session_latest_runs.run_id); END""",
    "schedule_session_latest_run_delete": """CREATE TRIGGER schedule_session_latest_run_delete
    AFTER DELETE ON schedule_runs WHEN OLD.session_id IS NOT NULL BEGIN
      DELETE FROM schedule_session_latest_runs WHERE session_id = OLD.session_id AND run_id = OLD.id;
      INSERT INTO schedule_session_latest_runs (session_id, run_id, fired_at)
      SELECT OLD.session_id, id, fired_at FROM schedule_runs WHERE session_id = OLD.session_id
      ORDER BY fired_at DESC, id DESC LIMIT 1 ON CONFLICT(session_id) DO UPDATE SET
        run_id = excluded.run_id, fired_at = excluded.fired_at; END""",
    "schedule_session_latest_run_update": """CREATE TRIGGER schedule_session_latest_run_update
    AFTER UPDATE OF session_id, fired_at ON schedule_runs BEGIN
      DELETE FROM schedule_session_latest_runs WHERE session_id = OLD.session_id AND run_id = OLD.id;
      INSERT INTO schedule_session_latest_runs (session_id, run_id, fired_at)
      SELECT OLD.session_id, id, fired_at FROM schedule_runs
      WHERE OLD.session_id IS NOT NULL AND session_id = OLD.session_id
      ORDER BY fired_at DESC, id DESC LIMIT 1 ON CONFLICT(session_id) DO UPDATE SET
        run_id = excluded.run_id, fired_at = excluded.fired_at;
      INSERT INTO schedule_session_latest_runs (session_id, run_id, fired_at)
      SELECT NEW.session_id, NEW.id, NEW.fired_at WHERE NEW.session_id IS NOT NULL
      ON CONFLICT(session_id) DO UPDATE SET run_id = excluded.run_id, fired_at = excluded.fired_at
      WHERE excluded.fired_at > schedule_session_latest_runs.fired_at
        OR (excluded.fired_at = schedule_session_latest_runs.fired_at
          AND excluded.run_id > schedule_session_latest_runs.run_id); END""",
}


class ScheduleCleanupError(RuntimeError):
    def __init__(self, message, *, rolled_back=False, unknown=False):
        super().__init__(message)
        self.outcome_known_rolled_back = rolled_back
        self.outcome_unknown = unknown


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def _sql(value):
    return re.sub(r"\s+", " ", value).strip().rstrip(";").lower()


def _schema(db):
    objects = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    for table, expected in (("schedule_runs", RUN_COLUMNS),
                            ("schedule_session_latest_runs", {"session_id", "run_id", "fired_at"})):
        columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
        if {r[1] for r in columns} != expected:
            raise ScheduleCleanupError("Unsupported Cindy schedule columns: " + table)
        primary = "id" if table == "schedule_runs" else "session_id"
        if [(r[1], r[2].lower()) for r in columns if r[5]] != [(primary, "text")]:
            raise ScheduleCleanupError("Unsupported schedule primary key")
    expected_fks = {
        "schedule_runs": {("schedules", "schedule_id", "id", "NO ACTION", "CASCADE"),
                          ("sessions", "session_id", "id", "NO ACTION", "SET NULL")},
        "schedule_session_latest_runs": {("sessions", "session_id", "id", "NO ACTION", "CASCADE"),
                                         ("schedule_runs", "run_id", "id", "NO ACTION", "CASCADE")},
    }
    for table, expected in expected_fks.items():
        actual = {tuple(r[2:7]) for r in db.execute(f'PRAGMA foreign_key_list("{table}")')}
        if actual != expected:
            raise ScheduleCleanupError("Unsupported schedule foreign keys")
    triggers = {r[1]: _sql(r[3]) for r in objects
                if r[0] == "trigger" and r[2] in expected_fks}
    if triggers != {name: _sql(value) for name, value in TRIGGERS.items()}:
        raise ScheduleCleanupError("Unsupported schedule triggers")
    for kind, table, _, _definition in objects:
        if kind != "table":
            continue
        quoted = table.replace('"', '""')
        for fk in db.execute(f'PRAGMA foreign_key_list("{quoted}")'):
            if fk[2] in expected_fks and not (table == "schedule_session_latest_runs"
                                             and tuple(fk[2:7]) in expected_fks[table]):
                raise ScheduleCleanupError("Unsupported inbound schedule dependency: " + table)
    return _hash([tuple(row) for row in objects])


def _rows(db):
    return {r[0]: dict(zip(META, r)) for r in
            db.execute('SELECT ' + ','.join(META) + ' FROM schedule_runs ORDER BY id')}


def _links(db):
    return [tuple(r) for r in db.execute(
        'SELECT session_id,run_id,fired_at FROM schedule_session_latest_runs ORDER BY session_id')]


def _expected_links(rows, links, ids):
    affected = {rows[k]['session_id'] for k in ids} - {None}
    expected = {r[0]: r for r in links if r[0] not in affected}
    for session_id in affected:
        candidates = [r for k, r in rows.items() if k not in ids and r['session_id'] == session_id]
        if candidates:
            latest = max(candidates, key=lambda r: (r['fired_at'], r['id']))
            expected[session_id] = (session_id, latest['id'], latest['fired_at'])
    return sorted(expected.values())


def _recovery_directory(evidence):
    return Path(evidence['database']).parent / ('.larj-cindy-runs-' + evidence['batch_binding'])


def _discard_recovery(directory):
    """Remove only the proven private SQLite copy and its named sidecars."""
    allowed = {'database.sqlite', 'database.sqlite-wal', 'database.sqlite-shm'}
    entries = list(directory.iterdir())
    for path in entries:
        info = path.lstat()
        if (path.name not in allowed or not stat.S_ISREG(info.st_mode) or
                info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ScheduleCleanupError('Unexpected recovery artifact; preserve evidence')
    # Keep the original copy until sidecars are gone, so a cleanup failure can
    # still re-prove the frozen original. No recursive directory deletion.
    for name in ('database.sqlite-wal', 'database.sqlite-shm', 'database.sqlite'):
        (directory / name).unlink(missing_ok=True)
    directory.rmdir()


def freeze(database, owner_root, run_ids):
    database = Path(database).absolute()
    _validate_database(database)
    with closing(connect_readonly(database)) as db:
        db.execute("BEGIN")
        schema = _schema(db)
        rows = _rows(db)
        links = _links(db)
    selected = set(run_ids).intersection(rows)
    binding = _hash([str(database), sorted(selected), schema, _hash(rows), _hash(links)])
    after_rows = {k: v for k, v in rows.items() if k not in selected}
    after_links = _expected_links(rows, links, selected)
    result = []
    for run_id in sorted(set(run_ids)):
        row = rows.get(run_id)
        if row is None:
            continue  # Terminal verification can revisit already absent IDs.
        if row['status'] not in TERMINAL or row['finished_at'] is None:
            raise ScheduleCleanupError("Run is not terminal: " + run_id)
        result.append({"database": str(database), "owner_process_root": str(Path(owner_root).absolute()),
                       "run_id": run_id, "row": row, "schema_sha256": schema,
                       "links_sha256": _hash(links), "runs_sha256": _hash(rows),
                       "batch_binding": binding, "selected_run_ids": sorted(selected),
                       "after_runs_sha256": _hash(after_rows), "after_links_sha256": _hash(after_links)})
    return result


def remaining(evidence):
    if not evidence:
        raise ScheduleCleanupError("Missing schedule evidence")
    databases = {e['database'] for e in evidence}
    if len(databases) != 1:
        raise ScheduleCleanupError("Schedule batch must have one database")
    database = Path(next(iter(databases)))
    _validate_database(database)
    with closing(connect_readonly(database)) as db:
        db.execute('BEGIN')
        schema = _schema(db)
        ids = {e['run_id'] for e in evidence}
        runs = _rows(db)
        links = _links(db)
        result = sorted(ids.intersection(runs) | {r[1] for r in links if r[1] in ids})
    # A post-commit crash may leave the rollback copy. Only discard it after
    # proving both the frozen original and a whole-batch before/after state.
    for binding in {e['batch_binding']: e for e in evidence}.values():
        directory = _recovery_directory(binding)
        if not directory.exists():
            continue
        if directory.is_symlink() or getattr(directory.stat(), 'st_file_attributes', 0) & 0x400:
            raise ScheduleCleanupError('Unsafe recovery directory')
        backup = directory / 'database.sqlite'
        actual = (schema, _hash(runs), _hash(links))
        before = (binding['schema_sha256'], binding['runs_sha256'], binding['links_sha256'])
        after = (binding['schema_sha256'], binding['after_runs_sha256'], binding['after_links_sha256'])
        if actual not in (before, after):
            raise ScheduleCleanupError('Recovery state is not the frozen before/after state')
        if backup.exists():
            _validate_database(backup)
            with closing(connect_readonly(backup)) as original:
                if (_schema(original) != binding['schema_sha256'] or
                        _hash(_rows(original)) != binding['runs_sha256'] or
                        _hash(_links(original)) != binding['links_sha256']):
                    raise ScheduleCleanupError('Recovery copy does not prove frozen original')
        else:
            # Recovery of an interrupted *cleanup*, after the verified main
            # copy was unlinked. No WAL frames may contain unverified data.
            wal = directory / 'database.sqlite-wal'
            if actual != after or (wal.exists() and wal.stat().st_size != 0):
                raise ScheduleCleanupError('Missing recovery copy without exact completed state')
        _discard_recovery(directory)
    return result


@dataclass(frozen=True)
class ScheduleCleanupResult:
    deleted_ids: tuple[str, ...]
    status: str = "deleted"

    def to_dict(self):
        return {"status": self.status, "deleted_ids": list(self.deleted_ids),
                "deleted_run_count": len(self.deleted_ids), "verified": True}


def execute(evidence, *, client_inspector=None, phase_callback=None):
    if not evidence or len({e['database'] for e in evidence}) != 1 or len(
            {e['owner_process_root'] for e in evidence}) != 1:
        raise ScheduleCleanupError("Exact single-store schedule evidence required")
    ids = {e['run_id'] for e in evidence}
    if len(ids) != len(evidence):
        raise ScheduleCleanupError("Duplicate run selection")
    if any(set(e['selected_run_ids']) != ids for e in evidence):
        raise ScheduleCleanupError('Frozen schedule batch selection differs')
    database = Path(evidence[0]['database'])
    owner = Path(evidence[0]['owner_process_root'])
    _validate_database(database)
    inspector = client_inspector if client_inspector is not None else running_related_clients
    _require_client_closed(owner, inspector, owner_client='cindy')
    current = freeze(database, owner, ids)
    if sorted(current, key=lambda e: e['run_id']) != sorted(evidence, key=lambda e: e['run_id']):
        raise ScheduleCleanupError("Schedule evidence changed")
    directory = _recovery_directory(evidence[0])
    directory.mkdir()  # Existing recovery evidence must never be overwritten.
    backup = directory / 'database.sqlite'
    with closing(connect_readonly(database)) as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
    committed = False
    db = None
    try:
        db = sqlite3.connect(database)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        schema, before, links = _schema(db), _rows(db), _links(db)
        if any(schema != e['schema_sha256'] or _hash(before) != e['runs_sha256']
               or _hash(links) != e['links_sha256'] or before.get(e['run_id']) != e['row']
               for e in evidence):
            raise ScheduleCleanupError("Schedule evidence changed under transaction")
        _require_client_closed(owner, inspector, owner_client='cindy')
        if phase_callback:
            phase_callback('mutation_started')
        for run_id in sorted(ids):
            if db.execute('DELETE FROM schedule_runs WHERE id=?', (run_id,)).rowcount != 1:
                raise ScheduleCleanupError("Unexpected affected run count")
        expected = {k: v for k, v in before.items() if k not in ids}
        if _rows(db) != expected:
            raise ScheduleCleanupError("Unselected schedule runs changed")
        if _links(db) != _expected_links(before, links, ids):
            raise ScheduleCleanupError("Latest-run dependency verification failed")
        db.commit()
        committed = True
        db.close()
        db = None
        if remaining(evidence):
            raise ScheduleCleanupError("Deleted schedule records remain")
        if phase_callback:
            phase_callback('verified')
        return ScheduleCleanupResult(tuple(sorted(ids)))
    except Exception as exc:
        if db is not None:
            try:
                db.rollback()
            finally:
                db.close()
        rolled_back = False
        if not committed:
            try:
                rolled_back = set(remaining(evidence)) == ids
            except Exception:
                pass
        raise ScheduleCleanupError(
            str(exc) + (f'; recovery copy: {backup}' if backup.exists() else ''),
            rolled_back=rolled_back, unknown=not rolled_back) from exc


def merge_context(context, adapters, service, selectors):
    ids = {value[len(PREFIX):] for value in selectors if value.startswith(PREFIX)}
    if not ids:
        return context
    from .planning import ActionImpact, ActionKind, CandidateAction, RiskLevel, TargetRef, storage_id_for_path
    actions = list(context.plan.actions)
    seen = set()
    for adapter in adapters:
        if getattr(adapter, 'name', '') != 'cindy':
            continue
        database = Path(adapter.database).absolute()
        if str(database).casefold() in seen:
            continue
        seen.add(str(database).casefold())
        for evidence in freeze(database, adapter.owner_process_root, ids):
            run_id = evidence['run_id']
            actions.append(CandidateAction(
                action_id=KIND + ':' + _hash([str(database), run_id])[:32],
                kind=ActionKind.DELETE_SCHEDULE_RUN,
                target=TargetRef(storage_id_for_path(database.parent), PREFIX + run_id),
                risk=RiskLevel.HIGH, available=True, unavailable_reason=None,
                impact=ActionImpact(owner_client='cindy', owner_process_root=evidence['owner_process_root'],
                    resource_path=str(database), external_storage_root=str(database.parent),
                    external_engine='codex', external_action_payload={'schedule_run_evidence': evidence}),
                snapshot_fingerprint=_hash(evidence), requires_explicit_selection=True,
                resource_kind='schedule_run'))
    plan = replace(context.plan, actions=tuple(actions),
                   plan_fingerprint='schedule:v1:' + _hash([a.to_dict() for a in actions]))
    return replace(context, plan=plan, actions=service.typed_actions(plan))
