"""`mission delete`: remove a finished or never-started mission and its local state (PRD-038).

The database rows go in one transaction (``BridgeStore.delete_mission``), which also records a
tombstone that the worker replicates to the coordinator. The files follow the commit: the mission's
runtime directory (controller log, provider run logs, verifier files), any run directory outside it
that an operation referenced, the lock files and the socket and configuration file of a station
whose server is gone. A live station or a running controller makes the command refuse and change
nothing.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from .bridge_store import BridgeStore, MissionLock, TransitionError, default_state_dir, mission_delete_refusal, store_path
from .bridge_states import DELETABLE_MISSION_STATUSES

# How long to wait for a liveness probe (the console, `mission list`) that holds the mission lock for a moment.
LOCK_WAIT_SECONDS = 2.0
_RUN_DIRECTORY = re.compile(r"\d{8}-\d{6}-.+")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _remove_tree(path: Path) -> int:
    """Remove a real directory and return how many files were in it; a symlink or missing path is left alone."""
    if path.is_symlink() or not path.is_dir():
        return 0
    count = sum(1 for item in path.rglob("*") if item.is_file() or item.is_symlink())
    shutil.rmtree(path, ignore_errors=True)
    return count


def _remove_external_logs(log_paths: list[str], runtime: Path) -> int:
    """Remove run directories that held a log outside the runtime directory.

    Only a directory named like a run (``YYYYMMDD-HHMMSS-...``) is removed, so a stored path that points
    at an unrelated file is never followed.
    """
    removed = 0
    for text in log_paths:
        directory = Path(text).parent
        if _inside(directory, runtime) or not _RUN_DIRECTORY.fullmatch(directory.name):
            continue
        removed += _remove_tree(directory)
    return removed


def _remove_station_files(mission_id: str) -> bool:
    """Remove the socket and configuration file of a station whose server is gone; never touches a live one."""
    try:
        from .bridge_sockets import mission_socket_path
        from .terminal import TmuxTerminalBackend

        return bool(TmuxTerminalBackend().reclaim_dead_station(mission_socket_path(mission_id)))
    except (OSError, ValueError, RuntimeError):
        return False


def delete_mission(mission_id: str) -> dict[str, Any]:
    """Delete one mission by its full ID; raises ``TransitionError`` when it is not in a deletable state.

    The caller has resolved the reference, so a mission that is already gone is reported by the
    resolver as not found (idempotent from the operator's side).
    """
    from .bridge_missions import station_live

    state = default_state_dir()
    with BridgeStore(store_path(), readonly=True) as reader:
        before = reader.get_mission(mission_id)
    status = str(before["status"])
    if status not in DELETABLE_MISSION_STATUSES:
        raise TransitionError(mission_delete_refusal(mission_id, status))
    short = mission_id[:8]
    lock = MissionLock(state / "locks", mission_id)
    try:
        lock.acquire(timeout=LOCK_WAIT_SECONDS)
    except BlockingIOError as exc:
        raise TransitionError(
            f"mission {short} still has an active controller, so it cannot be deleted; wait for it to exit or run: llm-bridge stop --mission {short}"
        ) from exc
    try:
        if station_live(mission_id):
            raise TransitionError(
                f"mission {short} still has a live provider station, so it cannot be deleted; close it with: llm-bridge stop --mission {short} "
                "(or llm-bridge gc), then delete the mission"
            )
        with BridgeStore(store_path()) as store:
            result = store.delete_mission(mission_id)
        runtime = state / "runtime" / f"mission-{mission_id}"
        files = _remove_tree(runtime) + _remove_external_logs(result.pop("log_paths"), runtime)
        station_files = _remove_station_files(mission_id)
        for stale in (state / "locks").glob(f"mission-{mission_id}.*lock"):
            stale.unlink(missing_ok=True)
        (state / "locks" / f"mission-{mission_id}.lock").unlink(missing_ok=True)
    finally:
        lock.release()
    worktree: dict[str, Any] | None = None
    if before.get("isolation") == "worktree":
        from . import bridge_isolation

        worktree = bridge_isolation.release(before)
    return {
        "status": "DELETED",
        "mission_id": mission_id,
        "name": result["name"],
        "previous_status": result["status"],
        "deleted_at": result["deleted_at"],
        "removed": {"tasks": result["tasks"], "events": result["events"], "operations": result["operations"],
                    "files": files, "station_files": station_files},
        "detail": f"Deleted mission {short} \"{result['name']}\" ({result['tasks']} tasks, {result['events']} events, {files} files)" + _worktree_note(worktree),
        **({"worktree": worktree} if worktree is not None else {}),
    }


def _worktree_note(worktree: dict[str, Any] | None) -> str:
    """The sentence about an isolated mission's worktree: removed, or kept with where it is and why."""
    if worktree is None:
        return ""
    if worktree["removed"]:
        return f" · removed worktree {worktree['path']} and branch {worktree['branch']}"
    return (f" · kept worktree {worktree['path']} on branch {worktree['branch']}: {worktree.get('reason', 'unmerged work')}; "
            f"merge it from the repository with: git merge {worktree['branch']}; "
            f"discard it with: git worktree remove --force {worktree['path']}")
