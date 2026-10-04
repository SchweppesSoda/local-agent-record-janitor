import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor import manual_delete, operation_coordinator
from local_agent_record_janitor.manual_delete import ManualDeletePlanError


class PlanIndexingPerformanceTests(unittest.TestCase):
    @staticmethod
    def _manual(
        action_id: str,
        thread_id: str,
        home: Path,
        *,
        descendants: tuple[str, ...] = (),
        affected: tuple[str, ...] | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            action_id=action_id,
            codex_home=home,
            thread_id=thread_id,
            descendants=descendants,
            affected_thread_ids=affected or (thread_id,),
        )

    @staticmethod
    def _candidate(action_id: str) -> SimpleNamespace:
        return SimpleNamespace(action_id=action_id)

    def test_coalesce_reuses_storage_identity_for_a_flat_selection(self) -> None:
        home = Path("codex-home")
        candidates = [
            self._candidate(f"action-{index:03d}") for index in range(100)
        ]
        manual_actions = {
            candidate.action_id: self._manual(
                candidate.action_id,
                f"thread-{index:03d}",
                home,
            )
            for index, candidate in enumerate(candidates)
        }
        canonical_calls: list[Path] = []

        def counted_canonical(path: Path) -> str:
            canonical_calls.append(path)
            return str(path)

        with (
            patch.object(
                operation_coordinator,
                "canonical_path",
                side_effect=counted_canonical,
            ),
            patch(
                "local_agent_record_janitor.manual_delete.build_manual_delete_closure",
                side_effect=lambda _action: SimpleNamespace(frontend_actions=()),
            ),
        ):
            coalesced = operation_coordinator.OperationCoordinator._coalesce_manual_candidates(
                candidates,
                manual_actions,
            )

        self.assertEqual(tuple(coalesced), tuple(candidates))
        self.assertEqual(canonical_calls, [home])

    def test_coalesce_keeps_alias_cascade_semantics_and_survivor_order(self) -> None:
        normal_home = Path(r"C:\Users\test\codex-home")
        extended_home = Path(r"\\?\C:\Users\test\codex-home")
        parent = self._manual(
            "parent-action",
            "parent",
            normal_home,
            descendants=("child",),
            affected=("parent", "child"),
        )
        child = self._manual(
            "child-action",
            "child",
            extended_home,
            affected=("child",),
        )
        candidates = [self._candidate("child-action"), self._candidate("parent-action")]
        manual_actions = {parent.action_id: parent, child.action_id: child}

        with (
            patch.object(
                operation_coordinator,
                "canonical_path",
                side_effect=lambda path: "physical-codex-home",
            ),
            patch(
                "local_agent_record_janitor.manual_delete.build_manual_delete_closure",
                side_effect=lambda _action: SimpleNamespace(frontend_actions=()),
            ),
        ):
            coalesced = operation_coordinator.OperationCoordinator._coalesce_manual_candidates(
                candidates,
                manual_actions,
            )

        self.assertEqual(tuple(coalesced), (candidates[1],))

    def test_coalesce_preserves_full_frontend_reference_closure_sharing(self) -> None:
        home = Path("codex-home")
        parent = self._manual(
            "parent-action",
            "parent",
            home,
            descendants=("child",),
            affected=("parent", "child"),
        )
        child = self._manual(
            "child-action",
            "child",
            home,
            affected=("child",),
        )
        sibling = self._manual("sibling-action", "sibling", home)
        candidates = [
            self._candidate("child-action"),
            self._candidate("parent-action"),
            self._candidate("sibling-action"),
        ]
        manual_actions = {
            item.action_id: item for item in (parent, child, sibling)
        }
        frontend_ids = {
            "parent": ("frontend-shared",),
            "child": ("frontend-shared", "frontend-child"),
            "sibling": ("frontend-sibling",),
        }

        def closure_for(action: SimpleNamespace) -> SimpleNamespace:
            return SimpleNamespace(
                frontend_actions=tuple(
                    SimpleNamespace(action_id=action_id)
                    for action_id in frontend_ids[action.thread_id]
                )
            )

        with patch(
            "local_agent_record_janitor.manual_delete.build_manual_delete_closure",
            side_effect=closure_for,
        ):
            coalesced = operation_coordinator.OperationCoordinator._coalesce_manual_candidates(
                candidates,
                manual_actions,
            )

        self.assertEqual(
            tuple(coalesced),
            (candidates[1], candidates[2]),
        )

    def test_overlap_index_reuses_storage_identity_for_disjoint_actions(self) -> None:
        home = Path("codex-home")
        actions = [
            self._manual(f"action-{index:03d}", f"thread-{index:03d}", home)
            for index in range(100)
        ]
        normalize_calls: list[Path] = []

        def counted_normalize(path: Path) -> str:
            normalize_calls.append(path)
            return str(path)

        with patch.object(
            manual_delete,
            "_normalize_home",
            side_effect=counted_normalize,
        ):
            manual_delete._reject_overlapping_actions(actions)

        self.assertEqual(normalize_calls, [home])

    def test_overlap_aliases_still_reject_with_original_first_pair(self) -> None:
        normal_home = Path(r"C:\Users\test\codex-home")
        extended_home = Path(r"\\?\C:\Users\test\codex-home")
        actions = [
            self._manual("first", "first", normal_home, affected=("shared",)),
            self._manual("second", "second", extended_home, affected=("shared",)),
            self._manual("third", "third", normal_home, affected=("shared",)),
        ]

        with patch.object(
            manual_delete,
            "_normalize_home",
            side_effect=lambda _path: "physical-codex-home",
        ):
            with self.assertRaisesRegex(
                ManualDeletePlanError,
                r"first / second: shared",
            ):
                manual_delete._reject_overlapping_actions(actions)

    def test_overlap_different_stores_remains_allowed(self) -> None:
        actions = [
            self._manual("one", "one", Path("home-one"), affected=("shared",)),
            self._manual("two", "two", Path("home-two"), affected=("shared",)),
        ]

        with patch.object(
            manual_delete,
            "_normalize_home",
            side_effect=lambda path: str(path),
        ):
            manual_delete._reject_overlapping_actions(actions)


if __name__ == "__main__":
    unittest.main()
