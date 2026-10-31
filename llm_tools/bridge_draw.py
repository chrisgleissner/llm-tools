"""Draw primitives and colour state shared by every Bridge curses screen.

The console (``bridge_tui``), the home screen, the mission switcher and the
viewers all draw through this module: clipped single-line writes, coloured
segments, the semantic colour-role table, window-size sync and the key-hint
splitter. It also holds ``ControlViewAction`` and the view-name vocabulary that
the mission switcher and the console both need, so neither imports the other.

Colour state (``_ROLE_ATTRS``, ``_ROLE_PAIRS``, ``_CURRENT_THEME``) lives here.
``bridge_tui`` re-exports the functions and answers reads of the three state
names through its module ``__getattr__``; code that replaces the state (tests
that stub the role table) must patch this module.
"""

from __future__ import annotations

import curses
import os
import re
import sys
import unicodedata
from dataclasses import dataclass

from . import bridge_theme
from .bridge_glyphs import glyph, localize
from .bridge_input import cell_slice, cell_width
from .bridge_theme import ROLES as THEME_ROLES, Style, fit_style
from .bridge_vocab import ViewName


VIEWS: tuple[str, ...] = tuple(str(view) for view in ViewName)


_VIEW_ALIASES = {
    "bridge": "Overview", "control": "Overview",
    "orders": "Tasks", "tasks": "Tasks",
    "stations": "Providers", "providers": "Providers",
    "log": "Activity", "events": "Activity",
    "systems": "Health", "system": "Health",
    "palette": "Actions",
}


def normalize_view(name: str) -> str:
    """Map a current or pre-rename view name (any case) to a name in VIEWS."""
    text = str(name).strip()
    for view in VIEWS:
        if text.casefold() == view.casefold():
            return view
    return _VIEW_ALIASES.get(text.casefold(), text)


@dataclass(frozen=True)
class ControlViewAction:
    """Open a parsed control command after restoring the current terminal."""

    mission_id: str
    view: str

    def __post_init__(self) -> None:
        # Callers may still pass a pre-rename view name ("Bridge", "Orders", ...).
        object.__setattr__(self, "view", normalize_view(self.view))


_COLOR_ROLES = THEME_ROLES
_ROLE_PAIRS: dict[str, int] = {}
_ROLE_ATTRS: dict[str, int] = {}
_CURRENT_THEME = "dark"


def _style_attrs(style: Style) -> int:
    attr = curses.A_BOLD if style.bold else 0
    if style.dim:
        attr |= curses.A_DIM
    if style.underline:
        attr |= curses.A_UNDERLINE
    if style.reverse:
        attr |= curses.A_REVERSE
    return attr


def initialize_colors(env: dict[str, str] | None = None, *, is_tty: bool | None = None) -> bool:
    """Initialize the Bridge semantic palette for the active curses terminal.

    The theme comes from ``bridge_theme`` (explicit env/config choice, else the
    detected terminal background).  With ``NO_COLOR`` / ``LLM_USAGE_NO_COLOR``
    or a terminal without colour, roles keep monochrome attributes (bold,
    underline, reverse); ``TERM=dumb`` and non-terminals get no styling at all.
    The terminal's own background is preserved except for the ``selected`` bar.
    """
    global _CURRENT_THEME, _ROLE_PAIRS, _ROLE_ATTRS
    values = os.environ if env is None else env
    resolved = bridge_theme.resolve_cached() if env is None else bridge_theme.resolve(values)
    _CURRENT_THEME = resolved.polarity
    _ROLE_PAIRS = {}
    _ROLE_ATTRS = {}
    if is_tty is None:
        is_tty = sys.stdout.isatty()
    if not is_tty or values.get("TERM", "") == "dumb":
        return False
    plain = bridge_theme.resolve_cached() if env is None else bridge_theme.resolve(values)
    if plain.name != "mono":
        plain = bridge_theme.resolve(None if env is None else values, theme_name="mono")
    mono = {role: _style_attrs(plain.style(role)) for role in _COLOR_ROLES}
    # `curses.wrapper` has already called start_color(), which makes the default
    # pair white-on-black and paints a black slab on a light terminal. Mono
    # (attributes only) must keep the terminal's own colours, so default colours
    # are enabled before any decision to draw without colour.
    try:
        capable = curses.has_colors()
        if capable:
            curses.start_color()
    except (AttributeError, curses.error):
        capable = False
        started = False
    else:
        started = True
    if capable:
        try:
            curses.use_default_colors()
        except (AttributeError, curses.error):
            pass
    if bridge_theme.colors_disabled(values) or not capable or not started:
        _ROLE_ATTRS = mono
        return False

    default_colors = False
    try:
        curses.use_default_colors()
        default_colors = True
    except (AttributeError, curses.error):
        pass

    neutral = curses.COLOR_WHITE if _CURRENT_THEME == "dark" else curses.COLOR_BLACK
    fallback_bg = -1 if default_colors else (curses.COLOR_WHITE if _CURRENT_THEME == "light" else curses.COLOR_BLACK)
    for pair, role in enumerate(_COLOR_ROLES, start=1):
        if pair >= curses.COLOR_PAIRS:
            _ROLE_ATTRS[role] = mono[role]
            continue
        style = fit_style(resolved.style(role), curses.COLORS, _CURRENT_THEME)
        foreground_color = -1 if style.fg is None else style.fg
        if foreground_color < 0 and not default_colors:
            foreground_color = neutral
        background_color = fallback_bg if style.bg is None else style.bg
        try:
            curses.init_pair(pair, foreground_color, background_color)
        except curses.error:
            _ROLE_ATTRS[role] = mono[role]
            continue
        _ROLE_PAIRS[role] = pair
        _ROLE_ATTRS[role] = curses.color_pair(pair) | _style_attrs(style)
    return bool(_ROLE_PAIRS)


def sync_window_size(window: curses.window) -> None:
    """Use the owned tty geometry even when ncurses cached a detached size."""
    try:
        size = os.get_terminal_size(sys.stdin.fileno())
        if window.getmaxyx() != (size.lines, size.columns):
            curses.resizeterm(size.lines, size.columns)
            window.resize(size.lines, size.columns)
    except (OSError, ValueError, AttributeError, curses.error):
        pass


QUIET = ".quiet"


def role_attr(role: str) -> int:
    """Return the current curses attribute for a semantic Bridge color role.

    `ROLE.quiet` is the same colour without bold. Historical failures and the
    blocked tasks inside a `Needs you` row use it, so that the one bold amber
    `Needs you` label stays the only element drawing the eye (colour guide rule 4).
    """
    if role.endswith(QUIET):
        return role_attr(role[: -len(QUIET)]) & ~curses.A_BOLD
    if role == "selected" and "selected" not in _ROLE_ATTRS:
        base = _ROLE_ATTRS.get("brand", 0)
        return base | curses.A_BOLD | curses.A_REVERSE
    if role == "secondary":
        role = "muted" if "muted" in _ROLE_ATTRS else "dim"
    return _ROLE_ATTRS.get(role, 0)


def _scrub(text: str, *, keep_newline: bool = False, drop_format: bool = True) -> str:
    """Replace control and surrogate characters with spaces and (optionally) drop format characters.

    Only the distinct characters of `text` are classified, so the cost does not
    grow with the length of repetitive text.
    """
    if text.isascii() and text.isprintable():
        return text  # printable ASCII has no control or format characters
    table: dict[int, str | None] = {}
    for char in set(text):
        if char.isascii() and char.isprintable():
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cs"}:
            if not (keep_newline and char == "\n"):
                table[ord(char)] = " "
        elif drop_format and category == "Cf":
            table[ord(char)] = None  # bidi overrides, zero-width marks
    return text.translate(table) if table else text


def _put(
    window: curses.window,
    y: int,
    x: int,
    value: object,
    width: int,
    *,
    role: str | None = None,
    attr: int = 0,
    preserve_spacing: bool = False,
) -> int:
    """Draw one clipped single-line run and return the terminal cells it used."""
    if width <= 0:
        return 0
    # Spacing is layout (columns, separators): sanitize control characters but
    # never collapse runs of spaces. `_text` is for untrusted values only.
    # `localize` runs first so an ASCII locale clips by the cells it really draws.
    text = value if isinstance(value, str) else str(value)
    size = width * 4 + 256
    while True:
        # Only the first `width` cells are drawn. Clean a bounded prefix and grow
        # it when zero-width or removed characters left the prefix too narrow.
        safe = localize(_scrub(text[:size].expandtabs(8)))
        if size >= len(text) or cell_width(safe) > width:
            break
        size *= 4
    rendered = cell_slice(safe, 0, width)
    style = attr | (role_attr(role) if role else 0)
    try:
        window.addnstr(y, x, rendered, width, style)
    except TypeError:
        # Keep lightweight recording windows compatible with curses' optional
        # attr argument; real curses windows always accept it.
        try:
            window.addnstr(y, x, rendered, width)
        except curses.error:
            pass
    except curses.error:
        pass
    return cell_width(rendered)


# --- multi-segment lines --------------------------------------------------------
# A segment is (text, role). Colour is applied to the smallest element that
# carries the meaning, so a task row is several segments, not one role.

Segment = tuple[str, str]


def _segments_text(segments: list[Segment]) -> str:
    return "".join(text for text, _role in segments)


def _segments_width(segments: list[Segment]) -> int:
    return sum(cell_width(localize(text)) for text, _role in segments)


def _clip_segments(segments: list[Segment], width: int, *, mark: bool = True) -> list[Segment]:
    """Clip segments to `width` cells; with `mark`, the cut ends in an ellipsis."""
    if width <= 0:
        return []
    if _segments_width(segments) <= width:
        return segments
    ellipsis = glyph("ellipsis") if mark else ""
    room = max(0, width - cell_width(ellipsis))
    out: list[Segment] = []
    used = 0
    for text, role in segments:
        text = localize(text)
        amount = cell_width(text)
        if used + amount <= room:
            out.append((text, role))
            used += amount
            continue
        piece = cell_slice(text, 0, room - used)
        if piece:
            out.append((piece, role))
        if mark:
            out.append((ellipsis, out[-1][1] if out else "info"))
        break
    return out


def _put_segments(
    window: curses.window,
    y: int,
    x: int,
    segments: list[Segment],
    width: int,
    *,
    attr: int = 0,
) -> int:
    """Draw (text, role) segments left to right, clipped by cell width; return cells used."""
    used = 0
    for text, role in segments:
        if used >= width:
            break
        if text:
            used += _put(window, y, x + used, text, width - used, role=role, attr=attr)
    return used


def _merge_segments(segments: list[Segment]) -> list[Segment]:
    merged: list[Segment] = []
    for text, role in segments:
        if not text:
            continue
        if merged and merged[-1][1] == role:
            merged[-1] = (merged[-1][0] + text, role)
        else:
            merged.append((text, role))
    return merged


_HINT_KEY = re.compile(
    r"^(?P<key>(?:Ctrl-\](?: [A-Za-z])?|Ctrl-[A-Za-z0-9]|Shift-\S+|F\d|PgUp|PgDn|Enter|Esc|Tab|\u2191\u2193|\?|/[a-z]+|/|[A-Za-z])"
    r"(?:/(?:Ctrl-\S|F[\d-]+|PgUp|PgDn|PgDn|\u2191\u2193|Shift-\S+|\S))*)(?:\s+(?P<verb>.*))?$"
)
_HINT_PLAIN = ("type ",)


def _split_hint(item: str) -> tuple[str, str]:
    """(key, action) of one hint item; text that names no key is all action."""
    if item.startswith(_HINT_PLAIN):
        return "", item
    match = _HINT_KEY.match(item)
    if not match:
        return "", item
    key = match.group("key")
    verb = match.group("verb") or ""
    if len(key) == 1 and key.isalpha() and not verb:
        return "", item  # a lone word, not a key
    return key, verb
