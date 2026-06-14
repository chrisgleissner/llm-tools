"""Tests for ``common.usage_prefix_text`` and its scope-walk helpers.

The usage prefix is the small ``5h=10% week=30%`` string that ralph-robin
can stamp on every relayed provider line. It is shell-out-cheap to call
when uncached, so this module exercises the rendering for several scope
kinds: reset_window, balance (with and without currency), and ungated
(BYOK). It also checks the legacy ``five_hour``/``week`` windows are
translated into the new generic scopes.
"""

from __future__ import annotations

import json
import os

from llm_tools import common


def _inject(provider: str, payload: dict) -> dict[str, str]:
    return {"LLM_SCHEDULER_USAGE_JSON": json.dumps({provider: payload})}


def test_usage_prefix_text_renders_legacy_windows() -> None:
    snap = {
        "five_hour": {"remaining": 75, "resets_at": 1234567890},
        "week": {"remaining": 50, "resets_at": 1234567890},
    }
    env = _inject("claude", snap)
    rendered = common.usage_prefix_text("claude", env)
    # Order matches the snapshot's order; 5h=75% week=50% (using the
    # short label "week" from USAGE_PREFIX_WINDOW_LABELS).
    assert "5h=75%" in rendered
    assert "week=50%" in rendered


def test_usage_prefix_text_renders_monthly() -> None:
    snap = {"monthly": {"remaining": 36}}
    env = _inject("copilot", snap)
    rendered = common.usage_prefix_text("copilot", env)
    assert "month=36%" in rendered


def test_usage_prefix_text_renders_balance_with_currency() -> None:
    snap = {
        "scopes": [
            {
                "name": "balance",
                "kind": "balance",
                "remaining_amount": 12.40,
                "currency": "GBP",
            }
        ]
    }
    env = _inject("kilo", snap)
    rendered = common.usage_prefix_text("kilo", env)
    assert rendered == "bal=GBP12.4"


def test_usage_prefix_text_renders_balance_without_currency() -> None:
    snap = {
        "scopes": [
            {
                "name": "balance",
                "kind": "balance",
                "remaining_amount": 100,
            }
        ]
    }
    env = _inject("kilo", snap)
    rendered = common.usage_prefix_text("kilo", env)
    assert rendered == "bal=100"


def test_usage_prefix_text_renders_ungated() -> None:
    snap = {
        "scopes": [
            {"name": "byok", "kind": "ungated"},
        ]
    }
    env = _inject("kilo", snap)
    rendered = common.usage_prefix_text("kilo", env)
    # The label falls back to the scope name when no label is set.
    assert "byok=byok" in rendered


def test_usage_prefix_text_returns_empty_for_no_windows() -> None:
    snap = {"available": True}
    env = _inject("claude", snap)
    assert common.usage_prefix_text("claude", env) == ""


def test_usage_prefix_text_returns_empty_when_snapshot_has_no_windows() -> None:
    # Snapshot has windows but none survive _legacy_snapshot_to_scopes.
    snap = {"five_hour": None, "week": None}
    env = _inject("claude", snap)
    assert common.usage_prefix_text("claude", env) == ""


def test_usage_prefix_text_skips_window_without_remaining_amount() -> None:
    snap = {
        "scopes": [
            {"name": "balance", "kind": "balance", "remaining_amount": None},
        ]
    }
    env = _inject("kilo", snap)
    assert common.usage_prefix_text("kilo", env) == ""


def test_usage_prefix_text_skips_window_without_remaining_pct() -> None:
    snap = {"five_hour": {"remaining": None, "resets_at": 1234567890}}
    env = _inject("claude", snap)
    assert common.usage_prefix_text("claude", env) == ""


def test_usage_prefix_text_uses_default_known_label_for_unknown_scope() -> None:
    # Unknown scope name falls back to the raw name, not the short label.
    snap = {"five_hour": {"remaining": 90, "resets_at": 1234567890}}
    env = _inject("claude", snap)
    rendered = common.usage_prefix_text("claude", env)
    assert "5h=90%" in rendered


def test_legacy_snapshot_to_scopes_handles_monthly_only() -> None:
    # Provider snapshot where only the monthly window is present
    # (matches the Copilot shape).
    snap = {"monthly": {"remaining": 20}}
    scopes = common._legacy_snapshot_to_scopes(snap)
    assert len(scopes) == 1
    assert scopes[0]["name"] == "monthly"
    assert scopes[0]["kind"] == "reset_window"
    assert scopes[0]["remaining_percent"] == 20.0


def test_legacy_snapshot_to_scopes_skips_invalid_windows() -> None:
    snap = {
        "five_hour": "not a dict",
        "week": {"remaining": 10, "resets_at": 1234567890},
    }
    scopes = common._legacy_snapshot_to_scopes(snap)
    assert len(scopes) == 1
    assert scopes[0]["name"] == "weekly"


def test_decision_scopes_prefers_existing_scope_dicts_for_kilo() -> None:
    # Kilo snapshots already carry a "scopes" list with the generic
    # shape; _decision_scopes must reuse it instead of re-deriving from
    # the legacy wire format.
    scope = {
        "name": "balance",
        "kind": "balance",
        "remaining_amount": 50.0,
    }
    snap = {"scopes": [scope]}
    assert common._decision_scopes(snap, "kilo") == [scope]


def test_decision_scopes_uses_existing_scopes_for_opencode() -> None:
    scope = {
        "name": "budget",
        "kind": "budget",
        "remaining_percent": 70.0,
    }
    snap = {"scopes": [scope]}
    assert common._decision_scopes(snap, "opencode") == [scope]


def test_decision_scopes_uses_existing_scopes_for_minimax() -> None:
    scope = {
        "name": "5h",
        "kind": "reset_window",
        "remaining_percent": 40.0,
    }
    snap = {"scopes": [scope]}
    assert common._decision_scopes(snap, "minimax") == [scope]


def test_decision_scopes_falls_back_to_legacy_for_codex() -> None:
    snap = {
        "five_hour": {"remaining": 60, "resets_at": 1234567890},
        "week": {"remaining": 30, "resets_at": 1234567890},
    }
    scopes = common._decision_scopes(snap, "codex")
    names = {s["name"] for s in scopes}
    assert names == {"5h", "weekly"}


def test_decision_scopes_legacy_when_existing_is_empty() -> None:
    # Empty list of "scopes" must not crash and must fall through to the
    # legacy wire format translation.
    snap = {
        "scopes": [],
        "five_hour": {"remaining": 60, "resets_at": 1234567890},
    }
    scopes = common._decision_scopes(snap, "kilo")
    assert scopes[0]["name"] == "5h"


def test_decision_scopes_legacy_when_existing_is_not_list() -> None:
    snap = {
        "scopes": "not a list",
        "five_hour": {"remaining": 60, "resets_at": 1234567890},
    }
    scopes = common._decision_scopes(snap, "kilo")
    assert scopes[0]["name"] == "5h"


def test_scope_filtered_auto_returns_all() -> None:
    scopes = [{"name": "5h"}, {"name": "weekly"}]
    assert common._scope_filtered(scopes, "auto") == scopes


def test_scope_filtered_returns_only_matching() -> None:
    scopes = [{"name": "5h"}, {"name": "weekly"}]
    assert common._scope_filtered(scopes, "weekly") == [{"name": "weekly"}]


def test_scope_filtered_returns_empty_when_no_match() -> None:
    scopes = [{"name": "5h"}]
    assert common._scope_filtered(scopes, "balance") == []
