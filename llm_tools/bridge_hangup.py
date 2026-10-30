"""Stop curses input loops when their terminal goes away.

After a hangup (a closed SSH session, a dead PTY master) ``getch``/``get_wch``
return ``ERR`` immediately on every call. A loop that treats ``ERR`` as "no input
yet, poll again" then spins at full CPU forever. SIGHUP normally ends the process,
but it can be ignored (``nohup``, an inherited ``SIG_IGN``), so the loops cannot
rely on it.

Every input loop reads keys through :func:`read_wch` and sets its poll timeout
through :func:`set_timeout`. The watcher counts consecutive ``ERR`` results that
come back well before the armed timeout. A real timeout (the full wait elapsed
with no input) resets the count, so an idle terminal is never mistaken for a
hangup. After several early ``ERR`` results it probes the terminal without
consuming input (poll for hangup, ``tcgetattr``, ``isatty``); only a failed probe
raises :class:`TerminalGone`.
"""

from __future__ import annotations

import contextlib
import curses
import os
import select
import signal
import termios
import threading
import time
from typing import Any, Callable, Iterator

EXIT_TERMINAL_GONE = 129  # 128 + SIGHUP: the terminal was hung up under the program


class TerminalGone(BaseException):
    """The controlling terminal was hung up.

    Derived from ``BaseException`` (like ``KeyboardInterrupt``) so broad
    ``except Exception`` handlers in the UI code cannot swallow it and keep
    looping. ``finally`` blocks still run, so locks, threads and the curses
    screen are released on the way out.
    """


EARLY_ERRORS_BEFORE_PROBE = 5  # consecutive early ERR results before the terminal is probed
EARLY_FRACTION = 0.5  # an ERR that returns in under this fraction of the timeout is "early"
PROBE_INTERVAL_SECONDS = 0.5  # at most one probe this often, so a quirky early ERR cannot cost a syscall per key


def terminal_gone(fd: int = 0) -> bool:
    """Whether ``fd`` no longer has a live terminal behind it. Never consumes input."""
    try:
        if not os.isatty(fd):
            return True
        termios.tcgetattr(fd)
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        for _fd, events in poller.poll(0):
            if events & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                return True
    except (OSError, termios.error, ValueError):
        return True
    return False


class TerminalWatch:
    """Detect a hung-up terminal from early ``ERR`` results of a curses read."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        probe: Callable[[], bool] = terminal_gone,
        threshold: int = EARLY_ERRORS_BEFORE_PROBE,
        probe_interval: float = PROBE_INTERVAL_SECONDS,
        only_real_windows: bool = True,
    ) -> None:
        self.clock = clock
        self.probe = probe
        self.threshold = threshold
        self.probe_interval = probe_interval
        self.only_real_windows = only_real_windows
        self.timeout_ms = -1
        self.early_errors = 0
        self._last_probe: float | None = None

    def set_timeout(self, window: Any, milliseconds: int) -> None:
        self.timeout_ms = milliseconds
        window.timeout(milliseconds)

    def read(self, window: Any) -> Any:
        started = self.clock()
        try:
            key = window.get_wch()
        except ValueError as exc:
            # ncurses cannot decode a UTF-8 sequence beyond U+10FFFF and Python reports that as ValueError;
            # every screen treats curses.error as "no key", so report undecodable input the same way.
            raise curses.error("undecodable input") from exc
        except curses.error:
            self.note_error(self.clock() - started, window)
            raise
        self.early_errors = 0
        return key

    def note_error(self, elapsed: float, window: Any = None) -> None:
        """Record one ``ERR`` that took ``elapsed`` seconds; raise :class:`TerminalGone` if the terminal is gone."""
        if self.only_real_windows and not isinstance(window, curses.window):
            return  # scripted test windows have no terminal behind them
        if self.timeout_ms > 0 and elapsed * 1000.0 >= self.timeout_ms * EARLY_FRACTION:
            self.early_errors = 0  # the full wait elapsed with no input: an ordinary timeout
            return
        if self.timeout_ms == 0:
            return  # non-blocking polling returns ERR at once by design
        self.early_errors += 1
        if self.early_errors < self.threshold:
            return
        now = self.clock()
        if self._last_probe is not None and now - self._last_probe < self.probe_interval:
            return
        self._last_probe = now
        if self.probe():
            raise TerminalGone("terminal hung up")
        self.early_errors = 0


_WATCH = TerminalWatch()


def abandon_terminal() -> int:
    """Point the standard descriptors at ``/dev/null`` and return :data:`EXIT_TERMINAL_GONE`.

    Without this the interpreter's final flush of ``sys.stdout`` into the dead
    terminal fails and replaces the exit status with 120.
    """
    with contextlib.suppress(OSError):
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            with contextlib.suppress(OSError):
                os.dup2(null, fd)
        if null > 2:
            os.close(null)
    return EXIT_TERMINAL_GONE


def read_wch(window: Any) -> Any:
    """``window.get_wch()`` that raises :class:`TerminalGone` instead of spinning on a dead terminal."""
    return _WATCH.read(window)


def set_timeout(window: Any, milliseconds: int) -> None:
    """``window.timeout(ms)`` that also tells the watcher how long a real timeout takes."""
    _WATCH.set_timeout(window, milliseconds)


def _raise_gone(_signum: int, _frame: Any) -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # only the first hangup unwinds; cleanup must not be interrupted again
    raise TerminalGone("SIGHUP")


@contextlib.contextmanager
def hangup_exits() -> Iterator[None]:
    """Make SIGHUP end the loop that owns the terminal, even if it was inherited as ignored."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    try:
        previous = signal.signal(signal.SIGHUP, _raise_gone)
    except (ValueError, OSError):
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal.SIGHUP, previous)


def run_curses(loop: Callable[[Any], Any]) -> Any:
    """``curses.wrapper`` that turns a hung-up terminal into :class:`TerminalGone`.

    It installs a SIGHUP handler that raises :class:`TerminalGone`, and it points the
    standard descriptors at ``/dev/null`` once the terminal is known to be gone, so
    the ``finally`` blocks of the callers (bracketed-paste reset, flushes) cannot
    fail on the dead terminal and mask the hangup.
    """
    _WATCH.early_errors = 0
    _WATCH.timeout_ms = -1
    with hangup_exits():
        try:
            return curses.wrapper(loop)
        except TerminalGone:
            abandon_terminal()
            raise
        except curses.error as exc:
            if not _caused_by_hangup(exc):
                raise
            # curses.wrapper's own cleanup (nocbreak, echo) fails on a dead terminal and hides the hangup.
            with contextlib.suppress(curses.error):
                curses.endwin()
            abandon_terminal()
            raise TerminalGone("terminal hung up") from exc


def _caused_by_hangup(exc: BaseException) -> bool:
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        if isinstance(link, TerminalGone):
            return True
        seen.add(id(link))
        link = link.__context__ or link.__cause__
    return False
