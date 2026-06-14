"""Tests for ralph-robin's selection logic with mixed reset-bound and Kilo providers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llm_tools import common, ralph_robin


# --- even_burn_candidate -----------------------------------------------------


def test_even_burn_candidate_usable() -> None:
    d = {"usable": True, "reason": "usable"}
    assert ralph_robin.even_burn_candidate(d) is True


def test_even_burn_candidate_rate_limited_with_other_window() -> None:
    d = {
        "usable": False,
        "reason": "rate-limited",
        "exhausted": [{"name": "5h"}],
    }
    # 5h is exhausted but weekly is still rankable.
    assert ralph_robin.even_burn_candidate(d) is True


def test_even_burn_candidate_weekly_fully_exhausted_excluded() -> None:
    d = {
        "usable": False,
        "reason": "rate-limited",
        "exhausted": [{"name": "weekly"}],
    }
    assert ralph_robin.even_burn_candidate(d) is False


def test_even_burn_candidate_budget_exhausted_included_when_other_scope_available() -> None:
    d = {
        "usable": False,
        "reason": "budget-exhausted",
        "exhausted": [{"name": "balance"}],
    }
    # Budget is exhausted but the balance scope (not a budget) is still
    # rankable, so the provider is still a valid even-burn candidate.
    assert ralph_robin.even_burn_candidate(d) is True


# --- remaining_daily_capacity -------------------------------------------------


def test_remaining_daily_capacity_with_weekly_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    d = {
        "windows": [
            {
                "name": "weekly",
                "kind": "reset_window",
                "remaining": 80.0,
                "reset_epoch": 1000 + 4 * 86400,
            }
        ]
    }
    assert ralph_robin.remaining_daily_capacity(d, os.environ) == pytest.approx(80.0 / 4.0)


def test_remaining_daily_capacity_with_budget_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    d = {
        "windows": [
            {
                "name": "budget",
                "kind": "budget",
                "remaining": 60.0,
                "reset_epoch": 1000 + 10 * 86400,
            }
        ]
    }
    assert ralph_robin.remaining_daily_capacity(d, os.environ) == pytest.approx(60.0 / 10.0)


def test_remaining_daily_capacity_skips_balance_and_ungated() -> None:
    d = {
        "windows": [
            {"name": "balance", "kind": "balance", "remaining_amount": 100.0},
            {"name": "ungated", "kind": "ungated", "label": "byok"},
        ]
    }
    assert ralph_robin.remaining_daily_capacity(d) is None


def test_remaining_daily_capacity_picks_highest_among_known() -> None:
    d = {
        "windows": [
            {"name": "weekly", "kind": "reset_window", "remaining": 70.0, "reset_epoch": None},
            {"name": "budget", "kind": "budget", "remaining": 80.0, "reset_epoch": None},
        ]
    }
    # Both have no reset, fall back to a 7-day window: 80/7 > 70/7.
    assert ralph_robin.remaining_daily_capacity(d) == pytest.approx(80.0 / 7.0)


# --- even_burn_index: pure rotation logic ------------------------------------


def test_even_burn_index_picks_highest_pace() -> None:
    cfg = ralph_robin.RalphConfig(providers=["codex", "kilo"], even_burn=True, scope="auto")
    decisions = [
        {
            "provider": "codex",
            "usable": True,
            "reason": "usable",
            "windows": [
                {"name": "weekly", "kind": "reset_window", "remaining": 30.0, "reset_epoch": None}
            ],
        },
        {
            "provider": "kilo",
            "usable": True,
            "reason": "usable",
            "windows": [
                {"name": "budget", "kind": "budget", "remaining": 80.0, "reset_epoch": None}
            ],
        },
    ]
    idx = ralph_robin.even_burn_index(cfg, decisions, current_index=0, skipped=set())
    assert idx == 1  # kilo budget 80/7 > codex weekly 30/7


def test_even_burn_index_skips_ungated_provider() -> None:
    cfg = ralph_robin.RalphConfig(providers=["kilo", "codex"], even_burn=True, scope="auto")
    decisions = [
        {
            "provider": "kilo",
            "usable": True,
            "reason": "usable",
            "windows": [
                {"name": "ungated", "kind": "ungated", "label": "byok"},
            ],
        },
        {
            "provider": "codex",
            "usable": True,
            "reason": "usable",
            "windows": [
                {"name": "weekly", "kind": "reset_window", "remaining": 50.0, "reset_epoch": None}
            ],
        },
    ]
    # Kilo's only scope is balance/ungated, so it has no pace rank; the
    # function returns None and the caller falls back to plain rotation.
    assert ralph_robin.even_burn_index(cfg, decisions, current_index=0, skipped=set()) is None


# --- select_provider: end-to-end --------------------------------------------------


def test_select_provider_falls_back_to_kilo_when_codex_rate_limited(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # codex 5h exhausted with a future reset, codex weekly still rate-rankable
    # but Codex overall is rate-limited; Kilo has a healthy budget scope so
    # plain rotation picks it.
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.delenv("LLM_SCHEDULER_USAGE_JSON", raising=False)
    monkeypatch.setenv("LLM_USAGE_KILO_BALANCE", "10")
    monkeypatch.setenv("LLM_USAGE_KILO_MONTHLY_BUDGET", "100")
    monkeypatch.setenv("LLM_USAGE_KILO_MONTHLY_SPENT", "20")
    cfg = ralph_robin.RalphConfig(providers=["codex", "kilo"], even_burn=False, scope="auto")
    cfg.even_burn = False  # plain rotation
    logs = common.setup_run_logs(tmp_path, "test")
    selection = ralph_robin.select_provider(cfg, logs, current_index=0, skipped=set())
    assert selection["provider"] in {"codex", "kilo"}


def test_select_tool_uses_kilo_in_byok_mode(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, fake_bin: Path
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/usr/bin/env python3\nprint('mock')\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("LLM_USAGE_KILO_MODE", "byok")
    monkeypatch.setenv("LLM_USAGE_KILO_BALANCE", "5")
    cfg = ralph_robin.RalphConfig(providers=["kilo"], even_burn=False, scope="auto")
    logs = common.setup_run_logs(tmp_path, "test")
    selection = ralph_robin.select_provider(cfg, logs, current_index=0, skipped=set())
    assert selection["provider"] == "kilo"
    assert selection["decision"]["usable"] is True


def test_select_provider_marks_kilo_byok_missing_cli_unusable(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/var/empty")
    monkeypatch.setenv("LLM_USAGE_KILO_MODE", "byok")
    monkeypatch.setenv("LLM_USAGE_KILO_BALANCE", "5")
    cfg = ralph_robin.RalphConfig(providers=["kilo"], even_burn=False, scope="auto")
    logs = common.setup_run_logs(tmp_path, "test")
    selection = ralph_robin.select_provider(cfg, logs, current_index=0, skipped=set())
    assert selection["provider"] == "kilo"
    assert selection["decision"]["usable"] is False
    assert selection["decision"]["reason"] == "missing-cli"
