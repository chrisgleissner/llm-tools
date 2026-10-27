"""Bridge-aware Ralph and scheduler waits poll control requests promptly."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from llm_tools import common, ralph_robin, scheduler
from llm_tools.bridge_store import BridgeStore


def test_ralph_wall_clock_wait_returns_on_operator_request(monkeypatch) -> None:
    calls = 0

    def interrupt() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    monkeypatch.setattr(ralph_robin, "sleep_seconds", lambda _seconds: time.sleep(0.01))
    started = time.monotonic()

    assert ralph_robin.wait_until_epoch(common.now_epoch() + 60, interrupt) is False
    assert time.monotonic() - started < 0.5


def test_bridge_cross_provider_wait_stays_awake_and_interruptible(monkeypatch, tmp_path) -> None:
    calls = 0
    logs = common.RunLogs(run_dir=tmp_path, text_log=tmp_path / "run.log", event_log=tmp_path / "events.jsonl")

    def interrupt() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    monkeypatch.setattr(ralph_robin, "sleep_seconds", lambda _seconds: time.sleep(0.01))
    result = ralph_robin.suspend_machine_until(
        ralph_robin.RalphConfig(), logs, common.now_epoch() + 60, time.monotonic(), 0,
        ralph_robin.SuspendState(), interrupt,
    )

    assert result is True
    assert calls >= 2


def test_scheduler_capacity_wait_returns_special_boundary_status(monkeypatch, tmp_path) -> None:
    calls = 0

    def interrupt() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    config = scheduler.SchedulerConfig(
        provider="codex", scope="5h", should_interrupt_wait=interrupt,
        not_before_epoch=common.now_epoch() + 60,
    )
    logs = common.RunLogs(run_dir=tmp_path, text_log=tmp_path / "run.log", event_log=tmp_path / "events.jsonl")

    try:
        scheduler.wait_until_usable(config, logs)
    except SystemExit as exc:
        assert exc.code == 143
    else:
        raise AssertionError("operator request should interrupt readiness wait")


def test_detached_pause_interrupts_long_provider_capacity_wait(tmp_path: Path, env: dict[str, str]) -> None:
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    blocked = {"available": True, "five_hour": {"remaining": 0, "reset_epoch": int(time.time()) + 86400}}
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({"codex": blocked})
    provider = tmp_path / "provider.py"
    provider.write_text("from pathlib import Path\nPath('ran').write_text('yes')\n", encoding="utf-8")
    template = shlex.join([sys.executable, str(provider)])

    def call(*args: object) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "llm_tools.bridge", *map(str, args)],
            env=env, text=True, capture_output=True, timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    created = subprocess.run(
        [sys.executable, "-m", "llm_tools.bridge", "mission", "create", "--prompt", "capacity wait",
         "--cwd", str(tmp_path), "--headless", "--providers", "codex", "--command-template", template, "--json"],
        env=env, text=True, capture_output=True, timeout=10,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    mission = json.loads(created.stdout)["mission"]
    mission_id = str(mission["id"])
    task_id = int(json.loads(created.stdout)["task"]["id"])
    database = state / "bridge.sqlite3"

    def current() -> tuple[str, str, str, int | None]:
        with BridgeStore(database) as store:
            operation = store.db.execute("SELECT status,exit_code FROM operations WHERE mission_id=? ORDER BY rowid DESC LIMIT 1", (mission_id,)).fetchone()
            return (
                store.get_mission(mission_id)["status"],
                store.get_task(task_id)["status"],
                str(operation["status"]) if operation else "",
                int(operation["exit_code"]) if operation and operation["exit_code"] is not None else None,
            )

    def wait_for(predicate, timeout: float = 6) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        assert predicate(), "Bridge capacity wait did not reach expected lifecycle state"

    def waiting_event() -> bool:
        with BridgeStore(database) as store:
            return any(event["kind"] == "provider-wait" for event in store.events(mission_id))

    try:
        call("start", "--mission", mission_id, "--headless")
        wait_for(waiting_event)
        started = time.monotonic()
        call("pause", "--mission", mission_id)
        wait_for(lambda: current()[0] == "PAUSED")
        assert time.monotonic() - started < 2.0
        assert current() == ("PAUSED", "READY", "", None)
        assert not (tmp_path / "ran").exists()
        call("stop", "--mission", mission_id)
        wait_for(lambda: current()[0] == "STOPPED")
    finally:
        try:
            call("stop", "--mission", mission_id)
        except (AssertionError, subprocess.SubprocessError):
            pass
