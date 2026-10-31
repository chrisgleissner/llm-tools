"""A curses input loop must end when its terminal goes away, not spin on ``ERR``."""

from __future__ import annotations

import curses
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from llm_tools import bridge_hangup
from llm_tools.bridge_hangup import EXIT_TERMINAL_GONE, TerminalGone, TerminalWatch
from tests.conftest import scaled, wait_for
from tests.process_hygiene import terminate_process_tree


# --- detector, with a fake clock and probe -----------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def watch(*, gone: bool = True, **kwargs) -> tuple[TerminalWatch, list[int]]:
    probes: list[int] = []

    def probe() -> bool:
        probes.append(1)
        return gone

    return TerminalWatch(clock=Clock(), probe=probe, only_real_windows=False, **kwargs), probes


class Window:
    """get_wch() that takes ``taken`` seconds on a fake clock and then raises, like a curses timeout."""

    def __init__(self, watcher: TerminalWatch, taken: float) -> None:
        self.watcher, self.taken, self.timeouts = watcher, taken, []

    def timeout(self, milliseconds: int) -> None:
        self.timeouts.append(milliseconds)

    def get_wch(self):
        self.watcher.clock.now += self.taken
        raise curses.error("no input")


def read_until_raise(watcher: TerminalWatch, window: Window, calls: int) -> int:
    for count in range(1, calls + 1):
        try:
            watcher.read(window)
        except curses.error:
            continue
        except TerminalGone:
            return count
    return 0


def test_immediate_errors_on_a_dead_terminal_raise_after_the_threshold() -> None:
    watcher, probes = watch(threshold=5)
    window = Window(watcher, taken=0.0)
    watcher.set_timeout(window, 250)
    assert read_until_raise(watcher, window, 20) == 5
    assert window.timeouts == [250]
    assert len(probes) == 1


def test_real_timeouts_are_never_mistaken_for_a_hangup() -> None:
    watcher, probes = watch(gone=True, threshold=3)
    window = Window(watcher, taken=0.25)  # the full poll interval elapsed with no input
    watcher.set_timeout(window, 250)
    assert read_until_raise(watcher, window, 200) == 0
    assert probes == []


def test_timeouts_that_return_slightly_early_still_count_as_timeouts() -> None:
    watcher, probes = watch(threshold=3)
    window = Window(watcher, taken=0.13)  # a loaded host can wake a little before the nominal time
    watcher.set_timeout(window, 250)
    assert read_until_raise(watcher, window, 50) == 0
    assert probes == []


def test_a_live_terminal_that_returns_early_errors_is_probed_but_kept() -> None:
    watcher, probes = watch(gone=False, threshold=3, probe_interval=0.0)
    window = Window(watcher, taken=0.0)
    watcher.set_timeout(window, 100)
    assert read_until_raise(watcher, window, 30) == 0
    assert probes  # it was asked, and the answer was "still there"
    assert watcher.early_errors < 3


def test_probe_is_throttled_while_the_terminal_stays_alive() -> None:
    watcher, probes = watch(gone=False, threshold=2, probe_interval=0.5)
    window = Window(watcher, taken=0.0)
    watcher.set_timeout(window, 100)
    read_until_raise(watcher, window, 50)  # the fake clock does not advance: one probe only
    assert len(probes) == 1


def test_a_key_or_a_full_timeout_resets_the_early_error_count() -> None:
    watcher, probes = watch(threshold=5)
    window = Window(watcher, taken=0.0)
    watcher.set_timeout(window, 100)
    for _ in range(4):
        with pytest.raises(curses.error):
            watcher.read(window)
    assert watcher.early_errors == 4
    window.taken = 0.1
    with pytest.raises(curses.error):
        watcher.read(window)  # a real timeout
    assert watcher.early_errors == 0
    window.taken = 0.0
    for _ in range(4):
        with pytest.raises(curses.error):
            watcher.read(window)
    assert probes == []
    window.get_wch = lambda: "x"  # type: ignore[method-assign]
    assert watcher.read(window) == "x"
    assert watcher.early_errors == 0


def test_non_blocking_polling_is_not_a_hangup() -> None:
    watcher, probes = watch(threshold=2)
    window = Window(watcher, taken=0.0)
    watcher.set_timeout(window, 0)
    assert read_until_raise(watcher, window, 50) == 0
    assert probes == []


def test_blocking_reads_that_error_are_counted() -> None:
    watcher, _ = watch(threshold=3)
    window = Window(watcher, taken=0.0)
    watcher.set_timeout(window, -1)
    assert read_until_raise(watcher, window, 10) == 3


def test_scripted_windows_are_ignored_unless_asked() -> None:
    watcher = TerminalWatch(clock=Clock(), probe=lambda: True, threshold=1)
    window = Window(watcher, taken=0.0)
    watcher.set_timeout(window, 100)
    assert read_until_raise(watcher, window, 10) == 0
    assert bridge_hangup.read_wch(type("W", (), {"get_wch": lambda self: "k"})()) == "k"
    fake = type("W", (), {"timeout": lambda self, ms: setattr(self, "ms", ms)})()
    bridge_hangup.set_timeout(fake, 77)
    assert fake.ms == 77


def pty_pair() -> tuple[int, int]:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    return master, slave


def test_terminal_probe_sees_a_live_pty_a_closed_master_and_a_pipe() -> None:
    master, slave = pty_pair()
    try:
        assert bridge_hangup.terminal_gone(slave) is False
        os.close(master)
        master = -1
        assert bridge_hangup.terminal_gone(slave) is True
    finally:
        if master >= 0:
            os.close(master)
        os.close(slave)
    read_end, write_end = os.pipe()
    try:
        assert bridge_hangup.terminal_gone(read_end) is True  # not a terminal
    finally:
        os.close(read_end)
        os.close(write_end)
    assert bridge_hangup.terminal_gone(987654) is True  # not even a descriptor


def test_probe_does_not_consume_pending_input() -> None:
    master, slave = pty_pair()
    try:
        os.write(master, b"abc")
        wait_for(lambda: select.select([slave], [], [], 0)[0], 2)
        assert bridge_hangup.terminal_gone(slave) is False
        attributes = termios.tcgetattr(slave)
        attributes[3] &= ~termios.ICANON
        termios.tcsetattr(slave, termios.TCSANOW, attributes)
        assert os.read(slave, 10) == b"abc"
    finally:
        os.close(master)
        os.close(slave)


# --- SIGHUP and the curses wrapper ------------------------------------------------------------

def test_sighup_raises_terminal_gone_even_when_it_was_inherited_as_ignored_and_is_restored() -> None:
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with pytest.raises(TerminalGone):
            with bridge_hangup.hangup_exits():
                os.kill(os.getpid(), signal.SIGHUP)
                time.sleep(1)
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_a_second_hangup_during_cleanup_does_not_interrupt_it() -> None:
    previous = signal.signal(signal.SIGHUP, signal.SIG_DFL)
    try:
        with pytest.raises(TerminalGone):
            with bridge_hangup.hangup_exits():
                try:
                    os.kill(os.getpid(), signal.SIGHUP)
                    time.sleep(1)
                finally:
                    os.kill(os.getpid(), signal.SIGHUP)  # ignored: the first one already unwound
                    time.sleep(0.05)
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_run_curses_turns_a_cleanup_error_after_a_hangup_into_terminal_gone(monkeypatch) -> None:
    abandoned: list[bool] = []
    monkeypatch.setattr(bridge_hangup, "abandon_terminal", lambda: abandoned.append(True) or EXIT_TERMINAL_GONE)
    monkeypatch.setattr(curses, "endwin", lambda: (_ for _ in ()).throw(curses.error("dead")))

    def wrapper(_loop):
        try:
            raise TerminalGone("terminal hung up")
        except TerminalGone:
            raise curses.error("nocbreak() returned ERR")  # what curses.wrapper's own cleanup does

    monkeypatch.setattr(curses, "wrapper", wrapper)
    with pytest.raises(TerminalGone):
        bridge_hangup.run_curses(lambda _w: 0)
    assert abandoned == [True]


def test_run_curses_abandons_the_terminal_when_the_loop_raises_terminal_gone(monkeypatch) -> None:
    abandoned: list[bool] = []
    monkeypatch.setattr(bridge_hangup, "abandon_terminal", lambda: abandoned.append(True) or EXIT_TERMINAL_GONE)

    def wrapper(_loop):
        raise TerminalGone("SIGHUP")

    monkeypatch.setattr(curses, "wrapper", wrapper)
    with pytest.raises(TerminalGone):
        bridge_hangup.run_curses(lambda _w: 0)
    assert abandoned == [True]


def test_run_curses_leaves_other_curses_errors_and_results_alone(monkeypatch) -> None:
    monkeypatch.setattr(bridge_hangup, "abandon_terminal", lambda: pytest.fail("terminal is fine"))
    monkeypatch.setattr(curses, "wrapper", lambda loop: loop(None))
    assert bridge_hangup.run_curses(lambda _w: "result") == "result"

    def failing(_loop):
        raise curses.error("setupterm failed")

    monkeypatch.setattr(curses, "wrapper", failing)
    with pytest.raises(curses.error):
        bridge_hangup.run_curses(lambda _w: 0)


def test_abandon_terminal_points_the_standard_descriptors_at_devnull(monkeypatch) -> None:
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "open", lambda path, flags: calls.append(("open", 0, 0)) or 41)
    monkeypatch.setattr(os, "dup2", lambda source, target: calls.append(("dup2", source, target)))
    monkeypatch.setattr(os, "close", lambda fd: calls.append(("close", fd, 0)))
    assert bridge_hangup.abandon_terminal() == EXIT_TERMINAL_GONE == 129
    assert [call for call in calls if call[0] == "dup2"] == [("dup2", 41, 0), ("dup2", 41, 1), ("dup2", 41, 2)]
    assert ("close", 41, 0) in calls


def test_hangup_handler_is_skipped_off_the_main_thread() -> None:
    import threading

    seen: list[object] = []

    def worker() -> None:
        with bridge_hangup.hangup_exits():
            seen.append(signal.getsignal(signal.SIGHUP))

    before = signal.getsignal(signal.SIGHUP)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen == [before]


# --- real curses programs on a pseudo-terminal -----------------------------------------------

ROOT = str(Path(__file__).resolve().parents[1])

PROGRAMS = {
    "viewer": "import sys\nfrom llm_tools import bridge_viewer as v\nsys.exit(v.run_document('hello\\n' * 200, title='t'))\n",
    "viewer-following": ("import os, sys\nfrom pathlib import Path\nfrom llm_tools import bridge_viewer as v\n"
                         "path = Path(os.environ['HANGUP_FILE'])\npath.write_text('line\\n' * 50)\n"
                         "sys.exit(v.run_document(path.read_text(), title='t', path=path, follow=True))\n"),
    "console": ("import sys\nfrom llm_tools.bridge_tui import run_console\n"
                "def dispatch(mission, command): return 'ok'\n"
                "def snapshot(mission): return {'mission': {'id': mission, 'name': 'x', 'status': 'RUNNING'}}\n"
                "sys.exit(run_console('m', dispatch, snapshot))\n"),
    "control": ("import sys\nfrom llm_tools.bridge_tui import run_control\n"
                "def dispatch(mission, command): return 'ok'\n"
                "def snapshot(mission): return {'mission': {'id': mission, 'name': 'x', 'status': 'RUNNING'}}\n"
                "sys.exit(run_control('m', dispatch, snapshot))\n"),
    "launcher": "import sys\nfrom llm_tools import bridge_launcher as l\nsys.exit(l.run_launcher(lambda argv: 0))\n",
    "picker": ("import sys\nfrom llm_tools import bridge_switcher as w\n"
               "from llm_tools.bridge_hangup import TerminalGone\n"
               "entries = [w.MissionEntry(mission_id='a' * 32, name='one', status='RUNNING', cwd='/tmp')]\n"
               "try:\n    w.run_picker(entries)\nexcept TerminalGone:\n    sys.exit(129)\nsys.exit(0)\n"),
}


class PtyChild:
    def __init__(self, program: str, tmp_path: Path, env: dict[str, str], *, ignore_sighup: bool) -> None:
        self.master, slave = pty_pair()
        child_env = dict(env, PYTHONPATH=os.pathsep.join(filter(None, (ROOT, env.get("PYTHONPATH", "")))),
                         TERM="xterm-256color", LANG="C.UTF-8", LC_ALL="C.UTF-8", LLM_TOOLS_NO_INHIBIT="1",
                         LLM_BRIDGE_STATE_DIR=str(tmp_path / "state"), HANGUP_FILE=str(tmp_path / "followed.log"))

        def prepare() -> None:
            if ignore_sighup:
                signal.signal(signal.SIGHUP, signal.SIG_IGN)  # nohup, or an inherited SIG_IGN

        self.process = subprocess.Popen(
            [sys.executable, "-c", PROGRAMS[program]], cwd=tmp_path, env=child_env,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True, preexec_fn=prepare,
        )
        os.close(slave)
        self.output = bytearray()

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and self.master >= 0:
            if select.select([self.master], [], [], 0.03)[0]:
                try:
                    self.output.extend(os.read(self.master, 65536))
                except OSError:
                    return

    def draw_something(self) -> None:
        deadline = time.monotonic() + scaled(10)
        while not self.output and time.monotonic() < deadline and self.process.poll() is None:
            self.pump(0.1)
        assert self.output, "the program drew nothing"
        self.pump(0.3)

    def hang_up(self) -> None:
        os.close(self.master)
        self.master = -1

    def close(self) -> None:
        try:
            terminate_process_tree(self.process)
        finally:
            if self.master >= 0:
                os.close(self.master)
                self.master = -1


@pytest.fixture()
def pty_child(tmp_path: Path, env: dict[str, str]):
    children: list[PtyChild] = []

    def start(program: str, *, ignore_sighup: bool = True) -> PtyChild:
        child = PtyChild(program, tmp_path, env, ignore_sighup=ignore_sighup)
        children.append(child)
        return child

    yield start
    for child in children:
        child.close()


@pytest.mark.parametrize("program", sorted(PROGRAMS))
def test_a_curses_program_exits_when_its_terminal_is_hung_up_with_sighup_ignored(pty_child, program) -> None:
    child = pty_child(program)
    child.draw_something()
    child.hang_up()
    try:
        status = child.process.wait(timeout=scaled(10))
    except subprocess.TimeoutExpired:
        pytest.fail(f"{program} kept running after its terminal went away")
    assert status == EXIT_TERMINAL_GONE


@pytest.mark.parametrize("program", ["viewer", "console", "launcher"])
def test_an_idle_terminal_is_not_mistaken_for_a_hangup(pty_child, program) -> None:
    child = pty_child(program)
    child.draw_something()
    child.pump(scaled(2.5))  # several poll timeouts with no input at all
    assert child.process.poll() is None


@pytest.mark.parametrize("program", ["viewer", "console"])
def test_an_ignored_sighup_still_ends_the_program_that_owns_the_terminal(pty_child, program) -> None:
    child = pty_child(program)
    child.draw_something()
    child.process.send_signal(signal.SIGHUP)
    try:
        status = child.process.wait(timeout=scaled(10))
    except subprocess.TimeoutExpired:
        pytest.fail(f"{program} ignored SIGHUP")
    assert status == EXIT_TERMINAL_GONE


def test_the_console_still_handles_input_and_then_exits_on_hangup(pty_child) -> None:
    child = pty_child("console", ignore_sighup=False)
    child.draw_something()
    os.write(child.master, b"help\r")
    child.pump(0.5)
    assert child.process.poll() is None
    child.hang_up()
    assert child.process.wait(timeout=scaled(10)) == EXIT_TERMINAL_GONE
