import unittest
from types import SimpleNamespace

from local_agent_record_janitor.operation_coordinator import OperationCoordinator


class _CountingTarget:
    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id
        self.thread_id_reads = 0

    @property
    def thread_id(self) -> str:
        self.thread_id_reads += 1
        return self._thread_id


class _CountingAction:
    kind = "delete_conversation"
    available = True
    unavailable_reason = None

    def __init__(self, action_id: str, thread_id: str) -> None:
        self.action_id = action_id
        self._target = _CountingTarget(thread_id)
        self.target_reads = 0
        self._impact = SimpleNamespace(external_action_payload={})
        self.impact_reads = 0

    @property
    def target(self) -> _CountingTarget:
        self.target_reads += 1
        return self._target

    @property
    def impact(self) -> SimpleNamespace:
        self.impact_reads += 1
        return self._impact


class RecordSelectionPerformanceTests(unittest.TestCase):
    @staticmethod
    def _context(actions: tuple[object, ...]) -> SimpleNamespace:
        return SimpleNamespace(
            plan=SimpleNamespace(actions=actions, errors=()),
        )

    def test_thousand_record_selectors_extract_each_action_identity_once(self) -> None:
        actions = tuple(
            _CountingAction(
                f"action-{index:04d}",
                f"thread-{index:04d}",
            )
            for index in range(1000)
        )
        selectors = tuple(f"thread-{index:04d}" for index in range(1000))

        selected, blockers = OperationCoordinator(SimpleNamespace())._select_candidates(
            self._context(actions),
            {
                "client": "native",
                "projects": (),
                "all_projects": False,
                "record_ids": selectors,
                "engines": (),
            },
        )

        self.assertEqual(selected, actions)
        self.assertEqual(blockers, [])
        self.assertEqual(
            [action.target_reads for action in actions],
            [1] * len(actions),
        )
        self.assertEqual(
            [action._target.thread_id_reads for action in actions],
            [1] * len(actions),
        )
        self.assertEqual(
            [action.impact_reads for action in actions],
            [1] * len(actions),
        )

    def test_record_ids_preserve_aliases_project_exactness_and_action_order(self) -> None:
        project = SimpleNamespace(
            action_id="project-action",
            kind="delete_native_project",
            available=True,
            unavailable_reason=None,
            target=SimpleNamespace(storage_id="store", thread_id="project-123"),
            impact=SimpleNamespace(external_action_payload={}),
        )
        native = SimpleNamespace(
            action_id="native-action",
            kind="delete_conversation",
            available=True,
            unavailable_reason=None,
            target=SimpleNamespace(storage_id="store", thread_id="native-123"),
            impact=SimpleNamespace(
                external_action_payload={"frontend_session_id": "ui-123"}
            ),
        )
        frontend = SimpleNamespace(
            action_id="frontend-action",
            kind="delete_frontend_session",
            available=True,
            unavailable_reason=None,
            target=SimpleNamespace(storage_id="store", thread_id="native-123"),
            impact=SimpleNamespace(
                external_action_payload={"frontend_session_id": "ui-123"}
            ),
        )
        actions = (project, native, frontend)
        selected, blockers = OperationCoordinator(SimpleNamespace())._select_candidates(
            self._context(actions),
            {
                "client": "cindy",
                "projects": (),
                "all_projects": False,
                "record_ids": (
                    "project-",
                    "native-",
                    "ui-",
                    "cindy:ui-",
                    "missing-id",
                ),
                "engines": (),
            },
        )

        self.assertEqual(selected, (native, frontend))
        self.assertEqual(
            [item["message"] for item in blockers],
            [
                "record selector 'missing-id' did not match this client store",
                "record selector 'project-' did not match this client store",
            ],
        )

        exact_project, project_blockers = OperationCoordinator(
            SimpleNamespace()
        )._select_candidates(
            self._context(actions),
            {
                "client": "cindy",
                "projects": (),
                "all_projects": False,
                "record_ids": ("project-123",),
                "engines": (),
            },
        )
        self.assertEqual(exact_project, (project,))
        self.assertEqual(project_blockers, [])


if __name__ == "__main__":
    unittest.main()
