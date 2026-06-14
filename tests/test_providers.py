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


CODEX_RATE_LIMITS_PAYLOAD = (
    '{"rateLimits":{"limitId":"codex","planType":"pro",'
    '"primary":{"usedPercent":20,"windowDurationMins":300,"resetsAt":5000},'
    '"secondary":{"usedPercent":84,"windowDurationMins":10080,"resetsAt":9000}},'
    '"rateLimitsByLimitId":{'
    '"codex":{"primary":{"usedPercent":20,"resetsAt":5000},"secondary":{"usedPercent":84,"resetsAt":9000}},'
    '"codex_bengalfox":{"limitName":"GPT-5.3-Codex-Spark",'
    '"primary":{"usedPercent":3,"resetsAt":5000},"secondary":{"usedPercent":7,"resetsAt":9000}}}}'
)


def test_codex_active_refresh_overrides_stale_local_snapshot(env: dict[str, str]) -> None:
    """The app-server payload is fresh, so an old local file never wins."""
    home = Path(env["HOME"])
    path = home / ".codex" / "sessions" / "stale.jsonl"
    path.write_text(
        '{"rate_limits":{"primary":{"used_percent":99,"resets_at":5000}}}\n',
        encoding="utf-8",
    )
    os.utime(path, (1000, 1000))
    live_env = env | {
        "LLM_USAGE_NOW_EPOCH": "2000",
        "LLM_USAGE_LOCAL_SNAPSHOT_MAX_AGE": "60",
        "LLM_USAGE_CODEX_RATE_LIMITS_JSON": CODEX_RATE_LIMITS_PAYLOAD,
    }
    raw = codex.read_codex(live_env)
    assert raw is not None
    assert raw.get("available") is not False
    assert raw["source"] == "codex app-server"
    assert raw["five_hour"]["used"] == 20.0
    assert raw["plan"] == "pro"
    keys = {row["key"] for row in raw["rows"]}
    assert keys == {"codex", "codex-spark"}
    snap = codex.read(live_env)
    assert snap.available is True
    assert {s.name for s in snap.scopes} == {"5h", "weekly"}


def test_codex_active_refresh_reports_not_authenticated(env: dict[str, str], fake_bin: Path) -> None:
    """A CLI on PATH but no credentials surfaces an auth reason, not stale data."""
    from .conftest import write_exe

    write_exe(fake_bin / "codex", "#!/usr/bin/env bash\nexit 0\n")
    live_env = {k: v for k, v in env.items() if k != "LLM_USAGE_DISABLE_CODEX_APP_SERVER"}
    api = common.read_codex_api(live_env)
    assert api == {
        "provider": "codex",
        "source": "codex app-server",
        "available": False,
        "reason": "not-authenticated",
    }
    snap = codex.read(live_env)
    assert snap.available is False
    assert snap.reason == "not-authenticated"


def test_codex_active_refresh_reports_missing_cli(env: dict[str, str], fake_bin: Path) -> None:
    """No codex binary on PATH is a startup problem, surfaced as missing-cli."""
    live_env = {k: v for k, v in env.items() if k != "LLM_USAGE_DISABLE_CODEX_APP_SERVER"}
    live_env["PATH"] = str(fake_bin)  # fake_bin has no codex
    api = common.read_codex_api(live_env)
    assert api is not None
    assert api["available"] is False
    assert api["reason"] == "missing-cli"


def test_codex_api_falls_back_to_fresh_cache_on_transient_failure(env: dict[str, str]) -> None:
    """A transient app-server failure serves the most recent cached payload."""
    cache = common.usage_cache_dir(env) / "codex-usage-api.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(CODEX_RATE_LIMITS_PAYLOAD + "\n", encoding="utf-8")
    # Disable flag (set by the fixture) makes the live read a transient miss.
    raw = common.read_codex_api(env)
    assert raw is not None
    assert raw["five_hour"]["used"] == 20.0
    assert raw["source"] == "codex app-server (cached)"


FAKE_APP_SERVER_OK = """#!/usr/bin/env python3
import sys, json
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if isinstance(msg, dict) and msg.get("id") == 2:
        print(json.dumps({"id": 2, "result": {"rateLimits": {"limitId": "codex", "planType": "pro",
            "primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": 5000},
            "secondary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": 9000}}}}))
        sys.stdout.flush()
        break
"""

FAKE_APP_SERVER_AUTH_ERROR = """#!/usr/bin/env python3
import sys, json
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if isinstance(msg, dict) and msg.get("id") == 2:
        print(json.dumps({"id": 2, "error": {"code": -32000, "message": "please login first"}}))
        sys.stdout.flush()
        break
"""


def _seed_codex_auth(home: Path) -> None:
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"tok"}}', encoding="utf-8")


def _codex_live_env(env: dict[str, str], fake_bin: Path, server_cmd: str) -> dict[str, str]:
    from .conftest import write_exe

    write_exe(fake_bin / "codex", "#!/usr/bin/env bash\nexit 0\n")
    live_env = {k: v for k, v in env.items() if k != "LLM_USAGE_DISABLE_CODEX_APP_SERVER"}
    live_env["LLM_USAGE_CODEX_APP_SERVER_CMD"] = server_cmd
    return live_env


def test_codex_app_server_subprocess_success(env: dict[str, str], fake_bin: Path) -> None:
    """Drive the real JSON-RPC handshake against a fake app-server binary."""
    from .conftest import write_exe

    home = Path(env["HOME"])
    _seed_codex_auth(home)
    server = write_exe(fake_bin / "fake-appserver-ok", FAKE_APP_SERVER_OK)
    live_env = _codex_live_env(env, fake_bin, str(server))
    raw = common.read_codex_api(live_env)
    assert raw is not None
    assert raw.get("available") is not False
    assert raw["source"] == "codex app-server"
    assert raw["five_hour"]["used"] == 42.0
    # A successful live read is cached for the transient-failure fallback.
    assert (common.usage_cache_dir(live_env) / "codex-usage-api.json").is_file()


def test_codex_app_server_subprocess_auth_error(env: dict[str, str], fake_bin: Path) -> None:
    """A JSON-RPC auth error from the app-server maps to not-authenticated."""
    from .conftest import write_exe

    home = Path(env["HOME"])
    _seed_codex_auth(home)
    server = write_exe(fake_bin / "fake-appserver-auth", FAKE_APP_SERVER_AUTH_ERROR)
    live_env = _codex_live_env(env, fake_bin, str(server))
    api = common.read_codex_api(live_env)
    assert api is not None
    assert api["available"] is False
    assert api["reason"] == "not-authenticated"


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


def test_progress_reporter_is_silent_when_disabled() -> None:
    import io

    buf = io.StringIO()
    reporter = usage.ProgressReporter(enabled=False, stream=buf)
    reporter.start()
    reporter.begin(6)
    reporter.advance()
    reporter.stop()
    assert buf.getvalue() == ""


def test_progress_reporter_animates_then_erases() -> None:
    import io

    buf = io.StringIO()
    reporter = usage.ProgressReporter(enabled=True, stream=buf, interval=0.01)
    reporter.start()
    reporter.begin(6)
    for _ in range(6):
        reporter.advance()
    time.sleep(0.05)
    reporter.stop()
    output = buf.getvalue()
    assert "refreshing usage" in output
    assert "6/6" in output
    # The line is fully erased on stop, leaving the terminal untouched.
    assert output.endswith("\r\033[K")


def test_progress_reporter_ascii_frames_without_symbols() -> None:
    import io

    buf = io.StringIO()
    reporter = usage.ProgressReporter(enabled=True, stream=buf, symbols=False, interval=0.01)
    reporter.start()
    reporter.begin(1)
    reporter.advance()
    time.sleep(0.03)
    reporter.stop()
    output = buf.getvalue()
    assert any(frame in output for frame in usage.ProgressReporter.FRAMES_ASCII)
    # No braille frames leak through when symbols are disabled.
    assert not any(frame in output for frame in usage.ProgressReporter.FRAMES_UNICODE)
