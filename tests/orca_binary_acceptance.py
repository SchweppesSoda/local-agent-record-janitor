"""Opt-in fixed-binary P5 acceptance; creates only fresh owned TEMP stores.

Run from this checkout with PYTHONPATH=src:
python -m tests.orca_binary_acceptance --binary ABSOLUTE_PINNED_CODEX_EXE
No user store, auth file, transcript, turn, or login is used.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import subprocess
import sys
from contextlib import closing
from pathlib import Path

PARENT = "00000000-0000-4000-8000-000000000101"
CHILD = "00000000-0000-4000-8000-000000000102"
SENTINEL = "00000000-0000-4000-8000-000000000103"


def file_metadata(path):
    info = path.stat()
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "file_id": info.st_ino,
            "device_id": info.st_dev, "nlink": info.st_nlink}


def row_digest(home):
    with closing(sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT * FROM threads WHERE id=?", (SENTINEL,)).fetchone()
    return hashlib.sha256(json.dumps(row, separators=(",", ":")).encode()).hexdigest()


def run(binary):
    # Only the fresh isolated worker imports/discovers product stores.
    from local_agent_record_janitor.adapters import OrcaAdapter
    from local_agent_record_janitor.cleanup_service import CleanupService
    from local_agent_record_janitor.operation_coordinator import OperationCoordinator
    from local_agent_record_janitor.orca_runtime import PINNED_BINARY_SHA256, RUNTIME_ACCEPTED, invocation_policy
    from tests.orca_native_support import create_native_schema, add_native_record
    from tests.orca_support import create_profile
    if os.name != "nt" or not RUNTIME_ACCEPTED or not binary.is_absolute():
        raise ValueError("Acceptance requires Windows, the admitted combination, and an absolute fixed binary")
    if file_metadata(binary)["sha256"] != PINNED_BINARY_SHA256:
        raise ValueError("Acceptance binary differs from its fixed SHA256")
    root = Path(tempfile.mkdtemp(prefix="janitor-orca-v3-acceptance-")).resolve(strict=True)
    home = create_profile(root / "orca", accounts=1)[0]
    create_native_schema(home)
    parent = add_native_record(home, PARENT)
    child = add_native_record(home, CHILD, parent=PARENT)
    sentinel = add_native_record(home, SENTINEL)
    index = home / "session_index.jsonl"
    index.write_text("".join(json.dumps({"id": value, "thread_name": "", "updated_at": "2026-10-03T00:00:00Z"}) + "\n"
                             for value in (PARENT, CHILD, SENTINEL)), encoding="utf-8")
    outside = root / "outside-sentinel"
    outside.write_bytes(b"isolated outside sentinel")
    before = {"sentinel": file_metadata(sentinel), "row": row_digest(home), "outside": file_metadata(outside)}
    adapter = OrcaAdapter(profile_root=root / "orca", codex_bin_hint=binary)
    coordinator = OperationCoordinator(CleanupService())
    plan = coordinator.plan_operation(client="orca", record_ids=(PARENT,), engines=("codex",),
        adapters=(adapter,), plan_path=root / "plan.json", operation_id="orca-binary-acceptance")
    if plan.get("goal_status") != "ready":
        raise ValueError("P5 plan blocked: " + json.dumps(plan.get("blockers", plan)))
    arguments = {"client": "orca", "record_ids": (PARENT,), "engines": ("codex",), "adapters": (adapter,),
                 "operation_id": plan["operation_id"], "plan_path": root / "plan.json", "plan_sha256": plan["plan_sha256"]}
    no_ack = coordinator.apply_operation(**arguments)
    assert no_ack["goal_status"] == "blocked" and not no_ack["mutation_started"]
    linked = home / "sessions" / "hardlink-negative"
    os.link(parent, linked)
    try:
        link_rejection = coordinator.apply_operation(**arguments, clients_closed=True)
        assert link_rejection["goal_status"] == "blocked" and not link_rejection["mutation_started"]
    finally:
        linked.unlink()
    cold_arguments = {key: value for key, value in arguments.items() if key != "adapters"}
    result = OperationCoordinator(CleanupService()).apply_operation(**cold_arguments, clients_closed=True)
    # Any ambiguous outcome is retained for status/verify; never send again.
    cold = OperationCoordinator(CleanupService())
    status = cold.status_operation(operation_id=plan["operation_id"], plan_path=root / "plan.json")
    verify = cold.verify_operation(operation_id=plan["operation_id"], plan_path=root / "plan.json")
    after = {"sentinel": file_metadata(sentinel), "row": row_digest(home), "outside": file_metadata(outside)}
    report = {"root": str(root), "binary": str(binary), "binary_sha256": PINNED_BINARY_SHA256,
              "invocation_policy": invocation_policy(), "schema_version": plan["schema_version"],
              "clients_closed_ack_rejected": no_ack["goal_status"], "hardlink_rejected": link_rejection["goal_status"],
              "result": result, "cold_status": status, "cold_verify": verify, "sentinel_unchanged": before == after,
              "approved_rollouts_absent": not parent.exists() and not child.exists(),
              "session_index_ids": [json.loads(line)["id"] for line in index.read_text().splitlines()]}
    output = root / "acceptance-result.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert result["goal_status"] == status["goal_status"] == verify["goal_status"] == "complete", str(output)
    assert before == after and report["approved_rollouts_absent"] and report["session_index_ids"] == [SENTINEL], str(output)
    return output


def run_isolated(binary):
    """Enter an entirely new process before any profile discovery/API cache."""
    from local_agent_record_janitor.orca_runtime import isolated_environment
    if os.name != "nt":
        raise ValueError("Fixed binary acceptance requires Windows")
    root = Path(tempfile.mkdtemp(prefix="janitor-orca-acceptance-wrapper-")).resolve(strict=True)
    home = root / "empty-default-codex-home"
    home.mkdir()
    scratch = root / "environment"
    scratch.mkdir()
    environment, cwd = isolated_environment(scratch, home)
    repository = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-I", "-B", "-c",
        "import sys;from pathlib import Path;sys.path[:0]=[sys.argv[1],str(Path(sys.argv[1])/'src')];"
        "from tests.orca_binary_acceptance import run;print(run(Path(sys.argv[2])))",
        str(repository), str(binary)]
    result = subprocess.run(command, env=environment, cwd=cwd, capture_output=True, text=True,
        timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("Isolated acceptance failed; preserve TEMP evidence at " + str(root) + "\n" + result.stderr)
    return result.stdout.strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    print(run_isolated(parser.parse_args().binary))
