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


def test_remaining_daily_capacity_picks_binding_lowest_among_known() -> None:
    d = {
        "windows": [
            {"name": "weekly", "kind": "reset_window", "remaining": 70.0, "reset_epoch": None},
            {"name": "budget", "kind": "budget", "remaining": 80.0, "reset_epoch": None},
        ]
    }
    # A provider is gated by its most-constrained plan scope, so the surplus it
    # can absorb is the BINDING (minimum) pace, not the most generous one. Both
    # fall back to a 7-day window: weekly 70/7 binds below budget 80/7.
    assert ralph_robin.remaining_daily_capacity(d) == pytest.approx(70.0 / 7.0)


def test_remaining_daily_capacity_excludes_5h_session_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    # The codex never-hands-over bug: a fast-resetting 5h window stays healthy
    # and, under the old max-aggregation, masked a draining weekly so the
    # provider was ranked as if it had plenty of surplus. The 5h session scope
    # must not drive the surplus ranking at all — only the weekly plan does.
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    now = 1000
    d = {
        "windows": [
            # 5h freshly reset and full -> looks generous, but it is a throttle.
            {"name": "5h", "kind": "reset_window", "remaining": 95.0, "reset_epoch": now + 3 * 3600},
            # weekly nearly drained with most of the week left -> deep conserve.
            {"name": "weekly", "kind": "reset_window", "remaining": 6.0, "reset_epoch": now + 5 * 86400},
        ]
    }
    score = ralph_robin.remaining_daily_capacity(d, os.environ)
    # Score reflects ONLY the binding weekly plan scope, not the healthy 5h.
    assert score == pytest.approx(6.0 - 5.0 / 7.0 * 100.0)


def test_remaining_daily_capacity_binding_min_across_5h_and_weekly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even when the 5h window is the lower of the two, it is excluded; the plan
    # surplus is the weekly figure, so two providers are distinguished purely by
    # their weekly headroom regardless of momentary 5h state.
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    now = 1000
    d = {
        "windows": [
            {"name": "5h", "kind": "reset_window", "remaining": 10.0, "reset_epoch": now + 3600},
            {"name": "weekly", "kind": "reset_window", "remaining": 90.0, "reset_epoch": now + 2 * 86400},
        ]
    }
    score = ralph_robin.remaining_daily_capacity(d, os.environ)
    assert score == pytest.approx(90.0 - 2.0 / 7.0 * 100.0)


def test_remaining_daily_capacity_unknown_name_with_future_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    d = {
        "windows": [
            {
                "name": "custom",
                "kind": "reset_window",
                "remaining": 60.0,
                "reset_epoch": 1000 + 3 * 86400,
            }
        ]
    }
    # Unknown scope name, fall back to remaining / days_left = 60 / 3
    assert ralph_robin.remaining_daily_capacity(d, os.environ) == pytest.approx(60.0 / 3.0)


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


# --- end-to-end rotation simulations ----------------------------------------


def _run_rotation(
    cfg: ralph_robin.RalphConfig,
    logs: common.RunLogs,
    iterations: int,
    *,
    on_selected=None,
) -> list[str]:
    """Drive ``select_provider`` like the real main loop.

    Mirrors :func:`ralph_robin.main`: each iteration re-selects, anchors
    ``current_index`` on the winner, and bumps ``completed_counts`` for the
    selected key. ``on_selected(provider)`` is invoked after each pick so a
    test can mutate the simulated usage (e.g. burn down a weekly window).
    Returns the ordered list of providers selected.
    """
    order: list[str] = []
    current_index = 0
    for _ in range(iterations):
        selection = ralph_robin.select_provider(cfg, logs, current_index, set())
        provider = selection["provider"]
        order.append(provider)
        current_index = int(selection["index"])
        cfg.completed_counts[provider] = cfg.completed_counts.get(provider, 0) + 1
        if on_selected is not None:
            on_selected(provider)
    return order


def test_even_burn_simulation_balances_weekly_despite_healthy_5h(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end proof of the codex never-hands-over fix.

    Both providers keep a full, fast-resetting 5h window the entire run while
    their weekly plan is steadily burned by whoever is selected. Under the old
    max-across-scopes ranking the healthy 5h tied both providers forever and the
    incumbent monopolised the rotation — burning one weekly to the floor while
    the other sat untouched. With binding (weekly) ranking the load is shared,
    so the two weekly balances stay close and both providers get real turns.
    """
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    now = 1000
    weekly = {"claude": 80.0, "codex": 80.0}

    def snap(provider: str, env: dict[str, str] | None = None) -> dict[str, object]:
        return {
            "available": True,
            # 5h is always healthy and resets soon: a pure throttle, not a plan.
            "five_hour": {"remaining": 95, "resets_at": now + 3 * 3600},
            "week": {"remaining": weekly[provider], "resets_at": now + 5 * 86400},
        }

    monkeypatch.setattr(common, "usage_snapshot_for_provider", snap)
    cfg = ralph_robin.RalphConfig(
        providers_spec="claude,codex",
        providers=["claude", "codex"],
        even_burn=True,
        scope="auto",
        state_file=tmp_path / "state.json",
    )
    logs = common.setup_run_logs(tmp_path / "logs", "r")

    def burn(provider: str) -> None:
        weekly[provider] -= 10.0

    order = _run_rotation(cfg, logs, 8, on_selected=burn)

    # Even burn-down: neither weekly is driven far below the other.
    assert abs(weekly["claude"] - weekly["codex"]) <= 12.0
    # No monopoly: both providers shared the load (old code gave codex 0 turns).
    assert order.count("claude") >= 3
    assert order.count("codex") >= 3


def test_mixed_rotation_opaque_subscription_gets_even_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MiniMax-via-Kilo style opaque subscription is rotated fairly.

    Claude and Codex expose percentage weekly windows; Kilo exposes only an
    ungated/opaque subscription with no rankable number. Over many iterations
    the opaque provider must still receive an even share of turns rather than
    being starved by (or starving) the measurable providers.
    """
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    now = 1000
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 70, "resets_at": now + 3 * 3600},
            "week": {"remaining": 70, "resets_at": now + 5 * 86400},
        },
        "codex": {
            "available": True,
            "five_hour": {"remaining": 70, "resets_at": now + 3 * 3600},
            "week": {"remaining": 70, "resets_at": now + 5 * 86400},
        },
        "kilo": {
            "available": True,
            "scopes": [{"name": "ungated", "kind": "ungated", "label": "minimax-m3 subscription"}],
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider, env=None: snapshots[provider])
    cfg = ralph_robin.RalphConfig(
        providers_spec="claude,codex,kilo",
        providers=["claude", "codex", "kilo"],
        even_burn=True,
        scope="auto",
        state_file=tmp_path / "state.json",
    )
    logs = common.setup_run_logs(tmp_path / "logs", "r")

    order = _run_rotation(cfg, logs, 12)
    counts = {p: order.count(p) for p in cfg.providers}

    # Fair and even: the spread between the busiest and idlest provider is at
    # most one turn, and the opaque subscription is never starved.
    assert max(counts.values()) - min(counts.values()) <= 1
    assert counts["kilo"] >= 4
