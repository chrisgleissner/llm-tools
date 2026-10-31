"""Every non-ASCII glyph Bridge draws, with the ASCII form used when the locale is not UTF-8.

Draw code names a glyph (``glyph("running")``) or passes finished text through
``localize``.  This module is the only place that knows the pairs, so a
terminal started with ``LANG=C`` (typically an SSH login) never receives
bytes it cannot display.  ``ascii_mode`` follows the locale codeset, which is
what curses will emit; ``sys.stdout.encoding`` is no help because Python's
UTF-8 mode reports ``utf-8`` even under the C locale.
"""

from __future__ import annotations

import functools
import locale
import sys
import unicodedata

# name -> (UTF-8, ASCII).  Composite forms come before the single glyphs they
# contain, because ``localize`` replaces in table order.
GLYPHS: dict[str, tuple[str, str]] = {
    "sep": ("·", "|"),
    "updown": ("↑↓", "Up/Down"),
    "updown_slash": ("↑/↓", "Up/Down"),
    "leftright": ("←/→", "Left/Right"),
    "up": ("↑", "Up"),
    "down": ("↓", "Down"),
    "left": ("←", "Left"),
    "dash": ("—", "-"),
    "endash": ("–", "-"),
    "ellipsis": ("…", "..."),
    "arrow": ("→", "->"),
    "times": ("×", "x"),
    "newline": ("↵", "\\n"),
    "cursor": ("▸", ">"),
    "prompt": ("›", ">"),
    "play": ("▶", ">"),
    "vbar": ("│", "|"),
    "hbar": ("─", "-"),
    "hbar_heavy": ("━", "-"),
    "chip": ("▌", "|"),
    "square": ("■", "#"),
    # State glyphs (the colour is never the only cue).
    "running": ("●", "*"),
    "here": ("●", "*"),
    "waiting": ("○", "o"),
    "review": ("◆", "#"),
    "done": ("✓", "v"),
    "failed": ("✕", "x"),
    "paused": ("‖", "="),
    # Bars (capacity and usage dashboard).
    "bar_full": ("█", "#"),
    "bar_empty": ("░", "."),
    "bar_fill": ("▮", "#"),
    "bar_hollow": ("▯", "."),
    "bar_half": ("▒", ":"),
}

SPINNER_UTF8 = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "-\\|/"


def _locale_codeset() -> str:
    """Codeset of the active locale, which is what curses will emit."""
    try:
        return locale.nl_langinfo(locale.CODESET)
    except (AttributeError, ValueError, OSError):
        return getattr(sys.stdout, "encoding", "") or ""


@functools.lru_cache(maxsize=1)
def ascii_mode() -> bool:
    """True when the locale codeset is not UTF-8 (``LANG=C`` over SSH)."""
    return _locale_codeset().upper().replace("-", "").replace("_", "") != "UTF8"


def glyph(name: str) -> str:
    """The glyph called ``name`` in the active character set."""
    utf8, plain = GLYPHS[name]
    return plain if ascii_mode() else utf8


def spinner_frames() -> str:
    """Animation frames for the working indicator in the active character set."""
    return SPINNER_ASCII if ascii_mode() else SPINNER_UTF8


def localize(text: str) -> str:
    """Replace the known glyphs, then any other non-ASCII, when the locale is not UTF-8."""
    if not ascii_mode() or text.isascii():
        return text
    for utf8, plain in GLYPHS.values():
        text = text.replace(utf8, plain)
    for frame in SPINNER_UTF8:
        text = text.replace(frame, SPINNER_ASCII[0])
    out = []
    for char in text:
        if char.isascii():
            out.append(char)
        else:
            folded = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode("ascii")
            out.append(folded or "?")
    return "".join(out)


# One-cell forms for text laid out in fixed columns (the Usage table).  The words
# ``localize`` uses ("Down") are wider than the glyph, which shifts every column
# to the right of the cell, so the table needs a form that is one cell wide.
_CELL_FORMS = {"✓": "+", "↑": "^", "↓": "v", "…": ".", "—": "-", "–": "-", "→": ">", "←": "<"}


def localize_cells(text: str) -> str:
    """``localize`` that maps every character to exactly one ASCII character, so columns keep their width."""
    if not ascii_mode() or text.isascii():
        return text
    single = {utf8: plain for utf8, plain in GLYPHS.values() if len(utf8) == 1 and len(plain) == 1}
    out = []
    for char in text:
        if char.isascii():
            out.append(char)
        elif char in _CELL_FORMS:
            out.append(_CELL_FORMS[char])
        elif char in single:
            out.append(single[char])
        elif char in SPINNER_UTF8:
            out.append(SPINNER_ASCII[0])
        else:
            folded = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode("ascii")
            out.append(folded[:1] or "?")
    return "".join(out)


# State vocabulary shared by the console, the home screen and the mission picker:
# status word -> (glyph name, theme role).  The glyph and the word are the
# non-colour cue; the role only adds colour.
STATE_LOOK: dict[str, tuple[str, str]] = {
    "RUNNING": ("running", "state_running"), "ACTIVE": ("running", "state_running"),
    "READY": ("waiting", "state_waiting"), "QUEUED": ("waiting", "state_waiting"),
    "CREATED": ("waiting", "state_waiting"), "WAITING": ("waiting", "state_waiting"),
    "INTENT": ("waiting", "state_waiting"), "PREPARED": ("waiting", "state_waiting"),
    "VERIFYING": ("review", "state_review"), "REVIEW": ("review", "state_review"),
    "COMPLETED": ("done", "state_done"), "DONE": ("done", "state_done"), "SUCCEEDED": ("done", "state_done"),
    "FAILED": ("failed", "state_failed"), "BLOCKED": ("failed", "state_failed"),
    "UNAVAILABLE": ("failed", "state_failed"), "ERROR": ("failed", "state_failed"),
    "CANCELLED": ("failed", "state_paused"),
    "PAUSED": ("paused", "state_paused"), "PAUSING": ("paused", "state_paused"),
    "STOPPED": ("paused", "state_paused"), "STOPPING": ("paused", "state_paused"),
}
FINISHED_STATES = frozenset({"COMPLETED", "CANCELLED", "DONE", "SUCCEEDED"})


def state_look(status: object) -> tuple[str, str] | None:
    """(glyph name, role) for a status word, or None when it is not a known state."""
    return STATE_LOOK.get(str(status).strip().upper())


def state_text(status: object, *, unicode: bool | None = None) -> str:
    """`● RUNNING`: the state glyph and word.  `unicode` forces the character set."""
    word = str(status).strip().upper()
    look = STATE_LOOK.get(word)
    if look is None:
        return word
    mark = glyph(look[0]) if unicode is None else GLYPHS[look[0]][0 if unicode else 1]
    return f"{mark} {word}"


def provider_role(name: object) -> str:
    """`provider_*` theme role for a provider or route name."""
    lowered = str(name).strip().lower()
    for provider in ("codex", "claude", "copilot"):
        if lowered.startswith(provider):
            return f"provider_{provider}"
    return "provider_other"
