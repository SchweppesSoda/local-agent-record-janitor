from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from codex_session_janitor.pi_delete import (
    PiDeletePlanError,
    PiDeleteSelectionError,
    build_pi_delete_plan,
    execute_pi_delete,
)
from codex_session_janitor.pi_sessions import build_pi_session_catalog


@dataclass(frozen=True)
class Record:
    pi_root: Path
    session_root: Path
    path: Path
    session_id: str
    version: int | None
    timestamp: str | None
    cwd: str | None
    parent_session: str | None
    child_paths: tuple[Path, ...]
    active: bool
    deletable: bool
    blockers: tuple[str, ...]
    stat_size: int
    stat_mtime_ns: int
    stat_file_attributes: int
    sha256: str
    action_id: str


@dataclass(frozen=True)
class Failure:
    message: str
    blocks_delete: bool = True


@dataclass(frozen=True)
class Catalog:
    records: tuple[Record, ...]
    errors: tuple[Failure, ...] = ()


class PiDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pi_root = Path(self.temp.name) / ".pi"
        self.session_root = self.pi_root / "agent" / "sessions"
        self.session_root.mkdir(parents=True)

    def record(self, session_id: str, *, parent: str | None = None,
               children: tuple[Path, ...] = (), active: bool = False,
               path: Path | None = None) -> Record:
        path = path or self.session_root / f"{session_id}.jsonl"
        contents = (
            '{"type":"session","id":"' + session_id
            + '","version":1,"cwd":"D:/project"'
            + (f',"parentSession":"{parent}"' if parent else "") + "}\n"
            + '{"type":"message"}\n'
        ).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        info = os.lstat(path)
        return Record(self.pi_root, self.session_root, path, session_id, 1, None,
                      "D:/project", parent, children, active, True, (),
                      info.st_size, info.st_mtime_ns,
                      getattr(info, "st_file_attributes", 0),
                      hashlib.sha256(contents).hexdigest(), f"pi:{session_id}:{path.name}")

    def test_preview_is_read_only_and_binds_file_and_header_identity(self) -> None:
        record = self.record("one")
        before = record.path.read_bytes()

        plan = build_pi_delete_plan(Catalog((record,)))

        action = plan.actions[0]
        self.assertTrue(action.available)
        self.assertEqual(action.action_id, record.action_id)
        self.assertEqual(action.relative_path, "one.jsonl")
        self.assertEqual(action.approval_payload()["header"]["session_id"], "one")
        self.assertEqual(record.path.read_bytes(), before)

    def test_selection_rejects_all_and_duplicates_but_allows_independent_parent_child(self) -> None:
        child = self.record("child")
        parent = self.record("parent", children=(child.path,))
        same_id = replace(child, action_id=parent.action_id)
        plan = build_pi_delete_plan(Catalog((parent, child)))
        with self.assertRaises(PiDeleteSelectionError):
            plan.with_selected_actions(("all",))
        with self.assertRaises(PiDeleteSelectionError):
            plan.with_selected_actions((parent.action_id, parent.action_id))
        selected = plan.with_selected_actions((parent.action_id, child.action_id))
        self.assertEqual(len(selected.actions), 2)  # two exact unlinks, never a cascade
        duplicate = build_pi_delete_plan(Catalog((parent, same_id)))
        self.assertFalse(duplicate.actions[0].available)
        duplicate_uuid = build_pi_delete_plan(Catalog((parent, replace(child, session_id="parent"))))
        self.assertTrue(all(not action.available for action in duplicate_uuid.actions))

    def test_v1_header_without_version_is_a_valid_precise_target(self) -> None:
        record = self.record("v1")
        contents = b'{"type":"session","id":"v1","cwd":"D:/project"}\n'
        record.path.write_bytes(contents)
        info = os.lstat(record.path)
        v1 = replace(record, version=None, stat_size=info.st_size, stat_mtime_ns=info.st_mtime_ns,
                     sha256=hashlib.sha256(contents).hexdigest())
        catalog = Catalog((v1,))
        selected = build_pi_delete_plan(catalog).with_selected_actions((v1.action_id,))
        result = execute_pi_delete(selected, catalog_builder=lambda: catalog,
                                   approved_plan_fingerprint=selected.plan_fingerprint or "",
                                   clients_closed=True)
        self.assertEqual(result.deleted[0].status, "deleted")

    def test_real_pi_inventory_record_and_pi_session_file_activity_guard(self) -> None:
        path = self.session_root / "real.jsonl"
        path.write_text(
            '{"type":"session","id":"real","version":3,'
            '"timestamp":"2026-08-01T00:00:00.000Z","cwd":"D:/project"}\n',
            encoding="utf-8",
        )
        kwargs = {"agent_dir": self.pi_root / "agent", "session_root": self.session_root}
        active = build_pi_session_catalog(environ={"PI_SESSION_FILE": str(path)}, **kwargs)
        self.assertFalse(active.records[0].deletable)
        catalog = build_pi_session_catalog(environ={}, **kwargs)
        self.assertEqual(catalog.records[0].agent_dir, self.pi_root / "agent")
        plan = build_pi_delete_plan(catalog).with_selected_actions((catalog.records[0].action_id,))
        result = execute_pi_delete(plan, catalog_builder=lambda: build_pi_session_catalog(environ={}, **kwargs),
                                   approved_plan_fingerprint=plan.plan_fingerprint or "", clients_closed=True)
        self.assertEqual(result.deleted[0].status, "deleted")

    def test_catalog_failure_path_escape_and_active_session_fail_closed(self) -> None:
        record = self.record("one")
        blocked = build_pi_delete_plan(Catalog((record,), (Failure("read failed"),)))
        self.assertFalse(blocked.actions[0].available)
        escaped = replace(record, path=self.session_root.parent / "outside.jsonl", action_id="escape")
        escape_plan = build_pi_delete_plan(Catalog((escaped,)))
        self.assertFalse(escape_plan.actions[0].available)
        active = build_pi_delete_plan(Catalog((replace(record, active=True),)))
        self.assertFalse(active.actions[0].available)

    def test_approved_execution_unlinks_only_target_and_reports_preserved_child(self) -> None:
        child = self.record("child")
        parent = self.record("parent", children=(child.path,))
        catalog = Catalog((parent, child))
        selected = build_pi_delete_plan(catalog).with_selected_actions((parent.action_id,))

        result = execute_pi_delete(selected, catalog_builder=lambda: catalog,
                                   approved_plan_fingerprint=selected.plan_fingerprint or "",
                                   clients_closed=True)

        self.assertEqual(result.deleted[0].action_id, parent.action_id)
        self.assertFalse(parent.path.exists())
        self.assertTrue(child.path.exists())
        self.assertEqual(result.deleted[0].preserved_child_references, (str(child.path),))

    def test_fingerprint_drift_blocks_before_unlink(self) -> None:
        record = self.record("one")
        selected = build_pi_delete_plan(Catalog((record,))).with_selected_actions((record.action_id,))
        changed = replace(record, cwd="D:/other")
        unlinks: list[Path] = []
        with self.assertRaises(PiDeletePlanError):
            execute_pi_delete(selected, catalog_builder=lambda: Catalog((changed,)),
                              approved_plan_fingerprint=selected.plan_fingerprint or "",
                              clients_closed=True, unlink_fn=lambda path: unlinks.append(path))
        self.assertEqual(unlinks, [])
        self.assertTrue(record.path.exists())

    def test_immediate_activity_drift_blocks_before_unlink(self) -> None:
        record = self.record("one")
        original = Catalog((record,))
        active = Catalog((replace(record, active=True),))
        selected = build_pi_delete_plan(original).with_selected_actions((record.action_id,))
        calls = 0
        def builder() -> Catalog:
            nonlocal calls
            calls += 1
            return original if calls == 1 else active

        result = execute_pi_delete(selected, catalog_builder=builder,
                                   approved_plan_fingerprint=selected.plan_fingerprint or "",
                                   clients_closed=True)
        self.assertEqual(result.not_deleted[0].status, "not_deleted")
        self.assertTrue(record.path.exists())

    def test_reparse_or_replaced_header_never_unlinks(self) -> None:
        record = self.record("one")
        catalog = Catalog((record,))
        selected = build_pi_delete_plan(catalog).with_selected_actions((record.action_id,))
        reparse = SimpleNamespace(st_mode=stat.S_IFREG | 0o600,
                                  st_size=record.stat_size, st_mtime_ns=record.stat_mtime_ns,
                                  st_file_attributes=0x400)
        called: list[Path] = []
        result = execute_pi_delete(selected, catalog_builder=lambda: catalog,
                                   approved_plan_fingerprint=selected.plan_fingerprint or "",
                                   clients_closed=True, lstat_fn=lambda _path: reparse,
                                   unlink_fn=lambda path: called.append(path))
        self.assertEqual(result.not_deleted[0].status, "not_deleted")
        self.assertEqual(called, [])

    def test_planning_rejects_file_attribute_drift(self) -> None:
        record = self.record("one")
        # PiSessionRecord now supplies this value.  A different current value
        # must make the preview unavailable, before an execution is attempted.
        drifted = replace(record, stat_file_attributes=0x400)
        plan = build_pi_delete_plan(Catalog((drifted,)))
        self.assertFalse(plan.actions[0].available)
        self.assertIn("changed after inventory", plan.actions[0].unavailable_reasons[0])

    def test_replacement_after_preview_never_unlinks(self) -> None:
        record = self.record("one")
        catalog = Catalog((record,))
        selected = build_pi_delete_plan(catalog).with_selected_actions((record.action_id,))
        record.path.write_bytes(b'{"type":"session","id":"other","version":1,"cwd":"D:/project"}\n')
        called: list[Path] = []
        with self.assertRaises(PiDeletePlanError):
            execute_pi_delete(selected, catalog_builder=lambda: catalog,
                              approved_plan_fingerprint=selected.plan_fingerprint or "",
                              clients_closed=True, unlink_fn=lambda path: called.append(path))
        self.assertEqual(called, [])

    def test_partial_result_keeps_later_error_distinct(self) -> None:
        first = self.record("first")
        second = self.record("second")
        catalog = Catalog((first, second))
        plan = build_pi_delete_plan(catalog).with_selected_actions((first.action_id, second.action_id))
        def unlink(path: Path) -> None:
            if path == second.path:
                raise PermissionError("denied")
            path.unlink()
        result = execute_pi_delete(plan, catalog_builder=lambda: Catalog(
                                   (first, second) if first.path.exists() else (second,)),
                                   approved_plan_fingerprint=plan.plan_fingerprint or "",
                                   clients_closed=True, unlink_fn=unlink)
        self.assertEqual([item.status for item in result.results], ["deleted", "unknown"])
        self.assertFalse(first.path.exists())
        self.assertTrue(second.path.exists())


if __name__ == "__main__":
    unittest.main()
