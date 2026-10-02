"""Synthetic persisted v3 structures from pinned Herdr d6b40d4; no runtime."""

from __future__ import annotations

import json
from pathlib import Path

CODEX_ID = "11111111-1111-4111-8111-111111111111"
CLAUDE_ID = "22222222-2222-4222-8222-222222222222"
SENTINEL = "HERDR_PRIVATE_ARGV_AND_ANSI_FIXTURE"


def agent_session(agent="codex", value=CODEX_ID, kind="id", source=None):
    return {"source": source or "herdr:" + agent, "agent": agent, "kind": kind, "value": value}


def pane(cwd: Path, session=None, **extra):
    result = {"cwd": str(cwd), **extra}
    if session is not None:
        result["agent_session"] = session
    return result


def tab(panes):
    ids = tuple(panes)
    layout = {"Pane": ids[0]}
    for identifier in ids[1:]:
        layout = {"Split": {"direction": "Horizontal", "ratio": 0.5, "first": layout, "second": {"Pane": identifier}}}
    return {"layout": layout, "panes": {str(i): value for i, value in panes.items()}, "zoomed": False}


def snapshot(cwd: Path, tabs=()):
    # All required persisted fields. Pane IDs include legal u32 zero; cwd is
    # project metadata and never supplies a Codex or Claude native root.
    workspaces = [{"identity_cwd": str(cwd), "tabs": list(tabs)}] if tabs else []
    return {"version": 3, "workspaces": workspaces, "active": 0 if workspaces else None, "selected": 0}


def recovery_name(sequence=0, timestamp=1):
    return f"session-{timestamp:039d}-1-{sequence}.json"


def write_snapshot(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def create_profile(root: Path, *, mixed=False):
    project = root / "project"
    project.mkdir(parents=True)
    codex = snapshot(project, (tab({0: pane(project, agent_session())}),))
    claude = snapshot(project, (tab({2: pane(project, agent_session("claude", CLAUDE_ID))}),))
    write_snapshot(root / "session.json", snapshot(project))
    write_snapshot(root / "session-snapshots" / recovery_name(), codex)
    write_snapshot(root / "session-backups" / recovery_name(1), claude)
    current = snapshot(project, (tab({1: pane(project, agent_session()), 2: pane(project, agent_session("claude", CLAUDE_ID))}),
                                 tab({3: pane(project, agent_session())})))
    write_snapshot(root / "sessions" / "work" / "session.json", current)
    write_snapshot(root / "sessions" / "same-id" / "session.json", codex)
    write_snapshot(root / "sessions" / "work" / "session-backups" / recovery_name(2), codex)
    write_snapshot(root / "session-history.json", {"version": 3, "workspaces": [{"tabs": [{"panes": {"1": {"ansi": SENTINEL, "lines": 1}}}]}]})
    if mixed:
        write_snapshot(root / "sessions" / "resume" / "session.json", snapshot(project, (tab({1: pane(project,
            agent_resume={"source": "herdr:codex", "agent": "codex", "argv": ["codex", "resume", CODEX_ID, SENTINEL]},
            launch_argv=["codex", SENTINEL])}),)))
        write_snapshot(root / "sessions" / "future-agent" / "session.json", snapshot(project, (tab({1: pane(project, agent_session("future-agent"))}),)))
        future = {**codex, "version": 4}
        write_snapshot(root / "session-backups" / recovery_name(3), future)
        (root / "session-backups" / recovery_name(4)).write_bytes(b"{broken " + SENTINEL.encode())
        (root / "session-backups" / recovery_name(5)).write_bytes(b"\xff\xfe" + SENTINEL.encode())
    return project
