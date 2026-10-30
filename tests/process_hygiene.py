"""Child-process hygiene for the test suite.

A test must not leave a live process behind. Orphaned tmux servers, controllers and PTY
children keep running (and, for a curses loop whose terminal is gone, keep a CPU busy) long
after the suite has finished.

A process is attributed to a test only by facts about that test: its PID did not exist when
the test started, and it is either a direct child of the pytest process or references one of
the test's private directories (its ``tmp_path`` or private runtime directory) in its
environment, working directory or command line. Nothing is matched or killed by program name.

Teardown can run while a test's own patches (``time.sleep``, ``os.listdir``, ``subprocess``)
are still active, so every primitive used here is captured at import.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from typing import Any

_listdir = os.listdir
_kill = os.kill
_killpg = os.killpg
_getpgid = os.getpgid
_waitpid = os.waitpid
_readlink = os.readlink
_sleep = time.sleep
_monotonic = time.monotonic
_open = open

OWNER_FILE = ".owner"
STALE_DIR_SECONDS = 30 * 60  # an ownerless runtime dir this old belongs to a session that no longer runs
_DEAD = {"Z", "X"}


def timeout_scale() -> float:
    try:
        value = float(os.environ.get("TEST_TIMEOUT_SCALE", "1"))
    except ValueError:
        return 1.0
    return value if value > 0 else 1.0


def _read(path: str) -> bytes:
    with _open(path, "rb") as stream:
        return stream.read()


def live_pids() -> set[int]:
    try:
        return {int(entry) for entry in _listdir("/proc") if entry.isdigit()}
    except OSError:
        return set()


def proc_stat(pid: int) -> tuple[str, int, str] | None:
    """``(state, ppid, starttime)`` from /proc/<pid>/stat, or ``None`` when the process is gone."""
    try:
        raw = _read(f"/proc/{pid}/stat").decode("ascii", "replace")
    except OSError:
        return None
    fields = raw[raw.rfind(")") + 2:].split()
    try:
        return fields[0], int(fields[1]), fields[19]
    except (IndexError, ValueError):
        return None


def is_alive(pid: int) -> bool:
    info = proc_stat(pid)
    return info is not None and info[0] not in _DEAD


def proc_command(pid: int) -> str:
    try:
        return _read(f"/proc/{pid}/cmdline").replace(b"\0", b" ").decode("utf-8", "replace").strip()[:160]
    except OSError:
        return "?"


def path_patterns(paths: list[str]) -> list[re.Pattern[str]]:
    # "test_x1" must not match inside "test_x10": the path may only be followed by a non-name character.
    return [re.compile(re.escape(path) + r"(?![A-Za-z0-9_])") for path in paths if path]


def references(pid: int, patterns: list[re.Pattern[str]]) -> bool:
    try:
        blob = _read(f"/proc/{pid}/environ").decode("utf-8", "replace")
        blob += "\0" + _read(f"/proc/{pid}/cmdline").decode("utf-8", "replace")
        blob += "\0" + _readlink(f"/proc/{pid}/cwd")
    except OSError:
        return False
    return any(pattern.search(blob) for pattern in patterns)


def live_candidates(before: set[int], patterns: list[re.Pattern[str]]) -> dict[int, str]:
    """Live processes that appeared after ``before`` and belong to the test."""
    me = os.getpid()
    found: dict[int, str] = {}
    for pid in live_pids() - before - {me}:
        info = proc_stat(pid)
        if info is None or info[0] in _DEAD:
            continue  # exited; a zombie only waits for its parent to reap it
        if info[1] == me or references(pid, patterns):
            found[pid] = proc_command(pid)
    return found


def _signal(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            _kill(pid, sig)
        except OSError:
            pass


def _reap(pids: list[int]) -> None:
    for pid in pids:
        try:
            _waitpid(pid, os.WNOHANG)
        except OSError:
            pass  # not our child, or already reaped


def terminate_processes(pids: list[int], *, grace: float = 1.0) -> list[int]:
    """SIGTERM, then SIGKILL after ``grace`` seconds, then reap direct children.

    Returns the PIDs that needed SIGKILL.
    """
    _signal(pids, signal.SIGTERM)
    deadline = _monotonic() + grace
    alive = list(pids)
    while alive and _monotonic() < deadline:
        _reap(alive)
        alive = [pid for pid in alive if is_alive(pid)]
        if alive:
            _sleep(0.02)
    if alive:
        _signal(alive, signal.SIGKILL)
        _sleep(0.05)
        _reap(alive)
    return alive


def terminate_process_tree(process: subprocess.Popen[Any], *, grace: float = 2.0) -> None:
    """Stop a test-started child and its process group on every exit path, and reap it.

    The child should be started with ``start_new_session=True`` so its group holds only its
    own descendants. SIGTERM goes to the group, SIGKILL follows after ``grace`` seconds.
    """
    group = process.pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if process.poll() is not None:
            break
        try:
            if _getpgid(process.pid) == group:
                _killpg(group, sig)
            else:
                process.send_signal(sig)
        except OSError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
    if process.poll() is None:
        process.wait(timeout=grace)
    try:
        _killpg(group, signal.SIGKILL)  # descendants that outlived the leader
    except OSError:
        pass


def reap_leaks(before: set[int], directories: list[str]) -> list[str]:
    """Stop processes the test left running and describe them (empty when it left none)."""
    patterns = path_patterns(directories)
    scale = timeout_scale()
    deadline = _monotonic() + 2.0 * scale
    leaked = live_candidates(before, patterns)
    while leaked and _monotonic() < deadline:
        _sleep(0.05)  # a process that was told to exit may still be going away
        leaked = live_candidates(before, patterns)
    if not leaked:
        return []
    survivors = terminate_processes(list(leaked), grace=1.0 * scale)
    return [f"pid {pid}{' (needed SIGKILL)' if pid in survivors else ''}: {command}" for pid, command in leaked.items()]


def start_token(pid: int) -> str:
    info = proc_stat(pid)
    return info[2] if info and info[0] not in _DEAD else ""


def write_owner(directory: str) -> None:
    """Record the owning pytest process in a private runtime directory."""
    with _open(os.path.join(directory, OWNER_FILE), "w", encoding="ascii") as stream:
        stream.write(f"{os.getpid()} {start_token(os.getpid())}\n")


def stale_runtime_dirs(parent: str = "/tmp") -> list[str]:
    """Private runtime directories whose owning test session no longer exists."""
    stale: list[str] = []
    try:
        names = [name for name in _listdir(parent) if name.startswith("lbt-")]
    except OSError:
        return stale
    now = time.time()
    for name in names:
        directory = os.path.join(parent, name)
        try:
            if os.stat(directory).st_uid != os.getuid():
                continue
            try:
                owner = _read(os.path.join(directory, OWNER_FILE)).decode("ascii").split()
                if len(owner) == 2 and start_token(int(owner[0])) == owner[1]:
                    continue  # its session is still running
                stale.append(directory)
            except (OSError, ValueError):
                # No usable owner file: an older harness or a session that died before writing it.
                if now - os.stat(directory).st_mtime >= STALE_DIR_SECONDS:
                    stale.append(directory)
        except OSError:
            continue
    return stale


def sweep_stale_runtime_dirs(parent: str = "/tmp") -> list[str]:
    """Stop processes left by dead sessions (tmux servers, controllers) and report the directories.

    Anything still referencing a stale directory was started by a test of a session that can
    no longer clean it up. Returns the directories that held live processes; the caller
    removes the directories.
    """
    me = os.getpid()
    held: list[str] = []
    for directory in stale_runtime_dirs(parent):
        patterns = path_patterns([directory])
        users = [pid for pid in live_pids() - {me} if is_alive(pid) and references(pid, patterns)]
        if users:
            terminate_processes(users, grace=1.0)
            held.append(directory)
    return held
