"""Opt-in v2 configured-home acceptance. Fresh owned TEMP synthetic stores only.

python -m tests.orca_configured_acceptance --binary ABSOLUTE_PINNED_CODEX_EXE

Requires the production source to admit the exact measured binary with v2
evidence and ephemeral auth. It never monkeypatches registration, a binary pin,
configuration policy, or authorization. Native deletion runs once, only through
the public coordinator; negative guard cases must not start an app-server.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import uuid

CANDIDATE_SHA256 = "fdda5fa3cf3fb3d000b876720742857676293e4315e4b045fae6f8bd7e866d1d"
PARENT = "00000000-0000-4000-8000-000000000301"
CHILD = "00000000-0000-4000-8000-000000000302"
SENTINEL = "00000000-0000-4000-8000-000000000303"
PRIVATE_MARKERS = (
    "ORCA_PRIVATE_OPTIONS_SENTINEL",
    "SYNTHETIC_OPAQUE_AUTH_DO_NOT_PARSE",
    "SYNTHETIC_OPAQUE_MCP_AUTH_DO_NOT_PARSE",
    "SYNTHETIC_OPAQUE_LEGACY_AUTH_DO_NOT_PARSE",
)
OWNERSHIP_MARKER = ".configured-acceptance-owned"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_metadata(path):
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
            or getattr(before, "st_file_attributes", 0) & 0x400):
        raise ValueError("acceptance_nonplain_file")
    fingerprint = hashlib.sha256()
    with path.open("rb") as stream:
        if (os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("acceptance_file_open_race")
        while block := stream.read(1024 * 1024):
            fingerprint.update(block)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink):
        raise ValueError("acceptance_file_read_race")
    return {"sha256": fingerprint.hexdigest(), "size": before.st_size,
            "device_id": before.st_dev, "file_id": before.st_ino, "nlink": before.st_nlink}


def native_rows(home):
    with closing(sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = [list(row) for row in connection.execute("SELECT * FROM threads ORDER BY id")]
        edges = [list(row) for row in connection.execute("SELECT * FROM thread_spawn_edges ORDER BY parent_thread_id,child_thread_id")]
        sentinel = list(connection.execute("SELECT * FROM threads WHERE id=?", (SENTINEL,)).fetchone() or ())
    return {"ids": [row[0] for row in rows], "row_sha256": digest(json.dumps(rows, separators=(",", ":")).encode()),
            "edges_sha256": digest(json.dumps(edges, separators=(",", ":")).encode()),
            "edge_count": len(edges), "sentinel_row_sha256": digest(json.dumps(sentinel, separators=(",", ":")).encode())}


def stable_startup(home):
    """Existing stable objects only; ephemeral runtime buckets are separate."""
    result = {}
    for name in ("installation_id", "skills", "thread-writer-locks", ".sqlite-maintenance.lock"):
        root = home / name
        if not root.exists():
            continue
        pending = [root]
        while pending:
            path = pending.pop()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("acceptance_linked_startup")
            relative = path.relative_to(home).as_posix()
            if stat.S_ISDIR(info.st_mode):
                result[relative] = {"kind": "directory", "device_id": info.st_dev, "file_id": info.st_ino}
                pending.extend(path.iterdir())
            else:
                result[relative] = {"kind": "file", **file_metadata(path)}
            if len(result) + len(pending) > 4096:
                raise ValueError("acceptance_startup_budget")
    return result


def assert_private_free(value):
    encoded = json.dumps(value, ensure_ascii=False)
    if any(marker in encoded for marker in PRIVATE_MARKERS):
        raise ValueError("acceptance_private_sentinel_leaked")


def save_json(path, value):
    assert_private_free(value)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def imports(repository):
    sys.path[:0] = [str(repository), str(repository / "src")]
    from local_agent_record_janitor import orca_runtime as runtime
    from local_agent_record_janitor import orca_target_safety as safety
    if (os.name != "nt" or not runtime.RUNTIME_ACCEPTED
            or runtime.PINNED_BINARY_SHA256 != CANDIDATE_SHA256
            or runtime.RUNTIME_POLICY_SCHEMA != "larj.orca-runtime.v2"
            or safety.EVIDENCE_SCHEMA != "larj.orca-target-safety.v2"
            or 'cli_auth_credentials_store="ephemeral"' not in runtime.CONFIG_OVERRIDES
            or 'approval_policy="never"' not in runtime.CONFIG_OVERRIDES
            or 'sandbox_mode="read-only"' not in runtime.CONFIG_OVERRIDES):
        raise ValueError("acceptance_requires_registered_v2_exact_candidate_ephemeral_policy")
    return runtime, safety


def require_owned(root):
    root = root.resolve(strict=True)
    info = root.lstat()
    if (root.name != "worker-owned" or not root.parent.name.startswith("janitor-orca-configured-acceptance-")
            or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not (root / OWNERSHIP_MARKER).is_file()):
        raise ValueError("acceptance_unowned_root")
    return root


def cold_action(repository, root, action):
    """Fresh process: recover adapters and authority solely from persisted plan."""
    root = require_owned(root)
    imports(repository)
    from local_agent_record_janitor.cleanup_service import CleanupService
    from local_agent_record_janitor.operation_coordinator import OperationCoordinator
    context = json.loads((root / "cold-context.json").read_text(encoding="utf-8"))
    coordinator = OperationCoordinator(CleanupService())
    if action == "apply":
        payload = coordinator.apply_operation(client="orca", record_ids=(PARENT,), engines=("codex",),
            operation_id=context["operation_id"], plan_path=root / "final-plan.json",
            plan_sha256=context["plan_sha256"], clients_closed=True)
    elif action == "status":
        payload = coordinator.status_operation(operation_id=context["operation_id"], plan_path=root / "final-plan.json")
    elif action == "verify":
        payload = coordinator.verify_operation(operation_id=context["operation_id"], plan_path=root / "final-plan.json")
    else:
        raise ValueError("acceptance_unknown_cold_action")
    output = root / ("cold-" + action + "-result.json")
    save_json(output, payload)
    print(json.dumps({"result": str(output), "goal_status": payload.get("goal_status"),
                      "mutation_started": payload.get("mutation_started")}), flush=True)


def worker(repository, source_binary, root, script, maintenance_warm_seconds=0):
    root = require_owned(root)
    runtime, safety = imports(repository)
    from local_agent_record_janitor.adapters import OrcaAdapter
    from local_agent_record_janitor.cleanup_service import CleanupService
    from local_agent_record_janitor.cli import main
    from local_agent_record_janitor.operation_coordinator import OperationCoordinator
    from tests.orca_native_support import add_native_record, create_native_schema
    from tests.orca_support import create_profile

    if file_metadata(source_binary)["sha256"] != CANDIDATE_SHA256:
        raise ValueError("acceptance_candidate_source_hash_unverified")
    # Own both package-manifest locations; no other agent's binary is modified.
    binary = root / "standalone-binary" / "bin" / "codex.exe"
    binary.parent.mkdir(parents=True)
    shutil.copyfile(source_binary, binary)
    if file_metadata(binary)["sha256"] != CANDIDATE_SHA256 or binary.stat().st_nlink != 1:
        raise ValueError("acceptance_candidate_copy_identity_unverified")
    for name in ("config.toml", "requirements.toml"):
        safety._absent_configuration(safety._system_configuration_root() / name)

    home = create_profile(root / "orca", accounts=1)[0]
    outside = root / "outside-sentinel"
    outside.write_bytes(b"synthetic outside sentinel unchanged\n")
    if maintenance_warm_seconds:
        create_native_schema(home)
        maintenance_sentinel = add_native_record(home, SENTINEL)
        (home / "config.toml").write_text('model="synthetic-preused-model"\n', encoding="utf-8")
        (home / "auth.json").write_text(PRIVATE_MARKERS[1], encoding="utf-8")
        maintenance_files = (outside, maintenance_sentinel, home / "config.toml", home / "auth.json")
        maintenance_before = {str(path): file_metadata(path) for path in maintenance_files}
        maintenance_rows_before = native_rows(home)
    warm_root = root / "warm-runtime"
    warm_root.mkdir()
    instance = {}
    def ready(name):
        instance.update(schema_version="larj.orca-runtime-instance.v1", job_name=name, **runtime.runtime_host_identity())
    # Startup is measured from the actual candidate, not reconstructed fixtures.
    with runtime.IsolatedCodexRuntime(binary, root=warm_root) as owned:
        server = owned.server(codex_home=home, timeout=20.0, job_ready_callback=ready)
        try:
            with server:
                account = server.request("account/read", {"refreshToken": False})
                if account.get("account") is not None or account.get("requiresOpenaiAuth") is not False:
                    raise ValueError("acceptance_warm_account_not_ephemeral_local")
                if server.stderr_tail:
                    raise ValueError("acceptance_warm_unexpected_stderr")
                deadline = time.monotonic() + maintenance_warm_seconds
                while time.monotonic() < deadline:
                    time.sleep(min(5, max(0, deadline - time.monotonic())))
                if maintenance_warm_seconds:
                    server.request("account/read", {"refreshToken": False})
        finally:
            server.close()
    if not instance or not runtime.runtime_instance_stopped(instance):
        raise ValueError("acceptance_warm_job_not_absent")
    if maintenance_warm_seconds:
        lock = file_metadata(home / ".sqlite-maintenance.lock")
        if lock["nlink"] != 1 or lock["size"] != 0:
            raise ValueError("acceptance_maintenance_lock_unverified")
    with closing(sqlite3.connect((home / "logs_2.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
        warm_log_count = connection.execute("SELECT count(*) FROM logs").fetchone()[0]
        warm_log_targets = list(connection.execute("SELECT level,target,count(*) FROM logs GROUP BY level,target"))
        if not maintenance_warm_seconds and warm_log_count:
            raise ValueError("acceptance_warm_logs_not_empty")
    # Genuine SQLite schemas must independently pass the fixed full-schema proof.
    schemas = {name: safety._schema_metadata(home / name) for name in (
        "state_5.sqlite", "logs_2.sqlite", "memories_1.sqlite", "queue_1.sqlite", "goals_1.sqlite")}
    startup_registry = json.loads(Path(safety.__file__).with_name("orca_startup_0160.json").read_text(encoding="utf-8"))
    if startup_registry.get("binary_sha256") != CANDIDATE_SHA256 or len(startup_registry.get("files", {})) != 49:
        raise ValueError("acceptance_startup_registry_candidate_mismatch")
    if safety._startup_control(home)["backfill_state"][1] != "complete":
        raise ValueError("acceptance_warm_backfill_incomplete")

    if maintenance_warm_seconds:
        # This independent case never deletes a record. The real maintenance
        # worker creates native logs, so its home is no longer an empty-log
        # deletion fixture; the production guard must continue to reject it.
        unchanged = (maintenance_before == {str(path): file_metadata(path) for path in maintenance_files}
                     and maintenance_rows_before == native_rows(home))
        adapter = OrcaAdapter(profile_root=root / "orca", codex_bin_hint=binary)
        evidence = safety.freeze_orca_target(adapter, home, SENTINEL, affected_ids=(SENTINEL,),
            rollout_paths=(maintenance_sentinel,), binary=binary)
        report = {"root": str(root), "binary_sha256": CANDIDATE_SHA256, "schemas": schemas,
            "maintenance_warm_seconds": maintenance_warm_seconds, "warm_job_absent": True,
            "maintenance_lock": lock, "native_runtime_log_count": warm_log_count,
            "native_log_targets": warm_log_targets,
            "configured_and_sentinel_files_unchanged": unchanged,
            "startup_manifest": safety._startup_manifest(home, after_start=True),
            "next_startup_blockers": evidence["blocker_codes"], "native_delete_attempted": False}
        if not unchanged or warm_log_count and "orca_startup_logs_cleanup_unverified" not in evidence["blocker_codes"]:
            raise ValueError("acceptance_maintenance_postcondition_failed")
        output = root / "maintenance-result.json"
        save_json(output, report)
        print(json.dumps({"report": str(output), "maintenance_verified": True,
                          "native_delete_attempted": False, "native_runtime_log_count": warm_log_count}), flush=True)
        return

    parent = add_native_record(home, PARENT)
    child = add_native_record(home, CHILD, parent=PARENT)
    sentinel = add_native_record(home, SENTINEL)
    index = home / "session_index.jsonl"
    index.write_text("".join(json.dumps({"id": value, "thread_name": "", "updated_at": "2026-10-03T00:00:00Z"}) + "\n"
                             for value in (PARENT, CHILD, SENTINEL)), encoding="utf-8")
    project = root / "trusted-project"
    (project / ".codex").mkdir(parents=True)
    project_config = project / ".codex" / "config.toml"
    project_config.write_bytes(b"synthetic project config is intentionally invalid TOML\n")
    config = home / "config.toml"
    config.write_text('model="synthetic-preused-model"\nmodel_provider="openai"\nmodel_reasoning_effort="high"\n'
                      'approval_policy="never"\nsandbox_mode="workspace-write"\ncli_auth_credentials_store="file"\n'
                      '[projects.' + json.dumps(str(project)) + ']\ntrust_level="trusted"\n', encoding="utf-8")
    auth = home / "auth.json"
    mcp_auth = home / ".credentials.json"
    legacy_auth = home / "credentials.json"
    for path, marker in zip((auth, mcp_auth, legacy_auth), PRIVATE_MARKERS[1:]):
        path.write_bytes((marker + " intentionally malformed synthetic bytes\n").encode())
    preserved = {"parent": parent, "child": child, "sentinel": sentinel, "index": index, "outside": outside,
                 "config": config, "auth": auth, "mcp_auth": mcp_auth, "legacy_auth": legacy_auth,
                 "project_config": project_config, "managed_marker": home / ".orca-managed-home"}
    def safety_state():
        with closing(sqlite3.connect((home / "logs_2.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
            logs = list(connection.execute("SELECT id,ts,ts_nanos,thread_id FROM logs ORDER BY id"))
        return {"files": {name: file_metadata(path) for name, path in preserved.items()},
                "native": native_rows(home), "startup": stable_startup(home), "logs_metadata": logs}
    baseline = safety_state()
    adapter = OrcaAdapter(profile_root=root / "orca", codex_bin_hint=binary)

    stdout, stderr = io.StringIO(), io.StringIO()
    exit_code = main(["records", "--client", "orca", "--engine", "codex", "--orca-root", str(root / "orca"), "--json"],
                     stdout=stdout, stderr=stderr, adapters=(adapter,))
    records = json.loads(stdout.getvalue())
    assert_private_free(records)
    assert_private_free(stderr.getvalue())
    if exit_code or records.get("goal_status") == "blocked":
        raise ValueError("acceptance_public_records_blocked")
    ids = {target.get("native_thread_id") for target in records.get("targets", ())}
    if not {PARENT, CHILD, SENTINEL}.issubset(ids):
        raise ValueError("acceptance_public_inventory_missing_synthetic_records")
    if baseline != safety_state():
        raise ValueError("acceptance_public_records_changed_store")
    save_json(root / "public-records.json", records)

    report = {"root": str(root), "binary": str(binary), "binary_source": str(source_binary),
              "binary_sha256": CANDIDATE_SHA256, "invocation_policy": runtime.invocation_policy(),
              "schemas": schemas, "warm_job_absent": True, "maintenance_warm_seconds": maintenance_warm_seconds,
              "public_records_ids": sorted(value for value in ids if value),
              "negative_cases": [], "native_delete_send_count_policy": "one apply; never retry ambiguous outcome"}
    def make_plan(label):
        coordinator = OperationCoordinator(CleanupService())
        path = root / (label + "-plan.json")
        plan = coordinator.plan_operation(client="orca", record_ids=(PARENT,), engines=("codex",),
            adapters=(adapter,), plan_path=path, operation_id="orca-configured-" + label)
        assert_private_free(plan)
        if plan.get("goal_status") != "ready":
            save_json(root / "blocked-plan.json", plan)
            raise ValueError("acceptance_configured_plan_not_ready_" + label)
        arguments = {"client": "orca", "record_ids": (PARENT,), "engines": ("codex",),
                     "operation_id": plan["operation_id"], "plan_path": path, "plan_sha256": plan["plan_sha256"]}
        return plan, arguments

    def negative(label, alter=lambda: None, restore=lambda: None, *, ack=True):
        _, arguments = make_plan(label)
        alter()
        try:
            during = safety_state()
            # The assertion hook never admits a write or changes qualification.
            # A passing case proves no Job/app-server start was even attempted.
            with patch.object(runtime.JobCodexAppServer, "start", side_effect=AssertionError("unexpected native startup")) as startup:
                result = OperationCoordinator(CleanupService()).apply_operation(**arguments, clients_closed=ack)
            assert_private_free(result)
            unchanged = during == safety_state()
            value = {"case": label, "result": result, "startup_calls": startup.call_count,
                     "synthetic_store_unchanged_during_guard": unchanged}
            report["negative_cases"].append(value)
            save_json(root / "partial-acceptance-result.json", report)
            if (result.get("goal_status") != "blocked" or result.get("mutation_started") is not False
                    or startup.call_count != 0 or not unchanged):
                raise ValueError("acceptance_prewrite_guard_failed_" + label)
        finally:
            restore()
        if baseline != safety_state():
            raise ValueError("acceptance_negative_restore_changed_store_" + label)

    negative("clients-closed-ack", ack=False)
    hardlink = home / "sessions" / "synthetic-hardlink-negative"
    negative("rollout-hardlink", lambda: os.link(parent, hardlink), hardlink.unlink)
    compressed = Path(str(parent) + ".zst")
    negative("compressed-rollout-added", lambda: compressed.write_bytes(b"synthetic compressed sibling"),
             compressed.unlink)
    for name in ("thread_history_1.sqlite", "memories_v2_1.sqlite", "agent_message_board_1.sqlite"):
        path = home / name
        negative(name + "-added", lambda path=path: path.write_bytes(b"synthetic unregistered store"),
                 lambda path=path: path.unlink())
    def change_log(*, insert):
        with closing(sqlite3.connect(home / "logs_2.sqlite")) as connection:
            if insert:
                connection.execute("INSERT INTO logs(ts,ts_nanos,level,target,thread_id) VALUES(0,0,'INFO','acceptance',?)", (SENTINEL,))
            else:
                connection.execute("DELETE FROM logs WHERE target='acceptance' AND thread_id=?", (SENTINEL,))
            connection.commit()
    negative("unrelated-expired-log-added", lambda: change_log(insert=True), lambda: change_log(insert=False))
    temporary_index = home / "session_index.jsonl.tmp"
    negative("native-index-replacement-added", lambda: os.link(sentinel, temporary_index), temporary_index.unlink)
    def change_memory(*, insert):
        with closing(sqlite3.connect(home / "memories_1.sqlite")) as connection:
            if insert:
                connection.execute("INSERT INTO stage1_outputs(thread_id,source_updated_at,raw_memory,rollout_summary,generated_at,selected_for_phase2) VALUES (?,1,'synthetic','synthetic',1,1)", (PARENT,))
            else:
                connection.execute("DELETE FROM stage1_outputs WHERE thread_id=?", (PARENT,))
            connection.commit()
    negative("global-memory-job-trigger-added", lambda: change_memory(insert=True), lambda: change_memory(insert=False))
    config_original = config.read_bytes()
    negative("config-value-drift", lambda: config.write_bytes(config_original.replace(b'"high"', b'"medium"')),
             lambda: config.write_bytes(config_original))
    negative("config-unknown-key", lambda: config.write_bytes(b'sqlite_home="synthetic-unapproved"\n' + config_original),
             lambda: config.write_bytes(config_original))
    auth_original = auth.read_bytes()
    negative("opaque-auth-content-drift", lambda: auth.write_bytes(auth_original + b"synthetic drift\n"),
             lambda: auth.write_bytes(auth_original))
    environment_config = home / "environments.toml"
    negative("environment-config-added", lambda: environment_config.write_bytes(b"synthetic unapproved environment metadata\n"),
             environment_config.unlink)
    skill = home / "skills" / ".system" / "skill-creator" / "SKILL.md"
    skill_original = skill.read_bytes()
    negative("stable-startup-content-drift", lambda: skill.write_bytes(skill_original + b"\nsynthetic content drift\n"),
             lambda: skill.write_bytes(skill_original))
    for label, manifest in (("binary-adjacent-manifest-added", binary.parent / "codex-package.json"),
                            ("binary-parent-manifest-added", binary.parent.parent / "codex-package.json")):
        if manifest.exists():
            raise ValueError("acceptance_owned_package_manifest_not_initially_absent")
        negative(label, lambda path=manifest: path.write_bytes(b'{"synthetic":"unapproved-package-context"}\n'),
                 lambda path=manifest: path.unlink())

    final_plan, _ = make_plan("final")
    save_json(root / "cold-context.json", {"operation_id": final_plan["operation_id"], "plan_sha256": final_plan["plan_sha256"]})
    save_json(root / "pre-apply-safety-state.json", baseline)
    def dispatch(action):
        empty = root / ("cold-" + action + "-default-home")
        empty.mkdir()
        environment_root = root / ("cold-" + action + "-environment")
        environment_root.mkdir()
        environment, cwd = runtime.isolated_environment(environment_root, empty)
        command = [sys.executable, "-I", "-B", str(script), "--cold", action, "--repository", str(repository),
                   "--binary", str(binary), "--root", str(root)]
        completed = subprocess.run(command, env=environment, cwd=cwd, capture_output=True, text=True,
                                   timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        metadata = {"returncode": completed.returncode, "stderr_sha256": digest(completed.stderr.encode()),
                    "stderr_line_count": len(completed.stderr.splitlines())}
        save_json(root / ("cold-" + action + "-process.json"), metadata)
        if completed.returncode:
            raise ValueError("acceptance_cold_process_failed_" + action)
        return json.loads((root / ("cold-" + action + "-result.json")).read_text(encoding="utf-8"))

    # Exactly one deletion attempt; retain any uncertainty, then inspect it cold.
    result = dispatch("apply")
    status = dispatch("status")
    verify = dispatch("verify")
    after_rows = native_rows(home)
    after_startup = stable_startup(home)
    unchanged_files = {name: file_metadata(path) == baseline["files"][name]
                       for name, path in preserved.items() if name not in {"parent", "child", "index"}}
    startup_preserved = all(after_startup.get(name) == value for name, value in baseline["startup"].items())
    startup_manifest = safety._startup_manifest(home, after_start=True)
    report.update(schema_version=final_plan["schema_version"], result=result, cold_status=status, cold_verify=verify,
                  approved_rollouts_absent=not parent.exists() and not child.exists(),
                  native_ids_after=after_rows["ids"], native_edges_after=after_rows["edge_count"],
                  sentinel_row_unchanged=after_rows["sentinel_row_sha256"] == baseline["native"]["sentinel_row_sha256"],
                  configured_and_sentinel_files_unchanged=unchanged_files, existing_stable_startup_preserved=startup_preserved,
                  startup_entries_added=sorted(set(after_startup) - set(baseline["startup"])),
                  final_startup_manifest=startup_manifest,
                  session_index_ids=[json.loads(line)["id"] for line in index.read_text().splitlines()])
    output = root / "acceptance-result.json"
    save_json(output, report)
    if not (result.get("goal_status") == status.get("goal_status") == verify.get("goal_status") == "complete"
            and report["approved_rollouts_absent"] and report["native_ids_after"] == [SENTINEL]
            and report["native_edges_after"] == 0 and report["sentinel_row_unchanged"]
            and all(unchanged_files.values()) and startup_preserved and report["session_index_ids"] == [SENTINEL]):
        raise ValueError("acceptance_configured_postcondition_failed")
    print(json.dumps({"report": str(output), "goal_status": result["goal_status"],
                      "negative_cases": len(report["negative_cases"]), "all_prewrite_guards_passed": True,
                      "configured_and_sentinel_files_unchanged": all(unchanged_files.values()),
                      "existing_stable_startup_preserved": startup_preserved}), flush=True)


def run_isolated(repository, binary, script, maintenance_warm_seconds=0):
    # Only the environment helper is imported before the fresh worker.
    sys.path.insert(0, str(repository / "src"))
    from local_agent_record_janitor.orca_runtime import isolated_environment
    root = Path(tempfile.mkdtemp(prefix="janitor-orca-configured-acceptance-")).resolve(strict=True)
    empty = root / "empty-default-home"
    empty.mkdir()
    environment_root = root / "worker-environment"
    environment_root.mkdir()
    work = root / "worker-owned"
    work.mkdir()
    (work / OWNERSHIP_MARKER).write_text(uuid.uuid4().hex + "\n", encoding="ascii")
    environment, cwd = isolated_environment(environment_root, empty)
    command = [sys.executable, "-I", "-B", str(script), "--worker", "--repository", str(repository),
               "--binary", str(binary), "--root", str(work), "--maintenance-warm-seconds", str(maintenance_warm_seconds)]
    completed = subprocess.run(command, env=environment, cwd=cwd, capture_output=True, text=True,
                               timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
    value = {"isolated_root": str(root), "returncode": completed.returncode,
             "stdout": completed.stdout.strip(), "stderr_sha256": digest(completed.stderr.encode()),
             "stderr_line_count": len(completed.stderr.splitlines())}
    save_json(root / "wrapper-result.json", value)
    print(json.dumps(value), flush=True)
    if completed.returncode:
        raise ValueError("configured acceptance failed; retain only synthetic TEMP evidence at " + str(root))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--cold", choices=("apply", "status", "verify"))
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--maintenance-warm-seconds", type=int, choices=(0, 65), default=0)
    arguments = parser.parse_args()
    try:
        if not arguments.repository.is_absolute() or not arguments.binary.is_absolute():
            raise ValueError("acceptance_paths_not_absolute")
        if arguments.cold:
            cold_action(arguments.repository, arguments.root, arguments.cold)
        elif arguments.worker:
            worker(arguments.repository, arguments.binary, arguments.root, Path(__file__).resolve(), arguments.maintenance_warm_seconds)
        else:
            run_isolated(arguments.repository, arguments.binary, Path(__file__).resolve(), arguments.maintenance_warm_seconds)
    except Exception as error:
        # Do not echo potentially rich product exceptions or stderr.
        failure = {"error_type": type(error).__name__, "error_sha256": digest(str(error).encode())}
        if str(error).startswith("acceptance_") and len(str(error)) < 160:
            failure["lab_error_code"] = str(error)
        if arguments.root and arguments.root.exists() and (arguments.root / OWNERSHIP_MARKER).is_file():
            save_json(arguments.root / (("cold-" + arguments.cold if arguments.cold else "worker") + "-failure.json"), failure)
        print(json.dumps(failure), file=sys.stderr, flush=True)
        raise SystemExit(1)
