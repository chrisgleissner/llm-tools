"""Regression tests for the internals review findings INT-001, 003, 005, 006, 007, 013, 014 and 018."""

from __future__ import annotations

import pytest

import llm_tools.bridge_runtime as runtime
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore


class _Spinning(Exception):
    """The patched sleep was called more often than any bounded wait should need."""


def _hooks(tmp_path, store, mission_id):
    hook = BridgeMissionHooks(mission_id, handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    return hook


def _running_mission(tmp_path, statuses):
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path)
    ids = []
    for index, status in enumerate(statuses):
        task_id = store.add_task(mission["id"], f"task {index + 1}")
        if status != "QUEUED":
            store.set_task_status(task_id, status)
        ids.append(task_id)
    store.set_mission_status(mission["id"], "RUNNING")
    return store, mission["id"], ids


def _waiting_events(store, mission_id):
    return [event for event in store.events(mission_id) if event["kind"] == "mission-waiting-for-operator"]


def test_int001_blocked_tasks_wait_with_backoff_one_event_and_resume_when_operator_retries(tmp_path, monkeypatch) -> None:
    store, mission_id, (first, _second) = _running_mission(tmp_path, ["BLOCKED", "BLOCKED"])
    delays: list[float] = []

    def sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) > 8:
            raise _Spinning(f"no task can run and the controller keeps polling: {delays}")
        if len(delays) == 5:
            store.set_task_status(first, "QUEUED")  # the operator runs `task retry`

    monkeypatch.setattr(runtime.time, "sleep", sleep)
    context = _hooks(tmp_path, store, mission_id).before_iteration()

    assert context["action"] == "run" and context["task_id"] == first
    assert len(delays) == 5
    assert all(later >= earlier for earlier, later in zip(delays, delays[1:]))
    assert delays[-1] > delays[0]  # backs off instead of polling every 0.5 s
    events = _waiting_events(store, mission_id)
    assert len(events) == 1  # one event for the whole wait, not one per poll
    assert [task["status"] for task in events[0]["data"]["tasks"]] == ["BLOCKED", "BLOCKED"]
    assert "task retry" in events[0]["data"]["command"]


def test_int001_new_wait_after_the_state_changed_records_a_second_event(tmp_path, monkeypatch) -> None:
    store, mission_id, (_first, second, _third) = _running_mission(tmp_path, ["BLOCKED", "BLOCKED", "BLOCKED"])
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 2:
            store.set_task_status(second, "CANCELLED")  # a different set of tasks now waits
        if len(calls) > 5:
            store.set_mission_status(mission_id, "STOPPING")

    monkeypatch.setattr(runtime.time, "sleep", sleep)
    result = _hooks(tmp_path, store, mission_id).before_iteration()

    assert result["action"] == "stop"
    assert len(_waiting_events(store, mission_id)) == 2


def test_int001_cancelled_tasks_count_as_finished_for_completion(tmp_path, monkeypatch) -> None:
    store, mission_id, (done, _cancelled) = _running_mission(tmp_path, ["QUEUED", "CANCELLED"])
    store.set_task_status(done, "READY")
    store.set_task_status(done, "RUNNING")
    store.db.execute("UPDATE tasks SET status='COMPLETED' WHERE id=?", (done,))

    def spin(_seconds: float) -> None:
        raise _Spinning("a mission with a completed and a cancelled task must finish")

    monkeypatch.setattr(runtime.time, "sleep", spin)
    result = _hooks(tmp_path, store, mission_id).before_iteration()

    assert result == {"action": "stop", "reason": "all tasks verified", "exit_code": 0}
    assert store.get_mission(mission_id)["status"] == "COMPLETED"


def test_int001_mission_whose_tasks_are_all_cancelled_stops_instead_of_waiting(tmp_path, monkeypatch) -> None:
    store, mission_id, _ids = _running_mission(tmp_path, ["CANCELLED", "CANCELLED"])

    def spin(_seconds: float) -> None:
        raise _Spinning("all tasks cancelled: nothing to wait for")

    monkeypatch.setattr(runtime.time, "sleep", spin)
    result = _hooks(tmp_path, store, mission_id).before_iteration()

    assert result["action"] == "stop"
    assert store.get_mission(mission_id)["status"] == "STOPPED"


def test_int001_status_says_the_mission_needs_the_operator(tmp_path) -> None:
    from llm_tools import bridge_format as fmt
    from llm_tools.bridge_monitor import project_snapshot

    store, mission_id, (_first, _second) = _running_mission(tmp_path, ["BLOCKED", "PAUSED"])
    needs = fmt.needs_attention(project_snapshot(store, mission_id))  # type: ignore[arg-type]

    assert needs is not None and needs["kind"] == "operator"
    assert "waiting for you" in needs["message"]


def _task_with_unresolved_operation(tmp_path, final_status):
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    store.begin_operation(task_id, iteration=1, station="codex")
    store.set_task_status(task_id, final_status)
    return store, mission["id"], task_id


@pytest.mark.parametrize("operator_status", ["CANCELLED", "PAUSED"])
def test_int005_crash_reconciliation_keeps_operator_cancelled_and_paused_tasks(tmp_path, operator_status) -> None:
    store, mission_id, task_id = _task_with_unresolved_operation(tmp_path, operator_status)

    assert store.reconcile_interrupted_operations(mission_id) == 1

    assert store.get_task(task_id)["status"] == operator_status
    operation = store.db.execute("SELECT status FROM operations WHERE task_id=?", (task_id,)).fetchone()
    assert operation["status"] == "UNCERTAIN"  # the operation is still recorded as uncertain


def test_int005_crash_reconciliation_still_blocks_a_running_task(tmp_path) -> None:
    store, mission_id, task_id = _task_with_unresolved_operation(tmp_path, "RUNNING")

    store.reconcile_interrupted_operations(mission_id)

    assert store.get_task(task_id)["status"] == "BLOCKED"


def test_int006_stop_on_a_running_mission_without_a_controller_ends_stopped(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    store = BridgeStore(tmp_path / "state" / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path, config={"headless": True})
    store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")  # the controller died; nobody holds the mission lock

    result = bridge._apply_operator_command(store, mission["id"], {"action": "stop"})

    assert result["result"]["mission_status"] == "STOPPED"
    assert store.get_mission(mission["id"])["status"] == "STOPPED"


def test_int006_stop_with_a_live_controller_still_requests_stopping(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge
    from llm_tools.bridge_store import MissionLock

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    store = BridgeStore(tmp_path / "state" / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path, config={"headless": True})
    store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")

    with MissionLock(tmp_path / "state" / "locks", mission["id"]):
        result = bridge._apply_operator_command(store, mission["id"], {"action": "stop"})

    assert result["result"]["mission_status"] == "STOPPING"


def test_int014_resume_waits_out_a_momentary_liveness_probe(tmp_path, monkeypatch) -> None:
    import fcntl
    import threading

    from llm_tools import bridge

    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path, config={"headless": True})
    store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    store.set_mission_status(mission["id"], "PAUSED")
    store.close()
    lock_dir = state / "locks"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file = lock_dir / f"mission-{mission['id']}.lock"
    lock_file.touch()
    started: list[str] = []
    monkeypatch.setattr(bridge, "require_launchable_rotation", lambda *args, **kwargs: None)
    monkeypatch.setattr(bridge, "start_mission", lambda mission_id, **kwargs: started.append(mission_id) or 0)

    holder = lock_file.open("r")
    fcntl.flock(holder.fileno(), fcntl.LOCK_SH)  # what MissionLock.is_held does, briefly
    release = threading.Timer(0.1, lambda: (fcntl.flock(holder.fileno(), fcntl.LOCK_UN), holder.close()))
    release.start()
    try:
        assert bridge.main(["resume", "--mission", mission["id"]]) == 0
    finally:
        release.join()

    assert started == [mission["id"]]  # the old zero-timeout probe took the probe for a controller and started nothing


def test_int014_lock_probe_that_cannot_decide_reports_the_lock_as_held(tmp_path, monkeypatch) -> None:
    import errno
    import fcntl

    from llm_tools.bridge_store import MissionLock

    lock = MissionLock(tmp_path / "locks", "m1")
    lock.acquire()
    lock.release()

    def no_locks(_fd, _operation):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)
    assert MissionLock.is_held(tmp_path / "locks", "m1") is True


def test_int007_setup_failure_after_the_mission_is_running_records_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    store = BridgeStore(tmp_path / "state" / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path, config={"headless": True})
    store.add_task(mission["id"], "work")

    def broken(*_args, **_kwargs):
        raise OSError("cannot write the frozen tool config")

    monkeypatch.setattr(runtime, "_install_frozen_tool_config", broken)
    monkeypatch.setattr(runtime.ralph_robin, "main", lambda *_a, **_k: pytest.fail("setup failed; no provider may start"))

    with pytest.raises(OSError, match="frozen tool config"):
        runtime.run_controller(mission["id"], headless=True)

    assert store.get_mission(mission["id"])["status"] == "FAILED"
    failed = [event for event in store.events(mission["id"]) if event["kind"] == "controller-failed"]
    assert len(failed) == 1 and "frozen tool config" in failed[0]["data"]["reason"]


def _sleeper_operation(tmp_path, monkeypatch):
    """A headless mission whose RUNNING operation names a live provider process with a verified identity."""
    import subprocess
    import sys

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    store = BridgeStore(tmp_path / "state" / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path, config={"headless": True})
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    store.mark_operation_running(operation_id, process_pid=process.pid, process_start=runtime._process_start_token(process.pid))
    return store, mission["id"], operation_id, process


def _fail_once(real):
    calls: list[int] = []

    def terminate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("could not terminate the provider")
        return real(*args, **kwargs)

    return terminate


def test_int003_controller_start_retries_a_failed_provider_termination(tmp_path, monkeypatch) -> None:
    import os
    import signal

    store, mission_id, operation_id, process = _sleeper_operation(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_terminate_owned_process_group", _fail_once(runtime._terminate_owned_process_group))
    monkeypatch.setattr(runtime.ralph_robin, "main", lambda *_a, **_k: 0)
    try:
        with pytest.raises(RuntimeError, match="could not terminate"):
            runtime.run_controller(mission_id, headless=True)
        assert process.poll() is None

        runtime.run_controller(mission_id, headless=True)  # the second start must still see the provider

        process.wait(timeout=5)  # the old code found no unresolved operation on the second start and left it running
        assert store.get_operation(operation_id)["status"] == "UNCERTAIN"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_int003_second_force_stop_retries_a_failed_provider_termination(tmp_path, monkeypatch) -> None:
    import os
    import signal

    from llm_tools import bridge

    store, mission_id, _operation_id, process = _sleeper_operation(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_terminate_owned_process_group", _fail_once(runtime._terminate_owned_process_group))
    try:
        assert bridge.main(["stop", "--mission", mission_id, "--force"]) == 1
        assert process.poll() is None
        assert store.get_mission(mission_id)["status"] == "STOPPING"

        assert bridge.main(["stop", "--mission", mission_id, "--force"]) == 0

        process.wait(timeout=5)  # the old code found no unresolved operation on the second attempt
        assert store.get_mission(mission_id)["status"] == "STOPPED"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_int003_force_stop_treats_a_reused_controller_pid_as_a_dead_controller(tmp_path, monkeypatch) -> None:
    import json
    import os
    import signal
    import subprocess
    import sys

    from llm_tools import bridge

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    store = BridgeStore(tmp_path / "state" / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path, config={"headless": True})
    store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    runtime_dir = tmp_path / "state" / "runtime" / f"mission-{mission['id']}"
    runtime_dir.mkdir(mode=0o700, parents=True)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    (runtime_dir / "controller-ready.json").write_text(json.dumps({
        "pid": unrelated.pid, "process_start": "boot:from-before-the-reboot", "startup_token": "t", "status": "RUNNING",
    }), encoding="utf-8")
    try:
        # The mission lock is free and the PID belongs to another process: the old controller is certainly gone.
        assert bridge.main(["stop", "--mission", mission["id"], "--force"]) == 0

        assert store.get_mission(mission["id"])["status"] == "STOPPED"
        assert unrelated.poll() is None  # never signalled
    finally:
        os.killpg(unrelated.pid, signal.SIGKILL)
        unrelated.wait(timeout=5)


def test_int013_idle_controller_polls_without_taking_the_write_lock(tmp_path, monkeypatch) -> None:
    store, mission_id, _ids = _running_mission(tmp_path, ["BLOCKED"])
    statements: list[str] = []
    store.db.set_trace_callback(lambda statement: statements.append(statement.split()[0:2].__str__()))
    polls: list[float] = []

    def sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 6:
            store.set_mission_status(mission_id, "STOPPING")

    monkeypatch.setattr(runtime.time, "sleep", sleep)
    assert _hooks(tmp_path, store, mission_id).before_iteration()["action"] == "stop"

    # Six idle polls: one write for the single waiting event, one for the status change the test makes.
    # Each poll used to open BEGIN IMMEDIATE through ready_tasks.
    assert len(polls) == 6
    assert statements.count("['BEGIN', 'IMMEDIATE']") <= 2


def test_int013_wait_signature_does_not_promote_tasks(tmp_path) -> None:
    store, mission_id, (task_id,) = _running_mission(tmp_path, ["QUEUED"])

    signature = _hooks(tmp_path, store, mission_id)._wait_signature()

    assert signature[2] == (task_id,)  # the runnable task is reported ...
    assert store.get_task(task_id)["status"] == "QUEUED"  # ... without a write or a task-ready event
    assert not any(event["kind"] == "task-ready" for event in store.events(mission_id))


def test_int013_sqlite_file_modes_are_secured_once_per_connection(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path)
    chmods: list[Path] = []
    real = Path.chmod
    monkeypatch.setattr(Path, "chmod", lambda self, mode: (chmods.append(self), real(self, mode))[1])

    for index in range(5):
        store.record_runtime_event(mission["id"], "test-event", {"n": index})

    assert len(chmods) == 0  # was one chmod pass over the database and its WAL files after every transaction
    assert (tmp_path / "bridge.sqlite3").stat().st_mode & 0o777 == 0o600


def test_int013_worker_session_reads_through_a_read_only_store(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from llm_tools import bridge_worker as worker

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    BridgeStore(tmp_path / "state" / "bridge.sqlite3").close()
    captured: dict[str, object] = {}
    monkeypatch.setattr(worker, "load_config", lambda env=None: SimpleNamespace())
    monkeypatch.setattr(worker, "terminal_connector", lambda env=None: None)
    monkeypatch.setattr(worker, "WorkerSession", lambda config, **kwargs: captured.update(kwargs))

    worker.default_session("1.0")

    with captured["open_store"]() as store:  # type: ignore[operator]
        assert store.readonly is True  # the 0.5 s event poll takes no write lock and chmods nothing


def test_int018_cancel_ends_the_running_provider_process(tmp_path, monkeypatch) -> None:
    import os
    import signal

    from llm_tools import bridge

    store, mission_id, operation_id, process = _sleeper_operation(tmp_path, monkeypatch)
    try:
        result = bridge._apply_operator_command(store, mission_id, {"action": "cancel"})

        process.wait(timeout=5)  # the old code returned with the provider still running
        assert result["result"]["mission_status"] == "CANCELLED"
        assert store.get_mission(mission_id)["status"] == "CANCELLED"
        assert "stop --force" not in result["result"].get("detail", "")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_int018_cancel_names_stop_force_when_the_provider_cannot_be_ended(tmp_path, monkeypatch) -> None:
    import os
    import signal

    from llm_tools import bridge

    store, mission_id, _operation_id, process = _sleeper_operation(tmp_path, monkeypatch)

    def refuse(*_args, **_kwargs):
        raise PermissionError("refusing to signal a process whose persisted identity does not match")

    monkeypatch.setattr(runtime, "_terminate_owned_process_group", refuse)
    try:
        result = bridge._apply_operator_command(store, mission_id, {"action": "cancel"})

        assert store.get_mission(mission_id)["status"] == "CANCELLED"  # the cancel itself still succeeds
        assert f"llm-bridge stop --force --mission {mission_id[:8]}" in result["result"]["detail"]
        assert process.poll() is None
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def test_int018_cancel_does_not_signal_a_reused_pid(tmp_path, monkeypatch) -> None:
    import os
    import signal

    from llm_tools import bridge

    store, mission_id, operation_id, process = _sleeper_operation(tmp_path, monkeypatch)
    store.db.execute("UPDATE operations SET process_start='boot:from-an-earlier-process' WHERE id=?", (operation_id,))
    try:
        bridge._apply_operator_command(store, mission_id, {"action": "cancel"})

        assert process.poll() is None  # the PID's current start token differs from the recorded one
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
