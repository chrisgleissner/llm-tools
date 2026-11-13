"""Read-only evidence of a task's last provider run: its retained log and its workspace change.

``task output`` and ``task diff`` (CLI, typed command layer, console) share the
functions here. Nothing in this module writes to the Bridge store or to the
repository it inspects.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .bridge_states import ACTIVE_OPERATION_STATUSES
from .bridge_store import BridgeStore

SCHEMA_VERSION = 1
OUTPUT_TAIL_BYTES = 256 * 1024
DIFF_MAX_BYTES = 1024 * 1024
ACTIVITY_LINE_CHARS = 160
ACTIVITY_TAIL_BYTES = 16 * 1024
CHANGED_FILES_RECORDED = 6  # the console projection keeps at most six list items per event field
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_GIT_TIMEOUT_SECONDS = 15
_CHECKPOINT_EVENT_WINDOW = 200


def _latest_operation(store: BridgeStore, task_id: int) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM operations WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
    return None if row is None else dict(row)


def _event_data(store: BridgeStore, mission_id: str, kind: str, *, task_id: int) -> dict[str, Any] | None:
    """The newest event of ``kind`` for a task (these events carry the task inside the payload)."""
    rows = store.db.execute(
        "SELECT data_json FROM events WHERE mission_id=? AND kind=? ORDER BY id DESC LIMIT ?",
        (mission_id, kind, _CHECKPOINT_EVENT_WINDOW),
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row[0])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("task_id") != task_id:
            continue
        return data
    return None


def _pruned_log(store: BridgeStore, operation_id: str) -> dict[str, Any] | None:
    row = store.db.execute(
        "SELECT data_json FROM events WHERE operation_id=? AND kind='operation-log-pruned' ORDER BY id DESC LIMIT 1", (operation_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _unavailable(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "available": False, "reason": reason, "message": message, **extra}


def _run_facts(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": operation["id"],
        "provider": operation.get("route_id") or operation.get("station"),
        "status": operation.get("status"),
        "exit_code": operation.get("exit_code"),
    }


def locate_output(store: BridgeStore, mission_id: str, task: dict[str, Any]) -> dict[str, Any]:
    """Where the retained log of the task's latest operation is, or why there is none. Reads no log text."""
    number = task["ordinal"]
    operation = _latest_operation(store, int(task["id"]))
    if operation is None:
        return _unavailable("not-run", f"task {number} has not run; there is no provider output yet", task=number)
    facts = {"task": number, **_run_facts(operation)}
    path = operation.get("logs_path")
    if not path:
        pruned = _pruned_log(store, str(operation["id"]))
        if pruned is not None:
            size, digest = pruned.get("size_bytes"), str(pruned.get("sha256") or "unknown")
            return _unavailable(
                "pruned",
                f"the provider log of task {number}'s last run was pruned ({size} bytes, sha256 {digest[:16]}). "
                "Bridge keeps only the size and digest of a pruned log; raise the number of kept runs with mission create --log-retained-runs",
                size_bytes=size, sha256=digest, **facts)
        if operation.get("status") in ACTIVE_OPERATION_STATUSES:
            return _unavailable("running", f"task {number} is still running and its provider has not written a log yet; try again shortly", **facts)
        return _unavailable("no-log", f"no log was recorded for task {number}'s last run (operation {str(operation['id'])[:8]}, {operation.get('status')})", **facts)
    if not Path(str(path)).is_file():
        return _unavailable("missing", f"the log of task {number}'s last run is no longer on disk: {path}", path=str(path), **facts)
    return {"schema_version": SCHEMA_VERSION, "available": True, "reason": None, "message": "", "path": str(path), **facts}


def task_output(store: BridgeStore, mission_id: str, task: dict[str, Any], *, tail_bytes: int = OUTPUT_TAIL_BYTES) -> dict[str, Any]:
    """``locate_output`` plus the end of the log text (at most ``tail_bytes``)."""
    found = locate_output(store, mission_id, task)
    if not found["available"]:
        return found
    try:
        with open(found["path"], "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(max(0, size - tail_bytes))
            data = stream.read(tail_bytes)
    except OSError as exc:
        return _unavailable("unreadable", f"the log of task {task['ordinal']}'s last run cannot be read: {exc}", **{k: v for k, v in found.items() if k not in {"available", "reason", "message", "schema_version"}})
    truncated = size > len(data)
    text = data.decode("utf-8", "replace")
    if truncated:
        text = f"[showing the last {len(data)} of {size} bytes; the full log is {found['path']}]\n" + text
    return {**found, "size_bytes": size, "truncated": truncated, "text": text}


# ---------------------------------------------------------------- activity line

_ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?<>=-]*[ -/]*[@-~]|\x1b[()][\x20-\x7e]|\x1b[@-Z\\^_<>=78]")


def _activity_unavailable(reason: str, message: str) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "available": False, "reason": reason, "message": message,
            "line": None, "at": None, "task": None, "provider": None}


def last_log_line(data: bytes) -> str | None:
    """The last line of ``data`` that has a letter or digit, as one plain line of at most ``ACTIVITY_LINE_CHARS``.

    Terminal escape sequences are removed, every control, format and surrogate character is replaced by a
    space or dropped, and white space is collapsed. A line made only of box-drawing characters, spinners
    or punctuation (a redrawn terminal screen is mostly that) is skipped.
    """
    from .bridge_format import printable  # imported here: bridge_format is only needed once a log has text

    text = _ESCAPES.sub("", data.decode("utf-8", "replace")).replace("\r\n", "\n").replace("\r", "\n")
    for raw in reversed(text.split("\n")):
        line = " ".join(printable(raw).split())
        if any(char.isalnum() for char in line):
            return line if len(line) <= ACTIVITY_LINE_CHARS else line[: ACTIVITY_LINE_CHARS - 1] + "\u2026"
    return None


def activity_line(store: BridgeStore, mission_id: str) -> dict[str, Any]:
    """One bounded line of what the provider of the mission's running operation printed last, and when.

    The line comes only from the operation's retained log, the file ``task output`` already returns in
    full to the same callers; the provider's terminal screen is never captured. At most the last
    ``ACTIVITY_TAIL_BYTES`` of the log are read and at most ``ACTIVITY_LINE_CHARS`` characters returned.
    ``at`` is the log file's modification time (UTC), the moment the provider last wrote.
    """
    from .fileio import open_regular_file

    row = store.db.execute(
        f"SELECT * FROM operations WHERE mission_id=? AND status IN ({','.join('?' * len(ACTIVE_OPERATION_STATUSES))}) ORDER BY rowid DESC LIMIT 1",
        (mission_id, *sorted(ACTIVE_OPERATION_STATUSES)),
    ).fetchone()
    if row is None:
        return _activity_unavailable("not-running", "no provider run is active in this mission")
    operation = dict(row)
    path = operation.get("logs_path")
    if not path:
        return _activity_unavailable("no-log", "the provider has not written a log yet")
    try:
        with open_regular_file(str(path)) as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(max(0, size - ACTIVITY_TAIL_BYTES))
            data = stream.read(ACTIVITY_TAIL_BYTES)
            modified = os.fstat(stream.fileno()).st_mtime
    except OSError:
        return _activity_unavailable("unreadable", "the provider log cannot be read")
    line = last_log_line(data)
    if line is None:
        return _activity_unavailable("no-output", "the provider has not printed a line of text yet")
    task = store.db.execute("SELECT ordinal FROM tasks WHERE id=?", (operation["task_id"],)).fetchone()
    return {
        "schema_version": SCHEMA_VERSION, "available": True, "reason": None, "message": "", "line": line,
        "at": datetime.fromtimestamp(modified, timezone.utc).isoformat(timespec="seconds"),
        "task": None if task is None else task[0],
        "provider": operation.get("route_id") or operation.get("station"),
    }


# ---------------------------------------------------------------- workspace change


def _git(root: Path, *args: str, limit: int | None = None) -> tuple[bytes, bool] | None:
    """Output of a read-only git command and whether it was cut at ``limit`` bytes; None when it failed."""
    import tempfile

    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
                stdout=output, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=_GIT_TIMEOUT_SECONDS, check=False,
            )
            output.seek(0)
            data = output.read(limit + 1) if limit is not None else output.read()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    if limit is not None and len(data) > limit:
        return data[:limit], True
    return data, False


def _names(raw: bytes) -> list[str]:
    return [os.fsdecode(item) for item in raw.split(b"\0") if item]


def workspace_change(root: Path, checkpoint: dict[str, Any], *, with_diff: bool) -> dict[str, Any] | None:
    """Files changed since ``checkpoint``'s commit, the untracked ones and optionally the diff text; None when git cannot say."""
    head = checkpoint.get("head")
    if not isinstance(head, str) or not head:
        return None
    base = _EMPTY_TREE if head == "unborn" else head
    names = _git(root, "diff", "--name-only", "-z", "--no-ext-diff", base)
    others = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if names is None or others is None:
        return None
    dirty = {str(item) for item in checkpoint.get("dirty") or []}
    files = _names(names[0])
    untracked = [name for name in _names(others[0]) if name not in dirty]
    result: dict[str, Any] = {"files": files, "untracked": untracked, "already_modified": sorted(dirty & set(files))}
    if with_diff:
        diff = _git(root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", base, limit=DIFF_MAX_BYTES)
        if diff is None:
            return None
        result["diff"] = diff[0].decode("utf-8", "replace")
        result["truncated"] = diff[1]
    return result


def recorded_change(root: str | Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """The few fields of the workspace change stored with a finished run, for the approval dialog; empty when unknown."""
    try:
        change = workspace_change(Path(root), checkpoint, with_diff=False)
    except (OSError, ValueError):
        change = None
    if change is None:
        return {}
    names = [*change["files"], *change["untracked"]]
    recorded: dict[str, Any] = {"changed_files": [name[:120] for name in names[:CHANGED_FILES_RECORDED]], "changed_total": len(names)}
    if change["already_modified"]:
        recorded["already_modified"] = len(change["already_modified"])
    return recorded


def task_diff(store: BridgeStore, mission_id: str, task: dict[str, Any]) -> dict[str, Any]:
    """The workspace change since the checkpoint recorded when the task's last run started."""
    number = task["ordinal"]
    mission = store.get_mission(mission_id)
    operation = _latest_operation(store, int(task["id"]))
    event = _event_data(store, mission_id, "repository-checkpoint", task_id=int(task["id"]))
    checkpoint = event.get("checkpoint") if isinstance(event, dict) else None
    if not isinstance(checkpoint, dict):
        suffix = "" if operation is not None else "; the task has not run"
        return _unavailable("no-checkpoint", f"no repository checkpoint was recorded for task {number}{suffix}", task=number)
    folder = str(checkpoint.get("root") or mission["cwd"])
    if checkpoint.get("reason") == "not a Git worktree":
        return _unavailable("not-git", f"task {number} ran in a folder that is not a git repository: {folder}; there is no diff to show", task=number)
    if not checkpoint.get("head"):
        return _unavailable("no-base", f"the repository checkpoint of task {number}'s last run records no starting commit (it predates task diff, or git could not be read)", task=number)
    root = Path(folder)
    if not root.is_dir():
        return _unavailable("missing-folder", f"the folder task {number} ran in no longer exists: {folder}", task=number)
    change = workspace_change(root, checkpoint, with_diff=True)
    if change is None:
        return _unavailable("git-failed", f"git could not compare {folder} with the checkpoint of task {number}'s last run (the starting commit may be gone)", task=number)
    head = str(checkpoint["head"])
    lines = [f"Changes since the start of the last run of task {number}: git diff {head[:12]} to the working tree in {folder}"]
    if change["already_modified"]:
        lines.append(f"Files already modified before the run started (their earlier edits are included): {', '.join(change['already_modified'])}")
    body = str(change["diff"])
    if not body and not change["untracked"]:
        text = f"No changes since the start of the last run of task {number}."
    else:
        lines.append("")
        if body:
            lines.append(body.rstrip("\n"))
        if change["untracked"]:
            lines.append(f"Untracked files ({len(change['untracked'])}): {', '.join(change['untracked'])}")
        if change["truncated"]:
            lines.append(f"[diff cut at {DIFF_MAX_BYTES} bytes; run git diff {head[:12]} for the rest]")
        text = "\n".join(lines) + "\n"
    return {
        "schema_version": SCHEMA_VERSION,
        "available": True,
        "reason": None,
        "message": "",
        "task": number,
        "base": head,
        "root": folder,
        "files": change["files"],
        "untracked": change["untracked"],
        "already_modified": change["already_modified"],
        "truncated": change["truncated"],
        "text": text,
    }
