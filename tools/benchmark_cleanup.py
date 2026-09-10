"""Measure cleanup using disposable stores and a fake native deletion server.

Run from a source checkout. No live store paths or real app-server are accepted.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent_record_janitor import codex_state
from local_agent_record_janitor.adapters import CindyAdapter, NativeIntegrityAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.client_inventory import build_client_inventory
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from tests.support import create_cindy_database
from tests.test_core_batch_fast import CoreBatchFastTests


def measure(client: str, count: int, phase: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="janitor-benchmark-") as temporary:
        root = Path(temporary)
        home = root / "codex-home"
        ids, paths = CoreBatchFastTests._native_fixture(home, count)
        database = root / "cindy.db"
        if client == "cindy":
            create_cindy_database(database, [
                {"id": "ui-" + value, "sdk_session_id": value,
                 "agent_kind": "codex", "status": "active"}
                for value in ids
            ])
        adapter = (
            NativeIntegrityAdapter(codex_home=home) if client == "native"
            else CindyAdapter(database=database, codex_home=home,
                              cindy_root=root, codex_bin_hint=root / "fake-codex")
        )
        counters: Counter[str] = Counter()
        original_connect = codex_state.connect_readonly

        def trace(sql: str) -> None:
            normalized = " ".join(sql.upper().split())
            if normalized.startswith("SELECT ") and " FROM THREADS" in normalized:
                counters["threads_queries"] += 1
                if " WHERE " not in normalized:
                    counters["unscoped_threads_queries"] += 1

        def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(trace)
            return connection

        class Server:
            def __enter__(self) -> Server:
                counters["server_starts"] += 1
                return self

            def __exit__(self, *_args: object) -> None:
                pass

            def delete_thread(self, record_id: str) -> None:
                if record_id not in paths:
                    raise AssertionError("Fake server received an unapproved record")
                counters["delete_requests"] += 1
                with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                    connection.execute("DELETE FROM threads WHERE id = ?", (record_id,))
                    connection.commit()
                for path in paths[record_id]:
                    path.unlink()

        coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *_: ()))
        with patch.object(codex_state, "connect_readonly", connect):
            started = time.perf_counter()
            if phase == "inventory":
                inventory = build_client_inventory((adapter,), client=client)
                status = "complete" if not inventory.errors else "error"
                assert len(inventory.records) == count, "Fixture records were lost"
            elif phase in {"plan_one", "plan_all"}:
                result = coordinator.plan_operation(
                    client=client, record_ids=(ids[0],) if phase == "plan_one" else ids,
                    adapters=(adapter,), plan_path=root / "plan.json",
                )
                status = result.get("goal_status")
            else:
                result = coordinator.run_operation(
                    client=client, record_ids=ids, adapters=(adapter,),
                    plan_path=root / "plan.json", clients_closed=True,
                    timeout=5, app_server_factory=lambda **_: Server(),
                    binary_resolver=lambda _: root / "fake-codex",
                )
                status = result.get("goal_status")
                assert counters["delete_requests"] == count, "Deletion was incomplete"
                assert counters["server_starts"] == 1, "Native server was not reused"
            elapsed = time.perf_counter() - started
        if phase != "run" and status not in {"ready", "complete"}:
            raise AssertionError(f"Benchmark {client}/{phase} returned {status}: {result.get('blockers')}")
        return {"client": client, "records": count, "phase": phase,
                "seconds": round(elapsed, 6), "status": status,
                "blocker_codes": sorted({str(item.get("blocker_code"))
                                         for item in result.get("blockers", [])})
                if phase == "run" else [], **dict(counters)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[10, 100])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--clients", nargs="+", choices=["native", "cindy"],
                        default=["native", "cindy"])
    parser.add_argument("--phases", nargs="+",
                        choices=["inventory", "plan_one", "plan_all", "run"],
                        default=["inventory", "plan_one", "plan_all", "run"])
    args = parser.parse_args()
    if args.repeat < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and repeat must be positive")
    for repetition in range(args.repeat):
        for client in args.clients:
            for size in args.sizes:
                for phase in args.phases:
                    print(json.dumps({"repeat": repetition + 1,
                                      **measure(client, size, phase)}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
