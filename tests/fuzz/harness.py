"""Sandbox, fake curses windows, watchdog and case bookkeeping shared by every fuzz target."""

from __future__ import annotations

import contextlib
import curses
import hashlib
import json
import os
import random
import signal
import sqlite3
import stat
import sys
import traceback
import types
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

# ------------------------------------------------------------------- case notes

_NOTES: list[str] = []


def note(label: str, value: object) -> None:
    """Remember an input of the running case so a failure report can show it."""
    text = repr(value)
    _NOTES.append(f"{label}: {text if len(text) <= 1500 else text[:1500] + '...<%d chars>' % len(text)}")
    del _NOTES[:-40]


def reset_notes() -> None:
    _NOTES.clear()


def current_notes() -> list[str]:
    return list(_NOTES)


# ---------------------------------------------------------------------- failures

class FuzzFailure(AssertionError):
    """An invariant of a fuzz case was violated."""


class Hang(BaseException):
    """The per-case watchdog expired (BaseException so ``except Exception`` cannot hide a hang)."""


class StreamExhausted(BaseException):
    """The scripted input ran out; the loop under test is abandoned here (not a failure)."""


@contextlib.contextmanager
def watchdog(seconds: float) -> Iterator[None]:
    """Raise :class:`Hang` in the main thread when ``seconds`` of wall time pass."""
    if seconds <= 0 or not hasattr(signal, "setitimer"):
        yield
        return
    try:
        previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Hang(f"case exceeded {seconds:.0f}s")))
    except ValueError:  # not the main thread
        yield
        return
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@dataclass
class Failure:
    target: str
    seed: int
    kind: str
    message: str
    trace: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"target": self.target, "seed": self.seed, "kind": self.kind, "message": self.message, "traceback": self.trace, "inputs": self.notes}

    def summary(self) -> str:
        return f"{self.target} seed={self.seed} [{self.kind}] {self.message}"


def rng_for(target: str, seed: int) -> random.Random:
    return random.Random(f"bridge-fuzz:{target}:{seed}")


def run_case(name: str, function: Callable[[random.Random, "Sandbox"], None], seed: int, sandbox: "Sandbox", *, timeout: float = 20.0) -> Failure | None:
    """Run one seeded case; return a :class:`Failure` or ``None``."""
    reset_notes()
    rng = rng_for(name, seed)
    try:
        with watchdog(timeout):
            function(rng, sandbox)
    except StreamExhausted:
        return None
    except Hang as exc:
        return Failure(name, seed, "hang", str(exc), traceback.format_exc(), current_notes())
    except FuzzFailure as exc:
        return Failure(name, seed, "invariant", str(exc), traceback.format_exc(), current_notes())
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - every escape is a finding
        return Failure(name, seed, "crash", f"{type(exc).__name__}: {exc}", traceback.format_exc(), current_notes())
    return None


# ----------------------------------------------------------------------- patching

_MISSING = object()


class Patcher:
    """Minimal monkeypatch usable outside pytest: attributes and environment, undone in reverse order."""

    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []

    def setattr(self, target: object, name: str, value: object) -> None:
        old = getattr(target, name, _MISSING)
        setattr(target, name, value)
        self._undo.append(lambda: delattr(target, name) if old is _MISSING else setattr(target, name, old))

    def setenv(self, name: str, value: str) -> None:
        old = os.environ.get(name)
        os.environ[name] = value
        self._undo.append(lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None))

    def delenv(self, name: str) -> None:
        old = os.environ.pop(name, None)
        if old is not None:
            self._undo.append(lambda: os.environ.__setitem__(name, old))

    def chdir(self, path: Path) -> None:
        old = os.getcwd()
        os.chdir(path)
        self._undo.append(lambda: os.chdir(old))

    def undo(self) -> None:
        while self._undo:
            try:
                self._undo.pop()()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "Patcher":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.undo()


class FakeClock:
    """Real monotonic time plus scripted jumps, so 0.12 s and 2 s input timers can be crossed instantly."""

    def __init__(self) -> None:
        self.offset = 0.0

    def advance(self, seconds: float) -> None:
        self.offset += seconds

    def module(self) -> types.SimpleNamespace:
        import time as real

        shim = types.SimpleNamespace(**{name: getattr(real, name) for name in dir(real) if not name.startswith("__")})
        shim.monotonic = lambda: real.monotonic() + self.offset
        return shim


# --------------------------------------------------------------------- sandbox

_STUB = "#!/bin/sh\necho 'fuzz harness: provider launch blocked' >&2\nexit 123\n"


class Sandbox:
    """A private state dir, HOME, PATH (provider stubs) and cwd for one run of cases."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.patcher = Patcher()
        self.state = self.root / "state"
        self.work = self.root / "work"
        self.home = self.root / "home"

    def __enter__(self) -> "Sandbox":
        for path in (self.state, self.work, self.home, self.root / "stubs", self.root / "run", self.root / "cache", self.root / "config"):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        os.chmod(self.root / "run", 0o700)
        for name in ("codex", "claude", "copilot", "kilo", "mmx", "opencode", "tmux", "xclip", "xsel"):
            stub = self.root / "stubs" / name
            stub.write_text(_STUB)
            stub.chmod(0o700)
        patch = self.patcher
        patch.setenv("LLM_BRIDGE_STATE_DIR", str(self.state))
        patch.setenv("HOME", str(self.home))
        patch.setenv("XDG_CACHE_HOME", str(self.root / "cache"))
        patch.setenv("XDG_CONFIG_HOME", str(self.root / "config"))
        patch.setenv("XDG_RUNTIME_DIR", str(self.root / "run"))
        patch.setenv("PATH", str(self.root / "stubs") + os.pathsep + os.environ.get("PATH", ""))
        patch.setenv("LLM_TOOLS_NO_INHIBIT", "1")
        patch.setenv("LLM_USAGE_NO_SERVICE", "1")
        patch.setenv("LLM_BRIDGE_NO_OSC", "1")
        patch.setenv("LLM_BRIDGE_THEME", "dark")
        patch.setenv("EDITOR", "true")
        patch.setenv("TERM", "xterm-256color")
        for name in ("VISUAL", "LLM_TOOLS_CONFIG", "LLM_USAGE_PROVIDERS", "NO_COLOR", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "ZAI_API_KEY", "TMUX", "TMUX_PANE"):
            patch.delenv(name)
        patch.chdir(self.work)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.patcher.undo()

    # -- helpers -------------------------------------------------------------

    def fresh_state(self) -> Path:
        """Replace the state dir with an empty one (a new database)."""
        import shutil

        shutil.rmtree(self.state, ignore_errors=True)
        self.state.mkdir(mode=0o700)
        os.chmod(self.state, 0o700)
        return self.state

    def database(self) -> Path:
        return self.state / "bridge.sqlite3"


def db_rows(path: Path, tables: tuple[str, ...] | None = None) -> dict[str, list[str]]:
    """Every row of every table (or of ``tables``) as sorted text, read through a read-only connection."""
    if not path.exists():
        return {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {
            table: sorted(repr(tuple(row)) for row in db.execute(f'SELECT * FROM "{table}"'))
            for table in names if tables is None or table in tables
        }
    finally:
        db.close()


def db_fingerprint(path: Path, tables: tuple[str, ...] | None = None) -> str:
    """Hash of every row of every table (or of ``tables``)."""
    if not path.exists():
        return "absent"
    digest = hashlib.sha256()
    for table, rows in db_rows(path, tables).items():
        digest.update(table.encode())
        for row in rows:
            digest.update(row.encode("utf-8", "replace"))
    return digest.hexdigest()


def db_diff(before: dict[str, list[str]], after: dict[str, list[str]], limit: int = 6) -> str:
    """A short description of the rows that differ, for failure messages."""
    lines = []
    for table in sorted(set(before) | set(after)):
        old, new = set(before.get(table, [])), set(after.get(table, []))
        for row in sorted(new - old)[:limit]:
            lines.append(f"+ {table}: {row[:300]}")
        for row in sorted(old - new)[:limit]:
            lines.append(f"- {table}: {row[:300]}")
    return "\n".join(lines) or "no row differences"


# ------------------------------------------------------------------ fake curses

def display_width(text: str) -> int:
    """Independent cell-width measure for the bounds checks (not the code under test)."""
    width = 0
    for char in text:
        category = unicodedata.category(char)
        if category in {"Mn", "Me", "Cf"}:
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} or ord(char) >= 0x1F300 else 1
    return width


class FakeWindow:
    """A curses window double that records every out-of-bounds or unsafe write.

    ``addnstr`` raises :class:`curses.error` for a start position outside the
    window (as real curses does) and records a violation when text that starts
    inside would run past the right edge (real curses would wrap it), contains
    control characters, or cannot be encoded as UTF-8.
    """

    def __init__(self, height: int, width: int, *, feed: list[Any] | None = None, clock: FakeClock | None = None, strict_origin: bool = True) -> None:
        self.height = height
        self.width = width
        self.violations: list[str] = []
        self.offscreen = 0
        self.writes = 0
        self.cursor: tuple[int, int] | None = None
        self.feed = feed if feed is not None else []
        self.clock = clock
        self.mouse: tuple[int, int, int] | None = None
        self.reads = 0
        self.last: object = None  # the value the latest get_wch returned
        self.strict_origin = strict_origin
        self.rows: list[list[str]] = [[" "] * width for _ in range(height)]
        self.timeout_ms = -1

    # -- geometry
    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def resize(self, height: int, width: int) -> None:
        self.height, self.width = max(1, height), max(1, width)
        self.rows = [[" "] * self.width for _ in range(self.height)]

    def erase(self) -> None:
        self.rows = [[" "] * self.width for _ in range(self.height)]

    clear = erase

    # -- drawing
    def addnstr(self, y: int, x: int, text: str, count: int = -1, attr: int = 0) -> None:
        self.writes += 1
        if not (0 <= y < self.height and 0 <= x < self.width):
            self.offscreen += 1
            raise curses.error("addnstr() returned ERR")
        chunk = text if count < 0 else text[:count]
        try:
            chunk.encode("utf-8")
        except UnicodeEncodeError:
            self.violations.append(f"unencodable text written at {y},{x}: {chunk!r}")
            raise curses.error("encode") from None
        if any(unicodedata.category(char) in {"Cc", "Cs"} for char in chunk):
            self.violations.append(f"control character written at {y},{x}: {chunk!r}")
        cells = display_width(chunk)
        if x + cells > self.width:
            self.violations.append(f"write overflows the window: {cells} cells at x={x} in width {self.width}: {chunk!r}")
        if not isinstance(attr, int):
            self.violations.append(f"attribute is not an int: {attr!r}")

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        self.addnstr(y, x, text, -1, attr)

    def move(self, y: int, x: int) -> None:
        if not (0 <= y < self.height and 0 <= x < self.width):
            raise curses.error("move() returned ERR")
        self.cursor = (y, x)

    def noutrefresh(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    def keypad(self, _enabled: bool) -> None:
        pass

    def timeout(self, milliseconds: int) -> None:
        self.timeout_ms = milliseconds

    def nodelay(self, _flag: bool) -> None:
        pass

    # -- input
    def get_wch(self) -> object:
        while True:
            self.reads += 1
            if not self.feed:
                raise StreamExhausted()
            item = self.feed.pop(0)
            if isinstance(item, tuple):
                tag = item[0]
                if tag == "mouse":
                    self.mouse = (item[1], item[2], item[3])
                elif tag == "resize":
                    self.resize(item[1], item[2])
                elif tag == "pause":
                    if self.clock is not None:
                        self.clock.advance(item[1])
                    raise curses.error("no input")
                elif tag == "invalid":
                    # ncurses cannot decode a UTF-8 sequence beyond U+10FFFF and Python raises ValueError, not curses.error
                    raise ValueError("chr() arg not in range(0x110000)")
                continue
            self.last = item
            return item

    def getch(self) -> int:
        item = self.get_wch()
        return item if isinstance(item, int) else ord(item[0]) if item else -1

    def check(self, label: str = "") -> None:
        if self.violations:
            sample = "; ".join(self.violations[:3])
            raise FuzzFailure(f"{label} wrote outside its window or unsafely ({len(self.violations)}x): {sample}")


@contextlib.contextmanager
def fake_curses(patcher: Patcher | None = None, *, window: FakeWindow | None = None) -> Iterator[Patcher]:
    """Replace the module-level curses entry points the Bridge screens call."""
    own = patcher is None
    patch = patcher or Patcher()

    def getmouse() -> tuple[int, int, int, int, int]:
        if window is None or window.mouse is None:
            raise curses.error("no mouse event")
        x, y, buttons = window.mouse
        window.mouse = None
        return 0, x, y, 0, buttons

    noop = lambda *_a, **_k: None  # noqa: E731
    for name in ("curs_set", "nonl", "set_escdelay", "use_env", "mousemask", "doupdate", "resizeterm", "def_prog_mode", "endwin", "reset_prog_mode", "start_color",
                 "use_default_colors", "init_pair", "unget_wch", "flushinp", "beep"):
        patch.setattr(curses, name, noop)
    patch.setattr(curses, "getmouse", getmouse)
    patch.setattr(curses, "has_colors", lambda: False)
    patch.setattr(curses, "color_pair", lambda _n: 0)
    patch.setattr(curses, "keyname", lambda _n: b"?")
    try:
        yield patch
    finally:
        if own:
            patch.undo()


def assert_private_dir(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise FuzzFailure(f"{path} is not private: {oct(mode)}")


def json_dump_safe(value: object) -> str:
    return json.dumps(value, default=repr, ensure_ascii=False)


def quiet_stdout() -> contextlib.AbstractContextManager[Any]:
    """Discard writes to stdout/stderr (screen loops emit mode-switch escapes)."""
    return _Redirect(open(os.devnull, "w"))


class _Redirect:
    def __init__(self, sink: Any) -> None:
        self.sink = sink
        self.saved: tuple[Any, Any] | None = None

    def __enter__(self) -> "_Redirect":
        self.saved = (sys.stdout, sys.stderr)
        sys.stdout = sys.stderr = self.sink
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self.saved is not None
        sys.stdout, sys.stderr = self.saved
        self.sink.close()


