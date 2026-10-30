"""Attaching to a station draws the intended pane once and does not make the provider repaint.

The tests attach a real terminal (a pseudo-terminal of a different size than the station) to a real
tmux station, record everything tmux writes to that terminal, and read the sizes the provider
process was given through SIGWINCH.  A resize after the first frame is what makes a provider
redraw twice (a half-scrolled screen), so the provider's resize log is part of the evidence.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from llm_tools.terminal import TmuxTerminalBackend
from tests.conftest import scaled, wait_for

pytestmark = [pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")]

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDER_MARK = "PROVIDER_PANE_MARK"
CONSOLE_MARK = "CONSOLE_PANE_MARK"

# Prints its mark, then appends "<mark> <lines>x<columns>" to a log at start and at every SIGWINCH.
_PANE_PROGRAM = (
    "import os, signal, sys, time\n"
    "def log(*_):\n"
    "    size = os.get_terminal_size()\n"
    "    open(sys.argv[2], 'a').write(f'{sys.argv[1]} {size.lines}x{size.columns}\\n')\n"
    "signal.signal(signal.SIGWINCH, log)\n"
    "print(sys.argv[1], flush=True)\n"
    "log()\n"
    "time.sleep(120)\n"
)

_ATTACH_CHILD = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from llm_tools.terminal import TerminalHandle, TmuxTerminalBackend\n"
    "fields = json.loads(sys.argv[1])\n"
    "handle = TerminalHandle(Path(fields['socket']), Path(fields['config']), fields['session'], fields['provider'], fields['bridge'], fields['rows'])\n"
    "sys.exit(TmuxTerminalBackend().attach(handle))\n"
)


def _start(tmp_path: Path):
    log = tmp_path / "resize.log"
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=[sys.executable, "-c", _PANE_PROGRAM, PROVIDER_MARK, str(log)],
        bridge_argv=[sys.executable, "-c", _PANE_PROGRAM, CONSOLE_MARK, str(log)],
        cwd=tmp_path,
        socket_path=tmp_path / "frames.sock",
        session_name="frames",
        size=(100, 30),
    )
    return backend, handle, log


def _entries(log: Path, who: str) -> list[str]:
    if not log.exists():
        return []
    return [line.split()[1] for line in log.read_text().splitlines() if line.startswith(who + " ")]


def _settled_entries(log: Path, who: str, quiet: float = 0.6) -> list[str]:
    """The provider's resize log once tmux has delivered everything it queued (it paces resizes)."""
    seen, since = _entries(log, who), time.monotonic()
    deadline = time.monotonic() + scaled(15)
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = _entries(log, who)
        if now != seen:
            seen, since = now, time.monotonic()
        elif time.monotonic() - since >= scaled(quiet):
            return seen
    raise AssertionError(f"the provider kept being resized: {seen}")


def _attach_stream(handle, rows: int, columns: int) -> bytes:
    """Run ``TmuxTerminalBackend.attach`` on a pseudo-terminal of the given size; return what tmux drew."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def controlling_terminal() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    fields = {
        "socket": str(handle.socket_path), "config": str(handle.config_path), "session": handle.session_name,
        "provider": handle.provider_pane, "bridge": handle.bridge_pane, "rows": handle.bridge_rows,
    }
    env = {key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}
    env["TERM"] = "xterm-256color"
    child = subprocess.Popen(
        [sys.executable, "-c", _ATTACH_CHILD, json.dumps(fields)], cwd=REPO_ROOT, env=env,
        stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=controlling_terminal,
    )
    os.close(slave)
    stream = bytearray()

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    stream.extend(os.read(master, 65536))
                except OSError:
                    return

    try:
        deadline = time.monotonic() + scaled(10)
        while PROVIDER_MARK.encode() not in stream and time.monotonic() < deadline and child.poll() is None:
            pump(0.05)
        pump(scaled(1.0))  # the 50 ms client-resized hook and any repaint it causes land well inside this
    finally:
        child.terminate()
        try:
            child.wait(5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        os.close(master)
    return bytes(stream)


@pytest.mark.parametrize("last_pane", ("provider", "console"))
def test_attach_lands_on_the_provider_pane_with_one_resize_and_one_clear(tmp_path: Path, last_pane: str) -> None:
    backend, handle, log = _start(tmp_path)
    try:
        assert wait_for(lambda: _entries(log, PROVIDER_MARK) and _entries(log, CONSOLE_MARK), scaled(5), 0.05)
        if last_pane == "console":
            backend.focus(handle, pane="bridge", expand=True)  # detached while the console was shown

        # What `llm-bridge attach` from the home screen or the picker does for a running mission.
        backend.focus(handle, pane="provider", expand=True)
        before = len(_settled_entries(log, PROVIDER_MARK))
        stream = _attach_stream(handle, rows=40, columns=140)
        text = stream.decode("utf-8", "replace")

        first = text.find(PROVIDER_MARK)
        assert first >= 0, f"the provider pane was never drawn: {text[-400:]!r}"
        assert CONSOLE_MARK not in text, "the console pane was drawn while attaching"
        assert text[:first].count("\x1b[2J") == 1, "the screen was cleared more than once before the provider pane appeared"
        assert text.count("\x1b[2J") == 1, "the screen was cleared again after the first frame"

        # The provider is resized once, to the size of the attaching terminal (40 rows less the status row).
        # Leaving the zoom and returning to it, or sizing the window after the client is attached, would add
        # entries such as 32x140 or 30x100, and the provider would repaint for each.
        sizes = _entries(log, PROVIDER_MARK)[before:]
        assert sizes == ["39x140"], sizes
    finally:
        backend.stop(handle)


def test_focus_on_the_pane_that_is_already_shown_does_not_resize_the_provider(tmp_path: Path) -> None:
    backend, handle, log = _start(tmp_path)
    try:
        assert wait_for(lambda: _entries(log, PROVIDER_MARK), scaled(5), 0.05)
        before = _settled_entries(log, PROVIDER_MARK)
        for _ in range(3):
            backend.focus(handle, pane="provider", expand=True)
        assert _settled_entries(log, PROVIDER_MARK) == before
        zoomed = subprocess.run(["tmux", "-S", str(handle.socket_path), "display-message", "-p", "#{window_zoomed_flag}"],
                                capture_output=True, text=True, check=True).stdout.strip()
        assert zoomed == "1"
    finally:
        backend.stop(handle)


def test_attach_does_not_leave_the_window_at_a_fixed_size(tmp_path: Path) -> None:
    """Sizing the window before the client attaches must not stop it from following later clients."""
    backend, handle, _log = _start(tmp_path)
    try:
        _attach_stream(handle, rows=40, columns=140)
        option = subprocess.run(["tmux", "-S", str(handle.socket_path), "show-options", "-w", "-t", "frames:station", "window-size"],
                                capture_output=True, text=True).stdout.strip()
        assert option == ""
    finally:
        backend.stop(handle)
