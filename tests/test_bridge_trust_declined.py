"""Declining Claude Code's folder-trust prompt holds the task instead of looping."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_tools import common, ralph_robin, scheduler
from llm_tools.bridge_format import needs_attention
from llm_tools.bridge_monitor import project_snapshot as status_snapshot
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore
from tests.conftest import run_cmd, write_exe

TRUST_SCREEN = """\
 Accessing workspace:
 /work/project
 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open
 source project, or work from your team). If not, take a moment to review what's in this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""
AVAILABLE = '{"available":true,"five_hour":{"remaining":50,"resets_at":"2026-06-02T23:00:00Z"},"week":{"remaining":50,"resets_at":"2026-06-07T23:00:00Z"}}'
EXPECTED = (
    "Claude Code was not allowed to trust this folder · trust it and run /start again, "
    "or choose another provider with /rotation"
)


def test_declined_prompt_is_recognised_only_as_the_last_screen() -> None:
    assert common.output_shows_declined_trust_prompt(TRUST_SCREEN)
    assert common.output_shows_declined_trust_prompt("\x1b[2J" + TRUST_SCREEN + "EXIT=1\n")
    # The older wording of the same dialog.
    assert common.output_shows_declined_trust_prompt("Do you trust the files in this folder?\n 1. Yes\n 2. No, exit (Esc)\n")
    # A session that got past the prompt and kept working is not a decline.
    assert not common.output_shows_declined_trust_prompt(TRUST_SCREEN + "\n".join(f"work line {n}" for n in range(40)))
    assert not common.output_shows_declined_trust_prompt("Error: something else failed\n")
    # The choice alone, without the trust question, is not enough.
    assert not common.output_shows_declined_trust_prompt("No, exit\n")


def test_submit_once_reports_declined_status_for_claude_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_headless(_cfg, _argv, output_file, status_file):
        output_file.write_text(TRUST_SCREEN, encoding="utf-8")
        status_file.write_text("1", encoding="utf-8")
        return 1

    monkeypatch.setattr(scheduler, "run_fresh_headless", fake_headless)
    logs = common.setup_run_logs(tmp_path, "declined-claude")
    claude = scheduler.SchedulerConfig(provider="claude", cwd=str(tmp_path), prompt_text="x")
    assert scheduler.submit_once(claude, logs, 1, ["claude"]) == common.OPERATOR_DECLINED_STATUS

    logs = common.setup_run_logs(tmp_path, "declined-codex")
    codex = scheduler.SchedulerConfig(provider="codex", cwd=str(tmp_path), prompt_text="x")
    assert scheduler.submit_once(codex, logs, 1, ["codex"]) != common.OPERATOR_DECLINED_STATUS


def test_submit_once_keeps_ordinary_claude_failures_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_headless(_cfg, _argv, output_file, status_file):
        output_file.write_text("Error: connection reset\n", encoding="utf-8")
        status_file.write_text("1", encoding="utf-8")
        return 1

    monkeypatch.setattr(scheduler, "run_fresh_headless", fake_headless)
    logs = common.setup_run_logs(tmp_path, "ordinary-failure")
    claude = scheduler.SchedulerConfig(provider="claude", cwd=str(tmp_path), prompt_text="x")
    assert scheduler.submit_once(claude, logs, 1, ["claude"]) == 1


def test_scheduler_does_not_relaunch_a_declined_provider(env: dict[str, str], tmp_path: Path) -> None:
    """A fake Claude that prints the prompt and exits 1 runs once, even with retry delays set."""
    launches = tmp_path / "launches"
    screen = tmp_path / "screen.txt"
    # The headless watchdog treats "Enter to confirm" as a blocked prompt (status 75)
    # before the exit is seen, so the fake prints the dialog without its footer.
    screen.write_text(TRUST_SCREEN.replace(" Enter to confirm · Esc to cancel\n", ""), encoding="utf-8")
    write_exe(
        Path(env["PATH"].split(":")[0]) / "fake-claude",
        f"""#!/usr/bin/env python3
import pathlib, sys
log = pathlib.Path({str(launches)!r})
log.write_text(log.read_text() + "x" if log.exists() else "x")
sys.stdout.write(pathlib.Path({str(screen)!r}).read_text())
sys.exit(1)
""",
    )
    result = run_cmd(
        [
            "./llm-scheduler", "--provider", "claude", "--prompt", "x", "--command-template", "fake-claude",
            "--retry-delays", "0,0", "--log-dir", str(tmp_path / "logs"),
        ],
        env | {"LLM_SCHEDULER_USAGE_JSON": AVAILABLE},
    )
    assert result.returncode == common.OPERATOR_DECLINED_STATUS, result.stderr
    assert launches.read_text() == "x"
    assert "operator-declined" in result.stderr


def test_ralph_stops_after_a_declined_prompt_outside_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selection = {
        "index": 0, "provider": "claude", "route": "", "rotation_reason": "ready",
        "all_rate_limited": False, "decision": {"provider": "claude", "usable": True}, "decisions": [],
    }
    launches: list[str] = []
    monkeypatch.setattr(ralph_robin, "select_provider", lambda *args: selection)
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda cfg: (launches.append(cfg.provider), common.OPERATOR_DECLINED_STATUS)[1])
    argv = [
        "--prompt", "x", "--providers", "claude", "--log-dir", str(tmp_path / "logs"),
        "--state-file", str(tmp_path / "state.json"), "--max-duration", "0", "--max-iterations", "0", "--min-iteration-seconds", "0",
    ]
    assert ralph_robin.main(argv) == common.OPERATOR_DECLINED_STATUS
    assert launches == ["claude"]


def _declined_mission(tmp_path: Path):
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("declined", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    context = {"task_id": task_id}
    config = SimpleNamespace(
        provider="claude", cwd=str(tmp_path), run_dir=None, model="", exec_mode="fresh", headless=True, native_tui=False,
        tmux_target="", tmux_socket="", tmux_pane="", env_overrides={}, prompt_text="x", max_output_bytes=0,
        max_output_rotations=0, log_dir=None,
    )
    assert hook.before_launch(context, {"provider": "claude"}, config, 1)
    return store, mission, task_id, hook, context, config


def test_hook_holds_the_task_and_records_why(tmp_path: Path) -> None:
    store, mission, task_id, hook, context, config = _declined_mission(tmp_path)

    hook.after_iteration(context, {"provider": "claude"}, config, 1, common.OPERATOR_DECLINED_STATUS, 12.0)

    assert store.get_task(task_id)["status"] == "BLOCKED"
    kinds = [event["kind"] for event in store.events(mission["id"])]
    assert "provider-declined" in kinds
    assert "task-provider-continuation" not in kinds
    assert "provider-handoff" not in kinds
    assert store.ready_tasks(mission["id"]) == []


def test_status_names_the_provider_and_what_to_do(tmp_path: Path) -> None:
    store, mission, task_id, hook, context, config = _declined_mission(tmp_path)
    hook.after_iteration(context, {"provider": "claude"}, config, 1, common.OPERATOR_DECLINED_STATUS, 12.0)

    snapshot = status_snapshot(store, mission["id"])
    attention = needs_attention(snapshot)

    assert attention is not None and attention["kind"] == "blocked"
    assert attention["message"] == EXPECTED
    assert snapshot["needs_you"]["message"] == EXPECTED


def test_other_failures_still_ask_to_retry(tmp_path: Path) -> None:
    store, mission, task_id, hook, context, config = _declined_mission(tmp_path)
    store.finish_operation(context["operation_id"], exit_code=common.AUTONOMY_ABORT_STATUS)

    attention = needs_attention(status_snapshot(store, mission["id"]))

    assert attention is not None
    assert "not allowed to trust" not in attention["message"]
    assert "task retry" in attention["message"]


def test_start_releases_only_the_declined_task(tmp_path: Path) -> None:
    store, mission, task_id, hook, context, config = _declined_mission(tmp_path)
    hook.after_iteration(context, {"provider": "claude"}, config, 1, common.OPERATOR_DECLINED_STATUS, 12.0)
    other = store.add_task(mission["id"], "unrelated blocked work")
    other_op = store.begin_operation(other, iteration=2, station="codex")
    store.finish_operation(other_op, exit_code=common.AUTONOMY_ABORT_STATUS)

    released = store.release_declined_tasks(mission["id"])

    assert released == [store.get_task(task_id)["ordinal"]]
    assert store.get_task(task_id)["status"] == "READY"
    assert store.get_task(other)["status"] == "BLOCKED"
    assert [event["kind"] for event in store.events(mission["id"])].count("task-declined-released") == 1
    assert store.release_declined_tasks(mission["id"]) == []


def test_declined_runs_do_not_use_up_the_provider_failure_budget(tmp_path: Path) -> None:
    store, mission, task_id, hook, context, config = _declined_mission(tmp_path)
    hook.after_iteration(context, {"provider": "claude"}, config, 1, common.OPERATOR_DECLINED_STATUS, 12.0)
    store.release_declined_tasks(mission["id"])
    second = store.begin_operation(task_id, iteration=2, station="claude")
    store.finish_operation(second, exit_code=1)

    continued = store.continue_task_after_provider_failure(second, max_failures=1)

    assert continued is not None and continued["status"] == "READY"


def test_ralph_in_bridge_waits_for_the_operator_instead_of_relaunching(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selection = {
        "index": 0, "provider": "claude", "route": "", "rotation_reason": "ready",
        "all_rate_limited": False, "decision": {"provider": "claude", "usable": True}, "decisions": [],
    }

    class Hooks:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def before_iteration(self):
            self.calls.append("before_iteration")
            if self.calls.count("before_iteration") == 1:
                return {"action": "run", "task_id": 7, "prompt": "task"}
            return {"action": "stop", "reason": "operator holds the task", "exit_code": 0}

        def before_launch(self, context, selection, scheduler_config, iteration):
            self.calls.append("before_launch")

        def after_iteration(self, context, selection, scheduler_config, iteration, status, seconds):
            self.calls.append(f"after_iteration:{status}")

    hooks = Hooks()
    launches: list[str] = []
    monkeypatch.setattr(ralph_robin, "select_provider", lambda *args: selection)
    monkeypatch.setattr(ralph_robin, "sleep_seconds", lambda seconds: None)
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda cfg: (launches.append(cfg.provider), common.OPERATOR_DECLINED_STATUS)[1])
    argv = [
        "--prompt", "x", "--providers", "claude", "--log-dir", str(tmp_path / "logs"),
        "--state-file", str(tmp_path / "state.json"), "--max-duration", "0", "--max-iterations", "0", "--min-iteration-seconds", "0",
    ]

    assert ralph_robin.main(argv, bridge_hooks=hooks) == 0
    assert launches == ["claude"]
    assert hooks.calls == ["before_iteration", "before_launch", f"after_iteration:{common.OPERATOR_DECLINED_STATUS}", "before_iteration"]


def test_console_needs_you_row_uses_the_same_wording(tmp_path: Path) -> None:
    from llm_tools import bridge_tui

    store, mission, task_id, hook, context, config = _declined_mission(tmp_path)
    hook.after_iteration(context, {"provider": "claude"}, config, 1, common.OPERATOR_DECLINED_STATUS, 12.0)

    rows = bridge_tui._needs_you_rows(status_snapshot(store, mission["id"]))

    assert rows == [EXPECTED]


def test_start_with_a_running_controller_releases_the_held_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from llm_tools import bridge_runtime

    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("declined", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    context = {"task_id": task_id}
    config = SimpleNamespace(
        provider="claude", cwd=str(tmp_path), run_dir=None, model="", exec_mode="fresh", headless=True, native_tui=False,
        tmux_target="", tmux_socket="", tmux_pane="", env_overrides={}, prompt_text="x", max_output_bytes=0,
        max_output_rotations=0, log_dir=None,
    )
    assert hook.before_launch(context, {"provider": "claude"}, config, 1)
    hook.after_iteration(context, {"provider": "claude"}, config, 1, common.OPERATOR_DECLINED_STATUS, 12.0)
    assert store.get_task(task_id)["status"] == "BLOCKED"

    controller = bridge_runtime._process_controller_lock(mission["id"])
    controller.acquire(timeout=1)
    try:
        assert bridge_runtime.launch_controller(mission["id"], headless=True) == 0
    finally:
        controller.release()

    assert store.get_task(task_id)["status"] == "READY"
    assert "released task" in capsys.readouterr().err
