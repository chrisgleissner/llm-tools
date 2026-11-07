"""New-mission form: sign-in and capacity annotations, direct Start key, hints; doctor verdict and provider list.

CLIUX-055 (capacity-aware Start row), CLIUX-066 (doctor verdict and exit code), CLIUX-031 (annotations, F5,
subtitle), CLIUX-067 (Objective hint), CLIUX-068 (empty state).
"""
from __future__ import annotations

import curses
import json
import time
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_doctor, bridge_launcher as launcher, bridge_missions, bridge_tui as tui
from llm_tools.bridge import main
from llm_tools.bridge_launcher import Launcher

from tests import test_bridge_launcher_home as home

Grid = home.Grid


@pytest.fixture(autouse=True)
def _terminal(monkeypatch):
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(tui, "initialize_colors", lambda: False)


def form(tmp_path: Path, **changes) -> Launcher:
    state = Launcher(str(tmp_path), creating=True, focus=1, installed=("codex", "claude", "copilot"),
                     providers="copilot", objective="x", **changes)
    return state


def hint(state: Launcher, focus: int, width: int = 200) -> str:
    state.focus = focus
    return launcher.field_hint(state, width)


# --- CLIUX-055: capacity on the Start row -----------------------------------------------------

def test_start_row_says_when_the_only_provider_has_no_quota(tmp_path):
    reset = time.time() + 24 * 86400 + 3600
    state = form(tmp_path)
    state.apply_capacity({"copilot": launcher.CapacityNote("rate-limited", reset)})
    day = time.strftime("%b", time.localtime(reset)) + " " + str(time.localtime(reset).tm_mday)
    assert hint(state, 5) == f"copilot has no quota until {day} · Start waits for it"
    assert state.annotation("copilot") == ("(quota empty · resets in 24d)", "warn")


def test_start_row_names_the_first_provider_that_has_capacity(tmp_path):
    state = form(tmp_path)
    state.providers = "copilot,codex"
    state.apply_capacity({"copilot": launcher.CapacityNote("rate-limited", time.time() + 5 * 86400)})
    text = hint(state, 5)
    assert text.startswith("Starts codex now · uses provider capacity · copilot has no quota until ")
    state.exhausted = {}
    assert hint(state, 5) == "Starts copilot now · uses provider capacity"


def test_start_row_without_a_reset_time_and_within_a_day(tmp_path):
    state = form(tmp_path)
    state.apply_capacity({"copilot": launcher.CapacityNote("insufficient-balance", None)})
    assert hint(state, 5) == "copilot has no quota left · Start waits for it"
    soon = time.time() + 3 * 3600
    state.apply_capacity({"copilot": launcher.CapacityNote("rate-limited", soon)})
    assert hint(state, 5) == f"copilot has no quota until {time.strftime('%H:%M', time.localtime(soon))} · Start waits for it"


def test_an_automatic_pick_moves_to_a_provider_with_capacity(tmp_path):
    state = Launcher(str(tmp_path), installed=("codex", "claude", "copilot"), providers="claude", auto_providers=True,
                     signins={"claude": (True, ""), "codex": (True, ""), "copilot": (True, "")})
    state.apply_capacity({"claude": launcher.CapacityNote("rate-limited", time.time() + 86400 * 3)})
    assert state.providers == "codex"
    assert state.annotation("claude") is not None


def test_an_operator_choice_or_configured_rotation_is_never_replaced(tmp_path):
    exhausted = {"claude": launcher.CapacityNote("rate-limited", time.time() + 86400 * 3)}
    configured = Launcher(str(tmp_path), installed=("codex", "claude"), providers="claude,codex", auto_providers=False)
    configured.apply_capacity(exhausted)
    assert configured.providers == "claude,codex"
    touched = Launcher(str(tmp_path), installed=("codex", "claude"), providers="claude", auto_providers=True)
    touched.toggle_provider("codex")
    touched.toggle_provider("codex")
    touched.apply_capacity(exhausted)
    assert touched.providers == "claude"
    only = Launcher(str(tmp_path), installed=("claude",), providers="claude", auto_providers=True)
    only.apply_capacity(exhausted)
    assert only.providers == "claude"  # nothing better to pick


def test_read_capacity_reports_only_exhausted_providers(monkeypatch):
    now = int(time.time())
    usage = {
        "copilot": {"available": True, "monthly": {"used": 100.0, "remaining": 0.0, "resets_at": now + 86400 * 24}},
        "codex": {"available": True, "five_hour": {"used": 10.0, "remaining": 90.0, "resets_at": now + 3600}},
    }
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", json.dumps(usage))
    found = launcher.read_capacity(("codex", "copilot", "claude"))
    assert set(found) == {"copilot"}
    assert found["copilot"].resets_at is not None and found["copilot"].resets_at - now > 86400 * 23


def test_capacity_reader_hands_the_result_over_once():
    calls = []
    reader = launcher.CapacityReader(("a",), read=lambda names: calls.append(names) or {"a": launcher.CapacityNote("rate-limited")})
    assert reader.poll() is None and not reader.pending
    reader.start()
    reader.start()  # a second start does nothing
    deadline = time.time() + 5
    result = None
    while result is None and time.time() < deadline:
        result = reader.poll()
    assert result == {"a": launcher.CapacityNote("rate-limited")} and calls == [("a",)]
    assert reader.poll() is None and not reader.pending


def test_capacity_reader_survives_a_failing_read():
    def boom(_names):
        raise RuntimeError("no network")

    reader = launcher.CapacityReader(("a",), read=boom)
    reader.start()
    deadline = time.time() + 5
    result = None
    while result is None and time.time() < deadline:
        result = reader.poll()
    assert result == {}


# --- CLIUX-031: annotations, direct Start key, hints --------------------------------------------

def test_providers_are_annotated_with_their_sign_in_state(tmp_path):
    state = form(tmp_path, signins={"codex": (False, "codex login"), "claude": (True, ""), "copilot": (None, "")})
    assert state.annotation("codex") == ("(not signed in: codex login)", "dim")
    assert state.annotation("claude") is None and state.annotation("copilot") is None
    grid = Grid(size=(24, 100))
    launcher._draw(grid, state)
    rows = [grid.row(index) for index in range(24)]
    assert any(row.strip().startswith("[ ] codex") and "(not signed in: codex login)" in row for row in rows)
    assert not any("claude" in row and "not signed in" in row for row in rows)
    assert state.vertical is True  # the one-line picker has no room for annotations


def test_selected_unsigned_provider_gets_a_specific_hint(tmp_path):
    state = form(tmp_path, signins={"codex": (False, "codex login")})
    state.providers = "codex"
    state.provider_cursor = 0
    assert hint(state, 1) == "codex not signed in: codex login · Space removes it from the rotation"
    state.providers = "copilot"
    state.provider_cursor = 0
    assert hint(state, 1) == "↑/↓ choose · Space adds to the rotation; [1] runs first · Tab next field"


def test_provider_hint_explains_the_numbers(tmp_path):
    state = Launcher(str(tmp_path), creating=True, installed=("codex",), providers="codex")
    assert "numbers show rotation order" not in hint(state, 1)
    assert hint(state, 1) == "←/→ choose · Space adds to the rotation; [1] runs first"


def test_f5_starts_from_any_field(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "provider_installed", lambda name: True)
    for focus in range(6):
        state = Launcher(str(tmp_path), creating=True, installed=("codex",), providers="codex", objective="do it", focus=focus)
        assert state.key(curses.KEY_F5) == "start"


def test_f5_reports_what_is_missing_instead_of_starting(tmp_path):
    state = Launcher(str(tmp_path), creating=True, installed=("codex",), providers="codex", objective="  ")
    assert state.key(curses.KEY_F5) is None
    assert state.error == "Enter a mission objective."


def test_form_footer_lists_the_start_key(tmp_path):
    state = form(tmp_path)
    for focus in (0, 1, 3, 4):
        state.focus = focus
        assert "F5 start" in launcher.form_footer(state)
    state.focus = 5
    assert "F5" not in launcher.form_footer(state)  # Enter already starts on this row


def test_subtitle_fits_80_columns_and_lists_only_installed(monkeypatch):
    installed = ("codex", "claude", "copilot", "kilo", "opencode")
    full = launcher.subtitle(installed)
    assert full.endswith("switch as limits run out") and len(full) > 79
    short = launcher.subtitle(installed, 80)
    assert short == "Run Codex, Claude Code, Copilot, Kilo and OpenCode on your tasks"
    assert launcher.subtitle(installed, 100) == full
    assert launcher.subtitle(("copilot",), 80) == "Run Copilot on your tasks"
    grid = Grid(size=(24, 80))
    state = Launcher("/w", installed=installed)
    launcher._draw(grid, state)
    assert grid.row(1) == short


# --- CLIUX-067: the Objective hint matches the keys ---------------------------------------------

def test_objective_hint_names_the_keys_that_work(tmp_path):
    state = Launcher(str(tmp_path), creating=True, installed=("codex",), providers="codex")
    text = hint(state, 0, 80)
    assert text == "Type or paste the task · F5 start now · Tab more options · Ctrl-E full editor"
    assert "moves on to Start" not in text and len(text) < 79
    state.focus = 0
    state.key("\t")
    assert state.focus == 1  # Tab goes to the Providers field, as the hint says ("more options")


# --- CLIUX-068: the empty state --------------------------------------------------------------------

def test_empty_state_names_only_installed_providers_and_keeps_the_key_phrase_together():
    lines = launcher.empty_state_lines(("copilot",), 80)
    text = " ".join(lines)
    assert "Copilot" in text and "Codex" not in text and "Claude" not in text
    narrow = launcher.empty_state_lines(("codex", "claude"), 60)
    assert any(line.endswith("N to create one") for line in narrow)
    assert launcher.empty_state_lines((), 80)[0].startswith("A mission is a goal plus ordered tasks run by your AI coding CLI")
    for width in range(30, 100):
        for line in launcher.empty_state_lines(("copilot",), width):
            assert not line.endswith(" N") and not line.endswith(" N to") and not line.endswith("·")


def test_empty_home_screen_draws_the_installed_provider_sentence(tmp_path):
    grid = Grid(size=(24, 80))
    state = Launcher(str(tmp_path), installed=("copilot",))
    launcher._draw(grid, state)
    text = " ".join(grid.row(index).strip() for index in range(5, 12))
    assert "run by Copilot in this folder" in text and "Codex" not in text


# --- CLIUX-066 / CLIUX-055: doctor ---------------------------------------------------------------------

def _install(directory: Path, *names: str) -> None:
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).write_text("#!/bin/sh\n")
        (directory / name).chmod(0o755)


@pytest.fixture()
def doctor_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _doctor(capsys, *argv: str) -> tuple[int, str]:
    code = main(["doctor", *argv])
    return code, capsys.readouterr().out


def test_doctor_lists_every_provider_the_form_offers(doctor_env, capsys):
    _install(doctor_env / "bin", "copilot", "tmux")
    code, out = _doctor(capsys, "--json")
    report = json.loads(out)
    assert [item["name"] for item in report["providers"]] == list(launcher.PROVIDER_ORDER)
    copilot = next(item for item in report["providers"] if item["name"] == "copilot")
    assert copilot["installed"] is True and copilot["logged_in"] is False
    assert {name for name, _cli in launcher.offered_entries()} >= set(launcher.detect_installed())
    assert code == 1


def test_doctor_exits_1_when_no_provider_can_run(doctor_env, capsys):
    _install(doctor_env / "bin", "tmux")
    code, out = _doctor(capsys, "--json")
    report = json.loads(out)
    assert code == 1
    assert report["ready"] is False and report["blocking"]
    assert "copilot: not on PATH" in report["blocking"]
    code, text = _doctor(capsys)
    assert code == 1
    assert "install one of: codex, claude, copilot, kilo, opencode, minimax" in text
    assert "rotation --set" not in text  # without a mission there is no rotation to edit
    assert "copilot: not on PATH · install its CLI to use it" in text


def test_doctor_names_the_sign_in_step_when_a_provider_is_installed(doctor_env, capsys):
    _install(doctor_env / "bin", "copilot", "tmux")
    code, text = _doctor(capsys)
    assert code == 1
    assert "Sign in to a provider first: copilot" in text


def test_doctor_exits_0_when_a_provider_is_ready(doctor_env, capsys, monkeypatch):
    _install(doctor_env / "bin", "claude", "tmux")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    code, out = _doctor(capsys, "--json")
    report = json.loads(out)
    assert code == 0 and report["ready"] is True and report["blocking"] == []
    code, text = _doctor(capsys)
    assert code == 0 and "Ready. In your project folder run llm-bridge" in text


def test_doctor_without_tmux_is_still_ready_for_headless_work(doctor_env, capsys, monkeypatch):
    _install(doctor_env / "bin", "claude")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    code, out = _doctor(capsys, "--json")
    assert code == 0 and json.loads(out)["tmux_available"] is False


def test_readiness_verdict_unit():
    ok = {"name": "claude", "installed": True, "logged_in": True}
    signed_out = {"name": "codex", "installed": True, "logged_in": False}
    missing = {"name": "kilo", "installed": False, "logged_in": None}
    assert bridge_doctor.readiness([ok, missing]) == {"ready": True, "blocking": []}
    assert bridge_doctor.readiness([signed_out, missing]) == {"ready": False, "blocking": ["codex: not signed in", "kilo: not on PATH"]}
    assert bridge_doctor.readiness([]) == {"ready": False, "blocking": ["no provider CLI is installed and signed in"]}
    broken = bridge_doctor.readiness([ok], integrity="database disk image is malformed")
    assert broken["ready"] is False and "integrity" in broken["blocking"][0]


def test_signin_state_is_offline_and_independent_of_path(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert bridge_doctor.signin_state("codex", env) == (False, "codex login")
    assert bridge_doctor.signin_state("claude", env) == (False, "claude, then /login")
    assert bridge_doctor.signin_state("claude", {**env, "CLAUDE_CODE_OAUTH_TOKEN": "t"}) == (True, "")
    assert bridge_doctor.signin_state("minimax", env) == (None, "")
