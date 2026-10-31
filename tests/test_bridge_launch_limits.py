"""A prompt the kernel cannot pass as one exec argument is refused, not retried for minutes."""

from __future__ import annotations

from pathlib import Path

from llm_tools import common, ralph_robin, scheduler
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore


def test_single_argument_at_the_kernel_limit_is_reported_with_its_size() -> None:
    assert common.command_line_limit_violation(["claude", "x" * (common.MAX_ARG_STRLEN - 1)], {}) == ""
    message = common.command_line_limit_violation(["claude", "x" * common.MAX_ARG_STRLEN], {})
    assert message.startswith(f"prompt too large for the provider command line ({common.MAX_ARG_STRLEN} bytes)")


def test_total_argument_and_environment_size_is_checked(monkeypatch) -> None:
    monkeypatch.setattr(common.os, "sysconf", lambda _name: 100_000)
    argv = ["claude", *["y" * 50_000] * 2]
    message = common.command_line_limit_violation(argv, {})
    assert "arguments and environment" in message


def _scheduler_config(tmp_path: Path, prompt: str) -> scheduler.SchedulerConfig:
    return scheduler.SchedulerConfig(
        provider="claude",
        prompt_text=prompt,
        cwd=str(tmp_path),
        log_dir=tmp_path / "logs",
        command_template="provider-that-must-not-start {prompt}",
        exec_mode="fresh",
        headless=True,
    )


def test_oversized_prompt_is_refused_before_launch_without_retries(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", '{"claude":{"available":true,"five_hour":{"remaining":80},"week":{"remaining":80}}}')
    monkeypatch.setattr(scheduler, "submit_once", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch")))
    config = _scheduler_config(tmp_path, "x" * 200_000)

    assert ralph_robin.run_scheduler_inline(config) == common.LAUNCH_REFUSED_STATUS

    run_dir = next((tmp_path / "logs").iterdir())
    assert (run_dir / "attempt-1.status").read_text() == str(common.LAUNCH_REFUSED_STATUS)
    assert "prompt too large for the provider command line (200000 bytes)" in (run_dir / "attempt-1.out").read_text()
    assert not (run_dir / "attempt-2.status").exists()


def test_exit_126_from_the_pane_is_not_retried_on_the_retry_schedule(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", '{"claude":{"available":true,"five_hour":{"remaining":80},"week":{"remaining":80}}}')
    attempts: list[int] = []

    def fake_submit(cfg, logs, attempt, argv):
        attempts.append(attempt)
        (logs.run_dir / f"attempt-{attempt}.status").write_text("126")
        return 1

    monkeypatch.setattr(scheduler, "submit_once", fake_submit)
    config = _scheduler_config(tmp_path, "short")
    config.retry_delays = "1,1,1"

    assert ralph_robin.run_scheduler_inline(config) == 1
    assert attempts == [1]


def test_bridge_fails_the_task_with_a_visible_event_and_does_not_hand_it_to_another_provider(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("large", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "attempt-1.out").write_text("prompt too large for the provider command line (204000 bytes); shorten it\n")
    config = scheduler.SchedulerConfig(provider="codex", cwd=str(tmp_path), run_dir=run_dir, log_dir=None, prompt_text="p")
    context = {"task_id": task_id}

    assert hooks.before_launch(context, {"provider": "codex"}, config, 1)
    hooks.after_iteration(context, {"provider": "codex"}, config, 1, common.LAUNCH_REFUSED_STATUS, 0.1)

    assert store.get_task(task_id)["status"] == "FAILED"
    events = {event["kind"]: event for event in store.events(mission["id"])}
    assert "provider-handoff" not in events
    refused = events["task-launch-refused"]["data"]
    assert refused["reason"].startswith("prompt too large for the provider command line (204000 bytes)")
    assert events["operation-exited"]["data"]["provider_started"] is False


def _ralph_argv(tmp_path: Path) -> list[str]:
    return [
        "--prompt", "task", "--providers", "codex",
        "--log-dir", str(tmp_path / "logs"), "--state-file", str(tmp_path / "state.json"),
        "--max-duration", "0", "--max-iterations", "0", "--min-iteration-seconds", "0",
    ]


def _ready_selection() -> dict:
    return {
        "index": 0, "provider": "codex", "route": "", "rotation_reason": "ready", "all_rate_limited": False,
        "decision": {"provider": "codex", "usable": True}, "decisions": [],
    }


def test_ralph_stops_on_a_refused_launch_without_a_controller(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ralph_robin, "select_provider", lambda *args: _ready_selection())
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda cfg: common.LAUNCH_REFUSED_STATUS)

    assert ralph_robin.main(_ralph_argv(tmp_path)) == common.LAUNCH_REFUSED_STATUS


def test_ralph_leaves_the_next_step_to_the_bridge_controller_after_a_refused_launch(monkeypatch, tmp_path) -> None:
    from tests.test_bridge_hooks import FakeBridgeHooks

    hooks = FakeBridgeHooks()
    monkeypatch.setattr(ralph_robin, "select_provider", lambda *args: _ready_selection())
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda cfg: common.LAUNCH_REFUSED_STATUS)

    assert ralph_robin.main(_ralph_argv(tmp_path), bridge_hooks=hooks) == 0
    assert hooks.calls == ["before_iteration", "before_launch", "after_iteration", "before_iteration"]
