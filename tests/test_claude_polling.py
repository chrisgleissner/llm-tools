from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

from llm_tools import common


class UsageResponse:
    def __enter__(self) -> UsageResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self) -> bytes:
        return b'{"five_hour":{"utilization":12,"resets_at":"2026-10-10T00:00:00Z"}}'


def credentials(env: dict[str, str]) -> Path:
    path = Path(env["HOME"]) / ".claude" / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "test-token", "refreshToken": "keep-me"}}))
    return path


@pytest.mark.parametrize("env_token", [False, True])
@pytest.mark.parametrize("retry_after", ["0", "-1", "invalid", "nan", "inf", None, "120"])
def test_rate_limit_cooldown_survives_repeated_reads(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, env_token: bool, retry_after: str | None,
) -> None:
    live_env = env | {"LLM_USAGE_NOW_EPOCH": "1000", "LLM_USAGE_LIVE_FETCH_RETRIES": "2"}
    cred = credentials(live_env)
    original = cred.read_bytes()
    if env_token:
        live_env["CLAUDE_CODE_OAUTH_TOKEN"] = "env-token"
    calls = []

    def limited(req: object, timeout: int = 20) -> UsageResponse:
        calls.append(req)
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        raise HTTPError(common.CLAUDE_OAUTH_USAGE_URL, 429, "rate limited", headers, None)

    monkeypatch.setattr(common, "urlopen", limited)
    monkeypatch.setattr(common.time, "sleep", lambda _: pytest.fail("must defer long/invalid Retry-After"))
    assert common.read_claude_api(live_env)["reason"] == "rate-limited"
    wait = 120 if retry_after == "120" else 60
    state = json.loads(common._claude_oauth_rate_limit_path(live_env).read_text())
    assert state["next_allowed_epoch"] == 1000 + wait
    assert common.read_claude_api(live_env | {"LLM_USAGE_NOW_EPOCH": str(1000 + wait - 1)})["reason"] == "rate-limited"
    assert len(calls) == 1
    monkeypatch.setattr(common, "urlopen", lambda *a, **kw: UsageResponse())
    recovered = common.read_claude_api(live_env | {"LLM_USAGE_NOW_EPOCH": str(1000 + wait)})
    assert recovered["five_hour"]["used"] == 12
    assert not common._claude_oauth_rate_limit_path(live_env).exists()
    assert cred.read_bytes() == original


def test_usage_sample_is_shared_only_within_freshness_ttl(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_env = env | {"LLM_USAGE_NOW_EPOCH": "1000"}
    credentials(live_env)
    calls = []

    def fetch(req: object, timeout: int = 20) -> UsageResponse:
        calls.append(req)
        return UsageResponse()

    monkeypatch.setattr(common, "urlopen", fetch)
    assert common.read_claude_api(live_env)["five_hour"]["used"] == 12
    cache = common.usage_cache_dir(live_env) / "claude-usage-api.json"
    os.utime(cache, (1000, 1000))
    assert common.read_claude_api(live_env | {"LLM_USAGE_NOW_EPOCH": "1030"})["five_hour"]["used"] == 12
    assert len(calls) == 1
    assert common.read_claude_api(live_env | {"LLM_USAGE_NOW_EPOCH": "1061"})["five_hour"]["used"] == 12
    assert len(calls) == 2
    # A fully elapsed sample must not bypass the TTL indefinitely.
    os.utime(cache, (1000, 1000))
    assert common.read_claude_api(live_env | {"LLM_USAGE_NOW_EPOCH": "2000000000"}) is not None
    assert len(calls) == 3


def test_concurrent_usage_readers_share_one_fetch(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials(env)
    calls = []
    barrier = threading.Barrier(6)

    def fetch(req: object, timeout: int = 20) -> UsageResponse:
        calls.append(req)
        return UsageResponse()

    def read() -> dict[str, object] | None:
        barrier.wait(timeout=5)
        return common.read_claude_api(env)

    monkeypatch.setattr(common, "urlopen", fetch)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(read) for _ in range(6)]
        for future in futures:
            assert future.result(timeout=5)["five_hour"]["used"] == 12
    assert len(calls) == 1


@pytest.mark.parametrize("now", [1000, 2000000000])
def test_expired_rate_limit_cache_remains_unavailable(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, now: int,
) -> None:
    live_env = env | {"LLM_USAGE_NOW_EPOCH": str(now)}
    credentials(live_env)
    cache = common.usage_cache_dir(live_env) / "claude-usage-api.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(UsageResponse().read())
    os.utime(cache, (now - 301, now - 301))
    common._record_claude_oauth_rate_limit(live_env, 120)
    monkeypatch.setattr(common, "urlopen", lambda *a, **kw: pytest.fail("cooldown must skip network"))
    assert common.read_claude_api(live_env)["reason"] == "rate-limited"


def test_usage_lock_failure_still_reads_provider(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials(env)
    cache_dir = common.usage_cache_dir(env)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "claude-usage-api.lock").mkdir()
    monkeypatch.setattr(common, "urlopen", lambda *a, **kw: UsageResponse())
    assert common.read_claude_api(env)["five_hour"]["used"] == 12
