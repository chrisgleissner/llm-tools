"""Background capacity observation shares one provider read per interval across Bridge processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from llm_tools import bridge_refresh, common


def _env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache"), "HOME": str(tmp_path / "home")}


def test_many_concurrent_readers_cause_one_live_read(tmp_path: Path) -> None:
    env = _env(tmp_path)
    reads = []

    def live() -> dict:
        reads.append(1)
        time.sleep(0.2)  # a slow provider: every other reader is waiting on the lock
        return {"provider": "claude", "available": True, "reads": len(reads)}

    results: list[dict] = []
    threads = [
        threading.Thread(target=lambda: results.append(common.shared_usage_snapshot("claude", env, 60.0, live)))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(reads) == 1
    assert len(results) == 8 and all(result["reads"] == 1 for result in results)


def test_cached_snapshot_expires_after_max_age_and_is_private(tmp_path: Path) -> None:
    env = _env(tmp_path)
    count = {"n": 0}

    def live() -> dict:
        count["n"] += 1
        return {"provider": "codex", "n": count["n"]}

    first = common.shared_usage_snapshot("codex", env, 0.3, live)
    assert common.shared_usage_snapshot("codex", env, 0.3, live) == first
    time.sleep(0.4)
    second = common.shared_usage_snapshot("codex", env, 0.3, live)
    assert second["n"] == 2

    directory = common.shared_snapshot_dir(env)
    assert directory.stat().st_mode & 0o077 == 0
    assert (directory / "codex.json").stat().st_mode & 0o077 == 0
    assert json.loads((directory / "codex.json").read_text())["snapshot"] == second


def test_separate_processes_share_the_cache(tmp_path: Path) -> None:
    env = _env(tmp_path)
    script = (
        "import sys, time\n"
        "from llm_tools import common\n"
        "def live():\n"
        "    open(sys.argv[1], 'a').write('x')\n"
        "    time.sleep(0.3)\n"
        "    return {'provider': 'claude', 'available': True}\n"
        "print(common.shared_usage_snapshot('claude', dict(__import__('os').environ), 60.0, live)['available'])\n"
    )
    marker = tmp_path / "live-reads"
    root = str(Path(__file__).resolve().parents[1])
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(marker)], env={**env, "PYTHONPATH": root}, stdout=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    outputs = [process.communicate(timeout=20)[0].strip() for process in processes]
    assert outputs == ["True"] * 4
    assert marker.read_text() == "x"  # four controllers, one live provider read


def test_unusable_cache_directory_degrades_to_a_direct_read(tmp_path: Path) -> None:
    blocked = tmp_path / "cache"
    blocked.write_text("a file where the cache directory should be")
    env = {**os.environ, "XDG_CACHE_HOME": str(blocked)}

    assert common.shared_usage_snapshot("claude", env, 60.0, lambda: {"provider": "claude", "ok": 1}) == {"provider": "claude", "ok": 1}


def test_provider_names_cannot_escape_the_cache_directory(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert common.shared_usage_snapshot("../../evil", env, 60.0, lambda: {"ok": 1}) == {"ok": 1}
    assert not (tmp_path / "evil.json").exists()


def test_shared_reads_apply_only_inside_the_context_and_never_to_injected_usage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("LLM_SCHEDULER_USAGE_JSON", raising=False)
    live = {"n": 0}

    def fake_live(provider: str, env: dict) -> dict:
        live["n"] += 1
        return {"provider": provider, "n": live["n"]}

    monkeypatch.setattr(common, "_live_usage_snapshot", fake_live)
    assert common.usage_snapshot_for_provider("claude")["n"] == 1
    assert common.usage_snapshot_for_provider("claude")["n"] == 2  # foreground reads stay live
    with common.shared_usage_reads(60):
        assert common.usage_snapshot_for_provider("claude")["n"] == 3
        assert common.usage_snapshot_for_provider("claude")["n"] == 3  # served from the shared cache
        monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", "{}")
        assert common.usage_snapshot_for_provider("claude")["n"] == 4  # injected test data bypasses the cache
    monkeypatch.delenv("LLM_SCHEDULER_USAGE_JSON")
    assert common.usage_snapshot_for_provider("claude")["n"] == 5


def test_refresh_observation_reads_through_the_shared_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LLM_SCHEDULER_USAGE_JSON", raising=False)
    live = {"n": 0}

    def fake_live(provider: str, env: dict) -> dict:
        live["n"] += 1
        return {"provider": provider, "available": True, "five_hour": {"used": 10, "reset": "2099-01-01T00:00:00Z"}}

    monkeypatch.setattr(common, "_live_usage_snapshot", fake_live)
    mission = {"config": {"providers": "claude", "routes": ""}}
    for _ in range(3):  # three controllers observing the same provider within one interval
        bridge_refresh.refresh_capacity_observation(mission)
    assert live["n"] == 1
