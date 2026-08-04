from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.adapters import NativeIntegrityAdapter
from local_agent_record_janitor.cleaner import CleanupReport, CleanupResult
from local_agent_record_janitor.cli import (
    EXIT_CONFIRMATION_REQUIRED,
    EXIT_ERROR,
    EXIT_OK,
    MANUAL_DELETE_CONFIRMATION,
    _path_identity,
    main,
)
from local_agent_record_janitor.inventory import (
    FrontendSessionRecord,
    ManagedConversation,
    SessionCatalog,
)
from local_agent_record_janitor.models import ConversationSummary, Finding

from tests.support import create_cindy_database, create_thread_index, write_rollout


class TTYStringIO(StringIO):
    def isatty(self) -> bool:
        return True


class CallbackTTYStringIO(TTYStringIO):
    def __init__(self, value: str, callback) -> None:
        super().__init__(value)
        self.callback = callback
        self.called = False

    def readline(self, *args, **kwargs):
        if not self.called:
            self.called = True
            self.callback()
        return super().readline(*args, **kwargs)


class RecordingAppServer:
    def __init__(self) -> None:
        self.deleted_thread_ids: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        self.deleted_thread_ids.append(thread_id)


class FakeAction:
    def __init__(self, home: Path, thread_id: str, frontend=(), *, action_id=None) -> None:
        self.action_id = action_id or f"record:v1:{thread_id}"
        self.codex_home = home
        self.thread_id = thread_id
        self.affected_thread_ids = (thread_id,)
        self.frontend_sessions = tuple(frontend)
        self.available = True

    def to_dict(self):
        return {
            "action_id": self.action_id,
            "codex_home": str(self.codex_home),
            "thread_id": self.thread_id,
            "affected_thread_ids": list(self.affected_thread_ids),
            "frontend_sessions": [item.to_dict() for item in self.frontend_sessions],
            "available": self.available,
        }


class FakePlan:
    def __init__(self, actions, *, selected=False, fingerprint=None) -> None:
        self.actions = tuple(actions)
        self.errors = ()
        self.selected = selected
        self.plan_fingerprint = fingerprint

    def with_selected_actions(self, selectors):
        selectors = tuple(selectors)
        if any(value.lower() == "all" for value in selectors):
            raise ValueError("Manual deletion never accepts an all selector")
        selected = tuple(
            action
            for action in self.actions
            if action.action_id in selectors
            or action.thread_id in selectors
            or any(action.thread_id.startswith(value) for value in selectors)
        )
        if not selected:
            raise ValueError("selector matched no action")
        return FakePlan(selected, selected=True, fingerprint="selected-fingerprint")

    def to_dict(self):
        return {
            "selected": self.selected,
            "plan_fingerprint": self.plan_fingerprint,
            "actions": [action.to_dict() for action in self.actions],
            "errors": [],
        }


def make_catalog() -> tuple[SessionCatalog, FrontendSessionRecord]:
    home = Path("C:/CodexHome")
    frontend = FrontendSessionRecord(
        platform="cindy",
        platform_session_id="cindy-1",
        thread_id="thread-one",
        database=Path("C:/Cindy/cindy-user.db"),
        codex_home=home,
        backend="codex",
        status="active",
        is_live=True,
    )
    unmapped = FrontendSessionRecord(
        platform="aionui",
        platform_session_id="aion-unassigned",
        thread_id=None,
        database=Path("C:/AionUi/aionui.db"),
        codex_home=home,
        backend="codex",
        status="active",
    )
    records = (
        ManagedConversation(
            codex_home=home,
            thread_id="thread-one",
            summary=ConversationSummary(
                thread_id="thread-one",
                display_name="正常会话",
                project_label="project-a",
                cwd="C:/work/a",
                indexed=True,
            ),
            frontend_sessions=(frontend,),
            indexed=True,
            artifact_present=True,
            deletable=True,
        ),
        ManagedConversation(
            codex_home=home,
            thread_id="thread-two",
            summary=ConversationSummary(
                thread_id="thread-two",
                display_name="异常会话",
                project_label="project-b",
            ),
            legacy_indexed=True,
            artifact_present=False,
            deletable=False,
            blockers=("legacy-index-only",),
        ),
    )
    return SessionCatalog(records=records, unmapped_frontend_sessions=(unmapped,)), frontend


class RecordsCliTests(unittest.TestCase):
    def test_path_identity_matches_inventory_resolved_absolute_form(self) -> None:
        self.assertEqual(_path_identity(Path(".")), _path_identity(Path.cwd()))

    def test_real_catalog_and_delete_preview_are_read_only_and_share_action_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "codex-home"
            rollout = write_rollout(
                home,
                "normal-thread",
                originator="codex_cli_rs",
            )
            create_thread_index(
                home,
                [{"id": "normal-thread", "rollout_path": str(rollout)}],
            )
            adapter = NativeIntegrityAdapter(codex_home=home)

            def snapshot() -> dict[str, bytes]:
                return {
                    str(path.relative_to(home)): path.read_bytes()
                    for path in home.rglob("*")
                    if path.is_file()
                }

            before = snapshot()
            records_output = StringIO()
            records_status = main(
                ["records", "--platform", "native", "--json"],
                adapters=(adapter,),
                stdin=StringIO(),
                stdout=records_output,
                stderr=StringIO(),
            )
            records_payload = json.loads(records_output.getvalue())
            action_id = records_payload["records"][0]["action_id"]

            delete_output = StringIO()
            delete_status = main(
                [
                    "delete",
                    "--platform",
                    "native",
                    "--action-id",
                    action_id,
                    "--json",
                ],
                adapters=(adapter,),
                stdin=StringIO(),
                stdout=delete_output,
                stderr=StringIO(),
            )
            delete_payload = json.loads(delete_output.getvalue())

            self.assertEqual(records_status, EXIT_OK)
            self.assertEqual(delete_status, EXIT_CONFIRMATION_REQUIRED)
            self.assertEqual(records_payload["count"], 1)
            self.assertEqual(
                delete_payload["selected_actions"][0]["action_id"],
                action_id,
            )
            self.assertEqual(snapshot(), before)

    def test_records_json_is_complete_despite_human_limit(self) -> None:
        catalog, _ = make_catalog()
        output = StringIO()
        with patch(
            "local_agent_record_janitor.inventory.build_session_catalog",
            return_value=catalog,
        ):
            status = main(
                ["records", "--json", "--limit", "1"],
                adapters=(),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(len(payload["records"]), 2)
        self.assertEqual(len(payload["unmapped_frontend_sessions"]), 1)
        self.assertEqual(payload["count"], 2)

    def test_records_human_output_shows_normal_unmapped_and_retained_reference(self) -> None:
        catalog, _ = make_catalog()
        output = StringIO()
        with patch(
            "local_agent_record_janitor.inventory.build_session_catalog",
            return_value=catalog,
        ):
            status = main(
                ["records", "--limit", "0"],
                adapters=(),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        text = output.getvalue()
        self.assertEqual(status, EXIT_OK)
        self.assertIn("正常会话", text)
        self.assertIn("aion-unassigned", text)
        self.assertIn("前端引用（只读保留）", text)

    def test_frontend_platform_view_only_contains_mapped_records(self) -> None:
        catalog, _ = make_catalog()
        output = StringIO()
        with patch(
            "local_agent_record_janitor.inventory.build_session_catalog",
            return_value=catalog,
        ):
            status = main(
                ["records", "--platform", "cindy", "--json"],
                adapters=(),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(
            [record["thread_id"] for record in payload["records"]],
            ["thread-one"],
        )

    def test_cindy_platform_view_includes_its_dedicated_storage_records(
        self,
    ) -> None:
        home = Path("C:/CindyGlobal/codex-home")
        catalog = SessionCatalog(
            records=(
                ManagedConversation(
                    codex_home=home,
                    thread_id="storage-only",
                    summary=ConversationSummary(thread_id="storage-only"),
                    indexed=True,
                    artifact_present=True,
                    deletable=True,
                ),
            )
        )
        adapter = SimpleNamespace(name="cindy", codex_home=home)
        output = StringIO()
        with patch(
            "local_agent_record_janitor.inventory.build_session_catalog",
            return_value=catalog,
        ):
            status = main(
                ["records", "--platform", "cindy", "--json"],
                adapters=(adapter,),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(
            [record["thread_id"] for record in payload["records"]],
            ["storage-only"],
        )


class DeleteCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, frontend = make_catalog()
        self.action = FakeAction(
            Path("C:/CodexHome"),
            "thread-one",
            (frontend,),
            action_id=self.catalog.records[0].action_id,
        )
        self.plan = FakePlan((self.action,))

    def _patches(self, *, report=None):
        return (
            patch(
                "local_agent_record_janitor.inventory.build_session_catalog",
                return_value=self.catalog,
            ),
            patch(
                "local_agent_record_janitor.manual_delete.build_manual_delete_plan",
                return_value=self.plan,
            ),
            patch(
                "local_agent_record_janitor.manual_delete.execute_manual_delete",
                return_value=report,
            ),
        )

    def test_delete_rejects_no_selection_and_all(self) -> None:
        for argv in (
            ["delete", "--yes", "--clients-closed"],
            ["delete", "--thread-id", "all", "--yes", "--clients-closed"],
        ):
            with self.subTest(argv=argv):
                output, errors = StringIO(), StringIO()
                first, second, execute = self._patches()
                with first, second, execute as execute_mock:
                    status = main(
                        argv,
                        adapters=(),
                        stdin=StringIO(),
                        stdout=output,
                        stderr=errors,
                    )
                self.assertEqual(status, EXIT_ERROR)
                execute_mock.assert_not_called()

    def test_local_cli_rediscovers_owner_namespace_added_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            appdata = base / "AppData"
            cindy_root = appdata / "CustomCindy"
            home = cindy_root / "codex-home"
            empty_native = base / "empty-native"
            empty_native.mkdir()
            thread_id = "namespace-drift-thread"
            rollout = write_rollout(home, thread_id, originator="cindy")
            create_thread_index(
                home,
                [{"id": thread_id, "rollout_path": str(rollout)}],
            )
            local = cindy_root / "cindy-local-v1.db"
            owner = cindy_root / "cindy-owner-added-after-preview.db"
            deleted_row = {
                "id": "local-deleted",
                "sdk_session_id": thread_id,
                "status": "deleted",
                "source": "desktop",
                "created_at": 1,
                "updated_at": 2,
                "parent_session_id": None,
                "agent_kind": "codex",
            }
            create_cindy_database(local, [deleted_row])

            def add_live_owner_namespace() -> None:
                create_cindy_database(
                    owner,
                    [{**deleted_row, "id": "owner-live", "status": "active"}],
                )

            input_stream = CallbackTTYStringIO(
                MANUAL_DELETE_CONFIRMATION + "\n",
                add_live_owner_namespace,
            )
            output = TTYStringIO()
            errors = StringIO()
            server = RecordingAppServer()

            status = main(
                [
                    "delete", "--platform", "cindy",
                    "--thread-id", thread_id,
                    "--appdata", str(appdata),
                    "--codex-home", str(empty_native),
                    "--cindy-root", str(cindy_root),
                    "--cindy-db", str(local),
                ],
                stdin=input_stream,
                stdout=output,
                stderr=errors,
                app_server_factory=lambda **_kwargs: server,
                binary_resolver=lambda _hint: Path("codex"),
            )

        self.assertEqual(status, EXIT_ERROR)
        self.assertTrue(input_stream.called)
        self.assertEqual(server.deleted_thread_ids, [])
        self.assertIn("unavailable", errors.getvalue() + output.getvalue())

    def test_non_tty_preview_is_one_json_document_and_exit_two(self) -> None:
        output = StringIO()
        first, second, execute = self._patches()
        with first, second, execute as execute_mock:
            status = main(
                ["delete", "--thread-id", "thread-one", "--json"],
                adapters=(),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_CONFIRMATION_REQUIRED)
        self.assertTrue(payload["confirmation_required"])
        self.assertEqual(payload["plan_fingerprint"], "selected-fingerprint")
        execute_mock.assert_not_called()

    def test_records_action_id_can_be_copied_to_delete(self) -> None:
        records_output = StringIO()
        first, second, execute = self._patches()
        with first, second, execute as execute_mock:
            records_status = main(
                ["records", "--json"],
                adapters=(),
                stdin=StringIO(),
                stdout=records_output,
                stderr=StringIO(),
            )
        action_id = json.loads(records_output.getvalue())["records"][0]["action_id"]
        delete_output = StringIO()
        first, second, execute = self._patches()
        with first, second, execute as execute_mock:
            delete_status = main(
                ["delete", "--action-id", action_id, "--json"],
                adapters=(),
                stdin=StringIO(),
                stdout=delete_output,
                stderr=StringIO(),
            )
        payload = json.loads(delete_output.getvalue())
        self.assertEqual(records_status, EXIT_OK)
        self.assertEqual(delete_status, EXIT_CONFIRMATION_REQUIRED)
        self.assertEqual(payload["selected_actions"][0]["action_id"], action_id)
        execute_mock.assert_not_called()

    def test_delete_native_view_keeps_all_supplied_frontend_guards(self) -> None:
        native = SimpleNamespace(name="native", codex_home=Path("C:/CodexHome"))
        cindy = SimpleNamespace(name="cindy", codex_home=Path("C:/CodexHome"))
        output = StringIO()
        with (
            patch(
                "local_agent_record_janitor.inventory.build_session_catalog",
                return_value=self.catalog,
            ) as catalog_builder,
            patch(
                "local_agent_record_janitor.manual_delete.build_manual_delete_plan",
                return_value=self.plan,
            ),
            patch(
                "local_agent_record_janitor.manual_delete.execute_manual_delete"
            ) as execute,
        ):
            status = main(
                [
                    "delete",
                    "--platform",
                    "native",
                    "--thread-id",
                    "thread-one",
                    "--json",
                ],
                adapters=(native, cindy),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        self.assertEqual(status, EXIT_CONFIRMATION_REQUIRED)
        guarded = tuple(catalog_builder.call_args.args[0])
        self.assertEqual(guarded, (native, cindy))
        execute.assert_not_called()

    def test_non_tty_execution_requires_clients_yes_and_fingerprint(self) -> None:
        cases = (
            (["delete", "--thread-id", "thread-one", "--yes"], "clients"),
            (
                [
                    "delete",
                    "--thread-id",
                    "thread-one",
                    "--yes",
                    "--clients-closed",
                ],
                "fingerprint",
            ),
        )
        for argv, needle in cases:
            with self.subTest(argv=argv):
                output = StringIO()
                first, second, execute = self._patches()
                with first, second, execute as execute_mock:
                    status = main(
                        argv + ["--json"],
                        adapters=(),
                        stdin=StringIO(),
                        stdout=output,
                        stderr=StringIO(),
                    )
                payload = json.loads(output.getvalue())
                self.assertEqual(status, EXIT_ERROR)
                self.assertIn(needle, json.dumps(payload).lower())
                execute_mock.assert_not_called()

    def test_fingerprint_error_is_exactly_one_json_document(self) -> None:
        output = StringIO()
        first, second, execute = self._patches()
        with first, second, execute as execute_mock:
            status = main(
                [
                    "delete",
                    "--thread-id",
                    "thread-one",
                    "--json",
                    "--yes",
                    "--clients-closed",
                    "--plan-fingerprint",
                    "wrong",
                ],
                adapters=(),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_ERROR)
        self.assertEqual(payload["error"]["kind"], "fingerprint_mismatch")
        execute_mock.assert_not_called()

    def test_tty_cancel_and_dedicated_confirmation(self) -> None:
        for confirmation, expected_calls in (("取消\n", 0), (MANUAL_DELETE_CONFIRMATION + "\n", 1)):
            with self.subTest(confirmation=confirmation):
                output = TTYStringIO()
                finding = Finding(
                    platform="manual",
                    platform_session_id="thread-one",
                    thread_id="thread-one",
                    reason="manual",
                    platform_db=Path("C:/CodexHome/state_5.sqlite"),
                    codex_home=Path("C:/CodexHome"),
                )
                report = CleanupReport(
                    planned=[finding],
                    results=[CleanupResult(finding=finding, status="deleted")],
                )
                first, second, execute = self._patches(report=report)
                with first, second, execute as execute_mock:
                    status = main(
                        ["delete", "--thread-id", "thread-one"],
                        adapters=(),
                        stdin=TTYStringIO(confirmation),
                        stdout=output,
                        stderr=StringIO(),
                    )
                self.assertEqual(execute_mock.call_count, expected_calls)
                self.assertEqual(status, EXIT_OK)
                self.assertIn(MANUAL_DELETE_CONFIRMATION, output.getvalue())

    def test_success_is_one_json_and_explicitly_retains_frontend_rows(self) -> None:
        finding = Finding(
            platform="manual",
            platform_session_id="thread-one",
            thread_id="thread-one",
            reason="manual",
            platform_db=Path("C:/CodexHome/state_5.sqlite"),
            codex_home=Path("C:/CodexHome"),
        )
        report = CleanupReport(
            planned=[finding],
            results=[CleanupResult(finding=finding, status="deleted")],
        )
        output = StringIO()
        first, second, execute = self._patches(report=report)
        with first, second, execute as execute_mock:
            status = main(
                [
                    "delete",
                    "--thread-id",
                    "thread-one",
                    "--json",
                    "--yes",
                    "--clients-closed",
                    "--plan-fingerprint",
                    "selected-fingerprint",
                ],
                adapters=(),
                stdin=StringIO(),
                stdout=output,
                stderr=StringIO(),
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, EXIT_OK)
        self.assertFalse(payload["third_party_references_deleted"])
        self.assertEqual(len(payload["retained_frontend_sessions"]), 1)
        execute_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
