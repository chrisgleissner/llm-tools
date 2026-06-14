"""Per-model usage rows (Codex Spark, Claude Sonnet) and table alignment.

These tests lock in two related behaviours:

* Providers can surface model-specific rate limits as their own rows under the
  same provider section, named in a dedicated ``Model`` column. The Tool column
  no longer overflows with a long combined name (the bug that broke alignment).
* Claude's per-model weekly buckets (``seven_day_sonnet`` etc.) are display
  only: they never gate scheduler/rotation decisions.
"""

from __future__ import annotations

import contextlib
from io import StringIO

from llm_tools import common, usage
from llm_tools.capacity import (
    CapacityKind,
    CapacityScope,
    PROVIDER_CLAUDE,
    ProviderSnapshot,
    SCOPE_AUTO,
    SCOPE_WEEKLY,
    decide,
)
from llm_tools.providers import claude as claude_provider


def _render(cfg: usage.Config, rows: list[usage.UsageRow]) -> str:
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_usage_rows(cfg, rows)
    return buf.getvalue()


def _cfg() -> usage.Config:
    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    cfg.show_remaining_time = False
    return cfg


def _bar_index(line: str) -> int:
    for token in ("█", "░"):
        idx = line.find(token)
        if idx != -1:
            return idx
    return -1


# --- normalize / freshen ------------------------------------------------------


def test_normalize_claude_obj_parses_per_model_weeks() -> None:
    obj = {
        "five_hour": {"utilization": 28.0, "resets_at": "2099-01-01T00:00:00Z"},
        "seven_day": {"utilization": 40.0, "resets_at": "2099-01-02T00:00:00Z"},
        "seven_day_sonnet": {"utilization": 0.0, "resets_at": "2099-01-02T00:00:00Z"},
        "seven_day_opus": None,
        "seven_day_haiku": {"utilization": 12.0, "resets_at": "2099-01-02T00:00:00Z"},
    }
    norm = common.normalize_claude_obj(obj, "test")
    assert norm is not None
    models = {entry["model"]: entry["week"] for entry in norm["model_weeks"]}
    assert set(models) == {"Sonnet", "Haiku"}  # opus was null -> skipped
    assert models["Sonnet"]["used"] == 0.0
    assert models["Haiku"]["used"] == 12.0


def test_normalize_claude_obj_without_models_has_no_model_weeks() -> None:
    obj = {"rate_limits": {"five_hour": {"used_percentage": 10}}}
    norm = common.normalize_claude_obj(obj, "test")
    assert norm is not None
    assert "model_weeks" not in norm


def test_freshen_zeroes_stale_model_week() -> None:
    obj = {
        "five_hour": None,
        "week": None,
        "model_weeks": [
            {"model": "Sonnet", "week": {"used": 90.0, "resets_at": "2000-01-01T00:00:00Z"}},
        ],
    }
    common.freshen_provider_windows(obj, {"LLM_USAGE_NOW_EPOCH": "4102444800"})  # year 2100
    week = obj["model_weeks"][0]["week"]
    assert week["used"] == 0.0
    assert week["resets_at"] is None


# --- snapshot / decisions -----------------------------------------------------


def test_claude_read_builds_display_only_model_scopes(monkeypatch) -> None:
    raw = {
        "provider": "claude",
        "source": "claude api",
        "five_hour": {"used": 20.0, "resets_at": "2099-01-01T00:00:00Z"},
        "week": {"used": 40.0, "resets_at": "2099-01-02T00:00:00Z"},
        "model_weeks": [
            {"model": "Sonnet", "week": {"used": 0.0, "resets_at": "2099-01-02T00:00:00Z"}},
        ],
    }
    monkeypatch.setattr(claude_provider, "read_claude", lambda env=None: raw)
    snap = claude_provider.read({})
    assert snap.available is True
    assert [s.name for s in snap.scopes] == ["5h", "weekly"]
    assert len(snap.model_scopes) == 1
    assert snap.model_scopes[0].extras["model"] == "Sonnet"


def test_decide_ignores_model_scopes() -> None:
    # The aggregate weekly window has plenty of capacity; a Sonnet-only window
    # is fully exhausted. decide() must remain usable because model_scopes are
    # never consulted.
    snap = ProviderSnapshot(
        provider=PROVIDER_CLAUDE,
        available=True,
        scopes=[CapacityScope(name="weekly", kind=CapacityKind.RESET_WINDOW, remaining_percent=80.0)],
        model_scopes=[CapacityScope(name="weekly", kind=CapacityKind.RESET_WINDOW, remaining_percent=0.0, extras={"model": "Sonnet"})],
    )
    for scope in (SCOPE_AUTO, SCOPE_WEEKLY):
        decision = decide(snap, scope, min_remaining_percent=10.0, min_remaining_amount=0.0, poll_interval=60)
        assert decision.usable is True, scope


# --- rendering ----------------------------------------------------------------


def test_claude_sonnet_row_is_colocated_under_claude() -> None:
    snap = ProviderSnapshot(
        provider=PROVIDER_CLAUDE,
        available=True,
        source="claude api",
        scopes=[
            CapacityScope(name="5h", kind=CapacityKind.RESET_WINDOW, remaining_percent=72.0, source="claude api"),
            CapacityScope(name="weekly", kind=CapacityKind.RESET_WINDOW, remaining_percent=60.0, source="claude api"),
        ],
        model_scopes=[
            CapacityScope(name="weekly", kind=CapacityKind.RESET_WINDOW, remaining_percent=100.0, source="claude api", extras={"model": "Sonnet"}),
        ],
    )
    cfg = _cfg()
    rows = usage.claude_rows(cfg, snap)
    # Aggregate 5h + weekly, plus the Sonnet weekly row, all under "Claude".
    assert [r.provider for r in rows] == ["Claude", "Claude", "Claude"]
    assert rows[-1].model == "Sonnet"
    out = _render(cfg, rows)
    assert "Sonnet" in out
    # The Sonnet row must not start a new provider block (Tool column blank).
    sonnet_line = next(line for line in out.splitlines() if "Sonnet" in line)
    assert sonnet_line.startswith(" ")


def test_table_header_includes_model_column_when_requested() -> None:
    cfg = _cfg()
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_table_header(cfg, show_model=True)
    assert "Model" in buf.getvalue()
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_table_header(cfg, show_model=False)
    assert "Model" not in buf.getvalue()


def test_codex_spark_renders_under_codex_with_model_column() -> None:
    cfg = _cfg()
    codex_json = {
        "provider": "codex",
        "source": "~/.codex",
        "rows": [
            {"key": "codex", "name": "Codex", "five_hour": {"used": 20.0}, "week": {"used": 30.0}},
            {"key": "codex-spark", "name": "GPT-5.3-Codex-Spark", "five_hour": {"used": 99.0}, "week": {"used": 96.0}},
        ],
    }
    out = _render(cfg, usage.codex_rows(cfg, codex_json))
    assert "GPT-5.3 Spark" not in out  # no longer crammed into the Tool column
    assert "Spark" in out
    spark_line = next(line for line in out.splitlines() if "Spark" in line)
    assert spark_line.startswith(" ")  # colocated under Codex, Tool blank


def test_model_rows_keep_columns_aligned() -> None:
    """The progress bar (Remaining column) must start at the same offset on
    every row regardless of model sub-rows; this is the alignment the long
    "GPT-5.3 Spark" name used to break."""
    cfg = _cfg()
    rows = [
        usage.UsageRow("Claude", "5h", 72.0, "72%", None, "s"),
        usage.UsageRow("Claude", "weekly", 60.0, "60%", None, "s"),
        usage.UsageRow("Claude", "weekly", 100.0, "100%", None, "s", model="Sonnet"),
        usage.UsageRow("Codex", "5h", 1.0, "1%", None, "s"),
        usage.UsageRow("Codex", "weekly", 4.0, "4%", None, "s", model="Spark"),
    ]
    out = _render(cfg, rows)
    bar_offsets = {_bar_index(line) for line in out.splitlines() if _bar_index(line) != -1}
    assert len(bar_offsets) == 1, out  # every bar starts in the same column


def test_no_model_column_when_no_models() -> None:
    cfg = _cfg()
    rows = [usage.UsageRow("Copilot", "monthly", 38.0, "38%", None, "s")]
    out = _render(cfg, rows)
    assert "Model" not in out
