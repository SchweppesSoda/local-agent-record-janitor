from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.cleaner import ScanFailure, ScanReport
from local_agent_record_janitor.models import Finding
from local_agent_record_janitor.planning import (
    ActionImpact,
    ActionKind,
    CandidateAction,
    CleanupPlan,
    Observation,
    RiskLevel,
    TargetRef,
    storage_id_for_path,
)


class _Adapter:
    def __init__(self, name: str, codex_home: Path) -> None:
        self.name = name
        self.codex_home = codex_home


class CleanupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.database = self.root / "frontend.sqlite"

    def finding(self, platform: str, thread_id: str) -> Finding:
        return Finding(
            platform=platform,
            platform_session_id=f"session-{thread_id}",
            thread_id=thread_id,
            reason="presentation text must not drive decisions",
            platform_db=self.database,
            codex_home=self.home,
            details={
                "finding_type": "frontend_deleted_reference",
                "diagnostic_artifact_present": True,
            },
        )

    def test_prepare_calls_one_scanner_and_one_planner(self) -> None:
        calls = {"scan": 0, "plan": 0}
        report = ScanReport(
            findings=[
                self.finding("cindy", "cindy-target"),
                self.finding("aionui", "aion-target"),
            ]
        )

        def scanner(_adapters: object, **_kwargs: object) -> ScanReport:
            calls["scan"] += 1
            return report

        def planner(filtered: ScanReport) -> CleanupPlan:
            calls["plan"] += 1
            self.assertEqual(
                [finding.thread_id for finding in filtered.findings],
                ["cindy-target"],
            )
            target = TargetRef(storage_id_for_path(self.home), "cindy-target")
            observation = Observation(
                observation_id="observation:v1:cindy",
                target=target,
                platform="cindy",
                platform_session_id="session-cindy-target",
                finding_type="frontend_deleted_reference",
                reason="display only",
            )
            action = CandidateAction(
                action_id="action:v1:cindy",
                kind=ActionKind.DELETE_CONVERSATION,
                target=target,
                risk=RiskLevel.REVIEW,
                available=True,
                unavailable_reason=None,
                impact=ActionImpact(
                    affected_thread_ids=("cindy-target",),
                ),
                snapshot_fingerprint="snapshot:v1:cindy",
                observation_ids=(observation.observation_id,),
            )
            return CleanupPlan(
                observations=(observation,),
                actions=(action,),
                plan_fingerprint="plan:v1:test",
            )

        service = CleanupService(
            scanner=scanner,
            planner=planner,
            clock=lambda: datetime(2026, 8, 19, tzinfo=timezone.utc),
        )
        adapter = _Adapter("cindy", self.home)
        context = service.prepare(
            (adapter,),  # type: ignore[arg-type]
            platforms=("cindy",),
        )

        self.assertEqual(calls, {"scan": 1, "plan": 1})
        self.assertEqual(len(context.report.findings), 1)
        self.assertEqual(context.actions[0].target.record_id, "cindy-target")
        self.assertEqual(
            context.actions[0].guard_token.snapshot_fingerprint,
            "snapshot:v1:cindy",
        )

    def test_snapshot_metadata_excludes_reason_and_chat_body(self) -> None:
        finding = self.finding("cindy", "safe-target")
        finding.details["internal_test_chat_body"] = "SECRET_CHAT_BODY"
        report = ScanReport(findings=[finding])
        service = CleanupService(
            scanner=lambda _adapters, **_kwargs: report,
            planner=lambda _report: CleanupPlan(),
            clock=lambda: datetime(2026, 8, 19, tzinfo=timezone.utc),
        )

        snapshot = service.scan((), platforms=("cindy",))
        rendered = json.dumps(snapshot.to_dict(), ensure_ascii=False)

        self.assertNotIn("SECRET_CHAT_BODY", rendered)
        self.assertNotIn(finding.reason, rendered)
        self.assertIn("safe-target", rendered)
        self.assertTrue(snapshot.snapshot_id.startswith("snapshot:v1:"))

    def test_scan_failure_becomes_structured_snapshot_blocker(self) -> None:
        report = ScanReport(
            errors=[
                ScanFailure(
                    platform="cindy",
                    message="unstructured display error",
                    error_type="OSError",
                    codex_home=self.home,
                )
            ]
        )
        service = CleanupService(
            scanner=lambda _adapters, **_kwargs: report,
            planner=lambda _report: CleanupPlan(),
        )

        snapshot = service.scan(())

        self.assertFalse(snapshot.scan_complete)
        self.assertEqual(
            [str(code) for code in snapshot.blocker_codes],
            ["scan_incomplete"],
        )
        self.assertNotIn(
            "unstructured display error",
            json.dumps(snapshot.to_dict(), ensure_ascii=False),
        )

    def test_service_owns_the_injected_client_inspector(self) -> None:
        seen: list[Path] = []
        service = CleanupService(
            client_inspector=lambda path: seen.append(Path(path)) or ("Cindy.exe",),
        )

        self.assertEqual(service.inspect_clients(self.home), ("Cindy.exe",))
        self.assertEqual(seen, [self.home])


if __name__ == "__main__":
    unittest.main()
