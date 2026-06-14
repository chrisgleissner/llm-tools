"""Tests for the provider adapter module structure."""

from __future__ import annotations

import inspect
import os
import threading
import time
from pathlib import Path

import pytest

from llm_tools import common
from llm_tools import usage
from llm_tools.capacity import (
    CapacityKind,
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_COPILOT,
    PROVIDER_KILO,
    PROVIDER_MINIMAX,
    ProviderSnapshot,
)
from llm_tools.providers import claude, codex, copilot, kilo, minimax


def test_providers_module_exports_all_providers() -> None:
    """Every supported provider has a dedicated module under providers/."""
    for module, name in (
        (codex, "codex"),
        (claude, "claude"),
        (copilot, "copilot"),
        (kilo, "kilo"),
        (minimax, "minimax"),
    ):
        assert inspect.ismodule(module), f"providers.{name} is not a module"
        # Each adapter exposes a read(env) that returns a ProviderSnapshot.
        assert callable(getattr(module, "read", None)), f"providers.{name} missing read()"


def test_providers_star_import_exports_constants() -> None:
    namespace: dict[str, object] = {}
    exec("from llm_tools.providers import *", namespace)
    assert namespace["PROVIDER_KILO"] == PROVIDER_KILO
    assert namespace["PROVIDER_MINIMAX"] == PROVIDER_MINIMAX
    assert namespace["PROVIDER_OPENCODE"] == "opencode"


def test_local_snapshot_max_age_is_capped() -> None:
    assert common.local_snapshot_max_age({}) == 60
    assert common.local_snapshot_max_age({"LLM_USAGE_LOCAL_SNAPSHOT_MAX_AGE": "5"}) == 5
    assert common.local_snapshot_max_age({"LLM_USAGE_LOCAL_SNAPSHOT_MAX_AGE": "300"}) == 60
    assert common.local_snapshot_max_age({"LLM_USAGE_LOCAL_SNAPSHOT_MAX_AGE": "0"}) == 60
    assert common.local_snapshot_max_age({"LLM_USAGE_LOCAL_SNAPSHOT_MAX_AGE": "bad"}) == 60


def test_usage_provider_parallelism_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage.os, "cpu_count", lambda: 8)
    assert usage.provider_parallelism({}) == 8
    assert usage.provider_parallelism({"LLM_USAGE_PROVIDER_PARALLELISM": "2"}) == 2
    assert usage.provider_parallelism({"LLM_USAGE_PROVIDER_PARALLELISM": "0"}) == 8
    assert usage.provider_parallelism({"LLM_USAGE_PROVIDER_PARALLELISM": "bad"}) == 8


def test_usage_provider_reads_fan_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import llm_tools.providers as providers

    barrier = threading.Barrier(6)

    def wait_for_peers(value: object) -> object:
        barrier.wait(timeout=2.0)
        return value

    monkeypatch.setattr(
        usage.common,
        "read_codex",
        lambda: wait_for_peers({"provider": "codex", "available": False, "reason": "fixture"}),
    )
    monkeypatch.setattr(
        providers,
        "read_claude_snapshot",
        lambda: wait_for_peers(ProviderSnapshot(provider="claude", available=False, reason="fixture")),
    )
    monkeypatch.setattr(
        providers,
        "read_copilot_snapshot",
        lambda: wait_for_peers(ProviderSnapshot(provider="copilot", available=False, reason="fixture")),
    )
    monkeypatch.setattr(
        providers,
        "read_kilo",
        lambda: wait_for_peers(ProviderSnapshot(provider="kilo", available=False, reason="fixture")),
    )
    monkeypatch.setattr(
        providers,
        "read_opencode",
        lambda: wait_for_peers(ProviderSnapshot(provider="opencode", available=False, reason="fixture")),
    )
    monkeypatch.setattr(
        providers,
        "read_minimax",
        lambda: wait_for_peers(ProviderSnapshot(provider="minimax", available=False, reason="fixture")),
    )
    cfg = usage.Config()
    cfg.provider_parallelism = 6
    start = time.monotonic()
    data = usage.read_all_provider_data(cfg)
    assert time.monotonic() - start < 1.0
    assert set(data) == {"codex", "claude", "copilot", "kilo", "opencode", "minimax"}


def test_codex_snapshot_normalises_legacy_shape(env: dict[str, str], tmp_path) -> None:
    (Path(env["HOME"]) / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
    (Path(env["HOME"]) / ".codex" / "sessions" / "s.jsonl").write_text(
        '{"rate_limits":{"primary":{"used_percent":10,"resets_at":"2030-01-01T00:00:00Z"},"secondary":{"used_percent":20,"resets_at":"2030-01-07T00:00:00Z"}}}\n',
        encoding="utf-8",
    )
    env["LLM_USAGE_NOW_EPOCH"] = "1000"
    snap = codex.read(env)
    assert snap.provider == PROVIDER_CODEX
    assert snap.available is True
    assert {s.name for s in snap.scopes} == {"5h", "weekly"}
    for scope in snap.scopes:
        assert scope.kind == CapacityKind.RESET_WINDOW
        assert scope.remaining_percent is not None


def test_codex_snapshot_unavailable_when_no_data(env: dict[str, str], monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(env["HOME"]))
    env["LLM_USAGE_NOW_EPOCH"] = "1000"
    snap = codex.read(env)
    assert snap.available is False
    assert snap.reason == "no-local-data"


def test_codex_snapshot_marks_old_active_local_data_stale(env: dict[str, str]) -> None:
    home = Path(env["HOME"])
    path = home / ".codex" / "sessions" / "stale.jsonl"
    path.write_text(
        '{"rate_limits":{"primary":{"used_percent":10,"resets_at":5000},"secondary":{"used_percent":20,"resets_at":9000}}}\n',
        encoding="utf-8",
    )
    os.utime(path, (1000, 1000))
    stale_env = env | {
        "LLM_USAGE_NOW_EPOCH": "2000",
        "LLM_USAGE_LOCAL_SNAPSHOT_MAX_AGE": "60",
    }
    raw = codex.read_codex(stale_env)
    assert raw is not None
    assert raw["available"] is False
    assert raw["reason"] == "stale-usage"
    snap = codex.read(stale_env)
    assert snap.available is False
    assert snap.reason == "stale-usage"


def test_codex_snapshot_uses_env_home_without_monkeypatch(env: dict[str, str]) -> None:
    home = Path(env["HOME"])
    (home / ".codex" / "sessions" / "env-home.jsonl").write_text(
        '{"rate_limits":{"primary":{"used_percent":10,"resets_at":900},"secondary":{"used_percent":20,"resets_at":900}}}\n',
        encoding="utf-8",
    )
    raw = codex.read_codex(env | {"LLM_USAGE_NOW_EPOCH": "1000"})
    assert raw is not None
    assert raw["five_hour"]["used"] == 0.0


def test_claude_snapshot_unavailable_when_no_data(env: dict[str, str], monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(env["HOME"]))
    env["LLM_USAGE_NOW_EPOCH"] = "1000"
    snap = claude.read(env)
    assert snap.available is False
    assert snap.reason == "no-local-data"


def test_claude_snapshot_marks_old_status_cache_stale(env: dict[str, str]) -> None:
    cache = common.usage_cache_dir(env) / "claude-status.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        '{"rate_limits":{"five_hour":{"used_percentage":10,"resets_at":5000},"seven_day":{"used_percentage":20,"resets_at":9000}}}\n',
        encoding="utf-8",
    )
    os.utime(cache, (1000, 1000))
    snap = claude.read(env | {"LLM_USAGE_NOW_EPOCH": "2000", "LLM_USAGE_LOCAL_SNAPSHOT_MAX_AGE": "60"})
    assert snap.available is False
    assert snap.reason == "stale-usage"


def test_copilot_snapshot_unavailable_when_no_data(env: dict[str, str]) -> None:
    env["LLM_USAGE_DISABLE_COPILOT"] = "1"
    env["LLM_USAGE_NOW_EPOCH"] = "1000"
    snap = copilot.read(env)
    # copilot CLI capture is disabled → unavailable.
    assert snap.available is False


def test_kilo_snapshot_uses_env(env: dict[str, str]) -> None:
    env["LLM_USAGE_KILO_BALANCE"] = "5"
    env["LLM_USAGE_KILO_CURRENCY"] = "USD"
    snap = kilo.read(env)
    assert snap.provider == PROVIDER_KILO
    balance = next(s for s in snap.scopes if s.kind == CapacityKind.BALANCE)
    assert balance.remaining_amount == 5.0
    assert balance.currency == "USD"


def test_minimax_snapshot_uses_env(env: dict[str, str]) -> None:
    env["PATH"] = "/var/empty"
    env["LLM_USAGE_MINIMAX_5H_PERCENT"] = "75"
    env["LLM_USAGE_MINIMAX_5H_RESET_EPOCH"] = "1700000000"
    env["LLM_USAGE_MINIMAX_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH"] = "1700003600"
    snap = minimax.read(env)
    assert snap.provider == PROVIDER_MINIMAX
    assert {s.name for s in snap.scopes} == {"5h", "weekly"}
    for scope in snap.scopes:
        assert scope.kind == CapacityKind.RESET_WINDOW
        assert scope.remaining_percent is not None
