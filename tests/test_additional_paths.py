from __future__ import annotations

import json
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm_tools import common, copilot_refresh, ralph_robin, scheduler, usage

from .conftest import run_cmd, write_exe


def test_usage_option_branches_and_unavailable(env: dict[str, str], tmp_path: Path) -> None:
    base = env | {"LLM_USAGE_DISABLE_COPILOT": "1"}
    assert run_cmd(["./llm-usage", "--no-header", "--hide-remaining-time", "--hide-source"], base).returncode == 0
    bad_offset = run_cmd(["./llm-usage", "--copilot-monthly-reset-offset-days", "x"], env)
    assert bad_offset.returncode == 2
    assert "expects an integer" in bad_offset.stderr
    missing_value = run_cmd(["./llm-usage", "--copilot-monthly-reset-offset-days"], env)
    assert missing_value.returncode == 2
    unknown = run_cmd(["./llm-usage", "--bad"], env)
    assert unknown.returncode == 2
    no_footer = run_cmd(["./llm-usage", "--json"], env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "No footer here"})
    assert json.loads(no_footer.stdout)["copilot"]["reason"] == "format-changed"
    timeout = common.read_copilot_live(env | {"LLM_USAGE_COPILOT_CAPTURE_CMD": "sleep 2", "LLM_USAGE_COPILOT_TIMEOUT": "1"})
    assert timeout["available"] is False


def test_usage_main_inprocess_and_render_helpers(env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("LLM_USAGE_DISABLE_COPILOT", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert usage.main(["--statusline"]) == 0
    assert capsys.readouterr().out.strip() == "Claude"
    cfg = usage.Config()
    cfg.no_header = True
    cfg.show_source = True
    cfg.show_remaining_time = False
    usage.print_unavailable_rows(cfg, "Missing")
    out = capsys.readouterr().out
    assert "Missing" in out


def test_scheduler_argument_branches(env: dict[str, str], tmp_path: Path) -> None:
    cases = [
        ["./llm-scheduler", "--tool"],
        ["./llm-scheduler", "--prompt"],
        ["./llm-scheduler", "--prompt-file"],
        ["./llm-scheduler", "--cwd"],
        ["./llm-scheduler", "--tmux"],
        ["./llm-scheduler", "--command-template"],
        ["./llm-scheduler", "--headless-idle-timeout"],
        ["./llm-scheduler", "--run-dir"],
        ["./llm-scheduler", "--unknown"],
    ]
    for args in cases:
        assert run_cmd(args, env).returncode == 2
    bad_at = run_cmd(["./llm-scheduler", "--tool", "codex", "--prompt", "x", "--at", "not-a-date", "--log-dir", str(tmp_path / "logs")], env)
    assert bad_at.returncode == 2
    assert not (tmp_path / "logs").exists()
    bad_env = run_cmd(["./llm-scheduler", "--tool", "codex", "--prompt", "x"], env | {"LLM_SCHEDULER_IDLE_TIMEOUT": "bad"})
    assert bad_env.returncode == 2
    wake = run_cmd(["./llm-scheduler", "--wake-test"], env)
    assert wake.returncode == 0
    assert json.loads(wake.stdout)["note"].startswith("wake is best effort")
    guarded = run_cmd(
        ["./llm-scheduler", "--tool", "claude", "--prompt", "x", "--suspend-until-ready"],
        env | {"LLM_TOOLS_RALPH_ROBIN_ACTIVE": "1"},
    )
    assert guarded.returncode == common.AUTONOMY_ABORT_STATUS
    assert "disabled inside an active ralph-robin" in guarded.stderr
    allowed = run_cmd(
        ["./llm-scheduler", "--tool", "claude", "--prompt", "x", "--suspend-until-ready", "--dry-run", "--command-template", "true"],
        env | {"LLM_TOOLS_RALPH_ROBIN_ACTIVE": "1", "LLM_TOOLS_RALPH_ROBIN_ALLOW_SUSPEND": "1", "LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}'},
    )
    assert allowed.returncode == 0


def test_scheduler_unavailable_suspend_and_no_stream(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    unavailable = '{"available":false,"reason":"missing-cli"}'
    result = run_cmd(
        [
            "./llm-scheduler",
            "--tool",
            "claude",
            "--prompt",
            "x",
            "--command-template",
            "provider-mock",
            "--max-unavailable-wait",
            "1",
            "--poll-interval",
            "1",
            "--no-retry",
            "--log-dir",
            str(tmp_path / "unavail"),
        ],
        env | {"LLM_SCHEDULER_USAGE_JSON": unavailable},
    )
    assert result.returncode == 0
    assert "chat ok" in result.stdout
    quiet = run_cmd(
        ["./llm-scheduler", "--tool", "codex", "--prompt", "x", "--command-template", "provider-mock", "--log-dir", str(tmp_path / "quiet")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}', "LLM_SCHEDULER_NO_STREAM": "1"},
    )
    assert quiet.returncode == 0
    assert "chat ok" not in quiet.stdout


def test_scheduler_suspend_dry_run_and_failures(env: dict[str, str], fake_bin: Path, tmp_path: Path) -> None:
    write_exe(fake_bin / "systemd-run", "#!/usr/bin/env python3\nprint('Running timer as unit: mocked.timer')\n")
    write_exe(fake_bin / "systemctl", "#!/usr/bin/env python3\nimport sys\nprint('running' if sys.argv[1:3] == ['--user','is-system-running'] else '')\n")
    exhausted = '{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}}'
    dry = run_cmd(
        ["./llm-scheduler", "--tool", "codex", "--prompt", "x", "--command-template", "true", "--suspend-until-ready", "--dry-run", "--log-dir", str(tmp_path / "dry")],
        env | {"LLM_USAGE_NOW_EPOCH": "1780430000", "LLM_SCHEDULER_USAGE_JSON": exhausted},
    )
    assert dry.returncode == 0
    assert "would schedule" in dry.stdout
    near = run_cmd(
        ["./llm-scheduler", "--tool", "codex", "--prompt", "x", "--command-template", "true", "--suspend-until-ready", "--dry-run", "--log-dir", str(tmp_path / "near")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":0,"resets_at":9999999999},"week":{"remaining":50}}', "LLM_USAGE_NOW_EPOCH": "9999999970"},
    )
    assert "suspend scheduling failed" in near.stderr


def test_scheduler_tmux_missing_and_template_error(env: dict[str, str], tmp_path: Path) -> None:
    result = run_cmd(
        ["./llm-scheduler", "--tool", "codex", "--prompt", "x", "--command-template", "unterminated '", "--log-dir", str(tmp_path / "bad-template")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}'},
    )
    assert result.returncode == 1
    tmux = run_cmd(
        ["./llm-scheduler", "--tool", "codex", "--prompt", "x", "--tmux", ":", "--no-retry", "--log-dir", str(tmp_path / "tmux")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}'},
    )
    assert tmux.returncode == 1


def test_ralph_and_scheduler_highlight_helpers(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert scheduler.provider_env(scheduler.SchedulerConfig()) is None
    env = scheduler.provider_env(scheduler.SchedulerConfig(ralph_robin_active=True))
    assert env is not None
    assert env["LLM_TOOLS_RALPH_ROBIN_ACTIVE"] == "1"

    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("LLM_USAGE_NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert scheduler.stream_color_enabled(Tty()) is True
    assert b"\x1b[32m+added\x1b[0m\n" == scheduler.highlight_provider_text(b"+added\n", stream_name="stdout", enabled=True)
    assert b"\x1b[31m-removed\x1b[0m\n" == scheduler.highlight_provider_text(b"-removed\n", stream_name="stdout", enabled=True)
    assert b"\x1b[36;1m@@ hunk\x1b[0m\n" == scheduler.highlight_provider_text(b"@@ hunk\n", stream_name="stdout", enabled=True)
    assert b"\x1b[36mgit status\x1b[0m\n" == scheduler.highlight_provider_text(b"git status\n", stream_name="stdout", enabled=True)
    assert b"\x1b[31;1merror failed\x1b[0m\n" == scheduler.highlight_provider_text(b"error failed\n", stream_name="stdout", enabled=True)
    assert b"\x1b[33mprogress\x1b[0m\n" == scheduler.highlight_provider_text(b"progress\n", stream_name="stderr", enabled=True)
    assert scheduler.highlight_provider_text(b"\x1b[31mred\x1b[0m\n", stream_name="stdout", enabled=True) == b"\x1b[31mred\x1b[0m\n"

    decision = {"tool": "claude", "usable": False, "reason": "rate-limited", "wait_until": 2000, "windows": [{"name": "5h", "remaining": 0}]}
    assert "rate-limited" in ralph_robin.decision_summary(decision)
    ralph_robin.print_usage_summary({"decisions": [decision, {"tool": "codex", "usable": True, "reason": "usable", "windows": [{"name": "5h", "remaining": 61.5}]}]})
    assert "claude" in capsys.readouterr().err


def test_ralph_validation_dry_run_rotation_and_autonomy(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    assert run_cmd(["./ralph-robin"], env).returncode == 2
    assert run_cmd(["./ralph-robin", "--tools", "bad", "--prompt", "x"], env).returncode == 2
    usage_json = '{"claude":{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}}'
    dry = run_cmd(
        ["./ralph-robin", "--prompt", "x", "--command-template", "provider-mock {tool}", "--dry-run", "--state-file", str(tmp_path / "s.json"), "--log-dir", str(tmp_path / "logs")],
        env | {"LLM_USAGE_NOW_EPOCH": "1780430000", "LLM_SCHEDULER_USAGE_JSON": usage_json},
    )
    assert dry.returncode == 0
    assert "dry-run" in dry.stderr
    run = run_cmd(
        ["./ralph-robin", "--prompt", "x", "--command-template", "provider-mock {tool}", "--state-file", str(tmp_path / "s2.json"), "--log-dir", str(tmp_path / "logs2"), "--no-retry"],
        env | {"LLM_USAGE_NOW_EPOCH": "1780430000", "LLM_SCHEDULER_USAGE_JSON": usage_json},
    )
    assert run.returncode == 0
    assert json.loads((tmp_path / "s2.json").read_text())["current_tool"] == "codex"
    blocked = run_cmd(
        ["./ralph-robin", "--prompt", "x", "--command-template", "provider-mock {tool}", "--state-file", str(tmp_path / "s3.json"), "--log-dir", str(tmp_path / "logs3"), "--no-retry"],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"claude":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}}', "PROVIDER_MODE": "blocking"},
    )
    assert blocked.returncode == common.AUTONOMY_ABORT_STATUS


def test_common_extra_branches(env: dict[str, str], fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert common.fmt_duration("bad") == "-"
    assert common.time_until("bad") == "-"
    assert common.parse_copilot_ai_credits("AI Credits: 17") == 17
    assert common.parse_copilot_monthly_used("Monthly: 42% used") == 42
    assert common.json_for_copilot(None)["reason"] == "unavailable"
    assert common.json_for_copilot({"provider": "copilot", "monthly": {"remaining": 1}, "ai_credits": {"used": 2}}, False).get("ai_credits") is None
    assert common.output_is_retryable(0, "chapter 429") is False
    assert common.output_is_retryable(0, "HTTP 429 Too Many Requests") is True
    assert common.output_is_retryable(42, "") is True
    assert common.argv_to_command_line(["a b", "$x"]) == "'a b' '$x'"
    assert common.template_argv("cmd {tool} {prompt_file} {cwd}", tool="codex", prompt="p", prompt_file=tmp_path / "p.txt", cwd="/tmp") == ["cmd", "codex", str(tmp_path / "p.txt"), "/tmp"]
    assert common.read_copilot_live(env | {"LLM_USAGE_DISABLE_COPILOT": "1"})["reason"] == "disabled"
    assert common.read_copilot_live(env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "auth required"})["reason"] == "not-authenticated"
    assert common.read_copilot_live(env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "Monthly: 5% used AI Credits: 9"})["monthly"]["remaining"] == 95
    write_exe(fake_bin / "date", "#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    assert common.parse_epoch("bad-date") is None
    monkeypatch.setenv("HOME", env["HOME"])
    cache = common.usage_cache_dir()
    cache.mkdir(parents=True)
    (cache / "claude-usage-api.json").write_text('{"rate_limits":{"five_hour":{"used_percentage":20}}}', encoding="utf-8")
    assert common.read_claude_api()["five_hour"]["used"] == 20


def test_parser_option_coverage(tmp_path: Path) -> None:
    prompt = tmp_path / "p.txt"
    prompt.write_text("x", encoding="utf-8")
    scfg = scheduler.parse_args([
        "--tool", "claude", "--prompt-file", str(prompt), "--at", "@100",
        "--window", "5h", "--min-remaining", "2", "--poll-interval", "3",
        "--max-unavailable-wait", "4", "--retry-delays", "5", "--cwd", str(tmp_path),
        "--fresh", "--headless", "--tmux", "s:w", "--auto-confirm", "--no-auto-confirm",
        "--headless-idle-timeout", "7", "--headless-question-timeout", "8",
        "--log-dir", str(tmp_path / "logs"), "--run-dir", str(tmp_path / "run"),
        "--wake", "--suspend-until-ready",
    ])
    assert scfg.tool == "claude"
    assert scfg.tmux_target == "s:w"
    assert scfg.suspend_until_ready is True
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(tool="codex", cwd="/c", attached=True), "p") == ["codex", "-C", "/c", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(tool="claude", attached=True), "p")[0] == "claude"
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(tool="copilot", cwd="/c", attached=True), "p") == ["copilot", "-C", "/c", "-i", "p"]
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(tool="codex")).startswith("Codex")
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(tool="claude")).startswith("Claude")
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(tool="copilot")).startswith("GitHub")

    rcfg = ralph_robin.parse_args([
        "--tools", " claude, codex ,,", "--prompt-file", str(prompt), "--window", "weekly",
        "--min-remaining", "2", "--poll-interval", "3", "--max-unavailable-wait", "4",
        "--retry-delays", "5", "--cwd", str(tmp_path), "--fresh", "--headless",
        "--tmux", "s:w", "--command-template", "true", "--auto-confirm", "--no-auto-confirm",
        "--headless-idle-timeout", "7", "--headless-question-timeout", "8",
        "--log-dir", str(tmp_path / "rlogs"), "--state-file", str(tmp_path / "state.json"),
        "--wake", "--suspend-until-ready",
    ])
    ralph_robin.validate_args(rcfg)
    assert rcfg.tools == ["claude", "codex"]
    assert ralph_robin.safe_args_json(rcfg)["tools"] == ["claude", "codex"]
    with pytest.raises(SystemExit):
        ralph_robin.parse_tools(" , ")


def test_scheduler_system_and_tmux_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logs = common.setup_run_logs(tmp_path / "logs", "t", "codex")
    cfg = scheduler.SchedulerConfig(tool="codex", prompt_text="p", cwd=str(tmp_path), log_dir=tmp_path / "logs", run_dir=logs.run_dir)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setattr(common, "have_cmd", lambda name: name in {"systemd-run", "systemctl", "rtcwake", "tmux"})

    class P:
        def __init__(self, code: int = 0, out: str = "ok") -> None:
            self.returncode = code
            self.stdout = out

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return P(0, "")
        return P(0, "ok")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    monkeypatch.setattr(common.subprocess, "run", fake_run)
    monkeypatch.setenv("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "1")
    assert scheduler.schedule_resume_and_suspend(cfg, logs, 2000, "rate-limited") is True
    assert "scheduled: logs written to" in capsys.readouterr().out
    assert any(c and c[0] == "systemd-run" for c in calls)

    cfg.wake = True
    scheduler.log_wake_plan(cfg, logs, 3000)
    assert cfg.wake_armed_target == 3000
    scheduler.log_wake_plan(cfg, logs, 3000)

    cfg.exec_mode = "tmux"
    cfg.tmux_target = "sess:win"
    status = tmp_path / "status"

    def fake_tmux(args, **kwargs):
        if args[:2] == ["tmux", "has-session"]:
            return P(0, "")
        if args[:2] == ["tmux", "list-windows"]:
            return P(0, "other\n")
        if args[:2] == ["tmux", "capture-pane"]:
            status.write_text("0", encoding="utf-8")
            return P(0, "pane")
        return P(0, "")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_tmux)
    out = tmp_path / "tmux.out"
    assert scheduler.run_tmux(cfg, logs, ["true"], out, status) == 0
    assert "pane" in out.read_text()


def test_common_process_helpers_and_estimators(env: dict[str, str], tmp_path: Path) -> None:
    status, text = common.run_pty_capture(
        [sys.executable, "-c", "print('Confirm folder trust'); input(); print('trusted')"],
        tmp_path,
        5,
        stream=False,
        auto_confirm=True,
        idle_timeout=0,
        question_idle_timeout=0,
    )
    assert status == 0
    assert "trusted" in text
    status2, text2 = common.run_pty_capture(
        [sys.executable, "-c", "print('What do you want to do?'); print('Enter to confirm - Esc to cancel'); import time; time.sleep(5)"],
        tmp_path,
        5,
        stream=False,
        auto_confirm=True,
        idle_timeout=0,
        question_idle_timeout=0,
    )
    assert status2 == common.AUTONOMY_ABORT_STATUS
    assert "autonomous abort" in text2

    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "llm-usage.log").write_text(
        '{"ts":1000,"provider":"p","window":"w","remaining":100}\n'
        '{"ts":1060,"provider":"p","window":"w","remaining":90}\n'
        '{"ts":1120,"provider":"p","window":"w","remaining":80}\n',
        encoding="utf-8",
    )
    assert common.estimate_remaining_time_from_log("p", "w", 40, env | {"LLM_USAGE_NOW_EPOCH": "1120", "LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS": "9999"}) == "4m"
    assert common.estimate_remaining_time_from_log("p", "w", 0, env) == "-"


def test_common_filesystem_provider_paths(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", env["HOME"])
    home = Path(env["HOME"])
    assert common.latest_matching_line(tmp_path / "missing", lambda _o: True, env) is None
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{bad\n{}\n", encoding="utf-8")
    assert common.latest_matching_line(tmp_path, lambda o: o == {}, env) == "{}"
    assert common.window_from("x", 1) is None
    assert common.normalize_codex_obj({}, "s") is None
    assert common.normalize_claude_obj("x", "s") is None
    (home / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "sessions" / "r.jsonl").write_text('{"rateLimits":{"primary":{"usedPercent":5}}}\n', encoding="utf-8")
    assert common.read_codex()["five_hour"]["used"] == 5
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "projects" / "r.jsonl").write_text('{"message":{"rateLimits":{"fiveHour":{"usedPercent":6}}}}\n', encoding="utf-8")
    assert common.read_claude()["five_hour"]["used"] == 6
    ccache = common.usage_cache_dir(env) / "copilot-usage.json"
    ccache.parent.mkdir(parents=True, exist_ok=True)
    ccache.write_text('{"provider":"copilot","monthly":{"remaining":1}}', encoding="utf-8")
    assert common.read_copilot(env | {"LLM_USAGE_COPILOT_CACHE_TTL": "999"})["monthly"]["remaining"] == 1


def test_copilot_refresh_module(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "copilot-usage.json"
    lock = tmp_path / "copilot-refresh.lock"
    lock.mkdir()
    monkeypatch.setenv("LLM_USAGE_COPILOT_CAPTURE_TEXT", "Monthly: 10% used AI Credits: 3")
    assert copilot_refresh.main([str(cache)]) == 0
    data = json.loads(cache.read_text())
    assert data["monthly"]["remaining"] == 90
    assert not lock.exists()
    assert copilot_refresh.main([]) == 2


def test_validation_and_selection_edge_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        common.require_cmd("definitely-not-installed-llm-tools-test")
    for args in [
        ("", "", "", "1", ""),
        (str(tmp_path), "bad", "1", "1", ""),
        (str(tmp_path), "1", "bad", "1", ""),
        (str(tmp_path), "1", "0", "1", ""),
        (str(tmp_path), "1", "1", "bad", ""),
        (str(tmp_path), "1", "1", "1", "x,no"),
    ]:
        with pytest.raises(SystemExit):
            common.validate_gate_args(*args)
    with pytest.raises(SystemExit):
        common.validate_prompt_args("x", "y")
    with pytest.raises(SystemExit):
        common.validate_prompt_args("", "")
    with pytest.raises(SystemExit):
        common.validate_prompt_args("", str(tmp_path / "missing"))
    with pytest.raises(SystemExit):
        common.validate_tool_window("codex", "monthly")
    with pytest.raises(SystemExit):
        common.validate_tool_window("codex", "bad")

    cfg = ralph_robin.RalphConfig(tools_spec="claude,codex", tools=["claude", "codex"], state_file=tmp_path / "state.json")
    cfg.state_file.write_text("{bad", encoding="utf-8")
    assert ralph_robin.current_index_from_state(cfg) == 0
    cfg.state_file.write_text('{"tools_spec":"other","current_index":9}', encoding="utf-8")
    assert ralph_robin.current_index_from_state(cfg) == 0
    cfg.dry_run = True
    ralph_robin.save_state(cfg, 1, "codex")
    assert json.loads(cfg.state_file.read_text() or "{}").get("current_index") == 9

    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setattr(common, "usage_snapshot_for_tool", lambda tool: {"available": False, "reason": "missing-cli"})
    sel = ralph_robin.select_tool(cfg, logs, 0, {"claude", "codex"})
    assert sel["rotation_reason"] == "all-skipped"
    sel2 = ralph_robin.select_tool(cfg, logs, 0, set())
    assert sel2["rotation_reason"] == "advanced-to-undetermined"
    assert sel2["all_rate_limited"] is False

    snapshots = {
        "claude": {"available": True, "five_hour": {"remaining": 0, "resets_at": 2000}, "week": {"remaining": 50}},
        "codex": {"available": True},
    }
    monkeypatch.setattr(common, "usage_snapshot_for_tool", lambda tool: snapshots[tool])
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    sel3 = ralph_robin.select_tool(cfg, logs, 0, set())
    assert sel3["tool"] == "codex"
    assert sel3["rotation_reason"] == "advanced-to-undetermined"
    assert sel3["all_rate_limited"] is False


def test_scheduler_more_system_edges(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logs = common.setup_run_logs(tmp_path / "logs", "s")
    cfg = scheduler.SchedulerConfig(tool="claude", prompt_text="p", cwd=str(tmp_path), log_dir=tmp_path / "logs", run_dir=logs.run_dir)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    assert scheduler.schedule_resume_and_suspend(cfg, logs, 2000, "rate") is False
    monkeypatch.setattr(common, "have_cmd", lambda name: name == "systemd-run")
    assert scheduler.schedule_resume_and_suspend(cfg, logs, 2000, "rate") is False

    class P:
        def __init__(self, code: int = 0, out: str = "") -> None:
            self.returncode = code
            self.stdout = out

    monkeypatch.setattr(common, "have_cmd", lambda name: name in {"systemd-run", "systemctl"})
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **k: P(1, "fail"))
    assert scheduler.schedule_resume_and_suspend(cfg, logs, 2000, "rate") is False

    def inactive(args, **kwargs):
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return P(1, "")
        return P(0, "ok")

    monkeypatch.setattr(scheduler.subprocess, "run", inactive)
    assert scheduler.schedule_resume_and_suspend(cfg, logs, 2000, "rate") is False

    cfg.pre_suspend_confirmation_seconds = 0
    scheduler.print_pre_suspend_confirmation(cfg, logs, 2000, "unit", "why")
    assert "suspend-until-ready armed" in capsys.readouterr().out

    cfg2 = scheduler.SchedulerConfig(tool="codex", prompt_text="p", cwd=str(tmp_path), exec_mode="tmux", tmux_target="session")
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    out = tmp_path / "out"
    status = tmp_path / "status"
    assert scheduler.run_tmux(cfg2, logs, ["true"], out, status) == 127
    assert "tmux not installed" in out.read_text()
