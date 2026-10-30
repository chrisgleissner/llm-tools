"""Process inspection follows a process tree instead of reading all of /proc, and polling backs off."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_tools import bridge_runtime, bridge_sessions
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_sessions import NativeSessionIdentity
from llm_tools.bridge_store import BridgeStore


@pytest.fixture()
def process_tree():
    """A parent that has one child and one grandchild, each in its own process."""
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(60)\"]); time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
    child_pid = int(parent.stdout.readline())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(bridge_sessions.child_pids(child_pid) or []) < 1:
        time.sleep(0.05)
    yield parent, child_pid
    try:
        os.killpg(parent.pid, 9)
    except ProcessLookupError:
        pass
    parent.wait(timeout=5)


def test_boot_id_is_read_once(monkeypatch) -> None:
    reads = []
    real_read_text = Path.read_text

    def counting(path, *args, **kwargs):
        if str(path).endswith("boot_id"):
            reads.append(1)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(bridge_sessions, "_BOOT_ID", "")
    monkeypatch.setattr(Path, "read_text", counting)
    for _ in range(5):
        assert bridge_sessions._proc_stat(os.getpid()) is not None
        assert bridge_runtime._proc_process_info(os.getpid()) is not None
    assert len(reads) == 1


def test_child_pids_lists_direct_children_only(process_tree) -> None:
    parent, child_pid = process_tree
    assert bridge_sessions.child_pids(parent.pid) == [child_pid]
    grandchildren = bridge_sessions.child_pids(child_pid)
    assert grandchildren is not None and len(grandchildren) == 1
    assert bridge_sessions.child_pids(2**22 + 12345) == []  # no such process: no children


def test_owned_processes_follow_children_without_scanning_proc(process_tree, monkeypatch) -> None:
    parent, child_pid = process_tree
    token = bridge_sessions._proc_stat(parent.pid)[3]

    def forbidden(_path):
        raise AssertionError("must not scan /proc")

    monkeypatch.setattr(bridge_sessions.os, "scandir", forbidden)
    owned = bridge_sessions._owned_processes(parent.pid, token)
    assert owned is not None and len(owned) == 3 and parent.pid in owned and child_pid in owned
    assert bridge_sessions._owned_processes(parent.pid, "other-boot:1") is None  # PID reuse: identity mismatch


def test_owned_processes_scan_proc_when_child_lists_are_unavailable(process_tree, monkeypatch) -> None:
    parent, child_pid = process_tree
    token = bridge_sessions._proc_stat(parent.pid)[3]
    monkeypatch.setattr(bridge_sessions, "child_pids", lambda _pid: None)

    owned = bridge_sessions._owned_processes(parent.pid, token)
    assert owned is not None and parent.pid in owned and child_pid in owned and len(owned) == 3


def test_current_owned_tree_matches_the_full_scan_and_refuses_a_reused_root(process_tree, monkeypatch) -> None:
    parent, _child = process_tree
    token = bridge_runtime._process_start_token(parent.pid)
    scanned = bridge_runtime._owned_process_tree(parent.pid, token)
    followed = bridge_runtime._current_owned_tree(parent.pid, token)
    assert set(followed) == set(scanned) and len(followed) == 3

    def forbidden(_path):
        raise AssertionError("must not scan /proc")

    monkeypatch.setattr(bridge_runtime.os, "scandir", forbidden)
    assert set(bridge_runtime._current_owned_tree(parent.pid, token)) == set(scanned)
    with pytest.raises(PermissionError, match="no longer matches"):
        bridge_runtime._current_owned_tree(parent.pid, "reused-token")


def test_current_owned_tree_falls_back_to_the_scan_for_an_unknown_root(monkeypatch) -> None:
    monkeypatch.setattr(bridge_runtime, "_owned_process_tree", lambda pid, start: {"scanned": pid})
    assert bridge_runtime._current_owned_tree(2**22 + 999, "token") == {"scanned": 2**22 + 999}


def test_process_group_absent_is_one_syscall_and_distinguishes_live_groups(process_tree) -> None:
    parent, _child = process_tree
    assert not bridge_runtime._process_group_absent(parent.pid)
    assert bridge_runtime._process_group_absent(2**22 + 4321)


def test_terminating_a_provider_tree_with_children_stops_every_member(tmp_path) -> None:
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", code], cwd=tmp_path, start_new_session=True)
    try:
        token = ""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (not token or not bridge_sessions.child_pids(process.pid)):
            token = bridge_runtime._process_start_token(process.pid)
            time.sleep(0.02)
        children = bridge_sessions.child_pids(process.pid)
        assert token and children
        bridge_runtime._terminate_owned_process_group(process.pid, token, timeout=2.0)
        assert process.wait(timeout=3) < 0
        deadline = time.monotonic() + 3
        while bridge_runtime._process_start_token(children[0]) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not bridge_runtime._process_start_token(children[0])
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=3)


def _hooks_with_codex_operation(tmp_path):
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    store.set_mission_status(mission["id"], "RUNNING")
    task_id = store.add_task(mission["id"], "work")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation_id, pane_id="%1", route_id="", model="")
    store.record_operation_process(operation_id, process_pid=os.getpid(), process_start=bridge_runtime._process_start_token(os.getpid()))
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    hooks._codex_owned_operations.add(operation_id)
    hooks._session_cwds[operation_id] = str(tmp_path)
    return hooks, store, operation_id


def test_codex_session_observer_backs_off_then_stops_when_the_session_is_found(tmp_path, monkeypatch) -> None:
    hooks, store, operation_id = _hooks_with_codex_operation(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(bridge_runtime.time, "monotonic", lambda: clock[0])
    searches: list[float] = []
    found: list[NativeSessionIdentity | None] = [None]

    def discover(pid, token, *, cwd):
        searches.append(clock[0])
        return found[0]

    monkeypatch.setattr(bridge_runtime, "discover_owned_codex_session", discover)

    for _ in range(80):  # eighty seconds of the controller's one-second wait loop
        hooks._observe_owned_codex_session(operation_id)
        clock[0] += 1.0
    gaps = [round(b - a) for a, b in zip(searches, searches[1:])]
    assert gaps[:4] == [1, 2, 5, 15] and set(gaps[3:]) == {15}
    assert len(searches) <= 8  # eighty polls became a handful of /proc inspections

    before = len(searches)
    found[0] = NativeSessionIdentity("codex", "11111111-2222-3333-4444-555555555555", str(tmp_path), "codex:session_meta")
    clock[0] += 60
    hooks._observe_owned_codex_session(operation_id)
    assert len(searches) == before + 1
    assert store.get_operation(operation_id)["restoration_state"] == "NATIVE_CREATED"
    for _ in range(20):
        clock[0] += 30
        hooks._observe_owned_codex_session(operation_id)
    assert len(searches) == before + 1  # found: never looked for again


def test_recording_the_process_searches_at_once_even_inside_a_backoff_interval(tmp_path, monkeypatch) -> None:
    hooks, _store, operation_id = _hooks_with_codex_operation(tmp_path)
    searches = []
    monkeypatch.setattr(bridge_runtime, "discover_owned_codex_session", lambda pid, token, *, cwd: searches.append(1))
    hooks._observe_owned_codex_session(operation_id)
    hooks._observe_owned_codex_session(operation_id)
    assert len(searches) == 1
    hooks._record_process(operation_id, os.getpid())
    assert len(searches) == 2


def test_finishing_an_operation_forgets_its_observer_schedule(tmp_path, monkeypatch) -> None:
    hooks, _store, operation_id = _hooks_with_codex_operation(tmp_path)
    monkeypatch.setattr(bridge_runtime, "discover_owned_codex_session", lambda pid, token, *, cwd: None)
    hooks._observe_owned_codex_session(operation_id)
    assert operation_id in hooks._codex_observe_schedule
    hooks._record_session_outcome(operation_id, "codex", str(tmp_path))
    assert operation_id not in hooks._codex_observe_schedule
