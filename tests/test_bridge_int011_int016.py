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



def test_int011_event_payload_over_the_cap_shortens_values_and_keeps_every_field(tmp_path: Path) -> None:
    long_list = [f"item-{n}" * 20 for n in range(5000)]
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        store._event(mission["id"], "probe-small", {"note": "short"})
        store._event(mission["id"], "probe-large", {"note": "short", "message": "m" * (EVENT_PAYLOAD_BYTES + 1), "items": long_list, "count": 7})
        events = {event["kind"]: event["data"] for event in store.events(mission["id"])}
    assert events["probe-small"] == {"note": "short"}
    large = events["probe-large"]
    assert large["truncated"] is True and large["payload_bytes"] > EVENT_PAYLOAD_BYTES
    assert large["note"] == "short" and large["count"] == 7
    assert large["message"].startswith("m" * 100) and len(large["message"]) < 2100  # the text survives, shortened
    assert large["items"].startswith('["item-0') and "truncated" in large["items"]  # a cut JSON summary, not dropped


def test_int011_multibyte_payload_over_the_cap_in_bytes_is_capped_although_its_character_count_is_not(tmp_path: Path) -> None:
    text = "\u00e4" * (EVENT_PAYLOAD_BYTES // 2 + 10)  # fewer characters than the cap, more bytes
    assert len(text) < EVENT_PAYLOAD_BYTES < len(text.encode("utf-8"))
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        store._event(mission["id"], "probe-multibyte", {"message": text})
        data = next(event["data"] for event in store.events(mission["id"]) if event["kind"] == "probe-multibyte")
    assert data["truncated"] is True


SLOW_VERIFIER = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, lambda *_: (print('{\"passed\": true, \"summary\": \"trapped\"}'), sys.exit(0)) if TRAP else sys.exit(143))\n"
    "open(PID_FILE, 'w').write(str(os.getpid()))\n"
    "time.sleep(SLEEP)\n"
    "print('{\"passed\": true, \"summary\": \"finished\"}')\n"
)


def _verifier_mission(tmp_path: Path, pid_file: Path, *, sleep: float = 120, trap: bool = False) -> tuple[BridgeStore, str, int]:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    script = tmp_path / "slow_verifier.py"
    script.write_text(f"PID_FILE = {str(pid_file)!r}\nSLEEP = {sleep}\nTRAP = {trap}\n" + SLOW_VERIFIER, encoding="utf-8")
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=cwd, config={"verifier": {"argv": [sys.executable, str(script)], "timeout_seconds": 300}})
    store.set_mission_status(mission["id"], "RUNNING")
    task_id = store.add_task(mission["id"], "work")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    return store, mission["id"], task_id


def _set_status_when_started(tmp_path: Path, pid_file: Path, mission_id: str, status: str) -> threading.Thread:
    def request() -> None:
        wait_for(pid_file.exists, 10, 0.02, "verifier started")
        with BridgeStore(tmp_path / "bridge.sqlite3") as other:
            other.set_mission_status(mission_id, status)

    thread = threading.Thread(target=request)
    thread.start()
    return thread


def test_int016_cancelled_mission_interrupts_a_running_verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_runtime, "VERIFIER_STOP_CHECK_SECONDS", 0.05)
    pid_file = tmp_path / "verifier.pid"
    store, mission_id, task_id = _verifier_mission(tmp_path, pid_file)
    ticks = ProgressCounter()
    thread = _set_status_when_started(tmp_path, pid_file, mission_id, "CANCELLED")
    try:
        outcome = run_configured_verifier(store, mission_id, task_id, runtime=tmp_path / "runtime", progress=ticks)
    finally:
        thread.join(5)
    assert outcome["reason"] == "interrupted" and outcome["passed"] is False
    assert outcome["elapsed_seconds"] < scaled(30)  # the verifier sleeps for 120 s
    verifier_pid = int(pid_file.read_text())
    wait_for(lambda: not Path(f"/proc/{verifier_pid}").exists() or Path(f"/proc/{verifier_pid}/stat").read_text().split(") ")[1][0] == "Z", 5, 0.05, "verifier process ended")
    assert ticks.value >= 1, "the verifier wait reports that the main loop is alive"
    assert store.get_task(task_id)["status"] == "VERIFYING"
    store.close()


def test_int016_interrupted_verifier_is_never_a_pass_even_when_it_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_runtime, "VERIFIER_STOP_CHECK_SECONDS", 0.05)
    pid_file = tmp_path / "verifier.pid"
    store, mission_id, task_id = _verifier_mission(tmp_path, pid_file, trap=True)  # prints passed:true and exits 0 on SIGTERM
    thread = _set_status_when_started(tmp_path, pid_file, mission_id, "CANCELLED")
    try:
        outcome = run_configured_verifier(store, mission_id, task_id, runtime=tmp_path / "runtime")
    finally:
        thread.join(5)
    assert outcome["reason"] == "interrupted" and outcome["passed"] is False
    assert outcome["exit_code"] == 0
    assert store.get_task(task_id)["status"] == "VERIFYING"
    store.close()


def test_int016_graceful_stop_lets_the_verifier_finish_and_the_task_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_runtime, "VERIFIER_STOP_CHECK_SECONDS", 0.05)
    pid_file = tmp_path / "verifier.pid"
    store, mission_id, task_id = _verifier_mission(tmp_path, pid_file, sleep=1.0)
    thread = _set_status_when_started(tmp_path, pid_file, mission_id, "STOPPING")
    try:
        outcome = run_configured_verifier(store, mission_id, task_id, runtime=tmp_path / "runtime")
    finally:
        thread.join(5)
    assert outcome["passed"] is True and outcome["summary"] == "finished"
    assert store.get_task(task_id)["status"] == "COMPLETED"
    assert store.get_mission(mission_id)["status"] == "STOPPING"
    store.close()


def test_int016_no_verifier_is_started_for_a_cancelled_mission(tmp_path: Path) -> None:
    pid_file = tmp_path / "verifier.pid"
    store, mission_id, task_id = _verifier_mission(tmp_path, pid_file, sleep=1.0)
    store.set_mission_status(mission_id, "CANCELLED")
    outcome = run_configured_verifier(store, mission_id, task_id, runtime=tmp_path / "runtime")
    assert outcome["reason"] == "mission-cancelled"
    assert not pid_file.exists()
    store.close()


def test_int016_verify_request_stays_in_config_when_its_verifier_was_interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_runtime, "VERIFIER_STOP_CHECK_SECONDS", 0.05)
    pid_file = tmp_path / "verifier.pid"
    store, mission_id, task_id = _verifier_mission(tmp_path, pid_file)
    store.set_mission_config_value(mission_id, "verification_requests", [task_id], event_kind="steering-verify-requested")
    hooks = BridgeMissionHooks(mission_id, handle=None, headless=True, controller_logs=tmp_path / "runtime" / "controller.log")
    hooks.bind(store)
    thread = _set_status_when_started(tmp_path, pid_file, mission_id, "CANCELLED")
    try:
        hooks._apply_pending_boundary_requests()
    finally:
        thread.join(5)
    assert store.get_mission(mission_id)["config"]["verification_requests"] == [task_id]
    kinds = [event["kind"] for event in store.events(mission_id)]
    assert "steering-verify-applied" not in kinds and "steering-verify-drained" not in kinds
    store.close()


def test_int016_a_verifier_of_a_running_mission_is_not_interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_runtime, "VERIFIER_STOP_CHECK_SECONDS", 0.05)
    pid_file = tmp_path / "verifier.pid"
    store, mission_id, task_id = _verifier_mission(tmp_path, pid_file, sleep=0.4)
    assert run_configured_verifier(store, mission_id, task_id, runtime=tmp_path / "runtime")["passed"] is True
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
