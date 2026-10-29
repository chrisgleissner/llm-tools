"""Semantic color roles shared by the Bridge curses UI and tmux status line."""

from __future__ import annotations

import os
from typing import Mapping, Literal

from . import common


Theme = Literal["dark", "light"]

_LIGHT_SGR: dict[str, str] = {
    "brand": "1;38;5;25",
    "info": "38;5;24",
    "ok": "1;38;5;28",
    "warn": "1;38;5;130",
    "error": "1;38;5;160",
    "dim": "38;5;240",
    "diff_add": "38;5;28",
    "diff_remove": "38;5;160",
    "diff_hunk": "1;38;5;25",
    "command": "1;38;5;25",
    "tool": "1;38;5;30",
    "stderr": "38;5;238",
    "heading": "1;38;5;25",
}

_DARK_SGR: dict[str, str] = {
    **common.ANSI_COLOR_ROLES,
    "heading": "1;38;5;39",
    "warn": "1;38;5;220",
    "error": "1;38;5;203",
}

_XTERM_BASE_RGB = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)
_XTERM_CUBE = (0, 95, 135, 175, 215, 255)


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


def _background_is_light(colorfgbg: str) -> bool | None:
    parts = colorfgbg.split(";")
    if not parts or not parts[-1].isdigit():
        return None
    index = int(parts[-1])
    if index < 16:
        # The standard 16-color slots 7 and 15 are the light foregrounds
        # commonly used as a light terminal background in COLORFGBG.
        return index in {7, 15}
    red, green, blue = _xterm_rgb(min(index, 255))
    return 299 * red + 587 * green + 114 * blue >= 128_000


def resolve_theme(env: Mapping[str, str] | None = None) -> Theme:
    """Choose an explicit theme first, then COLORFGBG, then dark as fallback."""
    values = os.environ if env is None else env
    selected = values.get("LLM_BRIDGE_THEME", "").strip().lower()
    if selected in {"dark", "light"}:
        return selected  # type: ignore[return-value]
    detected = _background_is_light(values.get("COLORFGBG", ""))
    return "light" if detected else "dark"


def role_sgr(
    role: str,
    *,
    theme: Theme | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return a semantic role's SGR spec, honoring shared color overrides."""
    values = os.environ if env is None else env
    override_key = f"LLM_TOOLS_COLOR_{role.upper()}"
    if override_key in values:
        return common.color_code(role, dict(values))
    palette = _LIGHT_SGR if (theme or resolve_theme(values)) == "light" else _DARK_SGR
    return palette.get(role, palette.get("info", common.ANSI_COLOR_ROLES["info"]))


def sgr_foreground(sgr: str) -> tuple[int | None, bool, bool, bool]:
    """Decode foreground index plus bold/dim/underline attributes from SGR."""
    try:
        codes = [int(part) if part else 0 for part in sgr.split(";")]
    except ValueError:
        return None, False, False, False
    foreground: int | None = None
    bold = dim = underline = False
    index = 0
    while index < len(codes):
        code = codes[index]
        if code == 0:
            foreground = None
            bold = dim = underline = False
        elif code == 1:
            bold = True
        elif code == 2:
            dim = True
        elif code == 4:
            underline = True
        elif code == 22:
            bold = dim = False
        elif code == 24:
            underline = False
        elif code == 39:
            foreground = None
        elif 30 <= code <= 37:
            foreground = code - 30
        elif 90 <= code <= 97:
            foreground = code - 90 + 8
        elif code == 38 and index + 2 < len(codes):
            if codes[index + 1] == 5:
                foreground = max(0, min(255, codes[index + 2]))
                index += 2
            elif codes[index + 1] == 2 and index + 4 < len(codes):
                rgb = tuple(max(0, min(255, channel)) for channel in codes[index + 2 : index + 5])
                foreground = min(range(256), key=lambda candidate: sum(
                    (left - right) ** 2 for left, right in zip(_xterm_rgb(candidate), rgb)
                ))
                index += 4
        index += 1
    return foreground, bold, dim, underline


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
    return min(range(candidate_count), key=lambda candidate: sum(
        (left - right) ** 2 for left, right in zip(_xterm_rgb(candidate), rgb)
    ))


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def fit_role_color(index: int | None, colors: int, theme: Theme) -> int:
    """Keep low-color terminals readable against their detected background."""
    fitted = fit_color(index, colors, default=7 if theme == "dark" else 0)
    if fitted < 0 or colors <= 0:
        return 7 if theme == "dark" else 0
    background = _relative_luminance((0, 0, 0) if theme == "dark" else (255, 255, 255))
    foreground = _relative_luminance(_xterm_rgb(fitted))
    contrast = (max(foreground, background) + 0.05) / (min(foreground, background) + 0.05)
    if contrast >= 4.5:
        return fitted
    # Eight-color sets cannot represent bright red or dark green with enough
    # contrast on both backgrounds. Fall back to the theme's high-contrast
    # neutral; the role still keeps its bold/dim attributes and text cues.
    return 7 if theme == "dark" else 0


def tmux_role_markup(role: str, *, env: Mapping[str, str] | None = None) -> str:
    """Return tmux status markup for a role, or plain style when disabled."""
    values = os.environ if env is None else env
    if colors_disabled(values):
        return "#[default]"
    index, bold, dim, underline = sgr_foreground(role_sgr(role, env=values))
    parts = ["#[default]"]
    if index is not None:
        parts.append(f"#[fg=colour{index}]")
    if bold:
        parts.append("#[bold]")
    if dim:
        parts.append("#[dim]")
    if underline:
        parts.append("#[underscore]")
    return "".join(parts)
