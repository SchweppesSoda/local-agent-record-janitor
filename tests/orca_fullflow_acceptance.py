"""Opt-in, isolated fixed-binary native plus frontend acceptance on TEMP data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


def run(binary, scenario="complete"):
    from local_agent_record_janitor.adapters import OrcaAdapter
    from local_agent_record_janitor.cleanup_service import CleanupService
    from local_agent_record_janitor.operation_coordinator import OperationCoordinator
    from tests.test_orca_frontend_cleanup import OrcaFrontendCleanupTests
    from tests.orca_native_support import create_native_schema, add_native_record
    from tests.orca_support import CURRENT_ID, HISTORY_ID
    from tests.orca_binary_acceptance import file_metadata
    fixture = OrcaFrontendCleanupTests()
    # Preserve this opt-in acceptance's synthetic evidence for investigation.
    fixture.addCleanup = lambda cleanup: cleanup.__self__._finalizer.detach()
    fixture.setUp()
    root, home = fixture.root, fixture.homes[0]
    create_native_schema(home)
    paths = [add_native_record(home, sid) for sid in (CURRENT_ID, HISTORY_ID)]
    sentinel = add_native_record(home, "00000000-0000-4000-8000-000000000103")
    before = file_metadata(sentinel)
    adapter = OrcaAdapter(profile_root=root, codex_bin_hint=binary)
    plan_path = root.parent / "plan.json"
    coordinator = OperationCoordinator(CleanupService())
    plan = coordinator.plan_operation(client="orca", record_ids=("orca_fixture_01",), engines=("codex",),
        adapters=(adapter,), plan_path=plan_path, operation_id="orca-full-acceptance")
    output = root.parent / "acceptance-result.json"
    report = {"root": str(root), "plan": plan}
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert plan.get("goal_status") == "ready", str(output)
    args = dict(operation_id=plan["operation_id"], plan_path=plan_path, plan_sha256=plan["plan_sha256"])
    if scenario == "cold-continue":
        from local_agent_record_janitor.orca_journal_cleanup import OrcaFrontendError
        with patch("local_agent_record_janitor.orca_cleanup.execute", side_effect=OrcaFrontendError("synthetic_guard_closed")):
            first = OperationCoordinator(CleanupService()).apply_operation(**args, clients_closed=True)
        report["interrupted"] = first
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        assert all(not path.exists() for path in paths), str(output)
        # No native process may be restarted when its durable child completed.
        with patch("local_agent_record_janitor.orca_runtime.IsolatedCodexRuntime.__enter__",
                   side_effect=AssertionError("native child must not repeat")):
            result = OperationCoordinator(CleanupService()).apply_operation(**args, clients_closed=True)
    else:
        result = OperationCoordinator(CleanupService()).apply_operation(**args, clients_closed=True)
    cold = OperationCoordinator(CleanupService())
    status = cold.status_operation(operation_id=plan["operation_id"], plan_path=plan_path)
    verify = cold.verify_operation(operation_id=plan["operation_id"], plan_path=plan_path)
    report.update(result=result, cold_status=status, cold_verify=verify,
                  native_absent=all(not path.exists() for path in paths), sentinel_unchanged=file_metadata(sentinel) == before)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert result["goal_status"] == status["goal_status"] == verify["goal_status"] == "complete", str(output)
    assert report["native_absent"] and report["sentinel_unchanged"], str(output)
    return output


def run_isolated(binary, scenario="complete"):
    from local_agent_record_janitor.orca_runtime import isolated_environment
    if os.name != "nt":
        raise ValueError("Fixed binary acceptance requires Windows")
    root = Path(tempfile.mkdtemp(prefix="janitor-orca-fullflow-wrapper-")).resolve(strict=True)
    home, scratch = root / "empty-home", root / "environment"
    home.mkdir(); scratch.mkdir()
    environment, cwd = isolated_environment(scratch, home)
    repository = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-I", "-B", "-c",
        "import sys;from pathlib import Path;sys.path[:0]=[sys.argv[1],str(Path(sys.argv[1])/'src')];"
        "from tests.orca_fullflow_acceptance import run;print(run(Path(sys.argv[2]),sys.argv[3]))", str(repository), str(binary), scenario]
    result = subprocess.run(command, env=environment, cwd=cwd, capture_output=True, text=True,
                            timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("Isolated acceptance failed: " + result.stderr)
    return result.stdout.strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--scenario", choices=("complete", "cold-continue"), default="complete")
    args = parser.parse_args()
    print(run_isolated(args.binary, args.scenario))
