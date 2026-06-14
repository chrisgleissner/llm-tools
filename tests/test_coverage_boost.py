"""Focused unit tests for pure/easily-mocked helpers.

These exercise rendering, parsing, and suspend-decision helpers in-process so
the rate-limit-rotation and stream-rendering paths stay covered without driving
real providers, systemd, or wall-clock sleeps.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from llm_tools import common, ralph_robin, scheduler


# --------------------------------------------------------------------------- #
# scheduler: Claude stream rendering
# --------------------------------------------------------------------------- #


def test_render_claude_content_block_variants() -> None:
    assert scheduler.render_claude_content_block("plain text") == "plain text"
    assert scheduler.render_claude_content_block(123) == ""  # type: ignore[arg-type]
    assert scheduler.render_claude_content_block({"type": "text", "text": "hi"}) == "hi"

    tool_use = scheduler.render_claude_content_block(
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
    )
    assert tool_use.startswith("Tool call: Bash\n")
    assert '"command": "ls"' in tool_use

    # Non-serializable input falls back to str() instead of raising.
    weird = scheduler.render_claude_content_block(
        {"type": "tool_use", "name": "x", "input": {1, 2, 3}}
    )
    assert weird.startswith("Tool call: x\n")

    # tool_use with empty input emits only the call line.
    assert scheduler.render_claude_content_block({"type": "tool_use"}) == "Tool call: tool\n"

    ok_result = scheduler.render_claude_content_block(
        {"type": "tool_result", "content": [{"type": "text", "text": "done"}]}
    )
    assert ok_result == "Tool result:\ndone"

    err_result = scheduler.render_claude_content_block(
        {"type": "tool_result", "content": "boom", "is_error": True}
    )
    assert err_result == "Tool error:\nboom"

    assert scheduler.render_claude_content_block({"type": "tool_result", "content": None}) == ""
    assert scheduler.render_claude_content_block({"type": "unknown"}) == ""


def test_claude_stream_renderer_events() -> None:
    renderer = scheduler.ClaudeStreamRenderer()
    assert renderer.render_event({"type": "assistant", "message": "not-a-dict"}) == ""

    out = renderer.render_event(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}}
    )
    assert out == "answer"
    assert renderer.rendered_assistant_text is True

    user = renderer.render_event(
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "r"}]}}
    )
    assert "Tool result:" in user
    assert renderer.render_event({"type": "user", "message": 5}) == ""

    delta = scheduler.ClaudeStreamRenderer()
    assert delta.render_event({"type": "content_block_delta", "delta": {"text": "chunk"}}) == "chunk"
    assert delta.rendered_assistant_text is True

    # result is only surfaced when no assistant text was rendered yet.
    fresh = scheduler.ClaudeStreamRenderer()
    assert fresh.render_event({"type": "result", "result": "final"}) == "final"
    fresh.rendered_assistant_text = True
    assert fresh.render_event({"type": "result", "result": "final"}) == ""
    assert fresh.render_event({"type": "system"}) == ""


def test_claude_stream_renderer_render_line() -> None:
    renderer = scheduler.ClaudeStreamRenderer()
    assert renderer.render_line(b"   \n") == b""
    # Invalid JSON is passed through unchanged.
    assert renderer.render_line(b"not json") == b"not json"
    # Valid JSON that is not an object renders to nothing.
    assert renderer.render_line(b"[1, 2]") == b""
    rendered = renderer.render_line(
        b'{"type": "assistant", "message": {"content": [{"type": "text", "text": "yo"}]}}'
    )
    assert rendered == b"yo\n"


# --------------------------------------------------------------------------- #
# common: configurable line prefix
# --------------------------------------------------------------------------- #


def test_render_line_prefix_fields_and_order() -> None:
    # time field renders HH:MM:SS; combine with tool in the configured order.
    assert common.render_line_prefix(["time"], "codex", now=0).startswith(b"[")
    assert common.render_line_prefix(["tool"], "codex") == b"[codex] "
    assert common.render_line_prefix(["tool", "time"], "codex", now=0).endswith(b"] ")
    # An empty selection emits no marker at all (not even brackets).
    assert common.render_line_prefix([], "codex") == b""
    # The "tool"/"usage" fields drop out when no tool is known.
    assert common.render_line_prefix(["tool"], "") == b""


def test_render_line_prefix_usage_field_uses_cache() -> None:
    cache = common.UsagePrefixCache(clock=lambda: 0.0, builder=lambda tool: "5h=10% week=30%")
    out = common.render_line_prefix(["tool", "usage"], "codex", usage_cache=cache)
    assert out == b"[codex 5h=10% week=30%] "


def test_usage_prefix_cache_ttl_and_fallback() -> None:
    calls: list[str] = []
    now = {"t": 0.0}

    def builder(tool: str) -> str:
        calls.append(tool)
        return f"v{len(calls)}"

    cache = common.UsagePrefixCache(clock=lambda: now["t"], builder=builder)
    assert cache.get("codex", ttl=15.0) == "v1"
    # Within the TTL the cached value is reused (no second build).
    now["t"] = 10.0
    assert cache.get("codex", ttl=15.0) == "v1"
    assert calls == ["codex"]
    # Past the TTL it refreshes.
    now["t"] = 20.0
    assert cache.get("codex", ttl=15.0) == "v2"
    assert calls == ["codex", "codex"]

    # A builder failure reuses the last known value instead of breaking output.
    def boom(tool: str) -> str:
        raise RuntimeError("usage source down")

    failing = common.UsagePrefixCache(clock=lambda: now["t"], builder=boom)
    now["t"] = 100.0
    assert failing.get("codex", ttl=15.0) == ""  # no prior value -> empty


def test_line_prefixer_chunked_lines_stamped_once() -> None:
    prefixer = common.LinePrefixer(["tool"], "codex")
    # Half a line, then the rest: the marker appears once at the true line start.
    assert prefixer.apply(b"hel") == b"[codex] hel"
    assert prefixer.apply(b"lo\nworld\n") == b"lo\n[codex] world\n"
    # Disabled prefixer is a byte-exact passthrough.
    off = common.LinePrefixer([], "codex")
    assert off.apply(b"raw\n") == b"raw\n"


def test_parse_prefix_fields() -> None:
    assert ralph_robin.parse_prefix_fields("time,tool") == ["time", "tool"]
    assert ralph_robin.parse_prefix_fields("tool,time") == ["tool", "time"]
    # De-duplicated, whitespace tolerant.
    assert ralph_robin.parse_prefix_fields(" time , time ,tool") == ["time", "tool"]
    # "none"/"off"/empty disable entirely.
    assert ralph_robin.parse_prefix_fields("none") == []
    assert ralph_robin.parse_prefix_fields("") == []
    with pytest.raises(SystemExit):
        ralph_robin.parse_prefix_fields("time,bogus")


# --------------------------------------------------------------------------- #
# scheduler: progress guard + small helpers
# --------------------------------------------------------------------------- #


def test_progress_guard_detects_prompts_and_stalls() -> None:
    guard = scheduler.ProgressGuard()
    # A blocking prompt is reported immediately.
    assert guard.note_output("Press Enter to confirm") is True

    # A trailing question arms the question watchdog without blocking.
    assert guard.note_output("How should I proceed?") is False
    assert guard.question_seen_at is not None

    # Subsequent non-question output clears the pending question.
    assert guard.note_output("still working on it") is False
    assert guard.question_seen_at is None

    # No stall yet.
    assert guard.overdue() is None

    # Idle timeout fires when no progress for longer than idle_timeout.
    guard.last_progress = time.time() - (guard.idle_timeout + 5)
    assert "no output progress" in (guard.overdue() or "")

    # Question timeout fires when a question went unanswered.
    fresh = scheduler.ProgressGuard()
    fresh.last_progress = time.time()
    fresh.question_seen_at = time.time() - (fresh.question_idle_timeout + 5)
    assert "required a response" in (fresh.overdue() or "")


def test_is_undetermined_reason_and_sleep_until(monkeypatch: pytest.MonkeyPatch) -> None:
    assert scheduler.is_undetermined_reason("rate-limited") is False
    assert scheduler.is_undetermined_reason("inconclusive-usage") is True

    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "2000")
    # Target already in the past: must not sleep.
    slept: list[float] = []
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: slept.append(s))
    scheduler.sleep_until(1000)
    assert slept == []
    # Future target sleeps for the difference.
    scheduler.sleep_until(2030)
    assert slept == [30]


def test_provider_default_argv_kilo_and_opencode_cwd_handling() -> None:
    attached_kilo = scheduler.SchedulerConfig(tool="kilo", cwd="/tmp/work", attached=True)
    assert scheduler.provider_default_argv(attached_kilo, "prompt") == ["kilo", "run", "prompt"]

    headless_kilo = scheduler.SchedulerConfig(tool="kilo", cwd="/tmp/work")
    assert scheduler.provider_default_argv(headless_kilo, "prompt") == ["kilo", "run", "--auto", "prompt"]

    attached_opencode = scheduler.SchedulerConfig(tool="opencode", cwd="/tmp/work", attached=True)
    assert scheduler.provider_default_argv(attached_opencode, "prompt") == ["opencode"]


# --------------------------------------------------------------------------- #
# ralph_robin: duration parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("24h", 86400),
        ("90m", 5400),
        ("30s", 30),
        ("1d", 86400),
        ("1.5h", 5400),
        ("100", 100),
        ("0", 0),
        ("", None),
        ("abc", None),
        ("-5", None),
    ],
)
def test_parse_duration(text: str, expected: int | None) -> None:
    assert ralph_robin.parse_duration(text) == expected


# --------------------------------------------------------------------------- #
# ralph_robin: selection + suspend decisions
# --------------------------------------------------------------------------- #


def test_soonest_wait_until(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    assert ralph_robin.soonest_wait_until({"decisions": "not-a-list"}) is None
    assert ralph_robin.soonest_wait_until({}) is None
    # Only future resets count; the earliest future one wins.
    selection = {"decisions": [{"wait_until": 900}, {"wait_until": 1500}, {"wait_until": 1200}]}
    assert ralph_robin.soonest_wait_until(selection) == 1200
    # All in the past -> None.
    assert ralph_robin.soonest_wait_until({"decisions": [{"wait_until": 500}]}) is None


def test_decision_summary() -> None:
    summary = ralph_robin.decision_summary(
        {
            "reason": "usable",
            "windows": [{"name": "5h", "remaining": 42.0}, "ignored", {"name": "weekly"}],
        }
    )
    assert "5h 42% left" in summary
    assert summary.startswith("usable")

    rl = ralph_robin.decision_summary({"reason": "rate-limited", "wait_until": 1234567890})
    assert rl.startswith("rate-limited")
    assert "until " in rl


def _logs(tmp_path: Path) -> common.RunLogs:
    return common.setup_run_logs(tmp_path / "logs", "test")


def test_rtc_suspend_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    logs = _logs(tmp_path)
    monkeypatch.delenv("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", raising=False)

    # Missing systemd tooling -> no suspend.
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    assert ralph_robin.rtc_suspend(logs, 999999999999) is False

    # systemd present but the target is too soon to bother arming a timer.
    monkeypatch.setattr(common, "have_cmd", lambda name: True)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setenv("LLM_SCHEDULER_SUSPEND_MIN_LEAD", "120")
    assert ralph_robin.rtc_suspend(logs, 1010) is False


def test_suspend_machine_until(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = ralph_robin.RalphConfig()
    logs = _logs(tmp_path)
    monkeypatch.setattr(ralph_robin, "rtc_suspend", lambda *a, **k: False)
    sleeps: list[float] = []
    monkeypatch.setattr(ralph_robin, "sleep_seconds", lambda s: sleeps.append(s))
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")

    # Budget already exhausted -> stop the loop.
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 1000.0)
    assert ralph_robin.suspend_machine_until(cfg, logs, 2000, start_monotonic=0.0, max_duration=10) is False

    # Wait clamped to the remaining budget, then in-process sleep covers the gap.
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 0.0)
    assert ralph_robin.suspend_machine_until(cfg, logs, 5000, start_monotonic=0.0, max_duration=10) is True
    assert sleeps and sleeps[-1] == 10

    # No duration cap: sleep until the full renewal target.
    sleeps.clear()
    assert ralph_robin.suspend_machine_until(cfg, logs, 1030, start_monotonic=0.0, max_duration=0) is True
    assert sleeps[-1] == 30


def test_suspend_until_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = ralph_robin.RalphConfig()
    logs = _logs(tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(ralph_robin, "sleep_seconds", lambda s: sleeps.append(s))
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")

    # Known reset target: sleep until it.
    selection = {"decisions": [{"wait_until": 1090}]}
    assert ralph_robin.suspend_until_available(cfg, logs, selection, 0.0, 0, "rate-limited") is True
    assert sleeps[-1] == 90

    # No known reset: fall back to one poll interval.
    sleeps.clear()
    assert ralph_robin.suspend_until_available(cfg, logs, {"decisions": []}, 0.0, 0, "unknown") is True
    assert sleeps[-1] == float(int(cfg.poll_interval))

    # Budget exhausted -> stop the loop.
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 1000.0)
    assert ralph_robin.suspend_until_available(cfg, logs, selection, 0.0, 10, "rate-limited") is False


# --------------------------------------------------------------------------- #
# ralph_robin: argument parsing + validation errors
# --------------------------------------------------------------------------- #


def test_parse_args_help_and_unknown_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as help_exc:
        ralph_robin.parse_args(["--help"])
    assert help_exc.value.code == 0
    assert "Usage: ralph-robin" in capsys.readouterr().out

    with pytest.raises(SystemExit) as bad_exc:
        ralph_robin.parse_args(["--bogus"])
    assert bad_exc.value.code == 2
    assert "unknown option" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_iterations", "-1"),
        ("max_iterations", "x"),
        ("max_duration", "later"),
        ("min_iteration_seconds", "nope"),
    ],
)
def test_validate_args_rejects_bad_values(field: str, value: str) -> None:
    cfg = ralph_robin.RalphConfig(prompt_text="do work")
    setattr(cfg, field, value)
    with pytest.raises(SystemExit) as exc:
        ralph_robin.validate_args(cfg)
    assert exc.value.code == 2


@pytest.mark.parametrize("var", ["LLM_SCHEDULER_IDLE_TIMEOUT", "LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT"])
def test_validate_args_rejects_bad_timeout_env(var: str, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ralph_robin.RalphConfig(prompt_text="do work")
    monkeypatch.setenv(var, "bad")
    with pytest.raises(SystemExit) as exc:
        ralph_robin.validate_args(cfg)
    assert exc.value.code == 2
