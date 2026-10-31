"""Real-terminal checks for the colour guide: ASCII fallback, attributes-only mono and the theme hint."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled, wait_for

DOWN, ENTER, CTRL_D = b"\x1bOB", b"\r", b"\x04"
ESCAPES = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\([A-Z]|\x1b[=>]|\x1b\][^\x07]*\x07")


def run(env: dict[str, str], work: Path, steps: list[tuple[bytes, float]], *, size: tuple[int, int] = (30, 110)) -> bytes:
    """Run the launcher in a pseudo-terminal, send ``(keys, wait)`` steps, and return everything it wrote."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))
    root = str(Path(__file__).resolve().parents[1])
    env = {**env, "PYTHONPATH": root + os.pathsep + env.get("PYTHONPATH", "")}
    process = subprocess.Popen([sys.executable, "-m", "llm_tools.bridge"], cwd=work, env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()

    def pump(seconds: float, *, settle: float = 0.4) -> None:
        """Read the terminal for up to ``seconds`` (scaled); return early once the program has
        written something and then been silent for ``settle`` seconds, or has exited."""
        deadline = time.monotonic() + scaled(seconds)
        before = len(output)
        last_write = time.monotonic()
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.03)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    return
                last_write = time.monotonic()
            elif len(output) > before and time.monotonic() - last_write >= scaled(settle):
                return
            elif process.poll() is not None:
                return

    try:
        pump(2.5, settle=0.8)  # startup draws more than once
        for keys, wait in steps:
            os.write(master, keys)
            pump(wait)
        for _ in range(4):  # leave the console, then the launcher
            if process.poll() is not None:
                break
            os.write(master, CTRL_D)
            pump(0.6)
        try:
            process.wait(timeout=scaled(5))
        except subprocess.TimeoutExpired:
            process.kill()
        pump(0.1, settle=0)  # bytes the program wrote just before exiting
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=scaled(3))
        os.close(master)
    return bytes(output)


def tmux_screen(env: dict[str, str], work: Path, tmp_path: Path, keys: list[str], *, size: tuple[int, int] = (30, 110), ansi: bool = False,
                until: Callable[[str], bool] | None = None) -> str:
    """Run the launcher inside a scratch tmux and return the final screen (a terminal emulator for the diffed output)."""
    socket = tmp_path / "screen.sock"
    root = str(Path(__file__).resolve().parents[1])
    command = f"cd {work} && PYTHONPATH={root} exec {sys.executable} -m llm_tools.bridge"
    base = ["tmux", "-S", str(socket), "-f", "/dev/null"]
    subprocess.run([*base, "-u", "new-session", "-d", "-x", str(size[1]), "-y", str(size[0]), command], env=env, check=True)
    def capture() -> str:
        # Escape sequences included: a selection move changes attributes only, not text.
        return subprocess.run([*base, "capture-pane", "-p", "-e"], check=True, capture_output=True, text=True).stdout

    def settle(previous: str | None, limit: float, quiet: float = 0.3) -> None:
        """Wait until the screen is non-blank (and differs from ``previous`` when given) and has stopped changing."""
        deadline = time.monotonic() + scaled(limit)
        last, since = capture(), time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.05)
            now = capture()
            if now != last:
                last, since = now, time.monotonic()
            elif last.strip() and (previous is None or last != previous) and time.monotonic() - since >= scaled(quiet):
                return

    try:
        settle(None, 4.6, quiet=0.8)  # startup draws more than once
        for key in keys:
            before = capture()
            subprocess.run([*base, "send-keys", key], check=True)
            settle(before, 2.0)
        if until is not None and keys:
            # A key sent while the program is still starting can be discarded by curses; the
            # observable effect of the last key is awaited, and the key repeated until it shows.
            for _attempt in range(6):
                if wait_for(lambda: until(capture()), 1.5):
                    break
                subprocess.run([*base, "send-keys", keys[-1]], check=True)
        args = ["capture-pane", "-p"] + (["-e"] if ansi else [])
        return subprocess.run([*base, *args], check=True, capture_output=True, text=True).stdout
    finally:
        subprocess.run([*base, "kill-server"], check=False, capture_output=True)


@pytest.fixture()
def seeded(tmp_path, env):
    """A state directory with one mission, and an environment for a 256-colour terminal."""
    work = tmp_path / "работа"
    work.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    with BridgeStore(state_dir / "bridge.sqlite3") as store:
        store.create_mission("Überprüfung 日本語", "objective", cwd=work)
    env.update(LLM_BRIDGE_STATE_DIR=str(state_dir), TERM="xterm-256color", LLM_TOOLS_NO_INHIBIT="1", LLM_BRIDGE_NO_OSC="1")
    for name in ("COLORFGBG", "LLM_BRIDGE_THEME", "LLM_BRIDGE_BACKGROUND", "NO_COLOR", "LC_CTYPE", "PYTHONUTF8", "PYTHONIOENCODING",
                 "PYTHONCOERCECLOCALE", "TMUX", "TMUX_PANE"):
        env.pop(name, None)
    return env, work


OPEN_CONSOLE = [(DOWN, 0.4), (ENTER, 2.0)]


def test_console_opened_from_the_home_screen_is_pure_ascii_in_the_c_locale(seeded, tmp_path) -> None:
    env, work = seeded
    env.update(LANG="C", LC_ALL="C")
    output = run(env, work, OPEN_CONSOLE)
    assert [byte for byte in output if byte > 0x7F] == [], output[-600:]
    screen = tmux_screen(env, work, tmp_path, ["Down", "Enter"])
    assert screen.splitlines()[0].startswith("LLM BRIDGE | | ") and "o CREATED" in screen.splitlines()[0]  # chip and state glyph fall back
    assert "Ctrl-K actions" in screen and "?" in screen.splitlines()[-1]
    screen.encode("ascii")


def test_console_state_glyphs_and_chip_appear_in_a_utf8_locale(seeded, tmp_path) -> None:
    env, work = seeded
    env.update(LANG="C.UTF-8", LC_ALL="C.UTF-8")
    screen = tmux_screen(env, work, tmp_path, ["Down", "Enter"])
    assert screen.splitlines()[0].startswith("LLM BRIDGE · ▌ Überprüfung 日本語 · ○ CREATED")
    assert "Controller" in screen and "Ctrl-K actions" in screen


def sgr_codes(output: bytes) -> list[int]:
    codes: list[int] = []
    for params in re.findall(rb"\x1b\[([0-9;]*)m", output):
        codes.extend(int(part) if part else 0 for part in params.split(b";"))
    return codes


def explicit_colours(output: bytes) -> list[str]:
    """SGR parameters that pick a colour (as opposed to default colours and attributes)."""
    found: list[str] = []
    for params in re.findall(rb"\x1b\[([0-9;]*)m", output):
        parts = [int(part) if part else 0 for part in params.split(b";")]
        for index, code in enumerate(parts):
            if 30 <= code <= 37 or 40 <= code <= 47 or 90 <= code <= 97 or 100 <= code <= 107:
                found.append(f"{code}")
            elif code in (38, 48):
                found.append(f"{code};{parts[index + 1:index + 3]}")
    return found


def test_no_color_emits_attributes_only_and_keeps_the_terminal_background(seeded) -> None:
    env, work = seeded
    env.update(LANG="C.UTF-8", LC_ALL="C.UTF-8", NO_COLOR="1")
    output = run(env, work, OPEN_CONSOLE)
    assert b"CREATED" in output
    assert explicit_colours(output) == [], explicit_colours(output)[:10]  # no 37m/40m/38;5/48;5 slab on a light terminal
    codes = set(sgr_codes(output))
    assert {1, 7} & codes  # bold and reverse still carry emphasis
    assert 49 in codes or b"\x1b[39;49m" in output  # default colours are enabled, so pair 0 follows the terminal


def test_colour_mode_uses_explicit_palette_colours(seeded) -> None:
    env, work = seeded
    env.update(LANG="C.UTF-8", LC_ALL="C.UTF-8", LLM_BRIDGE_THEME="dark")
    output = run(env, work, OPEN_CONSOLE)
    assert any(item.startswith("38;") for item in explicit_colours(output))
    assert b"48;5;25" in output  # the selection bar is the one filled background


def test_undetected_background_shows_the_theme_hint_once(seeded, tmp_path) -> None:
    env, work = seeded
    env.update(LANG="C.UTF-8", LC_ALL="C.UTF-8")
    first = tmux_screen(env, work, tmp_path, [])
    assert "theme: dark (guessed) · set LLM_BRIDGE_THEME=light|dark" in first
    after_a_key = tmux_screen(env, work, tmp_path, ["Down"], until=lambda screen: "guessed" not in screen)
    assert "guessed" not in after_a_key  # shown until the first key
    env["COLORFGBG"] = "0;15"
    assert "guessed" not in tmux_screen(env, work, tmp_path, [])
    env.pop("COLORFGBG")
    env["LLM_BRIDGE_THEME"] = "light"
    assert "guessed" not in tmux_screen(env, work, tmp_path, [])
