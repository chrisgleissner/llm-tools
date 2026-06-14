from __future__ import annotations

import json
import io
import os
import subprocess
import sys
from urllib.error import HTTPError
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


def test_read_claude_api_refreshes_oauth_token(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    cred = Path(env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "expired-token",
                    "refreshToken": "refresh-token",
                    "expiresAt": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    requests: list[tuple[str, bytes | None, str | None]] = []

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self._text = text

        def read(self) -> bytes:
            return self._text.encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        url = req.full_url
        data = req.data
        auth = req.headers.get("Authorization")
        requests.append((url, data, auth))
        if url == common.CLAUDE_OAUTH_USAGE_URL and auth == "Bearer expired-token":
            raise HTTPError(url, 401, "unauthorized", hdrs=None, fp=None)
        if url == common.CLAUDE_OAUTH_TOKEN_URL:
            body = (data or b"").decode("utf-8")
            assert "grant_type=refresh_token" in body
            assert "refresh_token=refresh-token" in body
            assert f"client_id={common.CLAUDE_OAUTH_CLIENT_ID.replace(':', '%3A').replace('/', '%2F')}" in body
            return FakeResponse('{"access_token":"fresh-token","refresh_token":"fresh-refresh","expires_in":3600}')
        if url == common.CLAUDE_OAUTH_USAGE_URL and auth == "Bearer fresh-token":
            return FakeResponse(
                '{"rate_limits":{"five_hour":{"used_percentage":12,"resets_at":"2026-06-14T18:00:00Z"},'
                '"seven_day":{"used_percentage":34,"resets_at":"2026-06-20T18:00:00Z"}}}'
            )
        raise AssertionError(f"unexpected request: {url} auth={auth!r}")

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    data = common.read_claude_api(env)
    assert data is not None
    assert data["five_hour"]["used"] == 12
    saved = json.loads(cred.read_text(encoding="utf-8"))
    assert saved["claudeAiOauth"]["accessToken"] == "fresh-token"
    assert saved["claudeAiOauth"]["refreshToken"] == "fresh-refresh"
    assert [item[0] for item in requests] == [
        common.CLAUDE_OAUTH_USAGE_URL,
        common.CLAUDE_OAUTH_TOKEN_URL,
        common.CLAUDE_OAUTH_USAGE_URL,
    ]


def test_usage_dashboard_ready_guidance_and_reset(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    cfg = usage.Config()
    cfg.color_enabled = False

    assert usage.render_ready(10, cfg) == "yes"
    assert usage.render_ready(0, cfg) == "no"
    assert usage.classify_budget_guidance("weekly", 60, 1000 + int(3.5 * 86400)).text == "↑ headroom"
    assert usage.classify_budget_guidance("weekly", 50, 1000 + int(3.5 * 86400)).text == "= on pace"
    assert usage.classify_budget_guidance("weekly", 40, 1000 + int(3.5 * 86400)).text == "↓ conserve"

    assert usage.format_reset(1000 + 36 * 60, cfg) == "36m"
    assert usage.format_reset(1000 + 4 * 3600 + 34 * 60, cfg) == "4h 34m"
    assert usage.format_reset(1000 + 5 * 86400 + 2 * 3600, cfg) == "5d 2h"

    rows = [
        usage.UsageRow("Codex", "5h", 70, "70%", 1000 + 9000, "fixture"),
        usage.UsageRow("Codex", "weekly", 40, "40%", 1000 + int(3.5 * 86400), "fixture"),
        usage.UsageRow("Claude", "5h", 0, "0%", 1000 + 1800, "fixture"),
        usage.UsageRow("Claude", "weekly", 91, "91%", 1000 + int(5 * 86400), "fixture"),
    ]
    usage.print_table_header(cfg)
    usage.print_usage_rows(cfg, rows)
    out = capsys.readouterr().out
    assert "Ready" in out
    assert "Guidance" in out
    assert "yes" in out
    assert "no" in out
    assert "↑ headroom" in out
    assert "↓ conserve" in out
    assert "× empty" in out
    assert "open" not in out
    assert "closed" not in out
    assert "Pace / Gate" not in out
    assert "╞" not in out
    assert "◆" not in out
    assert "Use" not in out.splitlines()[0]


@pytest.mark.parametrize("window", ["weekly", "monthly"])
def test_usage_budget_guidance_compares_remaining_to_time_left(monkeypatch: pytest.MonkeyPatch, window: str) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    duration = usage.window_seconds(window)
    assert duration is not None
    reset = 1000 + int(duration / 2)

    assert usage.classify_budget_guidance(window, 56, reset).text == "↑ headroom"
    assert usage.classify_budget_guidance(window, 54, reset).text == "= on pace"
    assert usage.classify_budget_guidance(window, 44, reset).text == "↓ conserve"
    assert usage.classify_budget_guidance(window, 50, None).text == "· no rate data"


def test_usage_session_guidance_forecasts_runout(env: dict[str, str]) -> None:
    env = env | {
        "LLM_USAGE_NOW_EPOCH": "1600",
        "LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS": "9999",
        "XDG_CACHE_HOME": str(Path(env["HOME"]) / ".cache"),
    }
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    log = cache / "llm-usage.log"
    log.write_text("", encoding="utf-8")

    assert usage.classify_session_guidance("Codex", "5h", 0, 3600, env).text == "× empty"
    assert usage.classify_session_guidance("Codex", "5h", 20, 3600, env).text == "· no rate data"

    log.write_text(
        '{"ts":1000,"provider":"Codex","window":"5h","remaining":20}\n'
        '{"ts":1600,"provider":"Codex","window":"5h","remaining":10}\n',
        encoding="utf-8",
    )
    assert usage.classify_session_guidance("Codex", "5h", 10, 3600, env).text == "! empty in 10m"

    log.write_text(
        '{"ts":1000,"provider":"Codex","window":"5h","remaining":90}\n'
        '{"ts":1600,"provider":"Codex","window":"5h","remaining":80}\n',
        encoding="utf-8",
    )
    assert usage.classify_session_guidance("Codex", "5h", 80, 2600, env).text == "✓ lasts until reset"


def test_usage_table_snapshot_has_guidance_and_no_old_dial(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = usage.Config()
    cfg.color_enabled = False
    rows = [
        usage.UsageRow("Codex", "5h", 84, "84%", 1000 + 4 * 3600, "fixture"),
        usage.UsageRow("Codex", "weekly", 33, "33%", 1000 + 5 * 86400 + 3600, "fixture"),
        usage.UsageRow("Claude", "5h", 0, "0%", 1000 + 120, "fixture"),
        usage.UsageRow("Claude", "weekly", 91, "91%", 1000 + 4 * 86400 + 23 * 3600, "fixture"),
        usage.UsageRow("Copilot", "monthly", 36, "36%", 1000 + 17 * 86400 + 10 * 3600, "fixture"),
    ]

    usage.print_dashboard_header(cfg)
    usage.print_table_header(cfg)
    usage.print_usage_rows(cfg, rows)
    out = capsys.readouterr().out

    assert out.startswith("LLM Usage · ")
    assert "\n\nBars: █ available · ░ spent" in out
    assert "Bars: █ available · ░ spent" in out
    assert "Guidance:" in out
    assert "Provider   Ready   Scope     Remaining" in out
    assert "Codex      yes     5h         84% ████████░░" in out
    assert "                   weekly     33% ███░░░░░░░   ↓ conserve" in out
    assert "Claude     no      5h          0% ░░░░░░░░░░   × empty" in out
    assert "╞" not in out
    assert "◆" not in out
    assert "──╞═══╡──◆" not in out


def test_usage_unicode_column_alignment_is_stable(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = usage.Config()
    cfg.color_enabled = False
    rows = [
        usage.UsageRow("Codex", "5h", 84, "84%", 1000 + 4 * 3600, "fixture"),
        usage.UsageRow("Codex", "weekly", 33, "33%", 1000 + 5 * 86400, "fixture"),
    ]

    usage.print_table_header(cfg)
    usage.print_usage_rows(cfg, rows)
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    width = usage.table_fixed_width(cfg)
    assert all(usage.visible_len(line) == width for line in lines)
    assert width <= 120


def test_scheduler_argument_branches(env: dict[str, str], tmp_path: Path) -> None:
    cases = [
        ["./llm-scheduler", "--provider"],
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
    bad_at = run_cmd(["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--at", "not-a-date", "--log-dir", str(tmp_path / "logs")], env)
    assert bad_at.returncode == 2
    assert not (tmp_path / "logs").exists()
    bad_env = run_cmd(["./llm-scheduler", "--provider", "codex", "--prompt", "x"], env | {"LLM_SCHEDULER_IDLE_TIMEOUT": "bad"})
    assert bad_env.returncode == 2
    wake = run_cmd(["./llm-scheduler", "--wake-test"], env)
    assert wake.returncode == 0
    assert json.loads(wake.stdout)["note"].startswith("wake is best effort")
    guarded = run_cmd(
        ["./llm-scheduler", "--provider", "claude", "--prompt", "x", "--suspend-until-ready"],
        env | {"LLM_TOOLS_RALPH_ROBIN_ACTIVE": "1"},
    )
    assert guarded.returncode == common.AUTONOMY_ABORT_STATUS
    assert "disabled inside an active ralph-robin" in guarded.stderr
    allowed = run_cmd(
        ["./llm-scheduler", "--provider", "claude", "--prompt", "x", "--suspend-until-ready", "--dry-run", "--command-template", "true"],
        env | {"LLM_TOOLS_RALPH_ROBIN_ACTIVE": "1", "LLM_TOOLS_RALPH_ROBIN_ALLOW_SUSPEND": "1", "LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}'},
    )
    assert allowed.returncode == 0


def test_scheduler_unavailable_suspend_and_no_stream(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    unavailable = '{"available":false,"reason":"missing-cli"}'
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
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
        ["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--command-template", "provider-mock", "--log-dir", str(tmp_path / "quiet")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}', "LLM_SCHEDULER_NO_STREAM": "1"},
    )
    assert quiet.returncode == 0
    assert "chat ok" not in quiet.stdout


def test_scheduler_suspend_dry_run_and_failures(env: dict[str, str], fake_bin: Path, tmp_path: Path) -> None:
    write_exe(fake_bin / "systemd-run", "#!/usr/bin/env python3\nprint('Running timer as unit: mocked.timer')\n")
    write_exe(fake_bin / "systemctl", "#!/usr/bin/env python3\nimport sys\nprint('running' if sys.argv[1:3] == ['--user','is-system-running'] else '')\n")
    exhausted = '{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}}'
    dry = run_cmd(
        ["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--command-template", "true", "--suspend-until-ready", "--dry-run", "--log-dir", str(tmp_path / "dry")],
        env | {"LLM_USAGE_NOW_EPOCH": "1780430000", "LLM_SCHEDULER_USAGE_JSON": exhausted},
    )
    assert dry.returncode == 0
    assert "would schedule" in dry.stdout
    near = run_cmd(
        ["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--command-template", "true", "--suspend-until-ready", "--dry-run", "--log-dir", str(tmp_path / "near")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":0,"resets_at":9999999999},"week":{"remaining":50}}', "LLM_USAGE_NOW_EPOCH": "9999999970"},
    )
    assert "suspend scheduling failed" in near.stderr


def test_scheduler_tmux_missing_and_template_error(env: dict[str, str], tmp_path: Path) -> None:
    result = run_cmd(
        ["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--command-template", "unterminated '", "--log-dir", str(tmp_path / "bad-template")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}'},
    )
    assert result.returncode == 1
    tmux = run_cmd(
        ["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--tmux", ":", "--no-retry", "--log-dir", str(tmp_path / "tmux")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}'},
    )
    assert tmux.returncode == 1


def test_ralph_and_scheduler_highlight_helpers(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert scheduler.provider_env(scheduler.SchedulerConfig()) is None
    env = scheduler.provider_env(scheduler.SchedulerConfig(provider="codex", ralph_robin_active=True, ralph_robin_providers="claude,codex"))
    assert env is not None
    assert env["LLM_TOOLS_RALPH_ROBIN_ACTIVE"] == "1"
    assert env["LLM_TOOLS_RALPH_ROBIN_SELECTED_PROVIDER"] == "codex"
    assert env["LLM_TOOLS_RALPH_ROBIN_PROVIDERS"] == "claude,codex"

    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("LLM_USAGE_NO_COLOR", raising=False)
    monkeypatch.delenv("LLM_TOOLS_COLOR_DIFF_ADD", raising=False)
    monkeypatch.delenv("LLM_TOOLS_SYMBOL_COMMAND", raising=False)
    monkeypatch.delenv("LLM_TOOLS_NO_SYMBOLS", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert common.ANSI_COLOR_ROLES["info"] == "39"
    assert common.ANSI_COLOR_ROLES["heading"] == "1;39"
    assert common.ANSI_COLOR_ROLES["ok"].endswith(";77")
    assert common.ANSI_COLOR_ROLES["error"].endswith(";81")
    assert not {
        "1;38;5;203",
        "38;5;203",
        "1;38;5;219",
        "1;38;5;222",
        "1;38;5;183",
    } & set(common.ANSI_COLOR_ROLES.values())
    assert scheduler.stream_color_enabled(Tty()) is True
    assert f"\x1b[{common.color_code('diff_add')}m+added\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"+added\n", stream_name="stdout", enabled=True)
    assert f"\x1b[{common.color_code('diff_remove')}m-removed\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"-removed\n", stream_name="stdout", enabled=True)
    assert f"\x1b[{common.color_code('diff_hunk')}m@@ hunk\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"@@ hunk\n", stream_name="stdout", enabled=True)
    assert f"\x1b[{common.color_code('command')}mgit status\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"git status\n", stream_name="stdout", enabled=True)
    assert f"\x1b[{common.color_code('error')}merror failed\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"error failed\n", stream_name="stdout", enabled=True)
    assert f"\x1b[{common.color_code('warn')}mwarning: check this\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"warning: check this\n", stream_name="stdout", enabled=True)
    assert scheduler.highlight_provider_text(b"progress\n", stream_name="stderr", enabled=True) == b"progress\n"
    assert f"\x1b[{common.color_code('error')}merror failed\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"error failed\n", stream_name="stderr", enabled=True)
    assert scheduler.highlight_provider_text(b"\x1b[31mred\x1b[0m\n", stream_name="stdout", enabled=True) == b"\x1b[31mred\x1b[0m\n"
    monkeypatch.setenv("LLM_TOOLS_COLOR_DIFF_ADD", "1;34")
    assert b"\x1b[1;34m+added\x1b[0m\n" == scheduler.highlight_provider_text(b"+added\n", stream_name="stdout", enabled=True)
    monkeypatch.setenv("LLM_TOOLS_SYMBOL_COMMAND", "$")
    assert f"\x1b[{common.color_code('command')}mgit status\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"git status\n", stream_name="stdout", enabled=True)
    monkeypatch.setenv("LLM_TOOLS_NO_SYMBOLS", "1")
    assert f"\x1b[{common.color_code('command')}mgit status\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"git status\n", stream_name="stdout", enabled=True)
    assert scheduler.highlight_provider_text(b"git status\n", stream_name="stdout", enabled=False) == b"git status\n"

    decision = {"provider": "claude", "usable": False, "reason": "rate-limited", "wait_until": 2000, "windows": [{"name": "5h", "remaining": 0}]}
    assert "rate-limited" in ralph_robin.decision_summary(decision)
    ralph_robin.print_usage_summary({"decisions": [decision, {"provider": "codex", "usable": True, "reason": "usable", "windows": [{"name": "5h", "remaining": 61.5}]}]})
    assert "claude" in capsys.readouterr().err
    monkeypatch.setattr(ralph_robin, "color_enabled", lambda: True)
    ralph_robin.status_line("plain body", level="error")
    body = capsys.readouterr().err.split(": ", 1)[1]
    assert body == "plain body\n"


def test_ralph_validation_dry_run_rotation_and_autonomy(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    assert run_cmd(["./ralph-robin"], env).returncode == 2
    assert run_cmd(["./ralph-robin", "--providers", "bad", "--prompt", "x"], env).returncode == 2
    usage_json = '{"claude":{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}}'
    dry = run_cmd(
        ["./ralph-robin", "--prompt", "x", "--command-template", "provider-mock {provider}", "--dry-run", "--state-file", str(tmp_path / "s.json"), "--log-dir", str(tmp_path / "logs")],
        env | {"LLM_USAGE_NOW_EPOCH": "1780430000", "LLM_SCHEDULER_USAGE_JSON": usage_json},
    )
    assert dry.returncode == 0
    assert "dry-run" in dry.stderr
    run = run_cmd(
        ["./ralph-robin", "--prompt", "x", "--command-template", "provider-mock {provider}", "--state-file", str(tmp_path / "s2.json"), "--log-dir", str(tmp_path / "logs2"), "--no-retry", "--max-iterations", "1"],
        env | {"LLM_USAGE_NOW_EPOCH": "1780430000", "LLM_SCHEDULER_USAGE_JSON": usage_json},
    )
    assert run.returncode == 0
    assert json.loads((tmp_path / "s2.json").read_text())["current_provider"] == "codex"
    blocked = run_cmd(
        ["./ralph-robin", "--prompt", "x", "--command-template", "provider-mock {provider}", "--state-file", str(tmp_path / "s3.json"), "--log-dir", str(tmp_path / "logs3"), "--no-retry", "--max-duration", "3"],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"claude":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}}', "PROVIDER_MODE": "blocking"},
    )
    assert blocked.returncode == common.AUTONOMY_ABORT_STATUS


def test_ralph_injects_selected_provider_context(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    capture = tmp_path / "capture.txt"
    usage_json = '{"claude":{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":60},"week":{"remaining":68}}}'
    prompt = "When continuation is required, run exactly: llm-scheduler --provider claude --prompt-file task.md --suspend-until-ready"
    result = run_cmd(
        [
            "./ralph-robin",
            "--prompt",
            prompt,
            "--command-template",
            "provider-mock {provider} {prompt}",
            "--state-file",
            str(tmp_path / "state.json"),
            "--log-dir",
            str(tmp_path / "logs"),
            "--no-retry",
            "--max-iterations",
            "1",
        ],
        env
        | {
            "LLM_USAGE_NOW_EPOCH": "1780430000",
            "LLM_SCHEDULER_USAGE_JSON": usage_json,
            "PROVIDER_CAPTURE": str(capture),
        },
    )
    assert result.returncode == 0
    captured = capture.read_text(encoding="utf-8")
    assert "codex RALPH ROBIN RUNTIME CONTEXT" in captured
    assert "Current selected provider: codex" in captured
    assert "claude: rate-limited" in captured
    assert "codex: usable" in captured
    assert "Do not run provider-specific llm-scheduler --suspend-until-ready commands" in captured
    assert prompt in captured


def test_ensure_copilot_footer_settings(env: dict[str, str], tmp_path: Path) -> None:
    home = tmp_path / "copilot-home"
    cenv = env | {"COPILOT_HOME": str(home)}
    settings = home / "settings.json"
    # Fresh install: file is created with the footer items we scrape enabled.
    common.ensure_copilot_footer_settings(cenv)
    assert json.loads(settings.read_text())["footer"] == {"showQuota": True, "showAiUsed": True}
    # Idempotent: an already-enabled file is left byte-for-byte untouched.
    before = settings.stat().st_mtime_ns
    common.ensure_copilot_footer_settings(cenv)
    assert settings.stat().st_mtime_ns == before
    # Existing user settings are preserved while the required flags are flipped on.
    settings.write_text(json.dumps({"footer": {"showQuota": False, "showSandbox": True}, "beep": True}))
    common.ensure_copilot_footer_settings(cenv)
    data = json.loads(settings.read_text())
    assert data["footer"] == {"showQuota": True, "showSandbox": True, "showAiUsed": True}
    assert data["beep"] is True
    # Opt-out env disables the write entirely.
    home2 = tmp_path / "copilot-home-2"
    common.ensure_copilot_footer_settings(cenv | {"COPILOT_HOME": str(home2), "LLM_USAGE_COPILOT_NO_SETTINGS_WRITE": "1"})
    assert not (home2 / "settings.json").exists()
    # Unparseable settings are left untouched rather than clobbered.
    home3 = tmp_path / "copilot-home-3"
    home3.mkdir()
    (home3 / "settings.json").write_text("{not json")
    common.ensure_copilot_footer_settings(cenv | {"COPILOT_HOME": str(home3)})
    assert (home3 / "settings.json").read_text() == "{not json"


def test_freshen_stale_windows() -> None:
    now = 1000
    # Reset already passed: window rolled over -> full quota, reset cleared.
    assert common.freshen_window({"used": 90.0, "resets_at": 500, "window_minutes": 300}, now) == {
        "used": 0.0,
        "resets_at": None,
        "window_minutes": 300,
    }
    # Future reset is left untouched (same object returned).
    future = {"used": 90.0, "resets_at": 2000, "window_minutes": 300}
    assert common.freshen_window(future, now) is future
    # No reset and non-dict inputs pass through unchanged.
    no_reset = {"used": 90.0, "resets_at": None}
    assert common.freshen_window(no_reset, now) is no_reset
    assert common.freshen_window(None, now) is None
    # Provider-level walk freshens top-level and per-row windows using NOW_EPOCH.
    obj = {
        "five_hour": {"used": 50.0, "resets_at": 500},
        "week": {"used": 10.0, "resets_at": 5000},
        "rows": [{"five_hour": {"used": 70.0, "resets_at": 400}, "week": {"used": 5.0, "resets_at": 6000}}],
    }
    out = common.freshen_provider_windows(obj, {"LLM_USAGE_NOW_EPOCH": "1000"})
    assert out["five_hour"] == {"used": 0.0, "resets_at": None}
    assert out["week"]["used"] == 10.0
    assert out["rows"][0]["five_hour"]["used"] == 0.0
    assert out["rows"][0]["week"]["used"] == 5.0


def test_read_codex_freshens_elapsed_window(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", env["HOME"])
    home = Path(env["HOME"])
    (home / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
    # 5h reset 900 is in the past relative to NOW_EPOCH 1000; weekly 9999 is future.
    (home / ".codex" / "sessions" / "r.jsonl").write_text(
        '{"rate_limits":{"primary":{"used_percent":98,"resets_at":900},"secondary":{"used_percent":65,"resets_at":9999}}}\n',
        encoding="utf-8",
    )
    codex = common.read_codex(env | {"LLM_USAGE_NOW_EPOCH": "1000"})
    assert codex is not None
    assert codex["five_hour"] == {"used": 0.0, "resets_at": None, "window_minutes": 300}
    assert codex["week"]["used"] == 65  # unexpired weekly is preserved


def test_common_extra_branches(env: dict[str, str], fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert common.fmt_duration("bad") == "-"
    assert common.time_until("bad") == "-"
    assert common.parse_copilot_ai_credits("AI Credits: 17") == 17
    assert common.parse_copilot_monthly_used("Monthly: 42% used") == 42
    assert common.parse_copilot_monthly_used("Plan: 62% used · Session: 0 AIC used") == 62
    assert common.json_for_copilot(None)["reason"] == "unavailable"
    assert common.json_for_copilot({"provider": "copilot", "monthly": {"remaining": 1}, "ai_credits": {"used": 2}}, False).get("ai_credits") is None
    assert common.output_is_retryable(0, "chapter 429") is False
    assert common.output_is_retryable(0, "claude: rate-limited, codex: usable") is False
    assert common.output_is_retryable(0, "rate limit reached") is True
    assert common.output_is_retryable(0, "HTTP 429 Too Many Requests") is True
    assert common.output_is_retryable(42, "") is True
    # A model describing the SYSTEM UNDER TEST is not a provider rate limit: these
    # bare words must NOT trip a retry (the bug that re-ran/killed the loop).
    assert common.output_is_retryable(0, "the c64u device was overloaded and dropped out") is False
    assert common.output_is_retryable(0, "the REST endpoint was temporarily unavailable; try again later") is False
    assert common.output_is_retryable(0, "no rate-limit issues were observed this loop") is False
    # Genuine provider/transport signatures still retry.
    assert common.output_is_retryable(0, 'API Error: {"type":"overloaded_error"}') is True
    assert common.output_is_retryable(0, "HTTP 503 Service Unavailable") is True
    # trust_clean_exit (ralph-robin owns rate-limit handling) trusts exit 0 even
    # when the transcript pastes a device log that looks like a rate limit.
    assert common.output_is_retryable(0, "device log: HTTP 429 Too Many Requests", trust_clean_exit=True) is False
    assert common.output_is_retryable(1, "boom", trust_clean_exit=True) is True
    assert common.argv_to_command_line(["a b", "$x"]) == "'a b' '$x'"
    assert common.template_argv("cmd {provider} {prompt_file} {cwd}", provider="codex", prompt="p", prompt_file=tmp_path / "p.txt", cwd="/tmp") == ["cmd", "codex", str(tmp_path / "p.txt"), "/tmp"]
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
        "--provider", "claude", "--prompt-file", str(prompt), "--at", "@100",
        "--window", "5h", "--min-remaining", "2", "--poll-interval", "3",
        "--max-unavailable-wait", "4", "--retry-delays", "5", "--cwd", str(tmp_path),
        "--fresh", "--headless", "--tmux", "s:w", "--auto-confirm", "--no-auto-confirm",
        "--headless-idle-timeout", "7", "--headless-question-timeout", "8",
        "--log-dir", str(tmp_path / "logs"), "--run-dir", str(tmp_path / "run"),
        "--wake", "--suspend-until-ready",
    ])
    assert scfg.provider == "claude"
    assert scfg.tmux_target == "s:w"
    assert scfg.suspend_until_ready is True
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="codex", cwd="/c", attached=True), "p") == ["codex", "-C", "/c", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="claude", attached=True), "p") == ["claude", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="claude"), "p") == ["claude", "--print", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="claude", claude_stream_json=True), "p") == ["claude", "--print", "--output-format", "stream-json", "--verbose", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="copilot", cwd="/c", attached=True), "p") == ["copilot", "-C", "/c", "-i", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="kilo", cwd="/c", attached=True), "p") == ["kilo", "run", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="kilo", cwd="/c"), "p") == ["kilo", "run", "--auto", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="opencode", cwd="/c", attached=True), "p") == ["opencode"]
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(provider="codex")).startswith("Codex")
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(provider="claude")).startswith("Claude")
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(provider="copilot")).startswith("GitHub")

    rcfg = ralph_robin.parse_args([
        "--providers", " claude, codex ,,", "--prompt-file", str(prompt), "--window", "weekly",
        "--min-remaining", "2", "--poll-interval", "3", "--max-unavailable-wait", "4",
        "--retry-delays", "5", "--cwd", str(tmp_path), "--fresh", "--headless",
        "--tmux", "s:w", "--command-template", "true", "--auto-confirm", "--no-auto-confirm",
        "--headless-idle-timeout", "7", "--headless-question-timeout", "8",
        "--log-dir", str(tmp_path / "rlogs"), "--state-file", str(tmp_path / "state.json"),
        "--wake", "--suspend-until-ready",
    ])
    ralph_robin.validate_args(rcfg)
    assert rcfg.providers == ["claude", "codex"]
    assert ralph_robin.safe_args_json(rcfg)["providers"] == ["claude", "codex"]
    with pytest.raises(SystemExit):
        ralph_robin.parse_providers(" , ")


def test_scheduler_system_and_tmux_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logs = common.setup_run_logs(tmp_path / "logs", "t", "codex")
    cfg = scheduler.SchedulerConfig(provider="codex", prompt_text="p", cwd=str(tmp_path), log_dir=tmp_path / "logs", run_dir=logs.run_dir)
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


def test_estimate_remaining_time_survives_resets_and_gaps(env: dict[str, str]) -> None:
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    base = 1_000_000
    rows = [
        (base, 100),
        (base + 3600, 90),
        (base + 7200, 100),
        (base + 10800, 90),
        (base + 18000, 80),
    ]
    (cache / "llm-usage.log").write_text(
        "".join(f'{{"ts":{ts},"provider":"p","window":"w","remaining":{rem}}}\n' for ts, rem in rows),
        encoding="utf-8",
    )
    now_env = env | {"LLM_USAGE_NOW_EPOCH": str(base + 18000)}
    # The +10 jump at +7200 (90->100) is a reset: it anchors to the current
    # window, dropping the earlier burn. The trailing 2h gap exceeds max_gap, so
    # only the post-reset 10% over 3600s counts -> 50% lasts 5h.
    assert common.estimate_remaining_time_from_log("p", "w", 50, now_env) == "5h"
    stale_env = env | {"LLM_USAGE_NOW_EPOCH": str(base + 18601)}
    assert common.estimate_remaining_time_from_log("p", "w", 50, stale_env) == "-"
    assert common.estimate_remaining_time_from_log("p", "w", 50, stale_env | {"LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS": "0"}) == "5h"
    assert common.estimate_remaining_time_from_log("p", "w", 50, now_env | {"LLM_USAGE_REMAINING_TIME_LOOKBACK_SECONDS": "60"}) == "-"
    # Disabling the gap filter also counts the post-reset trailing 2h decrease:
    # 20% over 10800s within the anchored window -> 50% lasts 7h 30m.
    assert common.estimate_remaining_time_from_log("p", "w", 50, now_env | {"LLM_USAGE_REMAINING_TIME_MAX_GAP_SECONDS": "0"}) == "7h 30m"
    assert common.estimate_remaining_time_from_log("p", "w", 50, now_env | {"LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS": "bad"}) == "5h"


def test_estimate_remaining_time_requires_minimum_span_for_real_windows(env: dict[str, str]) -> None:
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    base = 1_000_000
    # A lone coarse step (a stale 80% reading jumping to the steady 49%) followed
    # by a few flat seconds. Read literally this looks like 31% burned in 33s, which
    # the old estimator extrapolated to "weekly gone in ~5m". With only ~3.5min of
    # history there is not enough evidence to estimate a weekly/5h ETA.
    rows = [(base, 80)] + [(base + 33 + i * 30, 49) for i in range(7)]
    log = cache / "llm-usage.log"
    now = rows[-1][0]
    for window in ("weekly", "5h", "monthly"):
        log.write_text(
            "".join(f'{{"ts":{ts},"provider":"Codex","window":"{window}","remaining":{rem}}}\n' for ts, rem in rows),
            encoding="utf-8",
        )
        now_env = env | {"LLM_USAGE_NOW_EPOCH": str(now)}
        assert common.estimate_remaining_time_from_log("Codex", window, 49, now_env) == "-"

    # Once the same flat reading has been observed across enough wall-clock history,
    # the lone step is diluted and a (large, sane) estimate appears instead of "-".
    long_rows = [(base, 80)] + [(base + 33 + i * 3600, 49) for i in range(8)]
    long_now = long_rows[-1][0]
    log.write_text(
        "".join(f'{{"ts":{ts},"provider":"Codex","window":"weekly","remaining":{rem}}}\n' for ts, rem in long_rows),
        encoding="utf-8",
    )
    est = common.estimate_remaining_time_from_log("Codex", "weekly", 49, env | {"LLM_USAGE_NOW_EPOCH": str(long_now)})
    assert est not in ("-", "1m")

    # The gate is tunable: dropping the fraction to 0 restores the raw estimate.
    short_env = env | {"LLM_USAGE_NOW_EPOCH": str(now), "LLM_USAGE_REMAINING_TIME_MIN_SPAN_FRACTION": "0"}
    log.write_text(
        "".join(f'{{"ts":{ts},"provider":"Codex","window":"weekly","remaining":{rem}}}\n' for ts, rem in rows),
        encoding="utf-8",
    )
    assert common.estimate_remaining_time_from_log("Codex", "weekly", 49, short_env) != "-"


def test_estimate_anchors_to_current_window(env: dict[str, str]) -> None:
    # The reported bug: burn from PRIOR 5h windows leaked into the current
    # window's velocity, pinning "empty in Xm" to a wildly short, stuck value.
    # Anchoring to the most recent reset must confine the estimate to the burn
    # actually observed in the current window.
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    base = 1_000_000
    rows = [(base, 100), (base + 600, 20)]  # prior window: heavy burn, then exhausted
    # Reset back to ~full, then a gentle ~1pp/min burn (99 -> 82 over 17 min).
    rows += [(base + 4000 + i * 60, 99 - i) for i in range(18)]
    now = rows[-1][0]
    (cache / "llm-usage.log").write_text(
        "".join(f'{{"ts":{ts},"provider":"Claude","window":"5h","remaining":{rem}}}\n' for ts, rem in rows),
        encoding="utf-8",
    )
    now_env = env | {"LLM_USAGE_NOW_EPOCH": str(now)}
    secs = common.estimate_remaining_seconds_from_log("Claude", "5h", 82, now_env)
    # Current window only: 17pp over 1020s -> 82pp lasts ~1h22m. Counting the
    # leaked prior burn would collapse this to ~22m.
    assert secs is not None and 3600 < secs < 18000


def test_estimate_ignores_impossible_drain_spikes(env: dict[str, str]) -> None:
    # A single reading that "drains" most of the window in seconds is physically
    # impossible (a transient bad sample). It must not dominate the velocity even
    # when it is inside the current window and never recovers above threshold.
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    base = 1_000_000
    rows = [(base, 99), (base + 30, 20)]  # 79pp "drained" in 30s -> impossible
    rows += [(base + 30 + i * 300, 20 - i) for i in range(1, 8)]  # gentle 20 -> 13
    now = rows[-1][0]
    (cache / "llm-usage.log").write_text(
        "".join(f'{{"ts":{ts},"provider":"Claude","window":"5h","remaining":{rem}}}\n' for ts, rem in rows),
        encoding="utf-8",
    )
    now_env = env | {"LLM_USAGE_NOW_EPOCH": str(now)}
    secs = common.estimate_remaining_seconds_from_log("Claude", "5h", 13, now_env)
    # Only the gentle 7pp/2100s burn should count (~1h+); the spike would force ~5m.
    assert secs is not None and secs > 3600
    # Lowering the guard below the spike's drain time lets it back in -> tiny ETA.
    leaky = now_env | {"LLM_USAGE_REMAINING_TIME_MIN_DRAIN_SECONDS": "1"}
    leaked = common.estimate_remaining_seconds_from_log("Claude", "5h", 13, leaky)
    assert leaked is not None and leaked < secs


def test_prune_usage_log(env: dict[str, str]) -> None:
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    log = cache / "llm-usage.log"
    common.prune_usage_log(env)
    lines = [f'{{"ts":{i},"provider":"p","window":"w","remaining":50}}' for i in range(100)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    common.prune_usage_log(env | {"LLM_USAGE_LOG_MAX_BYTES": "0"})
    assert len(log.read_text(encoding="utf-8").splitlines()) == 100
    common.prune_usage_log(env)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 100
    common.prune_usage_log(env | {"LLM_USAGE_LOG_MAX_BYTES": "10", "LLM_USAGE_LOG_TAIL_LINES": "5"})
    kept = log.read_text(encoding="utf-8").splitlines()
    assert len(kept) == 5
    assert kept[-1] == lines[-1]
    common.prune_usage_log(env | {"LLM_USAGE_LOG_MAX_BYTES": "bad"})
    assert len(log.read_text(encoding="utf-8").splitlines()) == 5


def test_common_filesystem_provider_paths(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", env["HOME"])
    # This path exercises the local-filesystem fallback readers, so keep Codex
    # from spawning the real app-server (it reads os.environ, not the fixture).
    monkeypatch.setenv("LLM_USAGE_DISABLE_CODEX_APP_SERVER", "1")
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


def test_copilot_refresh_wait_budget_cold_start_is_long(env: dict[str, str]) -> None:
    # Warm cache: short wait so we serve the stale value quickly.
    assert common.copilot_refresh_wait_budget(env, cache_present=True) == 1.0
    # Cold start: wait long enough for the first capture (timeout + margin).
    assert common.copilot_refresh_wait_budget(env, cache_present=False) == 12.0
    assert common.copilot_refresh_wait_budget(env | {"LLM_USAGE_COPILOT_TIMEOUT": "4"}, cache_present=False) == 6.0
    # Explicit override always wins, including the 0 used by other tests.
    assert common.copilot_refresh_wait_budget(env | {"LLM_USAGE_COPILOT_REFRESH_WAIT": "0"}, cache_present=False) == 0.0
    assert common.copilot_refresh_wait_budget(env | {"LLM_USAGE_COPILOT_REFRESH_WAIT": "bad"}, cache_present=True) == 1.0


def test_copilot_cold_start_returns_refreshed_data(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = env | {"XDG_CACHE_HOME": str(tmp_path / "cold-xdg"), "LLM_USAGE_COPILOT_CACHE_TTL": "999"}
    cache = common.usage_cache_dir(fresh) / "copilot-usage.json"
    lock = common.usage_cache_dir(fresh) / "copilot-refresh.lock"

    class PopenStub:
        def __init__(self, args: list[str], **kwargs: object) -> None:
            # Stand in for the background refresh: land real data and release the lock.
            cache.write_text('{"provider":"copilot","monthly":{"remaining":77}}', encoding="utf-8")
            try:
                lock.rmdir()
            except OSError:
                pass

    monkeypatch.setattr(common.subprocess, "Popen", PopenStub)
    # No cache exists yet, so without the cold-start wait this would be "refresh-pending".
    assert common.read_copilot(fresh)["monthly"]["remaining"] == 77


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
        common.validate_provider_window("codex", "monthly")
    with pytest.raises(SystemExit):
        common.validate_provider_window("codex", "bad")

    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    cfg.state_file.write_text("{bad", encoding="utf-8")
    assert ralph_robin.current_index_from_state(cfg) == 0
    cfg.state_file.write_text('{"providers_spec":"other","current_index":9}', encoding="utf-8")
    assert ralph_robin.current_index_from_state(cfg) == 0
    cfg.dry_run = True
    ralph_robin.save_state(cfg, 1, "codex")
    assert json.loads(cfg.state_file.read_text() or "{}").get("current_index") == 9

    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider: {"available": False, "reason": "missing-cli"})
    sel = ralph_robin.select_provider(cfg, logs, 0, {"claude", "codex"})
    assert sel["rotation_reason"] == "all-skipped"
    sel2 = ralph_robin.select_provider(cfg, logs, 0, set())
    assert sel2["rotation_reason"] == "advanced-to-undetermined"
    assert sel2["all_rate_limited"] is False

    snapshots = {
        "claude": {"available": True, "five_hour": {"remaining": 0, "resets_at": 2000}, "week": {"remaining": 50}},
        "codex": {"available": True},
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider: snapshots[provider])
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    sel3 = ralph_robin.select_provider(cfg, logs, 0, set())
    assert sel3["provider"] == "codex"
    assert sel3["rotation_reason"] == "advanced-to-undetermined"
    assert sel3["all_rate_limited"] is False


def test_ralph_even_burn_prefers_highest_remaining_daily_capacity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    # Even-burn compares remaining *daily* capacity = weekly remaining / days
    # until reset. For example, Codex could have less weekly headroom (50%) but resets in 2 days
    # (25%/day) so it outranks Claude's 80% spread over 5 days (16%/day).
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 80},
            "week": {"remaining": 80, "resets_at": 1000 + (5 * 86400)},
        },
        "codex": {
            "available": True,
            "five_hour": {"remaining": 50},
            "week": {"remaining": 50, "resets_at": 1000 + (2 * 86400)},
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider: snapshots[provider])

    selected = ralph_robin.select_provider(cfg, logs, 0, set())
    assert selected["provider"] == "codex"
    assert selected["rotation_reason"] == "even-burn"

    cfg.even_burn = False
    old_rotation = ralph_robin.select_provider(cfg, logs, 0, set())
    assert old_rotation["provider"] == "claude"
    assert old_rotation["rotation_reason"] == "current-usable"


def test_ralph_even_burn_prefers_higher_remaining_when_resets_align(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # When weekly resets are (near) simultaneous, daily capacity reduces to
    # remaining, so the provider with more weekly headroom wins regardless of
    # which one is current. This mirrors the real Claude(81%)/Codex(47%) case.
    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 96},
            "week": {"remaining": 81, "resets_at": 1000 + (6 * 86400)},
        },
        "codex": {
            "available": True,
            "five_hour": {"remaining": 85},
            "week": {"remaining": 47, "resets_at": 1000 + (6 * 86400)},
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider: snapshots[provider])

    # current_index points at codex; even-burn must still advance to claude.
    selected = ralph_robin.select_provider(cfg, logs, 1, set())
    assert selected["provider"] == "claude"
    assert selected["rotation_reason"] == "even-burn"


def test_ralph_even_burn_handles_unknown_weekly_reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Claude reports weekly remaining but no reset time. Even-burn must fall back
    # to a full weekly window and still rank Claude rather than silently bailing
    # to the current provider (codex).
    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 96},
            "week": {"remaining": 81},  # no resets_at -> reset_epoch is None
        },
        "codex": {
            "available": True,
            "five_hour": {"remaining": 85},
            "week": {"remaining": 47, "resets_at": 1000 + (6 * 86400)},
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider: snapshots[provider])

    # 81% / 7d ~= 11.6%/day beats 47% / 6d ~= 7.8%/day.
    selected = ralph_robin.select_provider(cfg, logs, 1, set())
    assert selected["provider"] == "claude"
    assert selected["rotation_reason"] == "even-burn"


def _usable_selection(provider: str = "claude") -> dict:
    return {
        "index": 0,
        "provider": provider,
        "rotation_reason": "even-burn",
        "all_rate_limited": False,
        "decision": {"provider": provider, "usable": True, "wait_until": None},
        "decisions": [{"provider": provider, "usable": True, "wait_until": None}],
    }


def _ralph_main_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--prompt", "x",
        "--providers", "claude,codex",
        "--log-dir", str(tmp_path / "logs"),
        "--state-file", str(tmp_path / "state.json"),
        *extra,
    ]


def test_ralph_loops_until_max_iterations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
    monkeypatch.setattr(ralph_robin, "select_provider", lambda cfg, logs, ci, sk: _usable_selection())
    calls: list[str] = []
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda scfg: (calls.append(scfg.provider), 0)[1])

    rc = ralph_robin.main(_ralph_main_argv(tmp_path, "--max-iterations", "3", "--max-duration", "0", "--min-iteration-seconds", "0"))
    assert rc == 0
    assert len(calls) == 3  # looped instead of exiting after the first success


def test_ralph_aborts_on_instant_success_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A provider that returns success instantly (misconfig / no-op) must not let
    # the orchestrator spin forever; it aborts after a sustained fast streak.
    monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
    monkeypatch.setattr(ralph_robin, "select_provider", lambda cfg, logs, ci, sk: _usable_selection())
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 1000.0)  # no time ever elapses per iteration
    monkeypatch.setattr(ralph_robin, "sleep_seconds", lambda s: None)
    calls: list[str] = []
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda scfg: (calls.append(scfg.provider), 0)[1])

    rc = ralph_robin.main(_ralph_main_argv(tmp_path, "--max-iterations", "0", "--max-duration", "0", "--min-iteration-seconds", "5"))
    assert rc == common.AUTONOMY_ABORT_STATUS
    assert len(calls) == ralph_robin.FAST_SUCCESS_ABORT_STREAK


def test_ralph_loops_until_max_duration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
    monkeypatch.setattr(ralph_robin, "select_provider", lambda cfg, logs, ci, sk: _usable_selection())
    clock = {"t": 0.0}
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: clock["t"])
    calls: list[str] = []

    def fake_run(scfg: scheduler.SchedulerConfig) -> int:
        clock["t"] += 2000.0  # each increment burns ~33 minutes
        calls.append(scfg.provider)
        return 0

    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", fake_run)
    rc = ralph_robin.main(_ralph_main_argv(tmp_path, "--max-iterations", "0", "--max-duration", "1h"))
    assert rc == 0
    assert len(calls) == 2  # 0s -> 2000s -> 4000s exceeds 3600s budget


def test_ralph_suspends_when_all_blocked_then_continues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "100")
    selections = [
        {  # everything blocked: soonest reset at epoch 1000
            "index": -1,
            "provider": "",
            "rotation_reason": "all-skipped",
            "decisions": [
                {"provider": "claude", "wait_until": 1000},
                {"provider": "codex", "wait_until": 2000},
            ],
        },
        _usable_selection(),  # provider free again after the wait
    ]
    monkeypatch.setattr(ralph_robin, "select_provider", lambda cfg, logs, ci, sk: selections.pop(0))
    # One chunk covers the whole wait here; the stub advances the wall clock so
    # wait_until_epoch's poll loop terminates (in production time.time() does).
    monkeypatch.setenv("LLM_RALPH_WAIT_POLL_SECONDS", "100000")
    slept: list[float] = []

    def fake_sleep(s: float) -> None:
        slept.append(s)
        monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", str(100 + int(sum(slept))))

    monkeypatch.setattr(ralph_robin, "sleep_seconds", fake_sleep)
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda scfg: 0)

    rc = ralph_robin.main(_ralph_main_argv(tmp_path, "--max-iterations", "1", "--max-duration", "0"))
    assert rc == 0
    assert slept == [900]  # waited until the soonest reset (epoch 1000 - now 100) instead of exiting


def test_ralph_suspends_machine_until_earliest_renewal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # When every provider is rate-limited, Ralph suspends until the EARLIEST
    # window renewal across the rotation (epoch 1000 here), then resumes its own
    # loop and re-selects. Suspend infra is disabled so it uses the in-process
    # fallback we can observe.
    monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "100")
    monkeypatch.setenv("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "1")
    selections = [
        {
            "index": 0,
            "provider": "claude",
            "rotation_reason": "all-unusable",
            "all_rate_limited": True,
            "decision": {"provider": "claude", "wait_until": 1000},
            "decisions": [
                {"provider": "claude", "wait_until": 2000},
                {"provider": "codex", "wait_until": 1000},
            ],
        },
        _usable_selection(),  # rotation recovers after the wake
    ]
    monkeypatch.setattr(ralph_robin, "select_provider", lambda cfg, logs, ci, sk: selections.pop(0))
    # One chunk covers the whole wait here; the stub advances the wall clock so
    # wait_until_epoch's poll loop terminates (in production time.time() does).
    monkeypatch.setenv("LLM_RALPH_WAIT_POLL_SECONDS", "100000")
    slept: list[float] = []

    def fake_sleep(s: float) -> None:
        slept.append(s)
        monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", str(100 + int(sum(slept))))

    monkeypatch.setattr(ralph_robin, "sleep_seconds", fake_sleep)
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda scfg: 0)

    rc = ralph_robin.main(_ralph_main_argv(tmp_path, "--max-iterations", "1", "--max-duration", "0", "--min-iteration-seconds", "0"))
    assert rc == 0
    assert slept == [900]  # epoch 1000 (earliest of 2000/1000) minus now 100


def test_ralph_even_burn_prefers_ready_provider_over_blocked_higher_weekly_headroom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 0, "resets_at": 1100},
            "week": {"remaining": 81, "resets_at": 1000 + (6 * 86400)},
        },
        "codex": {
            "available": True,
            "five_hour": {"remaining": 100, "resets_at": 2000},
            "week": {"remaining": 50, "resets_at": 1000 + (6 * 86400)},
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider: snapshots[provider])

    selected = ralph_robin.select_provider(cfg, logs, 1, set())
    assert selected["provider"] == "codex"
    assert selected["rotation_reason"] == "current-usable"
    assert selected["decision"]["reason"] == "usable"

    snapshots["claude"] = {
        "available": True,
        "five_hour": {"remaining": 100, "resets_at": 2000},
        "week": {"remaining": 0, "resets_at": 1000 + (6 * 86400)},
    }
    selected_weekly_exhausted = ralph_robin.select_provider(cfg, logs, 1, set())
    assert selected_weekly_exhausted["provider"] == "codex"
    assert selected_weekly_exhausted["rotation_reason"] == "current-usable"


def test_scheduler_more_system_edges(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logs = common.setup_run_logs(tmp_path / "logs", "s")
    cfg = scheduler.SchedulerConfig(provider="claude", prompt_text="p", cwd=str(tmp_path), log_dir=tmp_path / "logs", run_dir=logs.run_dir)
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

    cfg2 = scheduler.SchedulerConfig(provider="codex", prompt_text="p", cwd=str(tmp_path), exec_mode="tmux", tmux_target="session")
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    out = tmp_path / "out"
    status = tmp_path / "status"
    assert scheduler.run_tmux(cfg2, logs, ["true"], out, status) == 127
    assert "tmux not installed" in out.read_text()


def test_error_fallback_branches(env: dict[str, str], fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PATH", env["PATH"])
    write_exe(fake_bin / "date", "#!/usr/bin/env python3\nimport sys\nprint('1234' if '-d' in sys.argv else '')\n")
    assert common.parse_epoch("next friday") == 1234
    write_exe(fake_bin / "date", "#!/usr/bin/env python3\nimport sys\nprint('not-an-int')\n")
    assert common.parse_epoch("next friday") is None
    write_exe(fake_bin / "date", "#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    assert common.fmt_reset(None) == ""
    assert common.fmt_reset(0).startswith("1970-01-01")
    assert common.format_local_epoch(0).startswith("1970-01-01")
    assert common.now_epoch({"LLM_USAGE_NOW_EPOCH": "bad"}) > 0
    assert common.copilot_monthly_reset_epoch({"LLM_USAGE_NOW_EPOCH": "1798761600", "LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS": "bad"}) is not None
    assert common.num(True) is None
    assert common.num(None) is None
    assert common.num("nope") is None
    assert common.fmt_number("1.25") == "1.2"
    assert common.remaining_from_used(-5) == 100
    assert common.remaining_from_used(125) == 0
    assert common.window_from({"resets_at": 99}, 300) == {"used": None, "resets_at": 99, "window_minutes": 300}

    assert common.normalize_codex_obj({"msg": {"rateLimits": {"spark-model": {"primary": {"used_percent": 3}}}}}, "src")["rows"][0]["key"] == "codex-spark"
    assert common.normalize_claude_obj({"five_hour": {"utilization": 7}, "seven_day": {"used_percent": 8}}, "src")["week"]["used"] == 8
    assert common.json_for_provider(None, "codex") == {"provider": "codex", "available": False}
    assert common.decorate_window(None) is None
    assert common.json_for_copilot({"provider": "copilot", "monthly": None}, True)["available"] is False
    assert common.read_copilot_live(env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "trust_prompt_seen"})["reason"] == "trust-prompt"
    assert common.read_copilot_live(env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "AI Credits: 4"})["ai_credits"]["used"] == 4

    cache_dir = common.usage_cache_dir(env)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "copilot-usage.json").write_text("{bad", encoding="utf-8")
    fake_proc: list[list[str]] = []
    original_popen = subprocess.Popen

    class PopenStub:
        def __init__(self, args, **kwargs) -> None:
            fake_proc.append(list(args))

    monkeypatch.setattr(common.subprocess, "Popen", PopenStub)
    assert common.read_copilot(env | {"LLM_USAGE_COPILOT_CACHE_TTL": "999", "LLM_USAGE_COPILOT_REFRESH_WAIT": "0"})["reason"] == "refresh-pending"
    (cache_dir / "copilot-usage.json").write_text('{"provider":"copilot","available":false,"reason":"format-changed"}', encoding="utf-8")
    assert common.read_copilot(env | {"LLM_USAGE_COPILOT_CACHE_TTL": "999", "LLM_USAGE_COPILOT_REFRESH_WAIT": "0"})["reason"] == "refresh-pending"
    (cache_dir / "copilot-usage.json").write_text('{"provider":"copilot","available":false,"reason":"timeout"}', encoding="utf-8")
    assert common.read_copilot(env | {"LLM_USAGE_COPILOT_CACHE_TTL": "999", "LLM_USAGE_COPILOT_REFRESH_WAIT": "0"})["reason"] == "refresh-pending"
    fresh_env = env | {"XDG_CACHE_HOME": str(tmp_path / "fresh-xdg"), "LLM_USAGE_COPILOT_CACHE_TTL": "999", "LLM_USAGE_COPILOT_REFRESH_WAIT": "0"}
    assert common.read_copilot(fresh_env)["reason"] == "refresh-pending"
    assert fake_proc
    monkeypatch.setattr(common.subprocess, "Popen", original_popen)
    (cache_dir / "copilot-refresh.lock").mkdir(exist_ok=True)
    os.utime(cache_dir / "copilot-refresh.lock", (1, 1))
    (cache_dir / "copilot-usage.json").write_text('{"provider":"copilot","monthly":{"remaining":2}}', encoding="utf-8")
    os.utime(cache_dir / "copilot-usage.json", (1, 1))
    assert common.read_copilot(env | {"LLM_USAGE_COPILOT_CACHE_TTL": "1", "LLM_USAGE_COPILOT_REFRESH_WAIT": "0"})["monthly"]["remaining"] == 2

    log = cache_dir / "llm-usage.log"
    log.write_text(
        '{"ts":1000,"provider":"p","window":"w","remaining":80}\n'
        '{"ts":1060,"provider":"p","window":"w","remaining":90}\n'
        '{"ts":1120,"provider":"p","window":"w","remaining":89}\n'
        'not-json\n',
        encoding="utf-8",
    )
    assert common.estimate_remaining_time_from_log("p", "w", 1, env | {"LLM_USAGE_NOW_EPOCH": "1120", "LLM_USAGE_LOG_TAIL_LINES": "bad"}) == "1m"
    assert common.estimate_remaining_time_from_log("p", "missing", 1, env) == "-"
    assert common.estimate_remaining_time_from_log("p", "w", "bad", env) == "-"
    common.log_usage_sample("p", "w", "-", env)

    assert common.usage_decision_for_provider("copilot", "weekly", "1", "60", {}, env)["reason"] == "unsupported-scope"
    assert common.usage_decision_for_provider("codex", "monthly", "1", "60", {"available": True}, env)["reason"] == "unsupported-scope"
    assert common.usage_decision_for_provider("codex", "5h", "1", "60", {"available": True, "five_hour": {"resets_at": 2000}}, env)["reason"] == "inconclusive-usage"
    assert common.usage_snapshot_for_provider("unknown", env)["reason"] == "unsupported-provider"
    assert common.output_is_retryable(130, "", attached=True) is False
    assert common.output_is_retryable(1, "", attached=True) is True

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("same", encoding="utf-8")
    logs = common.setup_run_logs(tmp_path / "logs-same", "same", run_dir=tmp_path)
    assert common.load_prompt("", str(prompt), logs)[0] == "same"

    with pytest.raises(SystemExit):
        scheduler.parse_args(["--help"])
    monkeypatch.setenv("LLM_SCHEDULER_PRE_SUSPEND_CONFIRMATION_SECONDS", "bad")
    with pytest.raises(SystemExit):
        scheduler.parse_args([])
    monkeypatch.delenv("LLM_SCHEDULER_PRE_SUSPEND_CONFIRMATION_SECONDS", raising=False)
    cfg = scheduler.SchedulerConfig(provider="codex", prompt_text="p", cwd=str(tmp_path))
    monkeypatch.setenv("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "bad")
    with pytest.raises(SystemExit):
        scheduler.validate_args(cfg)
    monkeypatch.delenv("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", raising=False)
    assert scheduler.parse_date_d("not-a-date") is None
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(provider="codex", command_template="true")) == "from command template"
    assert scheduler.highlight_provider_text(b"Tool call: shell\nTitle:\nplain\n", stream_name="stdout", enabled=True).count(b"\x1b[") >= 2
    assert scheduler.highlight_provider_text(b"plain\n", stream_name="stdout", enabled=False) == b"plain\n"

    cfg = scheduler.SchedulerConfig(provider="codex", prompt_text="p", cwd=str(tmp_path), attached=True)
    logs2 = common.setup_run_logs(tmp_path / "submit-logs", "s")
    monkeypatch.setattr(scheduler, "run_fresh_attached", lambda _cfg, _argv, _out, _status: 0)
    assert scheduler.submit_once(cfg, logs2, 1, ["true"]) == 1
    (logs2.run_dir / "attempt-2.status").write_text("bad", encoding="utf-8")
    (logs2.run_dir / "attempt-2.out").write_text("", encoding="utf-8")
    monkeypatch.setattr(scheduler, "run_fresh_attached", lambda _cfg, _argv, _out, _status: 0)
    assert scheduler.submit_once(cfg, logs2, 2, ["true"]) == 1

    ucfg = usage.Config()
    ucfg.color_enabled = True
    assert usage.colorize_percent("9%", ucfg).startswith("\x1b[0;31m")
    assert usage.colorize_percent("29%", ucfg).startswith("\x1b[0;33m")
    assert usage.colorize_percent("30%", ucfg).startswith("\x1b[0;32m")
    assert usage.colorize_percent("bad%", ucfg) == "bad%"
    plain = usage.Config()
    plain.color_enabled = False
    assert usage.progress_bar(100) == "█" * 10
    assert usage.progress_bar(0) == "░" * 10
    assert usage.progress_bar(35) == "█" * 4 + "░" * 6
    assert usage.render_remaining("100%", plain) == "100% ██████████"
    assert usage.render_remaining("35%", plain) == " 35% ████░░░░░░"
    assert usage.render_remaining("unavailable", plain) == "unavailable"
    assert usage.render_remaining("-", plain) == "-"
    assert usage.render_remaining("9%", ucfg).startswith("\x1b[0;31m")
    # Daily budget helper remains available to scheduler/Ralph paths.
    at1000 = {"LLM_USAGE_NOW_EPOCH": "1000"}
    assert common.daily_budget_percent(50, 1000 + 86400, at1000) == 50.0  # 1 day out -> 50%
    assert common.daily_budget_percent(20, 1000 + 7200, at1000) == 20.0  # 2h out -> all remaining
    assert round(common.daily_budget_percent(35, 1000 + (5 * 86400), at1000), 1) == 7.0
    assert common.daily_budget_percent(None, 2000, at1000) is None
    assert common.daily_budget_percent(50, None, at1000) is None
    assert common.daily_budget_percent(50, 900, at1000) is None  # reset already passed
    assert usage.render_daily_budget(None, plain) == "· no rate data"
    assert usage.render_daily_budget(60, plain, 50) == "↑ headroom"
    assert usage.render_daily_budget(50, plain, 50) == "= on pace"
    assert usage.render_daily_budget(40, plain, 50) == "↓ conserve"
    assert usage.render_daily_budget(50, ucfg, 50).startswith("\x1b[0;32m")
    assert usage.render_daily_budget(60, ucfg, 50).startswith("\x1b[0;36m")
    assert usage.render_daily_budget(40, ucfg, 50).startswith("\x1b[0;33m")
    assert usage.render_guidance_info(usage.GuidanceInfo("× empty", "empty"), ucfg).startswith("\x1b[0;31m")
    assert usage.render_gate(1, plain) == "yes"
    assert usage.render_gate(0, plain) == "no"
    assert usage.render_gate(1, ucfg) == "yes"
    assert usage.render_gate(0, ucfg).startswith("\x1b[1;31m")
    assert usage.render_pace_or_gate("5h", 94, plain) == "· no rate data"
    assert usage.is_short_window("5h") is True
    assert usage.is_budget_window("weekly") is True
    usage.print_codex_rows(ucfg, {"source": "src", "five_hour": {"used": 10}, "week": {"used": 20}})
    usage.print_copilot_rows(ucfg, None)
    out = capsys.readouterr().out
    assert "Codex" in out and "Copilot" in out
