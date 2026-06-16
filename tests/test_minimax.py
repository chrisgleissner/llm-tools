"""Tests for the MiniMax provider adapter (mmx CLI)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from llm_tools import common, usage
from llm_tools.capacity import (
    CapacityKind,
    PROVIDER_MINIMAX,
    SCOPE_5H,
    SCOPE_WEEKLY,
)
from llm_tools.providers import (
    minimax_cli,
    minimax_command_argv,
    minimax_model,
    read_minimax,
)


# --- Env-var reader -----------------------------------------------------------


def test_minimax_model_defaults_to_general() -> None:
    assert minimax_model({}) == "general"


def test_minimax_model_accepts_override() -> None:
    assert minimax_model({"LLM_USAGE_MINIMAX_MODEL": "video"}) == "video"


def test_minimax_model_falls_back_for_blank() -> None:
    assert minimax_model({"LLM_USAGE_MINIMAX_MODEL": ""}) == "general"


# --- read_minimax: env-var path -----------------------------------------------


def test_read_minimax_missing_cli_without_env(env: dict[str, str]) -> None:
    # No mmx binary on PATH and no env-var fallback: should report missing-cli.
    env["PATH"] = "/var/empty"
    snap = read_minimax(env)
    assert snap.provider == PROVIDER_MINIMAX
    assert snap.available is False
    assert snap.reason == "missing-cli"


def test_read_minimax_env_5h_above_minimum(env: dict[str, str]) -> None:
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "75"
    env["LLM_USAGE_MINIMAX_5H_RESET_EPOCH"] = "1700000000"
    env["PATH"] = "/var/empty"
    snap = read_minimax(env)
    assert snap.available is True
    assert len(snap.scopes) == 1
    five = snap.scopes[0]
    assert five.name == SCOPE_5H
    assert five.kind == CapacityKind.RESET_WINDOW
    assert five.remaining_percent == 75.0
    assert five.reset_epoch == 1700000000


def test_read_minimax_env_weekly_only(env: dict[str, str]) -> None:
    env["LLM_USAGE_MINIMAX_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH"] = "1700003600"
    env["PATH"] = "/var/empty"
    snap = read_minimax(env)
    assert snap.available is True
    assert [s.name for s in snap.scopes] == [SCOPE_WEEKLY]
    weekly = snap.scopes[0]
    assert weekly.remaining_percent == 97.0
    assert weekly.reset_epoch == 1700003600


def test_read_minimax_env_both_windows(env: dict[str, str]) -> None:
    env.update(
        {
            "LLM_USAGE_MINIMAX_5H_PERCENT": "75",
            "LLM_USAGE_MINIMAX_5H_RESET_EPOCH": "1700000000",
            "LLM_USAGE_MINIMAX_WEEKLY_PERCENT": "97",
            "LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH": "1700003600",
            "PATH": "/var/empty",
        }
    )
    snap = read_minimax(env)
    assert snap.available is True
    assert {s.name for s in snap.scopes} == {SCOPE_5H, SCOPE_WEEKLY}


def test_read_minimax_env_clamps_percent(env: dict[str, str]) -> None:
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "150"  # bogus; clamp to 100
    env["LLM_USAGE_MINIMAX_WEEKLY_PERCENT"] = "-5"  # bogus; clamp to 0
    env["PATH"] = "/var/empty"
    snap = read_minimax(env)
    by_name = {s.name: s for s in snap.scopes}
    assert by_name[SCOPE_5H].remaining_percent == 100.0
    assert by_name[SCOPE_WEEKLY].remaining_percent == 0.0


def test_read_minimax_env_accepts_millisecond_reset(env: dict[str, str]) -> None:
    # `mmx` reports millisecond epochs. The reader must accept both seconds
    # and milliseconds so the env-var fallback can mirror either source.
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "50"
    env["LLM_USAGE_MINIMAX_5H_RESET_EPOCH"] = "1781431200000"
    env["PATH"] = "/var/empty"
    snap = read_minimax(env)
    assert snap.scopes[0].reset_epoch == 1781431200


def test_read_minimax_env_ignores_garbage_values(env: dict[str, str]) -> None:
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "not-a-number"
    env["LLM_USAGE_MINIMAX_WEEKLY_PERCENT"] = "?"
    env["PATH"] = "/var/empty"
    snap = read_minimax(env)
    # Garbage values mean no scope data; the reader falls through to
    # missing-cli because there is no CLI and no env-var data to surface.
    assert snap.available is False
    assert snap.reason == "missing-cli"


# --- read_minimax: CLI path ---------------------------------------------------


def test_read_minimax_parses_quota_show_json(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "mmx"
    payload = {
        "model_remains": [
            {
                "model_name": "general",
                "start_time": 1781413200000,
                "end_time": 1781431200000,
                "current_interval_remaining_percent": 75,
                "current_weekly_remaining_percent": 97,
                "weekly_end_time": 1781481600000,
            },
            {
                "model_name": "video",
                "current_interval_remaining_percent": 100,
                "current_weekly_remaining_percent": 100,
            },
        ],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps(" + json.dumps(payload) + "))\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    snap = read_minimax(env)
    assert snap.available is True
    by_name = {s.name: s for s in snap.scopes}
    assert by_name[SCOPE_5H].remaining_percent == 75.0
    assert by_name[SCOPE_5H].reset_epoch == 1781431200
    assert by_name[SCOPE_WEEKLY].remaining_percent == 97.0
    assert by_name[SCOPE_WEEKLY].reset_epoch == 1781481600
    # Source advertises the CLI path.
    assert "mmx quota" in (snap.source or "")


def test_read_minimax_parses_stdout_json_with_stderr_warning(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "mmx"
    payload = {
        "model_remains": [
            {
                "model_name": "general",
                "current_interval_remaining_percent": 44,
                "current_weekly_remaining_percent": 88,
            }
        ]
    }
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdout.write(json.dumps(" + json.dumps(payload) + "))\n"
        "sys.stderr.write('warning: using cached auth\\n')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    snap = read_minimax(env)
    assert snap.available is True
    by_name = {s.name: s for s in snap.scopes}
    assert by_name[SCOPE_5H].remaining_percent == 44.0
    assert by_name[SCOPE_WEEKLY].remaining_percent == 88.0


def test_read_minimax_classifies_stderr_error_envelope(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "mmx"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stderr.write(json.dumps({'error': {'code': 1, 'message': 'please login first'}}))\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    snap = read_minimax(env)
    assert snap.available is False
    assert snap.reason == "not-authenticated"


def test_read_minimax_ignores_non_matching_model(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "mmx"
    # CLI returns only a "video" row; the "general" model is missing.
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'model_remains':[{'model_name':'video','current_interval_remaining_percent':100,'current_weekly_remaining_percent':100}]}))\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    snap = read_minimax(env)
    # No matching model + no env fallback → unavailable with missing-cli
    # because the CLI is the only path the reader could have used to get
    # the data, and the payload did not include a "general" row.
    assert snap.available is False


def test_read_minimax_handles_cli_timeout(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "mmx"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_MINIMAX_TIMEOUT"] = "1"
    snap = read_minimax(env)
    # CLI is installed but the call timed out, so the reader reports
    # inconclusive-usage rather than missing-cli: the binary is present.
    assert snap.available is False
    assert snap.reason == "inconclusive-usage"


def test_read_minimax_handles_nonzero_exit(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "mmx"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('auth error')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    # Env-var fallback should still surface usable data even when the CLI fails.
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "40"
    env["LLM_USAGE_MINIMAX_5H_RESET_EPOCH"] = "1700000000"
    snap = read_minimax(env)
    assert snap.available is True
    assert snap.scopes[0].remaining_percent == 40.0


def test_read_minimax_handles_invalid_json(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "mmx"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "print('not json at all')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "60"
    env["LLM_USAGE_MINIMAX_5H_RESET_EPOCH"] = "1700000000"
    snap = read_minimax(env)
    assert snap.available is True
    assert snap.scopes[0].remaining_percent == 60.0


# --- Command construction -----------------------------------------------------


def test_minimax_command_argv_attached() -> None:
    argv = minimax_command_argv(cfg_attached=True, cwd="/tmp/work", prompt="hello world")
    assert argv == ["mmx"]


def test_minimax_command_argv_headless() -> None:
    argv = minimax_command_argv(cfg_attached=False, cwd="/tmp/work", prompt="hello world")
    assert argv == ["mmx", "run", "--auto", "-C", "/tmp/work", "hello world"]


def test_minimax_cli_returns_none_when_missing(env: dict[str, str]) -> None:
    env["PATH"] = "/var/empty"
    assert minimax_cli(env) is None


def test_minimax_cli_finds_binary(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "mmx"
    fake.write_text("#!/usr/bin/env python3\nprint('mmx')\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    assert minimax_cli(env) == str(fake)


# --- Scheduler / Ralph integration --------------------------------------------


def test_scheduler_accepts_minimax_provider(env: dict[str, str]) -> None:
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "75"
    env["LLM_USAGE_MINIMAX_5H_RESET_EPOCH"] = "1700000000"
    env["LLM_USAGE_MINIMAX_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH"] = "1700003600"
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "minimax",
            "--prompt",
            "x",
            "--scope",
            "5h",
            "--command-template",
            "true",
            "--dry-run",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    run_dir = Path(result.stdout.strip().split()[-1])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["type"] == "usage_decision"]
    assert decisions
    assert decisions[0]["data"]["usable"] is True


def test_scheduler_rejects_minimax_invalid_scope(env: dict[str, str]) -> None:
    bad = run_cmd(
        ["./llm-scheduler", "--provider", "minimax", "--prompt", "x", "--scope", "balance"],
        env,
    )
    assert bad.returncode == 2
    assert "not valid for minimax" in bad.stderr


def test_scheduler_minimax_5h_below_minimum_blocks(env: dict[str, str]) -> None:
    # Use a reset epoch far in the future so the decider still treats the
    # window as waiting on reset; otherwise an already-past reset makes
    # the scope look fresh.
    env["LLM_USAGE_NOW_EPOCH"] = "1781413200"  # 2026-06-13
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "0"
    env["LLM_USAGE_MINIMAX_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_MINIMAX_WEEKLY_PERCENT"] = "0"
    env["LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH"] = "1781481600"
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "minimax",
            "--prompt",
            "x",
            "--scope",
            "auto",
            "--command-template",
            "true",
            "--dry-run",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    run_dir = Path(result.stdout.strip().split()[-1])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["type"] == "usage_decision"]
    assert decisions
    assert decisions[0]["data"]["usable"] is False
    assert decisions[0]["data"]["reason"] == "rate-limited"


def test_ralph_robin_rejects_unknown_provider(env: dict[str, str]) -> None:
    bad = run_cmd(
        ["./ralph-robin", "--providers", "minimax,bogus", "--prompt", "x"],
        env,
    )
    assert bad.returncode == 2
    assert "invalid provider in --providers" in bad.stderr


def test_ralph_robin_accepts_minimax_provider(env: dict[str, str]) -> None:
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "75"
    env["LLM_USAGE_MINIMAX_5H_RESET_EPOCH"] = "1700000000"
    env["LLM_USAGE_MINIMAX_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH"] = "1700003600"
    result = run_cmd(
        [
            "./ralph-robin",
            "--providers",
            "minimax",
            "--prompt",
            "x",
            "--command-template",
            "true",
            "--no-retry",
            "--max-iterations",
            "1",
            "--max-duration",
            "30s",
            "--state-file",
            str(Path(env["HOME"]) / "ralph-state.json"),
            "--log-dir",
            str(Path(env["HOME"]) / "ralph-logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr


# --- Usage table rendering ----------------------------------------------------


def test_usage_table_renders_minimax_rows(env: dict[str, str], capsys, monkeypatch) -> None:
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("LLM_USAGE_DISABLE_COPILOT", "1")
    monkeypatch.setenv("LLM_USAGE_MINIMAX_5H_PERCENT", "75")
    monkeypatch.setenv("LLM_USAGE_MINIMAX_5H_RESET_EPOCH", "1700000000")
    monkeypatch.setenv("LLM_USAGE_MINIMAX_WEEKLY_PERCENT", "97")
    monkeypatch.setenv("LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH", "1700003600")
    monkeypatch.setenv("PATH", "/var/empty")  # no mmx binary

    merged = dict(env)
    for key in (
        "LLM_USAGE_MINIMAX_5H_PERCENT",
        "LLM_USAGE_MINIMAX_5H_RESET_EPOCH",
        "LLM_USAGE_MINIMAX_WEEKLY_PERCENT",
        "LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH",
        "PATH",
    ):
        if key in os.environ:
            merged[key] = os.environ[key]
    snap = read_minimax(merged)
    json_obj = {
        "provider": snap.provider,
        "available": snap.available,
        "reason": snap.reason,
        "source": snap.source,
        "selected_model": snap.selected_model,
        "scopes": [
            {
                "name": s.name,
                "kind": s.kind,
                "remaining_percent": s.remaining_percent,
                "remaining_amount": s.remaining_amount,
                "total_amount": s.total_amount,
                "currency": s.currency,
                "reset_epoch": s.reset_epoch,
                "label": s.label,
                "source": s.source,
            }
            for s in snap.scopes
        ],
    }

    from io import StringIO
    import contextlib

    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_minimax_rows(cfg, json_obj)
    out = buf.getvalue()
    assert "MiniMax" in out
    assert "5h" in out
    assert "weekly" in out
    assert "75%" in out
    assert "97%" in out


def test_usage_table_renders_minimax_unavailable(env: dict[str, str], capsys, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/var/empty")

    from io import StringIO
    import contextlib

    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_minimax_rows(cfg, None)
    out = buf.getvalue()
    assert "MiniMax" in out
    assert "unavailable" in out


def test_usage_table_collapses_inconclusive_to_unavailable(capsys) -> None:
    """An ``inconclusive-usage`` reason (e.g. MiniMax with no token plan) is
    shown as the short ``unavailable`` in the table, not the internal code.
    The internal reason still flows through for scheduler gating."""
    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    minimax_json = {
        "provider": PROVIDER_MINIMAX,
        "available": False,
        "reason": "inconclusive-usage",
        "source": "mmx cli",
        "scopes": [],
    }
    usage.print_minimax_rows(cfg, minimax_json)
    out = capsys.readouterr().out
    assert "MiniMax" in out
    assert "unavailable" in out
    assert "inconclusive-usage" not in out
    # The exhausted-quota marker must not appear for an unmeasured provider.
    assert "× empty" not in out


def test_display_remaining_maps_reason_codes_but_keeps_real_values() -> None:
    for reason in ("inconclusive-usage", "missing-cli", "refresh-pending", "reader-error"):
        assert usage.display_remaining(reason) == "unavailable"
    # Percentages, balances, and unmetered states pass through untouched.
    for value in ("82%", "spent $19.1", "£12.40", "byok", "ungated", "-"):
        assert usage.display_remaining(value) == value


# --- JSON contract ------------------------------------------------------------


def test_minimax_json_top_level_in_llm_usage_json(env: dict[str, str]) -> None:
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "75"
    env["LLM_USAGE_MINIMAX_5H_RESET_EPOCH"] = "1700000000"
    env["LLM_USAGE_MINIMAX_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH"] = "1700003600"
    env["LLM_USAGE_DISABLE_COPILOT"] = "1"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("PATH", "/var/empty")
    result = run_cmd(["./llm-usage", "--json"], env)
    monkeypatch.undo()
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "minimax" in data
    assert data["minimax"]["provider"] == "minimax"
    assert data["minimax"]["available"] is True
    assert {s["name"] for s in data["minimax"]["scopes"]} == {"5h", "weekly"}


# --- Helpers ------------------------------------------------------------------


def run_cmd(args, env):
    from .conftest import run_cmd as _run_cmd
    return _run_cmd(args, env)
