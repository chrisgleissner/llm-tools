"""Shared curses doubles for the Bridge UI tests.

``FakeWindow`` is a recording replacement for a ``curses`` window.  It keeps

* ``calls``  - every draw call as a ``DrawCall(y, x, text, attr, role, extra)``,
  in the order the code made them,
* a cell grid  - what is on screen after the calls, with a *role* for each
  written cell, so ``rows()`` / ``segments()`` / ``frame()`` describe the screen
  the way a person (and a golden file) reads it,
* ``frames``  - a ``Frame`` captured at every ``refresh()`` / ``noutrefresh()``,
* a scripted key queue for ``get_wch()`` / ``getch()`` / ``getkey()``.

Attributes are mapped back to semantic role names.  Bridge draws with
``role_attr(role) | extra``.  ``RoleAttrs`` replaces the process-wide role
attribute table with one unique value per role (the real value depends on the
theme and the terminal), so a recorded draw call can be decoded to
``role`` + ``extra`` flags (``+underline`` and so on) without knowing the theme.
``role.quiet`` (the same colour without bold) is decoded too.

Quick start::

    def test_something(monkeypatch):
        screen = FakeScreen(24, 80, keys=["KEY_DOWN", "\\n"])
        screen.install(monkeypatch)            # role table + curses.* stubs
        bridge_tui._draw_full(screen, state)
        assert screen.rows()[0].startswith("LLM BRIDGE")
        assert screen.role_at(0, 0) == "brand"
        frame = screen.frame()                 # rows + segments + cursor

``FakeWindow`` alone (no ``install``) is enough for code that never asks for a
role: it then records raw attribute integers in ``DrawCall.attr`` and reports
``role`` as ``None``.

Migrating the hand-rolled fakes
-------------------------------
About fifteen test files define their own ``Screen`` / ``FakeWindow`` /
``_Window``.  They differ in three ways, all covered here:

``lines`` (list of row strings)
    ``FakeWindow.lines`` returns every row with trailing blanks kept, like the
    old fakes (rows ``ljust``-ed to the window width).  Tests that compared
    against right-trimmed text should use ``rows()``.
``writes`` (list of ``(y, x, text, attr)``)
    ``FakeWindow.writes`` has the same shape.  ``calls`` is the richer form.
``rows`` as a dict ``{y: text}`` (the ``Window`` in the document test)
    Use ``row(y)`` / ``rows()``.

Replace ``Screen(h, w)`` by ``FakeWindow(h, w)``; tests that need the colour
guide helpers should call ``install(monkeypatch)`` (it also stubs
``curses.doupdate`` / ``curs_set`` / ``nonl`` and so on, which most of the old
autouse ``_quiet_curses`` fixtures did).

Behaviour that matches real curses on purpose: a write below the window or
starting past the right edge raises ``curses.error``; text longer than the
space left on the row is clipped (``overflows`` counts how often, because a
real window would wrap it onto the next row, which Bridge never wants).
"""

from __future__ import annotations

import curses
import importlib
import unicodedata
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

# Modules that own (or re-export) the colour state.  Refactors that move
# ``role_attr`` / ``_ROLE_ATTRS`` / ``initialize_colors`` only need to add the new module here.
COLOR_STATE_MODULES = ("llm_tools.bridge_tui", "llm_tools.bridge_draw")
# Modules that look ``initialize_colors`` / ``sync_window_size`` up by name when they run.
CURSES_HELPER_MODULES = ("llm_tools.bridge_tui", "llm_tools.bridge_draw", "llm_tools.bridge_launcher", "llm_tools.bridge_switcher")

_BOLD = curses.A_BOLD
_EXTRA_FLAGS = (
    (curses.A_REVERSE, "reverse"),
    (curses.A_UNDERLINE, "underline"),
    (curses.A_DIM, "dim"),
    (curses.A_STANDOUT, "standout"),
    (curses.A_BLINK, "blink"),
)
_TOKEN_SHIFT = 24
_TOKEN_MASK = (1 << _TOKEN_SHIFT) - 1


class ScriptExhausted(BaseException):
    """The scripted key queue ran out while the code under test asked for another key.

    Derived from ``BaseException`` so a draw loop's ``except Exception`` cannot swallow it.
    """


def cells_of(char: str) -> int:
    """Terminal cells one character occupies (0 for combining marks, 2 for wide ones)."""
    if unicodedata.combining(char) or unicodedata.category(char) in {"Cf", "Mn", "Me"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} or ord(char) >= 0x1F300 else 1


@dataclass(frozen=True)
class DrawCall:
    """One ``addnstr`` / ``addstr`` call."""

    y: int
    x: int
    text: str
    attr: int
    role: str | None  # decoded semantic role, ``None`` when no table is installed, "none" for attr 0
    extra: tuple[str, ...] = ()  # decoded non-role attribute flags, such as ``underline``

    def __iter__(self):  # lets ``for y, x, text, attr in screen.calls`` keep working
        yield self.y
        yield self.x
        yield self.text
        yield self.attr


@dataclass(frozen=True)
class Frame:
    """What the screen showed at one refresh."""

    height: int
    width: int
    rows: tuple[str, ...]  # right-trimmed text, one string per screen row
    segments: tuple[tuple[tuple[int, str, str], ...], ...]  # per row: (column, text, role)
    cursor: tuple[int, int] | None  # last ``move`` since the previous erase
    cursor_visible: bool | None  # last ``curs_set`` value (None: never set)


class RoleAttrs:
    """A role table with one unique attribute value per role, plus the decoder for it."""

    def __init__(self, roles: Iterable[str] | None = None) -> None:
        if roles is None:
            from llm_tools import bridge_theme

            roles = bridge_theme.ROLES
        self.table: dict[str, int] = {role: ((index + 1) << _TOKEN_SHIFT) | _BOLD for index, role in enumerate(roles)}
        self._names = {value >> _TOKEN_SHIFT: role for role, value in self.table.items()}

    def decode(self, attr: int) -> tuple[str, tuple[str, ...]]:
        """``(role, extra flags)`` of an attribute value built from this table."""
        token = attr >> _TOKEN_SHIFT
        low = attr & _TOKEN_MASK
        extras = [name for flag, name in _EXTRA_FLAGS if low & flag]
        if token == 0:
            if low & _BOLD:
                extras.insert(0, "bold")
            return "none", tuple(extras)
        role = self._names.get(token, f"?{token}")
        if not low & _BOLD:
            role += ".quiet"  # role_attr strips the bold bit for ``.quiet`` roles
        return role, tuple(extras)


class FakeWindow:
    """Recording stand-in for a curses window.  See the module docstring."""

    def __init__(self, height: int, width: int, *, keys: Sequence[object] = (), roles: RoleAttrs | None = None) -> None:
        self.height, self.width = height, width
        self.roles = roles
        self.keys: deque[object] = deque(keys)
        self.calls: list[DrawCall] = []
        self.frames: list[Frame] = []
        self.cursor: tuple[int, int] | None = None
        self.cursor_visible: bool | None = None
        self.timeouts: list[int] = []
        self.keypad_enabled: bool | None = None
        self.overflows = 0
        self.refreshes = 0
        self._cursor_pos = (0, 0)
        self._grid: list[list[tuple[str, int] | None]] = []
        self._reset_grid()

    # --- state ----------------------------------------------------------------------------------
    def _reset_grid(self) -> None:
        self._grid = [[None] * self.width for _ in range(self.height)]

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def getbegyx(self) -> tuple[int, int]:
        return 0, 0

    def getyx(self) -> tuple[int, int]:
        return self._cursor_pos

    def resize(self, height: int, width: int) -> None:
        self.height, self.width = height, width
        self._reset_grid()

    def erase(self) -> None:
        """Blank the window; ``calls`` then holds the draw calls of the frame being drawn."""
        self._reset_grid()
        self.calls = []
        self.cursor = None

    def clear(self) -> None:
        self.erase()

    # --- drawing --------------------------------------------------------------------------------
    def _write(self, y: int, x: int, text: str, attr: int) -> None:
        if not (0 <= y < self.height) or not (0 <= x < self.width):
            raise curses.error(f"write outside the window: y={y} x={x} in {self.height}x{self.width}")
        role: str | None = None
        extra: tuple[str, ...] = ()
        if self.roles is not None:
            role, extra = self.roles.decode(attr)
        self.calls.append(DrawCall(y, x, text, attr, role, extra))
        row = self._grid[y]
        column = x
        for char in text:
            amount = cells_of(char)
            if amount == 0:
                # A combining mark joins the previous cell.
                if column > x and row[column - 1] is not None:
                    previous, previous_attr = row[column - 1]  # type: ignore[misc]
                    row[column - 1] = (previous + char, previous_attr)
                continue
            if column + amount > self.width:
                self.overflows += 1
                break
            row[column] = (char, attr)
            for filler in range(1, amount):
                row[column + filler] = ("", attr)  # continuation cell of a wide character
            column += amount
        self._cursor_pos = (y, min(column, self.width - 1))

    def addnstr(self, y: int, x: int, text: str, count: int, attr: int = 0) -> None:
        self._write(y, x, text[:count], attr)

    def addstr(self, *args: Any) -> None:
        """``addstr(text[, attr])`` at the cursor, or ``addstr(y, x, text[, attr])``."""
        if args and isinstance(args[0], str):
            y, x = self._cursor_pos
            self._write(y, x, args[0], int(args[1]) if len(args) > 1 else 0)
        else:
            y, x, text, *rest = args
            self._write(y, x, text, int(rest[0]) if rest else 0)

    def move(self, y: int, x: int) -> None:
        self.cursor = (y, x)
        self._cursor_pos = (y, x)

    # --- refresh and frame capture --------------------------------------------------------------
    def noutrefresh(self) -> None:
        self.refreshes += 1
        self.frames.append(self.frame())

    def refresh(self) -> None:
        self.noutrefresh()

    def frame(self) -> Frame:
        """The current screen as a ``Frame`` (does not count as a refresh)."""
        return Frame(self.height, self.width, tuple(self.rows()), tuple(tuple(row) for row in self.segments()), self.cursor, self.cursor_visible)

    # --- reading the screen ---------------------------------------------------------------------
    def row(self, y: int) -> str:
        """Row ``y`` as text, trailing blanks removed.  A wide character counts once."""
        return "".join(" " if cell is None else cell[0] for cell in self._grid[y]).rstrip()

    def rows(self) -> list[str]:
        return [self.row(y) for y in range(self.height)]

    @property
    def lines(self) -> list[str]:
        """Rows with trailing blanks kept (the shape the old hand-rolled fakes exposed)."""
        return ["".join(" " if cell is None else cell[0] for cell in row) for row in self._grid]

    @property
    def writes(self) -> list[tuple[int, int, str, int]]:
        return [(call.y, call.x, call.text, call.attr) for call in self.calls]

    def role_name(self, attr: int) -> str:
        return self.roles.decode(attr)[0] if self.roles is not None else str(attr)

    def role_at(self, y: int, x: int) -> str | None:
        """Role of the cell at ``(y, x)``; ``None`` when nothing was written there."""
        cell = self._grid[y][x]
        return None if cell is None else self.role_name(cell[1])

    def segments(self) -> list[list[tuple[int, str, str]]]:
        """Per row, ``(column, text, role)`` runs of written cells with the same attribute.

        Cells that were never written are skipped (they separate runs); blanks that were drawn
        (a selection bar, padding) are kept because they carry a role.  A run ends where the
        attribute changes.  Text is exact, including trailing spaces inside a run.
        """
        out: list[list[tuple[int, str, str]]] = []
        for y in range(self.height):
            runs: list[tuple[int, str, str]] = []
            current: list[str] = []
            start = 0
            attr_of_run: int | None = None
            for x, cell in enumerate(self._grid[y]):
                if cell is None or (cell[0] == "" and attr_of_run is None):
                    if current:
                        runs.append((start, "".join(current), self._label(attr_of_run)))
                        current, attr_of_run = [], None
                    continue
                char, attr = cell
                if attr_of_run is not None and attr != attr_of_run:
                    runs.append((start, "".join(current), self._label(attr_of_run)))
                    current = []
                    attr_of_run = None
                if attr_of_run is None:
                    start, attr_of_run = x, attr
                current.append(char)
            if current:
                runs.append((start, "".join(current), self._label(attr_of_run)))
            out.append(runs)
        return out

    def _label(self, attr: int | None) -> str:
        if attr is None:
            return "none"
        if self.roles is None:
            return str(attr)
        role, extra = self.roles.decode(attr)
        return role + "".join(f"+{flag}" for flag in extra)

    # --- input ----------------------------------------------------------------------------------
    def timeout(self, milliseconds: int) -> None:
        self.timeouts.append(milliseconds)

    def keypad(self, enabled: bool) -> None:
        self.keypad_enabled = bool(enabled)

    def nodelay(self, _enabled: bool) -> None:
        pass

    def leaveok(self, _enabled: bool) -> None:
        pass

    def scrollok(self, _enabled: bool) -> None:
        pass

    def idlok(self, _enabled: bool) -> None:
        pass

    def bkgd(self, *_args: Any) -> None:
        pass

    def push_keys(self, *keys: object) -> None:
        self.keys.extend(keys)

    def _next_key(self) -> object:
        if not self.keys:
            raise ScriptExhausted("scripted key queue is empty")
        return self.keys.popleft()

    def get_wch(self) -> object:
        """Next scripted key: a ``str`` for characters and ``KEY_*`` names, an ``int`` for curses key codes."""
        return self._next_key()

    def getch(self) -> int:
        key = self._next_key()
        return ord(key) if isinstance(key, str) and len(key) == 1 else int(key)  # type: ignore[arg-type]

    def getkey(self) -> str:
        key = self._next_key()
        return key if isinstance(key, str) else curses.keyname(int(key)).decode()  # type: ignore[arg-type]


class FakeScreen(FakeWindow):
    """A ``FakeWindow`` that can also wire the process-wide curses state for a test.

    ``install(monkeypatch)`` does what the many per-file ``_quiet_curses`` fixtures did, and
    the role table of the colour guide test:

    * replaces the role attribute table in every colour-state module with unique values per
      role, so draw calls decode back to role names (``roles`` is set on this screen);
    * stubs ``curses.doupdate`` / ``curs_set`` (recorded as ``cursor_visible``) / ``nonl`` /
      ``use_env`` / ``def_prog_mode`` / ``reset_prog_mode`` / ``endwin`` / ``resizeterm`` /
      ``set_escdelay`` / ``mousemask``;
    * makes ``initialize_colors`` and ``sync_window_size`` no-ops wherever they are bound, so
      a draw loop cannot overwrite the role table or resize the window from the real tty;
    * with ``wrapper=True``, makes ``curses.wrapper(fn)`` call ``fn(self)``.
    """

    def install(self, monkeypatch: Any, *, wrapper: bool = False, roles: RoleAttrs | None = None) -> "FakeScreen":
        table = roles or self.roles or RoleAttrs()
        self.roles = table
        seen: set[int] = set()
        for name in COLOR_STATE_MODULES:
            module = _import_optional(name)
            if module is not None and hasattr(module, "_ROLE_ATTRS"):
                monkeypatch.setattr(module, "_ROLE_ATTRS", table.table)
                seen.add(id(module))
        for name in CURSES_HELPER_MODULES:
            module = _import_optional(name)
            if module is None:
                continue
            if hasattr(module, "initialize_colors"):
                monkeypatch.setattr(module, "initialize_colors", lambda *_a, **_k: True)
            if hasattr(module, "sync_window_size"):
                monkeypatch.setattr(module, "sync_window_size", lambda _window: None)

        def curs_set(value: int) -> int:
            self.cursor_visible = bool(value)
            return 1

        for name, value in (
            ("doupdate", lambda: None),
            ("curs_set", curs_set),
            ("nonl", lambda: None),
            ("use_env", lambda _flag: None),
            ("def_prog_mode", lambda: None),
            ("reset_prog_mode", lambda: None),
            ("endwin", lambda: None),
            ("resizeterm", lambda *_a: None),
            ("set_escdelay", lambda _ms: None),
            ("mousemask", lambda _mask: (0, 0)),
        ):
            monkeypatch.setattr(curses, name, value, raising=False)
        if wrapper:
            monkeypatch.setattr(curses, "wrapper", lambda function, *a, **k: function(self, *a, **k))
        return self


def _import_optional(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None
