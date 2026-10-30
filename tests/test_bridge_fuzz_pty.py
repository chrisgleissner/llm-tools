"""Real-terminal chaos smoke test: random bytes and resizes at Bridge's screens.

The control view and the home screen run in a pseudo-terminal while a seeded
stream of terminal noise (random escape sequences, broken and nested bracketed
pastes, wheel reports, function keys, invalid UTF-8, control characters) and
resize signals is written to them. Afterwards the process must still be alive,
must leave on Ctrl-D with status 0, must not have printed a traceback, and the
database must be unchanged.

Bytes that would legitimately end the process or act on the mission are left
out: Ctrl-C, Ctrl-D, Ctrl-Z and Ctrl-\\ (tty signals and end of input), ``q``,
Ctrl-S and F2 (they save an open task composer), and Enter outside a bracketed
paste (an Enter could submit a typed command).
"""

from __future__ import annotations

import fcntl
import os
import pty
import random
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from llm_tools.bridge_store import BridgeStore

from tests.fuzz import generators as gen
from tests.conftest import scaled
from tests.fuzz.harness import db_diff, db_rows

pytestmark = [pytest.mark.fuzz, pytest.mark.pty, pytest.mark.subprocess]

ROOT = Path(__file__).resolve().parents[1]
# tty signal characters and end-of-input, the keys that quit a screen, and Ctrl-S, which saves an open task composer
FORBIDDEN = frozenset({0x03, 0x04, 0x13, 0x1A, 0x1C, ord("q"), ord("Q")})
SAVE_KEYS = {b"\x1bOQ": b"\x1bOP", b"\x1b[12~": b"\x1b[13~", b"\x1b[[B": b"\x1b[[A"}  # F2 saves an open task composer
ENTERS = frozenset({0x0D, 0x0A})
WHEEL_BUTTONS = (35, 64, 65, 66, 67, 68, 96, 97)


def chaos_bytes(rng: random.Random, *, launcher: bool, limit: int = 6000) -> bytes:
    """Seeded terminal noise without the bytes that would end the run or submit a command."""
    drop = FORBIDDEN | ENTERS | ({ord("s"), ord("S")} if launcher else set())
    pieces: list[bytes] = []
    total = 0
    while total < limit:
        roll = rng.random()
        if roll < 0.2:
            piece = gen.paste_block(rng)  # newlines stay: they are inert inside a bracketed paste
            piece = bytes(b for b in piece if b not in FORBIDDEN | ({ord("s"), ord("S")} if launcher else set()))
        else:
            piece = gen.raw_byte_stream(rng, 60, avoid=frozenset(drop), mouse_buttons=WHEEL_BUTTONS)
        pieces.append(piece)
        total += len(piece)
    data = b"".join(pieces)
    for save, other in SAVE_KEYS.items():
        data = data.replace(save, other)
    if launcher:
        # A lone Escape quits the home screen; keep every Escape attached to a following byte.
        data = data.replace(b"\x1b\x1b", b"\x1b[").replace(b"\x1b", b"\x1b[")
        data = data.replace(b"\x1b[[", b"\x1b[")
    return data.rstrip(b"\x1b")


class Screen:
    def __init__(self, master: int, process: subprocess.Popen[bytes]) -> None:
        self.master, self.process = master, process
        self.output = bytearray()

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.01)[0]:
                try:
                    self.output.extend(os.read(self.master, 65536))
                except OSError:
                    return

    def send(self, data: bytes, rng: random.Random) -> None:
        index = 0
        while index < len(data):
            size = rng.choice([1, 3, 8, 24, 64])
            os.write(self.master, data[index:index + size])
            index += size
            self.pump(rng.choice([0.0, 0.002, 0.01]))

    def resize(self, rows: int, columns: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        os.kill(self.process.pid, signal.SIGWINCH)


def run_chaos(argv: list[str], work: Path, state: Path, rng: random.Random, *, launcher: bool, seconds: float) -> tuple[int, bytes, str]:
    env = {**os.environ, "LLM_BRIDGE_STATE_DIR": str(state), "TERM": "xterm-256color", "LLM_BRIDGE_THEME": "dark", "EDITOR": "true",
           "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT), os.environ.get("PYTHONPATH", ""))))}
    env.pop("NO_COLOR", None)
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
    errors = work / "stderr.txt"
    with errors.open("wb") as stderr:
        process = subprocess.Popen(argv, cwd=work, env=env, stdin=slave, stdout=slave, stderr=stderr, close_fds=True)
    os.close(slave)
    screen = Screen(master, process)
    try:
        screen.pump(scaled(1.5))
        assert process.poll() is None, f"exited during start-up: {process.returncode}"
        data = chaos_bytes(rng, launcher=launcher)
        started = time.monotonic()
        index = 0
        while time.monotonic() - started < seconds and index < len(data):
            chunk = data[index:index + rng.randint(1, 96)]
            index += len(chunk)
            os.write(master, chunk)
            if rng.random() < 0.08:
                rows, columns = gen.terminal_size(rng)
                screen.resize(rows, columns)
            screen.pump(rng.choice([0.0, 0.003, 0.01, 0.02]))
            assert process.poll() is None, f"process exited during chaos with status {process.returncode} after {index} bytes: {errors.read_text(errors='replace')[-600:]!r} {bytes(screen.output[-200:])!r}"
        screen.resize(30, 110)
        # An unterminated paste is quarantined after two idle seconds.
        screen.pump(scaled(2.4))
        assert process.poll() is None, f"process exited after the noise with status {process.returncode}: {errors.read_text(errors='replace')[-600:]!r}"
        for attempt in range(14):
            os.write(master, gen.PASTE_END + b"\x15\x04")  # close an open paste, clear the command line, Ctrl-D
            screen.pump(scaled(0.5))
            if process.poll() is not None:
                break
            if attempt % 2:
                os.write(master, b"\x1b")  # back out of an overlay or composer
                screen.pump(scaled(0.3))
        try:
            process.wait(timeout=scaled(5))
        except subprocess.TimeoutExpired:
            pytest.fail(f"Ctrl-D did not end the process; last output: {bytes(screen.output[-300:])!r}")
        return process.returncode, bytes(screen.output), errors.read_text(errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)


def saved_mission(tmp_path: Path) -> tuple[Path, Path, str]:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir()
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("Chaos mission", "Survive terminal noise", cwd=work, initial_prompt="first task", config={"native_tui": True, "providers": "codex"})
        store.add_task(str(mission["id"]), "second task", priority=2)
    return state, work, str(mission["id"])


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_control_view_survives_terminal_noise(tmp_path: Path, seed: int) -> None:
    state, work, mission = saved_mission(tmp_path)
    before = db_rows(state / "bridge.sqlite3")
    status, output, stderr = run_chaos([sys.executable, os.environ.get("FUZZ_TRACE_MAIN", "-m"), *([] if "FUZZ_TRACE_MAIN" in os.environ else ["llm_tools.bridge"]), "control", "--mission", mission], work, state, random.Random(f"pty-control-{seed}"), launcher=False, seconds=1.8)
    assert "Traceback" not in stderr, stderr[-1500:]
    assert b"Traceback" not in output, output[-800:]
    assert status == 0, (status, stderr[-500:])
    after = db_rows(state / "bridge.sqlite3")
    assert after == before, db_diff(before, after)


@pytest.mark.parametrize("seed", [1, 2])
def test_home_screen_survives_terminal_noise(tmp_path: Path, seed: int) -> None:
    state, work, _mission = saved_mission(tmp_path)
    before = db_rows(state / "bridge.sqlite3")
    status, output, stderr = run_chaos([sys.executable, "-m", "llm_tools.bridge"], work, state, random.Random(f"pty-home-{seed}"), launcher=True, seconds=1.8)
    assert "Traceback" not in stderr, stderr[-1500:]
    assert b"Traceback" not in output, output[-800:]
    assert status == 0, (status, stderr[-500:])
    after = db_rows(state / "bridge.sqlite3")
    assert after == before, db_diff(before, after)


@pytest.mark.parametrize("screen", ["home", "control"])
def test_ctrl_s_does_not_freeze_the_screen(tmp_path: Path, screen: str) -> None:
    """Ctrl-S is XOFF to a terminal in flow-control mode: output stops until Ctrl-Q unless the screen disables IXON."""
    state, work, mission = saved_mission(tmp_path)
    env = {**os.environ, "LLM_BRIDGE_STATE_DIR": str(state), "TERM": "xterm-256color", "LLM_BRIDGE_THEME": "dark",
           "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT), os.environ.get("PYTHONPATH", ""))))}
    argv = [sys.executable, "-m", "llm_tools.bridge"] + (["control", "--mission", mission] if screen == "control" else [])
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
    process = subprocess.Popen(argv, cwd=work, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    view = Screen(master, process)
    try:
        view.pump(scaled(1.5))
        mark = len(view.output)
        os.write(master, b"\x13")
        view.pump(0.3)
        os.write(master, b"n" if screen == "home" else b"status")
        view.pump(scaled(1.5))
        fresh = bytes(view.output[mark:])
        assert (b"Objective" in fresh) if screen == "home" else (len(fresh) > 50), f"screen froze after Ctrl-S: {fresh[-200:]!r}"
        os.write(master, b"\x11\x15\x04\x04\x04")
        view.pump(1.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
