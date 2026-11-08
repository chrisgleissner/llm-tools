"""The F8 Usage view and the controller's capacity observation describe the same capacity (CLIUX-005)."""
from __future__ import annotations

import json
from typing import Any

import pytest

from llm_tools.bridge_usage import collect_usage

INJECTED = {"claude": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}}


@pytest.fixture
def hermetic_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "LLM_USAGE_NO_SERVICE": "1", "LLM_USAGE_LIVE_FETCH_RETRIES": "0", "LLM_USAGE_DISABLE_COPILOT": "1",
        "LLM_USAGE_DISABLE_CODEX_APP_SERVER": "1", "LLM_USAGE_DISABLE_COPILOT_ADDON": "1",
        "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "1", "LLM_USAGE_NO_COLOR": "1",
    }.items():
        monkeypatch.setenv(name, value)


def _by_provider(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in data["rows"]:
        grouped.setdefault(str(row["provider"]).lower(), []).append(row)
    return grouped


def test_embedded_read_uses_the_capacity_the_controller_observes(hermetic_usage: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The controller reads providers through `usage_snapshot_for_provider`, which honours LLM_SCHEDULER_USAGE_JSON.

    The embedded dashboard read its own provider readers and ignored the injection, so it said `unavailable`
    for a provider the controller saw at 80%.
    """
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", json.dumps(INJECTED))
    claude = _by_provider(collect_usage())["claude"]
    assert {row["scope"]: row["remaining_percent"] for row in claude} == {"5h": 80, "weekly": 80}


def _provider_data() -> dict[str, Any]:
    from llm_tools import usage
    from llm_tools.capacity import CapacityKind, CapacityScope

    def scope(name: str, percent: float) -> CapacityScope:
        return CapacityScope(name=name, kind=CapacityKind.RESET_WINDOW, remaining_percent=percent, reset_epoch=2000000000, resets_at="2000000000")

    claude = usage.ProviderSnapshot(provider="claude", available=True, source="fixture", scopes=[scope("5h", 80), scope("weekly", 80)])
    codex = {"provider": "codex", "source": "fixture", "rows": [], "five_hour": {"used": 20, "resets_at": 2000000000}, "week": {"used": 20, "resets_at": 2000000000}}
    gone = {name: usage.unavailable_snapshot(name, f"{name} cli", "missing-cli") for name in ("kilo", "minimax", "opencode", "zai")}
    return {"claude": claude, "codex": codex, "copilot": usage.unavailable_snapshot("copilot", "copilot cli", "not-authenticated"), **gone}


def test_embedded_dashboard_is_limited_to_the_rotation_and_hides_providers_that_are_not_installed(hermetic_usage: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools import usage

    monkeypatch.setattr(usage, "_render_data_for_frame", lambda cfg, **_kw: (_provider_data(), None))
    data = collect_usage({"claude"})
    assert "Claude" in data["text"] and "Codex" not in data["text"] and "Copilot" not in data["text"]
    assert {str(row["provider"]).lower() for row in data["rows"]} == {"claude"}
    # `a` shows every enabled provider that is installed: the signed-out one stays, the missing CLIs go.
    everything = data["all_text"]
    assert "Claude" in everything and "Codex" in everything and "Copilot" in everything
    assert not any(name in everything for name in ("Kilo", "MiniMax", "OpenCode", "Z.ai"))
    assert data["not_installed"] == ["kilo", "minimax", "opencode", "zai"]


def test_rotation_providers_come_from_the_projection() -> None:
    from llm_tools.bridge_usage import rotation_providers

    assert rotation_providers({"stations": [{"name": "claude", "status": "configured"}, {"name": "old", "status": "historical"}]}) == {"claude"}
    assert rotation_providers({"stations": {"configured": {"providers": ["claude", "codex"], "routes": ["r1"]}}}) == {"claude", "codex"}
    decisions = [{"route": "r1", "provider": "kilo", "capacity_provider": "zai"}]
    assert rotation_providers({"capacity": {"selection": {"decisions": decisions}}}) == {"kilo", "zai"}
    assert rotation_providers({}) == frozenset()


def _state(usage_block: dict[str, Any]):
    from tests.test_bridge_tui_experience import make_state

    decisions = [{"provider": "claude", "usable": True, "windows": [
        {"name": "5h", "kind": "reset_window", "remaining_percent": 80}, {"name": "weekly", "kind": "reset_window", "remaining_percent": 80}]}]
    snapshot = {"usage": usage_block, "capacity": {"selection": {"provider": "claude", "decisions": decisions}, "source_age_seconds": 38}}
    state = make_state(snapshot, view="Usage")
    state.content_width = 200
    return state


_TEXT = ("LLM Usage · 03:33\n\nProvider   Ready   Scope    Remaining\n────────   ─────   ──────   ─────────\n"
         "Claude     no      5h       unavailable\n")


def test_usage_view_names_its_source_and_reports_a_difference_from_the_controller() -> None:
    rows = [{"provider": "claude", "scope": "5h", "remaining_percent": None}, {"provider": "claude", "scope": "weekly", "remaining_percent": None}]
    state = _state({"status": "ready", "age_seconds": 3, "text": _TEXT, "rows": rows})
    shown = state.rows()
    assert shown[0] == "Usage · ready · updated 3s ago · live llm-usage read"
    assert shown[1] == "claude differs from the controller's last observation (38s ago): 5h 80% left · weekly 80% left"
    agreeing = [{"provider": "claude", "scope": "5h", "remaining_percent": 78}, {"provider": "claude", "scope": "weekly", "remaining_percent": 80}]
    state = _state({"status": "ready", "age_seconds": 3, "text": _TEXT, "rows": agreeing})
    assert not any("differs from the controller" in row for row in state.rows())


def test_usage_view_toggles_between_the_rotation_and_all_providers_with_a() -> None:
    block = {"status": "ready", "age_seconds": 3, "text": _TEXT, "rows": [], "all_text": _TEXT.replace("Claude   ", "Everyone "), "all_rows": []}
    state = _state(block)
    assert "rotation (a: all)" in state.rows()[0] and not any(row.startswith("Everyone") for row in state.rows())
    state.handle_key("a")
    assert "all providers (a: rotation)" in state.rows()[0] and any(row.startswith("Everyone") for row in state.rows())
    state.handle_key("a")
    assert not any(row.startswith("Everyone") for row in state.rows())
    plain = _state({"status": "ready", "age_seconds": 3, "text": _TEXT, "rows": []})
    plain.handle_key("a")  # nothing to toggle without a rotation
    assert plain.usage_all is False


def test_an_exhausted_monthly_window_reads_empty_not_conserve() -> None:
    from llm_tools import usage

    reset = 2000000000
    now = {"LLM_USAGE_NOW_EPOCH": str(reset - 20 * 86400)}
    assert usage.classify_guidance("Copilot", "monthly", 0, reset, now).text == "× empty"
    assert usage.classify_guidance("Claude", "weekly", 0, reset, now).severity == "empty"
    # A window with quota left, and one that was not measured, keep their wording (blank-versus-unavailable policy).
    assert usage.classify_guidance("Copilot", "monthly", 20, reset, now).text == "↓ conserve"
    assert usage.classify_guidance("Copilot", "monthly", None, reset, now).severity == "unknown"
