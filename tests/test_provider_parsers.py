"""Unit tests for the pure parsing/normalization helpers in the new
provider adapters (Kilo, OpenCode, MiniMax).

These functions translate raw CLI output (JSON or TUI text) and env-var
overrides into the small, stable vocabulary the capacity model consumes.
They are pure and deterministic, so they are exercised here directly rather
than through a fake CLI subprocess.
"""

from __future__ import annotations

import pytest

from llm_tools.providers import kilo, minimax
from llm_tools.providers import opencode as oc


# --- _parse_balance (shared shape across Kilo and OpenCode) -------------------


@pytest.mark.parametrize("module", [kilo, oc])
def test_parse_balance_handles_all_inputs(module) -> None:
    pb = module._parse_balance
    assert pb(None) is None
    assert pb("") is None
    assert pb(True) is None  # bool is not a number here
    assert pb(False) is None
    assert pb(5) == 5.0
    assert pb(5.5) == 5.5
    assert pb("12.40") == 12.40
    assert pb("£12.40") == 12.40  # leading currency symbol stripped
    assert pb("$0") == 0.0
    assert pb("not-a-number") is None


def test_opencode_parse_balance_strips_thousands_separator() -> None:
    # OpenCode's parser additionally collapses a thousands separator.
    assert oc._parse_balance("1,234.50") == 1234.50


# --- _first ------------------------------------------------------------------


@pytest.mark.parametrize("module", [kilo, oc])
def test_first_returns_first_present_non_empty(module) -> None:
    first = module._first
    assert first({"a": None, "b": "", "c": 3}, ("a", "b", "c")) == 3
    assert first({"a": 1}, ("a",)) == 1
    assert first({}, ("missing",)) is None


# --- stats payload parsers ---------------------------------------------------


def test_kilo_parse_stats_payload() -> None:
    assert kilo._parse_kilo_stats_payload(None) is None
    assert kilo._parse_kilo_stats_payload([]) is None
    assert kilo._parse_kilo_stats_payload({}) is None  # no known keys
    out = kilo._parse_kilo_stats_payload(
        {"credits": "25.5", "unit": "$", "monthly_budget": 50, "budget_used": 10}
    )
    assert out == {"balance": 25.5, "currency": "$", "budget": 50.0, "spent": 10.0}


def test_opencode_parse_stats_payload() -> None:
    assert oc._parse_opencode_stats_payload(None) is None
    assert oc._parse_opencode_stats_payload("text") is None
    assert oc._parse_opencode_stats_payload({}) is None
    out = oc._parse_opencode_stats_payload(
        {"total_cost": "7.50", "currency": "USD", "monthly_budget": "100", "budget_used": "30"}
    )
    assert out == {"cost": 7.50, "currency": "USD", "budget": 100.0, "spent": 30.0}


# --- stats text parsers ------------------------------------------------------


def test_kilo_parse_stats_text_empty_and_balance_line() -> None:
    assert kilo._parse_kilo_stats_text("") is None
    out = kilo._parse_kilo_stats_text("balance: 12.5 USD\nbudget: 50\nspent: 5\n")
    assert out is not None
    assert out["balance"] == 12.5
    assert out["currency"] == "USD"
    assert out["budget"] == 50.0
    assert out["spent"] == 5.0


def test_opencode_parse_stats_text_tui_and_fallback() -> None:
    assert oc._parse_opencode_stats_text("") is None
    # TUI box-drawing layout with a prefix currency symbol.
    tui = "│Total Cost                  $7.50│\n│Sessions                       10│\n"
    out = oc._parse_opencode_stats_text(tui)
    assert out is not None
    assert out["cost"] == 7.50
    assert out["currency"] == "$"
    # Fallback "key: value" layout (covers the tolerant line parser).
    out2 = oc._parse_opencode_stats_text("balance: 9.99 USD\nmonthly-budget: 40\n")
    assert out2 is not None
    assert out2["balance"] == 9.99
    assert out2["currency"] == "USD"
    assert out2["budget"] == 40.0


# --- monthly reset epoch (December rollover + bad input) ----------------------


@pytest.mark.parametrize(
    "module, day_var, now_epoch",
    [
        (kilo, "LLM_USAGE_KILO_MONTHLY_RESET_DAY", "1765584000"),  # 2025-12-13 UTC
        (oc, "LLM_USAGE_OPENCODE_MONTHLY_RESET_DAY", "1765584000"),
    ],
)
def test_monthly_reset_epoch_december_rollover(module, day_var, now_epoch) -> None:
    reset = module.kilo_monthly_reset_epoch if module is kilo else module.opencode_monthly_reset_epoch
    env = {"LLM_USAGE_NOW_EPOCH": now_epoch, day_var: "1"}
    # The 1st has already passed in December, so the next reset rolls into
    # the following January (year + 1).
    assert reset(env) > int(now_epoch)


@pytest.mark.parametrize(
    "module, day_var",
    [
        (kilo, "LLM_USAGE_KILO_MONTHLY_RESET_DAY"),
        (oc, "LLM_USAGE_OPENCODE_MONTHLY_RESET_DAY"),
    ],
)
def test_monthly_reset_epoch_bad_day_falls_back(module, day_var) -> None:
    reset = module.kilo_monthly_reset_epoch if module is kilo else module.opencode_monthly_reset_epoch
    env = {"LLM_USAGE_NOW_EPOCH": "1780272000", day_var: "not-an-int"}
    assert isinstance(reset(env), int)


# --- MiniMax helpers ---------------------------------------------------------


def test_minimax_safe_int_and_float() -> None:
    assert minimax._safe_int(None) is None
    assert minimax._safe_int("") is None
    assert minimax._safe_int(True) is None
    assert minimax._safe_int("7") == 7
    assert minimax._safe_int("x") is None
    assert minimax._safe_float(None) is None
    assert minimax._safe_float(True) is None
    assert minimax._safe_float("3.5") == 3.5
    assert minimax._safe_float("x") is None


def test_minimax_epoch_seconds_normalizes_milliseconds() -> None:
    assert minimax._epoch_seconds(None) is None
    assert minimax._epoch_seconds(1780000000) == 1780000000  # already seconds
    assert minimax._epoch_seconds(1780000000000) == 1780000000  # ms -> s


def test_minimax_row_for_model_and_payload() -> None:
    assert minimax._row_for_model({}, "general") is None
    assert minimax._row_for_model({"model_remains": "x"}, "general") is None
    payload = {
        "model_remains": [
            {"model_name": "video", "current_interval_remaining_percent": 10},
            {
                "model_name": "general",
                "current_interval_remaining_percent": 80,
                "current_weekly_remaining_percent": 55,
                "end_time": 1780000000000,
                "weekly_end_time": 1780500000000,
            },
        ]
    }
    row = minimax._row_for_model(payload, "general")
    assert row is not None and row["model_name"] == "general"
    parsed = minimax._parse_minimax_payload(payload, "general")
    assert parsed == {
        "interval_percent": 80.0,
        "weekly_percent": 55.0,
        "interval_reset_ms": 1780000000000,
        "weekly_reset_ms": 1780500000000,
    }
    # Non-dict payload and missing model both yield None.
    assert minimax._parse_minimax_payload(["x"], "general") is None
    assert minimax._parse_minimax_payload({"model_remains": []}, "general") is None


def test_minimax_parse_payload_clamps_percentages() -> None:
    payload = {
        "model_remains": [
            {"model_name": "general", "current_interval_remaining_percent": 150, "current_weekly_remaining_percent": -5},
        ]
    }
    parsed = minimax._parse_minimax_payload(payload, "general")
    assert parsed["interval_percent"] == 100.0
    assert parsed["weekly_percent"] == 0.0


def test_minimax_build_scopes_skips_missing_windows() -> None:
    assert minimax._build_scopes(None, None, None, None, "src") == []
    scopes = minimax._build_scopes(80.0, 1780000000, None, None, "src")
    assert [s.name for s in scopes] == ["5h"]
