from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import local_agent_record_janitor.codex_state as codex_state
from local_agent_record_janitor.cleaner import (
    ExpectedDeletionScope,
    clean_findings,
    finding_key,
)
from local_agent_record_janitor.models import Finding, RolloutRecord

from tests.support import create_thread_index, write_rollout


class _DeletingServer:
    def __init__(
        self,
        home: Path,
        paths: dict[str, Path],
        callback=None,
    ) -> None:
        self.home = home
        self.paths = paths
        self.callback = callback
        self.deleted: list[str] = []

    def __enter__(self) -> _DeletingServer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def delete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            connection.commit()
        self.paths[thread_id].unlink()
        if self.callback is not None:
            self.callback(thread_id)


class DeletionReadPerformanceTests(unittest.TestCase):
    def test_batch_delete_reuses_each_scope_snapshot_without_stale_verification(
        self,
    ) -> None:
        """One fresh read serves each check phase and every post-write verify."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            home.mkdir()
            thread_ids = tuple(f"native-{index:03d}" for index in range(4))
            paths = {
                thread_id: write_rollout(
                    home,
                    thread_id,
                    originator="codex_cli_rs",
                )
                for thread_id in thread_ids
            }
            create_thread_index(
                home,
                [
                    {"id": thread_id, "rollout_path": str(paths[thread_id])}
                    for thread_id in thread_ids
                ],
            )
            findings = [
                Finding(
                    platform="native",
                    platform_session_id=thread_id,
                    thread_id=thread_id,
                    reason="test cleanup",
                    platform_db=home / "state_5.sqlite",
                    codex_home=home,
                    rollout=RolloutRecord(
                        thread_id=thread_id,
                        path=paths[thread_id],
                        originator="codex_cli_rs",
                        source="app-server",
                        cwd=str(root),
                        timestamp="2026-07-31T00:00:00Z",
                        archived=False,
                    ),
                    codex_indexed=True,
                    details={
                        "cleanable": True,
                        "thread_delete_supported": True,
                    },
                )
                for thread_id in thread_ids
            ]
            expected_scopes = {
                finding_key(finding): ExpectedDeletionScope(
                    indexed_thread_ids=(finding.thread_id,),
                    rollout_paths=(str(finding.rollout.path),),
                )
                for finding in findings
                if finding.rollout is not None
            }
            server = _DeletingServer(home, paths)
            counters: Counter[str] = Counter()
            original_connect = codex_state.connect_readonly
            original_lineage = codex_state.read_native_lineage

            def trace(sql: str) -> None:
                normalized = " ".join(sql.upper().split())
                if (
                    normalized.startswith("SELECT ")
                    and " FROM THREADS" in normalized
                    and " WHERE " not in normalized
                ):
                    counters["unscoped_threads_queries"] += 1

            def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
                connection = original_connect(*args, **kwargs)
                connection.set_trace_callback(trace)
                return connection

            def lineage(*args: object, **kwargs: object):
                counters["lineage_reads"] += 1
                return original_lineage(*args, **kwargs)

            with patch.object(
                codex_state,
                "connect_readonly",
                side_effect=connect,
            ), patch.object(
                codex_state,
                "read_native_lineage",
                side_effect=lineage,
            ):
                report = clean_findings(
                    findings,
                    app_server_factory=lambda **_kwargs: server,
                    binary_resolver=lambda _hint: Path("codex"),
                    verification_attempts=1,
                    verification_interval=0,
                    expected_scopes=expected_scopes,
                )

            self.assertEqual(server.deleted, list(thread_ids))
            self.assertEqual([result.status for result in report.results], [
                "deleted",
            ] * len(thread_ids))
            # There is one pre-server read, one startup read, one fresh
            # immediate read, and one fresh post-result verification per root.
            # The immediate snapshot also supplies expected edge markers, so
            # it does not trigger a second full metadata read for that root.
            expected_reads = 2 * len(thread_ids) + 2
            self.assertEqual(counters["lineage_reads"], expected_reads)
            self.assertEqual(
                counters["unscoped_threads_queries"],
                expected_reads,
            )

    def test_large_targeted_edge_read_preserves_cross_chunk_and_duplicate_rows(
        self,
    ) -> None:
        """The large-scope path stays read-only and returns every matching row."""

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            home.mkdir()
            database = home / "state_5.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE thread_spawn_edges (
                        parent_thread_id TEXT NOT NULL,
                        child_thread_id TEXT NOT NULL,
                        status TEXT
                    );
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO thread_spawn_edges
                        (parent_thread_id, child_thread_id, status)
                    VALUES (?, ?, ?)
                    """,
                    (
                        ("target-000", "outside-child", "closed"),
                        ("outside-parent", "target-401", "closed"),
                        ("target-000", "outside-child", "closed"),
                        ("outside-parent", "outside-child", "closed"),
                    ),
                )
                connection.commit()

            target_ids = [
                f"target-{index:03d}" for index in range(402)
            ]
            records = codex_state.read_spawn_edge_records(home, target_ids)

            self.assertEqual(
                Counter(
                    (
                        record.parent_thread_id,
                        record.child_thread_id,
                        record.status,
                    )
                    for record in records
                ),
                Counter(
                    {
                        ("target-000", "outside-child", "closed"): 2,
                        ("outside-parent", "target-401", "closed"): 1,
                    }
                ),
            )

    def test_immediate_scope_read_is_refreshed_after_a_prior_delete(self) -> None:
        """A new child appearing after root one cannot hide behind a cache."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            home.mkdir()
            thread_ids = ("root-a", "root-b")
            paths = {
                thread_id: write_rollout(
                    home,
                    thread_id,
                    originator="codex_cli_rs",
                )
                for thread_id in thread_ids
            }
            create_thread_index(
                home,
                [
                    {"id": thread_id, "rollout_path": str(paths[thread_id])}
                    for thread_id in thread_ids
                ],
            )
            findings = [
                Finding(
                    platform="native",
                    platform_session_id=thread_id,
                    thread_id=thread_id,
                    reason="test cleanup",
                    platform_db=home / "state_5.sqlite",
                    codex_home=home,
                    rollout=RolloutRecord(
                        thread_id=thread_id,
                        path=paths[thread_id],
                        originator="codex_cli_rs",
                        source="app-server",
                        cwd=str(root),
                        timestamp="2026-07-31T00:00:00Z",
                        archived=False,
                    ),
                    codex_indexed=True,
                    details={
                        "cleanable": True,
                        "thread_delete_supported": True,
                    },
                )
                for thread_id in thread_ids
            ]
            expected_scopes = {
                finding_key(finding): ExpectedDeletionScope(
                    indexed_thread_ids=(finding.thread_id,),
                    rollout_paths=(str(finding.rollout.path),),
                )
                for finding in findings
                if finding.rollout is not None
            }

            def add_new_descendant(thread_id: str) -> None:
                if thread_id != "root-a":
                    return
                with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                    connection.execute(
                        """
                        INSERT INTO threads
                            (id, rollout_path, source, thread_source)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            "new-child",
                            None,
                            json.dumps(
                                {
                                    "subagent": {
                                        "thread_spawn": {
                                            "parent_thread_id": "root-b",
                                        }
                                    }
                                }
                            ),
                            "subagent",
                        ),
                    )
                    connection.commit()

            server = _DeletingServer(home, paths, add_new_descendant)
            report = clean_findings(
                findings,
                app_server_factory=lambda **_kwargs: server,
                binary_resolver=lambda _hint: Path("codex"),
                verification_attempts=1,
                verification_interval=0,
                expected_scopes=expected_scopes,
            )

            self.assertEqual(server.deleted, ["root-a"])
            self.assertEqual(
                [result.status for result in report.results],
                ["deleted", "unknown"],
            )
            self.assertIn(
                "current descendant closure changed",
                report.results[1].error or "",
            )


if __name__ == "__main__":
    unittest.main()
