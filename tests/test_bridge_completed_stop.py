"""A completed mission closes its own idle station; an ordinary stop is still safe afterwards."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

from llm_tools.bridge_runtime import mission_socket_path
from llm_tools.bridge_store import BridgeStore, MissionLock
from llm_tools.terminal import TmuxTerminalBackend
from tests.conftest import scaled


def test_completed_mission_closes_its_station_and_stop_is_a_safe_no_op(tmp_path: Path, env: dict[str, str]) -> None:
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({
        "codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}},
    })
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission_id = store.create_mission(
            "completed station", "Fixture completion", cwd=tmp_path, initial_prompt="Finish once",
            config={"providers": "codex", "routes": "", "command_template": "/bin/true", "frozen_tool_config": "",
                    "verifier": {"argv": [sys.executable, "-c", 'print("passed")'], "timeout_seconds": 5}},
        )["id"]

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "llm_tools.bridge", *args],
            env=env, text=True, capture_output=True, timeout=scaled(15),
        )

    def released() -> bool:
        lock = MissionLock(state / "locks", mission_id)
        try:
            lock.acquire()
        except BlockingIOError:
            return False
        lock.release()
        return True

    backend = TmuxTerminalBackend()
    handle = None
    try:
        started = cli("start", "--mission", mission_id)
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with BridgeStore(state / "bridge.sqlite3") as store:
                status = store.get_mission(mission_id)["status"]
            if status == "COMPLETED" and released():
                break
            time.sleep(0.03)
        assert status == "COMPLETED" and released()
        assert not mission_socket_path(mission_id, env=env).exists(), "a completed mission with no client attached must not keep its station"
        stopped = cli("stop", "--mission", mission_id)
        assert stopped.returncode == 0, stopped.stderr
        assert "No station to close" in stopped.stdout
    finally:
        cli("stop", "--mission", mission_id, "--force")
        if handle is not None:
            backend.stop(handle)
