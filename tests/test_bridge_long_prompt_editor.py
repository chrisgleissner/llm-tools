"""An unchanged real editor roundtrip must preserve full task instructions."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time

from llm_tools.bridge_store import BridgeStore


def test_real_curses_amendment_preserves_long_prompt_and_whitespace(tmp_path: Path, env: dict[str, str]) -> None:
    state = tmp_path / "state"
    database = state / "bridge.sqlite3"
    original = "Initial instruction\n\tIndented instruction\r\n" + "x" * 20000 + "\nTAIL_INSTRUCTION\n"
    with BridgeStore(database) as store:
        mission = store.create_mission("Editor preservation", original, cwd=tmp_path, initial_prompt=original)
        mission_id, task_id = mission["id"], mission["initial_task_id"]
    env.update(LLM_BRIDGE_STATE_DIR=str(state), TERM="xterm-256color", VISUAL="/bin/true")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "llm_tools.bridge", "control", "--mission", mission_id],
        stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
    )
    os.close(slave)

    def read_until(marker: bytes) -> None:
        output = bytearray()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is None:
            readable, _, _ = select.select([master], [], [], 0.03)
            if readable:
                output.extend(os.read(master, 65536))
                if marker in output:
                    return
        raise AssertionError(f"real control screen did not render {marker!r}")

    try:
        read_until(b"Editor preservation")
        os.write(master, b"\x1bOQ")  # xterm F2: Orders.
        read_until(b"Orders:")
        os.write(master, b"\x12")  # Ctrl-R with a no-op editor.
        amended = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with BridgeStore(database) as store:
                task = store.get_task(task_id)
            if task["revision"] > 1:
                amended = task["pending"].get("prompt")
                break
            readable, _, _ = select.select([master], [], [], 0.03)
            if readable:
                os.read(master, 65536)
        assert amended is not None, "the real editor command did not persist an amendment"
        assert amended == original, "an unchanged edit lost task text or significant whitespace"
    finally:
        if process.poll() is None:
            os.write(master, b"\x04")
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=2)
        os.close(master)
