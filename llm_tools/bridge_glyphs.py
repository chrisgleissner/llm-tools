"""Every non-ASCII glyph Bridge draws, with the ASCII form used when the locale is not UTF-8.

Draw code names a glyph (``glyph("running")``) or passes finished text through
``localize``.  This module is the only place that knows the pairs, so a
terminal started with ``LANG=C`` (typically an SSH login) never receives
bytes it cannot display.  ``ascii_mode`` follows the locale codeset, which is
what curses will emit; ``sys.stdout.encoding`` is no help because Python's
UTF-8 mode reports ``utf-8`` even under the C locale.

Python 3.7+ coerces the C locale to UTF-8 (PEP 538), so the codeset alone says
"UTF-8" for ``LANG=C``.  ``ascii_mode`` therefore also reads the locale the user
set (``LC_ALL``, ``LC_CTYPE``, ``LANG``), and ``LLM_BRIDGE_ASCII=1`` forces ASCII.
"""

from __future__ import annotations

import functools
import locale
import os
import sys
import unicodedata

from .bridge_vocab import MISSION_INTERRUPTED_LABEL, MISSION_NEEDS_YOU_LABEL, status_label

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
    "square": ("■", "@"),
    # State glyphs (the colour is never the only cue).
    "running": ("●", "*"),
    "here": ("●", "*"),
    "waiting": ("○", "o"),
    "review": ("◆", "?"),
    "done": ("✓", "+"),
    "failed": ("✕", "!"),
    "cancelled": ("⊘", "-"),
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


_FALSE_WORDS = {"", "0", "false", "no", "off"}
_C_LOCALES = {"C", "POSIX"}


def _ascii_forced() -> bool:
    """``LLM_BRIDGE_ASCII`` set to anything but empty, ``0``, ``false``, ``no`` or ``off``."""
    return os.environ.get("LLM_BRIDGE_ASCII", "").strip().lower() not in _FALSE_WORDS


def _is_c_locale_name(name: str) -> bool:
    """``C`` or ``POSIX`` without a UTF-8 codeset (``C.UTF-8`` is a real UTF-8 locale)."""
    base, _, codeset = name.split("@")[0].partition(".")
    return base in _C_LOCALES and codeset.lower().replace("-", "") != "utf8"


def _user_locale_is_c() -> bool:
    """True when the user's own locale settings select the C/POSIX locale, whatever Python coerced it to.

    The effective locale is the first of ``LC_ALL``, ``LC_CTYPE`` and ``LANG`` that is set.
    PEP 538 exports ``LC_CTYPE=C.UTF-8`` for a C locale when ``LC_ALL`` is unset, so that
    value together with ``LANG=C`` is the coercion's own trace and still counts as C.
    """
    env = os.environ
    effective = env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or ""
    if _is_c_locale_name(effective):
        return True
    if not env.get("LC_ALL") and env.get("LC_CTYPE", "").lower().replace("-", "") == "c.utf8":
        return _is_c_locale_name(env.get("LANG", ""))
    return False


@functools.lru_cache(maxsize=1)
def ascii_mode() -> bool:
    """True for ASCII glyphs: ``LLM_BRIDGE_ASCII=1``, a C/POSIX locale as the user set it, or a non-UTF-8 codeset."""
    if _ascii_forced() or _user_locale_is_c():
        return True
    return _locale_codeset().upper().replace("-", "").replace("_", "") != "UTF8"


def glyph_report() -> str:
    """One line for ``llm-bridge theme``: the active glyph set, why, and how to switch."""
    if not ascii_mode():
        return "UTF-8 (set LLM_BRIDGE_ASCII=1, or use LC_ALL=C, for plain ASCII symbols)"
    if _ascii_forced():
        return "ASCII (LLM_BRIDGE_ASCII is set)"
    if _user_locale_is_c():
        return "ASCII (the locale is C or POSIX; use a UTF-8 locale such as en_US.UTF-8 for symbols)"
    return "ASCII (the terminal locale is not UTF-8)"


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
    MISSION_NEEDS_YOU_LABEL: ("review", "state_review"),
    MISSION_INTERRUPTED_LABEL: ("failed", "state_failed"),
    "COMPLETED": ("done", "state_done"), "DONE": ("done", "state_done"), "SUCCEEDED": ("done", "state_done"),
    "FAILED": ("failed", "state_failed"), "BLOCKED": ("failed", "state_failed"),
    "UNAVAILABLE": ("failed", "state_failed"), "ERROR": ("failed", "state_failed"),
    "CANCELLED": ("cancelled", "state_paused"),
    "PAUSED": ("paused", "state_paused"), "PAUSING": ("paused", "state_paused"),
    "STOPPED": ("paused", "state_paused"), "STOPPING": ("paused", "state_paused"),
}
FINISHED_STATES = frozenset({"COMPLETED", "CANCELLED", "DONE", "SUCCEEDED"})


def state_look(status: object) -> tuple[str, str] | None:
    """(glyph name, role) for a status word, or None when it is not a known state."""
    return STATE_LOOK.get(str(status).strip().upper())


def state_text(status: object, *, unicode: bool | None = None) -> str:
    """`● RUNNING`: the state glyph and word.  `unicode` forces the character set."""
    word = status_label(str(status).strip().upper())
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
