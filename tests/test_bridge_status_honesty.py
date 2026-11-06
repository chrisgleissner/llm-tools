"""Status must not claim work that is not happening (CLIUX-051, -053, -054, -062)."""

from __future__ import annotations

import json
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_format as fmt
from llm_tools import bridge_monitor, scheduler
from llm_tools.bridge_runtime import BridgeMissionHooks, _process_start_token
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled, wait_for


def _undetermined_selection(provider: str = "claude", reason: str = "unavailable") -> dict[str, Any]:
    decision = {"provider": provider, "usable": False, "reason": reason, "wait_until": int(time.time()) + 60, "windows": []}
    return {
        "index": 0, "provider": provider, "route": "", "rotation_reason": "advanced-to-undetermined",
        "all_rate_limited": False, "decision": decision, "decisions": [decision],
    }


def _running_mission(database: Path, tmp_path: Path) -> tuple[str, int]:
    with BridgeStore(database) as store:
        mission = store.create_mission("wait", "objective", cwd=tmp_path, config={"headless": True, "providers": "claude"})
        task_id = store.add_task(mission["id"], "OK job")
        store.set_mission_status(mission["id"], "RUNNING")
    return str(mission["id"]), int(task_id)


def _launch_in_thread(database: Path, mission_id: str, tmp_path: Path, scfg: scheduler.SchedulerConfig, outcome: list[bool]) -> threading.Thread:
    """Run the controller's launch boundary on its own thread (the store belongs to the thread that opens it)."""
    def run() -> None:
        hooks = BridgeMissionHooks(mission_id, handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hooks.bind(BridgeStore(database))
        try:
            context = hooks.before_iteration()
            outcome.append(hooks.before_launch(context, _undetermined_selection(), scfg, 1))
        finally:
            hooks.store.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _scheduler_config(tmp_path: Path, wait: str) -> scheduler.SchedulerConfig:
    return scheduler.SchedulerConfig(provider="claude", prompt_text="x", cwd=str(tmp_path), max_unavailable_wait=wait, poll_interval="1")


# --- CLIUX-051 ---------------------------------------------------------------------------------

def test_capacity_data_wait_is_not_a_running_operation_and_its_deadline_does_not_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    mission_id, task_id = _running_mission(database, tmp_path)
    monkeypatch.setattr(scheduler, "_route_or_provider_decision", lambda cfg: ({}, {"usable": False, "reason": "unavailable"}))
    scfg = _scheduler_config(tmp_path, "4")
    outcome: list[bool] = []
    started = time.monotonic()
    thread = _launch_in_thread(database, mission_id, tmp_path, scfg, outcome)
    try:
        with BridgeStore(database, readonly=True) as reader:
            wait_for(lambda: bridge_monitor.project_snapshot(reader, mission_id)["waiting"] is not None, scaled(5), 0.05, message="no wait recorded")
            first = bridge_monitor.project_snapshot(reader, mission_id)
            time.sleep(1.2)
            second = bridge_monitor.project_snapshot(reader, mission_id)
        for snapshot in (first, second):
            assert snapshot["station_context"]["executing_operation"] is None  # no operation is open
            assert snapshot["current_task"]["status"] == "READY"
        assert first["waiting"]["launch_epoch"] == second["waiting"]["launch_epoch"]  # a fixed deadline, not a restarting poll
        launch = first["waiting"]["launch_epoch"]
        earlier = fmt.format_status(first, now=launch - 100)
        later = fmt.format_status(second, now=launch - 40)
        current_line = next(line for line in earlier.splitlines() if line.startswith("Current"))
        assert "RUNNING" not in current_line and "operation" not in current_line
        assert "1 READY" in earlier and "RUNNING" not in next(line for line in earlier.splitlines() if line.startswith("Tasks"))
        assert "waiting to start, no provider is running" in earlier
        assert "launches anyway in 1m" in earlier and "launches anyway in <1m" in later  # only counts down
        assert "claude has no usage data" in earlier
    finally:
        thread.join(timeout=scaled(10))
    assert outcome == [True] and time.monotonic() - started >= 3  # it launched only after the deadline
    assert scfg.unavailable_wait_done is True  # the scheduler does not start a second wait
    with BridgeStore(database, readonly=True) as reader:
        assert bridge_monitor.project_snapshot(reader, mission_id)["station_context"]["operation_status"] == "RUNNING"


def test_stop_during_the_capacity_data_wait_ends_it_at_once_with_no_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    mission_id, _task = _running_mission(database, tmp_path)
    monkeypatch.setattr(scheduler, "_route_or_provider_decision", lambda cfg: ({}, {"usable": False, "reason": "unavailable"}))
    outcome: list[bool] = []
    thread = _launch_in_thread(database, mission_id, tmp_path, _scheduler_config(tmp_path, "600"), outcome)
    time.sleep(0.6)
    with BridgeStore(database) as writer:
        writer.set_mission_status(mission_id, "STOPPING")
    thread.join(timeout=scaled(5))
    assert outcome == [False]
    with BridgeStore(database, readonly=True) as reader:
        assert reader.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


# --- CLIUX-053: invented reset time ---------------------------------------------------------------

def test_a_provider_with_no_capacity_data_gets_no_reset_time() -> None:
    now = 1_000_000.0
    decision = {"provider": "claude", "usable": False, "reason": "unavailable", "wait_until": int(now) + 40, "windows": []}
    waiting = bridge_monitor.waiting_from_wait({"all_rate_limited": False, "decisions": [decision]}, now)
    assert waiting["entries"][0]["reset_epoch"] is None
    text = fmt.format_waiting(waiting, "abcd1234", now)
    assert "resets in" not in text and "claude has no usage data" in text
    assert "resets in" not in bridge_monitor.waiting_message(waiting, now)


# --- CLIUX-053 / 054: stop --force then start -----------------------------------------------------------

def test_a_task_interrupted_by_stop_force_is_released_by_start_but_a_crashed_one_is_not(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "objective", cwd=tmp_path)
        forced = store.add_task(mission["id"], "forced")
        crashed = store.add_task(mission["id"], "crashed")
        store.set_mission_status(mission["id"], "RUNNING")
        for task in (forced, crashed):
            operation = store.begin_operation(task, iteration=1, station="claude")
            store.mark_operation_running(operation)
        # The two operations are reconciled by different causes; reconcile only the forced one first.
        store.db.execute("UPDATE operations SET status='EXITED',exit_code=1 WHERE task_id=?", (crashed,))
        store.db.execute("UPDATE tasks SET status='BLOCKED' WHERE id=?", (crashed,))
        assert store.reconcile_interrupted_operations(mission["id"], cause="stop-force") == 1
        assert store.get_task(forced)["status"] == "BLOCKED"
        released = store.release_interrupted_tasks(mission["id"])
        assert released == [store.get_task(forced)["ordinal"]]
        assert store.get_task(forced)["status"] == "READY"
        assert store.get_task(crashed)["status"] == "BLOCKED"
        assert [e["kind"] for e in store.events(mission["id"])].count("task-interrupted-released") == 1


def test_needs_you_names_the_forced_stop_and_the_start_command(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "objective", cwd=tmp_path)
        task = store.add_task(mission["id"], "job")
        store.set_mission_status(mission["id"], "RUNNING")
        store.mark_operation_running(store.begin_operation(task, iteration=1, station="claude"))
        store.reconcile_interrupted_operations(mission["id"], cause="stop-force")
        store.set_mission_status(mission["id"], "STOPPED")
        snapshot = bridge_monitor.project_snapshot(store, mission["id"])
    message = snapshot["needs_you"]["message"]
    assert f"task 1 was interrupted by stop --force · llm-bridge start --mission {str(mission['id'])[:8]} runs it again" == message
    assert "provider run ended without a result" not in message


def _call(env: dict[str, str], *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "llm_tools.bridge", *map(str, args)], env=env, text=True, capture_output=True, timeout=scaled(30))


def test_stop_force_with_no_provider_process_succeeds_and_says_so(tmp_path: Path, env: dict[str, str]) -> None:
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    database = state / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission = store.create_mission("m", "waiting without a provider process", cwd=tmp_path, config={"headless": True})
        mission_id = str(mission["id"])
        task = store.add_task(mission_id, "job")
        store.set_mission_status(mission_id, "RUNNING")
        store.mark_operation_running(store.begin_operation(task, iteration=1, station="claude"))  # no pid recorded
    runtime = state / "runtime" / f"mission-{mission_id}"
    runtime.mkdir(mode=0o700, parents=True)
    owner_code = (
        "import sys,time; from pathlib import Path; from llm_tools.bridge_store import MissionLock; "
        "lock=MissionLock(Path(sys.argv[1]),sys.argv[2]); lock.acquire(); Path(sys.argv[3]).write_text('ready'); time.sleep(30)"
    )
    owner = subprocess.Popen([sys.executable, "-c", owner_code, str(state / "locks"), mission_id, str(tmp_path / "owner.ready")], env=env, start_new_session=True)
    try:
        wait_for(lambda: (tmp_path / "owner.ready").exists(), scaled(8), 0.05, message="controller stand-in did not lock")
        (runtime / "controller-ready.json").write_text(json.dumps({
            "pid": owner.pid, "process_start": _process_start_token(owner.pid), "startup_token": "t", "status": "RUNNING",
        }), encoding="utf-8")
        stopped = _call(env, "stop", "--mission", mission_id, "--force")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert f"Stopped · no provider process was running · mission {mission_id[:8]} is STOPPED" in stopped.stdout
        assert "verifiable" not in stopped.stdout + stopped.stderr
        status = _call(env, "status", "--mission", mission_id)
        assert "interrupted by stop --force" in status.stdout
        assert f"llm-bridge start --mission {mission_id[:8]}" in status.stdout
    finally:
        if owner.poll() is None:
            owner.send_signal(signal.SIGKILL)
            owner.wait(timeout=scaled(3))


@pytest.mark.subprocess
@pytest.mark.slow
def test_start_after_stop_force_runs_the_interrupted_task_again(tmp_path: Path, env: dict[str, str]) -> None:
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({"codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}})
    runs = tmp_path / "runs.txt"
    provider = tmp_path / "provider.py"
    provider.write_text(f"import time\nopen({str(runs)!r}, 'a').write('run\\n')\ntime.sleep(60)\n")
    created = json.loads(_call(env, "mission", "create", "--prompt", "Resume me", "--cwd", tmp_path, "--providers", "codex", "--headless",
                               "--command-template", shlex.join([sys.executable, str(provider)]), "--json").stdout)
    mission_id = created["mission"]["id"]

    def count() -> int:
        return len(runs.read_text().splitlines()) if runs.exists() else 0

    try:
        assert _call(env, "start", "--mission", mission_id, "--headless").returncode == 0
        wait_for(lambda: count() == 1, scaled(20), 0.1, message="provider did not start")
        stopped = _call(env, "stop", "--mission", mission_id, "--force")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert _call(env, "start", "--mission", mission_id, "--headless").returncode == 0
        wait_for(lambda: count() == 2, scaled(20), 0.1, message="the interrupted task did not run again after start")
    finally:
        _call(env, "stop", "--mission", mission_id, "--force")


# --- CLIUX-062 -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["RUNNING", "PAUSED"])
def test_a_mission_with_no_controller_is_not_shown_as_plainly_running(tmp_path: Path, status: str) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "objective", cwd=tmp_path, config={"headless": True})
        store.add_task(mission["id"], "job")
        store.set_mission_status(mission["id"], "RUNNING")
        if status == "PAUSED":
            store.set_mission_status(mission["id"], "PAUSED")
        snapshot = bridge_monitor.project_snapshot(store, mission["id"])
    snapshot["lifecycle"]["controller_running"] = False
    text = fmt.format_status(snapshot)
    short = str(mission["id"])[:8]
    assert f'"m" · INTERRUPTED (was {status}, headless)' in text
    assert f"Controller  not running (stopped unexpectedly) — run: llm-bridge start --mission {short}" in text
    listing = fmt.format_mission_list([{**snapshot["mission"], "controller_running": False}])
    assert f"INTERRUPTED (was {status})" in listing and "no controller" not in listing


def test_a_provider_that_is_not_signed_in_is_named_in_needs_you(tmp_path: Path) -> None:
    now = time.time()
    decision = {"provider": "claude", "usable": False, "reason": "not-authenticated", "wait_until": int(now) + 40, "windows": []}
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "objective", cwd=tmp_path, config={"headless": True})
        store.add_task(mission["id"], "job")
        store.set_mission_status(mission["id"], "RUNNING")
        store.record_runtime_event(mission["id"], "provider-wait", {"target_epoch": int(now) + 900, "all_rate_limited": False, "decisions": [decision]})
        snapshot = bridge_monitor.project_snapshot(store, mission["id"])
    snapshot["lifecycle"]["controller_running"] = True
    needs = next(line for line in fmt.format_status(snapshot, now=now).splitlines() if line.startswith("Needs you"))
    assert "claude is not signed in" in needs and "llm-bridge doctor" in needs and "launches anyway in 14m" in needs
