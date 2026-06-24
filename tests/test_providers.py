"""Tests for the provider adapter module structure."""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

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

    barrier = threading.Barrier(7)

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
    monkeypatch.setattr(
        providers,
        "read_zai",
        lambda: wait_for_peers(ProviderSnapshot(provider="zai", available=False, reason="fixture")),
    )
    cfg = usage.Config()
    cfg.provider_parallelism = 7
    start = time.monotonic()
    data = usage.read_all_provider_data(cfg)
    assert time.monotonic() - start < 1.0
    assert set(data) == {"codex", "claude", "copilot", "kilo", "opencode", "minimax", "zai"}


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


# --- Stale-usage recovery: never surface stale data while a live path exists ---


def _fresh_snapshot() -> dict[str, object]:
    return {"provider": "claude", "source": "api", "five_hour": {"used": 1.0}, "week": {"used": 2.0}}


def _stale_snapshot() -> dict[str, object]:
    return common.stale_usage_provider("claude", "src", 1000, {})


def test_is_stale_usage_result_detects_marker() -> None:
    assert common.is_stale_usage_result(_stale_snapshot()) is True
    assert common.is_stale_usage_result(_fresh_snapshot()) is False
    assert common.is_stale_usage_result(None) is False
    assert common.is_stale_usage_result({"available": False, "reason": "not-authenticated"}) is False


def test_stale_recovery_schedule_disabled_when_retries_zero(env: dict[str, str]) -> None:
    # The test-suite pins LLM_USAGE_LIVE_FETCH_RETRIES=0, so recovery never
    # sleeps on the failure path unless a test opts in explicitly.
    assert common.stale_recovery_schedule(env) == []


def test_stale_recovery_schedule_bounded_exponential(env: dict[str, str]) -> None:
    sched = common.stale_recovery_schedule(
        env
        | {
            "LLM_USAGE_LIVE_FETCH_RETRIES": "2",
            "LLM_USAGE_STALE_RECOVERY_ATTEMPTS": "5",
            "LLM_USAGE_STALE_RECOVERY_DELAY": "0.5",
            "LLM_USAGE_STALE_RECOVERY_MAX_DELAY": "4",
        }
    )
    assert sched == [0.5, 1.0, 2.0, 4.0, 4.0]


def test_recover_stale_retries_until_fresh_when_live_path_present(env: dict[str, str]) -> None:
    calls = {"n": 0}

    def read_fn() -> dict[str, object]:
        calls["n"] += 1
        return _stale_snapshot() if calls["n"] < 3 else _fresh_snapshot()

    recovery_env = env | {
        "LLM_USAGE_LIVE_FETCH_RETRIES": "2",
        "LLM_USAGE_STALE_RECOVERY_ATTEMPTS": "5",
        "LLM_USAGE_STALE_RECOVERY_DELAY": "0",
        "LLM_USAGE_STALE_RECOVERY_MAX_DELAY": "0",
    }
    result = common.recover_stale_with_live_retry(read_fn, True, recovery_env)
    assert common.is_stale_usage_result(result) is False
    assert calls["n"] == 3


def test_recover_stale_no_retry_without_live_path(env: dict[str, str]) -> None:
    calls = {"n": 0}

    def read_fn() -> dict[str, object]:
        calls["n"] += 1
        return _stale_snapshot()

    result = common.recover_stale_with_live_retry(read_fn, False, env)
    assert common.is_stale_usage_result(result) is True
    assert calls["n"] == 1


def test_recover_stale_gives_up_after_budget(env: dict[str, str]) -> None:
    calls = {"n": 0}

    def read_fn() -> dict[str, object]:
        calls["n"] += 1
        return _stale_snapshot()

    recovery_env = env | {
        "LLM_USAGE_LIVE_FETCH_RETRIES": "2",
        "LLM_USAGE_STALE_RECOVERY_ATTEMPTS": "3",
        "LLM_USAGE_STALE_RECOVERY_DELAY": "0",
        "LLM_USAGE_STALE_RECOVERY_MAX_DELAY": "0",
    }
    result = common.recover_stale_with_live_retry(read_fn, True, recovery_env)
    assert common.is_stale_usage_result(result) is True
    # 1 initial read + 3 scheduled retries.
    assert calls["n"] == 4


def test_recover_stale_respects_wall_clock_deadline(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery loop aborts once the wall-clock budget is spent, even with
    attempts left, so a genuinely unreachable provider never stalls the loop."""
    ticks = iter([0.0, 100.0])  # deadline=20s; first loop check at 100s -> break
    monkeypatch.setattr(common.time, "monotonic", lambda: next(ticks))
    calls = {"n": 0}

    def read_fn() -> dict[str, object]:
        calls["n"] += 1
        return _stale_snapshot()

    recovery_env = env | {
        "LLM_USAGE_LIVE_FETCH_RETRIES": "2",
        "LLM_USAGE_STALE_RECOVERY_DELAY": "0",
        "LLM_USAGE_STALE_RECOVERY_MAX_DELAY": "0",
    }
    result = common.recover_stale_with_live_retry(read_fn, True, recovery_env)
    assert common.is_stale_usage_result(result) is True
    assert calls["n"] == 1  # only the initial read; deadline aborted all retries


def test_claude_has_auth(env: dict[str, str]) -> None:
    cred = Path(env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    assert common._claude_has_auth(env) is False
    cred.write_text('{"claudeAiOauth":{"refreshToken":"r"}}', encoding="utf-8")
    assert common._claude_has_auth(env) is True
    cred.write_text('{"claudeAiOauth":{"accessToken":"a"}}', encoding="utf-8")
    assert common._claude_has_auth(env) is True
    cred.write_text('{"claudeAiOauth":{}}', encoding="utf-8")
    assert common._claude_has_auth(env) is False


def test_codex_live_available(env: dict[str, str], fake_bin: Path) -> None:
    from .conftest import write_exe

    # Injected payload always counts as a live path.
    assert common.codex_live_available(env | {"LLM_USAGE_CODEX_RATE_LIMITS_JSON": "{}"}) is True
    # App-server explicitly disabled → no live path.
    assert common.codex_live_available(env | {"LLM_USAGE_DISABLE_CODEX_APP_SERVER": "1"}) is False
    # CLI present + authenticated → live path.
    write_exe(fake_bin / "codex", "#!/usr/bin/env bash\nexit 0\n")
    auth = Path(env["HOME"]) / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text('{"tokens":{"access_token":"a"}}', encoding="utf-8")
    live_env = {k: v for k, v in env.items() if k != "LLM_USAGE_DISABLE_CODEX_APP_SERVER"}
    assert common.codex_live_available(live_env) is True
    # CLI present but unauthenticated → no live path.
    auth.write_text("{}", encoding="utf-8")
    assert common.codex_live_available(live_env) is False


def test_claude_read_retries_live_fetch_before_surfacing_stale(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient stale read with credentials present is re-driven to fresh."""
    cred = Path(env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text('{"claudeAiOauth":{"accessToken":"a","refreshToken":"r"}}', encoding="utf-8")
    calls = {"n": 0}

    def fake_raw(_env: dict[str, str]) -> dict[str, object]:
        calls["n"] += 1
        return _stale_snapshot() if calls["n"] == 1 else _fresh_snapshot()

    monkeypatch.setattr(common, "_read_claude_raw", fake_raw)
    recovery_env = env | {
        "LLM_USAGE_LIVE_FETCH_RETRIES": "2",
        "LLM_USAGE_STALE_RECOVERY_DELAY": "0",
        "LLM_USAGE_STALE_RECOVERY_MAX_DELAY": "0",
    }
    result = common.read_claude(recovery_env)
    assert common.is_stale_usage_result(result) is False
    assert calls["n"] == 2


def test_claude_read_surfaces_stale_without_credentials(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credentials → no live path → stale is returned immediately, no retry."""
    calls = {"n": 0}

    def fake_raw(_env: dict[str, str]) -> dict[str, object]:
        calls["n"] += 1
        return _stale_snapshot()

    monkeypatch.setattr(common, "_read_claude_raw", fake_raw)
    result = common.read_claude(env | {"LLM_USAGE_LIVE_FETCH_RETRIES": "2"})
    assert common.is_stale_usage_result(result) is True
    assert calls["n"] == 1


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_refresh_oauth_token_retries_transient_failure(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient blip on the refresh POST is retried, not fatal."""
    from urllib.error import URLError

    cred = Path(env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred_data = {"claudeAiOauth": {"refreshToken": "r"}}
    calls = {"n": 0}

    def fake_urlopen(_req: object, timeout: float = 0) -> _FakeResp:
        calls["n"] += 1
        if calls["n"] == 1:
            raise URLError("network still settling")
        return _FakeResp(b'{"access_token":"new-token","expires_in":3600}')

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    token = common._refresh_claude_oauth_access_token(
        cred,
        cred_data,
        env | {"LLM_USAGE_LIVE_FETCH_RETRIES": "2", "LLM_USAGE_LIVE_FETCH_RETRY_DELAY": "0"},
    )
    assert token == "new-token"
    assert calls["n"] == 2
    # The rotated token is persisted for the next read.
    assert "new-token" in cred.read_text(encoding="utf-8")


def test_refresh_oauth_token_does_not_retry_4xx(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4xx means the refresh token is bad — authoritative, not retried."""
    from urllib.error import HTTPError

    cred = Path(env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred_data = {"claudeAiOauth": {"refreshToken": "r"}}
    calls = {"n": 0}

    def fake_urlopen(_req: object, timeout: float = 0) -> _FakeResp:
        calls["n"] += 1
        raise HTTPError("url", 400, "bad request", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    token = common._refresh_claude_oauth_access_token(
        cred,
        cred_data,
        env | {"LLM_USAGE_LIVE_FETCH_RETRIES": "3", "LLM_USAGE_LIVE_FETCH_RETRY_DELAY": "0"},
    )
    assert token is None
    assert calls["n"] == 1


def test_codex_read_retries_live_fetch_before_surfacing_stale(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient stale Codex read with a live path is re-driven to fresh."""
    calls = {"n": 0}

    def fake_once(_env: dict[str, str]) -> dict[str, object]:
        calls["n"] += 1
        return _stale_snapshot() if calls["n"] == 1 else _fresh_snapshot()

    monkeypatch.setattr(codex, "_read_codex_once", fake_once)
    recovery_env = env | {
        # Injected payload marks the live path as available without spawning a CLI.
        "LLM_USAGE_CODEX_RATE_LIMITS_JSON": "{}",
        "LLM_USAGE_LIVE_FETCH_RETRIES": "2",
        "LLM_USAGE_STALE_RECOVERY_DELAY": "0",
        "LLM_USAGE_STALE_RECOVERY_MAX_DELAY": "0",
    }
    result = codex.read_codex(recovery_env)
    assert common.is_stale_usage_result(result) is False
    assert calls["n"] == 2


# --- Copilot additional-usage ($) via the GitHub billing API ------------------

# now_epoch 1781587377 == 2026-06-16; current billing month is 2026-06.
_ADDON_NOW = "1781587377"
COPILOT_USAGE_PAYLOAD = json.dumps(
    {
        "usageItems": [
            {"date": "2026-06-01T00:00:00Z", "product": "copilot", "sku": "Copilot Premium Request", "netAmount": 1.5},
            {"date": "2026-06-01T00:00:00Z", "product": "copilot", "sku": "Copilot AI Credits", "netAmount": 0.25},
            {"date": "2026-05-01T00:00:00Z", "product": "copilot", "sku": "Copilot Premium Request", "netAmount": 9.9},
            {"date": "2026-06-01T00:00:00Z", "product": "actions", "sku": "Actions Linux", "netAmount": 5.0},
        ]
    }
)


def _addon_env(env: dict[str, str], **extra: str) -> dict[str, str]:
    out = {k: v for k, v in env.items() if k not in ("LLM_USAGE_DISABLE_COPILOT_ADDON", "LLM_USAGE_DISABLE_COPILOT_MONTHLY")}
    out["LLM_USAGE_NOW_EPOCH"] = _ADDON_NOW
    out.update(extra)
    return out


def test_copilot_addon_spent_sums_current_month_copilot_only(env: dict[str, str]) -> None:
    spent = common._copilot_addon_spent_from_usage(
        COPILOT_USAGE_PAYLOAD, env | {"LLM_USAGE_NOW_EPOCH": _ADDON_NOW}
    )
    # 1.5 + 0.25 from June copilot; May copilot and June actions excluded.
    assert spent == 1.75


def test_copilot_addon_spent_none_without_copilot_items(env: dict[str, str]) -> None:
    payload = json.dumps({"usageItems": [{"date": "2026-06-01T00:00:00Z", "product": "actions", "netAmount": 5.0}]})
    assert common._copilot_addon_spent_from_usage(payload, env | {"LLM_USAGE_NOW_EPOCH": _ADDON_NOW}) is None


def test_read_copilot_addon_injected_payload(env: dict[str, str]) -> None:
    res = common.read_copilot_addon(_addon_env(env, LLM_USAGE_COPILOT_ADDON_USAGE_JSON=COPILOT_USAGE_PAYLOAD))
    assert res == {"spent": 1.75, "currency": "$", "source": "github billing"}


def test_read_copilot_addon_disabled_returns_none(env: dict[str, str]) -> None:
    # The fixture pins LLM_USAGE_DISABLE_COPILOT_ADDON=1.
    assert common.read_copilot_addon(env | {"LLM_USAGE_COPILOT_ADDON_USAGE_JSON": COPILOT_USAGE_PAYLOAD}) is None


def test_read_copilot_addon_cache_hit(env: dict[str, str]) -> None:
    cache = common.usage_cache_dir(env) / "copilot-addon.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"spent": 2.5, "currency": "$", "source": "github billing"}), encoding="utf-8")
    # Fresh cache (TTL high) is served without any token/network access.
    res = common.read_copilot_addon(_addon_env(env, LLM_USAGE_COPILOT_ADDON_TTL="100000"))
    assert res == {"spent": 2.5, "currency": "$", "source": "github billing"}


def test_read_copilot_addon_no_token_returns_none(env: dict[str, str]) -> None:
    fake_only_path = env["PATH"].split(":", 1)[0]  # fake_bin, which has no gh
    e = {k: v for k, v in env.items() if k not in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")}
    e = _addon_env(e, PATH=fake_only_path)
    assert common.read_copilot_addon(e) is None


def test_read_copilot_addon_live_via_mocked_api(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float = 0) -> _FakeResp:
        url = getattr(req, "full_url", "")
        if url.endswith("/user"):
            return _FakeResp(b'{"login":"octocat"}')
        return _FakeResp(COPILOT_USAGE_PAYLOAD.encode("utf-8"))

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    e = _addon_env(env, GITHUB_TOKEN="ghp_fake", LLM_USAGE_COPILOT_ADDON_TTL="900")
    cache = common.usage_cache_dir(e) / "copilot-addon.json"
    if cache.exists():
        cache.unlink()
    res = common.read_copilot_addon(e)
    assert res == {"spent": 1.75, "currency": "$", "source": "github billing"}
    assert cache.is_file()  # result persisted for the TTL window


def test_github_token_env_precedence(env: dict[str, str]) -> None:
    base = {k: v for k, v in env.items() if k not in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")}
    assert common._github_token(base | {"COPILOT_GITHUB_TOKEN": "a", "GH_TOKEN": "b", "GITHUB_TOKEN": "c"}) == "a"
    assert common._github_token(base | {"GH_TOKEN": "b", "GITHUB_TOKEN": "c"}) == "b"
    assert common._github_token(base | {"GITHUB_TOKEN": "c"}) == "c"


def test_github_token_gh_fallback(env: dict[str, str], fake_bin: Path) -> None:
    from .conftest import write_exe

    write_exe(fake_bin / "gh", '#!/usr/bin/env bash\n[ "$1" = auth ] && echo gho_faketoken\n')
    base = {k: v for k, v in env.items() if k not in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")}
    assert common._github_token(base) == "gho_faketoken"


def test_copilot_snapshot_carries_addon_as_display_only_balance(env: dict[str, str]) -> None:
    snap = copilot.read(
        _addon_env(
            env,
            LLM_USAGE_COPILOT_CAPTURE_TEXT="Plan: 40% used · Session: 0 AIC used",
            LLM_USAGE_COPILOT_ADDON_USAGE_JSON=COPILOT_USAGE_PAYLOAD,
        )
    )
    assert snap.available is True
    # The add-on rides in model_scopes (display-only), so it never gates Ready.
    balances = [s for s in snap.model_scopes if s.kind == "balance"]
    assert len(balances) == 1
    assert balances[0].remaining_amount == 1.75
    assert not any(s.kind == "balance" for s in snap.scopes)


def test_copilot_rows_render_spent_balance_row(env: dict[str, str]) -> None:
    snap = copilot.read(
        _addon_env(
            env,
            LLM_USAGE_COPILOT_CAPTURE_TEXT="Plan: 40% used · Session: 0 AIC used",
            LLM_USAGE_COPILOT_ADDON_USAGE_JSON=COPILOT_USAGE_PAYLOAD,
        )
    )
    cfg = usage.Config()
    rows = usage.copilot_rows(cfg, usage._legacy_copilot(snap, False))
    monthly = next(r for r in rows if r.scope == "monthly")
    spend = next(r for r in rows if r.scope == "spend")
    assert monthly.remaining == 60.0  # 40% used -> 60% remaining
    # Underlying spent is 1.75; the amount is left-aligned ("$1.8"), no "spent".
    assert spend.left_text == "$1.8"
    assert spend.amount == 1.75
    assert spend.remaining == 1.0  # informational; keeps Ready truthy
    # Copilot stays Ready: the spend row must not gate readiness.
    assert usage.provider_ready(rows, "Copilot") is True


def _copilot_legacy_json(remaining: float | None, spent: float | None = None) -> dict[str, Any]:
    """Build a Copilot legacy-json dict (the shape ``copilot_rows`` consumes)."""
    used = None if remaining is None else max(0.0, min(100.0, 100.0 - remaining))
    out: dict[str, Any] = {
        "provider": "copilot",
        "source": "github billing",
        "available": True,
        "monthly": {"used": used, "remaining": remaining},
    }
    if spent is not None:
        out["add_on"] = {"spent": spent, "currency": "$", "source": "github billing"}
    return out


def test_copilot_exhausted_allowance_with_overage_is_ready_payg(env: dict[str, str]) -> None:
    """Included allowance spent (0% left) but GitHub billing already shows
    overage being charged ($ net spend > 0): pay-as-you-go is demonstrably
    enabled, so Copilot stays Ready and the allowance row explains itself."""
    cfg = usage.Config()
    cfg.copilot_spend_limit = None
    rows = usage.copilot_rows(cfg, _copilot_legacy_json(remaining=0.0, spent=3.0))
    monthly = next(r for r in rows if r.scope == "monthly")
    assert monthly.gates_ready is False
    assert monthly.guidance_override == "pay-as-you-go"
    assert usage.provider_ready(rows, "Copilot") is True


def test_copilot_exhausted_allowance_without_overage_or_limit_not_ready(env: dict[str, str]) -> None:
    """Allowance spent, no overage charged, and no declared limit: we cannot
    prove pay-as-you-go is funded, so Copilot stays Ready=no (unchanged)."""
    cfg = usage.Config()
    cfg.copilot_spend_limit = None
    rows = usage.copilot_rows(cfg, _copilot_legacy_json(remaining=0.0, spent=0.0))
    monthly = next(r for r in rows if r.scope == "monthly")
    assert monthly.gates_ready is True
    assert usage.provider_ready(rows, "Copilot") is False


def test_copilot_declared_spend_limit_with_headroom_is_ready(env: dict[str, str]) -> None:
    """A declared monthly spend limit with headroom keeps Copilot Ready even
    with zero overage charged yet, and the Guidance shows spend vs limit."""
    cfg = usage.Config()
    cfg.copilot_spend_limit = 25.0
    cfg.copilot_spend_currency = "$"
    rows = usage.copilot_rows(cfg, _copilot_legacy_json(remaining=0.0, spent=3.0))
    monthly = next(r for r in rows if r.scope == "monthly")
    assert monthly.gates_ready is False
    assert monthly.guidance_override == "pay-as-you-go $3/25"
    assert usage.provider_ready(rows, "Copilot") is True


def test_copilot_declared_spend_limit_exceeded_not_ready(env: dict[str, str]) -> None:
    """Once billed overage reaches the declared limit, GitHub blocks further
    use, so Copilot must report Ready=no."""
    cfg = usage.Config()
    cfg.copilot_spend_limit = 25.0
    cfg.copilot_spend_currency = "$"
    rows = usage.copilot_rows(cfg, _copilot_legacy_json(remaining=0.0, spent=30.0))
    monthly = next(r for r in rows if r.scope == "monthly")
    assert monthly.gates_ready is True
    assert usage.provider_ready(rows, "Copilot") is False


def test_copilot_declared_spend_limit_unknown_spend_not_ready(env: dict[str, str]) -> None:
    """A declared limit with NO billing signal (no add-on / GitHub token) cannot
    verify headroom against the month's billed netAmount, so pay-as-you-go must
    not be assumed funded: Copilot stays Ready=no and shows no misleading
    "$0/<limit>" override. A known $0 spend (add-on present) stays funded; this
    guards only the unknown-spend case."""
    cfg = usage.Config()
    cfg.copilot_spend_limit = 25.0
    cfg.copilot_spend_currency = "$"
    # spent=None -> no add_on block at all -> billed spend is unknown.
    rows = usage.copilot_rows(cfg, _copilot_legacy_json(remaining=0.0, spent=None))
    monthly = next(r for r in rows if r.scope == "monthly")
    assert monthly.gates_ready is True
    assert monthly.guidance_override is None
    assert usage.provider_ready(rows, "Copilot") is False

    # Sanity: a *known* $0 spend (add-on present) is still funded and ready.
    rows_known_zero = usage.copilot_rows(cfg, _copilot_legacy_json(remaining=0.0, spent=0.0))
    monthly_zero = next(r for r in rows_known_zero if r.scope == "monthly")
    assert monthly_zero.gates_ready is False
    assert monthly_zero.guidance_override == "pay-as-you-go $0/25"
    assert usage.provider_ready(rows_known_zero, "Copilot") is True


def test_load_copilot_spend_limit_env_overrides_config(env: dict[str, str], tmp_path: Path) -> None:
    """The env knob wins over any config file so a quick override needs no
    file edit; an absent/blank knob yields no limit."""
    # Point config at a nonexistent file so the loader returns {} and the test
    # never reads an ambient user config.
    base = env | {"LLM_TOOLS_CONFIG": str(tmp_path / "absent.toml")}
    assert usage._load_copilot_spend_limit(base | {"LLM_USAGE_COPILOT_SPEND_LIMIT": "40"}) == (40.0, "$")
    assert usage._load_copilot_spend_limit(
        base | {"LLM_USAGE_COPILOT_SPEND_LIMIT": "40", "LLM_USAGE_COPILOT_SPEND_CURRENCY": "£"}
    ) == (40.0, "£")
    # Non-positive / unparseable -> no declared limit (currency still defaults).
    assert usage._load_copilot_spend_limit(base | {"LLM_USAGE_COPILOT_SPEND_LIMIT": "0"})[0] is None
    assert usage._load_copilot_spend_limit(base | {"LLM_USAGE_COPILOT_SPEND_LIMIT": "nope"})[0] is None


def test_copilot_with_allowance_headroom_ready_without_payg(env: dict[str, str]) -> None:
    """The common case is unchanged: included allowance with headroom gates
    Ready normally and never triggers the pay-as-you-go override."""
    cfg = usage.Config()
    cfg.copilot_spend_limit = None
    rows = usage.copilot_rows(cfg, _copilot_legacy_json(remaining=60.0, spent=0.0))
    monthly = next(r for r in rows if r.scope == "monthly")
    assert monthly.gates_ready is True
    assert monthly.guidance_override is None
    assert usage.provider_ready(rows, "Copilot") is True


def _no_color_cfg() -> "usage.Config":
    cfg = usage.Config()
    cfg.color_enabled = False
    return cfg


def test_render_spent_without_budget_is_left_aligned_amount() -> None:
    cfg = _no_color_cfg()
    cfg.monthly_budget = None
    cfg.budget_currency = "$"
    # No budget -> amount only, left-positioned (number-first like "% rows"),
    # never the old right-aligned "spent $27.4".
    assert usage.render_spent(27.4, "$", cfg) == "$27.4"


def test_render_spent_with_budget_shows_amount_and_bar() -> None:
    cfg = _no_color_cfg()
    cfg.monthly_budget = 50.0
    cfg.budget_currency = "$"
    # $27.4 of $50 -> 54.8% consumed -> 5/10 cells filled, amount on the left.
    assert usage.render_spent(27.4, "$", cfg) == " $27.4 █████░░░░░"


def test_render_spent_mixed_currency_shows_plain_amount() -> None:
    cfg = _no_color_cfg()
    cfg.monthly_budget = 50.0
    cfg.budget_currency = "$"
    # A £ amount has no comparable $ budget -> no bar, just the left amount.
    assert usage.render_spent(12.4, "£", cfg) == "£12.4"


def test_render_spent_missing_currency_uses_budget_currency() -> None:
    cfg = _no_color_cfg()
    cfg.monthly_budget = 50.0
    cfg.budget_currency = "€"
    # Missing spend currency is treated as the configured budget currency, so
    # the amount and guidance do not mix "$" with a non-dollar budget.
    assert usage.render_spent(12.4, None, cfg) == " €12.4 ██░░░░░░░░"
    assert usage.spend_guidance(12.4, None, cfg) == "24.8% of €50"


def test_spend_guidance_shows_share_of_budget() -> None:
    cfg = _no_color_cfg()
    cfg.monthly_budget = 50.0
    cfg.budget_currency = "$"
    assert usage.spend_guidance(27.4, "$", cfg) == "54.8% of $50"
    cfg.monthly_budget = None
    assert usage.spend_guidance(27.4, "$", cfg) == ""


def test_spend_color_thresholds() -> None:
    assert usage.spend_color_code(10) == "0;32"  # green
    assert usage.spend_color_code(75) == "0;33"  # yellow
    assert usage.spend_color_code(95) == "0;31"  # red


def test_render_spent_and_amount_handle_none() -> None:
    cfg = _no_color_cfg()
    cfg.monthly_budget = 50.0
    assert usage.render_spent(None, "$", cfg) == "-"
    assert usage.format_amount(None, "$") == "-"


def test_render_spent_colored_emits_ansi() -> None:
    cfg = _no_color_cfg()
    cfg.color_enabled = True
    cfg.monthly_budget = 50.0
    cfg.budget_currency = "$"
    out = usage.render_spent(48.0, "$", cfg)
    assert out.startswith("\033[0;31m")  # 96% consumed -> red
    assert "█" in out and out.endswith("\033[0m")
    guidance = usage.spend_guidance(48.0, "$", cfg)
    assert guidance.startswith("\033[0;31m") and "96% of $50" in guidance


def test_budget_total_row_sums_spend_rows() -> None:
    cfg = _no_color_cfg()
    cfg.monthly_budget = 50.0
    cfg.budget_currency = "$"
    rows = [
        usage.UsageRow("Kilo", "balance", 1.0, "spent $27.4", None, "kilo", amount=27.4, currency="$", kind="balance", spent=True),
        usage.UsageRow("OpenCode", "balance", 1.0, "spent $4.3", None, "oc", amount=4.3, currency="$", kind="balance", spent=True),
        # A non-spend row and a foreign-currency spend row are both excluded.
        usage.UsageRow("Claude", "5h", 80.0, "80%", None, "claude"),
        usage.UsageRow("Kilo", "balance", 1.0, "spent £9.0", None, "kilo", amount=9.0, currency="£", kind="balance", spent=True),
    ]
    total = usage.budget_total_row(cfg, rows)
    assert total is not None
    assert total.provider == "Budget"
    assert total.amount == 31.7
    assert total.spent is True


def test_budget_total_row_none_without_spend() -> None:
    cfg = _no_color_cfg()
    cfg.monthly_budget = None
    assert usage.budget_total_row(cfg, []) is None


def test_budget_total_row_labels_total_without_budget() -> None:
    cfg = _no_color_cfg()
    cfg.monthly_budget = None
    cfg.budget_currency = "$"
    rows = [usage.UsageRow("Kilo", "spend", 1.0, "$27.4", None, "kilo", amount=27.4, currency="$", kind="balance", spent=True)]
    total = usage.budget_total_row(cfg, rows)
    assert total is not None
    assert total.provider == "Total"  # no budget -> plain total, not "Budget"
    assert total.amount == 27.4
    assert total.reset is None  # no budget -> no reset


def test_render_spent_over_budget_caps_bar_and_flags_red() -> None:
    cfg = _no_color_cfg()
    cfg.monthly_budget = 20.0
    cfg.budget_currency = "$"
    out = usage.render_spent(27.4, "$", cfg)
    assert out == " $27.4 ██████████"  # 137% -> bar fully filled, not overflowing
    assert usage.spend_guidance(27.4, "$", cfg) == "137% of $20"  # overage visible


def test_load_monthly_budget_env_overrides_config() -> None:
    amount, currency = usage._load_monthly_budget(
        {"LLM_USAGE_MONTHLY_BUDGET": "75", "LLM_USAGE_BUDGET_CURRENCY": "€"}
    )
    assert amount == 75.0
    assert currency == "€"


def test_load_monthly_budget_invalid_env_is_ignored(tmp_path: "Path") -> None:
    # Invalid amount + an isolated (empty) config dir -> no budget, default currency.
    amount, currency = usage._load_monthly_budget(
        {"LLM_USAGE_MONTHLY_BUDGET": "lots", "XDG_CONFIG_HOME": str(tmp_path), "LLM_TOOLS_CONFIG": str(tmp_path / "none.toml")}
    )
    assert amount is None
    assert currency == "$"


def test_next_month_epoch_is_first_of_next_month() -> None:
    import datetime as _dt

    # now_epoch 1781587377 == 2026-06-16 -> next reset is 2026-07-01 UTC.
    epoch = usage._next_month_epoch({"LLM_USAGE_NOW_EPOCH": "1781587377"})
    nxt = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
    assert (nxt.year, nxt.month, nxt.day) == (2026, 7, 1)


def test_usage_table_renders_budget_bars_and_blanks(monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib
    from io import StringIO

    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1781587377")
    cfg = _no_color_cfg()
    cfg.monthly_budget = 50.0
    cfg.budget_currency = "$"
    rows = [
        usage.UsageRow("Kilo", "balance", 1.0, "spent $27.4", None, "kilo", amount=27.4, currency="$", kind="balance", spent=True),
    ]
    budget = usage.budget_total_row(cfg, rows)
    assert budget is not None
    rows.append(budget)
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_usage_rows(cfg, rows)
    out = buf.getvalue()
    assert "$27.4 █████░░░░░" in out  # amount on the left + spend bar
    assert "54.8% of $50" in out  # share-of-budget guidance (27.4/50)
    assert "Budget" in out  # the synthetic total row
    # A balance scope has no reset -> the Resets in cell is blank, not "-".
    kilo_line = next(line for line in out.splitlines() if line.startswith("Kilo"))
    assert not kilo_line.rstrip().endswith("-")


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


def test_progress_reporter_anchor_docks_to_fixed_cell() -> None:
    import io

    buf = io.StringIO()
    reporter = usage.ProgressReporter(enabled=True, stream=buf, interval=0.01, anchor=(1, 19))
    reporter.start()
    reporter.begin(6)
    reporter.advance()
    time.sleep(0.05)
    reporter.stop()
    output = buf.getvalue()
    # Draws at the fixed cell with cursor save/restore so body printing below is
    # never disturbed, and never uses the line-relative carriage-return form.
    assert "\x1b7\x1b[1;19H\x1b[K" in output
    assert output.count("\x1b8") >= 1
    assert "\r\x1b[K" not in output
    # Fully erased at the same cell on stop, leaving the header line clean.
    assert output.endswith("\x1b7\x1b[1;19H\x1b[K\x1b8")


def test_render_watch_frame_docks_spinner_right_of_clock(monkeypatch, capsys) -> None:
    # Pretend stdout is a TTY so the inline-spinner redraw path is taken.
    monkeypatch.setattr(usage.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(
        usage,
        "_fetch_provider_data",
        lambda cfg, anchor=None: {
            "codex": {"provider": "codex", "available": False, "reason": "test", "source": "test"},
            **{
                k: usage.unavailable_snapshot(k, "test")
                for k in ("claude", "copilot", "kilo", "opencode", "minimax", "zai")
            },
        },
    )
    cfg = usage.parse_args([])
    cfg.watch_interval = "1"
    cfg.use_service = False
    usage.render_watch_frame(cfg)
    out = capsys.readouterr().out
    # Homes the cursor (no full ESC[2J wipe) and closes the frame with ESC[J.
    assert out.startswith("\x1b[H")
    assert "\x1b[2J" not in out
    assert out.rstrip().endswith("\x1b[J")
    # Header line still carries the clock and gets a per-line clear.
    assert "LLM Usage" in out
    assert "\x1b[K" in out


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
