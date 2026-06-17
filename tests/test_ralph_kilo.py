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
    # pace deviation: 80% − (4/7 × 100%) ≈ +22.86 (headroom)
    assert ralph_robin.remaining_daily_capacity(d, os.environ) == pytest.approx(80.0 - 4.0 / 7.0 * 100.0)


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
    # pace deviation: 60% − (10/30 × 100%) ≈ +26.67 (headroom; 30-day budget period)
    assert ralph_robin.remaining_daily_capacity(d, os.environ) == pytest.approx(60.0 - 10.0 / 30.0 * 100.0)


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
    # Both use fallback (no reset_epoch): kilo 80/7 > codex 30/7
    assert idx == 1


def test_even_burn_index_prefers_on_pace_over_conserve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider that is on-pace beats a provider that is flagged conserve.

    Real-world case: Claude weekly at 4% with 13h 23m left (= on pace, delta ≈ −4%)
    vs Codex weekly at 92% with 6d 22h left (↓ conserve, delta ≈ −7%).
    The pace deviation for Claude is higher so ralph picks Claude.
    """
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    now = 1000
    claude_reset = now + int((13 * 3600 + 23 * 60))   # ≈ 0.558 days
    codex_reset = now + int((6 * 86400 + 22 * 3600))  # ≈ 6.917 days

    cfg = ralph_robin.RalphConfig(providers=["claude", "codex"], even_burn=True, scope="auto")
    decisions = [
        {
            "provider": "claude",
            "usable": True,
            "reason": "usable",
            "windows": [
                {"name": "weekly", "kind": "reset_window", "remaining": 4.0, "reset_epoch": claude_reset},
            ],
        },
        {
            "provider": "codex",
            "usable": True,
            "reason": "usable",
            "windows": [
                {"name": "weekly", "kind": "reset_window", "remaining": 92.0, "reset_epoch": codex_reset},
            ],
        },
    ]
    idx = ralph_robin.even_burn_index(cfg, decisions, current_index=0, skipped=set())
    # Claude: 4 − (0.558/7×100) ≈ −4.0% (on pace); Codex: 92 − (6.917/7×100) ≈ −6.8% (conserve)
    # Claude has higher pace deviation → idx 0
    assert idx == 0


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


def test_mixed_capacity_fair_index_edge_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ralph_robin.mixed_capacity_fair_index([], [], 0, set(), None, lambda d: d.get("score")) is None
    assert ralph_robin._count_for({"bad": "x"}, "bad") == 0

    decisions = [
        {"id": "a", "usable": True, "score": 10.0},
        {"id": "b", "usable": True, "score": 5.0},
        {"id": "opaque", "usable": True, "score": None},
    ]
    calls: dict[str, int] = {}

    def capacity(decision: dict[str, object]) -> float | None:
        key = str(decision["id"])
        calls[key] = calls.get(key, 0) + 1
        if key == "a" and calls[key] > 1:
            return None
        value = decision.get("score")
        return float(value) if isinstance(value, float) else None

    assert ralph_robin.mixed_capacity_fair_index(decisions, ["a", "b", "opaque"], 0, set(), {}, capacity) == 1

    calls.clear()

    def capacity_disappears(decision: dict[str, object]) -> float | None:
        key = str(decision["id"])
        calls[key] = calls.get(key, 0) + 1
        if key in {"a", "b"} and calls[key] > 1:
            return None
        value = decision.get("score")
        return float(value) if isinstance(value, float) else None

    assert (
        ralph_robin.mixed_capacity_fair_index(decisions, ["a", "b", "opaque"], 0, set(), {}, capacity_disappears)
        == 0
    )

    monkeypatch.setattr(ralph_robin, "rotation_order_indices", lambda length, current_index: [])
    assert (
        ralph_robin.mixed_capacity_fair_index(
            [{"id": "a", "usable": True, "score": 1.0}, {"id": "opaque", "usable": True, "score": None}],
            ["a", "opaque"],
            0,
            set(),
            {},
            lambda d: d.get("score"),
        )
        == 0
    )


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
    selection = ralph_robin.select_provider(
        cfg,
        logs,
        current_index=0,
        skipped=set(),
        completed_counts=cfg.completed_counts,
    )
    assert selection["provider"] in {"codex", "kilo"}


def test_select_provider_mixed_capacity_fair_rotation_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    cfg = ralph_robin.RalphConfig(
        providers=["claude", "kilo", "codex"],
        even_burn=True,
        scope="auto",
        completed_counts={"claude": 1, "kilo": 0, "codex": 1},
    )
    logs = common.setup_run_logs(tmp_path, "test")
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 80},
            "week": {"remaining": 80, "resets_at": 1000 + 4 * 86400},
        },
        "kilo": {
            "available": True,
            "scopes": [{"name": "ungated", "kind": "ungated", "label": "byok"}],
        },
        "codex": {
            "available": True,
            "five_hour": {"remaining": 40},
            "week": {"remaining": 40, "resets_at": 1000 + 4 * 86400},
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider, env=None: snapshots[provider])

    selection = ralph_robin.select_provider(cfg, logs, current_index=0, skipped=set())
    assert selection["provider"] == "kilo"
    assert selection["rotation_reason"] == "fair-rotation"


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
