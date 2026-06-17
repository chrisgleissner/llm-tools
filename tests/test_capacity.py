"""Tests for the generic capacity-scope abstraction."""

from __future__ import annotations

import os

import pytest

from llm_tools import capacity
from llm_tools.capacity import (
    CapacityKind,
    CapacityScope,
    ProviderSnapshot,
    SCOPE_5H,
    SCOPE_AUTO,
    SCOPE_BALANCE,
    SCOPE_BUDGET,
    SCOPE_BYOK,
    SCOPE_MONTHLY,
    SCOPE_UNGATED,
    SCOPE_WEEKLY,
    UsageDecision,
    decide,
    effective_scopes,
    is_undetermined_reason,
    scope_pace,
    validate_scope,
    valid_scopes_for_provider,
)


def _snap(*scopes: CapacityScope, available: bool = True, reason: str = "") -> ProviderSnapshot:
    return ProviderSnapshot(provider="test", available=available, reason=reason, scopes=list(scopes))


def test_validate_scope_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="invalid --scope"):
        validate_scope("codex", "biweekly")


def test_validate_scope_rejects_unsupported_provider_scope() -> None:
    with pytest.raises(ValueError, match="not valid for copilot"):
        validate_scope("copilot", "weekly")
    with pytest.raises(ValueError, match="not valid for kilo"):
        validate_scope("kilo", "5h")
    with pytest.raises(ValueError, match="not valid for codex"):
        validate_scope("codex", "balance")
    with pytest.raises(ValueError, match="not valid for minimax"):
        validate_scope("minimax", "balance")


def test_validate_scope_accepts_provider_specific() -> None:
    assert validate_scope("codex", "auto") == "auto"
    assert validate_scope("codex", "5h") == "5h"
    assert validate_scope("kilo", "auto") == "auto"
    assert validate_scope("kilo", "balance") == "balance"
    assert validate_scope("kilo", "budget") == "budget"
    assert validate_scope("kilo", "byok") == "byok"
    assert validate_scope("kilo", "ungated") == "ungated"
    assert validate_scope("minimax", "auto") == "auto"
    assert validate_scope("minimax", "5h") == "5h"
    assert validate_scope("minimax", "weekly") == "weekly"


def test_valid_scopes_for_provider() -> None:
    assert SCOPE_5H in valid_scopes_for_provider("codex")
    assert SCOPE_WEEKLY in valid_scopes_for_provider("codex")
    assert SCOPE_MONTHLY not in valid_scopes_for_provider("codex")
    assert SCOPE_MONTHLY in valid_scopes_for_provider("copilot")
    assert SCOPE_BALANCE in valid_scopes_for_provider("kilo")
    assert SCOPE_BUDGET in valid_scopes_for_provider("kilo")
    assert SCOPE_5H in valid_scopes_for_provider("minimax")
    assert SCOPE_WEEKLY in valid_scopes_for_provider("minimax")
    assert SCOPE_BALANCE not in valid_scopes_for_provider("minimax")


def test_effective_scopes_auto_returns_all() -> None:
    snap = _snap(
        CapacityScope(name="5h", kind=CapacityKind.RESET_WINDOW, remaining_percent=50.0),
        CapacityScope(name="weekly", kind=CapacityKind.RESET_WINDOW, remaining_percent=80.0),
    )
    out = effective_scopes(snap, SCOPE_AUTO)
    assert [s.name for s in out] == ["5h", "weekly"]


def test_effective_scopes_filters_by_name() -> None:
    snap = _snap(
        CapacityScope(name="5h", kind=CapacityKind.RESET_WINDOW, remaining_percent=50.0),
        CapacityScope(name="weekly", kind=CapacityKind.RESET_WINDOW, remaining_percent=80.0),
    )
    out = effective_scopes(snap, "weekly")
    assert [s.name for s in out] == ["weekly"]


def test_decide_unavailable_snapshot() -> None:
    snap = ProviderSnapshot(provider="x", available=False, reason="not-authenticated")
    d = decide(snap, "auto", 1.0, 1.0, 60)
    assert d.usable is False
    assert d.reason == "not-authenticated"
    assert d.wait_until is not None


def test_decide_unavailable_snapshot_vetoes_even_with_scopes() -> None:
    snap = _snap(
        CapacityScope(name="ungated", kind=CapacityKind.UNGATED, label="byok"),
        available=False,
        reason="missing-cli",
    )
    d = decide(snap, "auto", 1.0, 1.0, 60)
    assert d.usable is False
    assert d.reason == "missing-cli"
    assert [scope.name for scope in d.scopes] == ["ungated"]


def test_decide_missing_cli() -> None:
    snap = _snap(
        CapacityScope(name="balance", kind=CapacityKind.BALANCE, remaining_amount=10.0, currency="GBP")
    )
    d = decide(snap, "auto", 1.0, 1.0, 60, cli_present=False)
    assert d.reason == "missing-cli"


def test_decide_reset_window_blocked_by_minimum() -> None:
    snap = _snap(
        CapacityScope(
            name="5h", kind=CapacityKind.RESET_WINDOW, remaining_percent=0.0, reset_epoch=2_000
        )
    )
    d = decide(snap, "auto", 1.0, 1.0, 60, env={"LLM_USAGE_NOW_EPOCH": "1000"})
    assert d.usable is False
    assert d.reason == "rate-limited"
    assert d.wait_until == 2000


def test_decide_reset_window_past_reset_is_usable() -> None:
    snap = _snap(
        CapacityScope(
            name="5h", kind=CapacityKind.RESET_WINDOW, remaining_percent=0.0, reset_epoch=500
        )
    )
    d = decide(snap, "auto", 1.0, 1.0, 60, env={"LLM_USAGE_NOW_EPOCH": "1000"})
    assert d.usable is True


def test_decide_balance_below_minimum_is_insufficient() -> None:
    snap = _snap(
        CapacityScope(name="balance", kind=CapacityKind.BALANCE, remaining_amount=0.0, currency="GBP")
    )
    d = decide(snap, "auto", 1.0, 1.0, 60)
    assert d.usable is False
    assert d.reason == "insufficient-balance"


def test_decide_balance_above_minimum_is_usable() -> None:
    snap = _snap(
        CapacityScope(name="balance", kind=CapacityKind.BALANCE, remaining_amount=12.5, currency="GBP")
    )
    d = decide(snap, "auto", 1.0, 5.0, 60)
    assert d.usable is True


def test_decide_balance_inconclusive_when_missing_amount() -> None:
    snap = _snap(CapacityScope(name="balance", kind=CapacityKind.BALANCE))
    d = decide(snap, "auto", 1.0, 1.0, 60)
    assert d.usable is False
    assert d.reason == "inconclusive-usage"


def test_decide_budget_below_minimum_with_known_reset() -> None:
    snap = _snap(
        CapacityScope(
            name="budget",
            kind=CapacityKind.BUDGET,
            remaining_percent=0.0,
            reset_epoch=9_999_999,
        )
    )
    d = decide(snap, "auto", 1.0, 1.0, 60, env={"LLM_USAGE_NOW_EPOCH": "1000"})
    assert d.usable is False
    assert d.reason == "budget-exhausted"
    assert d.wait_until == 9_999_999


def test_decide_budget_below_minimum_without_reset_polls() -> None:
    snap = _snap(
        CapacityScope(name="budget", kind=CapacityKind.BUDGET, remaining_percent=0.0)
    )
    d = decide(snap, "auto", 1.0, 1.0, 60, env={"LLM_USAGE_NOW_EPOCH": "1000"})
    assert d.usable is False
    assert d.reason == "budget-exhausted"
    assert d.wait_until == 1060


def test_decide_ungated_is_usable_without_data() -> None:
    snap = _snap(CapacityScope(name="ungated", kind=CapacityKind.UNGATED, label="byok"))
    d = decide(snap, "auto", 1.0, 1.0, 60)
    assert d.usable is True


def test_decide_unknown_scope_only_returns_inconclusive() -> None:
    snap = _snap(CapacityScope(name="usage", kind=CapacityKind.UNKNOWN))
    d = decide(snap, "auto", 1.0, 1.0, 60)
    assert d.usable is False
    assert d.reason == "inconclusive-usage"


def test_decide_unsupported_scope_value() -> None:
    snap = _snap(
        CapacityScope(name="balance", kind=CapacityKind.BALANCE, remaining_amount=10.0)
    )
    d = decide(snap, "weekly", 1.0, 1.0, 60)
    # No weekly scope on the snapshot → unsupported-scope.
    assert d.usable is False
    assert d.reason == "unsupported-scope"


def test_decide_auto_balance_and_budget_both_required() -> None:
    snap = _snap(
        CapacityScope(
            name="budget",
            kind=CapacityKind.BUDGET,
            remaining_percent=80.0,
            reset_epoch=2_000,
        ),
        CapacityScope(name="balance", kind=CapacityKind.BALANCE, remaining_amount=0.5),
    )
    # Balance below minimum makes the combined decision insufficient even
    # though the budget is fine.
    d = decide(snap, "auto", 1.0, 1.0, 60, env={"LLM_USAGE_NOW_EPOCH": "1000"})
    assert d.usable is False
    assert d.reason == "insufficient-balance"


def test_decide_auto_single_blocked_scope_uses_scope_reason() -> None:
    snap = _snap(
        CapacityScope(
            name="5h", kind=CapacityKind.RESET_WINDOW, remaining_percent=None, reset_epoch=2000
        )
    )
    d = decide(snap, "auto", 1.0, 1.0, 60, env={"LLM_USAGE_NOW_EPOCH": "1000"})
    assert d.reason == "inconclusive-usage"


def test_decide_mixed_blocked_kinds_reports_most_specific() -> None:
    snap = _snap(
        CapacityScope(
            name="5h", kind=CapacityKind.RESET_WINDOW, remaining_percent=0.0, reset_epoch=2_000
        ),
        CapacityScope(name="balance", kind=CapacityKind.BALANCE, remaining_amount=0.0),
    )
    d = decide(snap, "auto", 1.0, 1.0, 60, env={"LLM_USAGE_NOW_EPOCH": "1000"})
    # Balance is the most specific (insufficient-balance beats rate-limited).
    assert d.reason == "insufficient-balance"


def test_scope_pace_reset_window() -> None:
    s = CapacityScope(
        name="weekly", kind=CapacityKind.RESET_WINDOW, remaining_percent=80.0, reset_epoch=1000 + 4 * 86400
    )
    # pace deviation: 80% − (4/7 × 100%) ≈ +22.86 (headroom)
    assert scope_pace(s, 1000) == pytest.approx(80.0 - 4.0 / 7.0 * 100.0)


def test_scope_pace_unknown_reset_falls_back_to_week() -> None:
    s = CapacityScope(name="weekly", kind=CapacityKind.RESET_WINDOW, remaining_percent=50.0)
    assert scope_pace(s, 1000) == 50.0 / 7.0


def test_scope_pace_balance_and_ungated_return_none() -> None:
    assert scope_pace(CapacityScope(name="balance", kind=CapacityKind.BALANCE, remaining_amount=10.0), 1000) is None
    assert scope_pace(CapacityScope(name="ungated", kind=CapacityKind.UNGATED), 1000) is None
    assert scope_pace(CapacityScope(name="x", kind=CapacityKind.UNKNOWN), 1000) is None


def test_is_undetermined_reason() -> None:
    assert is_undetermined_reason("inconclusive-usage") is True
    assert is_undetermined_reason("unavailable") is True
    assert is_undetermined_reason("rate-limited") is False
    assert is_undetermined_reason("budget-exhausted") is False
    assert is_undetermined_reason("insufficient-balance") is False
