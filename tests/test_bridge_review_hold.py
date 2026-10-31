"""A native mission holds before its next task until the operator decides on the reviewed one.

Store and controller-hook behaviour against fake backends; the real tmux
end-to-end proof is in ``test_bridge_review_hold_tmux.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

from llm_tools import bridge_missions, bridge, bridge_format, bridge_runtime
from llm_tools.bridge import main
from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore

NATIVE = {"providers": "codex", "routes": "", "native_tui": True, "headless": False}


class _Cfg:
    run_dir = None
    provider = "codex"

    def __init__(self, cwd: Path) -> None:
        self.cwd = str(cwd)


def _mission(store: BridgeStore, tmp_path: Path, config: dict[str, Any] | None = None, tasks: int = 2) -> tuple[str, list[int]]:
    mission = store.create_mission("mission", "objective", cwd=tmp_path, initial_prompt="first task", config=dict(NATIVE if config is None else config))
    ids = [int(mission["initial_task_id"])]
    for number in range(2, tasks + 1):
        ids.append(store.add_task(mission["id"], f"task {number} prompt", title=f"Task {number}"))
    store.set_mission_status(mission["id"], "RUNNING")
    return mission["id"], ids


def _run_to_verifying(store: BridgeStore, hooks: BridgeMissionHooks, task_id: int, tmp_path: Path) -> None:
    """Drive one provider run for ``task_id`` through the real hook (including a configured verifier)."""
    store.begin_iteration(task_id)
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation)
    hooks.after_iteration({"operation_id": operation}, {"provider": "codex"}, _Cfg(tmp_path), 1, 0, 1.0)


def _hooks(store: BridgeStore, mission_id: str, tmp_path: Path, *, native: bool = True, handle: object | None = None) -> BridgeMissionHooks:
    hooks = BridgeMissionHooks(mission_id, handle=handle, headless=not native, native_tui=native, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    return hooks


class _Sleeper:
    """Replaces ``time.sleep`` in the hold loop and lets a test act while the controller holds."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        self.actions: list[Callable[[], None]] = []
        monkeypatch.setattr(bridge_runtime.time, "sleep", self)

    def __call__(self, _seconds: float) -> None:
        self.calls += 1
        assert self.actions, "the controller kept holding with nothing left to release it"
        self.actions.pop(0)()


def _kinds(store: BridgeStore, mission_id: str, *kinds: str) -> list[dict[str, Any]]:
    return [event for event in store.events(mission_id) if event["kind"] in kinds]


def test_native_mission_holds_until_approved(tmp_path: Path, monkeypatch) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, second) = _mission(store, tmp_path)
        hooks = _hooks(store, mission_id, tmp_path)
        _run_to_verifying(store, hooks, first, tmp_path)
        seen: dict[str, Any] = {}

        def inspect_then_approve() -> None:
            snapshot = project_snapshot(store, mission_id)
            seen["snapshot"] = snapshot
            seen["status"] = bridge_format.format_status(snapshot)
            assert store.get_task(second)["status"] == "QUEUED"  # not started while holding
            store.verify_task(first, approved=True)

        sleeper = _Sleeper(monkeypatch)
        sleeper.actions.append(inspect_then_approve)
        decision = hooks.before_iteration()

        assert decision["action"] == "run" and decision["task_id"] == second
        hold = seen["snapshot"]["hold"]
        assert hold["reason"] == "review" and hold["task_ordinal"] == 1 and hold["tasks"] == [1] and hold["since"]
        assert "task_id" not in hold and "marker" not in hold
        needs = seen["snapshot"]["needs_you"]
        assert needs["kind"] == "approval" and needs["tasks"] == [1]
        assert needs["message"] == "task 1 awaits your review · /approve 1 or /reject 1 reason · /continue to start the next task now"
        assert "Holding     the next task has not started" in seen["status"] and "task 1 awaits your review" in seen["status"]
        assert "Needs you   task 1 awaits your review" in seen["status"]
        held, released = _kinds(store, mission_id, "review-hold", "review-hold-released")
        assert held["kind"] == "review-hold" and held["task_ordinal"] == 1
        assert held["data"]["summary"] == "holding before the next task · task 1 awaits your review"
        assert released["data"]["how"] == "approved" and released["data"]["summary"] == "hold released · task 1 approved"
        assert project_snapshot(store, mission_id)["hold"] is None


def test_reject_releases_the_hold_and_requeues_the_task(tmp_path: Path, monkeypatch) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, _second) = _mission(store, tmp_path)
        hooks = _hooks(store, mission_id, tmp_path)
        _run_to_verifying(store, hooks, first, tmp_path)
        sleeper = _Sleeper(monkeypatch)
        sleeper.actions.append(lambda: store.reject_task(first, "tests were not run"))

        decision = hooks.before_iteration()

        assert decision["action"] == "run" and decision["task_id"] == first  # sent back, so it runs again first
        assert "tests were not run" in decision["prompt"]
        released = _kinds(store, mission_id, "review-hold-released")[0]
        assert released["data"]["how"] == "rejected"


def test_continue_releases_once_and_the_reviewed_task_keeps_waiting(tmp_path: Path, monkeypatch) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, second, third) = _mission(store, tmp_path, tasks=3)
        hooks = _hooks(store, mission_id, tmp_path)
        _run_to_verifying(store, hooks, first, tmp_path)
        sleeper = _Sleeper(monkeypatch)
        sleeper.actions.append(lambda: seen.update(result=store.release_review_hold(mission_id)))
        seen: dict[str, Any] = {}

        decision = hooks.before_iteration()

        assert decision["action"] == "run" and decision["task_id"] == second
        assert seen["result"]["released"] == [1]
        assert "task 1 stays awaiting approval" in seen["result"]["detail"]
        assert store.get_task(first)["status"] == "VERIFYING"
        released = _kinds(store, mission_id, "review-hold-released")[0]
        assert released["data"]["how"] == "continue"
        assert "task 1 still awaits approval" in released["data"]["summary"]
        # The release was for task 1 only: task 2 reaching review holds again, naming task 2.
        _run_to_verifying(store, hooks, second, tmp_path)
        again = store.review_hold(mission_id)
        assert again is not None and again["task_ordinal"] == 2 and again["tasks"] == [2]
        # A second continue has nothing left to release once nothing holds.
        store.release_review_hold(mission_id)
        assert store.release_review_hold(mission_id) == {"released": [], "noop": True, "detail": "Nothing to continue · no task is holding the next one back"}


def test_rejected_then_rereviewed_task_holds_again_after_an_earlier_continue(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, _second) = _mission(store, tmp_path)
        hooks = _hooks(store, mission_id, tmp_path)
        _run_to_verifying(store, hooks, first, tmp_path)
        store.release_review_hold(mission_id)
        assert store.review_hold(mission_id) is None
        store.reject_task(first, "again please")
        # Task 1 runs a second time and returns for review: a new review round, so it holds.
        _run_to_verifying(store, hooks, first, tmp_path)
        hold = store.review_hold(mission_id)
        assert hold is not None and hold["task_ordinal"] == 1


@pytest.mark.parametrize("config", [
    {**NATIVE, "auto_continue": True},
    {"providers": "codex", "routes": "", "native_tui": False, "headless": False},
    {"providers": "codex", "routes": "", "native_tui": True, "headless": True},
    {"providers": "codex", "routes": ""},
])
def test_no_hold_with_auto_continue_or_unattended_modes(tmp_path: Path, monkeypatch, config) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, second) = _mission(store, tmp_path, config)
        hooks = _hooks(store, mission_id, tmp_path, native=bool(config.get("native_tui")))
        _run_to_verifying(store, hooks, first, tmp_path)
        monkeypatch.setattr(bridge_runtime.time, "sleep", lambda _s: pytest.fail("the controller must not hold"))

        decision = hooks.before_iteration()

        assert decision["action"] == "run" and decision["task_id"] == second
        assert store.get_task(first)["status"] == "VERIFYING"
        assert project_snapshot(store, mission_id)["hold"] is None
        assert _kinds(store, mission_id, "review-hold") == []


def test_explicit_auto_continue_off_holds_even_when_unattended_by_default(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, _second) = _mission(store, tmp_path, {"providers": "codex", "routes": "", "auto_continue": False})
        _run_to_verifying(store, _hooks(store, mission_id, tmp_path, native=False), first, tmp_path)
        assert store.review_hold(mission_id)["task_ordinal"] == 1


def test_passing_verifier_completes_the_task_without_a_hold(tmp_path: Path, monkeypatch) -> None:
    config = {**NATIVE, "verifier": {"argv": [sys.executable, "-c", "print('{\"passed\": true, \"summary\": \"ok\"}')"], "timeout_seconds": 10}}
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, second) = _mission(store, tmp_path, config)
        hooks = _hooks(store, mission_id, tmp_path)
        _run_to_verifying(store, hooks, first, tmp_path)
        monkeypatch.setattr(bridge_runtime.time, "sleep", lambda _s: pytest.fail("a verified task needs no review hold"))

        assert store.get_task(first)["status"] == "COMPLETED"
        assert hooks.before_iteration()["task_id"] == second


def test_failing_verifier_leaves_the_task_in_review_and_holds(tmp_path: Path) -> None:
    config = {**NATIVE, "verifier": {"argv": [sys.executable, "-c", "import sys; sys.exit(3)"], "timeout_seconds": 10}}
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, _second) = _mission(store, tmp_path, config)
        _run_to_verifying(store, _hooks(store, mission_id, tmp_path), first, tmp_path)

        assert store.get_task(first)["status"] == "VERIFYING"
        assert store.review_hold(mission_id)["task_ordinal"] == 1


def test_nothing_to_hold_back_means_no_hold(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first,) = _mission(store, tmp_path, tasks=1)
        _run_to_verifying(store, _hooks(store, mission_id, tmp_path), first, tmp_path)
        snapshot = project_snapshot(store, mission_id)

        assert snapshot["hold"] is None
        assert snapshot["needs_you"]["kind"] == "approval" and "/continue" not in snapshot["needs_you"]["message"]
        assert "hold" not in snapshot["needs_you"]


def test_a_task_that_depends_on_the_reviewed_one_cannot_start_so_nothing_holds(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, second) = _mission(store, tmp_path)
        store.add_dependency(mission_id, second, first)
        _run_to_verifying(store, _hooks(store, mission_id, tmp_path), first, tmp_path)

        assert store.review_hold(mission_id) is None


def test_hold_message_is_shown_in_the_provider_pane(tmp_path: Path, monkeypatch) -> None:
    shown: list[str] = []

    class FakeBackend:
        def show_provider_message(self, handle: object, text: str) -> bool:
            shown.append(text)
            return True

    monkeypatch.setattr(bridge_runtime, "TmuxTerminalBackend", FakeBackend)
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, second) = _mission(store, tmp_path)
        hooks = _hooks(store, mission_id, tmp_path, handle=object())
        _run_to_verifying(store, hooks, first, tmp_path)
        sleeper = _Sleeper(monkeypatch)
        sleeper.actions += [lambda: None, lambda: store.verify_task(first, approved=True)]
        hooks.before_iteration()

        assert shown == ["Task 1 is waiting for your review. Ctrl-] then B, then /approve or /reject."]  # once, not per poll


def test_hold_message_failure_does_not_stop_the_hold(tmp_path: Path, monkeypatch) -> None:
    class BrokenBackend:
        def show_provider_message(self, handle: object, text: str) -> bool:
            raise OSError("tmux went away")

    monkeypatch.setattr(bridge_runtime, "TmuxTerminalBackend", BrokenBackend)
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, second) = _mission(store, tmp_path)
        hooks = _hooks(store, mission_id, tmp_path, handle=object())
        _run_to_verifying(store, hooks, first, tmp_path)
        sleeper = _Sleeper(monkeypatch)
        sleeper.actions.append(lambda: store.verify_task(first, approved=True))

        assert hooks.before_iteration()["task_id"] == second


def test_stop_during_a_hold_ends_the_loop(tmp_path: Path, monkeypatch) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, _second) = _mission(store, tmp_path)
        hooks = _hooks(store, mission_id, tmp_path)
        _run_to_verifying(store, hooks, first, tmp_path)
        sleeper = _Sleeper(monkeypatch)
        sleeper.actions.append(lambda: store.set_mission_status(mission_id, "STOPPING"))

        assert hooks.before_iteration()["action"] == "stop"


def test_turning_auto_continue_on_while_holding_releases_the_hold(tmp_path: Path, monkeypatch) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, second) = _mission(store, tmp_path)
        hooks = _hooks(store, mission_id, tmp_path)
        _run_to_verifying(store, hooks, first, tmp_path)
        sleeper = _Sleeper(monkeypatch)
        sleeper.actions.append(lambda: bridge._set_mission_auto_continue(store, mission_id, True))

        assert hooks.before_iteration()["task_id"] == second
        assert _kinds(store, mission_id, "review-hold-released")[0]["data"]["how"] == "auto-continue"


def test_released_hold_for_a_task_that_was_cancelled_is_recorded_plainly(tmp_path: Path, monkeypatch) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, second) = _mission(store, tmp_path)
        hooks = _hooks(store, mission_id, tmp_path)
        _run_to_verifying(store, hooks, first, tmp_path)
        sleeper = _Sleeper(monkeypatch)
        sleeper.actions.append(lambda: store.set_task_status(first, "CANCELLED"))

        assert hooks.before_iteration()["task_id"] == second
        released = _kinds(store, mission_id, "review-hold-released")[0]["data"]
        assert released["how"] == "cancelled" and released["summary"] == "hold released · task 1 is cancelled"


# --------------------------------------------------------------------------- CLI


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    return directory


def _cli(capsys, *argv: str, code: int = 0) -> str:
    assert main(list(argv)) == code, capsys.readouterr()
    captured = capsys.readouterr()
    return captured.out if code == 0 else captured.err


def test_mission_create_auto_continue_flag_and_mission_set(tmp_path, state, capsys) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    base = ["mission", "create", "--prompt", "obj", "--cwd", str(folder), "--providers", "codex", "--native", "--json"]
    plain = json.loads(_cli(capsys, *base))
    flagged = json.loads(_cli(capsys, *base, "--auto-continue"))
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert "auto_continue" not in store.get_mission(plain["mission"]["id"])["config"]
        assert store.get_mission(flagged["mission"]["id"])["config"]["auto_continue"] is True

    mission = plain["mission"]["id"]
    assert "Auto-continue on" in _cli(capsys, "mission", "set", "--mission", mission, "--auto-continue", "on")
    out = json.loads(_cli(capsys, "mission", "set", "--mission", mission, "--auto-continue", "off", "--json"))
    assert out["auto_continue"] is False and "holds" in out["detail"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mission)["config"]["auto_continue"] is False
        assert [event["kind"] for event in store.events(mission) if event["kind"] == "mission-auto-continue"] == ["mission-auto-continue"] * 2


def test_continue_command_cli_and_console(tmp_path, state, capsys) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    created = json.loads(_cli(capsys, "mission", "create", "--prompt", "obj", "--cwd", str(folder), "--providers", "codex", "--native", "--json"))
    mission = created["mission"]["id"]
    first = int(created["task"]["id"])
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.add_task(mission, "second", title="Second")
        store.set_mission_status(mission, "RUNNING")
        operation = store.begin_operation(first, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)

    status = _cli(capsys, "status", "--mission", mission)
    assert "Needs you   task 1 awaits your review · /approve 1 or /reject 1 reason · /continue to start the next task now" in status
    assert json.loads(_cli(capsys, "status", "--mission", mission, "--json"))["hold"]["task_ordinal"] == 1

    assert _cli(capsys, "continue", "--mission", mission).strip() == "Continuing · task 1 stays awaiting approval while the next task starts"
    assert json.loads(_cli(capsys, "status", "--mission", mission, "--json"))["hold"] is None
    assert "Nothing to continue" in _cli(capsys, "continue", "--mission", mission)
    again = json.loads(_cli(capsys, "continue", "--mission", mission, "--json"))
    assert again["result"]["released"] == [] and again["status"] == "APPLIED"

    # The same grammar from the console, including the slash form.
    console = dispatch_console(mission, "/continue")
    assert "Nothing to continue" in console


def dispatch_console(mission_id: str, line: str) -> str:
    return str(bridge.dispatch_console_command(mission_id, line))


def test_format_status_hides_hold_without_one() -> None:
    snapshot = {"mission": {"id": "a" * 32, "name": "n", "status": "RUNNING", "cwd": "/x", "config": {}}, "tasks": [], "task_counts": {}, "hold": None}
    assert "Holding" not in bridge_format.format_status(snapshot)
    snapshot["hold"] = {"reason": "review", "task_ordinal": 4, "since": "not a time"}
    assert "Holding     the next task has not started · task 4 awaits your review" in bridge_format.format_status(snapshot)


@pytest.mark.parametrize(("config", "expected"), [
    (NATIVE, "Task 1 is waiting for your review. Ctrl-] then B, then /approve or /reject."),
    ({**NATIVE, "auto_continue": True}, ""),
    ({**NATIVE, "verifier": {"argv": ["true"], "timeout_seconds": 3}}, ""),
])
def test_review_hint_is_prepared_before_launch_only_when_a_clean_exit_means_review(tmp_path: Path, monkeypatch, config, expected) -> None:
    hints: list[str] = []

    class HintBackend:
        def set_review_hint(self, handle: object, text: str) -> bool:
            hints.append(text)
            return True

    monkeypatch.setattr(bridge_runtime, "TmuxTerminalBackend", HintBackend)
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, _second) = _mission(store, tmp_path, config)
        hooks = _hooks(store, mission_id, tmp_path, handle=object())
        hooks._publish_review_hint(store.get_mission(mission_id), first)
        assert hints == [expected]

        class Broken:
            def set_review_hint(self, handle: object, text: str) -> bool:
                raise OSError("tmux went away")

        monkeypatch.setattr(bridge_runtime, "TmuxTerminalBackend", Broken)
        hooks._publish_review_hint(store.get_mission(mission_id), first)  # a courtesy footer never stops the launch
        _hooks(store, mission_id, tmp_path, native=False)._publish_review_hint(store.get_mission(mission_id), first)
