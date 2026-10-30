"""Bridge colour themes: detection, contrast, configuration and the theme command."""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge, bridge_theme as theme, bridge_tui, config as toolconfig
from tests.conftest import scaled


@pytest.fixture(autouse=True)
def _fresh_theme_cache():
    theme.reset_cache()
    yield
    theme.reset_cache()


def _no_probes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the terminal and macOS probes with recorders that find nothing."""
    calls: list[str] = []
    monkeypatch.setattr(theme, "_osc_skip_reason", lambda values: None)
    monkeypatch.setattr(theme, "_osc11_background", lambda values: calls.append("osc") or None)
    monkeypatch.setattr(theme, "_macos_prefers_dark", lambda values: calls.append("macos") or None)
    return calls


# --- contrast -----------------------------------------------------------------

def _palette_cases() -> list[tuple[str, str, dict[str, tuple[int, int, int]], float]]:
    return [
        ("dark", "dark", theme.DARK_BACKGROUNDS, theme.TEXT_CONTRAST),
        ("light", "light", theme.LIGHT_BACKGROUNDS, theme.TEXT_CONTRAST),
        ("high-contrast", "dark", theme.DARK_BACKGROUNDS, 6.0),
        ("high-contrast", "light", theme.LIGHT_BACKGROUNDS, 6.0),
    ]


@pytest.mark.parametrize(("name", "polarity", "backgrounds", "text_minimum"), _palette_cases())
def test_every_role_meets_contrast_on_typical_backgrounds(name, polarity, backgrounds, text_minimum) -> None:
    resolved = theme.resolve({"LLM_BRIDGE_THEME": name, "LLM_BRIDGE_BACKGROUND": polarity})
    assert resolved.polarity == polarity
    checked = 0
    for role in theme.ROLES:
        style = resolved.style(role)
        if style.bg is not None:
            # The selection bar carries its own background; check its text on it.
            assert style.fg is not None
            assert theme.contrast_ratio(theme.xterm_rgb(style.fg), theme.xterm_rgb(style.bg)) >= theme.TEXT_CONTRAST, role
            continue
        if style.fg is None:
            continue  # terminal default foreground is the user's own choice
        minimum = theme.DECORATIVE_CONTRAST if role in theme.DECORATIVE_ROLES else text_minimum
        for label, background in backgrounds.items():
            ratio = theme.contrast_ratio(theme.xterm_rgb(style.fg), background)
            assert ratio >= minimum, f"{name}/{role} fg={style.fg} on {label}: {ratio:.2f} < {minimum}"
        checked += 1
    assert checked >= 25


def test_muted_is_text_grade_and_dim_is_decorative() -> None:
    assert "dim" in theme.DECORATIVE_ROLES and "muted" not in theme.DECORATIVE_ROLES
    assert theme.contrast_ratio((255, 255, 255), (0, 0, 0)) == pytest.approx(21.0)
    assert theme.is_light_background((255, 255, 255)) and not theme.is_light_background((0x30, 0x0A, 0x24))


def test_only_the_selection_bar_fills_a_background() -> None:
    for name, polarity in (("dark", "dark"), ("light", "light"), ("high-contrast", "dark"), ("high-contrast", "light")):
        resolved = theme.resolve({"LLM_BRIDGE_THEME": name, "LLM_BRIDGE_BACKGROUND": polarity})
        assert [role for role in theme.ROLES if resolved.style(role).bg is not None] == ["selected"]


def test_role_catalogue_is_documented_and_keeps_legacy_roles() -> None:
    legacy = {"brand", "info", "ok", "warn", "error", "dim", "diff_add", "diff_remove", "diff_hunk",
              "command", "tool", "stderr", "heading"}
    assert legacy <= set(theme.ROLES)
    assert set(theme.ROLES) == set(theme.ROLE_DESCRIPTIONS)
    for role in theme.ROLES:
        assert role in theme.__doc__, f"{role} is missing from the module docstring"
    for palette in theme.PALETTES.values():
        assert set(palette) == set(theme.ROLES)


# --- detection order ------------------------------------------------------------

def test_detection_order_env_then_config_then_colorfgbg_then_osc_then_macos_then_default(monkeypatch) -> None:
    calls = _no_probes(monkeypatch)
    light_cfg = {"name": "light"}

    # 1. LLM_BRIDGE_THEME beats config, COLORFGBG and every probe.
    result = theme.resolve({"LLM_BRIDGE_THEME": "dark", "COLORFGBG": "0;15"}, probe=True, theme_cfg=light_cfg)
    assert (result.name, result.polarity) == ("dark", "dark")
    assert result.name_source == "LLM_BRIDGE_THEME=dark"
    # 2. Config name beats COLORFGBG.
    result = theme.resolve({"COLORFGBG": "15;0"}, probe=True, theme_cfg=light_cfg)
    assert result.polarity == "light" and "[bridge.theme] name" in result.name_source
    assert calls == []
    # 3. Explicit background variable, then config background, then COLORFGBG.
    result = theme.resolve({"LLM_BRIDGE_BACKGROUND": "light", "COLORFGBG": "15;0"}, probe=True, theme_cfg={})
    assert result.polarity == "light" and result.polarity_source == "LLM_BRIDGE_BACKGROUND=light"
    result = theme.resolve({"COLORFGBG": "15;0"}, probe=True, theme_cfg={"background": "light"})
    assert result.polarity == "light" and "[bridge.theme] background" in result.polarity_source
    result = theme.resolve({"COLORFGBG": "0;15"}, probe=True, theme_cfg={})
    assert result.polarity == "light" and result.polarity_source == "COLORFGBG=0;15"
    assert calls == []
    # 4. OSC 11 beats macOS.
    monkeypatch.setattr(theme, "_osc11_background", lambda values: (250, 250, 250))
    monkeypatch.setattr(theme, "_macos_prefers_dark", lambda values: calls.append("macos") or True)
    result = theme.resolve({}, probe=True, theme_cfg={})
    assert result.polarity == "light" and result.polarity_source.startswith("OSC 11") and result.probed
    assert result.background_rgb == (250, 250, 250)
    assert calls == []
    monkeypatch.setattr(theme, "_osc11_background", lambda values: (0x30, 0x0A, 0x24))
    assert theme.resolve({}, probe=True, theme_cfg={}).polarity == "dark"
    # 5. macOS when the terminal does not answer.
    monkeypatch.setattr(theme, "_osc11_background", lambda values: None)
    result = theme.resolve({}, probe=True, theme_cfg={})
    assert result.polarity == "dark" and result.polarity_source == "macOS AppleInterfaceStyle"
    monkeypatch.setattr(theme, "_macos_prefers_dark", lambda values: False)
    assert theme.resolve({}, probe=True, theme_cfg={}).polarity == "light"
    # 6. Fall back to dark.
    monkeypatch.setattr(theme, "_macos_prefers_dark", lambda values: None)
    result = theme.resolve({}, probe=True, theme_cfg={})
    assert result.polarity == "dark" and "default" in result.polarity_source
    assert any("no answer" in note for note in result.probe_notes)


def test_hermetic_resolution_never_probes(monkeypatch) -> None:
    calls = _no_probes(monkeypatch)
    assert theme.resolve({}).polarity == "dark"
    assert theme.resolve({"COLORFGBG": "0;15"}).polarity == "light"
    assert calls == []


def test_osc_probe_is_skipped_inside_tmux_without_tty_and_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(theme.sys.stdin, "fileno", lambda: 0)
    assert "tmux" in theme._osc_skip_reason({"TMUX": "/tmp/tmux-1/default,1,0"})
    assert "tmux" in theme._osc_skip_reason({"TERM": "screen-256color"})
    assert "LLM_BRIDGE_NO_OSC" in theme._osc_skip_reason({"LLM_BRIDGE_NO_OSC": "1", "TERM": "xterm"})
    monkeypatch.setattr(theme.os, "isatty", lambda fd: False)
    assert "not a terminal" in theme._osc_skip_reason({"TERM": "xterm"})
    monkeypatch.setattr(theme.os, "isatty", lambda fd: True)
    monkeypatch.setattr(theme.sys.stdout, "fileno", lambda: 1, raising=False)
    assert theme._osc_skip_reason({"TERM": "xterm"}) is None


def test_skipped_probe_is_reported_and_colour_disabled_never_probes(monkeypatch) -> None:
    calls = _no_probes(monkeypatch)
    monkeypatch.setattr(theme, "_osc_skip_reason", lambda values: "inside tmux/screen")
    result = theme.resolve({}, probe=True, theme_cfg={})
    assert any("skipped: inside tmux" in note for note in result.probe_notes)
    calls.clear()
    monkeypatch.setattr(theme, "_osc_skip_reason", lambda values: None)
    assert theme.resolve({"NO_COLOR": "1"}, probe=True, theme_cfg={}).name == "mono"
    assert calls == []


def test_prepare_exports_probed_background_for_the_tmux_station(monkeypatch, capsys) -> None:
    _no_probes(monkeypatch)
    monkeypatch.setattr(theme, "_osc11_background", lambda values: (255, 255, 255))
    for key in ("COLORFGBG", "LLM_BRIDGE_THEME", "LLM_BRIDGE_BACKGROUND", "NO_COLOR", "LLM_USAGE_NO_COLOR", "TMUX"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LLM_BRIDGE_COLOR_NOPE", "red")
    resolved = theme.prepare()
    assert resolved.polarity == "light"
    assert os.environ["LLM_BRIDGE_BACKGROUND"] == "light"
    err = capsys.readouterr().err
    assert err.count("llm-bridge: theme: ") == 1 and "unknown role 'nope'" in err
    theme.prepare()
    assert capsys.readouterr().err == ""  # warnings are printed once per process
    # A child process inheriting the export resolves the same background without a probe.
    child = theme.resolve({"LLM_BRIDGE_BACKGROUND": os.environ["LLM_BRIDGE_BACKGROUND"], "TMUX": "x"})
    assert child.polarity == "light"
    monkeypatch.delenv("LLM_BRIDGE_BACKGROUND")


def test_macos_defaults_probe(monkeypatch) -> None:
    class Result:
        def __init__(self, code: int, out: str) -> None:
            self.returncode, self.stdout = code, out

    terminal = {"TERM_PROGRAM": "Apple_Terminal"}
    monkeypatch.setattr(theme.platform, "system", lambda: "Linux")
    assert theme._macos_prefers_dark(terminal) is None
    monkeypatch.setattr(theme.platform, "system", lambda: "Darwin")
    assert theme._macos_prefers_dark({"TERM_PROGRAM": "WezTerm"}) is None
    assert theme._macos_prefers_dark({**terminal, "SSH_CONNECTION": "a b c d"}) is None
    monkeypatch.setattr(theme.shutil, "which", lambda name: None)
    assert theme._macos_prefers_dark(terminal) is None
    monkeypatch.setattr(theme.shutil, "which", lambda name: "/usr/bin/defaults")
    monkeypatch.setattr(theme.subprocess, "run", lambda *a, **k: Result(0, "Dark\n"))
    assert theme._macos_prefers_dark({"TERM_PROGRAM": "iTerm.app"}) is True
    monkeypatch.setattr(theme.subprocess, "run", lambda *a, **k: Result(1, ""))  # key absent in light mode
    assert theme._macos_prefers_dark(terminal) is False

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("defaults", 1)

    monkeypatch.setattr(theme.subprocess, "run", boom)
    assert theme._macos_prefers_dark(terminal) is None


@pytest.mark.parametrize(
    ("colorfgbg", "light"),
    [("15;0", False), ("0;15", True), ("0;7", True), ("15;default;0", False), ("0;231", True),
     ("0;16", False), ("junk", None), ("", None)],
)
def test_colorfgbg_parsing(colorfgbg, light) -> None:
    assert theme._background_is_light(colorfgbg) is light


# --- OSC 11 exchange over a pty ------------------------------------------------------

class _FakeTerminal(threading.Thread):
    """Plays the terminal emulator on the master side of a pty."""

    def __init__(self, master: int, replies: list[bytes], *, late: bytes = b"") -> None:
        super().__init__(daemon=True)
        self.master, self.replies, self.late = master, replies, late
        self.received = b""

    def run(self) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and b"\x1b[c" not in self.received:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    self.received += os.read(self.master, 4096)
                except OSError:
                    return
        for reply in self.replies:
            os.write(self.master, reply)
        if self.late:
            time.sleep(0.25)
            os.write(self.master, self.late)


def _query(replies: list[bytes], *, timeout: float = 0.5, late: bytes = b"") -> tuple[Any, _FakeTerminal, int, float]:
    master, slave = pty.openpty()
    terminal = _FakeTerminal(master, replies, late=late)
    terminal.start()
    before = termios.tcgetattr(slave)
    started = time.monotonic()
    try:
        result = theme.query_terminal_background(timeout, in_fd=slave, out_fd=slave)
        elapsed = time.monotonic() - started
        terminal.join(2)
        assert termios.tcgetattr(slave) == before, "terminal modes must be restored"
        return result, terminal, slave, elapsed
    finally:
        pass


def _pending_input(fd: int) -> bytes:
    import fcntl

    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        return os.read(fd, 4096)
    except BlockingIOError:
        return b""
    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (b"\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[?62;c", (255, 255, 255)),
        (b"\x1b]11;rgb:3030/0a0a/2424\x1b\\\x1b[?62;4c", (48, 10, 36)),
        (b"\x1b]11;rgb:ff/00/80\x07\x1b[?1;2c", (255, 0, 128)),
        (b"\x1b]11;#102030\x07\x1b[?6c", (16, 32, 48)),
    ],
)
def test_osc11_exchange_parses_replies_and_leaves_no_input(reply, expected) -> None:
    result, terminal, slave, elapsed = _query([reply])
    assert result == expected
    assert b"\x1b]11;?\x07" in terminal.received and b"\x1b[c" in terminal.received
    assert _pending_input(slave) == b""
    assert elapsed < 0.4


def test_osc11_unsupported_terminal_answers_only_da1_and_returns_quickly() -> None:
    result, _terminal, slave, elapsed = _query([b"\x1b[?62;c"], timeout=2.0)
    assert result is None
    assert elapsed < 1.0  # the DA1 sentinel avoids waiting out the timeout
    assert _pending_input(slave) == b""


def test_osc11_timeout_flushes_late_bytes_and_stays_within_budget() -> None:
    result, terminal, slave, elapsed = _query([], timeout=0.1)
    assert result is None
    assert 0.08 <= elapsed < 0.5
    # A reply arriving after the timeout is the terminal's problem, but whatever
    # arrived before the flush must not stay queued for curses to read.
    assert _pending_input(slave) == b""


def test_osc11_reply_split_across_reads_and_with_noise_before_it() -> None:
    result, _terminal, slave, _elapsed = _query([b"junk\x1b]11;rgb:0000/", b"0000/0000\x07\x1b[?6c"])
    assert result == (0, 0, 0)
    assert _pending_input(slave) == b""


def test_osc11_not_attempted_without_a_terminal() -> None:
    read, write = os.pipe()
    try:
        assert theme.query_terminal_background(0.05, in_fd=read, out_fd=write) is None
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [("rgb:ffff/ffff/ffff", (255, 255, 255)), ("rgba:0000/0000/0000/ffff", (0, 0, 0)), ("#fafafa", (250, 250, 250)),
     ("rgb:f/0/8", (255, 0, 136)), ("hsl:1/2/3", None), ("#12", None), ("", None)],
)
def test_parse_osc11_color(payload, expected) -> None:
    assert theme.parse_osc11_color(payload) == expected


def test_osc_timeout_environment_is_bounded() -> None:
    assert theme._osc_timeout({}) == pytest.approx(0.1)
    assert theme._osc_timeout({"LLM_BRIDGE_OSC_TIMEOUT_MS": "300"}) == pytest.approx(0.3)
    assert theme._osc_timeout({"LLM_BRIDGE_OSC_TIMEOUT_MS": "99999"}) == pytest.approx(1.0)
    assert theme._osc_timeout({"LLM_BRIDGE_OSC_TIMEOUT_MS": "x"}) == pytest.approx(0.1)


# --- 8 / 16 colour mapping -------------------------------------------------------------

@pytest.mark.parametrize("colors", [8, 16])
@pytest.mark.parametrize("polarity", ["dark", "light"])
def test_low_colour_mapping_avoids_unreadable_pairs(colors, polarity) -> None:
    background = (0, 0, 0) if polarity == "dark" else (255, 255, 255)
    resolved = theme.resolve({"LLM_BRIDGE_THEME": polarity})
    for role in theme.ROLES:
        style = theme.fit_style(resolved.style(role), colors, polarity)
        assert style.bg is None
        if style.fg is not None:
            assert 0 <= style.fg < colors
            assert theme.contrast_ratio(theme.xterm_rgb(style.fg), background) >= theme.LOW_COLOR_CONTRAST, (role, style.fg)
    # The two classic traps.
    assert theme.fit_role_color(21, 8, "dark") not in {0, 4}          # blue on black
    assert theme.fit_role_color(226, 16, "light") not in {3, 11}      # yellow on white
    assert theme.fit_role_color(46, 8, "light") not in {2, 3, 6, 7}   # green/cyan on white


def test_low_colour_fitting_details() -> None:
    assert theme.fit_role_color(203, 256, "dark") == 203       # 256 colours: palette untouched
    assert theme.fit_role_color(None, 8, "dark") == 7 and theme.fit_role_color(None, 8, "light") == 0
    assert theme.fit_role_color(39, 0, "dark") == 7
    assert theme.fit_color(None, 8, default=-1) == -1 and theme.fit_color(5, 0) == -1 and theme.fit_color(5, 8) == 5
    # Selection bar falls back to reverse video: a filled background cannot be matched reliably.
    selected = theme.fit_style(theme.parse_sgr("1;38;5;231;48;5;25"), 8, "dark")
    assert selected.reverse and selected.bg is None and selected.fg is None and selected.bold
    # Bold selects the bright (low-contrast) colour on light 8-colour terminals, so it is dropped there.
    assert not theme.fit_style(theme.Style(fg=160, bold=True), 8, "light").bold
    assert theme.fit_style(theme.Style(fg=160, bold=True), 16, "light").bold
    assert theme.fit_style(theme.Style(fg=203, bold=True), 8, "dark").bold
    assert theme.fit_style(theme.Style(bold=True), 8, "dark") == theme.Style(bold=True)
    assert theme.fit_style(theme.Style(fg=203), 256, "dark") == theme.Style(fg=203)


@pytest.mark.parametrize(
    ("env", "colors"),
    [({"TERM": "xterm-256color"}, 256), ({"TERM": "xterm", "COLORTERM": "truecolor"}, 256), ({"TERM": "xterm"}, 8),
     ({"TERM": "xterm-16color"}, 16), ({"TERM": "linux"}, 8), ({"TERM": "dumb"}, 0), ({}, 0), ({"TERM": "weird"}, 256)],
)
def test_terminal_colors_from_environment(env, colors) -> None:
    assert theme.terminal_colors(env) == colors


# --- styles, configuration and overrides --------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1;38;5;203", theme.Style(fg=203, bold=True)),
        ("#ff0000", theme.Style(fg=196)),
        ("#f00 bold underline", theme.Style(fg=196, bold=True, underline=True)),
        ("bold #00ff00 bg:#000000", theme.Style(fg=46, bg=16, bold=True)),
        ("bright-cyan dim", theme.Style(fg=14, dim=True)),
        ("fg:208 on:236 reverse", theme.Style(fg=208, bg=236, reverse=True)),
        ("1 38;5;77", theme.Style(fg=77, bold=True)),
        ("31", theme.Style(fg=1)),
        ("", theme.Style()),
        ("default", theme.Style()),
        ("38;2;255;0;0", theme.Style(fg=196)),
        ({"color": "#ff8800", "bold": True}, theme.Style(fg=208, bold=True)),
        ({"fg": "red", "bg": "236", "underline": True, "dim": False}, theme.Style(fg=1, bg=236, underline=True)),
    ],
)
def test_parse_style_accepts_sgr_hex_names_and_tables(text, expected) -> None:
    assert theme.parse_style(text) == expected
    assert theme.parse_sgr(expected.sgr()) == expected  # the SGR form round-trips


@pytest.mark.parametrize(
    "bad",
    ["#12345", "#gggggg", "notacolour", "99", "38;5", "1;x", "fg:300", {"colour": "red"}, {"bold": "yes"}, {"color": 5}, 5, None, ["red"]],
)
def test_parse_style_rejects_invalid_values(bad) -> None:
    with pytest.raises(ValueError):
        theme.parse_style(bad)


def test_hex_converts_to_nearest_256_index_not_base_slot() -> None:
    assert theme.nearest_256((0, 0, 0)) == 16
    assert theme.nearest_256((255, 255, 255)) == 231
    assert theme.nearest_256((95, 135, 255)) == 69
    assert theme.parse_hex_color("#abc") == (0xAA, 0xBB, 0xCC)


def test_sgr_foreground_compat_and_background_decoding() -> None:
    assert theme.sgr_foreground("1;38;5;203") == (203, True, False, False)
    assert theme.sgr_foreground("garbage") == (None, False, False, False)
    style = theme.parse_sgr("7;2;4;91;104;27;22;24")
    assert style == theme.Style(fg=9, bg=12)
    assert theme.parse_sgr("0;31;1;0") == theme.Style()
    assert theme.parse_sgr("48;5;10;49;39") == theme.Style()
    assert theme.parse_sgr("38;5") == theme.Style()  # non-strict tolerates truncation
    assert theme.Style(fg=9, bg=12, bold=True).sgr() == "1;91;104"
    assert theme.Style(fg=203, bg=16).monochrome() == theme.Style()


def _write_config(tmp_path: Path, body: str) -> dict[str, str]:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return {"LLM_TOOLS_CONFIG": str(path), "TERM": "xterm-256color"}


def test_config_theme_name_and_role_overrides(tmp_path) -> None:
    env = _write_config(tmp_path, '''
[bridge.theme]
name = "light"
[bridge.theme.roles]
state_running = "#00875f bold"
error = "1;38;5;196"
accent = { color = "#5f5faf", bg = "#eeeeee", underline = true }
''')
    resolved = theme.resolve(env)
    assert resolved.name == "light" and "[bridge.theme] name" in resolved.name_source
    assert resolved.style("state_running") == theme.Style(fg=29, bold=True)
    assert resolved.style("error") == theme.Style(fg=196, bold=True)
    assert resolved.style("accent") == theme.Style(fg=61, bg=255, underline=True)
    assert resolved.origins["error"] == "config" and resolved.origins["brand"] == "theme"
    assert resolved.warnings == ()


def test_invalid_config_values_are_ignored_with_warnings(tmp_path) -> None:
    env = _write_config(tmp_path, '''
[bridge.theme]
name = "neon"
background = "purple"
[bridge.theme.roles]
error = "#zzz"
nonsense = "red"
brand = 7
ok = "1;38;5;46"
''')
    resolved = theme.resolve(env)
    assert resolved.polarity == "dark" and resolved.style("ok") == theme.Style(fg=46, bold=True)
    joined = "\n".join(resolved.warnings)
    for fragment in ("'neon' is not a theme", "background must be", "error:", "unknown role 'nonsense'", "brand:"):
        assert fragment in joined
    assert resolved.style("error") == theme.resolve({}).style("error")


def test_config_table_shapes_never_crash(tmp_path) -> None:
    env = _write_config(tmp_path, '[bridge]\ntheme = "dark"\n')
    assert theme.resolve(env).name == "dark"  # malformed table ignored
    env = _write_config(tmp_path, '[bridge.theme]\nname = 3\nroles = "red"\n')
    joined = "\n".join(theme.resolve(env).warnings)
    assert "name must be a string" in joined and "roles must be a table" in joined
    env = _write_config(tmp_path, "this is = not toml [")
    resolved = theme.resolve(env)
    assert any("could not be read" in warning for warning in resolved.warnings)
    assert toolconfig.bridge_theme_config({"bridge": {"theme": {"name": "x"}}}) == {"name": "x"}
    assert toolconfig.bridge_theme_config({"bridge": 3}) == {}
    assert toolconfig.bridge_theme_config({}) == {}


def test_real_environment_reads_the_config_file(tmp_path, monkeypatch) -> None:
    env = _write_config(tmp_path, '[bridge.theme]\nname = "high-contrast"\nbackground = "light"\n')
    _no_probes(monkeypatch)
    monkeypatch.setenv("LLM_TOOLS_CONFIG", env["LLM_TOOLS_CONFIG"])
    for key in ("LLM_BRIDGE_THEME", "COLORFGBG", "NO_COLOR", "LLM_USAGE_NO_COLOR", "LLM_BRIDGE_BACKGROUND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    resolved = theme.resolve()
    assert resolved.name == "high-contrast" and resolved.polarity == "light"
    assert theme.resolve_cached() is theme.resolve_cached()
    assert theme.resolve_theme() == "light"


def test_environment_overrides_beat_config_and_each_other(tmp_path) -> None:
    env = _write_config(tmp_path, '[bridge.theme.roles]\nerror = "#ff0000"\nwarn = "#ffaa00"\n')
    env.update({
        "LLM_TOOLS_COLOR_ERROR": "1;38;5;33",
        "LLM_BRIDGE_COLOR_ERROR": "#00ff00 underline",
        "LLM_TOOLS_COLOR_WARN": "1;34",       # legacy variable still works
        "LLM_TOOLS_COLOR_UNRELATED": "1",     # ralph-only roles stay silent
        "LLM_BRIDGE_COLOR_BOGUS": "red",
        "LLM_BRIDGE_COLOR_OK": "banana",
    })
    resolved = theme.resolve(env)
    assert resolved.style("error") == theme.Style(fg=46, underline=True)
    assert resolved.origins["error"] == "env"
    assert resolved.style("warn") == theme.Style(fg=4, bold=True)
    joined = "\n".join(resolved.warnings)
    assert "LLM_BRIDGE_COLOR_BOGUS" in joined and "LLM_BRIDGE_COLOR_OK" in joined and "UNRELATED" not in joined
    assert theme.role_sgr("error", theme="dark", env={"LLM_TOOLS_COLOR_ERROR": "1;34"}) == "1;34"
    assert theme.role_sgr("brand", env={"LLM_BRIDGE_THEME": "light"}) == "1;38;5;25"


def test_theme_names_and_aliases() -> None:
    assert theme.resolve({"LLM_BRIDGE_THEME": "HC", "LLM_BRIDGE_BACKGROUND": "light"}).name == "high-contrast"
    assert theme.resolve({"LLM_BRIDGE_THEME": "monochrome"}).name == "mono"
    assert theme.resolve({"LLM_BRIDGE_THEME": "auto", "COLORFGBG": "0;15"}).name == "light"
    bad = theme.resolve({"LLM_BRIDGE_THEME": "plaid", "LLM_BRIDGE_BACKGROUND": "plaid"})
    assert bad.name == "dark" and len(bad.warnings) == 2


# --- mono / NO_COLOR -----------------------------------------------------------------------

@pytest.mark.parametrize("env", [{"NO_COLOR": ""}, {"TERM": "dumb"}, {"LLM_USAGE_NO_COLOR": "1"}, {"LLM_BRIDGE_THEME": "mono"}])
def test_mono_has_attributes_only(env) -> None:
    resolved = theme.resolve({**env, "LLM_BRIDGE_COLOR_ERROR": "#ff0000 underline"})
    assert resolved.name == "mono"
    assert all(resolved.style(role).fg is None and resolved.style(role).bg is None for role in theme.ROLES)
    assert resolved.style("error") == theme.Style(underline=True)  # the colour of an override is stripped
    assert resolved.style("selected").reverse and not resolved.style("state_failed").reverse  # reverse is for the selection only
    assert resolved.style("state_review").bold and resolved.style("capacity_empty").bold and resolved.style("state_failed").bold
    assert resolved.style("accent").underline and not resolved.style("accent").bold
    assert resolved.style("brand").bold and not resolved.style("info").bold
    assert [role for role in theme.ROLES if resolved.style(role).reverse] == ["selected"]
    emphasised = {role for role in theme.ROLES if resolved.style(role) != theme.Style()}
    assert len(emphasised) >= 12


def test_forced_theme_preview_overrides_no_color() -> None:
    assert theme.resolve({"NO_COLOR": "1"}, theme_name="light").name == "light"


def test_tmux_markup_follows_theme_overrides_and_disabled_modes() -> None:
    base = {"TERM": "xterm-256color"}
    assert "colour203" in theme.tmux_role_markup("error", env={**base, "LLM_BRIDGE_THEME": "dark"})
    assert "colour160" in theme.tmux_role_markup("error", env={**base, "LLM_BRIDGE_THEME": "light"})
    custom = theme.tmux_role_markup("brand", env={**base, "LLM_BRIDGE_THEME": "dark", "LLM_BRIDGE_COLOR_BRAND": "#ff0000 underline"})
    assert custom == "#[default]#[fg=colour196]#[underscore]"
    assert theme.tmux_role_markup("selected", env={**base, "LLM_BRIDGE_THEME": "light"}).startswith("#[default]#[fg=colour16]#[bg=colour153]#[bold]")
    mono = theme.tmux_role_markup("brand", env={"TERM": "xterm", "NO_COLOR": "1"})
    assert mono == "#[default]#[bold]" and "colour" not in mono
    assert theme.tmux_role_markup("brand", env={"TERM": "dumb"}) == "#[default]"
    assert theme.tmux_style_markup(theme.Style(dim=True, reverse=True)) == "#[default]#[dim]#[reverse]"
    assert theme.tmux_role_markup("brand", env=None).startswith("#[default]")


# --- curses wiring ----------------------------------------------------------------------------

class _FakeCurses:
    def __init__(self, monkeypatch, colors: int = 256, pairs: int = 512, default_colors: bool = True) -> None:
        self.pairs: dict[int, tuple[int, int]] = {}
        curses = bridge_tui.curses
        monkeypatch.setattr(curses, "has_colors", lambda: True)
        monkeypatch.setattr(curses, "start_color", lambda: None)
        monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: self.pairs.__setitem__(pair, (fg, bg)))
        monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
        monkeypatch.setattr(curses, "COLORS", colors, raising=False)
        monkeypatch.setattr(curses, "COLOR_PAIRS", pairs, raising=False)
        if default_colors:
            monkeypatch.setattr(curses, "use_default_colors", lambda: None)
        else:
            def refuse() -> None:
                raise curses.error("no default colours")
            monkeypatch.setattr(curses, "use_default_colors", refuse)


def test_curses_pairs_use_every_role_and_only_the_selection_has_a_background(monkeypatch) -> None:
    fake = _FakeCurses(monkeypatch)
    assert bridge_tui.initialize_colors({"TERM": "xterm-256color", "LLM_BRIDGE_THEME": "light"}, is_tty=True)
    assert len(fake.pairs) == len(theme.ROLES)
    backgrounds = {role for pair, role in enumerate(theme.ROLES, start=1) if fake.pairs[pair][1] != -1}
    assert backgrounds == {"selected"}
    assert fake.pairs[theme.ROLES.index("selected") + 1] == (16, 153)
    assert bridge_tui.role_attr("state_failed") & bridge_tui.curses.A_BOLD
    assert bridge_tui.role_attr("selected") & 0xFF00 == (theme.ROLES.index("selected") + 1) << 8
    assert bridge_tui.role_attr("secondary") == bridge_tui.role_attr("muted")
    assert bridge_tui.role_attr("no-such-role") == 0


def test_curses_eight_colours_use_reverse_selection_and_neutral_without_default_colours(monkeypatch) -> None:
    fake = _FakeCurses(monkeypatch, colors=8, default_colors=False)
    assert bridge_tui.initialize_colors({"TERM": "xterm", "LLM_BRIDGE_THEME": "dark"}, is_tty=True)
    info = fake.pairs[theme.ROLES.index("info") + 1]
    assert info == (bridge_tui.curses.COLOR_WHITE, bridge_tui.curses.COLOR_BLACK)
    assert bridge_tui.role_attr("selected") & bridge_tui.curses.A_REVERSE


def test_curses_pair_exhaustion_and_init_failures_fall_back_to_attributes(monkeypatch) -> None:
    fake = _FakeCurses(monkeypatch, pairs=5)
    assert bridge_tui.initialize_colors({"TERM": "xterm-256color", "LLM_BRIDGE_THEME": "dark"}, is_tty=True)
    assert len(fake.pairs) == 4
    assert bridge_tui.role_attr("brand") != 0 and bridge_tui.role_attr("state_failed") & bridge_tui.curses.A_BOLD
    _FakeCurses(monkeypatch)

    def broken(pair: int, fg: int, bg: int) -> None:
        raise bridge_tui.curses.error("nope")

    monkeypatch.setattr(bridge_tui.curses, "init_pair", broken)
    assert not bridge_tui.initialize_colors({"TERM": "xterm-256color"}, is_tty=True)
    assert bridge_tui.role_attr("state_failed") & bridge_tui.curses.A_BOLD  # mono attributes remain


def test_curses_no_colour_keeps_monochrome_attributes_but_dumb_and_non_tty_get_none(monkeypatch) -> None:
    fake = _FakeCurses(monkeypatch)
    assert not bridge_tui.initialize_colors({"TERM": "xterm", "NO_COLOR": "1"}, is_tty=True)
    assert fake.pairs == {}
    assert bridge_tui.role_attr("brand") & bridge_tui.curses.A_BOLD
    assert bridge_tui.role_attr("selected") & bridge_tui.curses.A_REVERSE
    assert bridge_tui.role_attr("info") == 0
    assert not bridge_tui.initialize_colors({"TERM": "dumb"}, is_tty=True)
    assert bridge_tui.role_attr("brand") == 0
    assert not bridge_tui.initialize_colors({"TERM": "xterm"}, is_tty=False)
    assert bridge_tui.role_attr("brand") == 0
    monkeypatch.setattr(bridge_tui.curses, "has_colors", lambda: False)
    assert not bridge_tui.initialize_colors({"TERM": "xterm"}, is_tty=True)
    assert bridge_tui.role_attr("brand") & bridge_tui.curses.A_BOLD

    def no_start() -> None:
        raise bridge_tui.curses.error("not initialised")

    monkeypatch.setattr(bridge_tui.curses, "has_colors", lambda: True)
    monkeypatch.setattr(bridge_tui.curses, "start_color", no_start)
    assert not bridge_tui.initialize_colors({"TERM": "xterm"}, is_tty=True)
    assert bridge_tui.role_attr("brand") & bridge_tui.curses.A_BOLD


def test_curses_without_env_uses_the_cached_process_theme(monkeypatch) -> None:
    _FakeCurses(monkeypatch)
    _no_probes(monkeypatch)
    monkeypatch.setenv("LLM_BRIDGE_THEME", "light")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("LLM_USAGE_NO_COLOR", raising=False)
    assert bridge_tui.initialize_colors(is_tty=True)
    assert bridge_tui._CURRENT_THEME == "light"


# --- `llm-bridge theme` ---------------------------------------------------------------------------

def _run_theme(monkeypatch, capsys, *argv: str, env: dict[str, str] | None = None) -> str:
    for key in ("COLORFGBG", "LLM_BRIDGE_THEME", "LLM_BRIDGE_BACKGROUND", "NO_COLOR", "LLM_USAGE_NO_COLOR", "TMUX"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LLM_BRIDGE_NO_OSC", "1")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    theme.reset_cache()
    assert bridge.main(["theme", *argv]) == 0
    return capsys.readouterr().out


def test_theme_command_reports_background_theme_and_a_swatch_per_role(monkeypatch, capsys) -> None:
    out = _run_theme(monkeypatch, capsys, env={"COLORFGBG": "0;15"})
    lines = out.splitlines()
    assert lines[0].startswith("Background  light") and "COLORFGBG=0;15" in lines[0]
    assert lines[1].startswith("Theme       light")
    assert lines[2].startswith("Colours     256")
    for role in theme.ROLES:
        assert any(line.split()[:1] == [role] for line in lines), role
    running = next(line for line in lines if line.startswith("  state_running"))
    assert "● RUNNING" in running and "sample text" in running
    assert "\x1b" not in out  # piped output carries no escape codes


def test_theme_command_colours_swatches_when_asked_and_previews_other_themes(monkeypatch, capsys) -> None:
    out = _run_theme(monkeypatch, capsys, "--theme", "high-contrast", "--color", "always", env={"COLORFGBG": "0;15"})
    assert "Theme       high-contrast" in out and "Background  light" in out
    assert "\x1b[1;38;5;88m✕ FAILED" in out and "\x1b[0m" in out
    mono = _run_theme(monkeypatch, capsys, "--theme", "mono", "--color", "always")
    assert "\x1b[1m✕ FAILED" in mono and "38;5" not in mono and "\x1b[1;7m▶ SELECTED" in mono
    low = _run_theme(monkeypatch, capsys, "--colors", "8", "--color", "always", env={"COLORFGBG": "15;0"})
    assert "Colours     8" in low and "38;5" not in low and "\x1b[7m▶ SELECTED" in low.replace("1;7m", "7m")


def test_theme_command_json(monkeypatch, capsys) -> None:
    payload = json.loads(_run_theme(monkeypatch, capsys, "--json", "--colors", "16",
                                    env={"LLM_BRIDGE_THEME": "dark", "LLM_BRIDGE_COLOR_BRAND": "#ff0000"}))
    assert payload["schema_version"] == 1
    assert payload["theme"] == {"name": "dark", "source": "LLM_BRIDGE_THEME=dark"}
    assert payload["background"]["polarity"] == "dark" and payload["background"]["rgb"] is None
    assert payload["colors"] == 16 and payload["color_enabled"] is True
    assert sorted(payload["roles"]) == sorted(theme.ROLES)
    brand = payload["roles"]["brand"]
    assert brand["origin"] == "env" and brand["fg"] == 196 and brand["sgr"] == "38;5;196"
    assert brand["fitted_sgr"] == "91"
    assert payload["roles"]["selected"]["bg"] == 25 and payload["roles"]["selected"]["fitted_sgr"] == "1;7"
    assert payload["roles"]["error"]["origin"] == "theme"


def test_theme_command_reports_overrides_warnings_and_disabled_colour(monkeypatch, capsys) -> None:
    out = _run_theme(monkeypatch, capsys, env={"LLM_BRIDGE_COLOR_ERROR": "#ff0000", "LLM_BRIDGE_COLOR_OK": "bad"})
    assert "Overrides   error (env)" in out
    assert "Warning     LLM_BRIDGE_COLOR_OK" in out
    assert "Note        OSC 11 query skipped: disabled by LLM_BRIDGE_NO_OSC" in out
    out = _run_theme(monkeypatch, capsys, env={"NO_COLOR": "1"})
    assert "Theme       mono" in out and "(colour disabled)" in out


def test_theme_command_is_a_known_subcommand_of_the_shared_parser() -> None:
    parser = bridge._build_parser()
    args = parser.parse_args(["theme", "--theme", "light", "--colors", "8", "--json"])
    assert (args.command, args.theme_name, args.colors, args.json) == ("theme", "light", 8, True)
    with pytest.raises(SystemExit):
        parser.parse_args(["theme", "--theme", "plaid"])


# --- real process under a pty that answers OSC 11 ----------------------------------------------------

def _run_theme_under_pty(env: dict[str, str], reply: bytes | None) -> str:
    for key in ("COLORFGBG", "LLM_BRIDGE_THEME", "LLM_BRIDGE_BACKGROUND", "NO_COLOR", "LLM_USAGE_NO_COLOR",
                "TMUX", "STY", "LLM_BRIDGE_NO_OSC", "TERM_PROGRAM"):
        env.pop(key, None)
    env["TERM"] = "xterm-256color"
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "llm_tools.bridge", "theme"],
        stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
    )
    os.close(slave)
    output = bytearray()
    answered = False
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            if not answered and b"\x1b]11;?\x07" in output and b"\x1b[c" in output:
                answered = True
                if reply is not None:
                    os.write(master, reply)
                os.write(master, b"\x1b[?62;c")
            if process.poll() is not None and not ready:
                break
        process.wait(timeout=scaled(5))
        assert process.returncode == 0, bytes(output)
        assert answered, "the process never queried the terminal background"
        return output.decode("utf-8", "replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def test_theme_command_under_a_pty_answering_osc11_with_white_chooses_light(env) -> None:
    out = _run_theme_under_pty(env, b"\x1b]11;rgb:ffff/ffff/ffff\x07")
    assert "Background  light" in out and "OSC 11 terminal query (rgb 255,255,255)" in out
    assert "Theme       light" in out


def test_theme_command_under_a_pty_answering_osc11_with_aubergine_chooses_dark(env) -> None:
    out = _run_theme_under_pty(env, b"\x1b]11;rgb:3030/0a0a/2424\x1b\\")
    assert "Background  dark" in out and "OSC 11 terminal query (rgb 48,10,36)" in out


def test_theme_command_under_a_pty_without_osc11_support_falls_back_to_dark(env) -> None:
    out = _run_theme_under_pty(env, None)
    assert "Background  dark" in out and "default (no background signal found)" in out
    assert "OSC 11 query got no answer" in out
