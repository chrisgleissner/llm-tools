"""Redraw scheduling for Bridge's curses screens: draw when something changed, not on a fixed tick.

A screen loop owns one :class:`RedrawPlanner`.  Input, a new data snapshot, a
resize or any other state change calls :meth:`RedrawPlanner.mark`.  Text that
changes with time (``heartbeat 3s ago``, a spinner) reports how soon it will
change while the frame is drawn, inside :func:`collect`; the planner turns the
earliest of those reports into the next deadline.  The loop sleeps in
``getch`` until the earliest of that deadline and its own timers, so an idle
screen costs nothing between changes.

The helpers have no curses dependency, so they are unit-tested with injected
clocks.
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

# A deadline closer than this is treated as "now" so a rounding error cannot spin the loop.
MIN_WAIT_SECONDS = 0.02
# The longest a loop sleeps without looking at the terminal size again.
MAX_WAIT_SECONDS = 1.0


class TimedElements:
    """How soon the text drawn in one frame will next change."""

    def __init__(self) -> None:
        self.owner = threading.get_ident()
        self.after: float | None = None
        self.age_steps: int | None = None

    def wall(self, seconds: float) -> None:
        """Text that changes ``seconds`` from the moment of drawing."""
        self.after = seconds if self.after is None else min(self.after, seconds)

    def age(self, value: int) -> None:
        """An age shown as ``Ns`` below a minute and in whole minutes above it."""
        steps = 1 if value < 59 else 60 - value % 60
        self.age_steps = steps if self.age_steps is None else min(self.age_steps, steps)

    def next_deadline(self, now: float, age_origin: float | None = None) -> float | None:
        """Earliest time at which something drawn needs a repaint, or ``None`` for a static frame.

        Ages come from a snapshot that is advanced one whole second at a time from
        ``age_origin`` (when the snapshot was read), so their changes fall on
        ``age_origin + k`` seconds.  Without an origin they are counted from ``now``.
        """
        candidates: list[float] = []
        if self.after is not None:
            candidates.append(now + max(MIN_WAIT_SECONDS, self.after))
        if self.age_steps is not None:
            if age_origin is None:
                candidates.append(now + self.age_steps)
            else:
                candidates.append(max(now + MIN_WAIT_SECONDS, age_origin + int(now - age_origin) + self.age_steps))
        return min(candidates) if candidates else None


_collector: TimedElements | None = None


@contextmanager
def collect() -> Iterator[TimedElements]:
    """Gather the timing reports made while one frame is drawn (by this thread only)."""
    global _collector
    previous, _collector = _collector, TimedElements()
    try:
        yield _collector
    finally:
        _collector = previous


def note_wall(seconds: float) -> None:
    """Report that text drawn now changes after ``seconds``; a no-op outside :func:`collect`."""
    current = _collector
    if current is not None and current.owner == threading.get_ident():
        current.wall(seconds)


def note_age(value: int) -> None:
    """Report an age drawn now (see :meth:`TimedElements.age`); a no-op outside :func:`collect`."""
    current = _collector
    if current is not None and current.owner == threading.get_ident():
        current.age(value)


class RedrawPlanner:
    """Dirty flag plus one timed deadline."""

    def __init__(self) -> None:
        self.dirty = True
        self.deadline: float | None = None

    def mark(self) -> None:
        self.dirty = True

    def should_draw(self, now: float) -> bool:
        return self.dirty or (self.deadline is not None and now >= self.deadline)

    def drawn(self, deadline: float | None) -> None:
        """Record a completed frame and when the next timed repaint is due."""
        self.dirty = False
        self.deadline = deadline

    def wait(self, now: float, *others: float | None) -> float:
        """Seconds to sleep: the earliest of the deadline and ``others``, within sane bounds."""
        waits = [value for value in others if value is not None]
        if self.deadline is not None:
            waits.append(self.deadline - now)
        if self.dirty:
            return 0.0
        return min(MAX_WAIT_SECONDS, max(0.0, min(waits))) if waits else MAX_WAIT_SECONDS


def timeout_ms(seconds: float) -> int:
    """curses ``timeout`` argument for a wait, rounded up so the loop never wakes early."""
    return max(1, int(seconds * 1000 + 0.999))


def probe_interval(now: float, last_input: float | None, *, fast: float = 0.25, slow: float = 1.0, after: float = 10.0) -> float:
    """How often to probe for new data: ``fast`` while the operator is active, ``slow`` after ``after`` idle seconds."""
    return slow if last_input is not None and now - last_input >= after else fast


class StoreChangeProbe:
    """Cheap "did another connection commit?" check on a persistent read-only SQLite connection.

    ``PRAGMA data_version`` changes only when a different connection commits to
    the database, so comparing two readings answers "is a new snapshot needed?"
    without running the snapshot queries.  Any failure returns ``None``, which a
    caller must treat as "changed".
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: sqlite3.Connection | None = None

    def __call__(self) -> int | None:
        try:
            if self._db is None:
                if not self.path.exists():
                    return None
                db = sqlite3.connect(f"file:{quote(str(self.path))}?mode=ro", uri=True, timeout=0.25, isolation_level=None)
                db.execute("PRAGMA query_only=ON")
                self._db = db
            return int(self._db.execute("PRAGMA data_version").fetchone()[0])
        except (sqlite3.Error, OSError):
            self.close()
            return None

    def close(self) -> None:
        db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass


class BackgroundReload:
    """Re-read a data set on a worker thread when the database changed (or a read was requested).

    ``poll`` is called from the screen loop.  It never blocks on the read: a
    due read starts a thread, and a later ``poll`` returns its result.  A read
    is due when ``request`` was called or when ``probe`` (a cheap change
    counter such as :class:`StoreChangeProbe`) reports a different value from
    the one seen when the previous read started.  Probes run every ``fast``
    seconds while the operator is active and every ``slow`` seconds after
    ``after`` idle seconds.  ``invalidate`` drops any read in flight, so a
    result that predates a synchronous reload is never applied.
    """

    def __init__(
        self,
        load: Callable[[], Any],
        probe: Callable[[], int | None] | None = None,
        *,
        fast: float = 0.25,
        slow: float = 1.0,
        after: float = 10.0,
    ) -> None:
        self.load = load
        self.probe = probe
        self.fast, self.slow, self.after = fast, slow, after
        self._forced = True
        self._seen: int | None = None
        self._rebase = False
        self._next_probe = 0.0
        self._generation = 0
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._result: tuple[int, bool, Any] | None = None

    def request(self) -> None:
        """Read again on the next ``poll``, whatever the probe says."""
        self._forced = True

    def invalidate(self) -> None:
        """Forget a read in flight and request a fresh one."""
        with self._lock:
            self._generation += 1
            self._result = None
        self._forced = True

    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._done.is_set()

    def wait(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds for the read in flight; true when its result is ready."""
        return self._done.wait(timeout) if self.running() or self._result is not None else True

    def wake_in(self, now: float) -> float | None:
        """Seconds until ``poll`` has something to do, or ``None`` when it is waiting on the probe interval alone."""
        if self._result is not None or self._forced:
            return 0.0
        if self.running():
            return 0.05  # a worker is about to publish
        return max(0.0, self._next_probe - now) if self.probe is not None else None

    def poll(self, now: float, last_input: float | None = None, *, hold: bool = False) -> tuple[bool, Any] | None:
        """``(ok, value)`` once a read finished, else ``None``; starts a read when one is due.

        While ``hold`` is true (a form is open, input is queued) nothing is
        applied and nothing is started.
        """
        if hold:
            return None
        with self._lock:
            finished, self._result = self._result, None
        if finished is not None:
            _generation, ok, value = finished
            if self._rebase and self.probe is not None:
                self._seen, self._rebase = self.probe(), False
            if not ok:
                self._next_probe = max(self._next_probe, now + self.slow)
                self._seen = None  # try again at the next probe
            return ok, value
        if self.running():
            return None
        due = self._forced
        reading: int | None = None
        if self.probe is not None and (due or now >= self._next_probe):
            reading = self.probe()
            self._next_probe = now + (probe_interval(now, last_input, fast=self.fast, slow=self.slow, after=self.after)
                                      if reading is not None else self.slow)
            due = due or reading is None or reading != self._seen
        if not due:
            return None
        self._forced = False
        self._seen, self._rebase = reading, self.probe is not None and reading is None
        self._start()
        return None

    def _start(self) -> None:
        generation = self._generation
        self._done.clear()

        def work() -> None:
            try:
                outcome: tuple[int, bool, Any] = (generation, True, self.load())
            except Exception as exc:  # the data is advisory; the caller keeps the last good copy
                outcome = (generation, False, exc)
            with self._lock:
                if generation == self._generation:
                    self._result = outcome
            self._done.set()

        self._thread = threading.Thread(target=work, name="llm-bridge-reload", daemon=True)
        self._thread.start()
