"""Why a mission is waiting or failed must be visible in the status snapshot and `status`."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_format, bridge_monitor, bridge_runtime
from llm_tools.bridge import main, status_snapshot
from llm_tools.bridge_runtime import BridgeMissionHooks, run_controller
from llm_tools.bridge_store import BridgeStore

RESTRICTED = '[usage]\nproviders = ["claude", "codex"]\n'
TWO_DAYS_NINE_HOURS = 2 * 86400 + 9 * 3600 + 120


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.delenv("LLM_USAGE_PROVIDERS", raising=False)
    return directory


@pytest.fixture()
def store(state: Path):
    with BridgeStore(state / "bridge.sqlite3") as opened:
        yield opened


def _running_mission(store: BridgeStore, tmp_path: Path, **config: Any) -> str:
    mission = store.create_mission("waiting", "objective", cwd=tmp_path, initial_prompt="work", config={"providers": "copilot", "headless": True, **config})
    store.set_mission_status(mission["id"], "RUNNING")
    return mission["id"]


def _decision(name: str, percent: float, reset_in: float, window: str = "monthly", reason: str = "rate-limited") -> dict[str, Any]:
    reset = int(time.time() + reset_in)
    return {
        "provider": name, "usable": False, "reason": reason, "wait_until": reset,
        "windows": [{"name": window, "kind": "reset_window", "remaining_percent": percent, "reset_epoch": reset}],
    }


def _wait_event(store: BridgeStore, mission_id: str, decisions: list[dict[str, Any]], *, all_limited: bool = True, target: int | None = None) -> None:
    store.record_runtime_event(mission_id, "provider-wait", {
        "target_epoch": target, "all_rate_limited": all_limited, "reason": "rate-limited", "decisions": decisions,
    })


def test_snapshot_exposes_the_capacity_wait_the_station_never_showed(store, tmp_path, state) -> None:
    mission_id = _running_mission(store, tmp_path)
    store.record_runtime_event(mission_id, "provider-selection", {"provider": "", "decisions": []})
    reset = int(time.time() + TWO_DAYS_NINE_HOURS)
    _wait_event(store, mission_id, [_decision("copilot", 0, TWO_DAYS_NINE_HOURS)], target=reset)

    waiting = status_snapshot(mission_id)["waiting"]

    assert waiting["reason"] == "capacity"
    assert waiting["providers"] == ["copilot"]
    assert waiting["until_epoch"] == reset
    assert waiting["detail"] == "copilot monthly 0% · resets in 2d 9h"
    assert waiting["entries"][0]["window"] == "monthly" and waiting["entries"][0]["remaining_percent"] == 0


def test_status_text_says_what_the_mission_waits_for_and_how_to_change_it(store, tmp_path, state, capsys) -> None:
    mission_id = _running_mission(store, tmp_path)
    _wait_event(store, mission_id, [_decision("copilot", 0, TWO_DAYS_NINE_HOURS)])
    assert main(["status", "--mission", mission_id]) == 0
    line = next(row for row in capsys.readouterr().out.splitlines() if row.startswith("Waiting"))
    assert line == f"Waiting     for capacity · copilot resets in 2d 9h · change providers with: llm-bridge rotation --mission {mission_id[:8]} --set LIST"


def test_waiting_ends_when_a_provider_is_selected_or_an_operation_runs(store, tmp_path, state) -> None:
    mission_id = _running_mission(store, tmp_path)
    _wait_event(store, mission_id, [_decision("copilot", 0, 3600)])
    assert status_snapshot(mission_id)["waiting"] is not None
    store.record_runtime_event(mission_id, "capacity-refresh", {"decisions": []})
    assert status_snapshot(mission_id)["waiting"] is not None, "the background capacity observer must not end the wait"
    store.record_runtime_event(mission_id, "provider-selection", {"provider": "claude", "decisions": []})
    assert status_snapshot(mission_id)["waiting"] is None


def test_waiting_is_only_reported_for_a_running_mission_without_an_operation(store, tmp_path, state) -> None:
    mission_id = _running_mission(store, tmp_path)
    _wait_event(store, mission_id, [_decision("copilot", 0, 3600)])
    task_id = store.list_tasks(mission_id)[0]["id"]
    operation = store.begin_operation(task_id, iteration=1, station="copilot")
    store.mark_operation_running(operation, process_pid=1, process_start="t")
    assert status_snapshot(mission_id)["waiting"] is None
    store.finish_operation(operation, exit_code=0)
    assert status_snapshot(mission_id)["waiting"] is not None
    store.set_mission_status(mission_id, "PAUSED")
    assert status_snapshot(mission_id)["waiting"] is None


def test_unavailable_providers_are_reported_by_reason_not_as_capacity(store, tmp_path, state) -> None:
    mission_id = _running_mission(store, tmp_path)
    decision = {"provider": "copilot", "usable": False, "reason": "not-authenticated", "windows": []}
    _wait_event(store, mission_id, [decision], all_limited=False)
    waiting = status_snapshot(mission_id)["waiting"]
    assert waiting["reason"] == "unavailable" and waiting["detail"] == "copilot not-authenticated"
    assert "for a usable provider · copilot not-authenticated" in bridge_format.format_waiting(waiting, "abc")


def test_waiting_message_names_each_provider_and_the_key_for_options() -> None:
    now = 1_000_000.0
    waiting = bridge_monitor.waiting_from_wait({
        "all_rate_limited": True, "decisions": [
            {"provider": "copilot", "usable": False, "windows": [{"name": "monthly", "remaining_percent": 0, "reset_epoch": now + 2 * 86400 + 9 * 3600 + 5}]},
            {"provider": "claude", "usable": False, "windows": [{"name": "5h", "remaining_percent": 0, "reset_epoch": now + 3600 + 5}]},
            {"provider": "codex", "usable": True},
        ],
    }, now)
    assert bridge_monitor.waiting_message(waiting, now) == (
        "Waiting for capacity: copilot monthly quota resets in 2d 9h; claude 5h quota resets in 1h. Ctrl-] then B for options."
    )
    assert waiting["providers"] == ["copilot", "claude"]
    bare = bridge_monitor.waiting_from_wait({"all_rate_limited": True, "target_epoch": now + 300, "decisions": []}, now)
    assert bridge_monitor.waiting_message(bare, now).startswith("Waiting for capacity: next provider ready in 5m.")


@pytest.mark.parametrize(("seconds", "text"), [(-5, "now"), (30, "<1m"), (125, "2m"), (3600, "1h"), (3900, "1h 5m"), (86400, "1d"), (TWO_DAYS_NINE_HOURS, "2d 9h")])
def test_countdown_uses_two_units_where_useful(seconds: int, text: str) -> None:
    assert bridge_format.countdown(seconds) == text


class _FakeBackend:
    messages: list[str] = []
    result = True

    def show_provider_message(self, handle: object, text: str) -> bool:
        self.messages.append(text)
        return self.result


def test_native_controller_writes_the_wait_into_the_empty_provider_pane_once(store, tmp_path, state, monkeypatch) -> None:
    mission_id = _running_mission(store, tmp_path, native_tui=True)
    monkeypatch.setattr(bridge_runtime, "TmuxTerminalBackend", _FakeBackend)
    _FakeBackend.messages = []
    hooks = BridgeMissionHooks(mission_id, handle=object(), headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")  # type: ignore[arg-type]
    hooks.bind(store)
    selection = {"provider": "", "decisions": [_decision("copilot", 0, TWO_DAYS_NINE_HOURS)], "all_rate_limited": True, "decision": {}, "rotation_reason": "all-rate-limited"}
    hooks.on_wait(selection, int(time.time() + TWO_DAYS_NINE_HOURS))
    hooks.on_wait(selection, int(time.time() + TWO_DAYS_NINE_HOURS))
    assert _FakeBackend.messages == ["Waiting for capacity: copilot monthly quota resets in 2d 9h. Ctrl-] then B for options."]
    assert status_snapshot(mission_id)["waiting"]["providers"] == ["copilot"]


def test_headless_and_pane_errors_never_break_the_wait(store, tmp_path, state, monkeypatch) -> None:
    mission_id = _running_mission(store, tmp_path)
    selection = {"provider": "", "decisions": [_decision("copilot", 0, 60)], "all_rate_limited": True, "decision": {}, "rotation_reason": "x"}
    headless = BridgeMissionHooks(mission_id, handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    headless.bind(store)
    headless.on_wait(selection, None)  # no station: only the event is recorded

    class Broken(_FakeBackend):
        def show_provider_message(self, handle: object, text: str) -> bool:
            raise RuntimeError("tmux went away")

    monkeypatch.setattr(bridge_runtime, "TmuxTerminalBackend", Broken)
    native = BridgeMissionHooks(mission_id, handle=object(), headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")  # type: ignore[arg-type]
    native.bind(store)
    native.on_wait(selection, None)
    assert native._waiting_message == ""


# --- controller failure reason -------------------------------------------------


def _failing_mission(tmp_path: Path, store: BridgeStore, providers: str = "claude", frozen: str = "") -> str:
    mission = store.create_mission("failing", "objective", cwd=tmp_path, config={"providers": providers, "routes": "", "headless": True, "frozen_tool_config": frozen})
    store.add_task(mission["id"], "work")
    return mission["id"]


def test_disabled_rotation_fails_the_controller_with_a_recorded_reason(store, tmp_path, state, monkeypatch, capsys) -> None:
    mission_id = _failing_mission(tmp_path, store, providers="copilot", frozen=RESTRICTED)
    monkeypatch.setattr(bridge_runtime.ralph_robin, "main", lambda *a, **k: pytest.fail("ralph must not start"))
    assert run_controller(mission_id, headless=True) == 2
    assert store.get_mission(mission_id)["status"] == "FAILED"
    snapshot = status_snapshot(mission_id)
    assert snapshot["lifecycle"]["failure_reason"].startswith("copilot is disabled by [usage].providers in this mission's saved config")
    assert main(["status", "--mission", mission_id]) == 0
    controller = next(row for row in capsys.readouterr().out.splitlines() if row.startswith("Controller"))
    assert controller.startswith("Controller  failed — copilot is disabled by")
    kinds = [event["kind"] for event in store.events(mission_id)]
    assert "controller-failed" in kinds


def test_unexpected_exception_is_recorded_as_a_one_line_reason(store, tmp_path, state, monkeypatch) -> None:
    mission_id = _failing_mission(tmp_path, store)

    def boom(*_args, **_kwargs):
        raise RuntimeError("provider\nlauncher   exploded")

    monkeypatch.setattr(bridge_runtime.ralph_robin, "main", boom)
    assert run_controller(mission_id, headless=True) == 1
    assert status_snapshot(mission_id)["lifecycle"]["failure_reason"] == "provider launcher exploded"


def test_system_exit_reason_comes_from_the_last_logged_error(store, tmp_path, state, monkeypatch) -> None:
    mission_id = _failing_mission(tmp_path, store)
    log = bridge_runtime._runtime_directory(mission_id) / "controller.log"

    def exits(*_args, **_kwargs):
        log.write_text("warning: noise\nerror: config: could not parse TOML\n", encoding="utf-8")
        raise SystemExit(2)

    monkeypatch.setattr(bridge_runtime.ralph_robin, "main", exits)
    assert run_controller(mission_id, headless=True) == 2
    assert status_snapshot(mission_id)["lifecycle"]["failure_reason"] == "config: could not parse TOML"


def test_system_exit_without_a_logged_error_still_names_the_status(store, tmp_path, state, monkeypatch) -> None:
    mission_id = _failing_mission(tmp_path, store)
    monkeypatch.setattr(bridge_runtime.ralph_robin, "main", lambda *a, **k: (_ for _ in ()).throw(SystemExit(7)))
    run_controller(mission_id, headless=True)
    assert status_snapshot(mission_id)["lifecycle"]["failure_reason"].startswith("ralph-robin exited with status 7")


def test_controller_returning_while_running_records_why(store, tmp_path, state, monkeypatch) -> None:
    mission_id = _failing_mission(tmp_path, store)
    monkeypatch.setattr(bridge_runtime.ralph_robin, "main", lambda *a, **k: 0)
    run_controller(mission_id, headless=True)
    assert status_snapshot(mission_id)["lifecycle"]["failure_reason"] == "the controller stopped while the mission was still running"


def test_a_later_successful_run_clears_the_failure_reason(store, tmp_path, state, monkeypatch) -> None:
    mission_id = _failing_mission(tmp_path, store)
    monkeypatch.setattr(bridge_runtime.ralph_robin, "main", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("first")))
    run_controller(mission_id, headless=True)
    assert status_snapshot(mission_id)["lifecycle"]["failure_reason"] == "first"

    def stops(*_args, **_kwargs):
        store.set_mission_status(mission_id, "STOPPING")
        return 0

    monkeypatch.setattr(bridge_runtime.ralph_robin, "main", stops)
    run_controller(mission_id, headless=True)
    snapshot = status_snapshot(mission_id)
    assert snapshot["mission"]["status"] == "STOPPED" and snapshot["lifecycle"]["failure_reason"] is None


def test_detached_launch_reports_the_recorded_reason_instead_of_a_bare_exit_status(store, tmp_path, state, monkeypatch) -> None:
    mission_id = _failing_mission(tmp_path, store, providers="copilot", frozen=RESTRICTED)
    monkeypatch.setenv("LLM_TOOLS_NO_INHIBIT", "1")
    with pytest.raises(RuntimeError, match=r"controller exited with status 2: copilot is disabled by \[usage\]\.providers"):
        bridge_runtime.launch_controller(mission_id, headless=True)


# --- state-appropriate controller line -----------------------------------------


def _snapshot(status: str, **lifecycle: Any) -> dict[str, Any]:
    return {
        "mission": {"id": "abcdef1234567890", "name": "m", "status": status, "cwd": "/tmp", "config": {"headless": True}},
        "lifecycle": {"controller": "unknown", "heartbeat": {"reported_state": "ACTIVE", "age_seconds": 16}, "controller_running": False, **lifecycle},
        "tasks": [], "task_counts": {},
    }


@pytest.mark.parametrize(("status", "expected"), [
    ("STOPPED", "Controller  stopped — run: llm-bridge start --mission abcdef12"),
    ("PAUSED", "Controller  paused — run: llm-bridge resume --mission abcdef12"),
    ("COMPLETED", "Controller  finished"),
    ("CANCELLED", "Controller  finished"),
    ("FAILED", "Controller  failed — reason not recorded; inspect with: llm-bridge events --mission abcdef12"),
])
def test_status_never_shows_a_stale_heartbeat_for_a_finished_or_idle_mission(status: str, expected: str) -> None:
    text = bridge_format.format_status(_snapshot(status))
    assert expected in text.splitlines()
    assert "heartbeat" not in text


def test_failed_status_shows_the_reason() -> None:
    text = bridge_format.format_status(_snapshot("FAILED", failure_reason="copilot is disabled by LLM_USAGE_PROVIDERS · add it there"))
    assert "Controller  failed — copilot is disabled by LLM_USAGE_PROVIDERS · add it there" in text.splitlines()


def test_running_status_keeps_the_live_heartbeat() -> None:
    snapshot = _snapshot("RUNNING", controller="RUNNING", controller_running=True)
    assert "Controller  active · heartbeat 16s ago" in bridge_format.format_status(snapshot)


def test_stopped_mission_through_the_cli_prints_the_start_hint(store, tmp_path, state, capsys) -> None:
    mission_id = _running_mission(store, tmp_path)
    store.set_mission_status(mission_id, "STOPPING")
    store.set_mission_status(mission_id, "STOPPED")
    store.record_runtime_event(mission_id, "controller-heartbeat", {"controller_state": "ACTIVE", "observed_at": "2099-01-01T00:00:00Z"})
    assert main(["status", "--mission", mission_id]) == 0
    out = capsys.readouterr().out
    assert f"Controller  stopped — run: llm-bridge start --mission {mission_id[:8]}" in out and "heartbeat" not in out


def test_json_status_carries_the_new_fields(store, tmp_path, state, capsys) -> None:
    mission_id = _running_mission(store, tmp_path)
    assert main(["status", "--mission", mission_id, "--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["waiting"] is None and snapshot["lifecycle"]["failure_reason"] is None
