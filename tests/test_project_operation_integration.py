import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from local_agent_record_janitor.adapters import AionUIAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator


class ProjectOperationIntegrationTests(unittest.TestCase):
    def test_aion_orphan_project_rows_run_as_one_database_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "aionui.db"
            codex_home = root / "codex-home"
            codex_home.mkdir()
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE conversations (
                        id TEXT PRIMARY KEY,
                        project_id TEXT,
                        title TEXT,
                        working_dir TEXT
                    );
                    CREATE TABLE acp_session (
                        conversation_id TEXT NOT NULL,
                        session_id TEXT
                    );
                    """
                )
                connection.executemany(
                    "INSERT INTO conversations (id, project_id, title, working_dir) VALUES (?, ?, ?, ?)",
                    (
                        (f"conversation-{index}", f"project-{index}", f"Project {index}", str(root))
                        for index in range(10)
                    ),
                )
                connection.commit()

            adapter = AionUIAdapter(
                database=database,
                codex_home=codex_home,
                codex_bin_hint=root / "codex",
            )
            plan_path = root / "operation-plan.json"
            result = OperationCoordinator(CleanupService()).run_operation(
                client="aionui",
                all_projects=True,
                adapters=(adapter,),
                plan_path=plan_path,
                clients_closed=True,
            )

            self.assertEqual(result["goal_status"], "complete")
            self.assertEqual(result["counts"]["batch_count"], 1)
            self.assertEqual(len(result["batches"][0]["action_ids"]), 10)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            project_actions = [
                action
                for action in plan["actions"]
                if action["kind"] == "delete_project_item"
            ]
            self.assertEqual(len(project_actions), 10)
            self.assertEqual(
                {
                    action["target"]["thread_id"] for action in project_actions
                },
                {f"project-{index}" for index in range(10)},
            )
            self.assertEqual(
                {
                    evidence["conversation_id"]
                    for action in project_actions
                    for evidence in action["impact"]["frontend_project_evidence"]
                },
                {f"conversation-{index}" for index in range(10)},
            )
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
