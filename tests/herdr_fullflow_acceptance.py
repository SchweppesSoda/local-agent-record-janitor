"""Opt-in real Codex deletion with a synthetic Herdr profile in isolated TEMP."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


def run(binary, scenario="complete"):
    from local_agent_record_janitor.herdr_bound_adapter import HerdrBoundAdapter, SCHEMA
    from local_agent_record_janitor.herdr_cleanup_json import fingerprint
    from local_agent_record_janitor.cleanup_service import CleanupService
    from local_agent_record_janitor.operation_coordinator import OperationCoordinator
    from tests.herdr_support import CODEX_ID, agent_session, pane, snapshot, tab, write_snapshot, recovery_name
    from tests.orca_native_support import create_native_schema, add_native_record
    from tests.orca_binary_acceptance import file_metadata
    root = Path(tempfile.mkdtemp(prefix="janitor-herdr-fullflow-")).resolve()
    profile, home = root / "herdr", root / "codex"
    profile.mkdir(); home.mkdir()
    create_native_schema(home)
    selected = add_native_record(home, CODEX_ID)
    keep_id = "33333333-3333-4333-8333-333333333333"
    sentinel = add_native_record(home, keep_id)
    before = file_metadata(sentinel)
    # An inert synthetic executable identity exercises the same share-mode
    # gate; this test does not claim to start a real Herdr GUI/server.
    herdr_bin = root / "synthetic-herdr.exe"
    herdr_bin.write_bytes(b"synthetic Herdr runtime identity: never launched")
    value = snapshot(root, (tab({0: pane(root, agent_session(), private="ERASE"),
                               1: pane(root, agent_session(value=keep_id), private="KEEP")}),))
    write_snapshot(profile / "session.json", value)
    write_snapshot(profile / "session-backups" / recovery_name(), value)
    write_snapshot(profile / "session-history.json", {"version": 3, "layout_fingerprint": fingerprint(value),
        "workspaces": [{"tabs": [{"panes": {"0": {"ansi": "ERASE", "lines": 1},
                                               "1": {"ansi": "KEEP", "lines": 1}}}]}]})
    manifest = {"schema_version": SCHEMA, "profile_root": str(profile), "runtime_binaries": [str(herdr_bin)],
                "native_stores": [{"session": "default", "engine": "codex", "root": str(home), "codex_binary": str(binary)}]}
    plan_path = root / "plan.json"
    plan = OperationCoordinator(CleanupService()).plan_operation(client="herdr", record_ids=(CODEX_ID,),
        adapters=(HerdrBoundAdapter(manifest),), plan_path=plan_path, operation_id="herdr-acceptance")
    output = root / "acceptance-result.json"
    report = {"plan": plan, "scenario": scenario}
    def save():
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    save()
    assert plan["goal_status"] == "ready", str(output)
    args = dict(operation_id=plan["operation_id"], plan_path=plan_path, plan_sha256=plan["plan_sha256"])
    if scenario == "cold-continue":
        with patch("local_agent_record_janitor.herdr_cleanup.execute", side_effect=RuntimeError("synthetic pre-mutation stop")):
            report["interrupted"] = OperationCoordinator(CleanupService()).apply_operation(**args, clients_closed=True)
        save()
        assert not selected.exists(), str(output)
        with patch("local_agent_record_janitor.operation_coordinator.OperationCoordinator._execute_manual_batch",
                   side_effect=AssertionError("completed native child must not repeat")):
            result = OperationCoordinator(CleanupService()).apply_operation(**args, clients_closed=True)
    else:
        result = OperationCoordinator(CleanupService()).apply_operation(**args, clients_closed=True)
    report["result"] = result
    report["status"] = OperationCoordinator(CleanupService()).status_operation(operation_id=plan["operation_id"], plan_path=plan_path)
    report["verify"] = OperationCoordinator(CleanupService()).verify_operation(operation_id=plan["operation_id"], plan_path=plan_path)
    report["sentinel_unchanged"] = file_metadata(sentinel) == before
    report["native_absent"] = not selected.exists()
    save()
    assert all(report[key]["goal_status"] == "complete" for key in ("result", "status", "verify")), str(output)
    assert report["native_absent"] and report["sentinel_unchanged"], str(output)
    return output


def run_isolated(binary, scenario):
    from local_agent_record_janitor.orca_runtime import isolated_environment
    if os.name != "nt":
        raise ValueError("Qualified Herdr lifecycle requires Windows")
    root = Path(tempfile.mkdtemp(prefix="janitor-herdr-wrapper-")).resolve()
    home, scratch = root / "empty-home", root / "environment"
    home.mkdir(); scratch.mkdir()
    environment, cwd = isolated_environment(scratch, home)
    repository = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-I", "-B", "-c",
        "import sys;from pathlib import Path;sys.path[:0]=[sys.argv[1],str(Path(sys.argv[1])/'src')];"
        "from tests.herdr_fullflow_acceptance import run;print(run(Path(sys.argv[2]),sys.argv[3]))",
        str(repository), str(binary), scenario]
    result = subprocess.run(command, env=environment, cwd=cwd, capture_output=True, text=True,
                            timeout=240, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("Isolated Herdr acceptance failed: " + result.stderr)
    return result.stdout.strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--scenario", choices=("complete", "cold-continue"), default="complete")
    args = parser.parse_args()
    print(run_isolated(args.binary, args.scenario))
