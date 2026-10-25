"""Crash-safe mission state and task transition contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llm_tools.bridge_store import BridgeStore, MissionLock, TransitionError


def test_task_order_dependencies_and_boundary_amendment_are_durable(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("bridge", "Implement it", cwd=tmp_path)
    first = store.add_task(mission["id"], "first task", priority=2, order=10)
    second = store.add_task(mission["id"], "second task", priority=9, order=20, depends_on=[first])
    parallel = store.add_task(mission["id"], "parallel", priority=2, order=5)

    assert [task["id"] for task in store.ready_tasks(mission["id"])] == [parallel, first]
    revision = store.amend_task(first, prompt="replacement objective")
    assert revision == 2
    assert store.get_task(first)["prompt"] == "first task"
    started = store.begin_iteration(first)
    assert started["prompt"] == "replacement objective"
    assert started["revision"] == 2

    store.close()
    reopened = BridgeStore(tmp_path / "bridge.sqlite3")
    assert reopened.get_task(first)["prompt"] == "replacement objective"
    assert reopened.get_task(first)["original_prompt"] == "first task"
    assert [task["id"] for task in reopened.ready_tasks(mission["id"])] == [parallel, first]
    with pytest.raises(TransitionError, match="dependency"):
        reopened.add_dependency(mission["id"], first, second)


def test_operation_intent_precedes_launch_and_crash_becomes_uncertain(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "do something")

    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    intent = store.get_operation(operation_id)
    assert intent["status"] == "PREPARED"
    assert store.get_task(task_id)["status"] == "RUNNING"
    assert store.reconcile_interrupted_operations(mission["id"]) == 1
    assert store.get_operation(operation_id)["status"] == "UNCERTAIN"
    assert store.get_task(task_id)["status"] == "BLOCKED"
    assert store.ready_tasks(mission["id"]) == []


def test_success_requires_explicit_verification_and_dependencies_unlock_afterwards(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    first = store.add_task(mission["id"], "write code", acceptance=["tests pass"])
    second = store.add_task(mission["id"], "integrate", depends_on=[first])
    op = store.begin_operation(first, iteration=1, station="claude")

    result = store.finish_operation(op, exit_code=0, logs_path="run.out")
    assert result["status"] == "VERIFYING"
    assert store.ready_tasks(mission["id"]) == []
    with pytest.raises(TransitionError, match="evidence"):
        store.verify_task(first)
    store.verify_task(first, evidence="pytest -q: 12 passed")
    assert [task["id"] for task in store.ready_tasks(mission["id"])] == [second]


def test_nonzero_and_autonomy_abort_are_not_marked_complete(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    failed_id = store.add_task(mission["id"], "fails")
    failed_op = store.begin_operation(failed_id, iteration=1, station="codex")
    assert store.finish_operation(failed_op, exit_code=2)["status"] == "FAILED"

    blocked_id = store.add_task(mission["id"], "stalls")
    blocked_op = store.begin_operation(blocked_id, iteration=1, station="claude")
    assert store.finish_operation(blocked_op, exit_code=75)["status"] == "BLOCKED"


def test_events_share_transaction_and_mission_lock_is_exclusive(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "task")
    assert [event["kind"] for event in store.events(mission["id"])] == ["mission-created", "task-added"]

    lock_a = MissionLock(tmp_path / "locks", mission["id"])
    lock_a.acquire()
    lock_b = MissionLock(tmp_path / "locks", mission["id"])
    with pytest.raises(BlockingIOError):
        lock_b.acquire()
    lock_a.release()
    lock_b.acquire()
    lock_b.release()

    with sqlite3.connect(tmp_path / "bridge.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_operation_identity_commands_and_worktree_exclusion(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task = store.add_task(mission["id"], "task")
    op_id = store.begin_operation(task, iteration=1, station="kilo")
    store.mark_operation_running(op_id, pane_id="%0", process_pid=123, process_start="boot:1", route_id="kilo-coder", model="vendor/model")
    op = store.get_operation(op_id)
    assert (op["status"], op["pane_id"], op["process_pid"], op["route_id"], op["model"]) == ("RUNNING", "%0", 123, "kilo-coder", "vendor/model")
    with pytest.raises(TransitionError, match="unresolved operation"):
        store.begin_operation(task, iteration=2, station="kilo")

    command = store.enqueue_command(mission["id"], {"action": "pause"}, command_id="request-1")
    duplicate = store.enqueue_command(mission["id"], {"action": "pause"}, command_id="request-1")
    assert duplicate["id"] == command["id"]
    assert store.pending_commands(mission["id"])[0]["payload"] == {"action": "pause"}
    assert store.finish_command("request-1", status="APPLIED", result={"status": "PAUSED"})["result"] == {"status": "PAUSED"}
    with pytest.raises(TransitionError, match="different request"):
        store.enqueue_command(mission["id"], {"action": "stop"}, command_id="request-1")

    store.set_mission_status(mission["id"], "RUNNING")
    with pytest.raises(TransitionError, match="already owns worktree"):
        store.create_mission("duplicate", "another objective", cwd=tmp_path)


def test_private_state_directory_is_required_and_is_not_chmodded(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(PermissionError, match="private"):
        BridgeStore(shared / "bridge.sqlite3")
    assert shared.stat().st_mode & 0o777 == 0o755


def test_completion_requires_evidence_and_all_tasks_verified(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task = store.add_task(mission["id"], "one task")
    op = store.begin_operation(task, iteration=1, station="codex")
    store.finish_operation(op, exit_code=0)
    store.set_mission_status(mission["id"], "RUNNING")
    with pytest.raises(TransitionError, match="invalid task transition"):
        store.set_task_status(task, "COMPLETED")
    with pytest.raises(TransitionError, match="tasks are not complete"):
        store.set_mission_status(mission["id"], "COMPLETED")


def test_amendment_during_operation_invalidates_that_operation_success(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task = store.add_task(mission["id"], "original")
    op = store.begin_operation(task, iteration=1, station="codex")
    store.amend_task(task, prompt="new revision")
    result = store.finish_operation(op, exit_code=0)
    assert result["status"] == "READY"
    assert result["prompt"] == "new revision"
    assert result["pending"] == {}
    with pytest.raises(TransitionError, match="not awaiting verification"):
        store.verify_task(task, approved=True)


def test_ready_transitions_are_evented_and_new_blocking_dependency_demotes_task(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task = store.add_task(mission["id"], "task")
    dependency = store.add_task(mission["id"], "dependency")
    assert store.ready_tasks(mission["id"])[0]["id"] == task
    assert any(event["kind"] == "task-ready" and event["task_id"] == task for event in store.events(mission["id"]))
    store.add_dependency(mission["id"], task, dependency)
    assert store.get_task(task)["status"] == "QUEUED"
    assert task not in [entry["id"] for entry in store.ready_tasks(mission["id"])]


def test_worktree_ownership_covers_nested_paths(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    root = store.create_mission("root", "objective", cwd=tmp_path)
    store.set_mission_status(root["id"], "RUNNING")
    nested = tmp_path / "nested"
    nested.mkdir()
    with pytest.raises(TransitionError, match="already owns worktree"):
        store.create_mission("nested", "another objective", cwd=nested)


def test_transaction_rolls_back_keyboard_interrupt_and_wal_files_are_private(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    with pytest.raises(KeyboardInterrupt):
        with store._transaction():
            store.db.execute("UPDATE missions SET name='partial' WHERE id=?", (mission["id"],))
            raise KeyboardInterrupt()
    assert store.get_mission(mission["id"])["name"] == "mission"
    task = store.add_task(mission["id"], "after interrupt")
    assert store.get_task(task)["prompt"] == "after interrupt"
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(store.path) + suffix)
        if path.exists():
            assert path.stat().st_mode & 0o777 == 0o600


def test_version_one_database_migrates_original_prompts_and_new_tables(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE missions(id TEXT PRIMARY KEY,name TEXT NOT NULL,objective TEXT NOT NULL,cwd TEXT NOT NULL,repo_identity TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,mission_id TEXT NOT NULL,title TEXT NOT NULL,prompt TEXT NOT NULL,pending_json TEXT NOT NULL DEFAULT '{}',priority INTEGER NOT NULL DEFAULT 0,ordering INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'QUEUED',revision INTEGER NOT NULL DEFAULT 1,acceptance_json TEXT NOT NULL DEFAULT '[]',evidence_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE task_dependencies(task_id INTEGER NOT NULL,depends_on INTEGER NOT NULL,PRIMARY KEY(task_id,depends_on));
            CREATE TABLE operations(id TEXT PRIMARY KEY,mission_id TEXT NOT NULL,task_id INTEGER NOT NULL,iteration INTEGER NOT NULL,station TEXT NOT NULL,status TEXT NOT NULL,exit_code INTEGER,logs_path TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,mission_id TEXT NOT NULL,task_id INTEGER,operation_id TEXT,kind TEXT NOT NULL,data_json TEXT NOT NULL,created_at TEXT NOT NULL);
            PRAGMA user_version=1;
            """
        )
        db.execute("INSERT INTO missions VALUES('old','old','objective',?,'/tmp','CREATED','now','now')", (str(tmp_path),))
        db.execute("INSERT INTO tasks(mission_id,title,prompt,created_at,updated_at) VALUES('old','old task','verbatim old prompt','now','now')")
    store = BridgeStore(path)
    assert store.get_task(1)["original_prompt"] == "verbatim old prompt"
    assert store.get_mission("old")["config"] == {}
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2

