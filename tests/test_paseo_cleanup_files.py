"""Read-only transformations and private synthetic stores; never live cleanup."""
from pathlib import Path
import copy
import json
import sys
import unittest
import tempfile

WORKSPACE = next(parent for parent in Path(__file__).resolve().parents
                 if (parent / 'src/local_agent_record_janitor/paseo_cleanup_files.py').is_file())
sys.path.insert(0, str(WORKSPACE / "src"))
from local_agent_record_janitor import paseo_cleanup_files as f

SELECTED = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
STAMP = "2026-10-05T00:00:00.000Z"
results = []


def record(agent_id=SELECTED):
    return {"id": agent_id, "provider": "codex", "cwd": "D:/project", "createdAt": STAMP,
            "updatedAt": STAMP, "labels": {}, "persistence": {"provider": "codex", "sessionId": "native-a"}}


def run(agent_id=SELECTED, status="succeeded", run_id="run-1"):
    return {"id": run_id, "scheduledFor": STAMP, "startedAt": STAMP, "endedAt": STAMP,
            "status": status, "agentId": agent_id, "workspaceId": "workspace-a", "output": "output", "error": None}


def schedule(target=None, runs=None):
    return {"id": "abcdef12", "name": "schedule", "prompt": "prompt", "cadence": {"type": "every", "everyMs": 1000},
            "target": target or {"type": "new-agent", "config": {"provider": "codex", "cwd": "D:/project"}},
            "status": "active", "createdAt": STAMP, "updatedAt": STAMP, "nextRunAt": STAMP, "lastRunAt": STAMP,
            "pausedAt": None, "expiresAt": None, "maxRuns": 2, "runs": runs if runs is not None else [run()]}


def transformed(value):
    f._schedule(value)
    return f.transform({"kind": "schedule", "value": value, "raw": f.encode(value)}, {SELECTED})


def blocked(thunk):
    try:
        thunk()
    except (ValueError, UnicodeError, RecursionError):
        return
    raise AssertionError("unsafe/unsupported input accepted")


def vector(name, thunk):
    results.append((name, thunk))


def ledger_preserved():
    value = schedule(runs=[run(), run(OTHER, "failed", "run-2")])
    after = f.decode(transformed(value))
    expected = copy.deepcopy(value)
    expected["runs"][0].update(agentId=None, output=None, error=None)
    assert after == expected
    assert len(after["runs"]) == len(value["runs"])
    assert [r["status"] for r in after["runs"]] == [r["status"] for r in value["runs"]]


vector("selected completed run redaction preserves unselected data and ledger", ledger_preserved)
vector("owned schedule containing unselected historical run must block whole-file deletion", lambda:
       blocked(lambda: transformed(schedule({"type": "agent", "agentId": SELECTED}, [run(OTHER)]))))
vector("selected running run blocks", lambda: blocked(lambda: transformed(schedule(runs=[run(status="running")]))))
vector("owned schedule with null-ID running run blocks", lambda: blocked(lambda:
       transformed(schedule({"type": "agent", "agentId": SELECTED}, [run(None, "running")]))))
vector("unknown top-level schedule field blocks", lambda: blocked(lambda:
       f._schedule({**schedule(), "futureBinding": OTHER})))
vector("unknown run field blocks", lambda: blocked(lambda:
       f._schedule(schedule(runs=[{**run(), "futureBinding": OTHER}]))))
vector("unknown new-agent config field blocks", lambda: blocked(lambda:
       f._schedule(schedule({"type": "new-agent", "config": {"provider": "codex", "cwd": "D:/project", "futureBinding": OTHER}}))))
vector("nonguid target agent identity blocks", lambda: blocked(lambda:
       f._schedule(schedule({"type": "agent", "agentId": "agent-a"}))))
vector("nonguid unselected run identity blocks", lambda: blocked(lambda:
       f._schedule(schedule(runs=[run(), run("agent-b", run_id="run-2")]))))


def malformed_config_fields():
    for key, value in {"archiveOnFinish": 1, "isolation": "ssh", "modeId": None,
                       "model": "  ", "thinkingOptionId": 42, "title": False,
                       "providerOptions": [], "featureValues": "wrong", "mcpServers": False,
                       "systemPrompt": []}.items():
        config = {"provider": "codex", "cwd": "D:/project", key: value}
        blocked(lambda config=config: f._schedule(schedule({"type": "new-agent", "config": config})))


vector("every new-agent config field has source-matching type validation", malformed_config_fields)
vector("whitespace-only cron timezone blocks", lambda: blocked(lambda:
       f._schedule({**schedule(), "cadence": {"type": "cron", "expression": "* * * * *", "timezone": "  "}})))
vector("whitespace-only new-agent cwd blocks", lambda: blocked(lambda:
       f._schedule(schedule({"type": "new-agent", "config": {"provider": "codex", "cwd": "  "}}))))
vector("duplicate JSON keys block", lambda: blocked(lambda: f.decode(b'{"x":1,"x":2}')))
vector("JSON exponent overflow blocks at decode", lambda: blocked(lambda: f.decode(b'{"x":1e400}')))
vector("negative-zero JSON token blocks before rewriting unselected opaque values", lambda:
       blocked(lambda: f.decode(b'{"x":-0}')))
vector("huge integer JSON token cannot qualify an infinite JavaScript value", lambda:
       blocked(lambda: f.decode(b'{"x":' + b'9' * 400 + b'}')))


def surrogate_retained():
    value = schedule(runs=[run(), {**run(OTHER, run_id="run-2"), "output": "\ud800"}])
    raw = json.dumps(value, ensure_ascii=True).encode("utf-8")
    parsed = f.decode(raw)
    f._schedule(parsed)
    after = f.transform({"kind": "schedule", "value": parsed, "raw": raw}, {SELECTED})
    assert f.decode(after)["runs"][1]["output"] == "\ud800"


vector("native-valid escaped surrogate in unselected output remains encodable", surrogate_retained)


def unknown_agent_nested():
    with tempfile.TemporaryDirectory(prefix="larj-paseo-files-test-") as temporary:
        root = Path(temporary).resolve(strict=True)
        assert root.parent == Path(tempfile.gettempdir()).resolve(strict=True)
        (root / "agents").mkdir()
        primary = root / "agents" / f"{SELECTED}.json"
        for field, value in {"owner": {"kind": "daemon", "daemonId": "daemon-a", "executionId": "exec-a", "futureNativeBinding": OTHER},
                             "runtimeInfo": {"provider": "codex", "sessionId": "native-a", "futureNativeBinding": OTHER},
                             "persistence": {"provider": "codex", "sessionId": "native-a", "futureNativeBinding": OTHER},
                             "config": {"futureNativeBinding": OTHER}}.items():
            primary.write_bytes(f.encode({**record(), field: value}))
            blocked(lambda: f.rows(root))


vector("unknown structural agent nested fields fail closed", unknown_agent_nested)


def atomic_closure():
    with tempfile.TemporaryDirectory(prefix="larj-paseo-files-test-") as temporary:
        root = Path(temporary).resolve(strict=True)
        assert root.parent == Path(tempfile.gettempdir()).resolve(strict=True)
        (root / "agents" / "project").mkdir(parents=True)
        (root / "schedules").mkdir()
        kept = root / "agents" / f"{OTHER}.json"
        kept.write_bytes(f.encode(record(OTHER)))
        (root / "agents" / f"{SELECTED}.json").write_bytes(f.encode(record()))
        atomic_name = f".{SELECTED}.json.123.1780000000000.33333333-3333-4333-8333-333333333333.tmp"
        (root / "agents" / "project" / atomic_name).write_bytes(f.encode(record()))
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        evidence = f.freeze(root, [SELECTED])
        deletes = [x["before"]["path"] for x in evidence["files"] if x["after_sha256"] is None]
        assert deletes == [f"agents/{SELECTED}.json", f"agents/project/{atomic_name}"]
        assert f.remaining(evidence) == 2
        assert before == {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        (root / "agents" / "project" / atomic_name).write_bytes(f.encode(record(OTHER)))
        blocked(lambda: f.rows(root))


vector("atomic copies freeze by exact owner and conflicting basename blocks", atomic_closure)


def noncanonical_atomic_uuid():
    with tempfile.TemporaryDirectory(prefix="larj-paseo-files-test-") as temporary:
        root = Path(temporary).resolve(strict=True)
        assert root.parent == Path(tempfile.gettempdir()).resolve(strict=True)
        (root / "agents").mkdir()
        atom = f".{SELECTED}.json.123.1780000000000.----33333333333343338333333333333333.tmp"
        (root / "agents" / atom).write_bytes(f.encode(record()))
        blocked(lambda: f.rows(root))


vector("noncanonical UUID cannot classify an unowned file as a native atomic copy", noncanonical_atomic_uuid)
class PaseoCleanupFilesTests(unittest.TestCase):
    def test_fixed_schema_server_vectors(self):
        self.assertEqual(len(results), 20)
        for name, test in results:
            with self.subTest(vector=name):
                test()


if __name__ == '__main__':
    unittest.main()
