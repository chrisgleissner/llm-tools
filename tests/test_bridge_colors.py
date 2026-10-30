"""Accessible semantic colors for the Bridge curses and tmux interfaces."""

from __future__ import annotations

import curses
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

from llm_tools import bridge_theme, bridge_tui
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled


def _luminance(rgb: tuple[int, int, int]) -> float:
    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: tuple[int, int, int], background: tuple[int, int, int]) -> float:
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"COLORFGBG": "15;0"}, "dark"),
        ({"COLORFGBG": "0;15"}, "light"),
        ({"COLORFGBG": "15;0", "LLM_BRIDGE_THEME": "light"}, "light"),
        ({"COLORFGBG": "0;15", "LLM_BRIDGE_THEME": "dark"}, "dark"),
        ({}, "dark"),
    ],
)
def test_theme_resolution_prefers_explicit_choice_and_detects_colorfgbg(env, expected) -> None:
    assert bridge_theme.resolve_theme(env) == expected


@pytest.mark.parametrize(
    ("env", "disabled"),
    [
        ({"NO_COLOR": ""}, True),
        ({"TERM": "dumb"}, True),
        ({"LLM_USAGE_NO_COLOR": "1"}, True),
        ({"LLM_USAGE_NO_COLOR": "0"}, False),
        ({"TERM": "xterm-256color"}, False),
    ],
)
def test_color_disable_contract(env, disabled) -> None:
    assert bridge_theme.colors_disabled(env) is disabled


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_semantic_roles_have_contrast_and_shared_overrides(theme) -> None:
    background = (0, 0, 0) if theme == "dark" else (255, 255, 255)
    for role in ("brand", "ok", "warn", "error", "command", "heading"):
        color, bold, _dim, _underline = bridge_theme.sgr_foreground(
            bridge_theme.role_sgr(role, theme=theme, env={})
        )
        assert color is not None
        assert _contrast(bridge_theme._xterm_rgb(color), background) >= 4.5, (theme, role, color)
        assert bold, (theme, role)

    assert bridge_theme.role_sgr("error", theme=theme, env={"LLM_TOOLS_COLOR_ERROR": "1;34"}) == "1;34"
    assert bridge_theme.sgr_foreground(bridge_theme.role_sgr("error", theme=theme, env={"LLM_TOOLS_COLOR_ERROR": "1;34"}))[0] == 4


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_eight_color_fallback_keeps_semantic_roles_legible(theme) -> None:
    background = (0, 0, 0) if theme == "dark" else (255, 255, 255)
    for role in ("brand", "ok", "warn", "error", "command", "heading"):
        color, _bold, _dim, _underline = bridge_theme.sgr_foreground(
            bridge_theme.role_sgr(role, theme=theme, env={})
        )
        fitted = bridge_theme.fit_role_color(color, 8, theme)
        assert 0 <= fitted < 8
        assert _contrast(bridge_theme._xterm_rgb(fitted), background) >= bridge_theme.LOW_COLOR_CONTRAST, (theme, role, fitted)


def test_selected_role_is_high_contrast_and_monochrome_keeps_emphasis(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui, "_ROLE_ATTRS", {"brand": 16})
    assert bridge_tui.role_attr("selected") & bridge_tui.curses.A_REVERSE
    assert bridge_tui.role_attr("selected") & bridge_tui.curses.A_BOLD
    assert bridge_tui.role_attr("selected") == (
        16 | bridge_tui.curses.A_REVERSE | bridge_tui.curses.A_BOLD
    )


def test_short_status_messages_map_to_semantic_roles() -> None:
    assert bridge_tui._status_role("Rejected: invalid transition") == "error"
    assert bridge_tui._status_role("Warning: usage snapshot is stale") == "warn"
    assert bridge_tui._status_role("Applied: task add") == "ok"
    assert bridge_tui._status_role("PgUp older events | Ctrl-D exit") == "dim"


def test_curses_color_init_respects_no_color_and_maps_palette(monkeypatch) -> None:
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(bridge_tui.curses, "has_colors", lambda: True)
    monkeypatch.setattr(bridge_tui.curses, "start_color", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(bridge_tui.curses, "color_pair", lambda pair: pair << 8)
    monkeypatch.setattr(bridge_tui.curses, "COLORS", 8, raising=False)
    monkeypatch.setattr(bridge_tui.curses, "COLOR_PAIRS", 64, raising=False)

    assert not bridge_tui.initialize_colors({"NO_COLOR": "1", "TERM": "xterm"}, is_tty=True)
    assert calls == []
    assert not bridge_tui.initialize_colors({"TERM": "xterm"}, is_tty=False)
    assert calls == []
    assert bridge_tui.initialize_colors({"TERM": "xterm", "LLM_BRIDGE_THEME": "light"}, is_tty=True)
    assert calls
    assert all(-1 <= fg < 8 and bg == -1 for _pair, fg, bg in calls)  # info keeps the terminal foreground
    assert bridge_tui._CURRENT_THEME == "light"
    assert not bridge_tui.role_attr("error") & curses.A_BOLD
    assert not bridge_tui.role_attr("heading") & curses.A_BOLD
    assert bridge_tui.role_attr("selected") & curses.A_BOLD
    bridge_tui.initialize_colors({"TERM": "xterm", "LLM_BRIDGE_THEME": "dark"}, is_tty=True)
    assert bridge_tui.role_attr("error") & curses.A_BOLD


class _RecordingWindow:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def addnstr(self, *args: object) -> None:
        self.calls.append(args)


def test_put_applies_semantic_role_attributes(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui, "role_attr", lambda role: {"error": 123}.get(role, 0))
    window = _RecordingWindow()
    bridge_tui._put(window, 2, 3, "Rejected: no capacity", 80, role="error")
    assert window.calls == [(2, 3, "Rejected: no capacity", 80, 123)]


def _read_until(master: int, process: subprocess.Popen[bytes], output: bytearray, marker: bytes) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process.poll() is None:
        readable, _, _ = select.select([master], [], [], 0.03)
        if readable:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break
        if marker in output:
            return
    assert marker in output, f"Bridge screen did not render {marker!r}"


@pytest.mark.parametrize("no_color", [False, True], ids=["color", "no-color"])
def test_real_curses_pty_obeys_color_mode(tmp_path: Path, env: dict[str, str], no_color: bool) -> None:
    state = tmp_path / "state"
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("Color contract", "Keep controls readable", cwd=tmp_path, initial_prompt="Start")
    env.update(LLM_BRIDGE_STATE_DIR=str(state), TERM="xterm-256color", LLM_BRIDGE_THEME="dark")
    env.pop("NO_COLOR", None)
    env.pop("LLM_USAGE_NO_COLOR", None)
    if no_color:
        env["NO_COLOR"] = "1"
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "llm_tools.bridge", "control", "--mission", mission["id"]],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    try:
        output = bytearray()
        _read_until(master, process, output, b"Color contract")
        assert b"Color contract" in output
        has_256_color = re.search(rb"\x1b\[[0-9;]*38;5;[0-9]+m", output) is not None
        assert has_256_color is not no_color
        os.write(master, b"\x04")
        process.wait(timeout=scaled(3))
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=scaled(2))
        os.close(master)


def test_tmux_status_markup_uses_theme_roles_and_honors_no_color() -> None:
    dark = bridge_theme.tmux_role_markup("error", env={"TERM": "xterm-256color", "LLM_BRIDGE_THEME": "dark"})
    light = bridge_theme.tmux_role_markup("error", env={"TERM": "xterm-256color", "LLM_BRIDGE_THEME": "light"})
    plain = bridge_theme.tmux_role_markup("error", env={"TERM": "dumb"})
    assert dark != light
    assert "colour203" in dark
    assert "colour160" in light
    assert "colour" not in plain


def test_styled_usage_and_response_keep_column_spacing_and_indentation(monkeypatch) -> None:
    class Window(_RecordingWindow):
        def getmaxyx(self): return (24, 100)
        def erase(self): self.calls.clear()
        def noutrefresh(self): pass
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    state = bridge_tui._ConsoleState("mission", lambda *_: "", lambda *_: {}, full=True)
    state.view_index = bridge_tui.VIEWS.index("Usage")
    state.snapshot = {"usage": {"text": "Provider       Scope       Remaining\ncodex          5h          72%"}}
    window = Window()
    bridge_tui._draw_full(window, state)
    lines = [(call[1], call[2]) for call in window.calls]
    assert (2, "Provider       Scope       Remaining") in lines  # two-cell gutter, spacing untouched
    assert (2, "codex          5h          72%") in lines
    state.output.extend(['{', '    "status": "FAILED"', '}'])
    bridge_tui._draw_response_overlay(window, state)
    assert '    "status": "FAILED"' in [call[2] for call in window.calls]


def test_active_navigation_and_negative_readiness_have_unambiguous_emphasis(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui, "role_attr", lambda role: 100 if role == "accent" else 10)
    window = _RecordingWindow()
    bridge_tui._draw_navigation(window, 1, "  ", 100)
    assert window.calls[-1][2] == "[2 Tasks]"
    assert window.calls[-1][-1] == 100
    assert window.calls[0][-1] == 10
    assert window.calls[0][2].index("[2 Tasks]") == window.calls[-1][1]
    assert bridge_tui._status_role("Next: none ready") == "dim"
    assert bridge_tui._status_role("ready=False") == "warn"


def test_shared_role_override_resets_follow_sgr_semantics() -> None:
    assert bridge_theme.sgr_foreground("1;2;4;0;31") == (1, False, False, False)
    assert bridge_theme.sgr_foreground("1;4;24;32") == (2, True, False, False)
