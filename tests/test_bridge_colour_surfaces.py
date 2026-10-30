"""Colour guide on the other surfaces: `mission colour`, the mission picker, the home screen and the tmux station."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_tools import bridge_glyphs as glyphs, bridge_launcher as launcher, bridge_switcher as sw, bridge_theme as theme, bridge_tui
from llm_tools.bridge_launcher import Launcher
from llm_tools.terminal import STATUS_CONTEXT_FORMAT, TmuxTerminalBackend
from tests.test_bridge_launcher_home import Grid, mission
from tests.test_bridge_launcher_overview import ascii_locale
from tests.conftest import scaled


# --- `llm-bridge mission colour` ---------------------------------------------------------------------------

def cli(env, state: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[1]
    return subprocess.run([sys.executable, str(root / "llm-bridge"), *args], env={**env, "LLM_BRIDGE_STATE_DIR": str(state)},
                          cwd=cwd or root, text=True, capture_output=True, timeout=scaled(60))


@pytest.fixture()
def made(env, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    created = cli(env, state, "mission", "create", "--name", "demo", "--prompt", "work", "--providers", "codex", "--json", cwd=tmp_path)
    assert created.returncode == 0, created.stderr
    mission_id = json.loads(created.stdout)["mission"]["id"]
    return env, state, mission_id, tmp_path


def test_mission_colour_pins_clears_and_records_an_event(made) -> None:
    env, state, mission_id, work = made
    automatic = theme.mission_number(mission_id)
    pinned = cli(env, state, "mission", "colour", "--mission", mission_id[:8], "3")
    assert pinned.returncode == 0 and pinned.stdout.strip() == f'Mission {mission_id[:8]} "demo" colour: 3 (blue, pinned)'
    as_json = json.loads(cli(env, state, "mission", "color", "--mission", mission_id, "7", "--json").stdout)
    assert as_json == {"schema_version": 1, "mission_id": mission_id, "name": "demo", "colour": 7, "pinned": True,
                       "role": "mission_7", "number": 7, "hue": "pink"}
    cleared = json.loads(cli(env, state, "mission", "colour", "--mission", mission_id, "auto", "--json").stdout)
    assert cleared["colour"] is None and cleared["pinned"] is False and cleared["number"] == automatic
    assert f"colour: {automatic} (" in cli(env, state, "mission", "colour", "--mission", mission_id, "auto").stdout
    kinds = [event["kind"] for event in json.loads(cli(env, state, "events", "--mission", mission_id, "--json").stdout)["events"]]
    assert kinds.count("mission-colour") == 4
    snapshot = json.loads(cli(env, state, "status", "--mission", mission_id, "--json").stdout)
    assert snapshot["mission"]["config"]["colour"] is None


def test_mission_colour_rejects_bad_values_and_unknown_missions(made) -> None:
    env, state, mission_id, _work = made
    for bad in ("0", "9", "red", "-1"):
        result = cli(env, state, "mission", "colour", "--mission", mission_id, bad)
        assert result.returncode != 0 and ("colour must be a number from 1 to 8" in result.stderr or "usage" in result.stderr.lower()), bad
    missing = cli(env, state, "mission", "colour", "--mission", "ffffffff", "2")
    assert missing.returncode != 0 and "no mission matches" in missing.stderr


def test_mission_colour_defaults_to_the_mission_of_the_folder(made) -> None:
    env, state, mission_id, work = made
    here = cli(env, state, "mission", "colour", "4", cwd=work)
    assert here.returncode == 0 and "colour: 4 (indigo, pinned)" in here.stdout


def test_console_activity_describes_a_colour_change() -> None:
    event = {"kind": "mission-colour", "created_at": "2033-05-18T03:00:00+00:00", "data": {"key": "colour", "value": 3}}
    assert bridge_tui._humanize_event(event) == ("Mission colour set to 3 (blue)", False)
    event["data"] = {"key": "colour", "value": None}
    assert bridge_tui._humanize_event(event) == ("Mission colour back to automatic", False)


def test_pinned_colour_reaches_the_console_snapshot() -> None:
    snapshot = bridge_tui._bounded_snapshot({"mission": {"id": "ab" * 16, "name": "n", "status": "RUNNING", "config": {"colour": 5}}})
    assert snapshot["mission"]["role"] == "mission_5"  # type: ignore[index]
    assert bridge_tui._mission_role_of({"mission": {"id": "ab" * 16}}) == theme.mission_role("ab" * 16)


# --- mission picker ---------------------------------------------------------------------------------------

def picker_entries() -> list[sw.MissionEntry]:
    return [
        sw.MissionEntry("a" * 32, "Alpha", "RUNNING", "/work/alpha", provider="codex", station=True, controller=True, task_id=3, task_status="RUNNING", task_title="Ship it", colour=2),
        sw.MissionEntry("0000000b" + "0" * 24, "Beta", "COMPLETED", "/work/beta", provider="claude-sonnet", updated_at="2026-01-01"),
        sw.MissionEntry("c" * 32, "Gamma", "PAUSED", "/work/gamma", provider="kilo"),
    ]


def test_picker_rows_use_chip_state_square_and_muted_facts() -> None:
    picker = sw.MissionPicker(picker_entries(), current="a" * 32, selected=2)
    lines = picker.segment_lines(24, 100)
    by_name = {next(text.strip() for text, _role in segments if text.strip() in {"Alpha", "Beta", "Gamma"}): segments for segments, _style in lines
               if any(text.strip() in {"Alpha", "Beta", "Gamma"} for text, _role in segments)}
    alpha = dict((text.strip(), role) for text, role in by_name["Alpha"])
    assert alpha["▌"] == "mission_2" and alpha["▶"] == "accent"  # pinned colour; "this mission" marker
    assert alpha["aaaaaaaa"] == "muted" and alpha["● RUNNING"] == "state_running"
    assert alpha["■"] == "provider_codex" and alpha["codex"] == "info" and alpha["/work/alpha"] == "muted"
    beta = dict((text.strip(), role) for text, role in by_name["Beta"])
    assert beta["✓ COMPLETED"] == "state_done" and beta["Beta"] == "muted" and beta["▌"] == theme.mission_role("0000000b" + "0" * 24)
    assert beta["■"] == "provider_claude"
    selected = [segments for segments, style in lines if style == "sel"]
    assert len(selected) == 1 and [role for _text, role in selected[0]] == ["selected"]  # one bar, no other fills
    head = lines[0][0]
    assert dict((text.strip(), role) for text, role in head)["Missions"] == "heading"
    prompt = lines[1][0]
    assert prompt[0] == ("Filter: ", "accent")
    footer = dict((text.strip(), role) for text, role in lines[-1][0])
    assert footer["Enter"] == "key" and footer["Esc"] == "key" and footer["switch"] == "muted"


def test_picker_text_matches_its_segments_and_is_ascii_without_utf8() -> None:
    picker = sw.MissionPicker(picker_entries(), current="a" * 32, unicode=False)
    for (segments, _style), (text, _same) in zip(picker.segment_lines(24, 90), picker.lines(24, 90)):
        assert "".join(part for part, _role in segments) == text
        text.encode("ascii")
    text = "\n".join(t for t, _ in picker.lines(24, 90))
    assert "* RUNNING" in text and "# " in text and "|" in text and "@" in text


def test_picker_json_carries_the_identity_role() -> None:
    payload = sw.missions_json(picker_entries())["missions"]
    assert payload[0]["colour"] == "mission_2" and payload[1]["colour"] == theme.mission_role("0000000b" + "0" * 24)


# --- home screen ------------------------------------------------------------------------------------------

def home_rows(tmp_path, *, probe=lambda ident: True, **kw) -> tuple[Grid, dict]:
    here = str(tmp_path)
    items = launcher.order_missions([
        mission("Live one", "RUNNING", "/srv/one", ident="1" * 16), mission("Done one", "COMPLETED", here, ident="2" * 16),
    ], here)
    items[0]["config"]["colour"] = 6
    state = Launcher(here, missions=items, installed=("codex",), probe=probe, selected=0, **kw)
    return state, items


def test_home_rows_pin_the_chip_and_move_the_here_marker_to_the_folder(tmp_path, monkeypatch) -> None:
    seen: dict[tuple[int, str], str] = {}
    roles: dict[str, str] = {}
    monkeypatch.setattr(bridge_tui, "role_attr", lambda role: roles.setdefault(role, len(roles) + 1))

    class Recording(Grid):
        def addnstr(self, y, x, text, count, *attrs):
            seen[(y, text.strip())] = attrs[0]
            super().addnstr(y, x, text, count, *attrs)

    state, items = home_rows(tmp_path)
    grid = Recording()
    launcher._draw(grid, state)
    inverse = {value: role for role, value in roles.items()}
    row = {text: inverse[attr] for (y, text), attr in seen.items() if y == 6}
    assert row["▌"] == "mission_6" and row["● RUNNING"] == "state_running" and row["Live one"] == "info"
    finished = {text: inverse[attr] for (y, text), attr in seen.items() if y == 7}
    assert finished["✓ COMPLETED"] == "state_done" and finished["Done one"] == "muted" and finished["▌"] == theme.mission_role("2" * 16)
    assert finished["●"] == "accent"  # the current folder's marker, in the folder column
    assert grid.row(7).count("●") == 1 and "● /" in grid.row(7)


def test_home_footer_names_keys_in_the_key_role() -> None:
    segments = launcher.footer_segments("↑↓ select · Enter open · N new mission · ? help")
    assert ("↑↓", "key") in segments and (" select", "muted") in segments and ("Enter", "key") in segments and (" · ", "dim") in segments


def test_home_screen_hides_disabled_providers_and_says_so(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "codex,claude")
    assert launcher.disabled_launch_clis() == frozenset(launcher.PROVIDER_ORDER) - {"codex", "claude"}
    monkeypatch.delenv("LLM_USAGE_PROVIDERS")
    monkeypatch.setattr("llm_tools.config.enabled_providers", lambda env=None: None)
    assert launcher.disabled_launch_clis() == frozenset()
    state = Launcher(str(tmp_path), creating=True, installed=("codex",), disabled=("claude", "kilo"), providers="codex")
    assert state.provider_note() == "(copilot, opencode, minimax not installed; claude, kilo disabled in config)"
    state.disabled = ()
    assert state.provider_note() == "(claude, copilot, kilo, opencode, minimax not installed)"
    state.disabled = ("claude",)
    state.providers = "claude"
    state.directory = str(tmp_path)
    state.objective = "do it"
    with pytest.raises(ValueError, match="claude is disabled by"):
        state.validate()


def test_disabled_launch_clis_survives_a_broken_config(monkeypatch) -> None:
    def broken(env=None):
        raise SystemExit(2)

    monkeypatch.setattr("llm_tools.config.enabled_providers", broken)
    assert launcher.disabled_launch_clis() == frozenset()


def test_theme_hint_is_shown_until_the_first_settled_key(tmp_path, monkeypatch) -> None:
    state = Launcher(str(tmp_path), installed=("codex",), theme_hint="theme: dark (guessed) · set LLM_BRIDGE_THEME=light|dark")
    grid = Grid()
    launcher._draw(grid, state)
    assert any(grid.row(y).startswith("theme: dark (guessed)") for y in range(34))
    state.key("x")  # inside the start-up grace period: terminal replies must not dismiss the note
    assert state.theme_hint
    state.theme_hint_since -= 5
    state.key("y")
    assert state.theme_hint == ""
    monkeypatch.setattr("llm_tools.bridge_theme.resolve_cached", lambda: theme.resolve({"COLORFGBG": "0;15"}))
    assert launcher.theme_guess_hint() == ""
    monkeypatch.setattr("llm_tools.bridge_theme.resolve_cached", lambda: theme.resolve({}))
    assert launcher.theme_guess_hint() == "theme: dark (guessed) · set LLM_BRIDGE_THEME=light|dark"


# --- one glyph table ----------------------------------------------------------------------------------------

def test_every_ui_glyph_has_an_ascii_form(monkeypatch) -> None:
    expected = {"sep": "|", "prompt": ">", "updown": "Up/Down", "dash": "-", "hbar": "-", "vbar": "|", "running": "*", "here": "*",
                "waiting": "o", "review": "#", "done": "v", "failed": "x", "paused": "=", "square": "#", "chip": "|", "ellipsis": "...",
                "cursor": ">", "play": ">", "times": "x", "newline": "\\n"}
    for name, plain in expected.items():
        assert glyphs.GLYPHS[name][1] == plain, name
    ascii_locale(monkeypatch)
    text = "· › ↑↓ ←/→ — │ ─ ▸ ● ○ ◆ ✓ ✕ ‖ ■ ▌ ↵ … × ▶ █░ ⠋"
    converted = glyphs.localize(text)
    converted.encode("ascii")
    assert glyphs.spinner_frames() == "-\\|/" and glyphs.localize("⠹ working") == "- working"
    assert glyphs.state_text("running") == "* RUNNING" and glyphs.state_text("running", unicode=True) == "● RUNNING"
    assert glyphs.state_text("odd") == "ODD" and glyphs.provider_role("Claude-Sonnet") == "provider_claude"


# --- tmux station ---------------------------------------------------------------------------------------------

def test_status_chip_and_styled_state_are_sanitized_and_use_theme_markup(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    backend = TmuxTerminalBackend()
    monkeypatch.setattr(backend, "_require_owned", lambda handle: None)
    monkeypatch.setattr(backend, "_run", lambda _socket, *args, **_kw: calls.append(args) or subprocess.CompletedProcess([], 0))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LLM_BRIDGE_THEME", "dark")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("LLM_USAGE_NO_COLOR", raising=False)
    theme.reset_cache()
    handle = type("Handle", (), {"socket_path": Path("/x")})()
    backend.set_status_chip(handle, "mission_3")
    assert calls[-1] == ("set-option", "-g", "@llm_bridge_chip", theme.tmux_style_markup(theme.mission_chip_style(3, theme.resolve_cached())))
    assert "bg=colour33" in calls[-1][-1] and "fg=colour16" in calls[-1][-1]
    backend.set_status_styled(handle, [("Task 6 ", "info"), ("● RUNNING #[fg=red]", "state_running"), (" · ", "dim"), ("‖ PAUSED", "state_paused")])
    value = calls[-1][-1]
    assert calls[-1][:3] == ("set-option", "-g", "@llm_bridge_context_styled")
    assert "●" in value and "RUNNING" in value and "#[fg=red]" not in value and "#{" not in value
    assert theme.tmux_role_markup("state_running") + "● RUNNING" in value and value.endswith("#[default]")
    backend.set_status_styled(handle, [])
    assert calls[-1][-1] == ""
    theme.reset_cache()


def test_status_row_format_prefers_the_styled_state_and_wraps_the_label_in_the_chip() -> None:
    assert "@llm_bridge_chip" in STATUS_CONTEXT_FORMAT and "@llm_bridge_context_styled" in STATUS_CONTEXT_FORMAT
    assert "#{@llm_bridge_chip} #{=/" in STATUS_CONTEXT_FORMAT and "@llm_bridge_label} #[default] " in STATUS_CONTEXT_FORMAT


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_real_station_draws_the_label_as_a_filled_chip_and_colours_the_state(tmp_path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=[sys.executable, "-c", "import time; time.sleep(30)"], bridge_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path, socket_path=tmp_path / "chip.sock", session_name="chip", size=(120, 30),
    )
    outer = tmp_path / "outer.sock"
    base = ["tmux", "-S", str(outer), "-f", "/dev/null"]
    try:
        backend.set_status_label(handle, "alpha-repo")
        backend.set_status_context(handle, "Task 6 RUNNING")
        backend.set_status_chip(handle, "mission_2")
        backend.set_status_styled(handle, [("Task 6 ", "info"), ("● RUNNING", "state_running")])
        subprocess.run([*base, "new-session", "-d", "-x", "120", "-y", "30",
                        f"env TERM=xterm-256color tmux -u -S {handle.socket_path} attach -t {handle.session_name}"], check=True)
        deadline = time.monotonic() + 8
        plain = coloured = ""
        while time.monotonic() < deadline:
            plain = subprocess.run([*base, "capture-pane", "-p", "-t", "0"], text=True, capture_output=True).stdout.rstrip("\n")
            if "Provider: Ctrl-] then P" in plain and "alpha-repo" in plain:
                break
            time.sleep(0.2)
        row = plain.splitlines()[-1]
        assert "alpha-repo" in row and "Task 6 ● RUNNING" in row and "Bridge: Ctrl-] then B" in row
        coloured = subprocess.run([*base, "capture-pane", "-p", "-e", "-t", "0"], text=True, capture_output=True).stdout.rstrip("\n").splitlines()[-1]
        chip = theme.mission_chip_style(2, theme.resolve_cached())
        assert f"48;5;{chip.bg}" in coloured and f"38;5;{chip.fg}" in coloured  # the label sits on the mission's colour
        state_colour = theme.resolve_cached().style("state_running").fg
        assert f"38;5;{state_colour}" in coloured  # the state word is drawn in its state role
    finally:
        subprocess.run([*base, "kill-server"], check=False, capture_output=True)
        backend.stop(handle)
