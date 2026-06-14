"""Tests for the OpenCode CLI provider adapter."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from llm_tools import common, scheduler
from llm_tools.capacity import (
    CapacityKind,
    CapacityScope,
    PROVIDER_OPENCODE,
    SCOPE_BALANCE,
    SCOPE_BUDGET,
)
from llm_tools.providers import (
    OPENCODE_MODES,
    opencode_cli,
    opencode_command_argv,
    opencode_currency,
    opencode_min_balance,
    opencode_mode,
    opencode_monthly_reset_epoch,
    read_opencode,
)


# --- Env-var reader -----------------------------------------------------------


def test_opencode_mode_defaults_to_gateway() -> None:
    assert opencode_mode({}) == "gateway"


def test_opencode_mode_accepts_known_values() -> None:
    for mode in OPENCODE_MODES:
        assert opencode_mode({"LLM_USAGE_OPENCODE_MODE": mode}) == mode


def test_opencode_mode_falls_back_for_unknown() -> None:
    assert opencode_mode({"LLM_USAGE_OPENCODE_MODE": "weird"}) == "gateway"


def test_opencode_min_balance_default_and_override() -> None:
    assert opencode_min_balance({}) == 1.0
    assert opencode_min_balance({"LLM_USAGE_OPENCODE_MIN_BALANCE": "5"}) == 5.0
    assert opencode_min_balance({"LLM_USAGE_OPENCODE_MIN_BALANCE": "bad"}) == 1.0


def test_opencode_currency_returns_env_value() -> None:
    assert opencode_currency({}) is None
    assert opencode_currency({"LLM_USAGE_OPENCODE_CURRENCY": "USD"}) == "USD"


def test_opencode_monthly_reset_epoch_advances_to_next_month(env: dict[str, str]) -> None:
    env["LLM_USAGE_NOW_EPOCH"] = "1000"
    epoch = opencode_monthly_reset_epoch(env)
    assert epoch > 1000
    assert epoch - 1000 < 32 * 86400


# --- read_opencode: env-var path -----------------------------------------------


def test_read_opencode_inconclusive_without_data(env: dict[str, str]) -> None:
    # No mode, no env vars; with no kilo binary on PATH, expect
    # inconclusive-usage. The conftest env PATH includes the host's
    # opencode install, so we need to force it off.
    env["PATH"] = "/var/empty"
    snap = read_opencode(env)
    assert snap.provider == PROVIDER_OPENCODE
    assert snap.available is False
    assert snap.reason == "inconclusive-usage"
    assert any(s.kind == CapacityKind.UNKNOWN for s in snap.scopes)


def test_read_opencode_balance_above_minimum(env: dict[str, str]) -> None:
    env["PATH"] = "/var/empty"
    env.update(
        {
            "LLM_USAGE_OPENCODE_BALANCE": "12.50",
            "LLM_USAGE_OPENCODE_CURRENCY": "USD",
            "LLM_USAGE_OPENCODE_MIN_BALANCE": "1",
        }
    )
    snap = read_opencode(env)
    assert snap.available is True
    balances = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE]
    assert balances
    assert balances[0].name == SCOPE_BALANCE
    assert balances[0].remaining_amount == 12.5
    assert balances[0].currency == "USD"


def test_read_opencode_balance_below_minimum(env: dict[str, str]) -> None:
    env["PATH"] = "/var/empty"
    env.update({"LLM_USAGE_OPENCODE_BALANCE": "0.5", "LLM_USAGE_OPENCODE_MIN_BALANCE": "1"})
    snap = read_opencode(env)
    assert snap.available is True
    assert any(
        s.kind == CapacityKind.BALANCE and s.remaining_amount == 0.5 for s in snap.scopes
    )


def test_read_opencode_budget_scope(env: dict[str, str]) -> None:
    env["PATH"] = "/var/empty"
    env.update(
        {
            "LLM_USAGE_OPENCODE_MONTHLY_BUDGET": "100",
            "LLM_USAGE_OPENCODE_MONTHLY_SPENT": "38",
            "LLM_USAGE_OPENCODE_CURRENCY": "USD",
        }
    )
    snap = read_opencode(env)
    budgets = [s for s in snap.scopes if s.kind == CapacityKind.BUDGET]
    assert budgets
    assert budgets[0].name == SCOPE_BUDGET
    assert budgets[0].total_amount == 100.0
    assert budgets[0].remaining_amount == 62.0
    assert budgets[0].remaining_percent == pytest.approx(62.0)
    assert budgets[0].reset_epoch is not None and budgets[0].reset_epoch > 0


def test_read_opencode_balance_and_budget_both_visible(env: dict[str, str]) -> None:
    env["PATH"] = "/var/empty"
    env.update(
        {
            "LLM_USAGE_OPENCODE_BALANCE": "5",
            "LLM_USAGE_OPENCODE_MONTHLY_BUDGET": "20",
            "LLM_USAGE_OPENCODE_MONTHLY_SPENT": "5",
        }
    )
    snap = read_opencode(env)
    kinds = {s.kind for s in snap.scopes}
    assert CapacityKind.BALANCE in kinds
    assert CapacityKind.BUDGET in kinds


def test_read_opencode_byok_mode_ungated(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "opencode"
    fake.write_text("#!/usr/bin/env python3\nprint('mock')\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_OPENCODE_MODE"] = "byok"
    snap = read_opencode(env)
    assert snap.available is True
    scopes = snap.scopes
    assert any(s.kind == CapacityKind.UNGATED for s in scopes)
    label = next(s.label for s in scopes if s.kind == CapacityKind.UNGATED)
    assert label == "byok"


def test_read_opencode_local_mode_label(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "opencode"
    fake.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_OPENCODE_MODE"] = "local"
    snap = read_opencode(env)
    assert snap.available is True
    label = next(s.label for s in snap.scopes if s.kind == CapacityKind.UNGATED)
    assert label == "local"


def test_read_opencode_ungated_mode_label(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "opencode"
    fake.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_OPENCODE_MODE"] = "ungated"
    snap = read_opencode(env)
    assert snap.available is True
    label = next(s.label for s in snap.scopes if s.kind == CapacityKind.UNGATED)
    assert label == "unmetered"


def test_read_opencode_missing_cli_ungated_reports_missing_cli(env: dict[str, str]) -> None:
    env["LLM_USAGE_OPENCODE_MODE"] = "byok"
    env["PATH"] = "/var/empty"
    snap = read_opencode(env)
    assert snap.available is False
    assert snap.reason == "missing-cli"


# --- OpenCode CLI stats output -----------------------------------------------


def test_read_opencode_parses_stats_text(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "opencode"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "print('┌────┐')\n"
        "print('│Total Cost                  $7.50│')\n"
        "print('│Avg Cost/Day                $1.50│')\n"
        "print('│Sessions                       10│')\n"
        "print('│Days                            5│')\n"
        "print('└────┘')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    snap = read_opencode(env)
    # The text parser should add a budget scope (env-var driven) plus the
    # parsed cost feeds into the source.
    budgets = [s for s in snap.scopes if s.kind == CapacityKind.BUDGET]
    assert any(s.source == "opencode stats + env" or "opencode stats" in s.source for s in snap.scopes)


def test_read_opencode_tui_surfaces_spent_balance(env: dict[str, str], fake_bin: Path) -> None:
    """The TUI-shaped ``opencode stats`` output must surface a BALANCE
    scope with ``extras={'spent': True}`` when no env-var balance is
    configured, so the table shows the captured cost instead of
    ``inconclusive-usage``."""
    fake = fake_bin / "opencode"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "print('┌────┐')\n"
        "print('│Total Cost                  $7.50│')\n"
        "print('│Avg Cost/Day                $1.50│')\n"
        "print('│Sessions                       10│')\n"
        "print('│Days                            5│')\n"
        "print('└────┘')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    snap = read_opencode(env)
    assert snap.available is True
    balances = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE]
    assert balances, f"expected a BALANCE scope from the TUI cost row, got {snap.scopes}"
    assert balances[0].remaining_amount == 7.5
    assert balances[0].currency == "$"
    assert balances[0].extras.get("spent") is True


def test_read_opencode_parses_stats_json(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "opencode"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'cost': 9.0, 'currency': 'USD', 'budget': 50, 'spent': 12}))\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    snap = read_opencode(env)
    budget = next(s for s in snap.scopes if s.kind == CapacityKind.BUDGET)
    assert budget.total_amount == 50.0
    assert budget.remaining_amount == 38.0


# --- OpenCode command construction --------------------------------------------


def test_opencode_command_argv_attached() -> None:
    argv = opencode_command_argv(cfg_attached=True, cwd="/tmp/work", prompt="hello world")
    assert argv == ["opencode"]


def test_opencode_command_argv_headless() -> None:
    argv = opencode_command_argv(cfg_attached=False, cwd="/tmp/work", prompt="hello world")
    assert argv == ["opencode", "run", "-C", "/tmp/work", "hello world"]


# --- Scheduler: OpenCode end-to-end ------------------------------------------


def test_scheduler_accepts_opencode_provider(env: dict[str, str]) -> None:
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "opencode",
            "--prompt",
            "x",
            "--scope",
            "byok",
            "--command-template",
            "true",
            "--dry-run",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env | {"LLM_USAGE_OPENCODE_MODE": "byok"},
    )
    assert result.returncode == 0, result.stderr
    run_dir = Path(result.stdout.strip().split()[-1])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["type"] == "usage_decision"]
    assert decisions


def test_scheduler_rejects_opencode_invalid_scope(env: dict[str, str]) -> None:
    bad = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "opencode",
            "--prompt",
            "x",
            "--scope",
            "5h",
        ],
        env,
    )
    assert bad.returncode == 2
    assert "not valid for opencode" in bad.stderr


def test_scheduler_opencode_balance_below_minimum_blocks(env: dict[str, str]) -> None:
    env["PATH"] = "/var/empty"
    env["LLM_USAGE_OPENCODE_BALANCE"] = "0.1"
    env["LLM_USAGE_OPENCODE_MIN_BALANCE"] = "5"
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "opencode",
            "--prompt",
            "x",
            "--scope",
            "balance",
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
    assert decisions[0]["data"]["reason"] == "insufficient-balance"


def test_scheduler_opencode_byok_launches_with_headless_command(
    env: dict[str, str], fake_bin: Path
) -> None:
    fake = fake_bin / "opencode"
    fake.write_text(f"#!{sys.executable}\nprint('hi from opencode')\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_OPENCODE_MODE"] = "byok"
    env["LLM_SCHEDULER_HEADLESS"] = "1"
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "opencode",
            "--prompt",
            "x",
            "--scope",
            "byok",
            "--command-template",
            "opencode run -C $PWD {prompt}",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    assert "hi from opencode" in result.stdout


def test_scheduler_dry_run_opencode_balance_event(env: dict[str, str]) -> None:
    env["PATH"] = "/var/empty"
    env["LLM_USAGE_OPENCODE_BALANCE"] = "42"
    env["LLM_USAGE_OPENCODE_CURRENCY"] = "USD"
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "opencode",
            "--prompt",
            "x",
            "--scope",
            "balance",
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
    decision_events = [e for e in events if e["type"] == "usage_decision"]
    assert decision_events
    assert decision_events[0]["data"]["usable"] is True
    windows = decision_events[0]["data"]["windows"]
    assert any(w["name"] == "balance" and w["kind"] == "balance" for w in windows)


def test_scheduler_dry_run_opencode_budget_pacing(env: dict[str, str]) -> None:
    env["PATH"] = "/var/empty"
    env["LLM_USAGE_OPENCODE_MONTHLY_BUDGET"] = "100"
    env["LLM_USAGE_OPENCODE_MONTHLY_SPENT"] = "50"
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "opencode",
            "--prompt",
            "x",
            "--scope",
            "budget",
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
    decision_events = [e for e in events if e["type"] == "usage_decision"]
    assert decision_events[0]["data"]["usable"] is True
    windows = decision_events[0]["data"]["windows"]
    budget = next(w for w in windows if w["name"] == "budget")
    assert budget["remaining"] == 50.0
    assert budget["kind"] == "budget"


# --- Usage table rendering for OpenCode --------------------------------------


def test_usage_table_renders_opencode_scope(env: dict[str, str], capsys, monkeypatch) -> None:
    from llm_tools import usage

    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("LLM_USAGE_DISABLE_COPILOT", "1")
    monkeypatch.setenv("LLM_USAGE_OPENCODE_BALANCE", "12.40")
    monkeypatch.setenv("LLM_USAGE_OPENCODE_CURRENCY", "USD")
    monkeypatch.setenv("LLM_USAGE_OPENCODE_MONTHLY_BUDGET", "50")
    monkeypatch.setenv("LLM_USAGE_OPENCODE_MONTHLY_SPENT", "20")
    monkeypatch.setenv("LLM_USAGE_KILO_BALANCE", "10")
    monkeypatch.setenv("LLM_USAGE_KILO_CURRENCY", "GBP")
    monkeypatch.setenv("PATH", "/var/empty")

    from io import StringIO
    import contextlib

    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    merged = dict(env)
    for key in (
        "LLM_USAGE_OPENCODE_BALANCE",
        "LLM_USAGE_OPENCODE_CURRENCY",
        "LLM_USAGE_OPENCODE_MONTHLY_BUDGET",
        "LLM_USAGE_OPENCODE_MONTHLY_SPENT",
        "LLM_USAGE_KILO_BALANCE",
        "LLM_USAGE_KILO_CURRENCY",
    ):
        value = os.environ.get(key)
        if value is not None:
            merged[key] = value
    snap = read_opencode(merged)
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
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_opencode_rows(cfg, json_obj)
    out = buf.getvalue()
    assert "OpenCode" in out
    assert "balance" in out
    assert "budget" in out
    assert "USD12" in out


def test_usage_table_renders_opencode_spent(env: dict[str, str], capsys, monkeypatch) -> None:
    """When the snapshot carries a spent-style BALANCE scope (cost from
    the CLI stats, no env-var balance), the renderer prefixes the value
    with ``spent`` so the table does not pretend the amount is remaining
    capacity."""
    from llm_tools import usage

    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("LLM_USAGE_DISABLE_COPILOT", "1")

    from io import StringIO
    import contextlib

    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    json_obj = {
        "provider": PROVIDER_OPENCODE,
        "available": True,
        "reason": "",
        "source": "opencode stats",
        "selected_model": None,
        "scopes": [
            {
                "name": SCOPE_BALANCE,
                "kind": CapacityKind.BALANCE,
                "remaining_percent": None,
                "remaining_amount": 0.0,
                "total_amount": None,
                "currency": "$",
                "reset_epoch": None,
                "label": None,
                "source": "opencode stats",
                "extras": {"spent": True},
            }
        ],
    }
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_opencode_rows(cfg, json_obj)
    out = buf.getvalue()
    assert "OpenCode" in out
    assert "spent $0" in out
    # A spent-cost row from an available snapshot means the provider is ready.
    assert "yes" in out


# --- Helpers ------------------------------------------------------------------


def run_cmd(args, env):
    from .conftest import run_cmd as _run_cmd
    return _run_cmd(args, env)
