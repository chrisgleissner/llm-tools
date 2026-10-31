"""Real tmux: ``llm-bridge gc`` and the picker's x key close idle stations of finished missions safely."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from llm_tools import bridge_runtime
from llm_tools.bridge_runtime import mission_socket_path
from llm_tools.bridge_store import BridgeStore
from llm_tools.terminal import TmuxTerminalBackend
from tests.conftest import scaled, wait_for as _shared_wait_for

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")

_SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
_EXIT = ["/bin/sh", "-c", "exit 0"]


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    return directory


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    return bool(_shared_wait_for(predicate, timeout, 0.05))


def _finished_mission_with_station(state: Path, tmp_path: Path, name: str, provider: list[str], native: bool = True):
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission_id = str(store.create_mission(name, "objective", cwd=tmp_path, initial_prompt="work", config={"native_tui": native})["id"])
        store.set_mission_status(mission_id, "RUNNING")
        store.db.execute("UPDATE missions SET status='COMPLETED' WHERE id=?", (mission_id,))
        store.db.commit()
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=provider, bridge_argv=_SLEEP, cwd=tmp_path,
        socket_path=mission_socket_path(mission_id), session_name=bridge_runtime._session_name(mission_id),
    )
    return mission_id, backend, handle


def test_gc_closes_idle_stations_and_keeps_one_with_a_live_provider(state: Path, tmp_path: Path) -> None:
    idle, backend, idle_handle = _finished_mission_with_station(state, tmp_path, "Idle", _EXIT)
    busy, _backend, busy_handle = _finished_mission_with_station(state, tmp_path, "Busy", _SLEEP)
    try:
        assert _wait_for(lambda: not backend.pane_is_alive(idle_handle, pane="provider"))
        result = subprocess.run([sys.executable, "-m", "llm_tools.bridge", "gc", "--json"], capture_output=True, text=True, timeout=scaled(60), check=True)
        payload = json.loads(result.stdout)
        assert {item["id"]: item["result"] for item in payload["stations"]} == {idle: "closed", busy: "provider-alive"}
        assert payload["closed"] == 1
        assert not idle_handle.socket_path.exists() and backend.pane_is_alive(busy_handle, pane="provider")
        human = subprocess.run([sys.executable, "-m", "llm_tools.bridge", "gc"], capture_output=True, text=True, timeout=scaled(60), check=True).stdout
        assert "provider process is still running; station kept  Busy" in human and human.strip().endswith("0 of 1 station closed")
        listing = json.loads(subprocess.run([sys.executable, "-m", "llm_tools.bridge", "missions", "--json"],
                                            capture_output=True, text=True, timeout=60, check=True).stdout)["missions"]
        by_id = {m["id"]: m for m in listing}
        assert by_id[busy]["station_idle"] is True and by_id[busy]["live"] is False and by_id[idle]["station"] is False
    finally:
        for handle in (busy_handle, idle_handle):
            try:
                backend.stop(handle)
            except (OSError, RuntimeError, PermissionError, subprocess.SubprocessError):
                pass


def _read_until(master: int, marker: bytes, output: bytearray, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + scaled(timeout)
    while marker not in output and time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break
    assert marker in output, f"missing {marker!r}: {bytes(output)[-600:]!r}"


def test_picker_x_then_y_closes_the_station_in_a_real_terminal(state: Path, tmp_path: Path) -> None:
    mission_id, backend, handle = _finished_mission_with_station(state, tmp_path, "Lingering", _EXIT)
    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - replaced by exec
        os.environ.update(TERM="xterm-256color", LANG="C.UTF-8")
        os.execvp(sys.executable, [sys.executable, "-m", "llm_tools.bridge", "missions", "--pick"])
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    output = bytearray()
    try:
        assert _wait_for(lambda: not backend.pane_is_alive(handle, pane="provider"))
        _read_until(master, b"station idle", output)
        assert b"0 live" in output or b" 0 live" in bytes(output)
        os.write(master, b"x")
        _read_until(master, b"Close the idle station of Lingering?", output)
        os.write(master, b"y")
        _read_until(master, b"Station closed: Lingering", output)
        assert _wait_for(lambda: not handle.socket_path.exists())
        os.write(master, b"\x1b")
        assert _wait_for(lambda: os.waitpid(pid, os.WNOHANG)[0] == pid, timeout=10)
        pid = 0
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
        os.close(master)
        try:
            backend.stop(handle)
        except (OSError, RuntimeError, PermissionError, subprocess.SubprocessError):
            pass
