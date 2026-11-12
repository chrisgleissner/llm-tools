"""The all-providers-blocked wait must use the central suspend path for a known reset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from llm_tools import common, ralph_robin
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore
from tests.test_bridge_quota_recovery_e2e import _codex_command, _ready_payload


def _logs(tmp_path: Path) -> common.RunLogs:
    return common.setup_run_logs(tmp_path / "logs", "test")


def _events(logs: common.RunLogs) -> list[dict[str, Any]]:
    return [json.loads(line) for line in logs.event_log.read_text(encoding="utf-8").splitlines() if line.strip()]


class Recorder:
    """Records the order of plan, suspend and awake-wait calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, outcome: common.SuspendOutcome | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.outcome = outcome
        monkeypatch.setattr(common, "suspend_with_wake", self._suspend)
        monkeypatch.setattr(
            ralph_robin, "wait_until_epoch",
            lambda epoch, callback=None: self.calls.append(("wait", int(epoch))) or True,
        )

    def _suspend(self, target: int, **kwargs: Any) -> common.SuspendOutcome:
        self.calls.append(("suspend", target))
        return self.outcome or common.SuspendOutcome(True, target, target, 0, True, "fixture", "ok", sleep_observed=True)

    def plan(self, target: int) -> tuple[int, common.WakeArm]:
        self.calls.append(("plan", target))
        return target, common.WakeArm(True, "fixture", True, unit="fixture", target_epoch=target)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]


def _known_selection(target: int) -> dict[str, Any]:
    return {"decisions": [{"provider": "codex", "reason": "rate-limited", "wait_until": target}]}


def test_known_reset_uses_central_suspend_once_after_arming(monkeypatch, tmp_path) -> None:
    logs = _logs(tmp_path)
    rec = Recorder(monkeypatch)
    target = common.now_epoch() + 3600
    events: list[tuple[str, dict[str, Any]]] = []

    assert ralph_robin.suspend_until_available(
        ralph_robin.RalphConfig(), logs, _known_selection(target), ralph_robin.monotonic(), 0,
        "all-providers-failed", lambda: False,
        state=ralph_robin.SuspendState(), suspend_allowed=lambda: True,
        suspend_event=lambda kind, data: events.append((kind, data)), suspend_plan=rec.plan,
    ) is True

    assert rec.calls == [("plan", target), ("suspend", target)]
    assert [kind for kind, _ in events if kind != "suspend-decision"] == [
        "suspend-intent", "suspend-outcome", "suspend-work-resumed",
    ]


def test_known_reset_without_bridge_uses_central_suspend(monkeypatch, tmp_path) -> None:
    rec = Recorder(monkeypatch)
    target = common.now_epoch() + 3600

    assert ralph_robin.suspend_until_available(
        ralph_robin.RalphConfig(), _logs(tmp_path), _known_selection(target), ralph_robin.monotonic(), 0,
        "all-providers-autonomy-abort", state=ralph_robin.SuspendState(),
    ) is True

    assert rec.calls == [("suspend", target)]


def test_stale_selection_is_refreshed_before_choosing_the_branch(monkeypatch, tmp_path) -> None:
    rec = Recorder(monkeypatch)
    target = common.now_epoch() + 1800
    refreshed: list[bool] = []
    stale = {"decisions": [{"provider": "codex", "usable": True}]}

    def refresh() -> dict[str, Any]:
        refreshed.append(True)
        return _known_selection(target)

    assert ralph_robin.suspend_until_available(
        ralph_robin.RalphConfig(), _logs(tmp_path), stale, ralph_robin.monotonic(), 0,
        "all-providers-failed", state=ralph_robin.SuspendState(), refresh_selection=refresh,
    ) is True

    assert refreshed == [True]
    assert rec.calls == [("suspend", target)]


def test_fresh_selection_is_not_refreshed_again(monkeypatch, tmp_path) -> None:
    Recorder(monkeypatch)
    target = common.now_epoch() + 1800

    def refresh() -> dict[str, Any]:
        raise AssertionError("an all-skipped selection was just read and must not be read again")

    ralph_robin.suspend_until_available(
        ralph_robin.RalphConfig(), _logs(tmp_path), _known_selection(target), ralph_robin.monotonic(), 0,
        "all-providers-skipped", state=ralph_robin.SuspendState(), refresh_selection=refresh,
    )


def test_unknown_reset_waits_awake_with_logged_reason_and_never_claims_suspend(monkeypatch, tmp_path, capsys) -> None:
    logs = _logs(tmp_path)
    rec = Recorder(monkeypatch)

    assert ralph_robin.suspend_until_available(
        ralph_robin.RalphConfig(), logs, {"decisions": [{"provider": "codex", "reason": "inconclusive-usage"}]},
        ralph_robin.monotonic(), 0, "all-providers-failed", state=ralph_robin.SuspendState(),
    ) is True

    assert rec.kinds() == ["wait"]
    wait_events = [event for event in _events(logs) if event["type"] == "all_blocked_wait"]
    assert len(wait_events) == 1
    assert wait_events[0]["data"]["reason_code"] == "unknown-recovery-time"
    assert wait_events[0]["data"]["awake_for_control"] is False
    assert not [event for event in _events(logs) if event["type"] == "all_blocked_suspend"]
    assert "suspending" not in capsys.readouterr().err


def test_policy_off_waits_awake_without_suspending(monkeypatch, tmp_path) -> None:
    logs = _logs(tmp_path)
    rec = Recorder(monkeypatch)
    target = common.now_epoch() + 3600
    events: list[tuple[str, dict[str, Any]]] = []

    assert ralph_robin.suspend_until_available(
        ralph_robin.RalphConfig(), logs, _known_selection(target), ralph_robin.monotonic(), 0,
        "all-providers-failed", lambda: False,
        state=ralph_robin.SuspendState(), suspend_allowed=lambda: False,
        suspend_event=lambda kind, data: events.append((kind, data)), suspend_plan=rec.plan,
    ) is True

    assert rec.calls == [("wait", target)]
    decisions = [data for kind, data in events if kind == "suspend-decision"]
    assert decisions and decisions[0]["branch"] == "awake"
    assert decisions[0]["reason_code"] == "bridge-suspend-not-eligible"


def test_refused_wake_falls_back_to_awake_wait_and_latches(monkeypatch, tmp_path) -> None:
    target = common.now_epoch() + 3600
    rec = Recorder(monkeypatch, outcome=common.SuspendOutcome(False, None, target, None, False, "none", "arm-failed"))
    state = ralph_robin.SuspendState()

    assert ralph_robin.suspend_until_available(
        ralph_robin.RalphConfig(), _logs(tmp_path), _known_selection(target), ralph_robin.monotonic(), 0,
        "all-providers-failed", state=state,
    ) is True

    assert rec.calls == [("suspend", target), ("wait", target)]
    assert state.disabled and state.disabled_reason == "arm-failed"


def test_exhausted_duration_budget_stops_without_suspending(monkeypatch, tmp_path) -> None:
    rec = Recorder(monkeypatch)
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 100.0)

    assert ralph_robin.suspend_until_available(
        ralph_robin.RalphConfig(), _logs(tmp_path), _known_selection(common.now_epoch() + 3600), 0.0, 10,
        "all-providers-failed", state=ralph_robin.SuspendState(),
    ) is False

    assert rec.calls == []


def test_legacy_call_without_state_stays_an_honest_awake_wait(monkeypatch, tmp_path, capsys) -> None:
    logs = _logs(tmp_path)
    rec = Recorder(monkeypatch)
    target = common.now_epoch() + 3600

    assert ralph_robin.suspend_until_available(
        ralph_robin.RalphConfig(), logs, _known_selection(target), ralph_robin.monotonic(), 0, "rate-limited",
    ) is True

    assert rec.calls == [("wait", target)]
    event = [event for event in _events(logs) if event["type"] == "all_blocked_wait"][0]
    assert event["data"]["reason_code"] == "no-suspend-state"
    assert "suspending" not in capsys.readouterr().err


# --- end to end through ralph_robin.main -------------------------------------------------------


def _failing_provider(tmp_path: Path, usage_file: Path, *, exhausted_reset: int | None) -> tuple[str, Path]:
    attempts = tmp_path / "attempts.jsonl"
    exhausted = (
        json.dumps({"codex": {"available": True, "five_hour": {"remaining": 0, "resets_at": exhausted_reset},
                               "week": {"remaining": 0, "resets_at": exhausted_reset}}})
        if exhausted_reset is not None else None
    )
    script = f"""from pathlib import Path
import json, sys
attempts = Path({str(attempts)!r})
number = len(attempts.read_text().splitlines()) + 1 if attempts.exists() else 1
with attempts.open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
exhausted = {exhausted!r}
if number == 1:
    if exhausted is not None:
        Path({str(usage_file)!r}).write_text(exhausted, encoding='utf-8')
    print(json.dumps({{'type': 'error', 'message': 'You\\u2019ve reached your usage limit. Please wait for limit to reset.'}}), flush=True)
    raise SystemExit(75)
print(json.dumps({{'type': 'item.completed', 'item': {{'type': 'agent_message', 'text': 'done'}}}}), flush=True)
"""
    _binary, template = _codex_command(tmp_path, script)
    return template, attempts


def _run_mission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, exhausted_reset: int | None, suspend_allowed: bool, amend: bool,
) -> tuple[int, list[tuple[str, int]], Path, str, int]:
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps({"codex": _ready_payload()}), encoding="utf-8")
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON_FILE", str(usage_file))
    monkeypatch.delenv("LLM_USAGE_NOW_EPOCH", raising=False)
    template, attempts = _failing_provider(tmp_path, usage_file, exhausted_reset=exhausted_reset)
    calls: list[tuple[str, int]] = []

    def suspend(target: int, **kwargs: Any) -> common.SuspendOutcome:
        calls.append(("suspend", target))
        usage_file.write_text(json.dumps({"codex": _ready_payload()}), encoding="utf-8")
        return common.SuspendOutcome(True, target, target, 0, True, "fixture", "ok", sleep_observed=True)

    def wait(epoch: int, callback: Any = None) -> bool:
        calls.append(("wait", int(epoch)))
        usage_file.write_text(json.dumps({"codex": _ready_payload()}), encoding="utf-8")
        return True

    monkeypatch.setattr(common, "suspend_with_wake", suspend)
    monkeypatch.setattr(ralph_robin, "wait_until_epoch", wait)

    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission = store.create_mission("quota", "objective", cwd=tmp_path)
        task_id = store.add_task(mission["id"], "current task")
        store.set_mission_status(mission["id"], "RUNNING")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(BridgeStore(database))
    hooks.suspend_allowed = lambda: suspend_allowed

    def prepare(target: int):
        calls.append(("plan", target))
        return target, common.WakeArm(True, "fixture", True, unit="fixture", target_epoch=target)

    hooks.prepare_suspend = prepare
    if amend:
        # An amendment recorded before the failing operation finishes stays pending until the boundary.
        original_before_launch = hooks.before_launch

        def before_launch(*args: Any, **kwargs: Any) -> bool:
            result = original_before_launch(*args, **kwargs)
            with BridgeStore(database) as other:
                other.amend_task(task_id, prompt="amended while the operation was active")
            return result

        hooks.before_launch = before_launch  # type: ignore[method-assign]
    try:
        result = ralph_robin.main(
            [
                "--prompt", "objective", "--providers", "codex", "--cwd", str(tmp_path),
                "--command-template", template, "--retry-delays", "", "--poll-interval", "1",
                "--max-iterations", "1", "--max-duration", "7200", "--min-iteration-seconds", "0",
                "--log-dir", str(tmp_path / "ralph-logs"), "--state-file", str(tmp_path / "ralph-state.json"),
            ],
            bridge_hooks=hooks,
        )
    finally:
        hooks.store.close()
    return result, calls, database, mission["id"], task_id


@pytest.mark.parametrize("amend", [False, True], ids=["initial-quota-wait", "exhausted-with-pending-amendment"])
def test_quota_failure_with_known_reset_suspends_once_then_runs_next_operation(monkeypatch, tmp_path, amend) -> None:
    import time

    reset = int(time.time()) + 3600
    result, calls, database, mission_id, task_id = _run_mission(
        tmp_path, monkeypatch, exhausted_reset=reset, suspend_allowed=True, amend=amend,
    )

    assert result == 0
    assert calls == [("plan", reset), ("suspend", reset)], "arm the wake first, suspend once, never wait awake before it"
    with BridgeStore(database) as store:
        rows = store.db.execute("SELECT exit_code FROM operations WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
        assert [row["exit_code"] for row in rows] == [1, 0]
        if amend:
            assert store.db.execute("SELECT status FROM amendments WHERE task_id=?", (task_id,)).fetchone()["status"] == "APPLIED"


def test_quota_failure_with_unknown_reset_waits_awake_and_still_continues(monkeypatch, tmp_path) -> None:
    result, calls, database, _mission_id, task_id = _run_mission(
        tmp_path, monkeypatch, exhausted_reset=None, suspend_allowed=True, amend=False,
    )

    assert result == 0
    assert [kind for kind, _ in calls] == ["wait"], "an unknown reset is an explicit awake wait, never a suspend"
    with BridgeStore(database) as store:
        rows = store.db.execute("SELECT exit_code FROM operations WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
        assert [row["exit_code"] for row in rows] == [1, 0]


def test_quota_failure_with_policy_off_waits_awake_and_still_continues(monkeypatch, tmp_path) -> None:
    import time

    reset = int(time.time()) + 3600
    result, calls, _database, _mission_id, _task_id = _run_mission(
        tmp_path, monkeypatch, exhausted_reset=reset, suspend_allowed=False, amend=False,
    )

    assert result == 0
    assert [kind for kind, _ in calls] == ["wait"]


# --- quota failures never exhaust the provider-failure budget -----------------------------------


def test_quota_failures_do_not_consume_the_provider_failure_budget(tmp_path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("budget", "objective", cwd=tmp_path)
        mission_id = mission["id"]
        task_id = store.add_task(mission_id, "task")
        store.set_mission_status(mission_id, "RUNNING")
        for iteration in range(1, 4):
            store.begin_iteration(task_id)
            operation = store.begin_operation(task_id, iteration=iteration, station="codex")
            store.finish_operation(operation, exit_code=1)
            store.record_runtime_event(mission_id, "provider-result-diagnosed", {
                "operation_id": operation, "failure": {"class": "capacity", "cause": "provider-quota"},
            })
            continued = store.continue_task_after_provider_failure(operation, max_failures=1)
            assert continued is not None and continued["status"] == "READY", f"quota failure {iteration} stranded the task"


def test_real_provider_failures_still_consume_the_budget(tmp_path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id = store.create_mission("budget", "objective", cwd=tmp_path)["id"]
        task_id = store.add_task(mission_id, "task")
        store.set_mission_status(mission_id, "RUNNING")
        results = []
        for iteration in (1, 2):
            store.begin_iteration(task_id)
            operation = store.begin_operation(task_id, iteration=iteration, station="codex")
            store.finish_operation(operation, exit_code=1)
            results.append(store.continue_task_after_provider_failure(operation, max_failures=1))
        assert results[0] is not None and results[1] is None
