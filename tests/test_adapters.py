from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from codex_session_janitor.adapters import (
    AdapterScanError,
    AionUIAdapter,
    CindyAdapter,
)
from codex_session_janitor.cleaner import scan_adapters

from tests.support import (
    create_aionui_database,
    create_cindy_database,
    create_thread_index,
    write_rollout,
)


class AionUIAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "aionui-backend.db"
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()

    def adapter(self) -> AionUIAdapter:
        return AionUIAdapter(
            database=self.database,
            codex_home=self.codex_home,
            codex_bin_hint=self.root / "does-not-exist",
        )

    def test_detects_residual_acp_mapping_after_conversation_deletion(self) -> None:
        thread_id = "aion-orphan"
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "codex-agent",
                    "agent_source": "builtin",
                    "session_status": "idle",
                    "last_active_at": 1_722_384_000_000,
                }
            ],
            metadata=[("codex-agent", "codex")],
        )
        rollout_path = write_rollout(
            self.codex_home, thread_id, originator="aionui-session"
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": thread_id,
                    "rollout_path": str(rollout_path),
                    "archived": 0,
                }
            ],
        )

        findings = self.adapter().scan()

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.platform, "aionui")
        self.assertEqual(finding.platform_session_id, "deleted-conversation")
        self.assertEqual(finding.thread_id, thread_id)
        self.assertTrue(finding.codex_indexed)
        self.assertFalse(finding.codex_archived)
        self.assertEqual(finding.rollout.path, rollout_path)
        self.assertEqual(finding.details["rollout_originator"], "aionui-session")
        self.assertTrue(finding.has_codex_artifacts)
        self.assertTrue(finding.details["cleanable"])
        self.assertTrue(finding.details["thread_delete_supported"])
        self.assertFalse(finding.details["needs_quarantine"])
        self.assertTrue(finding.details["cascade_safe"])
        self.assertEqual(finding.details["cascade_descendant_count"], 0)

    def test_existing_conversation_is_not_reported(self) -> None:
        thread_id = "aion-live"
        create_aionui_database(
            self.database,
            conversations=["live-conversation"],
            sessions=[
                {
                    "conversation_id": "live-conversation",
                    "session_id": thread_id,
                    "agent_id": "codex-agent",
                }
            ],
            metadata=[("codex-agent", "codex")],
        )
        write_rollout(self.codex_home, thread_id, originator="aionui-session")
        create_thread_index(self.codex_home, [{"id": thread_id}])

        self.assertEqual(self.adapter().scan(), [])

    def test_non_codex_mapping_with_foreign_rollout_is_not_reported(self) -> None:
        thread_id = "foreign-agent-thread"
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "gemini-agent",
                }
            ],
            metadata=[("gemini-agent", "gemini")],
        )
        write_rollout(self.codex_home, thread_id, originator="another-frontend")

        self.assertEqual(self.adapter().scan(), [])

    def test_aionui_originator_is_evidence_when_backend_is_not_codex(self) -> None:
        thread_id = "aion-originator-evidence"
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "stale-agent-metadata",
                }
            ],
            metadata=[("stale-agent-metadata", "gemini")],
        )
        write_rollout(self.codex_home, thread_id, originator="aionui-session")

        findings = self.adapter().scan()

        self.assertEqual([finding.thread_id for finding in findings], [thread_id])
        self.assertFalse(findings[0].details["cleanable"])
        self.assertTrue(findings[0].details["needs_quarantine"])
        self.assertEqual(findings[0].details["ownership_status"], "conflict")

    def test_without_metadata_foreign_originator_is_a_blocked_conflict(self) -> None:
        thread_id = "foreign-without-metadata"
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "unknown-agent",
                }
            ],
            include_metadata_table=False,
        )
        write_rollout(self.codex_home, thread_id, originator="another-frontend")

        findings = self.adapter().scan()

        self.assertEqual([finding.thread_id for finding in findings], [thread_id])
        self.assertFalse(findings[0].details["cleanable"])
        self.assertTrue(findings[0].details["originator_conflict"])
        self.assertTrue(findings[0].details["needs_quarantine"])

    def test_codex_backend_with_foreign_originator_is_blocked(self) -> None:
        thread_id = "codex-backend-foreign-origin"
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "codex-agent",
                }
            ],
            metadata=[("codex-agent", "codex")],
        )
        write_rollout(self.codex_home, thread_id, originator="cindy")

        finding = self.adapter().scan()[0]

        self.assertEqual(finding.details["ownership_status"], "conflict")
        self.assertTrue(finding.details["originator_conflict"])
        self.assertFalse(finding.details["cleanable"])
        self.assertIn("foreign originator", finding.details["cleanup_blocked_reason"])

    def test_index_only_residual_is_reported_for_explicit_codex_backend(self) -> None:
        thread_id = "index-only-aion"
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "codex-agent",
                }
            ],
            metadata=[("codex-agent", "codex")],
        )
        create_thread_index(
            self.codex_home, [{"id": thread_id, "archived": 1}]
        )

        findings = self.adapter().scan()

        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].rollout)
        self.assertTrue(findings[0].codex_indexed)
        self.assertTrue(findings[0].codex_archived)
        self.assertTrue(findings[0].has_codex_artifacts)
        self.assertTrue(findings[0].details["cleanable"])

    def test_unknown_backend_index_only_evidence_is_not_cleanable(self) -> None:
        thread_id = "unknown-backend-index-only"
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "unknown-agent",
                }
            ],
            include_metadata_table=False,
        )
        create_thread_index(self.codex_home, [{"id": thread_id}])

        finding = self.adapter().scan()[0]

        self.assertEqual(finding.details["ownership_status"], "insufficient")
        self.assertFalse(finding.details["cleanable"])
        self.assertTrue(finding.details["needs_quarantine"])

    def test_stale_mapping_sharing_a_live_thread_is_audited_but_blocked(self) -> None:
        thread_id = "shared-aion-thread"
        create_aionui_database(
            self.database,
            conversations=["live-conversation"],
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "codex-agent",
                },
                {
                    "conversation_id": "live-conversation",
                    "session_id": thread_id,
                    "agent_id": "codex-agent",
                },
            ],
            metadata=[("codex-agent", "codex")],
        )
        write_rollout(self.codex_home, thread_id, originator="aionui-session")

        adapter = self.adapter()
        finding = adapter.scan()[0]

        self.assertEqual(finding.details["live_reference_count"], 1)
        self.assertFalse(finding.details["cleanable"])
        self.assertIn(thread_id, adapter.live_thread_ids)

    def test_live_thread_inventory_excludes_foreign_backend(self) -> None:
        create_aionui_database(
            self.database,
            conversations=["codex-live", "foreign-live"],
            sessions=[
                {
                    "conversation_id": "codex-live",
                    "session_id": "codex-thread",
                    "agent_id": "codex-agent",
                },
                {
                    "conversation_id": "foreign-live",
                    "session_id": "foreign-thread",
                    "agent_id": "gemini-agent",
                },
            ],
            metadata=[
                ("codex-agent", "codex"),
                ("gemini-agent", "gemini"),
            ],
        )

        adapter = self.adapter()
        self.assertEqual(adapter.scan(), [])
        self.assertEqual(adapter.live_thread_ids, frozenset({"codex-thread"}))

    def test_live_unknown_backend_requires_matching_aionui_originator(self) -> None:
        create_aionui_database(
            self.database,
            conversations=["aion-live", "foreign-live"],
            sessions=[
                {
                    "conversation_id": "aion-live",
                    "session_id": "aion-thread",
                    "agent_id": "unknown-aion-agent",
                },
                {
                    "conversation_id": "foreign-live",
                    "session_id": "foreign-thread",
                    "agent_id": "unknown-foreign-agent",
                },
            ],
            include_metadata_table=False,
        )
        write_rollout(
            self.codex_home, "aion-thread", originator="aionui-session"
        )
        write_rollout(self.codex_home, "foreign-thread", originator="cindy")

        adapter = self.adapter()
        self.assertEqual(adapter.scan(), [])
        self.assertEqual(adapter.live_thread_ids, frozenset({"aion-thread"}))

    def test_known_descendant_blocks_cascading_thread_delete(self) -> None:
        thread_id = "aion-parent"
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "codex-agent",
                }
            ],
            metadata=[("codex-agent", "codex")],
        )
        rollout_path = write_rollout(
            self.codex_home, thread_id, originator="aionui-session"
        )
        create_thread_index(
            self.codex_home,
            [{"id": thread_id, "rollout_path": str(rollout_path)}],
            spawn_edges=[
                {
                    "parent_thread_id": thread_id,
                    "child_thread_id": "still-live-child",
                    "status": "open",
                }
            ],
        )

        finding = self.adapter().scan()[0]

        self.assertFalse(finding.details["cascade_safe"])
        self.assertEqual(finding.details["cascade_descendant_count"], 1)
        self.assertFalse(finding.details["cleanable"])

    def test_missing_spawn_edge_evidence_fails_closed(self) -> None:
        thread_id = "aion-cascade-unknown"
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "codex-agent",
                }
            ],
            metadata=[("codex-agent", "codex")],
        )
        create_thread_index(
            self.codex_home,
            [{"id": thread_id}],
            include_spawn_edges_table=False,
        )

        finding = self.adapter().scan()[0]

        self.assertFalse(finding.details["cascade_check_available"])
        self.assertFalse(finding.details["cascade_safe"])
        self.assertFalse(finding.details["cleanable"])
        self.assertTrue(finding.details["needs_quarantine"])

    def test_resolved_mapping_without_codex_artifact_is_not_reported(self) -> None:
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": "already-resolved",
                    "agent_id": "codex-agent",
                }
            ],
            metadata=[("codex-agent", "codex")],
        )

        self.assertEqual(self.adapter().scan(), [])

    def test_blank_session_id_is_ignored(self) -> None:
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-null",
                    "session_id": None,
                    "agent_id": "codex-agent",
                },
                {
                    "conversation_id": "deleted-blank",
                    "session_id": "  ",
                    "agent_id": "codex-agent",
                },
            ],
            metadata=[("codex-agent", "codex")],
        )

        self.assertEqual(self.adapter().scan(), [])

    def test_missing_database_is_empty_but_corrupt_database_is_an_error(self) -> None:
        self.assertEqual(self.adapter().scan(), [])
        self.database.write_text("not sqlite", encoding="utf-8")
        with self.assertRaises(AdapterScanError):
            self.adapter().scan()

        report = scan_adapters([self.adapter()])
        self.assertEqual(report.findings, [])
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].error_type, "AdapterScanError")

    def test_incompatible_schema_is_an_error(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE unrelated (id TEXT)")
            connection.commit()

        with self.assertRaises(AdapterScanError):
            self.adapter().scan()

    def test_corrupt_codex_state_database_is_an_error(self) -> None:
        thread_id = "state-error"
        create_aionui_database(
            self.database,
            sessions=[
                {
                    "conversation_id": "deleted-conversation",
                    "session_id": thread_id,
                    "agent_id": "codex-agent",
                }
            ],
            metadata=[("codex-agent", "codex")],
        )
        (self.codex_home / "state_5.sqlite").write_text(
            "not sqlite",
            encoding="utf-8",
        )

        with self.assertRaises(AdapterScanError):
            self.adapter().scan()


class CindyAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "cindy.db"
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()

    def adapter(self) -> CindyAdapter:
        return CindyAdapter(
            database=self.database,
            codex_home=self.codex_home,
            codex_bin_hint=self.root / "does-not-exist",
        )

    @staticmethod
    def session(
        *,
        session_id: str,
        thread_id: str | None,
        status: str | None,
        agent_kind: str = "codex",
    ) -> dict[str, object]:
        return {
            "id": session_id,
            "sdk_session_id": thread_id,
            "status": status,
            "source": "desktop",
            "created_at": 1_722_383_000_000,
            "updated_at": 1_722_384_000_000,
            "parent_session_id": None,
            "agent_kind": agent_kind,
        }

    def test_detects_soft_deleted_cindy_session(self) -> None:
        thread_id = "cindy-orphan"
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="deleted-session",
                    thread_id=thread_id,
                    status="deleted",
                )
            ],
        )
        rollout_path = write_rollout(
            self.codex_home, thread_id, originator="cindy", archived=True
        )
        create_thread_index(
            self.codex_home,
            [
                {
                    "id": thread_id,
                    "rollout_path": str(rollout_path),
                    "archived": 1,
                }
            ],
        )

        findings = self.adapter().scan()

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.platform, "cindy")
        self.assertEqual(finding.platform_session_id, "deleted-session")
        self.assertEqual(finding.thread_id, thread_id)
        self.assertTrue(finding.codex_indexed)
        self.assertTrue(finding.codex_archived)
        self.assertTrue(finding.rollout.archived)
        self.assertEqual(finding.details["session_status"], "deleted")
        self.assertTrue(finding.details["cleanable"])
        self.assertTrue(finding.details["thread_delete_supported"])
        self.assertFalse(finding.details["needs_quarantine"])
        self.assertTrue(finding.details["cascade_safe"])

    def test_live_and_non_codex_sessions_are_not_reported(self) -> None:
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="live-codex",
                    thread_id="live-thread",
                    status="active",
                ),
                self.session(
                    session_id="deleted-gemini",
                    thread_id="gemini-thread",
                    status="deleted",
                    agent_kind="gemini",
                ),
            ],
        )
        write_rollout(self.codex_home, "live-thread", originator="cindy")
        write_rollout(self.codex_home, "gemini-thread", originator="cindy")

        self.assertEqual(self.adapter().scan(), [])

    def test_foreign_rollout_originator_is_a_blocked_conflict(self) -> None:
        thread_id = "foreign-originator"
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="deleted-session",
                    thread_id=thread_id,
                    status="deleted",
                )
            ],
        )
        write_rollout(self.codex_home, thread_id, originator="aionui-session")

        finding = self.adapter().scan()[0]

        self.assertEqual(finding.details["ownership_status"], "conflict")
        self.assertTrue(finding.details["originator_conflict"])
        self.assertFalse(finding.details["cleanable"])
        self.assertTrue(finding.details["needs_quarantine"])

    def test_missing_originator_is_accepted_for_legacy_cindy_rollout(self) -> None:
        thread_id = "legacy-cindy"
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="deleted-session",
                    thread_id=thread_id,
                    status="deleted",
                )
            ],
        )
        write_rollout(self.codex_home, thread_id, originator=None)
        create_thread_index(self.codex_home, [])

        findings = self.adapter().scan()

        self.assertEqual([finding.thread_id for finding in findings], [thread_id])
        self.assertTrue(findings[0].details["cleanable"])

    def test_index_only_soft_deleted_session_is_reported(self) -> None:
        thread_id = "index-only-cindy"
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="deleted-session",
                    thread_id=thread_id,
                    status="deleted",
                )
            ],
        )
        create_thread_index(self.codex_home, [{"id": thread_id}])

        findings = self.adapter().scan()

        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].rollout)
        self.assertTrue(findings[0].codex_indexed)
        self.assertTrue(findings[0].details["cleanable"])

    def test_stale_session_sharing_a_live_thread_is_audited_but_blocked(self) -> None:
        thread_id = "shared-cindy-thread"
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="deleted-session",
                    thread_id=thread_id,
                    status="deleted",
                ),
                self.session(
                    session_id="live-session",
                    thread_id=thread_id,
                    status="active",
                ),
            ],
        )
        write_rollout(self.codex_home, thread_id, originator="cindy")

        adapter = self.adapter()
        finding = adapter.scan()[0]

        self.assertEqual(finding.details["live_reference_count"], 1)
        self.assertFalse(finding.details["cleanable"])
        self.assertIn(thread_id, adapter.live_thread_ids)

    def test_null_status_is_treated_as_live_and_blocks_cleanup(self) -> None:
        thread_id = "unknown-status-thread"
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="deleted-session",
                    thread_id=thread_id,
                    status="deleted",
                ),
                self.session(
                    session_id="unknown-session",
                    thread_id=thread_id,
                    status=None,
                ),
            ],
        )
        write_rollout(self.codex_home, thread_id, originator="cindy")

        adapter = self.adapter()
        finding = adapter.scan()[0]

        self.assertEqual(finding.details["live_reference_count"], 1)
        self.assertFalse(finding.details["cleanable"])
        self.assertIn(thread_id, adapter.live_thread_ids)

    def test_live_thread_inventory_excludes_non_codex_agent_kind(self) -> None:
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="live-codex",
                    thread_id="codex-thread",
                    status="active",
                ),
                self.session(
                    session_id="live-gemini",
                    thread_id="foreign-thread",
                    status="active",
                    agent_kind="gemini",
                ),
            ],
        )

        adapter = self.adapter()
        self.assertEqual(adapter.scan(), [])
        self.assertEqual(adapter.live_thread_ids, frozenset({"codex-thread"}))

    def test_known_descendant_blocks_cascading_thread_delete(self) -> None:
        thread_id = "cindy-parent"
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="deleted-session",
                    thread_id=thread_id,
                    status="deleted",
                )
            ],
        )
        rollout_path = write_rollout(
            self.codex_home, thread_id, originator="cindy"
        )
        create_thread_index(
            self.codex_home,
            [{"id": thread_id, "rollout_path": str(rollout_path)}],
            spawn_edges=[
                {
                    "parent_thread_id": thread_id,
                    "child_thread_id": "still-live-child",
                    "status": "closed",
                }
            ],
        )

        finding = self.adapter().scan()[0]

        self.assertFalse(finding.details["cascade_safe"])
        self.assertEqual(
            finding.details["cascade_descendant_thread_ids"],
            ["still-live-child"],
        )
        self.assertFalse(finding.details["cleanable"])

    def test_missing_spawn_edge_evidence_fails_closed(self) -> None:
        thread_id = "cindy-cascade-unknown"
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="deleted-session",
                    thread_id=thread_id,
                    status="deleted",
                )
            ],
        )
        write_rollout(self.codex_home, thread_id, originator="cindy")

        finding = self.adapter().scan()[0]

        self.assertFalse(finding.details["cascade_check_available"])
        self.assertFalse(finding.details["cascade_safe"])
        self.assertFalse(finding.details["cleanable"])
        self.assertTrue(finding.details["needs_quarantine"])

    def test_resolved_soft_delete_without_codex_artifact_is_not_reported(self) -> None:
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="deleted-session",
                    thread_id="already-resolved",
                    status="deleted",
                )
            ],
        )

        self.assertEqual(self.adapter().scan(), [])

    def test_blank_sdk_session_id_is_ignored(self) -> None:
        create_cindy_database(
            self.database,
            [
                self.session(
                    session_id="null-thread",
                    thread_id=None,
                    status="deleted",
                ),
                self.session(
                    session_id="blank-thread",
                    thread_id=" ",
                    status="deleted",
                ),
            ],
        )

        self.assertEqual(self.adapter().scan(), [])

    def test_missing_database_is_empty_but_corrupt_database_is_an_error(self) -> None:
        self.assertEqual(self.adapter().scan(), [])
        self.database.write_text("not sqlite", encoding="utf-8")
        with self.assertRaises(AdapterScanError):
            self.adapter().scan()

        report = scan_adapters([self.adapter()])
        self.assertEqual(report.findings, [])
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].error_type, "AdapterScanError")

    def test_incompatible_schema_is_an_error(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE unrelated (id TEXT)")
            connection.commit()

        with self.assertRaises(AdapterScanError):
            self.adapter().scan()


if __name__ == "__main__":
    unittest.main()
