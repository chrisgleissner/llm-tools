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
    # time field renders HH:MM:SS; combine with provider in the configured order.
    assert common.render_line_prefix(["time"], "codex", now=0).startswith(b"[")
    assert common.render_line_prefix(["provider"], "codex") == b"[codex] "
    assert common.render_line_prefix(["provider", "time"], "codex", now=0).endswith(b"] ")
    # An empty selection emits no marker at all (not even brackets).
    assert common.render_line_prefix([], "codex") == b""
    # The "provider"/"usage" fields drop out when no provider is known.
    assert common.render_line_prefix(["provider"], "") == b""


def test_render_line_prefix_usage_field_uses_cache() -> None:
    cache = common.UsagePrefixCache(clock=lambda: 0.0, builder=lambda provider: "5h=10% week=30%")
    out = common.render_line_prefix(["provider", "usage"], "codex", usage_cache=cache)
    assert out == b"[codex 5h=10% week=30%] "


def test_usage_prefix_cache_ttl_and_fallback() -> None:
    calls: list[str] = []
    now = {"t": 0.0}

    def builder(provider: str) -> str:
        calls.append(provider)
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
    def boom(provider: str) -> str:
        raise RuntimeError("usage source down")

    failing = common.UsagePrefixCache(clock=lambda: now["t"], builder=boom)
    now["t"] = 100.0
    assert failing.get("codex", ttl=15.0) == ""  # no prior value -> empty


def test_line_prefixer_chunked_lines_stamped_once() -> None:
    prefixer = common.LinePrefixer(["provider"], "codex")
    # Half a line, then the rest: the marker appears once at the true line start.
    assert prefixer.apply(b"hel") == b"[codex] hel"
    assert prefixer.apply(b"lo\nworld\n") == b"lo\n[codex] world\n"
    # Disabled prefixer is a byte-exact passthrough.
    off = common.LinePrefixer([], "codex")
    assert off.apply(b"raw\n") == b"raw\n"


def test_parse_prefix_fields() -> None:
    assert ralph_robin.parse_prefix_fields("time,provider") == ["time", "provider"]
    assert ralph_robin.parse_prefix_fields("provider,time") == ["provider", "time"]
    # De-duplicated, whitespace tolerant.
    assert ralph_robin.parse_prefix_fields(" time , time ,provider") == ["time", "provider"]
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
    attached_kilo = scheduler.SchedulerConfig(provider="kilo", cwd="/tmp/work", attached=True)
    assert scheduler.provider_default_argv(attached_kilo, "prompt") == ["kilo", "run", "prompt"]

    headless_kilo = scheduler.SchedulerConfig(provider="kilo", cwd="/tmp/work")
    assert scheduler.provider_default_argv(headless_kilo, "prompt") == [
        "kilo", "run", "--dir", "/tmp/work", "prompt",
    ]

    attached_opencode = scheduler.SchedulerConfig(provider="opencode", cwd="/tmp/work", attached=True)
    assert scheduler.provider_default_argv(attached_opencode, "prompt") == ["opencode"]

    headless_opencode = scheduler.SchedulerConfig(provider="opencode", cwd="/tmp/work")
    assert scheduler.provider_default_argv(headless_opencode, "prompt") == [
        "opencode", "run", "--dir", "/tmp/work", "prompt",
    ]


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


def test_suspend_block_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_RALPH_MIN_AWAKE_SECONDS", "60")
    monkeypatch.delenv("LLM_RALPH_MAX_SUSPENDS", raising=False)
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 1000.0)

    # Fresh state: nothing blocks a suspend.
    assert ralph_robin.suspend_block_reason(ralph_robin.SuspendState()) == ""

    # Fail-safe latch after an unreliable wake: stay awake from here on.
    disabled = ralph_robin.SuspendState(disabled=True, disabled_reason="unreliable-wake(drift=900s)")
    assert "unreliable-wake" in ralph_robin.suspend_block_reason(disabled)

    # Min-awake guard: we resumed too recently to suspend again.
    just_woke = ralph_robin.SuspendState(last_resume_monotonic=990.0)  # 10s ago < 60s
    assert ralph_robin.suspend_block_reason(just_woke).startswith("min-awake")
    long_awake = ralph_robin.SuspendState(last_resume_monotonic=900.0)  # 100s ago
    assert ralph_robin.suspend_block_reason(long_awake) == ""

    # Per-run cap.
    monkeypatch.setenv("LLM_RALPH_MAX_SUSPENDS", "2")
    assert ralph_robin.suspend_block_reason(ralph_robin.SuspendState(suspends=2)).startswith("max-suspends")
    assert ralph_robin.suspend_block_reason(ralph_robin.SuspendState(suspends=1)) == ""


def _wall_clock_sleep(monkeypatch: pytest.MonkeyPatch, now: dict[str, int]) -> list[float]:
    """Make now_epoch() track a fake wall clock that each sleep advances."""
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += int(round(seconds))

    monkeypatch.setattr(common, "now_epoch", lambda env=None: now["t"])
    monkeypatch.setattr(ralph_robin, "sleep_seconds", fake_sleep)
    return sleeps


def test_suspend_machine_until_awake_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When suspend_with_wake does not actually suspend (backend unavailable /
    simulated), the loop falls back to a bounded wall-clock wait that ends on time."""
    cfg = ralph_robin.RalphConfig()
    logs = _logs(tmp_path)
    state = ralph_robin.SuspendState()
    # suspend_with_wake reports "not suspended" (e.g. simulated) -> awake fallback.
    monkeypatch.setattr(
        common, "suspend_with_wake",
        lambda *a, **k: common.SuspendOutcome(False, None, int(a[0]), None, True, "simulated", "simulated"),
    )
    monkeypatch.setenv("LLM_RALPH_WAIT_POLL_SECONDS", "5")
    now = {"t": 1000}
    sleeps = _wall_clock_sleep(monkeypatch, now)

    # Budget already exhausted -> stop the loop.
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 1000.0)
    assert ralph_robin.suspend_machine_until(cfg, logs, 2000, 0.0, 10, state) is False

    # Wait clamped to the remaining budget, then chunked wall-clock wait covers it.
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 0.0)
    now["t"] = 1000
    sleeps.clear()
    assert ralph_robin.suspend_machine_until(cfg, logs, 5000, 0.0, 10, state) is True
    assert sum(sleeps) == 10 and max(sleeps) <= 5

    # No duration cap: wall clock advances in <=chunk steps up to the renewal.
    now["t"] = 1000
    sleeps.clear()
    assert ralph_robin.suspend_machine_until(cfg, logs, 1030, 0.0, 0, state) is True
    assert sum(sleeps) == 30 and max(sleeps) <= 5


def test_suspend_machine_until_unreliable_wake_latches_awake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unreliable wake disables further suspends for the rest of the run."""
    cfg = ralph_robin.RalphConfig()
    logs = _logs(tmp_path)
    state = ralph_robin.SuspendState()
    calls: list[int] = []

    def fake_suspend(target_epoch: int, **kwargs: object) -> common.SuspendOutcome:
        calls.append(target_epoch)
        # Woke 900s after target: the RTC wake clearly misbehaved.
        return common.SuspendOutcome(True, target_epoch + 900, target_epoch, 900, False, "systemd-timer", "ok")

    monkeypatch.setattr(common, "suspend_with_wake", fake_suspend)
    monkeypatch.setattr(ralph_robin, "wait_until_epoch", lambda target: None)
    monkeypatch.setattr(common, "now_epoch", lambda env=None: 1000)
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 0.0)

    assert ralph_robin.suspend_machine_until(cfg, logs, 5000, 0.0, 0, state) is True
    assert state.suspends == 1 and state.disabled is True and "unreliable-wake" in state.disabled_reason
    # Next time we must stay awake (no second real suspend attempt).
    assert ralph_robin.suspend_machine_until(cfg, logs, 9000, 0.0, 0, state) is True
    assert calls == [5000]  # suspend_with_wake was not called the second time


def test_suspend_until_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = ralph_robin.RalphConfig()
    logs = _logs(tmp_path)
    monkeypatch.setenv("LLM_RALPH_WAIT_POLL_SECONDS", "5")
    now = {"t": 1000}
    sleeps = _wall_clock_sleep(monkeypatch, now)

    # Known reset target: wait until it, in bounded chunks.
    selection = {"decisions": [{"wait_until": 1090}]}
    assert ralph_robin.suspend_until_available(cfg, logs, selection, 0.0, 0, "rate-limited") is True
    assert sum(sleeps) == 90 and max(sleeps) <= 5

    # No known reset: fall back to one poll interval.
    now["t"] = 1000
    sleeps.clear()
    assert ralph_robin.suspend_until_available(cfg, logs, {"decisions": []}, 0.0, 0, "unknown") is True
    assert sum(sleeps) == float(int(cfg.poll_interval)) and max(sleeps) <= 5

    # Budget exhausted -> stop the loop.
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 1000.0)
    assert ralph_robin.suspend_until_available(cfg, logs, selection, 0.0, 10, "rate-limited") is False


def test_suspend_machine_until_reliable_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = ralph_robin.RalphConfig()
    logs = _logs(tmp_path)
    state = ralph_robin.SuspendState()
    monkeypatch.setattr(
        common, "suspend_with_wake",
        lambda target, **k: common.SuspendOutcome(True, target, target, 0, True, "systemd-timer", "ok"),
    )
    monkeypatch.setattr(common, "now_epoch", lambda env=None: 1000)
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 5.0)
    assert ralph_robin.suspend_machine_until(cfg, logs, 5000, 0.0, 0, state) is True
    assert state.suspends == 1 and state.disabled is False and state.last_resume_monotonic == 5.0


def test_guard_against_auto_suspend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    logs = _logs(tmp_path)
    calls = {"acquire": 0}

    class FakeInhibitor:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def acquire(self) -> bool:
            calls["acquire"] += 1
            return False

        def release(self) -> None:
            pass

    monkeypatch.setattr(common, "IdleSuspendInhibitor", FakeInhibitor)
    # dry-run leaves machine power state untouched (no acquire).
    ralph_robin.guard_against_auto_suspend(ralph_robin.RalphConfig(dry_run=True), logs)
    assert calls["acquire"] == 0
    # a real run attempts to take the idle inhibitor.
    ralph_robin.guard_against_auto_suspend(ralph_robin.RalphConfig(dry_run=False), logs)
    assert calls["acquire"] == 1


def test_report_prior_suspend_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    logs = _logs(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(common, "suspend_ledger_path", lambda env=None: ledger)
    # No ledger yet -> no-op.
    ralph_robin.report_prior_suspend_failures(logs)
    # A start with no done is a prior wedged/aborted cycle -> warns without raising.
    common.ledger_record_start(ledger, "c1", 2000, {"XDG_CACHE_HOME": str(tmp_path)})
    ralph_robin.report_prior_suspend_failures(logs)
    assert ledger.exists()


def test_ralph_watchdog_flag() -> None:
    cfg = ralph_robin.parse_args(["--prompt", "x", "--watchdog"])
    assert cfg.watchdog is True
    assert ralph_robin.RalphConfig().watchdog is False


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
