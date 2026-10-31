"""Crash-safe mission state and task transition contracts."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from llm_tools.bridge_store import SCHEMA_VERSION, BridgeStore, MissionLock, TransitionError


def _activate(store: BridgeStore, mission: dict[str, object]) -> None:
    store.set_mission_status(str(mission["id"]), "RUNNING")


def test_task_order_dependencies_and_boundary_amendment_are_durable(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("bridge", "Implement it", cwd=tmp_path)
    first = store.add_task(mission["id"], "first task", priority=2, order=10)
    second = store.add_task(mission["id"], "second task", priority=9, order=20, depends_on=[first])
    parallel = store.add_task(mission["id"], "parallel", priority=2, order=5)

    assert [task["id"] for task in store.ready_tasks(mission["id"])] == [parallel, first]
    revision = store.amend_task(first, prompt="replacement objective")
    assert revision == 2
    # No provider step uses the old text, so the amendment applies at once.
    assert store.get_task(first)["prompt"] == "replacement objective"
    assert store.get_task(first)["pending"] == {}
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
    _activate(store, mission)
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
    _activate(store, mission)
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
    _activate(store, mission)
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
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_current_schema_open_preserves_identity_for_removed_worktree(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    live = tmp_path / "live"
    live.mkdir()
    store = BridgeStore(path)
    mission = store.create_mission("historical", "objective", cwd=live)
    expected_identity = mission["repo_identity"]
    store.db.execute("UPDATE missions SET cwd=? WHERE id=?", (str(tmp_path / "removed"), mission["id"]))
    store.close()

    reopened = BridgeStore(path)

    row = reopened.db.execute("SELECT cwd,repo_identity FROM missions WHERE id=?", (mission["id"],)).fetchone()
    assert row["cwd"] == str(tmp_path / "removed")
    assert row["repo_identity"] == expected_identity


def test_migration_preserves_identity_when_historical_cwd_is_gone(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    missing_cwd = tmp_path / "no-longer-mounted"
    historical_identity = "/historical/worktree"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE missions(id TEXT PRIMARY KEY,name TEXT NOT NULL,objective TEXT NOT NULL,cwd TEXT NOT NULL,repo_identity TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,mission_id TEXT NOT NULL,title TEXT NOT NULL,prompt TEXT NOT NULL,pending_json TEXT NOT NULL DEFAULT '{}',priority INTEGER NOT NULL DEFAULT 0,ordering INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'QUEUED',revision INTEGER NOT NULL DEFAULT 1,acceptance_json TEXT NOT NULL DEFAULT '[]',evidence_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE task_dependencies(task_id INTEGER NOT NULL,depends_on INTEGER NOT NULL,PRIMARY KEY(task_id,depends_on));
            CREATE TABLE operations(id TEXT PRIMARY KEY,mission_id TEXT NOT NULL,task_id INTEGER NOT NULL,iteration INTEGER NOT NULL,station TEXT NOT NULL,status TEXT NOT NULL,exit_code INTEGER,logs_path TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,mission_id TEXT NOT NULL,task_id INTEGER,operation_id TEXT,kind TEXT NOT NULL,data_json TEXT NOT NULL,created_at TEXT NOT NULL);
            PRAGMA user_version=3;
            """
        )
        db.execute(
            "INSERT INTO missions VALUES('old','old','objective',?,?, 'STOPPED','now','now')",
            (str(missing_cwd), historical_identity),
        )

    migrated = BridgeStore(path)

    assert migrated.db.execute("SELECT repo_identity FROM missions WHERE id='old'").fetchone()[0] == historical_identity


def test_operation_identity_commands_and_worktree_exclusion(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    _activate(store, mission)
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
    _activate(store, mission)
    task = store.add_task(mission["id"], "one task")
    op = store.begin_operation(task, iteration=1, station="codex")
    store.finish_operation(op, exit_code=0)
    with pytest.raises(TransitionError, match="cannot become completed"):
        store.set_task_status(task, "COMPLETED")
    with pytest.raises(TransitionError, match="tasks are not complete"):
        store.set_mission_status(mission["id"], "COMPLETED")


def test_amendment_during_operation_invalidates_that_operation_success(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    _activate(store, mission)
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
    assert store.get_task(1)["origin"] == "operator"
    assert store.get_task(1)["proposal_status"] == "NONE"
    assert store.get_mission("old")["config"] == {}
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_amendment_after_exit_invalidates_verification_and_requeues_task(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    _activate(store, mission)
    task = store.add_task(mission["id"], "first revision")
    operation = store.begin_operation(task, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    assert store.get_task(task)["status"] == "VERIFYING"
    store.amend_task(task, prompt="second revision")
    updated = store.get_task(task)
    assert updated["status"] == "READY"
    assert updated["prompt"] == "second revision"
    assert updated["pending"] == {}
    assert updated["revision"] == 2
    with pytest.raises(TransitionError, match="not awaiting verification"):
        store.verify_task(task, approved=True)


def test_running_task_cannot_gain_new_dependencies(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    _activate(store, mission)
    task = store.add_task(mission["id"], "running task")
    dependency = store.add_task(mission["id"], "new prerequisite")
    store.begin_operation(task, iteration=1, station="claude")
    with pytest.raises(TransitionError, match="running task"):
        store.add_dependency(mission["id"], task, dependency)


def test_dependency_invariant_covers_verifying_paused_ready_and_completion(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    _activate(store, mission)
    verifying = store.add_task(mission["id"], "verifying")
    prerequisite = store.add_task(mission["id"], "prerequisite")
    store.begin_operation(verifying, iteration=1, station="codex")
    store.finish_operation(store.db.execute("SELECT id FROM operations WHERE task_id=?", (verifying,)).fetchone()[0], exit_code=0)
    with pytest.raises(TransitionError, match="dependency"):
        store.add_dependency(mission["id"], verifying, prerequisite)
    with store._transaction():
        store.db.execute("INSERT INTO task_dependencies(task_id,depends_on) VALUES(?,?)", (verifying, prerequisite))
    with pytest.raises(TransitionError, match="dependency"):
        store.verify_task(verifying, approved=True)

    paused = store.add_task(mission["id"], "paused")
    store.set_task_status(paused, "PAUSED")
    store.add_dependency(mission["id"], paused, prerequisite)
    with pytest.raises(TransitionError, match="dependency"):
        store.set_task_status(paused, "READY")
    store.set_task_status(paused, "QUEUED")
    assert paused not in [task["id"] for task in store.ready_tasks(mission["id"]) if task["id"] != prerequisite]


def test_sibling_directories_in_one_git_worktree_are_exclusive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "b").mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("a", "objective", cwd=repo / "a")
    store.set_mission_status(mission["id"], "RUNNING")
    with pytest.raises(TransitionError, match="already owns worktree"):
        store.create_mission("b", "other objective", cwd=repo / "b")


def test_commit_failure_rolls_back_and_leaves_connection_reusable(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction():
            store.db.execute("PRAGMA defer_foreign_keys=ON")
            store.db.execute("INSERT INTO task_dependencies(task_id,depends_on) VALUES(999,998)")
    assert not store.db.in_transaction
    task = store.add_task(mission["id"], "connection still works")
    assert store.get_task(task)["prompt"] == "connection still works"


@pytest.mark.parametrize(("requested_status", "expected_status"), [("PAUSED", "PAUSED"), ("CANCELLED", "CANCELLED")])
def test_successful_child_exit_preserves_operator_task_state(tmp_path: Path, requested_status: str, expected_status: str) -> None:
    store = BridgeStore(tmp_path / f"{requested_status}.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    _activate(store, mission)
    task = store.add_task(mission["id"], "task being operated on")
    operation = store.begin_operation(task, iteration=1, station="codex")
    store.set_task_status(task, requested_status)

    result = store.finish_operation(operation, exit_code=0)

    assert result["status"] == expected_status
    assert store.get_operation(operation)["status"] == "EXITED"


def test_successful_child_exit_observes_mission_pause(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "mission-pause.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task = store.add_task(mission["id"], "task being operated on")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task, iteration=1, station="codex")
    store.set_mission_status(mission["id"], "PAUSED")

    result = store.finish_operation(operation, exit_code=0)

    assert result["status"] == "VERIFYING"


def test_reentering_an_acquired_mission_lock_does_not_drop_exclusion(tmp_path: Path) -> None:
    lock = MissionLock(tmp_path / "locks", "mission")
    competing = MissionLock(tmp_path / "locks", "mission")
    lock.acquire()
    try:
        with lock:
            with pytest.raises(BlockingIOError):
                competing.acquire()
    finally:
        lock.release()
    competing.acquire()
    competing.release()


def test_operation_process_identity_is_persisted_only_while_running(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    _activate(store, mission)
    task_id = store.add_task(mission["id"], "work")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation, process_pid=10, process_start="start-a")

    store.record_operation_process(operation, process_pid=11, process_start="start-b")

    assert store.get_operation(operation)["process_pid"] == 11
    assert store.get_operation(operation)["process_start"] == "start-b"
    store.finish_operation(operation, exit_code=0)
    with pytest.raises(TransitionError, match="not running"):
        store.record_operation_process(operation, process_pid=12, process_start="start-c")


@pytest.mark.parametrize("mission_status", ["PAUSED", "STOPPING", "STOPPED", "CANCELLED"])
def test_operation_intent_cannot_cross_a_nonrunning_mission_boundary(tmp_path: Path, mission_status: str) -> None:
    store = BridgeStore(tmp_path / f"{mission_status}.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    store.set_mission_status(mission["id"], "RUNNING")
    task = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], mission_status)

    with pytest.raises(TransitionError, match="mission is not running"):
        store.begin_operation(task, iteration=1, station="codex")


def test_runtime_events_are_transactional_and_payloads_are_bounded(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)

    event_id = store.record_runtime_event(mission["id"], "provider-selection", {"provider": "codex", "usable": True})

    event = store.events(mission["id"])[-1]
    assert event["id"] == event_id
    assert event["kind"] == "provider-selection"
    assert event["data"] == {"provider": "codex", "usable": True}
    with pytest.raises(ValueError, match="16 KiB"):
        store.record_runtime_event(mission["id"], "too-large", {"payload": "x" * 17_000})


def test_operation_log_directory_lookup_uses_prefix_index_and_excludes_siblings(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("mission", "objective", cwd=tmp_path)
        store.set_mission_status(mission["id"], "RUNNING")
        task = store.add_task(mission["id"], "work")
        target = store.begin_operation(task, iteration=1, station="codex")
        store.mark_operation_running(target)
        store.finish_operation(target, exit_code=0, logs_path=str(tmp_path / "logs" / "run" / "attempt.out"))
        sibling_task = store.add_task(mission["id"], "sibling")
        sibling = store.begin_operation(sibling_task, iteration=2, station="codex")
        store.mark_operation_running(sibling)
        store.finish_operation(sibling, exit_code=0, logs_path=str(tmp_path / "logs" / "runner" / "attempt.out"))

        rows = store.operation_logs_in_directory(mission["id"], tmp_path / "logs" / "run")
        plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM operations WHERE mission_id=? AND logs_path>=? AND logs_path<?",
            (mission["id"], str(tmp_path / "logs" / "run") + "/", str(tmp_path / "logs" / "run") + "0"),
        ).fetchall()

        assert [row["id"] for row in rows] == [target]
        assert "operations_mission_logs_path" in " ".join(str(row["detail"]) for row in plan)


def test_managed_verifier_process_identity_is_durable_until_exit(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("mission", "objective", cwd=tmp_path)
        task = store.add_task(mission["id"], "work")
        store.set_mission_status(mission["id"], "RUNNING")
        operation = store.begin_operation(task, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)

        process_id = store.prepare_managed_process(mission["id"], task, kind="verifier")
        assert store.unresolved_managed_processes(mission["id"])[0]["status"] == "STARTING"
        store.mark_managed_process_running(process_id, process_pid=1234, process_start="boot:123")

        running = store.get_managed_process(process_id)
        assert running["status"] == "RUNNING"
        assert running["process_pid"] == 1234
        assert running["process_start"] == "boot:123"
        store.finish_managed_process(process_id, exit_code=0)

        assert store.unresolved_managed_processes(mission["id"]) == []
        assert store.get_managed_process(process_id)["status"] == "EXITED"
        assert any(event["kind"] == "managed-process-exited" for event in store.events(mission["id"]))


def test_managed_process_rejects_unowned_or_ambiguous_lifecycle_changes(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("mission", "objective", cwd=tmp_path)
        task = store.add_task(mission["id"], "verify")
        other = store.create_mission("other", "other objective", cwd=tmp_path)
        foreign_task = store.add_task(other["id"], "foreign")
        store.set_mission_status(mission["id"], "RUNNING")
        operation = store.begin_operation(task, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)

        with pytest.raises(ValueError, match="unsupported managed process kind"):
            store.prepare_managed_process(mission["id"], task, kind="shell")
        with pytest.raises(TransitionError, match="another mission"):
            store.prepare_managed_process(mission["id"], foreign_task, kind="verifier")
        process_id = store.prepare_managed_process(mission["id"], task, kind="verifier")
        with pytest.raises(ValueError, match="valid pid and start token"):
            store.mark_managed_process_running(process_id, process_pid=1, process_start="")
        store.mark_managed_process_running(process_id, process_pid=1234, process_start="boot:123")
        with pytest.raises(TransitionError, match="not starting"):
            store.mark_managed_process_running(process_id, process_pid=1234, process_start="boot:123")
        with pytest.raises(ValueError, match="invalid managed process result status"):
            store.finish_managed_process(process_id, exit_code=0, status="RUNNING")

        store.finish_managed_process(process_id, exit_code=0)
        assert store.finish_managed_process(process_id, exit_code=0)["status"] == "EXITED"
        with pytest.raises(TransitionError, match="already resolved"):
            store.finish_managed_process(process_id, exit_code=2, status="FAILED")


def test_agent_proposals_require_operator_acceptance_before_scheduling(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("mission", "objective", cwd=tmp_path)
        source_task = store.add_task(mission["id"], "source work")
        store.set_mission_status(mission["id"], "RUNNING")
        operation_id = store.begin_operation(source_task, iteration=1, station="codex")
        store.finish_operation(operation_id, exit_code=0)

        proposed = store.propose_task(
            mission["id"], "Add focused regression coverage", source_operation_id=operation_id,
            title="Regression coverage", priority=50,
        )
        item = store.get_task(proposed)
        assert item["origin"] == "agent-proposal"
        assert item["proposal_status"] == "PENDING"
        assert item["proposed_by_operation"] == operation_id
        assert [task["id"] for task in store.list_proposals(mission["id"])] == [proposed]
        assert proposed not in {task["id"] for task in store.ready_tasks(mission["id"])}
        with pytest.raises(TransitionError, match="operator acceptance"):
            store.begin_operation(proposed, iteration=2, station="claude")

        store.decide_proposal(mission["id"], proposed, accept=True)
        assert store.get_task(proposed)["proposal_status"] == "ACCEPTED"
        assert proposed in {task["id"] for task in store.ready_tasks(mission["id"])}
        proposal_operation = store.begin_operation(proposed, iteration=2, station="claude")
        store.finish_operation(proposal_operation, exit_code=0)
        store.verify_task(proposed, evidence="accepted proposal complete")

        rejected = store.propose_task(
            mission["id"], "Unneeded follow-up", source_operation_id=operation_id,
        )
        store.decide_proposal(mission["id"], rejected, accept=False, reason="outside scope")
        rejected_item = store.get_task(rejected)
        assert rejected_item["proposal_status"] == "REJECTED"
        assert rejected_item["status"] == "CANCELLED"
        assert {task["id"] for task in store.list_proposals(mission["id"], status="ALL", limit=1)} == {proposed}
        assert [task["id"] for task in store.list_proposals(mission["id"], status="REJECTED")] == [rejected]


@pytest.mark.parametrize("terminal_status", ["COMPLETED", "CANCELLED"])
def test_finished_mission_rejects_proposal_acceptance_and_cancelled_rejects_tasks(tmp_path: Path, terminal_status: str) -> None:
    with BridgeStore(tmp_path / f"{terminal_status.lower()}.sqlite3") as store:
        mission = store.create_mission("mission", "objective", cwd=tmp_path)
        task = store.add_task(mission["id"], "source work")
        store.set_mission_status(mission["id"], "RUNNING")
        operation = store.begin_operation(task, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
        if terminal_status == "COMPLETED":
            store.verify_task(task, evidence="verified source work")
            proposal = store.propose_task(mission["id"], "new proposal", source_operation_id=operation)
            store.set_mission_status(mission["id"], "COMPLETED")
            with pytest.raises(TransitionError, match="takes no new proposals"):
                store.decide_proposal(mission["id"], proposal, accept=True)
            assert store.get_task(proposal)["proposal_status"] == "PENDING"
        else:
            store.set_mission_status(mission["id"], "CANCELLED")
            with pytest.raises(TransitionError, match="was cancelled"):
                store.add_task(mission["id"], "must not be stranded")

