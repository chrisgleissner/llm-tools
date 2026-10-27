"""Ralph's optional Bridge boundary hooks preserve the provider loop."""

from __future__ import annotations

from pathlib import Path

from llm_tools import ralph_robin


class FakeBridgeHooks:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def before_iteration(self):
        self.calls.append("before_iteration")
        if self.calls.count("before_iteration") == 1:
            return {"action": "run", "task_id": 7, "prompt": "mission task prompt"}
        return {"action": "stop", "reason": "test complete", "exit_code": 0}

    def before_launch(self, context, selection, scheduler_config, iteration):
        self.calls.append("before_launch")
        self.asserted_prompt = scheduler_config.prompt_text

    def after_iteration(self, context, selection, scheduler_config, iteration, status, seconds):
        self.calls.append("after_iteration")


def test_bridge_hooks_supply_task_prompt_and_wrap_scheduler_boundary(monkeypatch, tmp_path: Path) -> None:
    hooks = FakeBridgeHooks()
    selection = {
        "index": 0,
        "provider": "codex",
        "route": "",
        "rotation_reason": "ready",
        "all_rate_limited": False,
        "decision": {"provider": "codex", "usable": True},
        "decisions": [],
    }
    monkeypatch.setattr(ralph_robin, "select_provider", lambda *args: selection)
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda cfg: 0)
    argv = [
        "--prompt", "legacy prompt must not replace task",
        "--providers", "codex",
        "--log-dir", str(tmp_path / "logs"),
        "--state-file", str(tmp_path / "state.json"),
        "--max-duration", "0",
        "--max-iterations", "0",
        "--min-iteration-seconds", "0",
    ]
    assert ralph_robin.main(argv, bridge_hooks=hooks) == 0
    assert hooks.asserted_prompt.endswith("mission task prompt")
    assert hooks.calls == ["before_iteration", "before_launch", "after_iteration", "before_iteration"]


def test_bridge_hook_can_cancel_launch_when_operator_pauses_at_boundary(monkeypatch, tmp_path: Path) -> None:
    class PausedHooks(FakeBridgeHooks):
        def before_launch(self, context, selection, scheduler_config, iteration):
            self.calls.append("before_launch")
            return False

        def before_iteration(self):
            self.calls.append("before_iteration")
            if self.calls.count("before_iteration") == 1:
                return {"action": "run", "task_id": 7, "prompt": "task"}
            return {"action": "stop", "exit_code": 0}

    hooks = PausedHooks()
    selection = {
        "index": 0, "provider": "codex", "route": "", "rotation_reason": "ready",
        "all_rate_limited": False, "decision": {"provider": "codex", "usable": True}, "decisions": [],
    }
    monkeypatch.setattr(ralph_robin, "select_provider", lambda *args: selection)
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda cfg: (_ for _ in ()).throw(AssertionError("must not launch")))
    argv = ["--prompt", "legacy", "--providers", "codex", "--log-dir", str(tmp_path / "logs"), "--state-file", str(tmp_path / "state.json"), "--max-duration", "0", "--max-iterations", "0", "--min-iteration-seconds", "0"]

    assert ralph_robin.main(argv, bridge_hooks=hooks) == 0
    assert hooks.calls == ["before_iteration", "before_launch", "before_iteration"]

