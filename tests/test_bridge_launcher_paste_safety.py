"""Launcher paste safety: bracketed paste is requested, and bursts never act."""
from __future__ import annotations

import curses
import fcntl
import io
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

from llm_tools import bridge_launcher as launcher, bridge_tui
from llm_tools.bridge_launcher import Launcher
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled

BRACKET_ON, BRACKET_OFF = b"\x1b[?2004h", b"\x1b[?2004l"
GAP = 0.08  # realistic fast-typist gap, well above the 30 ms burst window


class Session:
    """The real launcher on a PTY with an isolated state directory."""

    def __init__(self, tmp_path: Path, env: dict[str, str]) -> None:
        self.state_dir = tmp_path / "state"
        self.work = tmp_path / "work dir"
        self.work.mkdir()
        root = str(Path(__file__).resolve().parents[1])
        env = dict(env, LLM_BRIDGE_STATE_DIR=str(self.state_dir), TERM="xterm-256color",
                   PYTHONPATH=os.pathsep.join(filter(None, (root, env.get("PYTHONPATH", "")))))
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        self.process = subprocess.Popen([sys.executable, "-m", "llm_tools.bridge"], cwd=self.work, env=env,
                                        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        self.output = bytearray()

    def pump(self, seconds: float = 0.05) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.02)[0]:
                try:
                    self.output.extend(os.read(self.master, 65536))
                except OSError:
                    return

    def wait_for(self, marker: bytes, timeout: float = 8) -> None:
        deadline = time.monotonic() + scaled(timeout)
        while time.monotonic() < deadline and marker not in self.output:
            self.pump(0.05)
        assert marker in self.output, f"missing {marker!r}; exit={self.process.poll()} tail={bytes(self.output[-800:])!r}"

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def missions(self) -> list[dict]:
        if not (self.state_dir / "bridge.sqlite3").exists():
            return []
        with BridgeStore(self.state_dir / "bridge.sqlite3") as store:
            return store.list_missions()

    def operations(self) -> int:
        if not (self.state_dir / "bridge.sqlite3").exists():
            return 0
        with BridgeStore(self.state_dir / "bridge.sqlite3") as store:
            return len(store.db.execute("SELECT 1 FROM operations").fetchall())

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=scaled(3))
            except subprocess.TimeoutExpired:
                self.process.kill()
        os.close(self.master)


@pytest.fixture
def session(tmp_path, env):
    opened = Session(tmp_path, env)
    yield opened
    opened.close()


def test_launcher_enables_bracketed_paste_on_start_and_disables_it_on_exit(session):
    session.wait_for(b"New mission in this folder")
    assert session.output.startswith(BRACKET_ON) or BRACKET_ON in session.output
    assert BRACKET_ON in session.output[:session.output.index(b"New mission in this folder")]
    session.send(b"\x04")
    assert session.process.wait(timeout=scaled(5)) == 0
    session.pump(0.2)
    last_on, last_off = session.output.rfind(BRACKET_ON), session.output.rfind(BRACKET_OFF)
    assert 0 <= last_on < last_off  # the final mode change is "off"


def test_unbracketed_multiline_burst_opens_composer_and_creates_nothing(session):
    session.wait_for(b"New mission in this folder")
    session.send(b"n")
    session.wait_for(b"Objective:")
    # A terminal without bracketed paste delivers the text as plain keystrokes.
    session.send(b"# Plan\nFirst paragraph\n\nSecond paragraph\r\r\r\r\r")
    session.wait_for(b"full source")  # the composer, not the next form field
    session.pump(0.8)
    assert session.process.poll() is None
    assert session.missions() == [] and session.operations() == 0
    assert b"CREATED" not in session.output


def test_unbracketed_burst_cannot_activate_save_and_needs_a_fresh_enter(session):
    session.wait_for(b"New mission in this folder")
    session.send(b"n")
    session.wait_for(b"Objective:")
    session.send(b"Burst objective" + b"\t" * 4 + b"\r")  # Save is focused, but the Enter arrives inside the burst
    session.wait_for(b"Press Enter again to save")
    session.pump(0.5)
    assert session.missions() == [] and session.operations() == 0
    time.sleep(GAP)
    session.send(b"\r")  # a deliberate Enter, after a pause
    session.wait_for(b"CREATED")
    missions = session.missions()
    assert len(missions) == 1 and missions[0]["status"] == "CREATED"
    assert session.operations() == 0


def test_bracketed_multiline_paste_lands_whole_in_the_composer(session):
    session.wait_for(b"New mission in this folder")
    session.send(b"n")
    session.wait_for(b"Objective:")
    session.send(b"\x1b[200~# Plan\nFirst paragraph\n\nSecond paragraph\x1b[201~")
    session.wait_for(b"Second paragraph")
    session.wait_for(b"4 lines")
    session.pump(0.3)
    assert b"First paragraph" in session.output
    assert session.missions() == [] and session.operations() == 0
    session.send(b"\x13")  # save the composer into the Objective field
    session.wait_for(b"Providers:")
    session.pump(0.3)
    assert session.missions() == []


def test_human_speed_keys_navigate_and_save(session):
    session.wait_for(b"New mission in this folder")
    session.send(b"n")
    session.wait_for(b"Objective:")
    # Enter walks the fields and stops on Save; Start needs Tab/Down and is never reached by Enter.
    for chunk in (b"Typed by a human", b"\r", b"\t", b"\t", b"\t", b"\r"):
        time.sleep(GAP)
        session.send(chunk)
    session.wait_for(b"CREATED")
    missions = session.missions()
    assert len(missions) == 1 and missions[0]["status"] == "CREATED"
    with BridgeStore(session.state_dir / "bridge.sqlite3") as store:
        assert store.list_tasks(missions[0]["id"])[0]["original_prompt"] == "Typed by a human"
    assert session.operations() == 0


# --- in-process burst guard --------------------------------------------------

HERE = str(Path.cwd())


@pytest.fixture(autouse=True)
def _codex_present(monkeypatch):
    monkeypatch.setattr(launcher, "provider_installed", lambda name: True)


def guarded(**kwargs) -> Launcher:
    return Launcher("/w", creating=True, directory=HERE, installed=("codex",), burst_window=0.03, **kwargs)


def test_enter_soon_after_another_key_does_not_activate_buttons():
    for focus, word in ((4, "save"), (5, "start")):
        state = guarded(focus=focus, objective="Do it")
        state.key("x")
        assert state.focus == focus
        assert state.key("\n") is None
        assert state.error == f"Press Enter again to {word}"
        assert state.key("\r") is None  # still a burst: the refused Enter refreshes the window


def test_enter_after_a_pause_activates_and_keystroke_clears_the_hint():
    state = guarded(focus=4, objective="Do it")
    state.last_input = time.monotonic()
    assert state.key("\n") is None and state.error.startswith("Press Enter again")
    state.key(curses.KEY_UP)
    assert state.error == ""
    state.focus = 4
    state.last_input = time.monotonic() - 1
    assert state.key("\n") == "create"
    state.focus = 5
    assert state.key("\n") == "start"


def test_enter_with_queued_input_is_a_burst_even_after_a_pause():
    pending = [True]
    state = guarded(focus=5, objective="Do it")
    state.input_pending = lambda: pending[0]
    state.last_input = time.monotonic() - 5
    assert state.key("\n") is None and "start" in state.error
    pending[0] = False
    state.last_input = time.monotonic() - 5
    assert state.key("\n") == "start"


def test_burst_newline_on_objective_inserts_it_and_keeps_focus():
    state = guarded(focus=0)
    for char in "line one":
        state.key(char)
        state.cursors[0] = len(state.objective)
    assert state.key("\n") == "compose-objective"
    assert state.objective == "line one\n" and state.focus == 0
    state.last_input = time.monotonic() - 5
    state.cursors[0] = len(state.objective)
    state.focus = 0
    assert state.key("\n") is None and state.focus == 1  # a human Enter still moves on


def test_enter_navigation_between_fields_is_not_guarded_and_clicks_are_deliberate():
    state = guarded(focus=1, objective="Do it")
    state.key("x")
    assert state.key("\n") is None and state.focus == 2
    state.focus = 4
    state.last_input = time.monotonic()
    state.input_pending = lambda: True
    assert state.key("\n", deliberate=True) == "create"


def test_bare_launcher_and_zero_window_never_guard():
    state = Launcher("/w", creating=True, directory=HERE, installed=("codex",), focus=4, objective="Do it")
    state.key("x")
    assert state.key("\n") == "create"


def test_stdin_pending_reports_queued_bytes_and_tolerates_bad_streams(monkeypatch):
    read, write = os.pipe()
    try:
        monkeypatch.setattr(sys, "stdin", os.fdopen(read, "r", closefd=False))
        assert launcher._stdin_pending() is False
        os.write(write, b"x")
        assert launcher._stdin_pending() is True
    finally:
        os.close(read)
        os.close(write)
    monkeypatch.setattr(sys, "stdin", io.StringIO())  # no file descriptor
    assert launcher._stdin_pending() is False


def test_set_bracketed_paste_ignores_a_closed_stdout(monkeypatch):
    class Broken:
        def write(self, _text): raise OSError("closed")
        def flush(self): pass
    monkeypatch.setattr(sys, "stdout", Broken())
    launcher._set_bracketed_paste(True)  # must not raise


class _Stream(io.StringIO):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def write(self, text: str) -> int:
        if "2004" in text:
            self.events.append("on" if text.endswith("h") else "off")
        return len(text)


def _prepare(tmp_path, monkeypatch, events):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launcher, "detect_installed", lambda: ("codex",))
    monkeypatch.setattr(launcher, "_load_config", lambda: {})
    monkeypatch.setattr(sys, "stdout", _Stream(events))


def test_bracketed_paste_is_off_around_every_handoff_and_exception_path(tmp_path, monkeypatch):
    events: list[str] = []
    _prepare(tmp_path, monkeypatch, events)
    actions = iter(["compose-objective", "help", "quit"])

    def wrapper(_callback):
        events.append("loop")
        return next(actions)

    monkeypatch.setattr(curses, "wrapper", wrapper)
    monkeypatch.setattr(bridge_tui, "compose_document", lambda *_a, **_k: events.append("composer") or "text")
    import llm_tools.bridge_viewer as viewer
    monkeypatch.setattr(viewer, "run_document", lambda *_a, **_k: events.append("viewer"))
    assert launcher.run_launcher(lambda args: 0) == 0
    assert events == ["on", "loop", "off", "composer", "on", "loop", "off", "viewer", "on", "loop", "off"]

    for exc in (KeyboardInterrupt(), EOFError()):
        events.clear()
        monkeypatch.setattr(curses, "wrapper", lambda _cb, exc=exc: (_ for _ in ()).throw(exc))
        assert launcher.run_launcher(lambda args: 0) == 0
        assert events == ["on", "off"]
    events.clear()
    monkeypatch.setattr(curses, "wrapper", lambda _cb: (_ for _ in ()).throw(curses.error("no tty")))
    assert launcher.run_launcher(lambda args: 0) == 2
    assert events == ["on", "off"]


def test_bracketed_paste_is_off_before_start_and_control_dispatch(tmp_path, monkeypatch):
    events: list[str] = []
    _prepare(tmp_path, monkeypatch, events)
    monkeypatch.setattr(curses, "wrapper", lambda _cb: events.append("loop") or "start")
    seen: list[str] = []

    def dispatch(args):
        seen.append(args[0])
        events.append(args[0])
        if args[0] == "mission":
            print('{"mission": {"id": "m1"}}')
        return 0

    assert launcher.run_launcher(dispatch) == 0
    assert events[:3] == ["on", "loop", "off"]
    assert events.index("off") < events.index("start") < events.index("control")


def test_burst_with_start_focused_never_dispatches_start(tmp_path, monkeypatch):
    events: list[str] = []
    _prepare(tmp_path, monkeypatch, events)
    monkeypatch.setattr(launcher, "_BURST_SECONDS", 0.03)
    monkeypatch.setattr(launcher, "provider_installed", lambda name: True)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(bridge_tui, "initialize_colors", lambda: False)
    from tests.test_bridge_launcher_home import Grid
    sent = ["n", *"Objective", *"\t" * 5, "\n", "\x04"]
    window = Grid(sent)  # delivered with no gap
    # A terminal that delivers keys with no gap has them in the input queue; report that, as the real
    # stdin would, so the test does not depend on how long the launcher takes to process each key. The
    # final Ctrl-D comes after a pause: it is not queued behind the Enter, so the launcher draws before it.
    monkeypatch.setattr(launcher, "_stdin_pending", lambda: window.delivered < len(sent) - 1)
    original_get = window.get_wch
    window.delivered = 0

    def get_wch():
        window.delivered += 1
        return original_get()

    window.get_wch = get_wch
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    calls: list[list[str]] = []
    assert launcher.run_launcher(lambda args: calls.append(args) or 0) == 0
    assert calls == []  # no mission created, nothing started
    assert "Press Enter again to start" in window.text()


# --- the guard must not depend on how long the loop takes ---------------------
#
# On a loaded machine the launcher can spend tens of milliseconds drawing, or not run at all, between
# two keys of a paste. Those keys are already in the terminal input queue (or arrive while the launcher
# is busy), so the time spent is not a gap between keystrokes.


class _Clock:
    """The launcher's clock: real time plus a skew that the tests advance by hand."""

    def __init__(self) -> None:
        self.skew = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.skew

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(launcher, "time", fake)
    return fake


def test_key_that_arrived_while_the_loop_was_busy_is_part_of_the_burst(clock):
    waiting = [False]
    state = guarded(focus=5, objective="Do it")
    state.input_pending = lambda: waiting[0]
    state.key("\t")
    clock.skew += 10  # a slow draw, or a descheduled process
    waiting[0] = True  # the next key arrived meanwhile
    state.note_ready()
    waiting[0] = False  # nothing queued behind it
    assert state.key("\n") is None
    assert state.error == "Press Enter again to start"


def test_gap_is_measured_from_when_the_loop_was_ready_not_from_the_previous_key(clock):
    state = guarded(focus=5, objective="Do it")
    state.key("\t")
    clock.skew += 10  # the loop was busy for ten seconds before it waited for input
    state.note_ready()
    clock.skew += 0.01  # the next key arrives 10 ms after the loop started waiting
    assert state.key("\n") is None and state.error == "Press Enter again to start"
    # A key that arrives after a real pause is still a deliberate Enter.
    state.key("\t")
    clock.skew += 10
    state.note_ready()
    clock.skew += 0.06
    assert state.key("\n") == "start"


def test_a_timed_out_wait_does_not_shorten_the_gap_since_the_last_key(clock):
    state = guarded(focus=5, objective="Do it")
    state.key("\t")
    state.note_ready()
    clock.skew += 1  # the wait timed out (a timer fired) and the loop waited again
    state.note_ready()
    clock.skew += 0.01
    assert state.key("\n") == "start"  # one second after the last key is a deliberate Enter


class _DrawStallGrid:
    """A window whose draws are slow and during which the next pasted key arrives.

    ``keys`` holds ``(key, arrives_during_draw)``. The terminal input queue is modelled: ``pending`` is
    true only for a key that arrived during the draw since the previous read.
    """

    def __init__(self, base, keys, clock):
        self.base, self.keys, self.clock = base, list(keys), clock
        self.drawn_since_read = False

    def __getattr__(self, name):
        return getattr(self.base, name)

    def refresh(self):
        self.clock.skew += 1.0  # a draw takes a second of wall-clock time
        self.drawn_since_read = True

    def get_wch(self):
        key, _arrives = self.keys.pop(0)
        self.drawn_since_read = False
        return key

    def pending(self) -> bool:
        return bool(self.keys) and self.keys[0][1] and self.drawn_since_read


def test_slow_draws_between_the_keys_of_a_burst_never_start_a_mission(tmp_path, monkeypatch, clock):
    events: list[str] = []
    _prepare(tmp_path, monkeypatch, events)
    monkeypatch.setattr(launcher, "_BURST_SECONDS", 0.03)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(bridge_tui, "initialize_colors", lambda: False)
    from tests.test_bridge_launcher_home import Grid
    keys = [(key, True) for key in ["n", *"Objective", *"\t" * 5, "\n"]] + [("\x04", False)]
    window = _DrawStallGrid(Grid(), keys, clock)
    monkeypatch.setattr(launcher, "_stdin_pending", window.pending)
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    calls: list[list[str]] = []
    assert launcher.run_launcher(lambda args: calls.append(args) or 0) == 0
    assert calls == []
    assert "Press Enter again to start" in window.text()
