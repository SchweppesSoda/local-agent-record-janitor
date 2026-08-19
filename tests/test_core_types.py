from __future__ import annotations

import unittest
from pathlib import Path

from local_agent_record_janitor.core_types import (
    Action,
    AuthorizedAction,
    BlockerCode,
    GuardToken,
    MutationKind,
    RecordKind,
    RecordRef,
    Result,
    ResultStatus,
)


class CoreTypesTests(unittest.TestCase):
    def make_action(self, *, available: bool = True) -> Action:
        return Action(
            action_id="action:v1:target",
            kind=MutationKind.DELETE_CONVERSATION,
            target=RecordRef(
                storage_id="storage:v1:home",
                kind=RecordKind.CONVERSATION,
                record_id="thread-1",
                locator=(("path", str(Path("sessions") / "thread-1.jsonl")),),
            ),
            snapshot_fingerprint="snapshot:v1:target",
            evidence_ids=("evidence-1", "evidence-1"),
            available=available,
            blocker_codes=(
                ()
                if available
                else (BlockerCode("live_frontend_reference"),)
            ),
        )

    def test_action_identity_and_guard_are_typed_and_stable(self) -> None:
        action = self.make_action()

        self.assertEqual(action.evidence_ids, ("evidence-1",))
        self.assertEqual(action.guard_token.storage_id, "storage:v1:home")
        self.assertEqual(len(action.guard_token.digest), 64)
        self.assertEqual(
            action.to_dict()["target"]["locator"],
            {"path": str(Path("sessions") / "thread-1.jsonl")},
        )

    def test_authorization_rejects_wrong_guard_or_unavailable_action(self) -> None:
        action = self.make_action()
        with self.assertRaisesRegex(ValueError, "guard token"):
            AuthorizedAction(
                action=action,
                plan_sha256="a" * 64,
                guard=GuardToken(
                    action_id=action.action_id,
                    storage_id=action.target.storage_id,
                    record_id="different",
                    snapshot_fingerprint=action.snapshot_fingerprint,
                ),
            )
        blocked = self.make_action(available=False)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            AuthorizedAction(
                action=blocked,
                plan_sha256="b" * 64,
                guard=blocked.guard_token,
            )

    def test_free_text_cannot_be_used_as_a_blocker_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid blocker code"):
            BlockerCode("Client is still running")
        with self.assertRaisesRegex(ValueError, "cannot carry blocker"):
            Action(
                action_id="action:v1:bad",
                kind=MutationKind.DELETE_CONVERSATION,
                target=RecordRef(
                    storage_id="storage:v1:home",
                    kind=RecordKind.CONVERSATION,
                    record_id="thread-1",
                ),
                snapshot_fingerprint="snapshot:v1:bad",
                available=True,
                blocker_codes=(BlockerCode("identity_conflict"),),
            )

    def test_result_counts_are_structured_and_nonnegative(self) -> None:
        result = Result(
            action_id="action:v1:target",
            status=ResultStatus.COMPLETE,
            modified=True,
            counts=(("deleted_file_count", 2),),
        )
        self.assertEqual(result.to_dict()["counts"], {"deleted_file_count": 2})
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            Result(
                action_id="action:v1:target",
                status=ResultStatus.UNKNOWN,
                modified=False,
                counts=(("remaining", -1),),
            )


if __name__ == "__main__":
    unittest.main()
