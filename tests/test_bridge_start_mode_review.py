"""Starting a durable mission must preserve its chosen terminal mode."""

from pathlib import Path
import json

import pytest

from llm_tools import bridge
from llm_tools import bridge_runtime


@pytest.mark.parametrize("mode", ["headless", "native_tui"])
def test_start_without_mode_override_uses_persisted_mission_mode(tmp_path: Path, monkeypatch, mode: str) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        mission = store.create_mission(
            "stored mode", "objective", cwd=tmp_path,
            config={"headless": mode == "headless", "native_tui": mode == "native_tui"},
        )
    launches = []
    monkeypatch.setattr(bridge, "start_mission", lambda mission_id, **kwargs: launches.append((mission_id, kwargs)) or 0)

    assert bridge.main(["start", "--mission", mission["id"]]) == 0

    assert launches[0][1]["headless"] is None
    assert launches[0][1]["native_tui"] is None


@pytest.mark.parametrize("stored_mode", ["headless", "native_tui"])
@pytest.mark.parametrize("override", [None, "headless", "native_tui"])
def test_start_launches_controller_with_persisted_mode_or_explicit_override(
    tmp_path: Path, monkeypatch, stored_mode: str, override: str | None,
) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        mission = store.create_mission(
            "stored mode", "objective", cwd=tmp_path,
            config={"headless": stored_mode == "headless", "native_tui": stored_mode == "native_tui"},
        )
    if override == "native_tui":
        monkeypatch.setattr(bridge_runtime.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)

    launched: dict[str, object] = {}

    class FakeController:
        pid = 43210
        returncode = None

        def poll(self):
            return self.returncode

    def fake_popen(argv, **_kwargs):
        launched["argv"] = list(argv)
        token = argv[argv.index("--startup-token") + 1]
        ready = bridge_runtime._ready_file(bridge_runtime._runtime_directory(mission["id"]))
        ready.write_text(json.dumps({"pid": 43210, "startup_token": token, "status": "RUNNING"}), encoding="utf-8")
        return FakeController()

    monkeypatch.setattr(bridge_runtime.subprocess, "Popen", fake_popen)
    argv = ["start", "--mission", mission["id"]]
    if override:
        argv.append("--native" if override == "native_tui" else "--headless")

    assert bridge.main(argv) == 0

    command = launched["argv"]
    assert ("--headless" in command) == (override == "headless" or (override is None and stored_mode == "headless"))
    assert ("--native" in command) == (override == "native_tui" or (override is None and stored_mode == "native_tui"))
