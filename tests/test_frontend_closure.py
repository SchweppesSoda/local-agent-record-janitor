import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.adapters import AionUIAdapter, CindyAdapter
from local_agent_record_janitor.blocker_codes import LIVE_FRONTEND_REFERENCE
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.frontend_reference_cleanup import (
    FrontendReferenceGuardError,
    guard_frontend_reference_closure,
)
from local_agent_record_janitor.inventory import (
    FrontendSessionRecord,
    ManagedConversation,
    SessionCatalog,
)
from local_agent_record_janitor.manual_delete import (
    build_manual_delete_closure,
    build_manual_delete_plan,
    frontend_actions_after_native_success,
)
from local_agent_record_janitor.models import ConversationSummary
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from tests.support import create_aionui_database, create_cindy_database, write_rollout
from local_agent_record_janitor.codex_state import find_thread_rollouts


class FrontendClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()

    def _record(
        self,
        thread_id: str,
        frontends: tuple[FrontendSessionRecord, ...],
        *,
        descendants: tuple[str, ...] = (),
    ) -> ManagedConversation:
        write_rollout(self.home, thread_id, originator="test")
        rollouts = tuple(find_thread_rollouts(self.home, thread_id))
        return ManagedConversation(
            codex_home=self.home,
            thread_id=thread_id,
            summary=ConversationSummary(thread_id=thread_id, indexed=True),
            rollouts=rollouts,
            frontend_sessions=frontends,
            descendant_thread_ids=descendants,
            thread_index={"id": thread_id},
            indexed=True,
            artifact_present=True,
            # The only structured blocker is the live frontend closure.
            deletable=False,
            blockers=("presentation text may change",),
            blocker_codes=(LIVE_FRONTEND_REFERENCE,),
            codex_bin_hints=(),
        )

    @staticmethod
    def _evidence(record: FrontendSessionRecord) -> dict[str, object]:
        return dict(record.details["frontend_reference"])

    def _coordinator_fixture(self) -> tuple[Path, Path, object, list[str]]:
        """Build a real native catalog with one exact AionUI live reference."""

        database = self.root / "coordinator-aionui.sqlite"
        create_aionui_database(
            database,
            conversations=["ui"],
            sessions=[
                {"conversation_id": "ui", "session_id": "native", "agent_id": "agent"}
            ],
            metadata=[("agent", "codex")],
        )
        rollout = write_rollout(self.home, "native", originator="test")
        from tests.support import create_thread_index

        create_thread_index(
            self.home,
            [{"id": "native", "rollout_path": str(rollout)}],
        )
        aionui = AionUIAdapter(database=database, codex_home=self.home)
        calls: list[str] = []

        class NativeWithAionUI:
            name = "native"

            def __init__(self) -> None:
                self.codex_home = self_home

            def list_sessions(self) -> list[FrontendSessionRecord]:
                return aionui.list_sessions()

        self_home = self.home
        return database, rollout, NativeWithAionUI(), calls

    def _deleting_server_factory(
        self,
        database: Path,
        rollout: Path,
        calls: list[str],
    ):
        class DeletingServer:
            def __enter__(self) -> "DeletingServer":
                return self

            def __exit__(self, *_exc_info: object) -> None:
                return None

            def delete_thread(self, thread_id: str) -> None:
                calls.append(thread_id)
                with closing(sqlite3.connect(self_home / "state_5.sqlite")) as connection:
                    connection.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
                    connection.commit()
                rollout.unlink(missing_ok=True)

        self_home = self.home
        return lambda **_kwargs: DeletingServer()

    def _add_aionui_reference(self, database: Path, conversation_id: str) -> None:
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "INSERT INTO conversations (id) VALUES (?)", (conversation_id,)
            )
            connection.execute(
                "INSERT INTO acp_session (conversation_id, session_id, agent_id) "
                "VALUES (?, ?, ?)",
                (conversation_id, "native", "agent"),
            )
            connection.commit()

    def test_coordinator_run_deletes_native_once_then_aionui_reference(self) -> None:
        database, rollout, adapter, calls = self._coordinator_fixture()
        coordinator = OperationCoordinator(
            CleanupService(client_inspector=lambda _home: ())
        )
        result = coordinator.run_operation(
            client="native",
            record_ids=("native",),
            adapters=(adapter,),
            plan_path=self.root / "coordinator-run-plan.json",
            clients_closed=True,
            timeout=5,
            app_server_factory=self._deleting_server_factory(database, rollout, calls),
            binary_resolver=lambda _hint: Path("codex"),
        )
        self.assertEqual(result.get("goal_status"), "complete")
        self.assertEqual(calls, ["native"])
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM acp_session WHERE session_id = ?",
                    ("native",),
                ).fetchone()[0],
                0,
            )

    def test_coordinator_plan_guard_blocks_new_reference_before_native_delete(self) -> None:
        database, rollout, adapter, calls = self._coordinator_fixture()
        coordinator = OperationCoordinator(
            CleanupService(client_inspector=lambda _home: ())
        )
        plan_path = self.root / "coordinator-guard-plan.json"
        plan = coordinator.plan_operation(
            client="native",
            record_ids=("native",),
            adapters=(adapter,),
            plan_path=plan_path,
        )
        self.assertEqual(plan.get("goal_status"), "ready")
        self._add_aionui_reference(database, "new-ui")
        result = coordinator.apply_operation(
            operation_id=plan["operation_id"],
            plan_path=plan_path,
            plan_sha256=plan["plan_sha256"],
            clients_closed=True,
            timeout=5,
            app_server_factory=self._deleting_server_factory(database, rollout, calls),
            binary_resolver=lambda _hint: Path("codex"),
        )
        self.assertEqual(result.get("goal_status"), "blocked")
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM acp_session WHERE session_id = ?",
                    ("native",),
                ).fetchone()[0],
                2,
            )

    def test_coordinator_native_unknown_does_not_start_frontend_batch(self) -> None:
        database, rollout, adapter, calls = self._coordinator_fixture()
        coordinator = OperationCoordinator(
            CleanupService(client_inspector=lambda _home: ())
        )
        unknown = SimpleNamespace(
            modified=False,
            session_cleanup=SimpleNamespace(
                results=(SimpleNamespace(status="unknown", finding=SimpleNamespace(thread_id="native")),)
            ),
        )
        with patch.object(coordinator, "_execute_manual_batch", return_value=unknown):
            result = coordinator.run_operation(
                client="native",
                record_ids=("native",),
                adapters=(adapter,),
                plan_path=self.root / "coordinator-unknown-plan.json",
                clients_closed=True,
                timeout=5,
                app_server_factory=self._deleting_server_factory(database, rollout, calls),
                binary_resolver=lambda _hint: Path("codex"),
            )
        self.assertEqual(result.get("goal_status"), "unknown")
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM acp_session WHERE session_id = ?",
                    ("native",),
                ).fetchone()[0],
                1,
            )

    def test_aionui_same_native_parent_child_pair_and_new_ref_guard(self) -> None:
        database = self.root / "aionui.sqlite"
        create_aionui_database(
            database,
            conversations=["root-ui-a", "root-ui-b", "child-ui"],
            sessions=[
                {"conversation_id": "root-ui-a", "session_id": "root"},
                {"conversation_id": "root-ui-b", "session_id": "root"},
                {"conversation_id": "child-ui", "session_id": "child"},
            ],
            metadata=[("agent", "codex")],
        )
        adapter = AionUIAdapter(database=database, codex_home=self.home)
        rows = adapter.list_sessions()
        root_rows = tuple(row for row in rows if row.thread_id == "root")
        child_rows = tuple(row for row in rows if row.thread_id == "child")
        catalog = SessionCatalog(
            records=(
                self._record("root", root_rows, descendants=("child",)),
                self._record("child", child_rows),
            )
        )
        plan = build_manual_delete_plan(catalog)
        root_action = next(item for item in plan.actions if item.thread_id == "root")
        self.assertTrue(root_action.available)
        self.assertTrue(root_action.frontend_closure_eligible)
        closure = build_manual_delete_closure(root_action)
        self.assertTrue(closure.eligible)
        self.assertEqual(len(closure.frontend_actions), 1)
        frontend = closure.frontend_actions[0]
        self.assertEqual(frontend.impact.frontend_reference_count, 3)
        self.assertEqual(
            set(frontend.impact.affected_thread_ids),
            {"root", "child"},
        )
        evidence = tuple(frontend.impact.frontend_reference_evidence)
        by_native = {
            "root": tuple(item for item in evidence if item["expected"]["session_id"] == "root"),
            "child": tuple(item for item in evidence if item["expected"]["session_id"] == "child"),
        }
        guarded = guard_frontend_reference_closure(by_native)
        self.assertEqual(guarded.collection_query_count, 1)
        self.assertEqual(guarded.checked_reference_count, 3)

        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "INSERT INTO conversations (id) VALUES (?)",
                ("new-ui",),
            )
            connection.execute(
                "INSERT INTO acp_session (conversation_id, session_id) VALUES (?, ?)",
                ("new-ui", "root"),
            )
            connection.commit()
        with self.assertRaises(FrontendReferenceGuardError):
            guard_frontend_reference_closure(by_native)

    def test_cindy_same_native_current_references_are_one_paired_action(self) -> None:
        database = self.root / "cindy.sqlite"
        create_cindy_database(
            database,
            [
                {"id": "cindy-a", "sdk_session_id": "shared", "status": "active", "agent_kind": "codex"},
                {"id": "cindy-b", "sdk_session_id": "shared", "status": "active", "agent_kind": "codex"},
            ],
        )
        adapter = CindyAdapter(
            database=database,
            codex_home=self.home,
            cindy_root=self.root,
        )
        rows = tuple(adapter.list_sessions())
        action = next(
            item
            for item in build_manual_delete_plan(
                SessionCatalog(records=(self._record("shared", rows),))
            ).actions
        )
        self.assertTrue(action.frontend_closure_eligible)
        closure = build_manual_delete_closure(action)
        self.assertEqual(len(closure.frontend_actions), 1)
        frontend = closure.frontend_actions[0]
        self.assertEqual(frontend.impact.frontend_reference_count, 2)
        evidence = frontend.impact.frontend_reference_evidence
        guarded = guard_frontend_reference_closure(
            {"shared": tuple(evidence)}
        )
        self.assertEqual(guarded.collection_query_count, 1)

        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "INSERT INTO sessions (id, sdk_session_id, status, agent_kind) VALUES (?, ?, ?, ?)",
                ("cindy-new", "shared", "active", "codex"),
            )
            connection.commit()
        with self.assertRaises(FrontendReferenceGuardError):
            guard_frontend_reference_closure({"shared": tuple(evidence)})

    def test_unknown_native_result_never_releases_frontend_actions(self) -> None:
        database = self.root / "aionui-unknown.sqlite"
        create_aionui_database(
            database,
            conversations=["ui"],
            sessions=[{"conversation_id": "ui", "session_id": "native"}],
            metadata=[("agent", "codex")],
        )
        rows = tuple(
            AionUIAdapter(database=database, codex_home=self.home).list_sessions()
        )
        action = next(
            item
            for item in build_manual_delete_plan(
                SessionCatalog(records=(self._record("native", rows),))
            ).actions
        )
        closure = build_manual_delete_closure(action)
        self.assertTrue(closure.frontend_actions)
        self.assertEqual(
            frontend_actions_after_native_success(
                closure,
                type("Result", (), {"status": "unknown"})(),
            ),
            (),
        )
        self.assertEqual(
            frontend_actions_after_native_success(
                closure,
                type("Result", (), {"status": "partial"})(),
            ),
            (),
        )
    def test_collection_query_count_is_cardinality_independent(self) -> None:
        for size in (1, 10, 100):
            database = self.root / f"aionui-{size}.sqlite"
            create_aionui_database(
                database,
                conversations=[f"ui-{index}" for index in range(size)],
                sessions=[
                    {"conversation_id": f"ui-{index}", "session_id": f"native-{index}"}
                    for index in range(size)
                ],
                metadata=[("agent", "codex")],
            )
            adapter = AionUIAdapter(database=database, codex_home=self.home)
            evidence = [self._evidence(row) for row in adapter.list_sessions()]
            by_native = {
                str(item["expected"]["session_id"]): (item,)
                for item in evidence
            }
            result = guard_frontend_reference_closure(by_native)
            self.assertEqual(result.collection_query_count, 1)
            self.assertEqual(result.checked_reference_count, size)


if __name__ == "__main__":
    unittest.main()
