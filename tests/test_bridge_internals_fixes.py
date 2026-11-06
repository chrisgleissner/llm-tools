"""Regression tests for the internals review findings INT-002, 004, 008, 009, 010, 011, 012, 015, 017."""

from __future__ import annotations

import itertools
import json
import os
import signal
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_tools import bridge_logs, bridge_runtime, common, scheduler
from llm_tools.bridge_logs import capture_process_output
from llm_tools.bridge_runtime import BridgeMissionHooks, _process_start_token, launch_controller, run_controller
from llm_tools.bridge_store import COMMAND_EVENT_RESULT_BYTES, BridgeStore, MissionLock
from tests.conftest import scaled, wait_for


def _headless_mission(state: Path, tmp_path: Path, *, tasks: int = 1) -> tuple[str, list[int]]:
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission(
            "internals", "objective", cwd=tmp_path,
            config={"headless": True, "frozen_tool_config": "[ralph]\nproviders = ['codex']\n"},
        )
        return str(mission["id"]), [store.add_task(mission["id"], f"work {index}") for index in range(tasks)]


class _FakeWorker:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


# --------------------------------------------------------------------------- INT-002

def _run_failing_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, subprocess.Popen, int, int]:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    mission_id, (task_id,) = _headless_mission(state, tmp_path)
    provider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    monkeypatch.setattr(bridge_runtime, "CapacityRefreshWorker", _FakeWorker)

    def fake_ralph(_argv, *, bridge_hooks):
        with BridgeStore(state / "bridge.sqlite3") as other:
            operation = other.begin_operation(task_id, iteration=1, station="codex")
            other.mark_operation_running(operation, process_pid=provider.pid, process_start=_process_start_token(provider.pid))
        raise RuntimeError("controller lost its database")

    monkeypatch.setattr(bridge_runtime.ralph_robin, "main", fake_ralph)
    return state, mission_id, provider, task_id, run_controller(mission_id, headless=True)


def test_controller_exception_stops_the_provider_process_group(tmp_path, monkeypatch) -> None:
    provider = None
    try:
        state, mission_id, provider, _task_id, code = _run_failing_controller(tmp_path, monkeypatch)
        assert code == 1
        assert wait_for(lambda: provider.poll() is not None, 5), "the provider process group survived the controller failure"
        with BridgeStore(state / "bridge.sqlite3") as store:
            assert store.get_mission(mission_id)["status"] == "FAILED"
            operation = store.db.execute("SELECT status FROM operations WHERE mission_id=?", (mission_id,)).fetchone()
            assert operation["status"] == "UNCERTAIN"
    finally:
        if provider is not None and provider.poll() is None:
            os.killpg(provider.pid, signal.SIGKILL)
            provider.wait(timeout=scaled(5))


def test_provider_that_cannot_be_stopped_is_recorded_as_orphaned(tmp_path, monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise PermissionError("identity mismatch")

    monkeypatch.setattr(bridge_runtime, "_terminate_owned_process_group", refuse)
    provider = None
    try:
        state, mission_id, provider, _task_id, code = _run_failing_controller(tmp_path, monkeypatch)
        assert code == 1
        with BridgeStore(state / "bridge.sqlite3") as store:
            assert store.get_mission(mission_id)["status"] == "FAILED"
            orphaned = [event for event in store.events(mission_id) if event["kind"] == "provider-orphaned"]
        assert len(orphaned) == 1
        assert orphaned[0]["data"]["process_pid"] == provider.pid
        assert orphaned[0]["data"]["process_start"] == _process_start_token(provider.pid)
        assert "identity mismatch" in orphaned[0]["data"]["reason"]
    finally:
        if provider is not None and provider.poll() is None:
            os.killpg(provider.pid, signal.SIGKILL)
            provider.wait(timeout=scaled(5))


# --------------------------------------------------------------------------- INT-004 / INT-015: scheduler watchdog

def test_question_followed_by_more_output_does_not_arm_the_watchdog() -> None:
    guard = scheduler.ProgressGuard()
    assert guard.note_output("Which approach should I take?\nI'll go with A and run the tests.\n") is False
    assert guard.question_seen_at is None


def test_question_at_the_end_of_output_still_arms_the_watchdog() -> None:
    guard = scheduler.ProgressGuard()
    assert guard.note_output("Working on it.\nWhich approach should I take?\n") is False
    assert guard.question_seen_at is not None


def test_tool_activity_clears_a_pending_question() -> None:
    guard = scheduler.ProgressGuard()
    guard.note_output("Which approach should I take?\n")
    assert guard.question_seen_at is not None
    guard.note_output("Which approach should I take?\n" + scheduler.TOOL_ACTIVITY_SCAN_MARKER)
    assert guard.question_seen_at is None


def test_stderr_never_arms_or_clears_the_question_but_can_still_block() -> None:
    guard = scheduler.ProgressGuard()
    assert guard.note_output("Continue without the lockfile?\n", from_stderr=True) is False
    assert guard.question_seen_at is None
    guard.note_output("Which approach should I take?\n")
    assert guard.note_output("some warning\n", from_stderr=True) is False
    assert guard.question_seen_at is not None  # a stderr line does not answer the question
    assert guard.note_output("Press Enter to confirm\n", from_stderr=True) is True


def test_progress_guard_ignores_wall_clock_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = itertools.count(1_000_000_000, 10_000)  # every reading jumps forward by almost three hours
    monkeypatch.setattr(scheduler.time, "time", lambda: float(next(clock)))
    guard = scheduler.ProgressGuard()
    guard.note_output("Which approach should I take?\n")
    assert guard.overdue() is None


_PROVIDER_SCRIPTS = {
    "stderr-question": "import sys,time; sys.stderr.write('Continue without the lockfile?\\n'); sys.stderr.flush(); time.sleep({quiet}); print('done')",
    "question-then-progress": "import time; print('Which approach should I take?'); print('Going with A and running the tests.', flush=True); time.sleep({quiet}); print('done')",
}


@pytest.fixture
def watchdog_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "1")
    monkeypatch.setenv("LLM_SCHEDULER_IDLE_TIMEOUT", "30")
    monkeypatch.setenv("LLM_SCHEDULER_NO_STREAM", "1")


def _run_exact(tmp_path: Path, code: str) -> int:
    cfg = scheduler.SchedulerConfig(provider="codex", cwd=str(tmp_path), exact_stdout=True, prompt_text="x")
    return scheduler.run_fresh_exact_stdout(cfg, [sys.executable, "-c", code], tmp_path / "out.txt", tmp_path / "status.txt")


@pytest.mark.parametrize("name", sorted(_PROVIDER_SCRIPTS))
def test_headless_run_that_keeps_working_is_not_aborted_by_an_earlier_question(tmp_path, watchdog_env, name) -> None:
    assert _run_exact(tmp_path, _PROVIDER_SCRIPTS[name].format(quiet=2.5)) == 0
    assert "done" in (tmp_path / "out.txt").read_text(encoding="utf-8")


def test_headless_run_that_ends_on_an_unanswered_question_is_still_aborted(tmp_path, watchdog_env) -> None:
    code = "import time; print('Which approach should I take?', flush=True); time.sleep(20)"
    assert _run_exact(tmp_path, code) == common.AUTONOMY_ABORT_STATUS
    assert "question required a response" in (tmp_path / "out.txt").read_text(encoding="utf-8")


def test_pty_run_that_keeps_working_is_not_aborted_by_an_earlier_question(tmp_path) -> None:
    code = _PROVIDER_SCRIPTS["question-then-progress"].format(quiet=2.5)
    status, text = common.run_pty_capture(
        [sys.executable, "-c", code], tmp_path, 30, stream=False, auto_confirm=False,
        idle_timeout=30, question_idle_timeout=1,
    )
    assert status == 0 and "done" in text


def test_pty_run_that_ends_on_an_unanswered_question_is_still_aborted(tmp_path) -> None:
    code = "import time; print('Which approach should I take?', flush=True); time.sleep(20)"
    status, text = common.run_pty_capture(
        [sys.executable, "-c", code], tmp_path, 30, stream=False, auto_confirm=False,
        idle_timeout=30, question_idle_timeout=1,
    )
    assert status == common.AUTONOMY_ABORT_STATUS
    assert "question required a response" in text


def test_pty_watchdog_ignores_wall_clock_steps(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = itertools.count(1_000_000_000, 10_000)
    monkeypatch.setattr(common.time, "time", lambda: float(next(clock)))
    status, text = common.run_pty_capture(
        [sys.executable, "-c", "import time; time.sleep(0.4); print('ok')"], tmp_path, 3600, stream=False,
        auto_confirm=False, idle_timeout=5, question_idle_timeout=5,
    )
    assert status == 0 and "ok" in text


def test_bridge_hands_a_first_watchdog_stop_to_the_next_entry_and_holds_the_second(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("watchdog", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = SimpleNamespace(
        provider="codex", cwd=str(tmp_path), run_dir=run_dir, model="", exec_mode="fresh", headless=True,
        native_tui=False, tmux_target="", tmux_socket="", tmux_pane="", env_overrides={},
        prompt_text="x", max_output_bytes=0, max_output_rotations=0, log_dir=None,
    )
    selection = {"provider": "codex"}
    for attempt in (1, 2):
        context = hook.before_iteration()
        assert context["task_id"] == task_id
        assert hook.before_launch(context, selection, config, attempt)
        hook.after_iteration(context, selection, config, attempt, common.AUTONOMY_ABORT_STATUS, 1.0)
        if attempt == 1:
            assert store.get_task(task_id)["status"] == "READY"
    assert store.get_task(task_id)["status"] == "BLOCKED"
    exhausted = [event for event in store.events(mission["id"]) if event["kind"] == "provider-handoff-exhausted"]
    assert len(exhausted) == 1 and exhausted[0]["data"]["autonomy_aborts"] == 2


# --------------------------------------------------------------------------- INT-008

def test_log_sink_keeps_draining_when_the_log_cannot_be_written(tmp_path, monkeypatch, capfd) -> None:
    def full_disk(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bridge_logs, "_append_rotating", full_disk)
    seen: list[str] = []

    def noticed() -> bool:
        seen.append(capfd.readouterr().err)
        return "not writable" in "".join(seen)

    with capture_process_output(tmp_path / "controller.log", max_bytes=1024, rotations=1):
        os.write(1, b"first\n")
        assert wait_for(noticed, 5, message="the sink never reported the failed log write")
        # The pipe must still have a reader; a dead reader makes this raise BrokenPipeError.
        os.write(1, b"second\n")
        os.write(2, b"third\n")


def test_log_sink_drain_timeout_does_not_replace_the_result(tmp_path, monkeypatch, capfd) -> None:
    monkeypatch.setattr(bridge_logs, "_DRAIN_TIMEOUT_SECONDS", 0.2)
    keep_open = -1
    try:
        with capture_process_output(tmp_path / "controller.log", max_bytes=1024, rotations=1):
            keep_open = os.dup(1)  # a descendant that outlives the controller holds the pipe open
            os.write(1, b"output\n")
        assert "did not drain" in capfd.readouterr().err
    finally:
        if keep_open >= 0:
            os.close(keep_open)


# --------------------------------------------------------------------------- INT-009

def test_failed_exec_in_the_pty_child_never_returns_into_the_caller(tmp_path) -> None:
    script = textwrap.dedent(
        """
        import os, sys
        from pathlib import Path
        from llm_tools import common
        print("PARENT", os.getpid(), flush=True)
        try:
            status, text = common.run_pty_capture(["/nonexistent/provider-cli"], Path(sys.argv[1]), 10, stream=False, auto_confirm=False)
        except BaseException as exc:
            print("RAISED", os.getpid(), type(exc).__name__, flush=True)
            raise
        print("RESULT", os.getpid(), status, flush=True)
        """
    )
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=scaled(20), cwd=str(Path(__file__).resolve().parent.parent))
    lines = result.stdout.splitlines()
    parent = lines[0].split()[1]
    assert not any(line.startswith("RAISED") for line in lines), result.stdout + result.stderr
    assert [line.split()[:2] for line in lines if line.startswith("RESULT")] == [["RESULT", parent]]
    assert lines[-1].split() == ["RESULT", parent, "127"], result.stdout + result.stderr


# --------------------------------------------------------------------------- INT-010

def test_a_second_start_does_not_spawn_while_the_first_is_launching(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    mission_id, _ = _headless_mission(state, tmp_path)
    monkeypatch.setattr(bridge_runtime, "_START_LOCK_WAIT_SECONDS", 0.1)
    spawned: list[object] = []
    monkeypatch.setattr(bridge_runtime, "subprocess", SimpleNamespace(Popen=lambda *a, **k: spawned.append(a), DEVNULL=subprocess.DEVNULL))
    first_launcher = MissionLock(state / "locks", mission_id, kind="start")
    first_launcher.acquire()
    try:
        with pytest.raises(RuntimeError, match="another start"):
            launch_controller(mission_id, headless=True)
    finally:
        first_launcher.release()
    assert spawned == []


def test_start_lock_is_released_once_the_controller_is_ready(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    mission_id, _ = _headless_mission(state, tmp_path)
    runtime = state / "runtime" / f"mission-{mission_id}"
    runtime.mkdir(parents=True, mode=0o700)

    def popen(command, **_kwargs):
        token = command[command.index("--startup-token") + 1]
        (runtime / "controller-ready.json").write_text(json.dumps({"pid": 4545, "startup_token": token, "status": "RUNNING"}), encoding="utf-8")
        return SimpleNamespace(pid=4545, returncode=None, poll=lambda: None)

    monkeypatch.setattr(bridge_runtime, "subprocess", SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL))
    assert launch_controller(mission_id, headless=True) == 0
    again = MissionLock(state / "locks", mission_id, kind="start")
    again.acquire()  # raises BlockingIOError while a launcher still holds it
    again.release()


def test_controller_records_its_identity_in_the_lock_it_holds(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    mission_id, _ = _headless_mission(state, tmp_path)
    monkeypatch.setattr(bridge_runtime, "CapacityRefreshWorker", _FakeWorker)
    owners: list[dict] = []

    def fake_ralph(_argv, *, bridge_hooks):
        owners.append(MissionLock.read_owner(state / "locks", mission_id))
        return 0

    monkeypatch.setattr(bridge_runtime.ralph_robin, "main", fake_ralph)
    run_controller(mission_id, headless=True, startup_token="token-1")
    assert owners == [{"pid": os.getpid(), "process_start": _process_start_token(os.getpid()), "startup_token": "token-1"}]
    # Once the controller has exited, the leftover record is not an owner.
    assert MissionLock.read_owner(state / "locks", mission_id) == {}


def test_force_stop_finds_the_controller_through_the_lock_when_the_ready_file_is_gone(tmp_path, env) -> None:
    from tests.test_bridge_force_stop import _call, _controller_locked, _mission_status, _wait

    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("mission", "controller identity", cwd=tmp_path)
        mission_id = str(mission["id"])
        store.set_mission_status(mission_id, "RUNNING")
    (state / "runtime" / f"mission-{mission_id}").mkdir(mode=0o700, parents=True)  # and no controller-ready.json
    owner_code = (
        "import os,sys,time; from pathlib import Path; from llm_tools.bridge_store import MissionLock; "
        "from llm_tools.bridge_runtime import _process_start_token; "
        "lock=MissionLock(Path(sys.argv[1]),sys.argv[2]); lock.acquire(); "
        "lock.write_owner({'pid': os.getpid(), 'process_start': _process_start_token(os.getpid()), 'startup_token': 't'}); "
        "Path(sys.argv[3]).write_text('ready'); time.sleep(30)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_code, str(state / "locks"), mission_id, str(tmp_path / "owner.ready")],
        env=env, start_new_session=True,
    )
    try:
        _wait(lambda: (tmp_path / "owner.ready").exists() and _controller_locked(state, mission_id))
        stopped = _call(env, "stop", "--mission", mission_id, "--force")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert _mission_status(state / "bridge.sqlite3", mission_id) == "STOPPED"
        assert owner.poll() is not None
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGTERM)
            owner.wait(timeout=scaled(3))


# --------------------------------------------------------------------------- INT-011

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def test_pruning_thins_old_wait_and_selection_events_and_keeps_audit_events(tmp_path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        mid = mission["id"]
        rows = []
        for minute in range(3 * 60):
            moment = NOW - timedelta(minutes=minute)
            for kind in ("provider-wait", "provider-selection"):
                rows.append((mid, kind, "{}", _stamp(moment)))
        for kind in ("task-added", "operation-exited", "controller-failed", "operation-uncertain"):
            rows.extend((mid, kind, "{}", _stamp(NOW - timedelta(hours=2, minutes=index))) for index in range(5))
        with store._transaction():
            store.db.executemany("INSERT INTO events(mission_id,kind,data_json,created_at) VALUES(?,?,?,?)", rows)

        def counts() -> dict[str, int]:
            return {row[0]: row[1] for row in store.db.execute("SELECT kind,COUNT(*) FROM events WHERE mission_id=? GROUP BY kind", (mid,))}

        before = counts()
        newest = {kind: store.latest_event(mid, kind)["id"] for kind in ("provider-wait", "provider-selection")}
        result = store.prune_plumbing_events(mid, now=NOW, budget_seconds=60)
        after = counts()
        assert result["complete"] is True
        for kind in ("provider-wait", "provider-selection"):
            assert before[kind] == 180
            assert 60 <= after[kind] <= 75, (kind, after[kind])  # the last hour in full, then one per 10 minutes
            assert store.latest_event(mid, kind)["id"] == newest[kind]
            assert result["deleted"][kind] == before[kind] - after[kind]
        for kind in ("task-added", "operation-exited", "controller-failed", "operation-uncertain"):
            assert after[kind] == before[kind] == 5


def test_pruning_deletes_only_old_finished_commands(tmp_path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        mid = mission["id"]
        old, recent = _stamp(NOW - timedelta(days=40)), _stamp(NOW - timedelta(days=2))
        for command_id, status, stamp in (
            ("old-applied", "APPLIED", old), ("old-rejected", "REJECTED", old), ("old-queued", "QUEUED", old),
            ("recent-applied", "APPLIED", recent),
        ):
            store.db.execute(
                "INSERT INTO commands(id,mission_id,payload_json,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (command_id, mid, "{}", status, "{}", stamp, stamp),
            )
        result = store.prune_plumbing_events(mid, now=NOW, budget_seconds=60)
        remaining = {row[0] for row in store.db.execute("SELECT id FROM commands WHERE mission_id=?", (mid,))}
        assert remaining == {"old-queued", "recent-applied"}
        assert result["deleted"] == {"commands": 2}
        assert not store.command_exists("old-applied")


def test_command_event_carries_a_summary_of_a_large_result(tmp_path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        mid = mission["id"]
        for command_id in ("small", "large"):
            store.db.execute(
                "INSERT INTO commands(id,mission_id,payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (command_id, mid, json.dumps({"action": "task.add"}), "QUEUED", _stamp(NOW), _stamp(NOW)),
            )
        store.finish_command("small", status="APPLIED", result={"task": {"prompt": "short"}})
        store.finish_command("large", status="APPLIED", result={"status": "APPLIED", "task": {"prompt": "x" * 100_000}})
        events = {event["data"]["command_id"]: event["data"] for event in store.events(mid) if event["kind"] == "command-applied"}
        assert events["small"]["result"] == {"task": {"prompt": "short"}}
        big = events["large"]["result"]
        assert big["truncated"] is True and big["status"] == "APPLIED" and "task" not in big
        assert len(json.dumps(big)) < COMMAND_EVENT_RESULT_BYTES
        # The commands table keeps the complete result.
        assert len(store.get_command("large")["result"]["task"]["prompt"]) == 100_000


# --------------------------------------------------------------------------- INT-012

def test_provider_exit_is_recorded_even_when_session_capture_fails(tmp_path, monkeypatch) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("session failure", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    context = hook.before_iteration()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "attempt-1.out").write_text("done\n", encoding="utf-8")
    config = SimpleNamespace(
        provider="codex", cwd=str(tmp_path), run_dir=run_dir, model="", exec_mode="fresh", headless=True,
        native_tui=False, tmux_target="", tmux_socket="", tmux_pane="", env_overrides={},
        prompt_text="x", max_output_bytes=0, max_output_rotations=0, log_dir=None,
    )
    selection = {"provider": "codex"}
    assert hook.before_launch(context, selection, config, 1)

    def broken(*_args, **_kwargs):
        raise FileNotFoundError("session file vanished")

    monkeypatch.setattr(bridge_runtime, "provider_session_metadata", broken)
    # A native provider session was being tracked for this operation.
    hook._session_started_ns[context["operation_id"]] = 1
    hook._session_candidates[context["operation_id"]] = SimpleNamespace(provider="codex", session_id="s", cwd=str(tmp_path), source="x")

    hook.after_iteration(context, selection, config, 1, 0, 1.0)

    operation = store.get_operation(context["operation_id"])
    assert operation["status"] == "EXITED" and operation["exit_code"] == 0
    assert operation["restoration_state"] == "UNAVAILABLE"
    assert store.get_task(task_id)["status"] == "VERIFYING"
    assert any(event["kind"] == "native-session-restoration" and event["data"]["restoration_state"] == "UNAVAILABLE" for event in store.events(mission["id"]))


# --------------------------------------------------------------------------- INT-017

def test_failed_lock_setup_does_not_leave_a_phantom_lock(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "locks"
    first = MissionLock(directory, "m1")
    real_chmod = os.chmod
    failures = iter([True])

    def flaky_chmod(path, mode, *args, **kwargs):
        if str(path).endswith("mission-m1.lock") and next(failures, False):
            raise OSError("unusual filesystem")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", flaky_chmod)
    with pytest.raises(OSError):
        first.acquire()
    assert first._file is None
    first.acquire()  # must really take the flock this time
    try:
        assert MissionLock.is_held(directory, "m1")
    finally:
        first.release()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "with space"])
def test_create_mission_rejects_ids_that_would_name_paths_outside_the_runtime_directory(tmp_path, bad) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        with pytest.raises(ValueError, match="invalid mission id"):
            store.create_mission("m", "o", cwd=tmp_path, mission_id=bad)
        assert store.create_mission("m", "o", cwd=tmp_path, mission_id="abcd-1234_x.y")["id"] == "abcd-1234_x.y"
