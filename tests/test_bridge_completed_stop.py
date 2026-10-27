"""A completed controller's retained terminal still needs an ordinary stop."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

from llm_tools.bridge_runtime import mission_socket_path
from llm_tools.bridge_store import BridgeStore, MissionLock
from llm_tools.terminal import TmuxTerminalBackend


def test_normal_stop_closes_completed_mission_station_without_force(tmp_path: Path, env: dict[str, str]) -> None:
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
            env=env, text=True, capture_output=True, timeout=15,
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
        handle = backend.open_existing(mission_socket_path(mission_id, env=env), f"mission-{mission_id}")
        stopped = cli("stop", "--mission", mission_id)
        assert stopped.returncode == 0, stopped.stderr
        assert not backend.session_exists(handle), "ordinary stop acknowledged success but retained the completed mission's station"
        assert not backend.pane_is_alive(handle, pane="bridge")
    finally:
        cli("stop", "--mission", mission_id, "--force")
        if handle is not None:
            backend.stop(handle)
