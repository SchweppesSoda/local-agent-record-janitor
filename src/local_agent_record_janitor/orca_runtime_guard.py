"""Private writer census for an approved Orca userData closure."""
from pathlib import Path
import os
import re

from .orca_journal_cleanup import fail


def related(rows, root):
    from .office_runtime import _RUNTIMES
    owned = set()
    normalized = str(root).replace("\\", "/").casefold()
    for row in rows:
        if type(row.get("pid")) is not int or type(row.get("parent")) is not int:
            fail("writer_census_unverified")
        if row["pid"] == os.getpid():
            continue
        name = str(row.get("name") or "").casefold()
        exe = str(row.get("executable") or "").replace("\\", "/").casefold()
        runtime = name in _RUNTIMES or exe.rsplit("/", 1)[-1] in _RUNTIMES
        if runtime and not isinstance(row.get("command"), str):
            fail("writer_census_unverified")
        command = str(row.get("command") or "").replace("\\", "/").casefold()
        branded = name in {"orca", "orca.exe", "orcad", "orcad.exe", "orca-daemon", "orca-daemon.exe"}
        branded |= bool(re.search(r"/(?:orca(?:\.app)?|orca-dev)/", exe))
        branded |= runtime and (normalized in command or bool(re.search(r"(?:^|[/\s])orcad(?:\.[cm]?js)?(?:\s|$)", command)))
        if branded:
            owned.add(row["pid"])
    for _ in range(len(rows) + 1):
        children = {row["pid"] for row in rows if row["parent"] in owned}
        if children <= owned:
            break
        owned.update(children)
    return owned


def require_closed(evidence, *, client_inspector=None):
    from .codex_desktop_state import running_related_clients
    from .orca_metadata import read_orca_journal
    from .orca_target_safety import inspect_orca_target_processes
    root = Path(evidence["root"])
    if client_inspector is not None and client_inspector is not running_related_clients:
        from .frontend_session_cleanup import _inspect_owner_process
        if tuple(_inspect_owner_process(client_inspector, root, owner_client="orca")):
            fail("writer_running")
        return
    from .office_runtime import processes
    rows = processes()
    declared = {item["pid"] for item in evidence["runtime_observations"] if "pid" in item}
    if related(rows, root) or any(row["pid"] in declared for row in rows):
        fail("writer_running")
    records, errors = read_orca_journal(root / "agent-session-journal.db", root)
    if errors:
        fail("writer_lease_unverified")
    report = inspect_orca_target_processes(record.lease for record in records)
    if report.get("clients_closed") is not True:
        fail("writer_lease_unverified")
