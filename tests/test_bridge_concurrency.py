"""Cross-process state contracts, using real SQLite and kernel locks."""
from __future__ import annotations

from pathlib import Path
import select
import subprocess
import sys

import pytest

from llm_tools.bridge_store import BridgeStore, MissionLock


def test_concurrent_task_submission_has_no_lost_tasks_or_events(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    with BridgeStore(path) as store:
        mid = store.create_mission("concurrent", "objective", cwd=tmp_path)["id"]
    code = '''import sys
from llm_tools.bridge_store import BridgeStore
with BridgeStore(sys.argv[1]) as store:
    for i in range(40):
        store.add_task(sys.argv[2], f"writer {sys.argv[3]} task {i}")
'''
    writers = [subprocess.Popen([sys.executable, "-c", code, str(path), mid, str(i)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(4)]
    for process in writers:
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr.decode()
    with BridgeStore(path) as store:
        tasks = store.list_tasks(mid)
        events = store.events(mid)
        assert len(tasks) == 160
        assert len({task["original_prompt"] for task in tasks}) == 160
        assert len({task["ordering"] for task in tasks}) == 160
        assert len([event for event in events if event["kind"] == "task-added"]) == 160
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_process_death_rolls_back_uncommitted_state_and_event(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    with BridgeStore(path) as store:
        mid = store.create_mission("crash", "objective", cwd=tmp_path)["id"]
    code = '''import os, sys
from llm_tools.bridge_store import BridgeStore
store = BridgeStore(sys.argv[1])
with store._transaction():
    store.db.execute("UPDATE missions SET name='uncommitted' WHERE id=?", (sys.argv[2],))
    store._event(sys.argv[2], 'uncommitted-event', {})
    os._exit(19)
'''
    process = subprocess.run([sys.executable, "-c", code, str(path), mid], timeout=10)
    assert process.returncode == 19
    with BridgeStore(path) as store:
        assert store.get_mission(mid)["name"] == "crash"
        assert [event["kind"] for event in store.events(mid)] == ["mission-created"]
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_kernel_ownership_is_released_on_controller_death(tmp_path: Path) -> None:
    directory = tmp_path / "locks"
    code = '''import sys, time
from llm_tools.bridge_store import MissionLock
lock = MissionLock(sys.argv[1], 'mission')
lock.acquire()
print('OWNED', flush=True)
time.sleep(60)
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(directory)],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 10)[0], "controller did not acquire ownership"
        assert process.stdout.readline().strip() == "OWNED"
        with pytest.raises(BlockingIOError):
            with MissionLock(directory, "mission"):
                pytest.fail("a second controller acquired the mission")
        process.kill()
        process.wait(timeout=10)
        with MissionLock(directory, "mission"):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()


@pytest.mark.parametrize("phase", ["PREPARED", "RUNNING"])
def test_committed_dispatch_survives_process_death_without_automatic_replay(tmp_path: Path, phase: str) -> None:
    path = tmp_path / "bridge.sqlite3"
    with BridgeStore(path) as store:
        mid = store.create_mission("dispatch crash", "objective", cwd=tmp_path)["id"]
        task_id = store.add_task(mid, "external operation")
        store.set_mission_status(mid, "RUNNING")
    code = '''import os, sys
from llm_tools.bridge_store import BridgeStore
store = BridgeStore(sys.argv[1])
op = store.begin_operation(int(sys.argv[2]), iteration=1, station='codex')
if sys.argv[3] == 'RUNNING':
    store.mark_operation_running(op, process_pid=os.getpid(), process_start='test-identity')
print(op, flush=True)
os._exit(23)
'''
    child = subprocess.run([sys.executable, "-c", code, str(path), str(task_id), phase],
                           text=True, capture_output=True, timeout=10)
    assert child.returncode == 23, child.stderr
    operation_id = child.stdout.strip()
    with BridgeStore(path) as store:
        assert store.get_operation(operation_id)["status"] == phase
        assert store.get_task(task_id)["status"] == "RUNNING"
        assert store.reconcile_interrupted_operations(mid) == 1
        assert store.get_operation(operation_id)["status"] == "UNCERTAIN"
        assert store.get_task(task_id)["status"] == "BLOCKED"
        assert store.ready_tasks(mid) == []
        assert store.reconcile_interrupted_operations(mid) == 0
    with BridgeStore(path) as reopened:
        assert reopened.get_task(task_id)["status"] == "BLOCKED"
        assert reopened.ready_tasks(mid) == []
