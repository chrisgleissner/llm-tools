"""Detaching from a mission station prints a Bridge message instead of tmux's session-name line."""

from __future__ import annotations

import fcntl
import os
import pty
import shutil
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from llm_tools.bridge_store import BridgeStore
from llm_tools.terminal import TmuxTerminalBackend, detach_notice
from tests.conftest import scaled

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")

_SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
MISSION = "f292f491" + "0" * 24


def _attach_and_detach(tmp_path: Path, session_name: str, env: dict[str, str], *, finish: str | None = None) -> tuple[int, str]:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=_SLEEP, bridge_argv=_SLEEP, cwd=tmp_path,
        socket_path=tmp_path / "s.sock", session_name=session_name,
    )
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    code = (
        "from pathlib import Path; from llm_tools.terminal import TerminalHandle,TmuxTerminalBackend; h=TerminalHandle("
        + ",".join([
            f"Path({str(handle.socket_path)!r})", f"Path({str(handle.config_path)!r})", repr(handle.session_name),
            repr(handle.provider_pane), repr(handle.bridge_pane), str(handle.bridge_rows),
        ])
        + "); raise SystemExit(TmuxTerminalBackend().attach(h))"
    )

    def controlling_terminal() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    process = subprocess.Popen(
        [sys.executable, "-c", code], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**env, "TERM": "xterm-256color"}, preexec_fn=controlling_terminal,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not backend.writable_clients(handle):
            time.sleep(0.05)
        assert backend.writable_clients(handle), "the client never attached"
        if finish:
            with BridgeStore(Path(env["LLM_BRIDGE_STATE_DIR"]) / "bridge.sqlite3") as store:
                store.set_mission_status(MISSION, finish)
        backend.detach_client(handle)
        stdout, _stderr = process.communicate(timeout=scaled(10))
        return process.returncode, stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=scaled(5))
        backend.stop(handle)
        os.close(master)
        os.close(slave)


@pytest.fixture()
def bridge_env(tmp_path: Path, monkeypatch) -> dict[str, str]:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.create_mission("c64bridge", "objective", cwd=tmp_path, mission_id=MISSION, initial_prompt="task")
    return {**os.environ, "LLM_BRIDGE_STATE_DIR": str(state)}


def test_detach_from_a_mission_station_says_it_keeps_running(tmp_path: Path, bridge_env: dict[str, str]) -> None:
    code, out = _attach_and_detach(tmp_path, f"mission-{MISSION}", bridge_env)

    assert code == 0
    assert out == "Detached from mission f292f491 (c64bridge) · it keeps running · llm-bridge attach to return\n"
    assert "[detached" not in out and MISSION not in out


def test_detach_from_a_finished_mission_does_not_promise_it_keeps_running(tmp_path: Path, bridge_env: dict[str, str]) -> None:
    code, out = _attach_and_detach(tmp_path, f"mission-{MISSION}", bridge_env, finish="CANCELLED")

    assert code == 0
    assert out == "Detached from mission f292f491 (c64bridge) · the mission is cancelled\n"


def test_other_sessions_keep_tmux_s_own_line(tmp_path: Path, bridge_env: dict[str, str]) -> None:
    code, out = _attach_and_detach(tmp_path, "plain-session", bridge_env)

    assert code == 0
    assert out == "[detached (from session plain-session)]\n"


def test_notice_without_a_saved_mission_still_names_the_station(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "empty-state"))

    assert detach_notice("mission-" + "ab12cd34" + "0" * 24) == "Detached from mission ab12cd34 · it keeps running · llm-bridge attach to return"
    assert detach_notice("not-a-mission") == ""
