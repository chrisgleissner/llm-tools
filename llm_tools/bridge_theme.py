"""Semantic colour roles, built-in themes and background detection for Bridge.

Rendering code colours text by *meaning* through a role name; the theme decides
what that meaning looks like on the user's terminal.  The same resolved theme
drives the curses UI (``bridge_tui``), the tmux status line (``terminal``) and
the ``llm-bridge theme`` command.

Roles (names are stable; add, never rename)
-------------------------------------------

Text and structure:
    ``brand``        product name, primary emphasis
    ``heading``      section and view headings
    ``info``         ordinary text (terminal default foreground)
    ``accent``       focus: active tab, focused field, cursor marker
    ``muted``        secondary text (IDs, timestamps, hints); stays readable
    ``dim``          separators, borders, disabled text (decorative)
    ``key``          keyboard shortcut hints such as ``Ctrl-D``
    ``selected``     selection bar; the only role with a background fill
    ``command``      commands and shell lines
    ``tool``         tool calls
    ``stderr``       provider diagnostic output

Severity and diff:
    ``ok`` ``warn`` ``error``                  outcome messages
    ``diff_add`` ``diff_remove`` ``diff_hunk`` diff output

Task and mission state:
    ``state_running``  actively working
    ``state_waiting``  queued or ready, nothing needed from you
    ``state_review``   verifying, or needs your decision
    ``state_done``     completed
    ``state_failed``   failed, blocked or cancelled with an error
    ``state_paused``   paused or stopped by the operator

Capacity health (quota remaining):
    ``capacity_ok`` ``capacity_low`` ``capacity_empty``

Provider identity:
    ``provider_codex`` ``provider_claude`` ``provider_copilot`` ``provider_other``

Mission identity (a stable hue per mission; chip form only, never a state colour):
    ``mission_1`` teal, ``mission_2`` azure, ``mission_3`` blue, ``mission_4``
    indigo, ``mission_5`` violet, ``mission_6`` magenta, ``mission_7`` pink,
    ``mission_8`` slate (ordered by hue; at least 10 apart in OKLab x100).  :func:`mission_role` picks one from the mission ID (or the
    mission's pinned ``colour``); :func:`mission_chip_style` /
    :func:`mission_chip_sgr` / :func:`tmux_mission_chip_markup` give the filled
    chip form (hue as background, contrast-checked foreground) used by the tmux
    station's left status segment.

Themes: ``dark``, ``light``, ``high-contrast`` (bold pure colours, adapts to the
detected background) and ``mono`` (attributes only).  ``auto`` (default) picks
``dark`` or ``light`` from the detected terminal background.

Background detection order (first match wins; result cached per process):
    1. ``LLM_BRIDGE_THEME`` (name; ``dark``/``light`` fix the background)
    2. ``[bridge.theme] name`` in the llm-tools config
    3. ``LLM_BRIDGE_BACKGROUND`` (``dark``/``light``; also exported by
       :func:`prepare` so a tmux station started from this process inherits it)
       and ``[bridge.theme] background``
    4. ``COLORFGBG`` (KDE Konsole, rxvt)
    5. OSC 11 query of the controlling terminal (skipped when stdin/stdout are
       not a tty, inside tmux/screen, with ``LLM_BRIDGE_NO_OSC=1``, or when
       colour is disabled)
    6. macOS ``defaults read -g AppleInterfaceStyle`` (Darwin, Terminal.app or
       iTerm2 only, never over SSH)
    7. ``dark``

Overrides: ``[bridge.theme.roles]`` in the config, then ``LLM_TOOLS_COLOR_<ROLE>``,
then ``LLM_BRIDGE_COLOR_<ROLE>`` (highest).  A value is a raw SGR string
(``1;38;5;203``), a hex colour (``#ff8800``), an ANSI colour name (``red``,
``bright-cyan``), ``fg:N``/``bg:N``/``fg:#hex``/``bg:#hex``, and the flags
``bold`` ``dim`` ``underline`` ``reverse``, separated by spaces.  Config also
accepts a table: ``{ color = "#ff8800", bg = "#202020", bold = true }``.
Invalid values are ignored and reported as warnings.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping

from .bridge_states import MISSION_COUNT, MISSION_HUE_NAMES, THEME_CHOICES, THEME_NAMES  # noqa: F401  (re-exported; shared with the CLI parser)

try:  # POSIX only; detection simply skips the terminal query elsewhere.
    import termios
except ImportError:  # pragma: no cover - non-POSIX platforms
    termios = None  # type: ignore[assignment]

Theme = Literal["dark", "light"]
Polarity = Theme


def __getattr__(name: str) -> Any:
    # ``platform`` is imported where it is used (macOS detection only); the module attribute stays readable.
    if name == "platform":
        import platform

        return platform
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_THEME_ALIASES = {
    "hc": "high-contrast",
    "high_contrast": "high-contrast",
    "highcontrast": "high-contrast",
    "monochrome": "mono",
    "none": "mono",
}

MISSION_ROLES: tuple[str, ...] = tuple(f"mission_{n}" for n in range(1, MISSION_COUNT + 1))

ROLE_DESCRIPTIONS: dict[str, str] = {
    "brand": "product name and primary emphasis",
    "info": "ordinary text (terminal default foreground)",
    "ok": "success messages",
    "warn": "warnings and cautions",
    "error": "errors and rejections",
    "dim": "separators, borders, disabled text",
    "diff_add": "added lines in a diff",
    "diff_remove": "removed lines in a diff",
    "diff_hunk": "diff hunk headers",
    "command": "commands and shell lines",
    "tool": "tool calls",
    "stderr": "provider diagnostic output",
    "heading": "section and view headings",
    "accent": "focus: active tab, focused field, cursor marker",
    "muted": "secondary text such as IDs, times and hints",
    "key": "keyboard shortcut hints",
    "selected": "selection bar (the only filled background)",
    "state_running": "actively working",
    "state_waiting": "queued or ready; nothing needed from you",
    "state_review": "verifying, or needs your decision",
    "state_done": "completed",
    "state_failed": "failed or blocked",
    "state_paused": "paused or stopped by the operator",
    "capacity_ok": "plenty of quota remaining",
    "capacity_low": "quota running low",
    "capacity_empty": "quota exhausted",
    "provider_codex": "Codex",
    "provider_claude": "Claude Code",
    "provider_copilot": "GitHub Copilot",
    "provider_other": "any other provider",
    **{f"mission_{n}": f"mission identity hue {n} of {MISSION_COUNT}" for n in range(1, MISSION_COUNT + 1)},
}
ROLES: tuple[str, ...] = tuple(ROLE_DESCRIPTIONS)

# Roles that are separators or disabled text rather than something to read.
DECORATIVE_ROLES: frozenset[str] = frozenset({"dim"})
TEXT_CONTRAST = 4.5
DECORATIVE_CONTRAST = 3.0
# Eight/sixteen-colour palettes are chosen by the terminal, so only a coarse
# check against the standard xterm values is possible (WCAG non-text level).
LOW_COLOR_CONTRAST = 3.0

# Typical terminal backgrounds the built-in palettes are verified against.
DARK_BACKGROUNDS: dict[str, tuple[int, int, int]] = {
    "#1e1e1e": (0x1E, 0x1E, 0x1E),
    "#300a24 (Ubuntu aubergine)": (0x30, 0x0A, 0x24),
    "#000000": (0, 0, 0),
}
LIGHT_BACKGROUNDS: dict[str, tuple[int, int, int]] = {
    "#ffffff": (255, 255, 255),
    "#fafafa": (0xFA, 0xFA, 0xFA),
    "#f6f6ef": (0xF6, 0xF6, 0xEF),
}

# --- palettes ---------------------------------------------------------------
# SGR strings so users can copy a value straight into an override.  "39" is the
# terminal's own foreground, which is always right for the user's background.

_DARK: dict[str, str] = {
    "brand": "1;38;5;39",
    "info": "39",
    "ok": "1;38;5;77",
    "warn": "1;38;5;220",
    "error": "1;38;5;203",
    "dim": "38;5;245",
    "diff_add": "38;5;77",
    "diff_remove": "38;5;210",
    "diff_hunk": "1;38;5;75",
    "command": "1;38;5;74",
    "tool": "1;38;5;80",
    "stderr": "38;5;174",
    "heading": "1;38;5;117",
    "accent": "1;38;5;81",
    "muted": "38;5;248",
    "key": "1;38;5;150",
    "selected": "1;38;5;231;48;5;25",
    "state_running": "1;38;5;78",
    "state_waiting": "38;5;110",
    "state_review": "1;38;5;214",
    "state_done": "38;5;108",
    "state_failed": "1;38;5;203",
    "state_paused": "38;5;176",
    "capacity_ok": "38;5;77",
    "capacity_low": "38;5;214",
    "capacity_empty": "1;38;5;203",
    "provider_codex": "38;5;79",
    "provider_claude": "38;5;209",
    "provider_copilot": "38;5;141",
    "provider_other": "38;5;110",
    "mission_1": "38;5;43",
    "mission_2": "38;5;159",
    "mission_3": "38;5;33",
    "mission_4": "38;5;147",
    "mission_5": "38;5;135",
    "mission_6": "38;5;200",
    "mission_7": "38;5;211",
    "mission_8": "38;5;102",
}

_LIGHT: dict[str, str] = {
    "brand": "1;38;5;25",
    "info": "39",
    "ok": "1;38;5;22",
    "warn": "1;38;5;94",
    "error": "1;38;5;160",
    "dim": "38;5;243",
    "diff_add": "38;5;22",
    "diff_remove": "38;5;124",
    "diff_hunk": "1;38;5;25",
    "command": "1;38;5;25",
    "tool": "1;38;5;23",
    "stderr": "38;5;238",
    "heading": "1;38;5;18",
    "accent": "1;38;5;61",
    "muted": "38;5;241",
    "key": "1;38;5;94",
    "selected": "1;38;5;16;48;5;153",
    "state_running": "1;38;5;22",
    "state_waiting": "38;5;24",
    "state_review": "1;38;5;94",
    "state_done": "38;5;60",
    "state_failed": "1;38;5;160",
    "state_paused": "38;5;90",
    "capacity_ok": "38;5;22",
    "capacity_low": "38;5;94",
    "capacity_empty": "1;38;5;160",
    "provider_codex": "38;5;23",
    "provider_claude": "38;5;95",
    "provider_copilot": "38;5;91",
    "provider_other": "38;5;24",
    "mission_1": "38;5;24",
    "mission_2": "38;5;26",
    "mission_3": "38;5;19",
    "mission_4": "38;5;17",
    "mission_5": "38;5;92",
    "mission_6": "38;5;53",
    "mission_7": "38;5;161",
    "mission_8": "38;5;242",
}

_HC_DARK: dict[str, str] = {
    "brand": "1;38;5;51",
    "info": "1",
    "ok": "1;38;5;46",
    "warn": "1;38;5;226",
    "error": "1;38;5;210",
    "dim": "38;5;250",
    "diff_add": "1;38;5;46",
    "diff_remove": "1;38;5;210",
    "diff_hunk": "1;38;5;51",
    "command": "1;38;5;51",
    "tool": "1;38;5;51",
    "stderr": "1;38;5;213",
    "heading": "1;38;5;231",
    "accent": "1;38;5;226",
    "muted": "38;5;252",
    "key": "1;38;5;226",
    "selected": "1;38;5;16;48;5;51",
    "state_running": "1;38;5;46",
    "state_waiting": "1;38;5;51",
    "state_review": "1;38;5;226",
    "state_done": "1;38;5;231",
    "state_failed": "1;38;5;210",
    "state_paused": "1;38;5;213",
    "capacity_ok": "1;38;5;46",
    "capacity_low": "1;38;5;226",
    "capacity_empty": "1;38;5;210",
    "provider_codex": "1;38;5;46",
    "provider_claude": "1;38;5;214",
    "provider_copilot": "1;38;5;213",
    "provider_other": "1;38;5;51",
    "mission_1": "1;38;5;159",
    "mission_2": "1;38;5;44",
    "mission_3": "1;38;5;153",
    "mission_4": "1;38;5;147",
    "mission_5": "1;38;5;177",
    "mission_6": "1;38;5;219",
    "mission_7": "1;38;5;211",
    "mission_8": "1;38;5;249",
}

_HC_LIGHT: dict[str, str] = {
    "brand": "1;38;5;18",
    "info": "1",
    "ok": "1;38;5;22",
    "warn": "1;38;5;58",
    "error": "1;38;5;88",
    "dim": "38;5;240",
    "diff_add": "1;38;5;22",
    "diff_remove": "1;38;5;88",
    "diff_hunk": "1;38;5;18",
    "command": "1;38;5;18",
    "tool": "1;38;5;91",
    "stderr": "1;38;5;53",
    "heading": "1;38;5;16",
    "accent": "1;38;5;54",
    "muted": "38;5;238",
    "key": "1;38;5;58",
    "selected": "1;38;5;231;48;5;18",
    "state_running": "1;38;5;22",
    "state_waiting": "1;38;5;18",
    "state_review": "1;38;5;58",
    "state_done": "1;38;5;16",
    "state_failed": "1;38;5;88",
    "state_paused": "1;38;5;53",
    "capacity_ok": "1;38;5;22",
    "capacity_low": "1;38;5;58",
    "capacity_empty": "1;38;5;88",
    "provider_codex": "1;38;5;22",
    "provider_claude": "1;38;5;58",
    "provider_copilot": "1;38;5;54",
    "provider_other": "1;38;5;18",
    "mission_1": "1;38;5;24",
    "mission_2": "1;38;5;21",
    "mission_3": "1;38;5;17",
    "mission_4": "1;38;5;56",
    "mission_5": "1;38;5;91",
    "mission_6": "1;38;5;53",
    "mission_7": "1;38;5;126",
    "mission_8": "1;38;5;239",
}

# Used when the terminal background could not be detected (SSH, no COLORFGBG, no
# OSC 11 answer).  No colour reaches 4.5:1 on both white and black (the best
# mid-tone manages about 3.6:1 on all typical backgrounds), so the palette keeps
# every hue at that best achievable level, makes bold carry emphasis, and uses
# the terminal's own foreground with the dim attribute for secondary text and
# reverse video for the selection.  ``LLM_BRIDGE_THEME=light|dark`` replaces it.
_NEUTRAL: dict[str, str] = {
    "brand": "1;38;5;33", "info": "39", "ok": "1;38;5;28", "warn": "1;38;5;166", "error": "1;38;5;196",
    "dim": "38;5;244", "diff_add": "38;5;28", "diff_remove": "38;5;167", "diff_hunk": "1;38;5;33",
    "command": "1;38;5;31", "tool": "1;38;5;30", "stderr": "38;5;131", "heading": "1",
    "accent": "1;38;5;31", "muted": "2", "key": "1;38;5;130", "selected": "1;7",
    "state_running": "1;38;5;28", "state_waiting": "39", "state_review": "1;38;5;130",
    "state_done": "38;5;29", "state_failed": "1;38;5;196", "state_paused": "38;5;134",
    "capacity_ok": "38;5;28", "capacity_low": "1;38;5;130", "capacity_empty": "1;38;5;196",
    "provider_codex": "38;5;29", "provider_claude": "38;5;166", "provider_copilot": "38;5;98",
    "provider_other": "38;5;33",
    "mission_1": "1;38;5;33", "mission_2": "1;38;5;30", "mission_3": "1;38;5;98", "mission_4": "1;38;5;164",
    "mission_5": "1;38;5;132", "mission_6": "1;38;5;102", "mission_7": "1;38;5;29", "mission_8": "1;38;5;99",
}

# Attributes only, as the colour guide specifies: bold for state_review, error
# and failures, underline for the focus (accent), reverse for the selection, and
# nothing else on state or capacity roles.  Structure keeps its emphasis (bold
# wordmark, headings and key names; dim for secondary text) because without it
# nothing would separate labels from values.  The state glyph and word carry
# the meaning.
_MONO: dict[str, str] = {
    "brand": "1", "info": "", "ok": "1", "warn": "1", "error": "1", "dim": "2",
    "diff_add": "1", "diff_remove": "2", "diff_hunk": "1", "command": "1", "tool": "",
    "stderr": "2", "heading": "1", "accent": "4", "muted": "2", "key": "1",
    "selected": "1;7", "state_running": "", "state_waiting": "", "state_review": "1",
    "state_done": "2", "state_failed": "1", "state_paused": "2",
    "capacity_ok": "", "capacity_low": "", "capacity_empty": "1",
    "provider_codex": "", "provider_claude": "", "provider_copilot": "", "provider_other": "",
    **{role: "" for role in MISSION_ROLES},
}

PALETTES: dict[tuple[str, str], dict[str, str]] = {
    ("dark", "dark"): _DARK,
    ("light", "light"): _LIGHT,
    ("high-contrast", "dark"): _HC_DARK,
    ("high-contrast", "light"): _HC_LIGHT,
    ("neutral", "dark"): _NEUTRAL,
    ("neutral", "light"): _NEUTRAL,
    ("mono", "dark"): _MONO,
    ("mono", "light"): _MONO,
}

# --- colour maths ------------------------------------------------------------

_XTERM_BASE_RGB = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)
_XTERM_CUBE = (0, 95, 135, 175, 215, 255)
_ANSI_NAMES = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")


def colors_disabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return (
        "NO_COLOR" in values
        or values.get("TERM", "") == "dumb"
        or values.get("LLM_USAGE_NO_COLOR", "").strip().lower() not in {"", "0", "false", "no"}
    )


def _xterm_rgb(index: int) -> tuple[int, int, int]:
    if 0 <= index < 16:
        return _XTERM_BASE_RGB[index]
    if 16 <= index <= 231:
        value = index - 16
        return (
            _XTERM_CUBE[value // 36],
            _XTERM_CUBE[(value // 6) % 6],
            _XTERM_CUBE[value % 6],
        )
    gray = 8 + (index - 232) * 10
    return (gray, gray, gray)


def xterm_rgb(index: int) -> tuple[int, int, int]:
    """RGB of an xterm-256 palette entry (slots 0-15 use xterm's defaults)."""
    return _xterm_rgb(max(0, min(255, index)))


def _distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> int:
    return sum((a - b) ** 2 for a, b in zip(left, right))


def nearest_256(rgb: tuple[int, int, int]) -> int:
    """Nearest cube/greyscale entry (16-255); slots 0-15 vary per terminal."""
    return min(range(16, 256), key=lambda candidate: _distance(_xterm_rgb(candidate), rgb))


def parse_hex_color(text: str) -> tuple[int, int, int]:
    value = text.strip().lstrip("#")
    if len(value) == 3 and re.fullmatch(r"[0-9a-fA-F]{3}", value):
        value = "".join(char * 2 for char in value)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError(f"invalid hex colour {text!r} (expected #rrggbb)")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue



def contrast_ratio(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    """WCAG 2 contrast ratio between two sRGB colours (1.0 to 21.0)."""
    high, low = sorted((relative_luminance(left), relative_luminance(right)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def is_light_background(rgb: tuple[int, int, int]) -> bool:
    """True when dark text contrasts more with ``rgb`` than light text does."""
    return relative_luminance(rgb) > 0.179


# --- styles ------------------------------------------------------------------


@dataclass(frozen=True)
class Style:
    """A role's appearance: optional xterm-256 fg/bg plus attributes."""

    fg: int | None = None
    bg: int | None = None
    bold: bool = False
    dim: bool = False
    underline: bool = False
    reverse: bool = False

    def sgr(self) -> str:
        parts: list[str] = []
        if self.bold:
            parts.append("1")
        if self.dim:
            parts.append("2")
        if self.underline:
            parts.append("4")
        if self.reverse:
            parts.append("7")
        if self.fg is not None:
            parts.append(_color_sgr(self.fg, 30))
        if self.bg is not None:
            parts.append(_color_sgr(self.bg, 40))
        return ";".join(parts)

    def monochrome(self) -> "Style":
        return Style(None, None, self.bold, self.dim, self.underline, self.reverse)


def _color_sgr(index: int, base: int) -> str:
    if index < 8:
        return str(base + index)
    if index < 16:
        return str(base + 60 + index - 8)
    return f"{base + 8};5;{index}"


_FLAGS = {"bold": "bold", "dim": "dim", "faint": "dim", "underline": "underline", "reverse": "reverse"}
_SGR_RE = re.compile(r"[0-9;]*")


def _ansi_name_index(name: str) -> int | None:
    name = name.lower().replace("_", "-")
    bright = name.startswith("bright-")
    base = name[7:] if bright else name
    if base in _ANSI_NAMES:
        return _ANSI_NAMES.index(base) + (8 if bright else 0)
    return None


def parse_color(text: str) -> int:
    """Parse ``#rrggbb``, ``0``-``255`` or an ANSI colour name to an index."""
    value = text.strip()
    if value.startswith("#"):
        return nearest_256(parse_hex_color(value))
    if value.isdigit() and 0 <= int(value) <= 255:
        return int(value)
    named = _ansi_name_index(value)
    if named is not None:
        return named
    raise ValueError(f"invalid colour {text!r} (use #rrggbb, 0-255 or an ANSI colour name)")


def parse_sgr(sgr: str, *, strict: bool = False) -> Style:
    """Decode an SGR parameter string into a :class:`Style`.

    Non-strict decoding ignores codes it does not model, matching terminal
    behaviour; strict decoding raises ``ValueError`` so overrides can be
    rejected with a message.
    """
    if not _SGR_RE.fullmatch(sgr.strip()):
        raise ValueError(f"invalid SGR string {sgr!r}")
    codes = [int(part) if part else 0 for part in sgr.strip().split(";")]
    fg: int | None = None
    bg: int | None = None
    bold = dim = underline = reverse = False
    index = 0
    while index < len(codes):
        code = codes[index]
        if code == 0:
            fg = bg = None
            bold = dim = underline = reverse = False
        elif code == 1:
            bold = True
        elif code == 2:
            dim = True
        elif code == 4:
            underline = True
        elif code == 7:
            reverse = True
        elif code == 22:
            bold = dim = False
        elif code == 24:
            underline = False
        elif code == 27:
            reverse = False
        elif code == 39:
            fg = None
        elif code == 49:
            bg = None
        elif 30 <= code <= 37:
            fg = code - 30
        elif 90 <= code <= 97:
            fg = code - 90 + 8
        elif 40 <= code <= 47:
            bg = code - 40
        elif 100 <= code <= 107:
            bg = code - 100 + 8
        elif code in (38, 48):
            target: int | None = None
            if index + 2 < len(codes) and codes[index + 1] == 5:
                if strict and codes[index + 2] > 255:
                    raise ValueError(f"colour index {codes[index + 2]} is above 255 in SGR {sgr!r}")
                target = max(0, min(255, codes[index + 2]))
                index += 2
            elif index + 4 < len(codes) and codes[index + 1] == 2:
                if strict and max(codes[index + 2 : index + 5]) > 255:
                    raise ValueError(f"colour channel above 255 in SGR {sgr!r}")
                rgb =tuple(max(0, min(255, channel)) for channel in codes[index + 2 : index + 5])
                target = nearest_256(rgb)  # type: ignore[arg-type]
                index += 4
            elif strict:
                raise ValueError(f"incomplete extended colour in SGR {sgr!r}")
            if code == 38:
                fg = target
            else:
                bg = target
        elif strict:
            raise ValueError(f"unsupported SGR code {code} in {sgr!r}")
        index += 1
    return Style(fg, bg, bold, dim, underline, reverse)


def sgr_foreground(sgr: str) -> tuple[int | None, bool, bool, bool]:
    """Decode foreground index plus bold/dim/underline attributes from SGR."""
    try:
        style = parse_sgr(sgr)
    except ValueError:
        return None, False, False, False
    return style.fg, style.bold, style.dim, style.underline


def parse_style(value: Any) -> Style:
    """Parse a user override: string, or config table; raises ``ValueError``."""
    if isinstance(value, Mapping):
        allowed = {"color", "fg", "bg", "bold", "dim", "underline", "reverse"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown key(s) {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(allowed))})")
        flags: dict[str, bool] = {}
        for flag in ("bold", "dim", "underline", "reverse"):
            if flag in value:
                if not isinstance(value[flag], bool):
                    raise ValueError(f"{flag} must be true or false")
                flags[flag] = value[flag]
        fg_text = value.get("color", value.get("fg"))
        for key, text in (("color", fg_text), ("bg", value.get("bg"))):
            if text is not None and not isinstance(text, str):
                raise ValueError(f"{key} must be a string")
        return Style(
            parse_color(fg_text) if fg_text is not None else None,
            parse_color(value["bg"]) if value.get("bg") is not None else None,
            **flags,
        )
    if not isinstance(value, str):
        raise ValueError("expected a string or a table")
    text = value.strip()
    if _SGR_RE.fullmatch(text):
        return parse_sgr(text, strict=True)
    fg: int | None = None
    bg: int | None = None
    flags = {}
    for token in text.replace(",", " ").split():
        lowered = token.lower()
        if lowered in _FLAGS:
            flags[_FLAGS[lowered]] = True
        elif lowered in {"normal", "plain", "default"}:
            continue
        elif _SGR_RE.fullmatch(token):
            inner = parse_sgr(token, strict=True)
            fg = inner.fg if inner.fg is not None else fg
            bg = inner.bg if inner.bg is not None else bg
            for name in ("bold", "dim", "underline", "reverse"):
                if getattr(inner, name):
                    flags[name] = True
        elif lowered.startswith(("bg:", "on:")):
            bg = parse_color(token.split(":", 1)[1])
        elif lowered.startswith("fg:"):
            fg = parse_color(token.split(":", 1)[1])
        else:
            fg = parse_color(token)
    return Style(fg, bg, **flags)


# --- low-colour fitting ------------------------------------------------------


def fit_color(index: int | None, colors: int, default: int = -1) -> int:
    """Map an xterm-256 or ANSI color to the terminal's available palette."""
    if index is None:
        return default
    if colors <= 0:
        return default
    if index < colors:
        return index
    candidate_count = min(colors, 256)
    rgb = _xterm_rgb(index)
    return min(range(candidate_count), key=lambda candidate: _distance(_xterm_rgb(candidate), rgb))


def fit_role_color(
    index: int | None,
    colors: int,
    theme: Theme,
    *,
    min_contrast: float = LOW_COLOR_CONTRAST,
) -> int:
    """Pick a legible foreground on a terminal with fewer than 256 colours.

    Terminals with 256 or more colours keep the palette index unchanged.  With
    8 or 16 colours the nearest ANSI colour that still contrasts with the
    detected background is used, so blue never lands on black and yellow,
    green or cyan never land on white.  If nothing contrasts, the theme's
    neutral (white on dark, black on light) is used.
    """
    neutral = 7 if theme == "dark" else 0
    if index is None or colors <= 0:
        return neutral
    if colors >= 256:
        return index
    background = (0, 0, 0) if theme == "dark" else (255, 255, 255)
    target = _xterm_rgb(index)
    candidates = [
        candidate
        for candidate in range(min(colors, 256))
        if contrast_ratio(_xterm_rgb(candidate), background) >= min_contrast
    ]
    if not candidates:
        return neutral
    return min(candidates, key=lambda candidate: _distance(_xterm_rgb(candidate), target))


def fit_style(style: Style, colors: int, theme: Theme) -> Style:
    """Adapt a style to the terminal's colour count without losing meaning.

    A filled background cannot be matched reliably with few colours, so it
    becomes reverse video.  Bold on a light 8-colour terminal usually selects
    the bright (low-contrast) colour, so it is dropped there.
    """
    if colors >= 256 or colors <= 0:
        return style
    if style.bg is not None:
        return Style(None, None, style.bold, style.dim, style.underline, True)
    if style.fg is None:
        return style
    fg = fit_role_color(style.fg, colors, theme)
    bold = style.bold and not (theme == "light" and colors <= 8)
    return Style(fg, None, bold, style.dim, style.underline, style.reverse)


def terminal_colors(env: Mapping[str, str] | None = None) -> int:
    """Best-effort colour count from the environment (no terminfo needed)."""
    values = os.environ if env is None else env
    term = values.get("TERM", "")
    if "256" in term or values.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return 256
    if "16color" in term:
        return 16
    if term in {"", "dumb"}:
        return 0
    if term.startswith(("xterm", "screen", "tmux", "rxvt", "linux", "vt", "ansi", "putty")):
        return 8
    return 256


# --- detection ---------------------------------------------------------------

_OSC11_QUERY = b"\x1b]11;?\x07\x1b[c"  # background query, then DA1 as a sentinel
_OSC11_REPLY = re.compile(rb"\x1b\]11;([^\x07\x1b]*)(?:\x07|\x1b\\)")
_DA1_REPLY = re.compile(rb"\x1b\[\?[0-9;]*c")
DEFAULT_OSC_TIMEOUT = 0.1


def parse_osc11_color(payload: str) -> tuple[int, int, int] | None:
    """Parse an OSC 11 reply colour (``rgb:rrrr/gggg/bbbb`` or ``#rrggbb``)."""
    match = re.fullmatch(
        r"rgba?:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})(?:/[0-9a-fA-F]{1,4})?",
        payload.strip(),
    )
    if match:
        channels = [round(int(group, 16) / (16 ** len(group) - 1) * 255) for group in match.groups()]
        return channels[0], channels[1], channels[2]
    try:
        return parse_hex_color(payload) if payload.strip().startswith("#") else None
    except ValueError:
        return None


def query_terminal_background(
    timeout: float = DEFAULT_OSC_TIMEOUT,
    *,
    in_fd: int | None = None,
    out_fd: int | None = None,
) -> tuple[int, int, int] | None:
    """Ask the terminal for its background colour with OSC 11.

    The query is followed by a DA1 request: every terminal answers DA1, and it
    answers after OSC 11, so seeing the DA1 reply proves the OSC 11 reply (if
    any) has already arrived.  That ends the wait at once on terminals without
    OSC 11 support and leaves no late reply behind to be typed into the UI.
    Terminal modes are restored and pending input is flushed on every path.
    """
    if termios is None:
        return None
    try:
        in_fd = sys.stdin.fileno() if in_fd is None else in_fd
        out_fd = sys.stdout.fileno() if out_fd is None else out_fd
        if not (os.isatty(in_fd) and os.isatty(out_fd)):
            return None
        saved = termios.tcgetattr(in_fd)
    except (ValueError, OSError, AttributeError, termios.error):
        return None
    raw = list(saved)
    raw[3] &= ~(termios.ECHO | termios.ICANON)
    raw[6] = list(saved[6])
    raw[6][termios.VMIN] = 0
    raw[6][termios.VTIME] = 0
    received = b""
    try:
        termios.tcsetattr(in_fd, termios.TCSANOW, raw)
        termios.tcflush(in_fd, termios.TCIFLUSH)
        os.write(out_fd, _OSC11_QUERY)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([in_fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(in_fd, 4096)
            if not chunk:
                break
            received += chunk
            if _DA1_REPLY.search(received):
                break
    except (OSError, ValueError):
        pass
    finally:
        try:
            termios.tcflush(in_fd, termios.TCIFLUSH)
            termios.tcsetattr(in_fd, termios.TCSADRAIN, saved)
        except (OSError, termios.error):
            pass
    match = _OSC11_REPLY.search(received)
    if not match:
        return None
    return parse_osc11_color(match.group(1).decode("ascii", "replace"))


def _osc_timeout(values: Mapping[str, str]) -> float:
    try:
        millis = float(values.get("LLM_BRIDGE_OSC_TIMEOUT_MS", ""))
    except ValueError:
        return DEFAULT_OSC_TIMEOUT
    return max(10.0, min(1000.0, millis)) / 1000.0


def _osc_skip_reason(values: Mapping[str, str]) -> str | None:
    if values.get("LLM_BRIDGE_NO_OSC", "").strip().lower() not in {"", "0", "false", "no"}:
        return "disabled by LLM_BRIDGE_NO_OSC"
    if values.get("TMUX") or values.get("TERM", "").startswith(("tmux", "screen")) or values.get("STY"):
        return "inside tmux/screen, which answers with its own colours"
    try:
        if not (os.isatty(sys.stdin.fileno()) and os.isatty(sys.stdout.fileno())):
            return "stdin/stdout is not a terminal"
    except (ValueError, OSError, AttributeError):
        return "stdin/stdout is not a terminal"
    return None


def _osc11_background(values: Mapping[str, str]) -> tuple[int, int, int] | None:
    return query_terminal_background(_osc_timeout(values))


def _macos_prefers_dark(values: Mapping[str, str]) -> bool | None:
    """``True`` for dark mode, ``False`` for light, ``None`` when unknown."""
    import platform

    if platform.system() != "Darwin":
        return None
    if values.get("TERM_PROGRAM", "") not in {"Apple_Terminal", "iTerm.app"}:
        return None
    if values.get("SSH_CONNECTION") or values.get("SSH_TTY"):
        return None
    defaults = shutil.which("defaults")
    if not defaults:
        return None
    try:
        result = subprocess.run(
            [defaults, "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=1.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # The key only exists in dark mode; a missing key (exit 1) means light.
    return result.returncode == 0 and result.stdout.strip().lower() == "dark"


def _background_is_light(colorfgbg: str) -> bool | None:
    parts = colorfgbg.split(";")
    if not parts or not parts[-1].isdecimal() or not parts[-1].isascii() or len(parts[-1]) > 4:
        return None
    index = int(parts[-1])
    if index < 16:
        # The standard 16-color slots 7 and 15 are the light foregrounds
        # commonly used as a light terminal background in COLORFGBG.
        return index in {7, 15}
    return is_light_background(_xterm_rgb(min(index, 255)))


def _normalise_name(text: str) -> str | None:
    name = text.strip().lower()
    name = _THEME_ALIASES.get(name, name)
    return name if name in THEME_CHOICES else None


def _normalise_polarity(text: str) -> Polarity | None:
    name = text.strip().lower()
    return name if name in {"dark", "light"} else None  # type: ignore[return-value]


# --- resolution --------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedTheme:
    """Everything needed to style Bridge output, plus how it was decided."""

    name: str
    name_source: str
    polarity: Polarity
    polarity_source: str
    background_rgb: tuple[int, int, int] | None
    styles: Mapping[str, Style]
    origins: Mapping[str, str]
    warnings: tuple[str, ...] = ()
    probe_notes: tuple[str, ...] = ()
    colors_disabled: bool = False
    probed: bool = False
    guessed: bool = False  # the background was not detected; the neutral palette is in use

    def style(self, role: str) -> Style:
        return self.styles.get(role) or self.styles["info"]

    def sgr(self, role: str) -> str:
        return self.style(role).sgr()


def _config_overrides(theme_cfg: Mapping[str, Any], warnings: list[str]) -> dict[str, Style]:
    roles = theme_cfg.get("roles")
    if roles is None:
        return {}
    if not isinstance(roles, Mapping):
        warnings.append("[bridge.theme] roles must be a table; ignored")
        return {}
    found: dict[str, Style] = {}
    for role, value in roles.items():
        if role not in ROLES:
            warnings.append(f"[bridge.theme.roles] unknown role '{role}' ignored")
            continue
        try:
            found[role] = parse_style(value)
        except ValueError as exc:
            warnings.append(f"[bridge.theme.roles] {role}: {exc}; ignored")
    return found


def _env_overrides(values: Mapping[str, str], warnings: list[str]) -> dict[str, Style]:
    found: dict[str, Style] = {}
    for prefix in ("LLM_TOOLS_COLOR_", "LLM_BRIDGE_COLOR_"):  # later prefix wins
        for key, text in values.items():
            if not key.startswith(prefix):
                continue
            role = key[len(prefix):].lower()
            if role not in ROLES:
                if prefix == "LLM_BRIDGE_COLOR_":
                    warnings.append(f"{key}: unknown role '{role}' ignored")
                continue
            try:
                found[role] = parse_style(text)
            except ValueError as exc:
                warnings.append(f"{key}: {exc}; ignored")
    return found


def _load_theme_config(values: Mapping[str, str], probe: bool, warnings: list[str]) -> dict[str, Any]:
    if not probe and "LLM_TOOLS_CONFIG" not in values:
        return {}
    from . import config as toolconfig  # the config (and the provider code behind it) is read only when asked for

    try:
        return toolconfig.bridge_theme_config(toolconfig.load_config(dict(values)))
    except SystemExit:
        warnings.append("llm-tools config could not be read; [bridge.theme] ignored")
        return {}
    except (OSError, ValueError, TypeError):  # pragma: no cover - defensive
        return {}


def resolve(
    env: Mapping[str, str] | None = None,
    *,
    probe: bool | None = None,
    theme_cfg: Mapping[str, Any] | None = None,
    theme_name: str | None = None,
    reference: "ResolvedTheme | None" = None,
) -> ResolvedTheme:
    """Resolve the active theme.

    ``env=None`` means the real process: the config file is read and the
    terminal / macOS probes may run.  An explicit mapping is hermetic (no
    probes, no config file unless it names ``LLM_TOOLS_CONFIG``) so tests and
    previews are deterministic.  ``theme_name`` forces a theme (used by
    ``llm-bridge theme --theme``); ``reference`` supplies an already detected
    background so previews do not query the terminal twice.
    """
    real = env is None
    values: Mapping[str, str] = os.environ if real else env  # type: ignore[assignment]
    probe = real if probe is None else probe
    warnings: list[str] = []
    notes: list[str] = []
    cfg = dict(theme_cfg) if theme_cfg is not None else _load_theme_config(values, probe, warnings)

    # Theme name.
    name, name_source = "auto", "default"
    candidates: list[tuple[str, str]] = []
    if theme_name is not None:
        candidates.append((theme_name, "--theme"))
    if values.get("LLM_BRIDGE_THEME", "").strip():
        candidates.append((values["LLM_BRIDGE_THEME"], "LLM_BRIDGE_THEME"))
    if isinstance(cfg.get("name"), str):
        candidates.append((cfg["name"], "[bridge.theme] name"))
    elif "name" in cfg:
        warnings.append("[bridge.theme] name must be a string; ignored")
    for text, source in candidates:
        normalised = _normalise_name(text)
        if normalised is None:
            warnings.append(f"{source}={text!r} is not a theme (use {', '.join(THEME_CHOICES)}); ignored")
            continue
        name = normalised
        name_source = f"{source} = {text.strip()!r}" if source.startswith("[") else f"{source}={text.strip()}"
        break
    disabled = colors_disabled(values)
    if disabled and theme_name is None:
        name, name_source = "mono", "NO_COLOR / TERM=dumb / LLM_USAGE_NO_COLOR"

    # Background polarity.
    background: tuple[int, int, int] | None = None
    probed = False
    polarity: Polarity
    if name in {"dark", "light"}:
        polarity, polarity_source = name, "theme name"  # type: ignore[assignment]
    elif name == "mono":
        polarity, polarity_source = "dark", "not needed for mono"
    elif reference is not None:
        polarity, polarity_source = reference.polarity, reference.polarity_source
        background = reference.background_rgb
        probed = reference.probed
    else:
        polarity, polarity_source, background, probed = _detect_polarity(values, cfg, probe, warnings, notes)

    guessed = name == "auto" and polarity_source.startswith("default (")
    palette_name = "neutral" if guessed else (name if name != "auto" else polarity)
    palette = PALETTES[(palette_name, polarity)]
    styles = {role: parse_sgr(palette.get(role, palette["info"])) for role in ROLES}
    origins = {role: "theme" for role in ROLES}
    overrides = {**_config_overrides(cfg, warnings)}
    origin_of = {role: "config" for role in overrides}
    for role, style in _env_overrides(values, warnings).items():
        overrides[role] = style
        origin_of[role] = "env"
    for role, style in overrides.items():
        styles[role] = style.monochrome() if name == "mono" else style
        origins[role] = origin_of[role]
    if name == "auto":
        name_source = f"auto, following the background ({polarity_source})"
    return ResolvedTheme(
        name=name if name != "auto" else polarity,
        name_source=name_source,
        polarity=polarity,
        polarity_source=polarity_source,
        background_rgb=background,
        styles=styles,
        origins=origins,
        warnings=tuple(warnings),
        probe_notes=tuple(notes),
        colors_disabled=disabled,
        probed=probed,
        guessed=guessed,
    )


def _detect_polarity(
    values: Mapping[str, str],
    cfg: Mapping[str, Any],
    probe: bool,
    warnings: list[str],
    notes: list[str],
) -> tuple[Polarity, str, tuple[int, int, int] | None, bool]:
    explicit = _normalise_polarity(values.get("LLM_BRIDGE_BACKGROUND", ""))
    if explicit:
        return explicit, f"LLM_BRIDGE_BACKGROUND={explicit}", None, False
    if values.get("LLM_BRIDGE_BACKGROUND", "").strip():
        warnings.append(f"LLM_BRIDGE_BACKGROUND={values['LLM_BRIDGE_BACKGROUND']!r} is not dark or light; ignored")
    if "background" in cfg:
        configured = _normalise_polarity(cfg["background"]) if isinstance(cfg["background"], str) else None
        if configured:
            return configured, f"[bridge.theme] background = {configured!r}", None, False
        warnings.append("[bridge.theme] background must be \"dark\" or \"light\"; ignored")
    colorfgbg = values.get("COLORFGBG", "")
    light = _background_is_light(colorfgbg) if colorfgbg else None
    if light is not None:
        return ("light" if light else "dark"), f"COLORFGBG={colorfgbg}", None, False
    if probe:
        reason = _osc_skip_reason(values)
        if reason:
            notes.append(f"OSC 11 query skipped: {reason}")
        else:
            rgb = _osc11_background(values)
            if rgb is not None:
                polarity: Polarity = "light" if is_light_background(rgb) else "dark"
                return polarity, f"OSC 11 terminal query (rgb {rgb[0]},{rgb[1]},{rgb[2]})", rgb, True
            notes.append("OSC 11 query got no answer within the timeout")
        mac = _macos_prefers_dark(values)
        if mac is not None:
            return ("dark" if mac else "light"), "macOS AppleInterfaceStyle", None, True
    else:
        notes.append("terminal probes not run (hermetic resolution)")
    return "dark", "default (no background signal found)", None, False


_CACHED: ResolvedTheme | None = None


def resolve_cached() -> ResolvedTheme:
    """Resolve once per process from the real environment and terminal."""
    global _CACHED
    if _CACHED is None:
        _CACHED = resolve()
    return _CACHED


def reset_cache() -> None:
    global _CACHED
    _CACHED = None


def prepare(stream: Any = None) -> ResolvedTheme:
    """Resolve the theme before curses starts and report invalid settings.

    Probing the terminal must happen before curses owns the input queue.  A
    probed background is exported as ``LLM_BRIDGE_BACKGROUND`` so a tmux
    station started from this process (whose panes answer OSC 11 with tmux's
    own colours) renders with the same theme.  Warnings print once, to stderr.
    """
    resolved = resolve_cached()
    if resolved.probed:
        os.environ.setdefault("LLM_BRIDGE_BACKGROUND", resolved.polarity)
    out = sys.stderr if stream is None else stream
    for message in resolved.warnings:
        print(f"llm-bridge: theme: {message}", file=out)
    if resolved.warnings and stream is None:
        # Printed once per process; later calls would repeat them.
        global _CACHED
        _CACHED = replace(resolved, warnings=())
    return resolved


def resolve_theme(env: Mapping[str, str] | None = None) -> Theme:
    """Background polarity (``dark`` or ``light``) for the given environment."""
    resolved = resolve_cached() if env is None else resolve(env)
    return resolved.polarity


def role_sgr(
    role: str,
    *,
    theme: Theme | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return a role's SGR string, honouring overrides; ``theme`` forces dark/light."""
    if theme is not None:
        resolved = resolve({} if env is None else env, theme_name=theme)
    else:
        resolved = resolve_cached() if env is None else resolve(env)
    return resolved.sgr(role)


def tmux_style_markup(style: Style) -> str:
    parts = ["#[default]"]
    if style.fg is not None:
        parts.append(f"#[fg=colour{style.fg}]")
    if style.bg is not None:
        parts.append(f"#[bg=colour{style.bg}]")
    if style.bold:
        parts.append("#[bold]")
    if style.dim:
        parts.append("#[dim]")
    if style.underline:
        parts.append("#[underscore]")
    if style.reverse:
        parts.append("#[reverse]")
    return "".join(parts)


def tmux_role_markup(role: str, *, env: Mapping[str, str] | None = None) -> str:
    """Return tmux status markup for a role from the resolved theme.

    ``TERM=dumb`` yields plain text; ``NO_COLOR`` keeps the monochrome
    attributes (bold, underline) since they are not colour.
    """
    values = os.environ if env is None else env
    if values.get("TERM", "") == "dumb":
        return "#[default]"
    resolved = resolve_cached() if env is None else resolve(values)
    return tmux_style_markup(resolved.style(role))


# --- mission identity ----------------------------------------------------------


def mission_number(mission_id: object, config: Mapping[str, Any] | None = None) -> int:
    """Identity hue number (1-8): the pinned ``colour`` if valid, else a stable hash of the ID."""
    pinned = config.get("colour") if isinstance(config, Mapping) else None
    if isinstance(pinned, str) and pinned.strip().isdigit():
        pinned = int(pinned.strip())
    if isinstance(pinned, int) and not isinstance(pinned, bool) and 1 <= pinned <= MISSION_COUNT:
        return pinned
    text = str(mission_id or "")
    try:
        return int(text[:8], 16) % MISSION_COUNT + 1
    except ValueError:
        return zlib.crc32(text.encode("utf-8", "replace")) % MISSION_COUNT + 1


def mission_role(mission_id: object, config: Mapping[str, Any] | None = None) -> str:
    """Role name ``mission_N`` for a mission; stable for the mission's whole life."""
    return f"mission_{mission_number(mission_id, config)}"


def _mission_name(which: int | str) -> str:
    name = which if isinstance(which, str) else f"mission_{which}"
    if name not in MISSION_ROLES:
        raise ValueError(f"mission colour must be 1-{MISSION_COUNT}, got {which!r}")
    return name


def mission_chip_style(which: int | str, resolved: "ResolvedTheme") -> Style:
    """Filled chip form of a mission hue: the hue as background, black or white text.

    The text colour is whichever of black and white contrasts more with the hue
    (always at least ``TEXT_CONTRAST``).  Monochrome themes use reverse video.
    """
    hue = resolved.style(_mission_name(which))
    background = hue.bg if hue.bg is not None else hue.fg
    if resolved.name == "mono" or background is None:
        return Style(None, None, True, False, False, True)
    rgb = _xterm_rgb(background)
    black, white = 16, 231
    foreground = black if contrast_ratio(_xterm_rgb(black), rgb) >= contrast_ratio(_xterm_rgb(white), rgb) else white
    return Style(foreground, background, True)


def mission_chip_sgr(which: int | str, theme: Theme | None = None, env: Mapping[str, str] | None = None) -> str:
    """SGR string of the chip form; ``theme`` forces dark or light."""
    if theme is not None:
        resolved = resolve({} if env is None else env, theme_name=theme)
    else:
        resolved = resolve_cached() if env is None else resolve(env)
    return mission_chip_style(which, resolved).sgr()


def tmux_mission_chip_markup(which: int | str, *, env: Mapping[str, str] | None = None) -> str:
    """tmux status markup for the mission chip (filled segment); plain for ``TERM=dumb``."""
    values = os.environ if env is None else env
    if values.get("TERM", "") == "dumb":
        return "#[default]"
    resolved = resolve_cached() if env is None else resolve(values)
    return tmux_style_markup(mission_chip_style(which, resolved))


# --- `llm-bridge theme` ------------------------------------------------------

_SAMPLE_GLYPHS = {
    "state_running": ("●", "RUNNING"), "state_waiting": ("○", "WAITING"),
    "state_review": ("◆", "REVIEW"), "state_done": ("✓", "DONE"),
    "state_failed": ("✕", "FAILED"), "state_paused": ("‖", "PAUSED"),
    "capacity_ok": ("▮", "72% left"), "capacity_low": ("▮", "12% left"),
    "capacity_empty": ("▯", "0% left"),
    "provider_codex": ("■", "codex"), "provider_claude": ("■", "claude"),
    "provider_copilot": ("■", "copilot"), "provider_other": ("■", "other"),
    "ok": ("✓", "OK"), "warn": ("!", "WARN"), "error": ("✕", "ERROR"),
    "selected": ("▶", "SELECTED"), "key": ("", "Ctrl-D"),
    **{f"mission_{n}": ("▌", f"mission {n}") for n in range(1, MISSION_COUNT + 1)},
}


def _sample_text(role: str) -> str:
    glyph, label = _SAMPLE_GLYPHS.get(role, ("", role.upper()))
    return f"{glyph} {label}".strip()


def swatch_line(resolved: ResolvedTheme, role: str, *, colors: int, color: bool) -> str:
    """One preview line: role name, sample rendered with the role, description."""
    sample = f"{_sample_text(role):<12} sample text"
    if color:
        style = fit_style(resolved.style(role), colors, resolved.polarity)
        sgr = style.sgr()
        sample = f"\x1b[{sgr}m{sample}\x1b[0m" if sgr else sample
    return f"  {role:<17} {sample}  {ROLE_DESCRIPTIONS[role]}"


def describe(resolved: ResolvedTheme, *, colors: int) -> dict[str, Any]:
    roles: dict[str, Any] = {}
    for role in ROLES:
        style = resolved.style(role)
        fitted = fit_style(style, colors, resolved.polarity)
        roles[role] = {
            "sgr": style.sgr(),
            "fg": style.fg,
            "bg": style.bg,
            "bold": style.bold,
            "dim": style.dim,
            "underline": style.underline,
            "reverse": style.reverse,
            "origin": resolved.origins[role],
            "fitted_sgr": fitted.sgr(),
            "description": ROLE_DESCRIPTIONS[role],
        }
    return {
        "schema_version": 1,
        "theme": {"name": resolved.name, "source": resolved.name_source},
        "background": {
            "polarity": resolved.polarity,
            "source": resolved.polarity_source,
            "rgb": list(resolved.background_rgb) if resolved.background_rgb else None,
            "guessed": resolved.guessed,
        },
        "colors": colors,
        "color_enabled": not resolved.colors_disabled,
        "probe_notes": list(resolved.probe_notes),
        "warnings": list(resolved.warnings),
        "roles": roles,
    }


def format_theme_report(resolved: ResolvedTheme, *, colors: int, color: bool) -> str:
    """Human-readable report for ``llm-bridge theme``."""
    background = resolved.polarity_source
    lines = [
        f"Background  {resolved.polarity}  ({background})",
        f"Theme       {resolved.name}  ({resolved.name_source})",
        f"Colours     {colors if colors else 'none'}"
        + ("  (colour disabled)" if resolved.colors_disabled else ""),
    ]
    lines.extend(f"Note        {note}" for note in resolved.probe_notes)
    lines.extend(f"Warning     {warning}" for warning in resolved.warnings)
    overridden = sorted(role for role, origin in resolved.origins.items() if origin != "theme")
    if overridden:
        lines.append("Overrides   " + ", ".join(f"{role} ({resolved.origins[role]})" for role in overridden))
    lines.append("")
    lines.extend(swatch_line(resolved, role, colors=colors, color=color) for role in ROLES)
    return "\n".join(lines)
