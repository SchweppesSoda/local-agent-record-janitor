from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path

from codex_session_janitor.cli import (
    EXIT_CONFIRMATION_REQUIRED,
    EXIT_ERROR,
    EXIT_OK,
    main,
)
from codex_session_janitor.pi_sessions import build_pi_session_catalog


@dataclass(frozen=True)
class PiRecord:
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
    sha256: str
    action_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "path": str(self.path),
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "cwd": self.cwd,
            "active": self.active,
            "deletable": self.deletable,
            "stat_size": self.stat_size,
            "stat_mtime_ns": self.stat_mtime_ns,
        }


@dataclass(frozen=True)
class PiCatalog:
    records: tuple[PiRecord, ...]
    errors: tuple[object, ...] = ()

    @property
    def sessions(self) -> tuple[PiRecord, ...]:
        return self.records

    @property
    def failures(self) -> tuple[object, ...]:
        return self.errors

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "records": [record.to_dict() for record in self.records],
            "errors": list(self.errors),
        }


class TTYStringIO(StringIO):
    def isatty(self) -> bool:
        return True


class PiCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session_root = self.root / "agent" / "sessions"
        self.session_root.mkdir(parents=True)
        self.path = self.session_root / "session.jsonl"
        body = (
            b'{"type":"session","id":"pi-session-1","version":3,'
            b'"timestamp":"2026-08-01T00:00:00.000Z","cwd":"D:/work"}\n'
            b'{"type":"message","message":{"content":"super secret body"}}\n'
        )
        self.path.write_bytes(body)
        info = os.lstat(self.path)
        self.record = PiRecord(
            pi_root=self.root,
            session_root=self.session_root,
            path=self.path,
            session_id="pi-session-1",
            version=3,
            timestamp="2026-08-01T00:00:00.000Z",
            cwd="D:/work",
            parent_session=None,
            child_paths=(),
            active=False,
            deletable=True,
            blockers=(),
            stat_size=info.st_size,
            stat_mtime_ns=info.st_mtime_ns,
            sha256=hashlib.sha256(body).hexdigest(),
            action_id="pi:record:1",
        )
        self.catalog = PiCatalog((self.record,))

    def builder(self, **_kwargs: object) -> PiCatalog:
        return self.catalog

    def test_pi_records_json_is_one_document_and_never_contains_message_body(self) -> None:
        output = StringIO()
        status = main(
            ["records", "--platform", "pi", "--json"],
            adapters=(),
            stdin=StringIO(),
            stdout=output,
            stderr=StringIO(),
            pi_catalog_builder=self.builder,
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(status, EXIT_OK)
        self.assertEqual(payload["records"], [])
        self.assertEqual(payload["pi_count"], 1)
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["pi_sessions"][0]["session_id"], "pi-session-1")
        self.assertNotIn("super secret body", output.getvalue())

    def test_real_inventory_builder_and_cli_accept_custom_session_root(self) -> None:
        """Exercise the public Pi inventory object, not only an injected fake."""

        catalog = build_pi_session_catalog(
            agent_dir=self.root / "agent",
            session_root=self.session_root,
        )
        self.assertEqual(len(catalog.records), 1)
        record = catalog.records[0]
        self.assertEqual(record.agent_dir, self.root / "agent")
        self.assertEqual(record.session_root, self.session_root)
        self.assertEqual(record.path, self.path)
        self.assertEqual(record.session_id, "pi-session-1")

        output = StringIO()
        status = main(
            [
                "records", "--platform", "pi", "--pi-agent-dir", str(self.root / "agent"),
                "--pi-session-dir", str(self.session_root), "--json",
            ],
            stdin=StringIO(), stdout=output, stderr=StringIO(),
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(payload["pi_sessions"][0]["session_id"], "pi-session-1")
        self.assertNotIn("super secret body", output.getvalue())

    def test_default_mixed_records_keeps_codex_json_contract_and_adds_pi_fields(self) -> None:
        output = StringIO()
        status = main(
            ["records", "--json"],
            adapters=(),
            stdin=StringIO(),
            stdout=output,
            stderr=StringIO(),
            pi_catalog_builder=self.builder,
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(status, EXIT_OK)
        self.assertIn("records", payload)
        self.assertIn("errors", payload)
        self.assertIn("count", payload)
        self.assertIn("pi_sessions", payload)
        self.assertIn("pi_failures", payload)
        self.assertEqual(payload["total_count"], payload["count"] + payload["pi_count"])

    def test_explicit_injected_adapters_do_not_implicitly_read_real_pi_home(self) -> None:
        output = StringIO()
        status = main(
            ["records", "--json"],
            adapters=(),
            stdin=StringIO(),
            stdout=output,
            stderr=StringIO(),
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(status, EXIT_OK)
        self.assertNotIn("pi_sessions", payload)

    def test_pi_delete_preview_then_fingerprint_bound_execution_deletes_only_session_file(self) -> None:
        preview_output = StringIO()
        preview_status = main(
            ["delete", "--platform", "pi", "--session-id", "pi-session-1", "--json"],
            stdin=StringIO(), stdout=preview_output, stderr=StringIO(),
            pi_catalog_builder=self.builder,
        )
        preview = json.loads(preview_output.getvalue())
        self.assertEqual(preview_status, EXIT_CONFIRMATION_REQUIRED)
        self.assertTrue(self.path.exists())
        self.assertEqual(preview["platform"], "pi")

        execution_output = StringIO()
        execution_status = main(
            [
                "delete", "--platform", "pi", "--action-id", "pi:record:1",
                "--plan-fingerprint", preview["plan_fingerprint"], "--clients-closed",
                "--yes", "--json",
            ],
            stdin=StringIO(), stdout=execution_output, stderr=StringIO(),
            pi_catalog_builder=self.builder,
        )
        result = json.loads(execution_output.getvalue())
        self.assertEqual(execution_status, EXIT_OK)
        self.assertFalse(self.path.exists())
        self.assertEqual(result["results"][0]["status"], "deleted")

    def test_real_cli_delete_preserves_auth_settings_and_other_session(self) -> None:
        """The full public path may unlink only the single approved JSONL."""

        agent_dir = self.root / "agent"
        auth_path = agent_dir / "auth.json"
        settings_path = agent_dir / "settings.json"
        other_path = self.session_root / "z-other.jsonl"
        auth_path.write_bytes(b'{"oauth":"do-not-read-or-change"}\n')
        settings_path.write_bytes(b'{"sessionDir":"sessions"}\n')
        other_path.write_bytes(
            b'{"type":"session","id":"pi-session-2","version":3}\n'
            b'{"type":"message","message":{"content":"other secret"}}\n'
        )
        preserved = {
            auth_path: auth_path.read_bytes(),
            settings_path: settings_path.read_bytes(),
            other_path: other_path.read_bytes(),
        }
        common = [
            "--platform", "pi", "--pi-agent-dir", str(agent_dir),
            "--pi-session-dir", str(self.session_root),
        ]
        preview_output = StringIO()
        preview_status = main(
            ["delete", *common, "--session-id", "pi-session-1", "--json"],
            stdin=StringIO(), stdout=preview_output, stderr=StringIO(),
        )
        preview = json.loads(preview_output.getvalue())
        self.assertEqual(preview_status, EXIT_CONFIRMATION_REQUIRED)

        execution_output = StringIO()
        execution_status = main(
            [
                "delete", *common, "--action-id", preview["selected_actions"][0]["action_id"],
                "--plan-fingerprint", preview["plan_fingerprint"], "--clients-closed",
                "--yes", "--json",
            ],
            stdin=StringIO(), stdout=execution_output, stderr=StringIO(),
        )
        result = json.loads(execution_output.getvalue())
        self.assertEqual(execution_status, EXIT_OK)
        self.assertFalse(self.path.exists())
        self.assertEqual(result["results"][0]["status"], "deleted")
        for path, contents in preserved.items():
            self.assertEqual(path.read_bytes(), contents)

    def test_pi_mixed_delete_and_pi_scan_clean_are_explicit_errors(self) -> None:
        for command in (
            ["delete", "--platform", "pi", "--platform", "native", "--json"],
            ["scan", "--platform", "pi", "--json"],
            ["clean", "--platform", "pi", "--json"],
        ):
            output = StringIO()
            status = main(command, adapters=(), stdin=StringIO(), stdout=output, stderr=StringIO())
            payload = json.loads(output.getvalue())
            self.assertEqual(status, EXIT_ERROR)
            self.assertIn("error", payload)

        all_output = StringIO()
        all_status = main(
            ["scan", "--platform", "all", "--json"], adapters=(),
            stdin=StringIO(), stdout=all_output, stderr=StringIO(),
        )
        self.assertEqual(all_status, EXIT_OK)
        self.assertEqual(json.loads(all_output.getvalue())["command"], "scan")

    def test_pi_records_rejects_codex_thread_selector(self) -> None:
        output = StringIO()
        status = main(
            ["records", "--platform", "pi", "--thread-id", "pi-session-1", "--json"],
            adapters=(), stdin=StringIO(), stdout=output, stderr=StringIO(),
            pi_catalog_builder=self.builder,
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_ERROR)
        self.assertIn("Pi records 不支持 --thread-id", payload["error"]["message"])

    def test_tty_pi_delete_numbers_the_same_limited_actions_it_selects(self) -> None:
        other_path = self.session_root / "z-other.jsonl"
        other_body = b'{"type":"session","id":"pi-session-2","version":3}\n'
        other_path.write_bytes(other_body)
        info = os.lstat(other_path)
        other = replace(
            self.record,
            path=other_path,
            session_id="pi-session-2",
            timestamp=None,
            cwd=None,
            stat_size=info.st_size,
            stat_mtime_ns=info.st_mtime_ns,
            sha256=hashlib.sha256(other_body).hexdigest(),
            action_id="pi:record:2",
        )
        self.catalog = PiCatalog((self.record, other))
        input_stream = TTYStringIO("1\nPi 客户端已关闭并确认永久删除\n")
        output = TTYStringIO()

        status = main(
            ["delete", "--platform", "pi", "--limit", "1"],
            stdin=input_stream, stdout=output, stderr=StringIO(),
            pi_catalog_builder=self.builder,
        )
        rendered = output.getvalue()
        self.assertEqual(status, EXIT_OK)
        self.assertIn("1. Pi session ID：pi-session-1", rendered)
        self.assertIn("另有 1 条 Pi 删除目标未进入本次编号", rendered)
        self.assertFalse(self.path.exists())
        self.assertTrue(other_path.exists())
        self.assertEqual(rendered.count("Pi 永久删除最终计划"), 1)


if __name__ == "__main__":
    unittest.main()
