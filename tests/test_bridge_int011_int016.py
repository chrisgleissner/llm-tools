"""INT-011 event payload cap, INT-016 verifier interruption and main-loop progress in the heartbeat."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from llm_tools import bridge_runtime
from llm_tools.bridge_refresh import CapacityRefreshWorker, ProgressCounter
from llm_tools.bridge_runtime import BridgeMissionHooks, run_configured_verifier
from llm_tools.bridge_store import EVENT_PAYLOAD_BYTES, BridgeStore
from tests.conftest import scaled, wait_for


def test_int011_event_payload_over_the_cap_is_stored_as_short_fields_and_size(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        store._event(mission["id"], "probe-small", {"note": "short"})
        store._event(mission["id"], "probe-large", {"note": "short", "blob": "x" * (EVENT_PAYLOAD_BYTES + 1)})
        events = {event["kind"]: event["data"] for event in store.events(mission["id"])}
    assert events["probe-small"] == {"note": "short"}
    assert events["probe-large"]["note"] == "short"
    assert events["probe-large"]["truncated"] is True
    assert events["probe-large"]["payload_bytes"] > EVENT_PAYLOAD_BYTES
    assert "blob" not in events["probe-large"]


def _slow_verifier_mission(tmp_path: Path, pid_file: Path) -> tuple[BridgeStore, str, int]:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    script = tmp_path / "slow_verifier.py"
    script.write_text(
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=cwd, config={"verifier": {"argv": [sys.executable, str(script)], "timeout_seconds": 300}})
    store.set_mission_status(mission["id"], "RUNNING")
    task_id = store.add_task(mission["id"], "work")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    return store, mission["id"], task_id


@pytest.mark.parametrize("new_status", ["STOPPING", "CANCELLED"])
def test_int016_stopping_or_cancelled_mission_interrupts_a_running_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_status: str
) -> None:
    monkeypatch.setattr(bridge_runtime, "VERIFIER_STOP_CHECK_SECONDS", 0.05)
    pid_file = tmp_path / "verifier.pid"
    store, mission_id, task_id = _slow_verifier_mission(tmp_path, pid_file)
    ticks = ProgressCounter()

    def request_stop() -> None:
        wait_for(pid_file.exists, 10, 0.02, "verifier started")
        with BridgeStore(tmp_path / "bridge.sqlite3") as other:
            other.set_mission_status(mission_id, new_status)

    thread = threading.Thread(target=request_stop)
    thread.start()
    try:
        outcome = run_configured_verifier(store, mission_id, task_id, runtime=tmp_path / "runtime", progress=ticks)
    finally:
        thread.join(5)
    assert outcome["reason"] == "interrupted"
    assert outcome["passed"] is False
    assert outcome["elapsed_seconds"] < scaled(30)  # the verifier sleeps for 120 s
    verifier_pid = int(pid_file.read_text())
    wait_for(lambda: not Path(f"/proc/{verifier_pid}").exists() or Path(f"/proc/{verifier_pid}/stat").read_text().split(") ")[1][0] == "Z", 5, 0.05, "verifier process ended")
    assert ticks.value >= 1, "the verifier wait reports that the main loop is alive"
    store.close()


def test_int016_a_verifier_of_a_running_mission_is_not_interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_runtime, "VERIFIER_STOP_CHECK_SECONDS", 0.05)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    code = "import json,time; time.sleep(0.4); print(json.dumps({'passed': True, 'summary': 'ok'}))"
    mission = store.create_mission("mission", "objective", cwd=cwd, config={"verifier": {"argv": [sys.executable, "-c", code], "timeout_seconds": 30}})
    store.set_mission_status(mission["id"], "RUNNING")
    task_id = store.add_task(mission["id"], "work")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    assert run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")["passed"] is True
    store.close()


def test_int016_heartbeat_carries_the_main_loop_progress_counter(tmp_path: Path) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
    progress = ProgressCounter()
    worker = CapacityRefreshWorker(
        mission["id"], database, interval_seconds=5, heartbeat_seconds=1,
        initial_refresh_delay_seconds=30, selector=lambda _m: {}, progress=progress,
    )
    worker.start()

    def heartbeats() -> list[int]:
        with BridgeStore(database) as store:
            return [int(event["data"]["main_loop_progress"]) for event in store.events(mission["id"]) if event["kind"] == "controller-heartbeat"]

    try:
        wait_for(lambda: len(heartbeats()) >= 1, 15)
        first = heartbeats()[-1]
        progress.tick()
        progress.tick()
        wait_for(lambda: heartbeats()[-1] >= first + 2, 15, 0.1, "heartbeat reports the ticked counter")
    finally:
        worker.stop()


def test_int016_hooks_tick_the_counter_in_the_main_loop(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("m", "o", cwd=tmp_path)
    store.set_mission_status(mission["id"], "RUNNING")
    store.set_mission_status(mission["id"], "STOPPING")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    before = hooks.progress.value
    assert hooks.before_iteration()["action"] == "stop"
    assert hooks.progress.value == before + 1
    store.close()
