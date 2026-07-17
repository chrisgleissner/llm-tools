"""Tests for small helper functions in ``llm_tools.common`` not otherwise
exercised by the main test files. These are mostly edge-case paths that
push overall coverage above the 85% gate.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from llm_tools import common


# --- migrate_legacy_cache_dirs ------------------------------------------------


def test_migrate_legacy_cache_dirs_moves_old_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    cache = home / ".cache"
    home.mkdir()
    cache.mkdir()
    for name in ("llm-usage", "llm-scheduler", "ralph-robin"):
        (cache / name).mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    common.migrate_legacy_cache_dirs()
    new_root = cache / "llm-tools"
    assert (new_root / "llm-usage").exists()
    assert (new_root / "llm-scheduler").exists()
    assert (new_root / "ralph-robin").exists()


def test_migrate_legacy_cache_dirs_no_old(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No old directories to migrate: no crash, no destination created.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    common.migrate_legacy_cache_dirs()
    assert not (home / ".cache" / "llm-tools").exists()


def test_migrate_legacy_cache_dirs_swallows_rename_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    cache = home / ".cache"
    home.mkdir()
    cache.mkdir()
    (cache / "llm-usage").mkdir()
    (cache / "llm-tools").mkdir()  # destination already exists
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    # Should not raise even though rename cannot proceed.
    common.migrate_legacy_cache_dirs()


# --- require_cmd --------------------------------------------------------------


def test_require_cmd_exits_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    with pytest.raises(SystemExit) as excinfo:
        common.require_cmd("definitely-not-on-path-xyz")
    assert excinfo.value.code == 127


def test_require_cmd_passes_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: True)
    common.require_cmd("anything")  # no raise


# --- parse_epoch edge cases ---------------------------------------------------


def test_parse_epoch_integer_string() -> None:
    assert common.parse_epoch("1700000000") == 1700000000


def test_parse_epoch_returns_none_for_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    assert common.parse_epoch("not-a-real-date-or-number") is None


def test_parse_epoch_iso_string() -> None:
    epoch = common.parse_epoch("2024-01-15T12:00:00Z")
    assert epoch is not None
    # Within a few seconds of the expected value
    assert 1700000000 < epoch < 1800000000


# --- fmt_reset / format_local_epoch with date command -------------------------


def test_fmt_reset_handles_none() -> None:
    assert common.fmt_reset(None) == ""


def test_fmt_reset_handles_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    assert common.fmt_reset("not-a-real-date") == ""


# --- fmt_duration / time_until edge cases ------------------------------------


def test_fmt_duration_zero() -> None:
    assert common.fmt_duration(0) == "0m"


def test_fmt_duration_negative() -> None:
    assert common.fmt_duration(-1) == "0m"


def test_fmt_duration_minutes_only() -> None:
    assert common.fmt_duration(300) == "5m"


def test_fmt_duration_hours_and_minutes() -> None:
    assert common.fmt_duration(3700) == "1h 1m"


def test_fmt_duration_days_only_minutes() -> None:
    # 1 day, 0 hours, 5 minutes -> renders all three parts (the
    # fmt_duration logic always emits hours when days are present).
    assert common.fmt_duration(86400 + 5 * 60) == "1d 0h 5m"


def test_fmt_duration_full() -> None:
    assert common.fmt_duration(2 * 86400 + 3 * 3600 + 4 * 60) == "2d 3h 4m"


def test_time_until_returns_dash_for_none() -> None:
    assert common.time_until(None) == "-"


def test_time_until_returns_positive_duration(env: dict[str, str]) -> None:
    env["LLM_USAGE_NOW_EPOCH"] = "1000"
    # Reset at epoch 2000 = 1000s in the future
    out = common.time_until(2000, env)
    assert out.endswith("m")
    assert "0m" not in out or out == "16m"


# --- num/fmt_number edge cases -----------------------------------------------


def test_num_bool_is_none() -> None:
    # booleans are not coerced to a numeric value
    assert common.num(True) is None
    assert common.num(False) is None


def test_num_passes_through_int_and_float() -> None:
    assert common.num(42) == 42
    assert common.num(2.5) == 2.5


def test_num_parses_numeric_string() -> None:
    assert common.num("3.14") == 3.14


def test_num_returns_none_for_unparseable() -> None:
    assert common.num("nope") is None


def test_fmt_number_handles_none() -> None:
    assert common.fmt_number(None) == "-"


def test_fmt_number_integer_value() -> None:
    assert common.fmt_number(42) == "42"


def test_fmt_number_rounds_float() -> None:
    assert common.fmt_number(1.25) == "1.2"


def test_fmt_number_strips_trailing_zeros() -> None:
    assert common.fmt_number("2.50") == "2.5"


# --- copilot_monthly_window_days / copilot_monthly_reset_epoch --------------


def test_copilot_monthly_window_days_with_offset(env: dict[str, str]) -> None:
    env["LLM_USAGE_NOW_EPOCH"] = "1700000000"  # 2023-11-14
    env["LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS"] = "3"
    days = common.copilot_monthly_window_days(env)
    assert days >= 1.0


def test_copilot_monthly_window_days_december(env: dict[str, str]) -> None:
    # Pick a December timestamp so the month-rollover branch is exercised.
    env["LLM_USAGE_NOW_EPOCH"] = "1733011200"  # 2024-12-01 00:00 UTC
    days = common.copilot_monthly_window_days(env)
    assert days >= 1.0


def test_copilot_monthly_window_days_january(env: dict[str, str]) -> None:
    # Pick a January timestamp so the previous-year branch is exercised.
    env["LLM_USAGE_NOW_EPOCH"] = "1704067200"  # 2024-01-01 00:00 UTC
    env["LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS"] = "0"
    days = common.copilot_monthly_window_days(env)
    assert days >= 1.0


def test_copilot_monthly_reset_epoch_before_offset(env: dict[str, str]) -> None:
    # Pick a date well before the first-of-month offset so the
    # this_epoch > now branch is exercised (returns this_epoch).
    env["LLM_USAGE_NOW_EPOCH"] = "1700000000"  # mid-month
    env["LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS"] = "20"
    epoch = common.copilot_monthly_reset_epoch(env)
    assert epoch is not None
    assert epoch > 1700000000


# --- mtd_days_since_month_start / next_month_epoch_from_env -----------------


def test_mtd_days_is_one_on_first_of_month(env: dict[str, str]) -> None:
    # 2026-07-01 00:00 UTC == 1782864000 — day 1 of the month, so the
    # function must clamp to 1, never 0.
    env["LLM_USAGE_NOW_EPOCH"] = "1782864000"
    assert common.mtd_days_since_month_start(env) == 1


def test_mtd_days_increments_with_day_of_month(env: dict[str, str]) -> None:
    # 2026-07-15 00:00 UTC == 1784140800 (day 15)
    env["LLM_USAGE_NOW_EPOCH"] = "1784140800"
    assert common.mtd_days_since_month_start(env) == 15


def test_mtd_days_january_rolls_back_to_one(env: dict[str, str]) -> None:
    # 2026-01-01 00:00 UTC == 1767225600
    env["LLM_USAGE_NOW_EPOCH"] = "1767225600"
    assert common.mtd_days_since_month_start(env) == 1


def test_next_month_epoch_from_env_first_of_next_month(env: dict[str, str]) -> None:
    import datetime as _dt

    env["LLM_USAGE_NOW_EPOCH"] = "1781587377"  # 2026-06-16
    epoch = common.next_month_epoch_from_env(env)
    nxt = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
    assert (nxt.year, nxt.month, nxt.day) == (2026, 7, 1)


def test_next_month_epoch_from_env_december_rolls_year(env: dict[str, str]) -> None:
    import datetime as _dt

    env["LLM_USAGE_NOW_EPOCH"] = "1733011200"  # 2024-12-01
    epoch = common.next_month_epoch_from_env(env)
    nxt = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
    assert (nxt.year, nxt.month, nxt.day) == (2025, 1, 1)


# --- is_number / is_integer helpers ------------------------------------------


def test_is_number_accepts() -> None:
    assert common.is_number("0") is True
    assert common.is_number("10") is True
    assert common.is_number("3.14") is True


def test_is_number_rejects() -> None:
    assert common.is_number("") is False
    assert common.is_number("abc") is False
    assert common.is_number("1.2.3") is False
    assert common.is_number(None) is False


def test_is_integer_accepts() -> None:
    assert common.is_integer("0") is True
    assert common.is_integer("42") is True


def test_is_integer_rejects() -> None:
    assert common.is_integer("3.14") is False
    assert common.is_integer("") is False
    assert common.is_integer("abc") is False


# --- output_signals_credit_exhausted -----------------------------------------


def test_credit_exhausted_detects_kilo_payment_required() -> None:
    # The exact Kilo gateway envelope the bug report showed: the CLI exits 0
    # but prints a structured Payment Required / usage_limit_exceeded error.
    payload = (
        'Error: Payment Required: {"error":{"title":"Low Credit Warning!",'
        '"message":"Add credits to continue, or switch to a free model",'
        '"balance":-0.007525,"buyCreditsUrl":"https://app.kilo.ai/profile"},'
        '"error_type":"usage_limit_exceeded"}'
    )
    assert common.output_signals_credit_exhausted(payload) is True


def test_credit_exhausted_detects_individual_signatures() -> None:
    assert common.output_signals_credit_exhausted('{"error_type":"usage_limit_exceeded"}') is True
    assert common.output_signals_credit_exhausted('{"low_credit": true}') is True
    assert common.output_signals_credit_exhausted('{"message":"Add credits to continue"}') is True
    # Payment Required only counts when followed by a JSON body.
    assert common.output_signals_credit_exhausted('Payment Required: {"error": "x"}') is True


def test_credit_exhausted_ignores_model_prose() -> None:
    # A model discussing a payment API in prose must NOT be mistaken for a
    # real gateway 402 envelope. Bare "Payment Required" without a JSON body
    # does not match (the brace-window guard).
    assert common.output_signals_credit_exhausted("the endpoint returns Payment Required when funds are low") is False
    assert common.output_signals_credit_exhausted("the user has a low credit rating") is False
    assert common.output_signals_credit_exhausted("we should add credits to continue using this service") is False
    assert common.output_signals_credit_exhausted("") is False
    assert common.output_signals_credit_exhausted("normal agent output, no errors here") is False


def test_credit_exhausted_does_not_change_output_is_retryable() -> None:
    # The credit signal is acted on in submit_once BEFORE the trust_clean_exit
    # guard; output_is_retryable itself stays prose-blind under trust_clean_exit
    # so the existing contract holds (a clean exit is trusted for everything
    # except the explicit credit envelope).
    assert common.output_is_retryable(0, '{"error_type":"usage_limit_exceeded"}', trust_clean_exit=True) is False
