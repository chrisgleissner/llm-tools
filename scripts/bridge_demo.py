#!/usr/bin/env python3
"""Record a scripted LLM Bridge demo as an animated GIF (and optionally MP4).

The tool has three independent parts:

* A terminal screen model and renderer.  ``parse_ansi`` turns the output of
  ``tmux capture-pane -e -p`` into a grid of styled cells, and ``Renderer``
  draws that grid with Pillow and a monospace TrueType font.  No browser or
  terminal emulator is involved.
* Frame handling and encoding: identical consecutive captures are merged into
  one frame that is held longer, and the frames are encoded with ffmpeg
  (palette optimised GIF, H.264 MP4) or, for GIFs without ffmpeg, with Pillow.
* A driver that runs Bridge in a private tmux server, in an isolated state
  directory, against a scripted fake provider, and plays a storyboard with
  human-like typing and pauses while a background thread captures the screen.

Usage (see docs/llm-bridge/demo.md):

    python3 scripts/bridge_demo.py --out demo.gif --mp4 demo.mp4 --theme dark
    python3 scripts/bridge_demo.py --frames-in frames.json --out light.gif --theme light

The driver is a manual tool and needs tmux, git and python3.  The renderer
needs only Pillow.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Terminal screen model
# ---------------------------------------------------------------------------

Color = int | tuple[int, int, int] | None
"""None is the terminal default, an int is a 256-colour index, a tuple is RGB."""


class Style(NamedTuple):
    fg: Color = None
    bg: Color = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False
    strike: bool = False


DEFAULT_STYLE = Style()


class Cell(NamedTuple):
    ch: str
    style: Style = DEFAULT_STYLE


BLANK = Cell(" ")


@dataclass
class Screen:
    cols: int
    rows: int
    cells: list[list[Cell]]

    def line_text(self, row: int) -> str:
        """Plain text of one row; wide characters appear once."""
        return "".join(cell.ch for cell in self.cells[row]).rstrip()

    def text(self) -> str:
        return "\n".join(self.line_text(r) for r in range(self.rows))


def char_width(ch: str) -> int:
    """Terminal cell width of one character: 0 (combining), 1 or 2."""
    code = ord(ch)
    if code < 0x300:
        return 1 if code >= 0x20 and code != 0x7F else 0
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def apply_sgr(style: Style, params: str) -> Style:
    """Apply one SGR parameter string such as ``1;38;5;39`` to a style."""
    if params == "":
        return DEFAULT_STYLE
    # Colon sub-parameters (38:2::r:g:b, 38:5:n, 4:3) are flattened to ';'.
    items = [int(p) if p.isdigit() else 0 for p in re.split(r"[;:]", params)]
    fg, bg = style.fg, style.bg
    bold, dim, italic, underline, reverse, strike = style[2:]
    i = 0
    while i < len(items):
        code = items[i]
        i += 1
        if code == 0:
            fg = bg = None
            bold = dim = italic = underline = reverse = strike = False
        elif code == 1:
            bold = True
        elif code == 2:
            dim = True
        elif code == 3:
            italic = True
        elif code in (4, 21):
            underline = True
            if code == 4 and i < len(items) and items[i] == 0 and ":" in params:
                underline = False  # 4:0 is "no underline"
                i += 1
        elif code == 7:
            reverse = True
        elif code == 9:
            strike = True
        elif code == 22:
            bold = dim = False
        elif code == 23:
            italic = False
        elif code == 24:
            underline = False
        elif code == 27:
            reverse = False
        elif code == 29:
            strike = False
        elif 30 <= code <= 37:
            fg = code - 30
        elif 90 <= code <= 97:
            fg = code - 90 + 8
        elif 40 <= code <= 47:
            bg = code - 40
        elif 100 <= code <= 107:
            bg = code - 100 + 8
        elif code == 39:
            fg = None
        elif code == 49:
            bg = None
        elif code in (38, 48):
            color: Color = None
            if i < len(items) and items[i] == 5 and i + 1 < len(items):
                color = max(0, min(255, items[i + 1]))
                i += 2
            elif i < len(items) and items[i] == 2:
                rest = items[i + 1 :]
                # 38;2;r;g;b, or 38:2:<colourspace>:r:g:b (an extra field).
                if len(rest) >= 4 and ":" in params:
                    rest = rest[1:]
                if len(rest) >= 3:
                    color = (rest[0] & 255, rest[1] & 255, rest[2] & 255)
                    i = len(items) - (len(rest) - 3)
                else:
                    i = len(items)
            else:
                i = len(items)
            if color is not None:
                if code == 38:
                    fg = color
                else:
                    bg = color
    return Style(fg, bg, bold, dim, italic, underline, reverse, strike)


_TOKEN_RE = re.compile(
    r"(\x1b\[[0-9;:?<>=!]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()][0-9A-Za-z]"  # charset selection
    r"|\x1b[@-Z\\-_=>])"  # two-byte escapes
)


def strip_ansi(text: str) -> str:
    """Remove every escape sequence, leaving the visible characters and newlines."""
    return "".join(part for part in _TOKEN_RE.split(text) if not part.startswith("\x1b"))


def parse_ansi(text: str, cols: int, rows: int) -> Screen:
    """Parse ``tmux capture-pane -e -p`` output into a ``cols`` x ``rows`` grid.

    SGR sequences set the style of the characters that follow; other escape
    sequences are ignored.  A newline starts the next row.  Wide characters
    occupy two cells (the second holds an empty placeholder), zero-width
    characters are attached to the preceding cell, and anything beyond the grid
    is dropped.
    """
    grid: list[list[Cell]] = [[BLANK] * cols for _ in range(rows)]
    style = DEFAULT_STYLE
    row = col = 0
    for token in _TOKEN_RE.split(text):
        if token.startswith("\x1b"):
            if token.startswith("\x1b[") and token.endswith("m") and not re.search(r"[?<>=!]", token):
                style = apply_sgr(style, token[2:-1])
            continue
        for ch in token:
            if ch == "\n":
                row += 1
                col = 0
                continue
            if ch == "\r":
                col = 0
                continue
            width = char_width(ch)
            if row >= rows:
                break
            if width == 0:
                if ord(ch) < 0x20 or ord(ch) == 0x7F:
                    continue
                if col > 0:
                    prev = grid[row][col - 1]
                    target = col - 2 if prev.ch == "" and col >= 2 else col - 1
                    base = grid[row][target]
                    grid[row][target] = Cell(base.ch + ch, base.style)
                continue
            if col + width > cols:
                continue
            grid[row][col] = Cell(ch, style)
            if width == 2:
                grid[row][col + 1] = Cell("", style)
            col += width
    return Screen(cols, rows, grid)


# ---------------------------------------------------------------------------
# Themes and colour resolution
# ---------------------------------------------------------------------------

RGB = tuple[int, int, int]


def hex_rgb(value: str) -> RGB:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


@dataclass(frozen=True)
class Theme:
    name: str
    bg: RGB
    fg: RGB
    ansi: tuple[RGB, ...]
    cursor: RGB
    chrome_bg: RGB
    chrome_fg: RGB
    chrome_line: RGB


DARK = Theme(
    name="dark",
    bg=hex_rgb("1e1e1e"),
    fg=hex_rgb("d4d4d4"),
    ansi=tuple(
        hex_rgb(c)
        for c in (
            "000000", "cd3131", "0dbc79", "e5e510", "2472c8", "bc3fbc", "11a8cd", "e5e5e5",
            "666666", "f14c4c", "23d18b", "f5f543", "3b8eea", "d670d6", "29b8db", "ffffff",
        )
    ),
    cursor=hex_rgb("d4d4d4"),
    chrome_bg=hex_rgb("2b2b2b"),
    chrome_fg=hex_rgb("a0a0a0"),
    chrome_line=hex_rgb("151515"),
)

LIGHT = Theme(
    name="light",
    bg=hex_rgb("fafafa"),
    fg=hex_rgb("383a42"),
    ansi=tuple(
        hex_rgb(c)
        for c in (
            "383a42", "ca1243", "50a14f", "c18401", "4078f2", "a626a4", "0184bc", "a0a1a7",
            "696c77", "e45649", "3a8c39", "986801", "2a5fd9", "7c1d7a", "01729f", "4b4e57",
        )
    ),
    cursor=hex_rgb("383a42"),
    chrome_bg=hex_rgb("e8e8ea"),
    chrome_fg=hex_rgb("6a6a70"),
    chrome_line=hex_rgb("d0d0d4"),
)

THEMES = {"dark": DARK, "light": LIGHT}
_CUBE = (0, 95, 135, 175, 215, 255)


def palette_color(index: int, theme: Theme) -> RGB:
    """Resolve an xterm 256-colour index; 0-15 come from the theme."""
    if index < 16:
        return theme.ansi[index]
    if index < 232:
        n = index - 16
        return (_CUBE[n // 36], _CUBE[(n // 6) % 6], _CUBE[n % 6])
    level = 8 + 10 * (index - 232)
    return (level, level, level)


def blend(a: RGB, b: RGB, weight: float) -> RGB:
    """``weight`` of ``a`` mixed with the rest of ``b``."""
    return tuple(round(x * weight + y * (1 - weight)) for x, y in zip(a, b))  # type: ignore[return-value]


def resolve_colors(style: Style, theme: Theme, *, invert: bool = False) -> tuple[RGB, RGB]:
    """Foreground and background RGB for a style, after reverse and dim."""

    def one(color: Color, default: RGB) -> RGB:
        if color is None:
            return default
        if isinstance(color, int):
            return palette_color(color, theme)
        return color

    fg = one(style.fg, theme.fg)
    bg = one(style.bg, theme.bg)
    if style.reverse != invert:
        fg, bg = bg, fg
    if style.dim:
        fg = blend(fg, bg, 0.6)
    return fg, bg


# ---------------------------------------------------------------------------
# Frames: capture samples, de-duplication and timing
# ---------------------------------------------------------------------------

Cursor = tuple[int, int] | None


@dataclass(frozen=True)
class Sample:
    """One capture: seconds since the recording started, screen text, cursor."""

    t: float
    text: str
    cursor: Cursor = None
    tag: tuple = ()  # what the caption band and highlight show with this capture (see ``Overlay.tag``)


@dataclass(frozen=True)
class Held:
    """A distinct screen and how long it stays visible."""

    text: str
    cursor: Cursor
    duration: float
    tag: tuple = ()


def dedupe_frames(samples: Sequence[Sample], tail_hold: float = 2.5) -> list[Held]:
    """Merge identical consecutive samples; each frame lasts until the next change.

    The last frame is held for ``tail_hold`` seconds so the loop does not snap
    straight back to the first frame.
    """
    held: list[Held] = []
    for index, sample in enumerate(samples):
        end = samples[index + 1].t if index + 1 < len(samples) else sample.t + tail_hold
        duration = max(0.0, end - sample.t)
        if held and held[-1].text == sample.text and held[-1].cursor == sample.cursor and held[-1].tag == sample.tag:
            last = held[-1]
            held[-1] = Held(last.text, last.cursor, last.duration + duration, last.tag)
        else:
            held.append(Held(sample.text, sample.cursor, duration, sample.tag))
    return held


def scale_durations(frames: Sequence[Held], speed: float = 1.0, max_hold: float | None = None) -> list[Held]:
    """Apply a playback speed and, optionally, cap how long any frame is held."""
    out = []
    for frame in frames:
        duration = frame.duration
        if max_hold is not None:
            duration = min(duration, max_hold)
        out.append(Held(frame.text, frame.cursor, duration / speed, frame.tag))
    return out


def quantise_durations(durations: Sequence[float], step: float = 0.01, minimum: float = 0.02) -> list[float]:
    """Round durations to ``step`` (GIF delays are centiseconds) without drifting.

    The rounding error of each frame is carried into the next so the total
    playing time stays within one step of the input total.
    """
    out: list[float] = []
    carry = 0.0
    for duration in durations:
        wanted = duration + carry
        value = max(minimum, round(wanted / step) * step)
        carry = wanted - value
        out.append(round(value, 6))
    return out


TRANSIENT_MARKERS = ("controller-active", "[0/0]")


def is_transient(text: str) -> bool:
    """True for a capture that is not worth showing.

    That is an empty screen (between two program launches) or the bare tmux
    window that appears for a moment after a station that was reached through
    the mission picker is detached: its header ``controller-active ... [0/0]``
    and the station's status line, with nothing else.  The tmux messages
    ``[detached (from session ...)]`` printed when a client leaves are dropped
    too: they only name internal sessions.
    """
    plain = strip_ansi(text)
    rows = [row for row in plain.split("\n") if row.strip()]
    if not rows:
        return True
    if all(row.startswith("[detached") or row.startswith("[exited") for row in rows):
        return True
    if len(rows) <= 3 and "LOADING" in plain and "loading" in plain:
        return True  # the controls' first paint, which shows the full mission id
    if len(rows) == 1 and rows[0].startswith(("Bridge: Ctrl-]", "\u25b6 Bridge: Ctrl-]")):
        return True  # an empty provider pane: only the station's status line (a station being drawn or respawned)
    return len(rows) <= 3 and all(marker in plain for marker in TRANSIENT_MARKERS)


def drop_transient_samples(samples: Sequence[Sample]) -> list[Sample]:
    """Remove captures for which ``is_transient`` holds.

    The highlight storyboard restarts ``llm-bridge`` in a shell loop when the
    controls are left, which leaves a screen or two with nothing on it.  The
    previous frame simply stays visible for that time.
    """
    return [sample for sample in samples if not is_transient(sample.text)]


def drop_blips(samples: Sequence[Sample], tolerance: int = 8) -> list[Sample]:
    """Remove single captures that differ from near-identical neighbours.

    When a client attaches to a station, tmux sometimes paints one intermediate
    screen (the pane scrolled by a row, a half-redrawn status line) between two
    screens that are the same apart from a ticking timer.  A capture is dropped
    when its neighbours differ from each other in at most ``tolerance`` cells
    and it differs from both by more than that.  It is not something a person
    would see.
    """
    kept: list[Sample] = []
    for index, sample in enumerate(samples):
        if 0 < index < len(samples) - 1:
            before, after = samples[index - 1].text, samples[index + 1].text
            if (
                text_distance(before, after) <= tolerance
                and text_distance(before, sample.text) > tolerance
                and text_distance(sample.text, after) > tolerance
            ):
                continue
        kept.append(sample)
    return kept


def text_distance(a: str, b: str) -> int:
    """Number of cells whose character differs between two captures (colour ignored)."""
    rows_a = strip_ansi(a).split("\n")
    rows_b = strip_ansi(b).split("\n")
    total = 0
    for index in range(max(len(rows_a), len(rows_b))):
        line_a = rows_a[index] if index < len(rows_a) else ""
        line_b = rows_b[index] if index < len(rows_b) else ""
        width = max(len(line_a), len(line_b))
        total += sum(1 for x, y in zip(line_a.ljust(width), line_b.ljust(width)) if x != y)
    return total


def loop_seam_end(frames: Sequence[Held], window: float) -> int:
    """Index of the last frame to keep so the loop closes on a screen like the first.

    Only frames that start within the last ``window`` seconds of the recording
    are candidates.  The one whose text is closest to the first frame wins; on a
    tie the later one does, so the recording is not cut short without a reason.
    Frames after it are left out by the caller.
    """
    if not frames:
        raise ValueError("no frames")
    total = sum(frame.duration for frame in frames)
    start = 0.0
    best_index, best_distance = len(frames) - 1, None
    for index, frame in enumerate(frames):
        if total - start <= window and index > 0:
            distance = text_distance(frames[0].text, frame.text)
            if best_distance is None or distance <= best_distance:
                best_index, best_distance = index, distance
        start += frame.duration
    return best_index


def sample_at(samples: Sequence[Sample], t: float) -> int:
    """Index of the capture that is on screen at time ``t`` (the last one at or before it)."""
    if not samples:
        raise ValueError("no samples")
    chosen = 0
    for index, sample in enumerate(samples):
        if sample.t <= t:
            chosen = index
        else:
            break
    return chosen


def information_density(text: str) -> int:
    """Count of visible non-blank cells, used to find the most informative screen."""
    return sum(1 for ch in strip_ansi(text) if not ch.isspace())


def poster_index(samples: Sequence[Sample], mark: float | None = None) -> int:
    """Capture to use as the poster image.

    The storyboard marks the moment it considers the most informative.  Without
    a mark (or for an old capture file) the densest screen wins.
    """
    if not samples:
        raise ValueError("no samples")
    if mark is not None:
        return sample_at(samples, mark)
    return max(range(len(samples)), key=lambda i: (information_density(samples[i].text), -i))


def save_frames(
    path: Path,
    samples: Sequence[Sample],
    size: tuple[int, int],
    marks: dict[str, float] | None = None,
    storyboard: str = "full",
) -> None:
    payload = {
        "storyboard": storyboard,
        "size": list(size),
        "marks": {name: round(t, 4) for name, t in (marks or {}).items()},
        "samples": [{"t": round(s.t, 4), "text": s.text, "cursor": list(s.cursor) if s.cursor else None} for s in samples],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def load_storyboard(path: Path) -> str:
    """Name of the storyboard a capture file was recorded with (captures made before the field existed are 'full')."""
    return str(json.loads(path.read_text(encoding="utf-8")).get("storyboard", "full"))


def load_marks(path: Path) -> dict[str, float]:
    """Named moments saved with a capture (empty for captures made before marks existed)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {str(name): float(t) for name, t in payload.get("marks", {}).items()}


def load_frames(path: Path) -> tuple[list[Sample], tuple[int, int]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    samples = [Sample(float(s["t"]), s["text"], tuple(s["cursor"]) if s.get("cursor") else None) for s in payload["samples"]]  # type: ignore[arg-type]
    cols, rows = payload["size"]
    return samples, (int(cols), int(rows))


# ---------------------------------------------------------------------------
# Renderer (Pillow)
# ---------------------------------------------------------------------------

ADVANCE_RATIO = 1233 / 2048  # DejaVu Sans Mono advance width / em

# (left, right, up, down) line weights: 0 none, 1 light, 2 heavy.
_BOX: dict[str, tuple[int, int, int, int]] = {
    "─": (1, 1, 0, 0), "━": (2, 2, 0, 0), "│": (0, 0, 1, 1), "┃": (0, 0, 2, 2),
    "┌": (0, 1, 0, 1), "┐": (1, 0, 0, 1), "└": (0, 1, 1, 0), "┘": (1, 0, 1, 0),
    "├": (0, 1, 1, 1), "┤": (1, 0, 1, 1), "┬": (1, 1, 0, 1), "┴": (1, 1, 1, 0), "┼": (1, 1, 1, 1),
    "┏": (0, 2, 0, 2), "┓": (2, 0, 0, 2), "┗": (0, 2, 2, 0), "┛": (2, 0, 2, 0),
    "┣": (0, 2, 2, 2), "┫": (2, 0, 2, 2), "┳": (2, 2, 0, 2), "┻": (2, 2, 2, 0), "╋": (2, 2, 2, 2),
    "╭": (0, 1, 0, 1), "╮": (1, 0, 0, 1), "╰": (0, 1, 1, 0), "╯": (1, 0, 1, 0),
    "═": (2, 2, 0, 0), "║": (0, 0, 2, 2), "╔": (0, 2, 0, 2), "╗": (2, 0, 0, 2),
    "╚": (0, 2, 2, 0), "╝": (2, 0, 2, 0), "╠": (0, 2, 2, 2), "╣": (2, 0, 2, 2),
    "╦": (2, 2, 0, 2), "╩": (2, 2, 2, 0), "╬": (2, 2, 2, 2),
    "╴": (1, 0, 0, 0), "╵": (0, 0, 1, 0), "╶": (0, 1, 0, 0), "╷": (0, 0, 0, 1),
}
# Block elements: (x0, y0, x1, y1) as fractions of the cell.
_BLOCKS: dict[str, tuple[float, float, float, float]] = {
    "▀": (0, 0, 1, 0.5), "▄": (0, 0.5, 1, 1), "█": (0, 0, 1, 1),
    "▌": (0, 0, 0.5, 1), "▐": (0.5, 0, 1, 1),
    "▁": (0, 7 / 8, 1, 1), "▂": (0, 3 / 4, 1, 1), "▃": (0, 5 / 8, 1, 1),
    "▅": (0, 3 / 8, 1, 1), "▆": (0, 1 / 4, 1, 1), "▇": (0, 1 / 8, 1, 1),
    "▉": (0, 0, 7 / 8, 1), "▊": (0, 0, 3 / 4, 1), "▋": (0, 0, 5 / 8, 1),
    "▍": (0, 0, 3 / 8, 1), "▎": (0, 0, 1 / 4, 1), "▏": (0, 0, 1 / 8, 1),
}
_SHADES = {"░": 0.25, "▒": 0.5, "▓": 0.75}


def _fc_match(pattern: str) -> str | None:
    """Path of the font that fontconfig picks for ``pattern``, if any."""
    exe = shutil.which("fc-match")
    if not exe:
        return None
    try:
        result = subprocess.run([exe, "-f", "%{file}", pattern], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    path = result.stdout.strip()
    return path if result.returncode == 0 and path and Path(path).is_file() else None


_DEJAVU_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF", "/Library/Fonts")
_STYLE_FILES = {
    (False, False): "DejaVuSansMono.ttf",
    (True, False): "DejaVuSansMono-Bold.ttf",
    (False, True): "DejaVuSansMono-Oblique.ttf",
    (True, True): "DejaVuSansMono-BoldOblique.ttf",
}


def find_font(bold: bool = False, italic: bool = False) -> str:
    """Locate a monospace TTF: DejaVu Sans Mono, else fontconfig's monospace."""
    for directory in _DEJAVU_DIRS:
        candidate = Path(directory) / _STYLE_FILES[(bold, italic)]
        if candidate.is_file():
            return str(candidate)
    suffix = (":bold" if bold else "") + (":italic" if italic else "")
    for pattern in (f"DejaVu Sans Mono{suffix}", f"monospace{suffix}"):
        found = _fc_match(pattern)
        if found:
            return found
    raise RuntimeError("No monospace TrueType font found; install DejaVu Sans Mono (fonts-dejavu-core)")


class Renderer:
    """Draw ``Screen`` objects as RGB images with a fixed cell grid."""

    def __init__(
        self,
        cols: int,
        rows: int,
        theme: Theme,
        *,
        cell_w: int = 10,
        cell_h: int = 20,
        chrome: bool = True,
        title: str = "llm-bridge",
        show_cursor: bool = True,
    ) -> None:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401  (imported lazily: tests of parsing need no Pillow)

        self._Image, self._ImageDraw, self._ImageFont = Image, ImageDraw, ImageFont
        self.cols, self.rows, self.theme = cols, rows, theme
        self.cell_w, self.cell_h = cell_w, cell_h
        self.chrome, self.title, self.show_cursor = chrome, title, show_cursor
        self.pad_x = 14
        self.pad_y = 8
        self.bar_h = 32 if chrome else 0
        width = cols * cell_w + 2 * self.pad_x
        height = rows * cell_h + 2 * self.pad_y + self.bar_h
        self.size = (width + width % 2, height + height % 2)
        font_size = cell_w / ADVANCE_RATIO
        self._fonts = {
            (b, i): ImageFont.truetype(find_font(b, i), font_size) for b in (False, True) for i in (False, True)
        }
        ascent, descent = self._fonts[(False, False)].getmetrics()
        self._baseline = ascent + max(0, (cell_h - ascent - descent) // 2)
        self._missing = self._font_missing_signature()
        self._fallbacks: dict[str, object | None] = {}
        self._row_cache: dict[tuple, object] = {}
        self._chrome_img = self._draw_chrome() if chrome else None

    # -- fonts --------------------------------------------------------------

    def _font_missing_signature(self) -> bytes:
        font = self._fonts[(False, False)]
        return bytes(font.getmask("￿"))

    def _has_glyph(self, font, ch: str) -> bool:
        return bytes(font.getmask(ch)) != self._missing or ord(ch) < 0x80

    def _fallback_font(self, ch: str, bold: bool, italic: bool):
        key = f"{ord(ch):x}{bold}{italic}"
        if key not in self._fallbacks:
            path = _fc_match(f"monospace:charset={ord(ch):x}") or _fc_match(f":charset={ord(ch):x}")
            font = None
            if path:
                try:
                    font = self._ImageFont.truetype(path, self.cell_w / ADVANCE_RATIO, index=0)
                except OSError:
                    font = None
            self._fallbacks[key] = font
        return self._fallbacks[key]

    # -- window chrome ------------------------------------------------------

    def _draw_chrome(self):
        theme = self.theme
        img = self._Image.new("RGB", self.size, theme.bg)
        d = self._ImageDraw.Draw(img)
        d.rectangle([0, 0, self.size[0], self.bar_h - 1], fill=theme.chrome_bg)
        d.line([0, self.bar_h - 1, self.size[0], self.bar_h - 1], fill=theme.chrome_line)
        for n, color in enumerate(("ff5f56", "ffbd2e", "27c93f")):
            cx, cy, r = 20 + n * 22, self.bar_h // 2, 6
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=hex_rgb(color))
        font = self._ImageFont.truetype(find_font(False, False), 13)
        w = d.textlength(self.title, font=font)
        d.text(((self.size[0] - w) / 2, self.bar_h // 2), self.title, font=font, fill=theme.chrome_fg, anchor="lm")
        return img

    # -- rows ---------------------------------------------------------------

    def _draw_box(self, d, ch: str, x: int, fg: RGB) -> None:
        cw, chh = self.cell_w, self.cell_h
        cx, cy = x + cw // 2, chh // 2
        left, right, up, down = _BOX[ch]
        light, heavy = 1, 2
        for weight, x0, y0, x1, y1 in (
            (left, x, cy, cx, cy),
            (right, cx, cy, x + cw - 1, cy),
            (up, cx, 0, cx, cy),
            (down, cx, cy, cx, chh - 1),
        ):
            if not weight:
                continue
            t = heavy if weight == 2 else light
            half = (t - 1) // 2
            if y0 == y1:
                d.rectangle([x0, y0 - half, x1, y0 - half + t - 1], fill=fg)
            else:
                d.rectangle([x0 - half, y0, x0 - half + t - 1, y1], fill=fg)

    def _render_row(self, cells: Sequence[Cell], cursor_col: int | None):
        theme, cw, chh = self.theme, self.cell_w, self.cell_h
        img = self._Image.new("RGB", (self.cols * cw, chh), theme.bg)
        d = self._ImageDraw.Draw(img)
        resolved: list[tuple[RGB, RGB]] = []
        for col, cell in enumerate(cells):
            resolved.append(resolve_colors(cell.style, theme, invert=(col == cursor_col)))
        # Backgrounds first, merged into runs.
        col = 0
        while col < len(cells):
            bg = resolved[col][1]
            end = col + 1
            while end < len(cells) and resolved[end][1] == bg:
                end += 1
            if bg != theme.bg:
                d.rectangle([col * cw, 0, end * cw - 1, chh - 1], fill=bg)
            col = end
        for col, cell in enumerate(cells):
            ch = cell.ch
            if ch in ("", " "):
                continue
            fg, _ = resolved[col]
            x = col * cw
            first = ch[0]
            if first in _BOX:
                self._draw_box(d, first, x, fg)
                continue
            if first in _BLOCKS:
                x0, y0, x1, y1 = _BLOCKS[first]
                d.rectangle([x + round(x0 * cw), round(y0 * chh), x + round(x1 * cw) - 1, round(y1 * chh) - 1], fill=fg)
                continue
            if first in _SHADES:
                shade = blend(fg, resolved[col][1], _SHADES[first])
                d.rectangle([x, 0, x + cw - 1, chh - 1], fill=shade)
                continue
            style = cell.style
            font = self._fonts[(style.bold, style.italic)]
            if not self._has_glyph(font, first):
                font = self._fallback_font(first, style.bold, style.italic) or font
            d.text((x, self._baseline), ch, font=font, fill=fg, anchor="ls")
            if style.underline:
                width = cw * (2 if char_width(first) == 2 else 1)
                d.line([x, self._baseline + 2, x + width - 1, self._baseline + 2], fill=fg)
            if style.strike:
                d.line([x, chh // 2, x + cw - 1, chh // 2], fill=fg)
        # Underline/strike on blank cells (e.g. underlined spaces).
        for col, cell in enumerate(cells):
            if cell.ch in ("", " ") and (cell.style.underline or cell.style.strike):
                fg, _ = resolved[col]
                y = self._baseline + 2 if cell.style.underline else chh // 2
                d.line([col * cw, y, (col + 1) * cw - 1, y], fill=fg)
        return img

    def render(self, screen: Screen, cursor: Cursor = None):
        """Return a PIL RGB image of the screen (cached per unchanged row)."""
        canvas = self._chrome_img.copy() if self._chrome_img is not None else self._Image.new("RGB", self.size, self.theme.bg)
        cur_x, cur_y = cursor if (cursor and self.show_cursor) else (-1, -1)
        for row in range(min(screen.rows, self.rows)):
            cells = screen.cells[row]
            cursor_col = cur_x if row == cur_y and 0 <= cur_x < self.cols else None
            key = (tuple(cells), cursor_col, self.theme.name)
            img = self._row_cache.get(key)
            if img is None:
                if len(self._row_cache) > 4000:
                    self._row_cache.clear()
                img = self._render_row(cells, cursor_col)
                self._row_cache[key] = img
            canvas.paste(img, (self.pad_x, self.bar_h + self.pad_y + row * self.cell_h))
        return canvas


# ---------------------------------------------------------------------------
# Caption band and reading highlight (limits storyboard)
# ---------------------------------------------------------------------------

# One short sentence per beat, burned into a band under the terminal.  The
# sentences say what the screen shows.  ``caption1`` starts at time 0; the
# storyboard marks the start of the others (``caption2`` to ``caption4``).
CAPTIONS = ("Claude is working", "5-hour limit hit", "Bridge hands the task to Codex", "Codex finishes \u00b7 you approve")
CAPTION_ACCENTS = {"dark": (12, 9, 13, 10), "light": (4, 1, 5, 2)}  # ANSI palette indexes: blue, red, purple, green
WIPE_STEPS = 3  # a new caption wipes in from the left over this many capture steps
WIPE_STEP_SECONDS = 0.1
WALK_STEP_SECONDS = 0.5


@dataclass(frozen=True)
class Walk:
    """A highlight that steps down the rows of one block of the Overview, one row at a time.

    It starts at the storyboard mark ``mark``.  ``label`` is the first word of a
    block on screen (``Recent``); the walk takes the last ``steps`` rows of that
    block (the newest events).  With ``rows`` set, the walk instead visits the
    single rows that start with each of those labels, in order.
    """

    mark: str
    label: str
    steps: int
    rows: tuple[str, ...] = ()


WALKS = (Walk("walk1", "Recent", 4), Walk("walk2", "", 3, ("Now", "Tasks", "Needs you")))


def block_rows(text: str, label: str) -> list[int]:
    """Row numbers of the block that starts with ``label`` (up to the next blank row); empty when absent."""
    lines = strip_ansi(text).split("\n")
    for index, line in enumerate(lines):
        if line.strip().startswith(label):
            end = index
            while end + 1 < len(lines) and lines[end + 1].strip():
                end += 1
            return list(range(index, end + 1))
    return []


def walk_row(text: str, walk: Walk, step: int) -> int | None:
    """The row the highlight is on at ``step`` of ``walk`` for this screen, if the block is on it."""
    chosen = walk_row_numbers(text, walk)
    return chosen[min(step, len(chosen) - 1)] if chosen else None


def walk_row_numbers(text: str, walk: Walk) -> list[int]:
    """The rows a walk steps through on this screen."""
    if walk.rows:
        lines = strip_ansi(text).split("\n")
        found = [next((n for n, line in enumerate(lines) if line.strip().startswith(label)), None) for label in walk.rows]
        return [n for n in found if n is not None]
    return block_rows(text, walk.label)[-walk.steps :]


class Overlay:
    """Caption beats and reading highlights, placed in time by the storyboard's marks."""

    def __init__(self, marks: dict[str, float], captions: Sequence[str] = CAPTIONS) -> None:
        self.marks = marks
        self.captions = tuple(captions)
        self.starts = [0.0] + [marks[f"caption{n}"] for n in range(2, len(self.captions) + 1) if f"caption{n}" in marks]

    @classmethod
    def from_marks(cls, marks: dict[str, float]) -> "Overlay | None":
        """None for a capture that carries no caption marks (every other storyboard)."""
        return cls(marks) if "caption2" in marks else None

    def tag(self, t: float) -> tuple:
        """(beat, wipe stage, walk number, walk step) at time ``t``."""
        beat = max(i for i, start in enumerate(self.starts) if start <= t)
        wipe = WIPE_STEPS if beat == 0 else min(WIPE_STEPS, int((t - self.starts[beat]) / WIPE_STEP_SECONDS))
        walk, step = -1, 0
        for number, spec in enumerate(WALKS):
            start = self.marks.get(spec.mark)
            if start is not None and start <= t:
                walk, step = number, min(spec.steps - 1, int((t - start) / WALK_STEP_SECONDS))
        return (beat, wipe, walk, step)


def mix(a: RGB, b: RGB, weight: float) -> RGB:
    return (round(a[0] + (b[0] - a[0]) * weight), round(a[1] + (b[1] - a[1]) * weight), round(a[2] + (b[2] - a[2]) * weight))


def luminance(color: RGB) -> float:
    def channel(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(color[0]) + 0.7152 * channel(color[1]) + 0.0722 * channel(color[2])


def contrast_ratio(a: RGB, b: RGB) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class CaptionBand:
    """The band under the terminal: four progress pills and one large caption."""

    PAD = 20
    PILL_Y, PILL_H = 8, 6

    def __init__(self, width: int, theme: Theme, captions: Sequence[str] = CAPTIONS) -> None:
        from PIL import Image, ImageDraw, ImageFont

        self._Image, self._ImageDraw = Image, ImageDraw
        self.width, self.theme, self.captions = width, theme, tuple(captions)
        longest = max(len(text) for text in self.captions)
        # Fit the longest caption to the width; on a phone the video is shown at about half size, so keep it large.
        size = int((width - 2 * self.PAD) / (longest * ADVANCE_RATIO))
        size = max(20, min(size, 38 if width < 900 else 36))
        self.font = ImageFont.truetype(find_font(True), size)
        self.height = (round(size * 1.9) + 16 + 1) // 2 * 2
        self.text_color: RGB = (255, 255, 255) if theme.name == "dark" else (20, 22, 26)
        self._bands: dict[int, object] = {}

    def accent(self, beat: int) -> RGB:
        indexes = CAPTION_ACCENTS[self.theme.name]
        return self.theme.ansi[indexes[beat % len(indexes)]]

    def background(self, beat: int) -> RGB:
        return mix(self.theme.bg, self.accent(beat), 0.22)

    def band(self, beat: int):
        if beat not in self._bands:
            img = self._Image.new("RGB", (self.width, self.height), self.background(beat))
            d = self._ImageDraw.Draw(img)
            count = len(self.captions)
            gap = 6
            pill_w = (self.width - 2 * self.PAD - gap * (count - 1)) // count
            for index in range(count):
                x = self.PAD + index * (pill_w + gap)
                color = self.accent(index) if index <= beat else mix(self.background(beat), self.text_color, 0.2)
                d.rounded_rectangle([x, self.PILL_Y, x + pill_w - 1, self.PILL_Y + self.PILL_H - 1], radius=3, fill=color)
            middle = self.PILL_Y + self.PILL_H + (self.height - self.PILL_Y - self.PILL_H) // 2
            d.text((self.PAD, middle), self.captions[beat % count], font=self.font, fill=self.text_color, anchor="lm")
            self._bands[beat] = img
        return self._bands[beat]

    def compose(self, canvas, tag: tuple):
        """The terminal image with the caption band under it; a new caption wipes in from the left."""
        beat, wipe = tag[0], tag[1]
        band = self.band(beat)
        if beat > 0 and wipe < WIPE_STEPS:
            mixed = self.band(beat - 1).copy()
            cut = round(self.width * (wipe + 1) / (WIPE_STEPS + 1))
            mixed.paste(band.crop((0, 0, cut, self.height)), (0, 0))
            band = mixed
        out = self._Image.new("RGB", (canvas.size[0], canvas.size[1] + self.height), self.theme.bg)
        out.paste(canvas, (0, 0))
        out.paste(band, (0, canvas.size[1]))
        return out


def apply_overlay(canvas, band: CaptionBand, renderer: "Renderer", text: str, tag: tuple):
    """Reading highlight on the terminal part, then the caption band under it."""
    walk, step = tag[2], tag[3]
    if walk >= 0:
        row = walk_row(text, WALKS[walk], step)
        if row is not None:
            from PIL import Image, ImageDraw

            accent = band.accent(tag[0])
            top = renderer.bar_h + renderer.pad_y + row * renderer.cell_h
            box = (renderer.pad_x - 6, top, renderer.pad_x + renderer.cols * renderer.cell_w + 6, top + renderer.cell_h)
            region = canvas.crop(box)
            canvas.paste(Image.blend(region, Image.new("RGB", region.size, accent), 0.24), box[:2])
            ImageDraw.Draw(canvas).rectangle([box[0], top, box[0] + 3, top + renderer.cell_h - 1], fill=accent)
    return band.compose(canvas, tag)


def tag_samples(samples: Sequence[Sample], overlay: Overlay | None) -> list[Sample]:
    """Samples with the overlay tag of their time (unchanged without an overlay)."""
    if overlay is None:
        return list(samples)
    return [Sample(x.t, x.text, x.cursor, overlay.tag(x.t)) for x in samples]


def apply_cuts(samples: Sequence[Sample], marks: dict[str, float]) -> tuple[list[Sample], dict[str, float]]:
    """Remove the recording time between the marks ``cut_start`` and ``cut_end``.

    The storyboard uses it for waiting that adds nothing to the story: after the
    operator returns to the provider station, tmux shows the old pane for a moment
    before the next provider's screen.  Later captures and marks move earlier by the
    length of the cut; marks inside it move to its start.
    """
    start, end = marks.get("cut_start"), marks.get("cut_end")
    if start is None or end is None or end <= start:
        return list(samples), dict(marks)
    gap = end - start
    kept = [Sample(x.t if x.t < start else x.t - gap, x.text, x.cursor, x.tag) for x in samples if x.t < start or x.t >= end]
    shifted = {name: (t if t < start else max(start, t - gap)) for name, t in marks.items() if name not in ("cut_start", "cut_end")}
    return kept, shifted


def longest_static(durations: Sequence[float]) -> float:
    """Longest time one frame stays on screen, not counting the last one (the final hold)."""
    return max(durations[:-1], default=0.0)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def encode_gif_pillow(images: Sequence, durations: Sequence[float], out: Path) -> None:
    """Write a looping GIF with Pillow: one shared 256-colour palette, no dithering."""
    from PIL import Image

    sample = list(images[:: max(1, len(images) // 12)])[:12]
    width, height = images[0].size
    mosaic = Image.new("RGB", (width, height * len(sample)))
    for n, img in enumerate(sample):
        mosaic.paste(img, (0, n * height))
    palette_image = mosaic.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    frames = [img.quantize(palette=palette_image, dither=Image.Dither.NONE) for img in images]
    delays = [int(round(d * 1000)) for d in durations]
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=delays, loop=0, optimize=False, disposal=1)


def _write_concat(images: Sequence, durations: Sequence[float], directory: Path) -> Path:
    lines = []
    for n, (img, duration) in enumerate(zip(images, durations)):
        name = f"f{n:05d}.png"
        img.save(directory / name)
        lines.append(f"file '{name}'")
        lines.append(f"duration {duration:.3f}")
    lines.append(f"file 'f{len(images) - 1:05d}.png'")  # the concat demuxer ignores the last duration
    listing = directory / "frames.txt"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return listing


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")


GIF_FILTER = (
    "[0:v]{pre}split[a][b];[a]palettegen=max_colors={colors}:stats_mode=full[p];"
    "[b][p]paletteuse=dither=none:diff_mode=rectangle"
)
# Settings that make the clips start and seek reliably in every browser: H.264 High in yuv420p, the index at
# the front of the file, and a key frame every second (30 frames) with no extra key frames at scene changes.
H264_ARGS = [
    "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-preset", "slow",
    "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-movflags", "+faststart", "-an",
]
VP9_ARGS = [
    "-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", "-b:v", "0", "-g", "30", "-keyint_min", "30",
    "-row-mt", "1", "-deadline", "good", "-cpu-used", "4", "-an",
]


def _encode_to_size(build, attempts: Sequence[tuple[int, float]], out: Path, max_bytes: int | None) -> tuple[int, float]:
    """Encode with ``build(crf, scale)`` for each attempt until the file fits ``max_bytes``.

    An attempt is a CRF and a scale (1.0 keeps the frame size).  Without ``max_bytes`` only the
    first attempt runs.  Returns the attempt that was kept.
    """
    for crf, scale in attempts:
        _run_ffmpeg(build(crf, scale))
        if max_bytes is None or out.stat().st_size <= max_bytes:
            return crf, scale
    raise RuntimeError(f"{out.name} is {out.stat().st_size} bytes after {len(attempts)} attempts, over the {max_bytes} byte limit")


def _scale_filter(scale: float) -> str:
    """Even-sized scaling for yuv420p; empty at full size."""
    return "" if scale >= 1.0 else f"scale=trunc(iw*{scale:g}/2)*2:trunc(ih*{scale:g}/2)*2:flags=lanczos,"


def encode_media(
    images: Sequence,
    durations: Sequence[float],
    gif: Path | None,
    mp4: Path | None,
    webm: Path | None = None,
    *,
    gif_colors: int = 256,
    gif_fps: float | None = None,
    gif_width: int | None = None,
    mp4_crf: int = 16,
    webm_crf: int = 32,
    max_bytes: int | None = None,
) -> None:
    """Encode rendered frames to a GIF, an H.264 MP4 and/or a VP9 WebM.

    With ``max_bytes`` the MP4 and WebM are re-encoded at a higher CRF until each fits.
    The GIF can be limited to ``gif_fps`` frames per second and ``gif_width`` pixels.
    """
    if not images:
        raise ValueError("no frames to encode")
    durations = quantise_durations(durations)
    total = sum(durations)  # the concat demuxer would otherwise show the last frame twice
    have_ffmpeg = shutil.which("ffmpeg") is not None
    if (mp4 or webm) and not have_ffmpeg:
        raise RuntimeError("MP4 and WebM output need ffmpeg")
    if gif and not have_ffmpeg:
        encode_gif_pillow(images, durations, gif)
        gif = None
    if not (gif or mp4 or webm):
        return
    with tempfile.TemporaryDirectory(prefix="bridge-demo-enc-") as tmp:
        listing = _write_concat(images, durations, Path(tmp))
        source = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        if gif:
            pre = ""
            if gif_fps:
                pre += f"fps={gif_fps:g},"
            if gif_width:
                pre += f"scale={gif_width}:-2:flags=lanczos,"
            _run_ffmpeg(
                [
                    *source, "-filter_complex", GIF_FILTER.format(colors=gif_colors, pre=pre),
                    "-fps_mode", "vfr", "-loop", "0", "-t", f"{total:.2f}", str(gif),
                ]
            )
        if mp4:
            _encode_to_size(
                lambda crf, scale: [
                    *source, "-vf", f"fps=30,{_scale_filter(scale)}format=yuv420p", *H264_ARGS, "-crf", str(crf),
                    "-t", f"{total:.2f}", str(mp4),
                ],
                [(crf, 1.0) for crf in range(mp4_crf, mp4_crf + 12, 2)] + [(mp4_crf + 4, 0.85), (mp4_crf + 8, 0.85)],
                mp4, max_bytes,
            )
        if webm:
            _encode_to_size(
                lambda crf, scale: [
                    *source, "-vf", f"fps=30,{_scale_filter(scale)}format=yuv420p", *VP9_ARGS, "-crf", str(crf),
                    "-t", f"{total:.2f}", str(webm),
                ],
                [(webm_crf + extra, 1.0) for extra in (0, 6, 12)] + [(webm_crf + extra, 0.8) for extra in (6, 12, 18)],
                webm, max_bytes,
            )


def render_samples(
    samples: Sequence[Sample],
    size: tuple[int, int],
    theme: Theme,
    *,
    speed: float = 1.0,
    tail_hold: float = 2.5,
    max_hold: float | None = 4.0,
    chrome: bool = True,
    show_cursor: bool = True,
    cell_w: int = 10,
    cell_h: int = 20,
    title: str = "llm-bridge",
    seam_window: float = 0.0,
    overlay: Overlay | None = None,
) -> tuple[list, list[float]]:
    """Dedupe, time-scale and draw samples; returns (images, durations in seconds).

    With ``seam_window`` > 0 the recording is cut after the frame in its last
    ``seam_window`` seconds that looks most like the first one (see
    ``loop_seam_end``), and that frame is held for ``tail_hold`` seconds.
    """
    cols, rows = size
    frames = scale_durations(dedupe_frames(samples, tail_hold), speed, max_hold)
    if seam_window > 0 and len(frames) > 1:
        end = loop_seam_end(frames, seam_window)
        last = frames[end]
        frames = [*frames[:end], Held(last.text, last.cursor, max(last.duration, tail_hold / speed), last.tag)]
    renderer = Renderer(cols, rows, theme, cell_w=cell_w, cell_h=cell_h, chrome=chrome, title=title, show_cursor=show_cursor)
    images = [renderer.render(parse_ansi(f.text, cols, rows), f.cursor) for f in frames]
    if overlay is not None:
        band = CaptionBand(renderer.size[0], theme, overlay.captions)
        images = [apply_overlay(image, band, renderer, f.text, f.tag) for image, f in zip(images, frames)]
    return images, [f.duration for f in frames]


# ---------------------------------------------------------------------------
# tmux driver
# ---------------------------------------------------------------------------

CURSOR_MARK = "@@CURSOR:"


class TmuxSession:
    """A private tmux server holding one detached session of a fixed size."""

    def __init__(self, name: str, size: tuple[int, int]) -> None:
        self.server = name
        self.cols, self.rows = size
        self.target = "demo"

    def _tmux(self, *args: str, check: bool = True, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-L", self.server, "-f", "/dev/null", *args],
            capture_output=True, text=True, check=check, timeout=timeout,
        )

    def start(self, command: list[str], cwd: Path) -> None:
        self._tmux(
            "new-session", "-d", "-s", self.target, "-x", str(self.cols), "-y", str(self.rows),
            "-c", str(cwd), shlex.join(command),
        )
        self._tmux("set-option", "-g", "status", "off")
        self._tmux("set-option", "-g", "escape-time", "0")
        self._tmux("resize-window", "-t", self.target, "-x", str(self.cols), "-y", str(self.rows))

    def geometry(self) -> tuple[int, int]:
        out = self._tmux("display-message", "-p", "-t", self.target, "#{pane_width}x#{pane_height}").stdout.strip()
        cols, rows = out.split("x")
        return int(cols), int(rows)

    def capture(self) -> tuple[str, Cursor]:
        proc = self._tmux(
            "capture-pane", "-e", "-p", "-t", self.target, ";",
            "display-message", "-p", "-t", self.target, CURSOR_MARK + "#{cursor_x}:#{cursor_y}:#{cursor_flag}",
            check=False,
        )
        text = proc.stdout
        index = text.rfind(CURSOR_MARK)
        cursor: Cursor = None
        if index >= 0:
            try:
                x, y, flag = text[index + len(CURSOR_MARK) :].strip().split(":")
                cursor = (int(x), int(y)) if flag == "1" else None
            except ValueError:
                cursor = None
            text = text[:index]
        return text, cursor

    def send(self, *keys: str, literal: bool = False) -> None:
        args = ["send-keys", "-t", self.target]
        if literal:
            args.append("-l")
            args.append("--")
        self._tmux(*args, *keys)

    def alive(self) -> bool:
        return self._tmux("has-session", "-t", self.target, check=False).returncode == 0

    def kill(self) -> None:
        self._tmux("kill-server", check=False)


class Recorder:
    """Capture the pane at a fixed rate on a background thread."""

    def __init__(self, session: TmuxSession, fps: float) -> None:
        self.session = session
        self.interval = 1.0 / fps
        self.samples: list[Sample] = []
        self.marks: dict[str, float] = {}
        self.latest = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._t0 = 0.0

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._thread.start()

    def _run(self) -> None:
        next_tick = self._t0
        while not self._stop.is_set():
            text, cursor = self.session.capture()
            now = time.monotonic()
            self.latest = strip_ansi(text)
            self.samples.append(Sample(now - self._t0, text, cursor))
            next_tick += self.interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_tick = time.monotonic()

    def mark(self, name: str) -> None:
        """Remember the current moment (seconds since the start) under ``name``."""
        self.marks[name] = time.monotonic() - self._t0

    def stop(self) -> list[Sample]:
        self._stop.set()
        self._thread.join(timeout=10)
        return self.samples


class Human:
    """Keyboard and pacing helpers that mimic a person at the terminal."""

    def __init__(self, session: TmuxSession, recorder: Recorder, *, cps: float = 25.0, seed: int = 7) -> None:
        self.session = session
        self.recorder = recorder
        self.cps = cps
        self.rng = random.Random(seed)

    def pause(self, seconds: float) -> None:
        time.sleep(seconds)

    def type(self, text: str) -> None:
        for ch in text:
            started = time.monotonic()
            self.session.send(ch, literal=True)
            delay = (1.0 / self.cps) * self.rng.uniform(0.7, 1.3) + (0.06 if ch in " ,." else 0)
            spent = time.monotonic() - started
            time.sleep(max(0.0, delay - spent))

    def key(self, *keys: str, after: float = 0.0) -> None:
        self.session.send(*keys)
        if after:
            self.pause(after)

    def prefix(self, key: str, after: float = 0.0) -> None:
        """Ctrl-] followed by ``key``, the way a person presses the two."""
        self.session.send("C-]")
        self.pause(0.25)
        self.session.send(key, literal=True)
        if after:
            self.pause(after)

    def wait_for(self, needle: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.recorder.latest:
                return
            time.sleep(0.05)
        raise TimeoutError(f"timed out waiting for {needle!r}; last screen:\n{self.recorder.latest}")

    def pick_in_picker(self, needle: str, cursor: str = "\u203a", max_moves: int = 8) -> None:
        """Press Down until the mission picker's cursor is on the row that contains ``needle``."""
        for _ in range(max_moves + 1):
            line = next((row for row in self.recorder.latest.splitlines() if needle in row), "")
            if line and cursor in line.split(needle)[0]:
                return
            self.key("Down", after=0.4)
        raise TimeoutError(f"picker cursor never reached {needle!r}; last screen:\n{self.recorder.latest}")

    def wait_for_any(self, needles: Sequence[str], timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(needle in self.recorder.latest for needle in needles):
                return
            time.sleep(0.05)
        raise TimeoutError(f"timed out waiting for any of {list(needles)!r}; last screen:\n{self.recorder.latest}")

    def wait_gone(self, needle: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle not in self.recorder.latest:
                return
            time.sleep(0.05)
        raise TimeoutError(f"{needle!r} still on screen after {timeout}s:\n{self.recorder.latest}")


@dataclass
class DemoWorld:
    """Everything the demo touches, below one private temporary directory."""

    root: Path
    theme: str
    state: Path = field(init=False)
    home: Path = field(init=False)
    bin: Path = field(init=False)
    runtime: Path = field(init=False)
    gate: Path = field(init=False)
    repos: dict[str, Path] = field(default_factory=dict)
    mission_id_hint: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)
    mission_ids: list[str] = field(default_factory=list)
    usage_file: Path = field(init=False)
    limit_file: Path = field(init=False)
    go_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.go_dir = self.root / LIMITS_GO_DIR
        self.usage_file = self.root / "usage.json"
        self.limit_file = self.root / "claude-limit"
        self.state = self.root / "state"
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.runtime = self.root / "run"
        self.gate = self.root / "gate"
        for path in (self.root, self.state, self.home, self.bin, self.runtime):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)

    def make_repo(self, name: str, files: dict[str, str]) -> Path:
        repo = self.home / name
        repo.mkdir(mode=0o755)
        for relative, content in files.items():
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        env = {**self.base_env(), "GIT_AUTHOR_NAME": "Demo", "GIT_AUTHOR_EMAIL": "demo@example.invalid",
               "GIT_COMMITTER_NAME": "Demo", "GIT_COMMITTER_EMAIL": "demo@example.invalid"}
        for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "Initial commit"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
        self.repos[name] = repo
        return repo

    def install_fake_clis(self, *, named: bool = False) -> None:
        """Provider launchers named like the real CLIs that run the demo provider.

        With ``named`` each launcher also tells the demo provider which CLI it
        plays (``DEMO_PROVIDER_NAME``), which the limits storyboard needs.
        """
        provider = REPO_ROOT / "scripts" / "bridge_demo_provider.py"
        bridge = REPO_ROOT / "llm-bridge"
        for name in ("codex", "claude", "copilot"):
            prefix = f"DEMO_PROVIDER_NAME={name} " if named else ""
            self._script(name, f'{prefix}exec {shlex.quote(sys.executable)} {shlex.quote(str(provider))} "$@"')
        self._script("llm-bridge", f'exec {shlex.quote(sys.executable)} {shlex.quote(str(bridge))} "$@"')

    def _script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def usage_json(self) -> str:
        now = int(time.time())
        window = lambda remaining, hours: {"remaining": remaining, "resets_at": now + int(hours * 3600)}  # noqa: E731
        return json.dumps(
            {
                "codex": {"available": True, "five_hour": window(66, 2.08), "week": window(81, 71)},
                "claude": {"available": True, "five_hour": window(92, 3.6), "week": window(74, 120)},
                "copilot": {"available": True, "monthly": {"used": 38, "resets_at": now + 14 * 86400}},
            }
        )

    def base_env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "xterm-256color",
        }

    def shell_env(self) -> dict[str, str]:
        env = self.base_env()
        env.update(
            {
                "LLM_BRIDGE_STATE_DIR": str(self.state),
                "LLM_BRIDGE_THEME": self.theme,
                "XDG_RUNTIME_DIR": str(self.runtime),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_CACHE_HOME": str(self.home / ".cache"),
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
                "LLM_USAGE_NO_SERVICE": "1",
                "LLM_USAGE_PROVIDERS": "claude,codex,copilot",
                "LLM_USAGE_DISABLE_COPILOT": "1",
                "LLM_USAGE_DISABLE_CODEX_APP_SERVER": "1",
                "LLM_USAGE_LIVE_FETCH_RETRIES": "0",
                "LLM_TOOLS_NO_INHIBIT": "1",
                "LLM_SCHEDULER_NO_ACTUAL_SUSPEND": "1",
                "LLM_SCHEDULER_USAGE_JSON": self.usage_json(),
                "LLM_TOOLS_LOCAL_BLOCK_DIR": str(self.root / "blocks"),
                "DEMO_PROVIDER_GATE": str(self.gate),
                "DEMO_PROVIDER_SPEED": "1.5",
                "PS1": "\\[\\e[1;32m\\]~/acme-api\\[\\e[0m\\] $ ",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            }
        )
        env.update(self.extra_env)
        return env

    def run_bridge(self, *args: str, cwd: Path | None = None) -> str:
        """Run a ``llm-bridge`` CLI command with the demo environment (not recorded)."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "llm-bridge"), *args],
            env=self.shell_env(), cwd=cwd or self.root, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"llm-bridge {' '.join(args)} failed: {result.stderr}{result.stdout}")
        return result.stdout

    def owned_pids(self) -> list[int]:
        """Processes (controllers, tmux stations, log sinks) started with this world's state dir."""
        marker = f"LLM_BRIDGE_STATE_DIR={self.state}".encode()
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                if marker in (entry / "environ").read_bytes().split(b"\0"):
                    found.append(int(entry.name))
            except OSError:
                continue
        return found

    def cleanup(self, keep: bool = False) -> None:
        for mission_id in dict.fromkeys([self.mission_id_hint, *self.mission_ids]):
            if not mission_id:
                continue
            try:
                self.run_bridge("stop", "--force", "--mission", mission_id)
            except Exception:
                pass
        for sock in sorted((self.runtime / "llm-bridge").glob("*.sock")) if (self.runtime / "llm-bridge").is_dir() else []:
            subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True, timeout=10)
        pids = self.owned_pids()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            time.sleep(0.5)
            pids = [p for p in pids if Path(f"/proc/{p}").exists()]
        if not keep:
            shutil.rmtree(self.root, ignore_errors=True)


# -- storyboard -------------------------------------------------------------

OBJECTIVE = "Add per-client rate limiting to the REST API"
FOLLOW_UPS = (
    ("Document the rate limits in the API guide", "Describe the 429 response and Retry-After header in docs/api.md."),
    ("Add a Retry-After header test", "Cover the header value for a client that exceeds its burst."),
)


def run_storyboard(world: DemoWorld, human: Human, size: tuple[int, int]) -> None:
    """Scenes a-e of the demo.  All waits poll the live screen text."""
    h = human
    repo = world.repos["acme-api"]

    # a. Home screen on first run, new mission form.
    h.pause(1.0)
    h.type("llm-bridge")
    h.pause(0.4)
    h.key("Enter")
    h.wait_for("Choose a mission")
    h.pause(2.6)
    h.key("n", after=1.4)
    h.type(OBJECTIVE)
    h.pause(1.0)
    h.key("Tab", after=1.6)  # Providers: codex is preselected
    h.key("Tab", after=0.9)  # Folder
    h.key("Tab", after=1.6)  # Mode: native interactive
    h.key("Tab", after=0.6)  # Save mission only
    h.key("Tab", after=1.0)  # Start and open provider (Start is the last row)
    h.key("Enter")

    # b. The station opens; the demo provider works.
    h.wait_for("demo provider", timeout=60)
    ids = json.loads(world.run_bridge("mission", "list", "--json", cwd=repo))
    missions = ids.get("missions", ids) if isinstance(ids, dict) else ids
    mission_id = str(missions[0]["id"])
    world.mission_id_hint = mission_id
    for title, body in FOLLOW_UPS:  # queued from "another terminal" while the provider works
        prompt = world.root / "task.md"
        prompt.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        world.run_bridge("task", "add", "--mission", mission_id, "--file", str(prompt), cwd=repo)
    h.wait_for("Edit src/api/server.py", timeout=60)
    h.pause(3.0)

    # c. Bridge controls: Overview, Tasks with preview, Actions.
    h.prefix("b", after=0.2)
    h.wait_for("Overview", timeout=15)
    h.pause(3.8)
    h.key("F2", after=1.8)
    h.key("Down", after=2.4)
    h.key("Down", after=2.2)
    h.key("C-k", after=3.2)
    h.key("Escape", after=0.8)

    # d. Back to the provider, which finishes; /exit hands the task to verification.
    h.prefix("p", after=0.2)
    world.gate.write_text("go", encoding="utf-8")
    h.wait_for("type /exit to finish", timeout=60)
    h.pause(2.4)
    h.type("/exit")
    h.pause(0.5)
    h.key("Enter")
    h.wait_for("Provider exited", timeout=30)
    h.pause(1.8)

    # e. Review and approve; Bridge has already moved on to the next task.
    h.prefix("b", after=0.2)
    h.key("F1", after=0.2)
    h.wait_for("Needs you", timeout=15)
    h.pause(3.6)
    h.type("/approve")
    h.pause(0.6)
    h.key("Enter")
    h.wait_for("Y", timeout=15)
    h.pause(1.6)
    h.key("y", after=0.2)
    h.wait_for("Applied", timeout=30)
    h.pause(2.4)
    h.key("F2", after=3.6)  # task 1 is now completed, task 2 is running


def run_multi_scene(world: DemoWorld, human: Human) -> None:
    """Optional scene f: leave the station, show the mission list with a second repo's mission."""
    h = human
    web = world.repos["acme-web"]
    created = json.loads(
        world.run_bridge(
            "mission", "create", "--prompt", "Fix the flaky checkout test", "--name", "Fix the flaky checkout test",
            "--cwd", str(web), "--native", "--providers", "codex", "--json", cwd=web,
        )
    )
    second = str(created["mission"]["id"])
    world.run_bridge("start", "--mission", second, cwd=web)
    h.pause(0.8)
    h.prefix("d", after=0.4)  # detach this terminal; both missions keep running
    h.wait_gone("Provider: Ctrl-] then P", timeout=15)  # back in the plain controls
    h.pause(2.2)
    h.key("C-d")  # leave the controls
    h.wait_for("[detached", timeout=15)
    h.pause(0.6)
    h.key("C-l", after=0.8)  # clear the shell
    h.type("llm-bridge")
    h.pause(0.4)
    h.key("Enter")
    h.wait_for("Choose a mission", timeout=15)
    h.pause(3.4)
    h.key("Down", after=1.0)
    h.key("Down", after=1.6)  # the second mission, in another folder
    h.key("Enter")
    h.wait_for("LLM BRIDGE · Fix the flaky", timeout=15)
    h.pause(3.6)


# -- highlight storyboard ---------------------------------------------------

REPO_FILES = {
    "README.md": "# project\n\nA small service.\n",
    "pyproject.toml": "[project]\nname = 'project'\nversion = '0.1.0'\n",
    "src/api/server.py": "app = Router()\n",
    "src/api/middleware.py": "def log_requests(request):\n    pass\n",
    "tests/test_server.py": "def test_ok():\n    assert True\n",
}


@dataclass(frozen=True)
class SeedMission:
    """A mission prepared before the highlight recording starts."""

    repo: str
    name: str
    prompt: str
    colour: int
    state: str  # "completed", "review", "created" or "running"


# Created in this order.  The mission that will be approved on camera is first
# because Bridge's /approve hint and command use the task's global number, which
# equals its number within the mission only for task 1.  Colours are pinned so
# the chips differ.  The names are short enough for the mission picker's
# columns at 90 cells.
HIGHLIGHT_MISSIONS = (
    SeedMission("acme-web", "Fix flaky test", "Fix the flaky checkout test", 3, "review"),
    SeedMission("acme-api", "Rate-limit API", "Add per-client rate limiting to the REST API", 1, "running"),
    SeedMission("acme-docs", "Draft v2 guide", "Draft the v2 migration guide", 4, "created"),
    SeedMission("acme-infra", "Migrate CI", "Migrate CI to the new runner image", 7, "completed"),
)
REVIEW_MISSION = "Fix flaky"
SEED_SPEED = "8"
LIVE_SPEED = "1.2"


def station_sockets(world: DemoWorld) -> set[Path]:
    directory = world.runtime / "llm-bridge"
    return set(directory.glob("*.sock")) if directory.is_dir() else set()


def station_screen(sock: Path) -> str:
    result = subprocess.run(["tmux", "-S", str(sock), "capture-pane", "-p"], capture_output=True, text=True, timeout=10)
    return result.stdout


def wait_station(sock: Path, needle: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    screen = ""
    while time.monotonic() < deadline:
        screen = station_screen(sock)
        if needle in screen:
            return
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {needle!r} in a station; last screen:\n{screen}")


def seed_missions(world: DemoWorld, missions: Sequence[SeedMission] = HIGHLIGHT_MISSIONS) -> None:
    """Create the missions of the highlight: one per state, each in its own repository.

    Finished providers get their gate file, are asked to ``/exit`` through the
    station's tmux socket, and (for ``completed``) are approved.  The running
    mission keeps its gate closed, so its provider stays busy on a live
    "Running tests" spinner until the recording is over.
    """
    gates = world.root / "gates"
    gates.mkdir(mode=0o700, exist_ok=True)
    world.extra_env["DEMO_PROVIDER_GATE_DIR"] = str(gates)
    # Create every mission first, then start them; the running one last so its
    # provider is waiting at the start of its work when the recording opens.
    ids: dict[str, str] = {}
    for seed in missions:
        repo = world.repos[seed.repo]
        created = json.loads(
            world.run_bridge(
                "mission", "create", "--prompt", seed.prompt, "--name", seed.name, "--cwd", str(repo),
                "--native", "--providers", "codex", "--json", cwd=repo,
            )
        )
        ids[seed.repo] = str(created["mission"]["id"])
        world.mission_ids.append(ids[seed.repo])
        world.run_bridge("mission", "colour", "--mission", ids[seed.repo], str(seed.colour), cwd=repo)
        if seed.state == "running":
            for title, body in FOLLOW_UPS:  # queued behind the running task
                prompt = world.root / "task.md"
                prompt.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
                world.run_bridge("task", "add", "--mission", ids[seed.repo], "--file", str(prompt), cwd=repo)
    for seed in sorted(missions, key=lambda seed: seed.state == "running"):
        repo = world.repos[seed.repo]
        mission_id = ids[seed.repo]
        if seed.state == "created":
            continue
        finished = seed.state in ("completed", "review")
        world.extra_env["DEMO_PROVIDER_SPEED"] = SEED_SPEED if finished else LIVE_SPEED
        if finished:
            (gates / repo.name).write_text("go", encoding="utf-8")
        before = station_sockets(world)
        world.run_bridge("start", "--mission", mission_id, cwd=repo)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (station_sockets(world) - before):
            time.sleep(0.2)
        new = station_sockets(world) - before
        if not new:
            raise RuntimeError(f"no station started for {seed.name!r}")
        sock = next(iter(new))
        if not finished:
            wait_station(sock, "Running tests", timeout=60)  # busy on the test run when the recording starts
            continue
        wait_station(sock, "type /exit to finish")
        subprocess.run(["tmux", "-S", str(sock), "send-keys", "-l", "/exit"], check=True, timeout=10)
        subprocess.run(["tmux", "-S", str(sock), "send-keys", "Enter"], check=True, timeout=10)
        wait_station(sock, "Provider exited", timeout=30)
        deadline = time.monotonic() + 30
        while "VERIFYING" not in world.run_bridge("status", "--mission", mission_id, cwd=repo) and time.monotonic() < deadline:
            time.sleep(0.3)
        if seed.state == "completed":
            world.run_bridge("task", "verify", "--mission", mission_id, "--task", "1", "--approve", "--evidence", "26 passed", cwd=repo)
    world.extra_env["DEMO_PROVIDER_SPEED"] = LIVE_SPEED


def run_highlight(world: DemoWorld, human: Human, size: tuple[int, int]) -> None:
    """The short hero loop; most of it is Bridge's own screens.

    Home (mission chips and states), the live mission's Overview, a glimpse of
    the provider station with Bridge's status line, the mission picker, the
    second mission's Overview with "Needs you", ``/approve``, and back home.
    ``mark("poster")`` names the Overview that shows the status strip, "Needs
    you", capacity and recent events.  Every beat is held 1.5 to 3 seconds
    except the short glimpse of the provider's output.
    """
    h = human
    h.pause(2.2)  # home screen: four missions in four states
    h.key("Enter", after=0.3)  # the live mission is already selected: its controls open
    h.wait_for("Overview", timeout=15)
    h.pause(2.0)
    h.type("/attach")
    h.key("Enter")
    h.wait_for("Running tests", timeout=30)  # the station: provider output above Bridge's status line
    h.pause(1.4)
    h.prefix("m", after=0.2)
    h.wait_for("Missions \u00b7", timeout=15)
    h.pause(1.3)
    h.pick_in_picker(REVIEW_MISSION)
    h.pause(0.4)
    h.key("Enter")
    h.wait_for("Provider exited", timeout=30)
    h.pause(0.5)
    h.prefix("B", after=0.2)
    h.wait_for("Needs you", timeout=15)
    h.pause(0.8)
    h.recorder.mark("poster")
    h.pause(1.2)
    h.type("/approve")
    h.pause(0.2)
    h.key("Enter")
    h.wait_for("Confirm", timeout=15)
    h.pause(0.6)
    h.key("y", after=0.2)
    h.wait_for("COMPLETED", timeout=30)
    h.pause(1.6)
    # Detaching a station reached through the picker first lands on the original
    # station (an empty tmux window, which is dropped from the recording, or the
    # provider's screen for a moment); detaching again shows that mission's controls, and leaving
    # those shows the home screen.
    h.prefix("d")
    h.wait_for_any(("controller-active", "Task 2 \u25cf RUNNING"), timeout=15)
    h.prefix("d", after=0.2)
    h.wait_for("Returned from the provider station", timeout=15)
    h.pause(0.2)
    h.key("C-d")
    h.wait_for("Choose a mission", timeout=30)
    h.key("Down")  # same selection as the first frame
    h.pause(2.2)


# -- limits storyboard ------------------------------------------------------

# The hero loop: a provider runs out of capacity and the mission carries on with
# the next one.  Everything on screen is Bridge's own output.  The scripted parts
# are the two stand-in providers and the capacity figures:
#
# * ``LLM_SCHEDULER_USAGE_JSON_FILE`` names a file that the driver rewrites; every
#   capacity read of the controller (Ralph's selection and the background
#   observer, every 5 seconds) takes the figures from it.
# * The usage history that the "empty in ~Nm" forecast is computed from is
#   written to the usage log before the mission starts: 33 minutes of Claude's
#   5-hour window being used up at a steady pace.  The forecast itself is
#   computed by Bridge from that log and the live figures.
# * ``[ralph] even_burn = false`` keeps the mission on one provider until it is
#   exhausted, and ``retry_delays = ""`` makes a provider that exits with an error
#   hand the task over at once instead of being retried after a pause.
#
# The clip starts on the provider station with Claude already streaming, so the
# first frame is not a still screen.  Each provider stand-in prints one line every
# ``DEMO_PROVIDER_CADENCE`` seconds and starts only when the driver writes its
# ``<name>.go`` file.  Four marks place the burned-in captions (``caption2`` to
# ``caption4``) and two place the reading highlights (``walk1``, ``walk2``).

OVERVIEW_TAB = ("[1 Overview]", "[Overview]")  # the second form is what a narrow terminal shows
LIMITS_ROTATION = "claude,codex"
LIMITS_CONFIG_TOML = '[ralph]\neven_burn = false\nretry_delays = ""\n'
LIMITS_REFRESH_SECONDS = 5  # the shortest interval that `mission create --capacity-refresh-seconds` accepts
# The 5-hour figures the capacity observer sees, in order: before the recording,
# two intermediate readings, and the exhausted window.
LIMITS_CLAUDE_STEPS = (14, 12, 6, 0)
LIMITS_CODEX_STEPS = (89, 89, 88, 86)
LIMITS_WINDOW_USED_MINUTES = 33.0
LIMITS_CLAUDE_RESET_HOURS = 5 - LIMITS_WINDOW_USED_MINUTES / 60
LIMITS_CODEX_RESET_HOURS = 3.4
LIMITS_HEAD_START = 1.1  # seconds Claude streams before the recording starts, so the first frame is already in motion
LIMITS_CLAUDE_SECONDS = 3.4  # recorded seconds of Claude working before its window is used up
LIMITS_BEFORE_CONSOLE = 0.15  # seconds between the provider's exit and the switch to Bridge's console
LIMITS_GO_DIR = "go"  # below the demo root: "<provider>.go" starts that stand-in's streaming


def limits_usage_payload(claude_left: float, codex_left: float, now: int) -> dict[str, object]:
    """Injected capacity figures: the shape of ``LLM_SCHEDULER_USAGE_JSON``."""

    def window(remaining: float, hours: float) -> dict[str, float]:
        return {"remaining": remaining, "resets_at": now + int(hours * 3600)}

    return {
        "claude": {
            "available": True,
            "five_hour": window(claude_left, LIMITS_CLAUDE_RESET_HOURS),
            "week": window(61, 100.0),
        },
        "codex": {
            "available": True,
            "five_hour": window(codex_left, LIMITS_CODEX_RESET_HOURS),
            "week": window(79, 120.0),
        },
    }


def write_limits_usage(path: Path, step: int) -> None:
    """Publish capacity step ``step``; the file is replaced in one rename so a reader never sees half of it."""
    payload = limits_usage_payload(LIMITS_CLAUDE_STEPS[step], LIMITS_CODEX_STEPS[step], int(time.time()))
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def limits_usage_history(now: int) -> list[dict[str, object]]:
    """One sample a minute of both 5-hour windows being used, ending at the first live figure.

    The log has the format of ``common.log_usage_sample``.  Claude falls from
    100% to its first live figure at about 2.6 points a minute; Codex falls
    slowly, so Bridge forecasts that it lasts past its reset.
    """
    minutes = int(LIMITS_WINDOW_USED_MINUTES)
    claude_rate = (100 - LIMITS_CLAUDE_STEPS[0]) / LIMITS_WINDOW_USED_MINUTES
    codex_rate = (100 - LIMITS_CODEX_STEPS[0]) / LIMITS_WINDOW_USED_MINUTES
    rows: list[dict[str, object]] = []
    for age in range(minutes, 0, -1):
        for provider, final, rate in (
            ("Claude", LIMITS_CLAUDE_STEPS[0], claude_rate),
            ("Codex", LIMITS_CODEX_STEPS[0], codex_rate),
        ):
            rows.append({"ts": now - age * 60, "provider": provider, "window": "5h", "remaining": round(final + age * rate, 1)})
    return rows


def prepare_limits(world: DemoWorld) -> str:
    """Create and start the limits mission; returns its id.  Nothing here is recorded."""
    repo = world.repos["acme-api"]
    world.go_dir.mkdir(mode=0o700, exist_ok=True)
    config = world.home / ".config" / "llm-tools"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(LIMITS_CONFIG_TOML, encoding="utf-8")
    history = world.home / ".cache" / "llm-tools" / "llm-usage"
    history.mkdir(parents=True, exist_ok=True)
    rows = limits_usage_history(int(time.time()))
    (history / "llm-usage.log").write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    write_limits_usage(world.usage_file, 0)
    world.extra_env.update(
        {
            "LLM_SCHEDULER_USAGE_JSON_FILE": str(world.usage_file),
            "LLM_USAGE_PROVIDERS": LIMITS_ROTATION,
            "DEMO_PROVIDER_SCENARIO": "limits",
            "DEMO_PROVIDER_LIMIT_FILE": str(world.limit_file),
            "DEMO_PROVIDER_GO_DIR": str(world.go_dir),
            "DEMO_PROVIDER_CADENCE": "0.5",
            "DEMO_PROVIDER_SPEED": "1",
        }
    )
    created = json.loads(
        world.run_bridge(
            "mission", "create", "--prompt", OBJECTIVE, "--name", "Rate-limit API", "--cwd", str(repo), "--native",
            "--providers", LIMITS_ROTATION, "--capacity-refresh-seconds", str(LIMITS_REFRESH_SECONDS), "--json", cwd=repo,
        )
    )
    mission_id = str(created["mission"]["id"])
    world.mission_ids.append(mission_id)
    world.mission_id_hint = mission_id
    world.run_bridge("start", "--mission", mission_id, cwd=repo)
    return mission_id


def limits_preroll(world: DemoWorld, session: TmuxSession) -> None:
    """Open Claude's provider station and release its stream; the recording starts ``LIMITS_HEAD_START`` later.

    Opens the running mission, attaches to the station (the stand-in has printed its
    banner and waits for its ``claude.go`` file), publishes the second capacity figure,
    then lets Claude start so the first recorded frame already shows work in progress.
    """
    wait_for_screen(session, "Choose a mission", timeout=30)
    session.send("Down")
    time.sleep(0.5)
    session.send("Enter")
    deadline = time.monotonic() + 60
    attached = False
    while time.monotonic() < deadline:
        text = strip_ansi(session.capture()[0])
        if "claude \u00b7 scripted" in text and "Ctrl-] then B" in text:
            attached = True
            break
        if any(tab in text for tab in OVERVIEW_TAB):
            session.send("/attach", literal=True)
            time.sleep(0.3)
            session.send("Enter")
            time.sleep(1.0)
        time.sleep(0.3)
    if not attached:
        raise TimeoutError("Claude's station never appeared")
    write_limits_usage(world.usage_file, 1)
    time.sleep(0.4)
    (world.go_dir / "claude.go").write_text("go", encoding="utf-8")
    time.sleep(LIMITS_HEAD_START)


def run_limits(world: DemoWorld, human: Human, size: tuple[int, int]) -> None:
    """Claude streams, hits its limit, Bridge hands the task to Codex, Codex passes its tests, the task awaits approval.

    The marks place the captions and highlights (see ``Overlay``).  ``poster``
    names the Overview that lists the hand-off, with the highlight on the
    switch.
    """
    h = human
    rec = h.recorder
    h.pause(LIMITS_CLAUDE_SECONDS)  # Claude's stream, one line every half second
    write_limits_usage(world.usage_file, 3)  # the 5-hour window is used up
    world.limit_file.write_text("go", encoding="utf-8")  # and the stand-in Claude says so
    h.wait_for("5-hour limit reached", timeout=15)
    rec.mark("caption2")
    h.wait_for("Provider exited", timeout=15)
    h.pause(LIMITS_BEFORE_CONSOLE)
    h.prefix("b", after=0.1)
    rec.mark("caption3")
    h.wait_for("Switched provider", timeout=30)  # Bridge hands the task to the next provider
    rec.mark("walk1")
    walk = WALKS[0]
    tail = walk_row_numbers(h.recorder.latest, walk)
    switched = next((n for n, row in enumerate(tail) if "Switched provider" in h.recorder.latest.split("\n")[row]), len(tail) - 1)
    rec.marks["poster"] = rec.marks["walk1"] + switched * WALK_STEP_SECONDS + WALK_STEP_SECONDS / 2
    h.pause(walk.steps * WALK_STEP_SECONDS + 0.1)
    (world.go_dir / "codex.go").write_text("go", encoding="utf-8")  # Codex starts streaming as the station comes back
    rec.mark("cut_start")
    h.prefix("p", after=0.1)
    h.wait_for("codex \u00b7 scripted", timeout=30)  # until then tmux shows the old pane; that wait is cut from the clip
    rec.mark("cut_end")
    rec.marks["cut_end"] -= 0.25  # keep the capture that first showed the banner
    h.wait_for("passed", timeout=40)
    rec.mark("caption4")
    h.wait_for("Provider exited", timeout=30)  # Codex stopped: the task goes to verification
    h.pause(0.2)
    h.prefix("b", after=0.1)
    h.wait_for("Needs you", timeout=20)
    rec.mark("walk2")
    h.pause((WALKS[1].steps - 1) * WALK_STEP_SECONDS + 0.1)  # the last step is held by the clip's final hold


STORYBOARDS = {"full": run_storyboard, "highlight": run_highlight, "limits": run_limits}


def redact(text: str, replacements: dict[str, str]) -> str:
    """Replace private path prefixes in captured text.

    The whole path that starts with the private prefix is rewritten, then padded
    with spaces to its old length so that the columns after it do not move.
    """
    for old, new in replacements.items():
        pattern = re.compile(re.escape(old) + r"[\w./@+-]*")

        def swap(match: re.Match[str], new: str = new, old: str = old) -> str:
            original = match.group(0)
            return (new + original[len(old):]).ljust(len(original))

        text = pattern.sub(swap, text)
    return text


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", value)
    if not match:
        raise argparse.ArgumentTypeError("size must look like 110x30")
    return int(match.group(1)), int(match.group(2))


HIGHLIGHT_DEFAULTS = {"size": (90, 24), "cps": 60.0, "tail_hold": 2.0, "max_hold": 3.0, "seam_window": 4.0, "cell_width": 10}
FULL_DEFAULTS = {"size": (110, 30), "cps": 25.0, "tail_hold": 3.0, "max_hold": 4.0, "seam_window": 0.0, "cell_width": 10}
LIMITS_DEFAULTS = {
    "size": (90, 24), "cps": 60.0, "tail_hold": 1.3, "max_hold": 1.6, "seam_window": 0.0, "cell_width": 12,
    "gif_fps": 12.0, "gif_width": 960, "max_kb": 600,
}
MAX_STATIC_SECONDS = 0.8  # the limits clip must never show one picture longer than this, apart from its final hold
STORYBOARD_DEFAULTS = {"full": FULL_DEFAULTS, "highlight": HIGHLIGHT_DEFAULTS, "limits": LIMITS_DEFAULTS}
# Storyboards that restart or leave programs, so captures of empty screens and half-painted panes are dropped.
FILTERED_STORYBOARDS = frozenset({"highlight", "limits"})


def resolve_defaults(args: argparse.Namespace) -> None:
    """Fill options the user left out with the defaults of the chosen storyboard."""
    defaults = STORYBOARD_DEFAULTS[args.storyboard]
    for name, value in defaults.items():
        if getattr(args, name) is None:
            setattr(args, name, value)


def wait_for_screen(session: TmuxSession, needle: str | tuple[str, ...], timeout: float = 30.0) -> None:
    """Wait until the screen shows ``needle`` (or, for a tuple, any of its items)."""
    needles = (needle,) if isinstance(needle, str) else needle
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = strip_ansi(session.capture()[0])
        if any(item in text for item in needles):
            return
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {needle!r}; last screen:\n{text}")


def record(args: argparse.Namespace) -> tuple[list[Sample], tuple[int, int], dict[str, float]]:
    for tool in ("tmux", "git"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is required to record the demo")
    highlight = args.storyboard == "highlight"
    limits = args.storyboard == "limits"
    root = Path(tempfile.mkdtemp(prefix="bd-"))
    world = DemoWorld(root, args.theme)
    session = TmuxSession(f"bridge-demo-{os.getpid()}", args.size)
    recorder: Recorder | None = None
    # Private temporary paths are replaced in the captured text (cosmetic only).
    replacements = {str(world.home): "/home/demo", str(world.root): "/tmp/demo"}

    def finish(items: list[Sample]) -> list[Sample]:
        cleaned = [Sample(x.t, redact(x.text, replacements), x.cursor, x.tag) for x in items]
        return drop_blips(drop_transient_samples(cleaned)) if args.storyboard in FILTERED_STORYBOARDS else cleaned

    try:
        world.install_fake_clis(named=limits)
        env = world.shell_env()
        if limits:
            world.make_repo("acme-api", REPO_FILES)
            prepare_limits(world)
            env = world.shell_env()  # now with the injected capacity file and the stand-in providers' settings
            command = ["env", "-i", *[f"{k}={v}" for k, v in env.items()], "sh", "-c", "while :; do llm-bridge; done"]
            session.start(command, world.repos["acme-api"])
            if session.geometry() != args.size:
                raise RuntimeError(f"tmux pane is {session.geometry()}, expected {args.size}")
            limits_preroll(world, session)
        elif highlight:
            for name in ("acme-api", "acme-web", "acme-infra", "acme-docs"):
                world.make_repo(name, REPO_FILES)
            seed_missions(world)
            env = world.shell_env()  # now with the gate directory and live provider speed
            # The shell loop reopens the home screen after the station is detached.
            command = ["env", "-i", *[f"{k}={v}" for k, v in env.items()], "sh", "-c", "while :; do llm-bridge; done"]
            session.start(command, world.repos["acme-api"])
            if session.geometry() != args.size:
                raise RuntimeError(f"tmux pane is {session.geometry()}, expected {args.size}")
            wait_for_screen(session, "Choose a mission", timeout=30)
            session.send("Down")  # select the live mission; the first row is "New mission"
            time.sleep(0.5)
        else:
            repo = world.make_repo(
                "acme-api",
                {
                    "README.md": "# acme-api\n\nA small REST API.\n",
                    "pyproject.toml": "[project]\nname = 'acme-api'\nversion = '0.1.0'\n",
                    "src/api/server.py": "app = Router()\n",
                    "src/api/middleware.py": "def log_requests(request):\n    pass\n",
                    "tests/test_server.py": "def test_ok():\n    assert True\n",
                },
            )
            if args.multi:
                world.make_repo("acme-web", {"README.md": "# acme-web\n", "src/checkout.js": "export const total = () => 0;\n"})
            command = ["env", "-i", *[f"{k}={v}" for k, v in env.items()], "bash", "--noprofile", "--norc", "-i"]
            session.start(command, repo)
            if session.geometry() != args.size:
                raise RuntimeError(f"tmux pane is {session.geometry()}, expected {args.size}")
            time.sleep(0.6)
            session.send("clear", "Enter")
            time.sleep(0.5)
        recorder = Recorder(session, args.fps)
        recorder.start()
        human = Human(session, recorder, cps=args.cps)
        STORYBOARDS[args.storyboard](world, human, args.size)
        if args.multi and not (highlight or limits):
            run_multi_scene(world, human)
            human.pause(1.0)
        samples = finish(recorder.stop())
        return samples, args.size, dict(recorder.marks)
    finally:
        if recorder is not None:
            partial = finish(recorder.stop())
            if args.frames_out:  # keep the capture even when the storyboard failed
                save_frames(args.frames_out, partial, args.size, recorder.marks, args.storyboard)
        session.kill()
        world.cleanup(keep=args.keep_temp)


def render_poster(
    samples: Sequence[Sample],
    size: tuple[int, int],
    theme: Theme,
    mark: float | None,
    *,
    chrome: bool = True,
    show_cursor: bool = False,
    cell_w: int = 10,
    cell_h: int = 20,
    title: str = "llm-bridge",
    overlay: Overlay | None = None,
):
    """Image of the most informative capture (see ``poster_index``), with its caption when there is an overlay."""
    cols, rows = size
    sample = samples[poster_index(samples, mark)]
    renderer = Renderer(cols, rows, theme, cell_w=cell_w, cell_h=cell_h, chrome=chrome, title=title, show_cursor=show_cursor)
    image = renderer.render(parse_ansi(sample.text, cols, rows), sample.cursor)
    if overlay is not None:
        band = CaptionBand(renderer.size[0], theme, overlay.captions)
        image = apply_overlay(image, band, renderer, sample.text, overlay.tag(sample.t))
    return image


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record or render an LLM Bridge demo animation.")
    parser.add_argument("--out", type=Path, default=Path("demo.gif"), help="GIF output path (default: demo.gif)")
    parser.add_argument("--mp4", type=Path, help="also write an H.264 MP4 (needs ffmpeg)")
    parser.add_argument("--webm", type=Path, help="also write a VP9 WebM (needs ffmpeg)")
    parser.add_argument("--poster", type=Path, help="also write the most informative frame as a PNG (for a video poster= attribute)")
    parser.add_argument("--storyboard", choices=sorted(STORYBOARDS), default=None, help="'full' is the 60 s walkthrough (default); 'highlight' is the 20 s loop; 'limits' is the 14 s hero loop in which a provider runs out and the mission carries on with the next one")
    parser.add_argument("--theme", choices=sorted(THEMES), default="dark", help="terminal colour theme; also sets LLM_BRIDGE_THEME while recording")
    parser.add_argument("--size", type=parse_size, default=None, help="terminal size in columns x rows (default: 110x30, highlight 100x28)")
    parser.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier (default: 1.0)")
    parser.add_argument("--fps", type=float, default=10.0, help="screen capture rate while recording (default: 10)")
    parser.add_argument("--cps", type=float, default=None, help="typing speed in characters per second (default: 25, highlight 60)")
    parser.add_argument("--max-hold", type=float, default=None, help="longest time one still frame may stay on screen, in seconds (default: 4, highlight 3)")
    parser.add_argument("--tail-hold", type=float, default=None, help="how long the last frame stays before the loop restarts (default: 3, highlight 2)")
    parser.add_argument("--seam-window", type=float, default=None, help="cut the end of the recording at the frame, within this many final seconds, that looks most like the first (default: off, highlight 4)")
    parser.add_argument("--cell-width", type=int, default=None, help="pixel width of a terminal cell (default: 10, highlight 9; height is twice this)")
    parser.add_argument("--gif-colors", type=int, default=256, help="palette size of the GIF (default: 256)")
    parser.add_argument("--gif-fps", type=float, default=None, help="frame rate of the GIF (default: the frames' own timing; limits 12)")
    parser.add_argument("--gif-width", type=int, default=None, help="width of the GIF in pixels (default: the frame width; limits 960)")
    parser.add_argument("--max-kb", type=int, default=None, help="raise the CRF until the MP4 and the WebM are at most this many KB (default: no limit; limits 600)")
    parser.add_argument("--no-cursor", action="store_true", help="do not draw the terminal cursor")
    parser.add_argument("--no-chrome", action="store_true", help="do not draw the window title bar")
    parser.add_argument("--frames-out", type=Path, help="save the raw captures as JSON so they can be re-rendered")
    parser.add_argument("--frames-in", type=Path, help="render saved captures instead of recording")
    parser.add_argument("--multi", action="store_true", help="add a second scene: a second repository's mission in the mission list (full storyboard)")
    parser.add_argument("--keep-temp", action="store_true", help="keep the temporary state directory (debugging)")
    args = parser.parse_args(argv)
    if args.speed <= 0 or args.fps <= 0 or (args.cps is not None and args.cps <= 0):
        parser.error("--speed, --fps and --cps must be positive")
    if args.storyboard is None:
        args.storyboard = load_storyboard(args.frames_in) if args.frames_in else "full"
    resolve_defaults(args)

    marks: dict[str, float] = {}
    if args.frames_in:
        samples, size = load_frames(args.frames_in)
        marks = load_marks(args.frames_in)
        if args.storyboard in FILTERED_STORYBOARDS:
            samples = drop_blips(drop_transient_samples(samples))
    else:
        samples, size, marks = record(args)
    theme = THEMES[args.theme]
    samples, marks = apply_cuts(samples, marks)
    overlay = Overlay.from_marks(marks) if args.storyboard == "limits" else None
    samples = tag_samples(samples, overlay)
    images, durations = render_samples(
        samples, size, theme, speed=args.speed, tail_hold=args.tail_hold, max_hold=args.max_hold,
        chrome=not args.no_chrome, show_cursor=not args.no_cursor, cell_w=args.cell_width, cell_h=args.cell_width * 2,
        seam_window=args.seam_window, overlay=overlay,
    )
    encode_media(
        images, durations, args.out, args.mp4, args.webm, gif_colors=args.gif_colors, gif_fps=args.gif_fps,
        gif_width=args.gif_width, max_bytes=args.max_kb * 1000 if args.max_kb else None,
        mp4_crf=30 if args.max_kb else 16, webm_crf=38 if args.max_kb else 32,
    )
    if args.poster:
        poster = render_poster(
            samples, size, theme, marks.get("poster"), chrome=not args.no_chrome,
            cell_w=args.cell_width, cell_h=args.cell_width * 2, overlay=overlay,
        )
        poster.save(args.poster)
    total = sum(durations)
    outputs = [str(path) for path in (args.out, args.mp4, args.webm, args.poster) if path]
    print(f"{len(images)} frames, {total:.1f}s -> {', '.join(outputs)}")
    if overlay is not None:
        still = longest_static(durations)
        print(f"longest still picture: {still:.2f}s (limit {MAX_STATIC_SECONDS}s apart from the final hold)")
        if still > MAX_STATIC_SECONDS + 0.05:
            print("warning: the clip holds one picture too long; check the pacing of the storyboard", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
