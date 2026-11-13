"""Bridge-aware Ralph and scheduler waits poll control requests promptly."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from llm_tools import common, ralph_robin, scheduler
from llm_tools.bridge_power import _root, _write_plan, shared_wake_target, update_wake_request
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore, MissionLock
from tests.conftest import scaled


def test_ralph_wall_clock_wait_returns_on_operator_request(monkeypatch) -> None:
    calls = 0

    def interrupt() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    monkeypatch.setattr(ralph_robin, "sleep_seconds", lambda _seconds: time.sleep(0.01))
    started = time.monotonic()

    assert ralph_robin.wait_until_epoch(common.now_epoch() + 60, interrupt) is False
    assert calls == 3, "the 60 second wait must stop at the third interrupt poll"
    assert time.monotonic() - started < scaled(5)


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


def test_opted_in_bridge_capacity_wait_uses_rtc_suspend_path(monkeypatch, tmp_path) -> None:
    logs = common.RunLogs(run_dir=tmp_path, text_log=tmp_path / "run.log", event_log=tmp_path / "events.jsonl")
    target = common.now_epoch() + 600
    outcomes = []

    def suspend(target_epoch: int, **kwargs: object) -> common.SuspendOutcome:
        outcomes.append(target_epoch)
        return common.SuspendOutcome(True, target_epoch, target_epoch, 0, True, "rtcwake", "ok", sleep_observed=True)

    monkeypatch.setattr(common, "suspend_with_wake", suspend)
    result = ralph_robin.suspend_machine_until(
        ralph_robin.RalphConfig(), logs, target, time.monotonic(), 0,
        ralph_robin.SuspendState(), lambda: False, lambda: True,
    )

    assert result is True
    assert outcomes == [target]


def test_opted_in_bridge_wait_uses_shared_prearmed_target_and_records_cycle(monkeypatch, tmp_path) -> None:
    logs = common.RunLogs(run_dir=tmp_path, text_log=tmp_path / "run.log", event_log=tmp_path / "events.jsonl")
    original_target = common.now_epoch() + 600
    shared_target = original_target - 120
    arm = common.WakeArm(True, "systemd-timer", False, unit="shared-wake", target_epoch=shared_target)
    planned = []
    outcomes = []
    events = []

    def prepare(target: int):
        planned.append(target)
        return shared_target, arm

    def suspend(target: int, **kwargs: object) -> common.SuspendOutcome:
        outcomes.append((target, kwargs.get("wake_arm")))
        return common.SuspendOutcome(True, target + 2, target, 2, True, "systemd-timer", "ok", sleep_observed=True)

    monkeypatch.setattr(common, "suspend_with_wake", suspend)
    result = ralph_robin.suspend_machine_until(
        ralph_robin.RalphConfig(), logs, original_target, time.monotonic(), 0,
        ralph_robin.SuspendState(), lambda: False, lambda: True,
        lambda kind, data: events.append((kind, data)), prepare,
    )

    assert result is True
    assert planned == [original_target]
    assert outcomes == [(shared_target, arm)]
    assert events[0][0] == "suspend-decision"
    assert events[0][1]["branch"] == "suspend"
    events = events[1:]
    assert [kind for kind, _data in events] == ["suspend-intent", "suspend-outcome", "suspend-work-resumed"]
    assert all(data["cycle_id"].startswith("ralph-robin-") for _kind, data in events)
    assert events[0][1]["wake_armed"] is True
    assert events[0][1]["wake_method"] == "systemd-timer"
    assert events[0][1]["wake_confirmed"] is False
    assert events[1][1]["woke_epoch"] == shared_target + 2
    assert events[1][1]["drift_seconds"] == 2
    assert events[1][1]["actual_sleep"] is True


def test_automatic_bridge_suspend_outcome_reconciles_later_owner(tmp_path, monkeypatch) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path), "LLM_USAGE_NOW_EPOCH": "1000"}
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
        hooks = BridgeMissionHooks(mission_id, handle=None, headless=True, controller_logs=tmp_path)
        hooks.bind(store)
        armed: list[int] = []
        monkeypatch.setattr(
            common, "arm_rtc_wake",
            lambda target, who, _env: armed.append(target) or common.WakeArm(True, "systemd-timer", False, unit=f"{who}.timer", target_epoch=target),
        )
        monkeypatch.setattr(common, "disarm_rtc_wake", lambda *_args: None)
        monkeypatch.setattr("llm_tools.bridge_power.subprocess.run", lambda *_args, **_kwargs: type("Proc", (), {"returncode": 1})())
        update_wake_request("first-owner", 4000, env=env)
        update_wake_request("later-owner", 6000, env=env)
        _write_plan(_root(env), None, None, 1000)  # common suspend consumed the 4000 wake
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "4000")

        hooks.on_suspend_event("suspend-outcome", {"cycle_id": "cycle", "reason": "ok"})

        assert shared_wake_target(env) == 6000
        assert armed == [4000, 4000, 6000]


def test_opted_in_bridge_capacity_wait_respects_pending_control(monkeypatch, tmp_path) -> None:
    logs = common.RunLogs(run_dir=tmp_path, text_log=tmp_path / "run.log", event_log=tmp_path / "events.jsonl")
    monkeypatch.setattr(common, "suspend_with_wake", lambda *_args, **_kwargs: pytest.fail("must not suspend"))
    result = ralph_robin.suspend_machine_until(
        ralph_robin.RalphConfig(), logs, common.now_epoch() + 600, time.monotonic(), 0,
        ralph_robin.SuspendState(), lambda: True, lambda: True,
    )

    assert result is True


def test_opted_in_bridge_capacity_wait_stays_awake_when_wake_cannot_arm(monkeypatch, tmp_path) -> None:
    logs = common.RunLogs(run_dir=tmp_path, text_log=tmp_path / "run.log", event_log=tmp_path / "events.jsonl")
    target = common.now_epoch() + 600
    interrupted = lambda: False
    waits = []
    monkeypatch.setattr(
        common, "suspend_with_wake",
        lambda *_args, **_kwargs: common.SuspendOutcome(False, None, target, None, False, "none", "arm-failed"),
    )
    monkeypatch.setattr(ralph_robin, "wait_until_epoch", lambda epoch, callback=None: waits.append((epoch, callback)))
    state = ralph_robin.SuspendState()

    result = ralph_robin.suspend_machine_until(
        ralph_robin.RalphConfig(), logs, target, time.monotonic(), 0,
        state, interrupted, lambda: True,
    )

    assert result is True
    assert state.disabled and state.disabled_reason == "arm-failed"
    assert waits == [(target, interrupted)]


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
        # A hang bound, not a speed assertion. `stop` of a finished mission waits up to
        # TERMINAL_STOP_LOCK_WAIT_SECONDS (15 s) for its controller to exit, and a call took up to 5 s
        # in measurements at a load average of 45 on 8 cores, so 10 s was shorter than the product's
        # own wait.
        result = subprocess.run(
            [sys.executable, "-m", "llm_tools.bridge", *map(str, args)],
            env=env, text=True, capture_output=True, timeout=scaled(40),
        )
        assert result.returncode == 0, result.stdout + result.stderr

    created = subprocess.run(
        [sys.executable, "-m", "llm_tools.bridge", "mission", "create", "--prompt", "capacity wait",
         "--cwd", str(tmp_path), "--headless", "--providers", "codex", "--command-template", template, "--json"],
        env=env, text=True, capture_output=True, timeout=scaled(10),
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

    def controller_running(mission_id: str) -> bool:
        return MissionLock.is_held(state / "locks", mission_id)

    def wait_for(predicate, timeout: float = 6) -> None:
        deadline = time.monotonic() + scaled(timeout)
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
        call("pause", "--mission", mission_id)
        # The bound is the controller's reaction to the request. The `pause` process itself needs
        # a Python start-up and an import of the Bridge, which is several seconds on a loaded host,
        # so the clock starts when the command has returned.
        started = time.monotonic()
        wait_for(lambda: current()[0] == "PAUSED")
        assert time.monotonic() - started < scaled(2.0)
        assert current() == ("PAUSED", "READY", "", None)
        assert not (tmp_path / "ran").exists()
        call("stop", "--mission", mission_id)
        wait_for(lambda: current()[0] == "STOPPED")
        # The detached controller records STOPPED before it exits. Leaving the test before it has
        # exited leaves a live process for the process-hygiene check.
        wait_for(lambda: not controller_running(mission_id), timeout=30)
    finally:
        try:
            if controller_running(mission_id):
                call("stop", "--force", "--mission", mission_id)
        except (AssertionError, subprocess.SubprocessError):
            pass
