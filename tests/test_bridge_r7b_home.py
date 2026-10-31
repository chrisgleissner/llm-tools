"""Home screen fixes from the first-time-user critique: Save before Start, rotation order, ID column, attach, saved note."""

from __future__ import annotations

import curses

import pytest

from llm_tools import bridge, bridge_launcher as launcher, bridge_tui
from llm_tools.bridge_launcher import Launcher

from .test_bridge_launcher_home import Grid, Recorder, mission, prepare, run_with
from .test_bridge_tui_experience import make_state


@pytest.fixture(autouse=True)
def _terminal(monkeypatch):
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(bridge_tui, "initialize_colors", lambda: False)
    monkeypatch.setattr(launcher, "provider_installed", lambda name: True)


def draw(state, size=(34, 120)) -> Grid:
    grid = Grid(size=size)
    launcher._draw(grid, state)
    return grid


# --- 13: Save before Start, and Enter never reaches Start ------------------------------------------

def test_form_lists_save_before_start_and_start_row_says_what_it_costs(tmp_path) -> None:
    state = Launcher(str(tmp_path), creating=True, installed=("codex", "claude"), providers="codex", objective="x", focus=5)
    grid = draw(state)
    rows = grid.text().splitlines()
    save = next(index for index, row in enumerate(rows) if "Save mission only" in row)
    start = next(index for index, row in enumerate(rows) if "Start and open provider" in row)
    assert save < start and rows[start].startswith("> ")
    assert "Starts codex now · uses provider capacity" in grid.text()
    state.providers = "claude,codex"
    # The hermetic HOME has no Claude state file, so the one-time folder trust prompt is announced.
    assert launcher.field_hint(state) == "Starts claude now · uses provider capacity · Claude asks once to trust this folder: choose Yes"
    state.focus = 4
    assert launcher.field_hint(state) == "Enter to save · no provider is started"


def test_repeated_enter_from_the_objective_stops_at_save_and_never_starts(tmp_path) -> None:
    state = Launcher(str(tmp_path), creating=True, directory=str(tmp_path), installed=("codex",), providers="codex", objective="Do it")
    actions = []
    focuses = []
    for _ in range(12):
        action = state.key("\n")
        focuses.append(state.focus)
        actions.append(action)
        if action:
            break
    assert actions[-1] == "create" and "start" not in actions
    assert max(focuses) == 4  # Save; the Start row was never focused
    assert state.focus == 4
    # Start needs an explicit Tab (or Down) and then Enter.
    state.key("\t")
    assert state.focus == 5 and state.key("\n") == "start"


# --- 14: rotation order and subtitle -----------------------------------------------------------------

def test_checklist_shows_rotation_order_first_then_unselected_alphabetically(tmp_path) -> None:
    routes = (launcher.RouteChoice("kilo-glm", "kilo", "zai/glm-5.2"), launcher.RouteChoice("claude-sonnet", "claude", "sonnet"))
    state = Launcher(str(tmp_path), creating=True, installed=("codex", "claude", "copilot", "kilo"), routes=routes,
                     providers="kilo-glm,codex", focus=1)
    assert [name for name, _ in state.choices()] == ["kilo-glm", "codex", "claude", "claude-sonnet", "copilot", "kilo"]
    text = draw(state).text()
    assert text.index("[1] kilo-glm") < text.index("[2] codex") < text.index("[ ] claude")
    # Toggling re-sorts and the cursor follows the toggled entry.
    state.provider_cursor = 5
    state.key(" ")  # kilo becomes third in the rotation
    assert state.providers == "kilo-glm,codex,kilo"
    assert [name for name, _ in state.choices()][:3] == ["kilo-glm", "codex", "kilo"]
    assert state.choices()[state.provider_cursor][0] == "kilo"


def test_horizontal_picker_uses_rotation_order_too(tmp_path) -> None:
    state = Launcher(str(tmp_path), creating=True, installed=("codex", "claude", "copilot"), providers="copilot,codex", focus=1)
    assert [name for _s, _e, name in state.provider_slots()] == ["copilot", "codex", "claude"]


def test_subtitle_names_only_the_installed_providers(tmp_path) -> None:
    assert launcher.subtitle(("codex", "claude")) == "Run Codex and Claude Code on your tasks · switch as limits run out"
    assert launcher.subtitle(("codex", "claude", "copilot")) == "Run Codex, Claude Code and Copilot on your tasks · switch as limits run out"
    assert launcher.subtitle(("codex",)) == "Run Codex on your tasks"
    assert launcher.subtitle(()) == launcher._SUBTITLE and "Copilot" not in launcher._SUBTITLE
    assert draw(Launcher(str(tmp_path), installed=("claude",))).row(1) == "Run Claude Code on your tasks"


# --- 15: ID column, attach and controls ------------------------------------------------------------

def test_list_has_a_muted_id_column(tmp_path, monkeypatch) -> None:
    seen = {}

    class Recording(Grid):
        def addnstr(self, y, x, text, count, *attrs):
            seen[(y, text.strip())] = attrs[0] if attrs else 0
            super().addnstr(y, x, text, count, *attrs)

    state = Launcher(str(tmp_path), missions=[mission("Alpha", "CREATED", ident="f292f491aaaaaaaa")], installed=("codex",))
    grid = Recording(size=(24, 100))
    launcher._draw(grid, state)
    assert " ID " in grid.row(4) + " " and "MISSION" in grid.row(4)
    assert "f292f491" in grid.row(6) and "f292f491a" not in grid.row(6)  # row 5 is "New mission in this folder"
    assert grid.row(4).index("ID") == grid.row(6).index("f292f491")
    assert grid.row(4).index("ID") < grid.row(4).index("MISSION")
    assert (6, "f292f491") in seen


def live_native(ident="0123456789abcdef") -> dict:
    item = mission("Live", "RUNNING", ident=ident)
    item["config"]["native_tui"] = True
    return item


def test_enter_attaches_to_a_live_native_mission_and_o_opens_controls(tmp_path) -> None:
    state = Launcher(str(tmp_path), missions=[live_native()], selected=1, installed=("codex",), probe=lambda ident: True)
    assert state.key("\n") == "attach"
    assert state.key("o") == "open"
    footer = launcher.list_footer(state)
    assert "Enter attach" in footer and "o controls" in footer
    grid = draw(state)
    assert grid.row(33) == "↑↓ select · Enter attach · o controls · N new mission · ? help · Q quit"
    # A mission without a live controller, or a headless one, opens its controls.
    stale = Launcher(str(tmp_path), missions=[live_native()], selected=1, installed=("codex",), probe=lambda ident: False)
    assert stale.key("\n") == "open"
    headless = live_native()
    headless["config"]["headless"] = True
    assert Launcher(str(tmp_path), missions=[headless], selected=1, probe=lambda ident: True).key("\n") == "open"
    created = Launcher(str(tmp_path), missions=[mission("New", "CREATED")], selected=1)
    assert created.key("\n") == "open" and "Enter controls" in launcher.list_footer(created) and "S start" in launcher.list_footer(created)
    assert Launcher(str(tmp_path), missions=[mission("Done", "COMPLETED")], selected=1).key("s") is None
    assert created.key("S") == "start-selected"
    typed = Launcher(str(tmp_path), missions=[live_native()], selected=1, probe=lambda ident: True)
    typed.key("/")
    assert typed.key("o") is None and typed.filter_text == "o"  # while filtering, o is text


def _controller_is_live(monkeypatch) -> None:
    monkeypatch.setattr(launcher.Launcher, "controller_state",
                        lambda self, item: "live" if str(item.get("status")) in launcher._LIVE_STATUSES else None)


def test_enter_on_a_live_mission_runs_attach_then_returns_to_the_list(tmp_path, monkeypatch) -> None:
    prepare(tmp_path, monkeypatch)
    with bridge.open_store() as store:
        created = store.create_mission("Live", "objective", cwd=tmp_path, config={"native_tui": True})
        store.set_mission_status(created["id"], "RUNNING")
    _controller_is_live(monkeypatch)
    Recorder([curses.KEY_DOWN, "\n", "q"], monkeypatch)
    calls = []
    assert launcher.run_launcher(lambda args: calls.append(args) or 0) == 0
    assert calls == [["attach", "--mission", created["id"]]]  # detached: back on the list, then Q


def test_attach_without_a_station_falls_back_to_the_controls(tmp_path, monkeypatch) -> None:
    prepare(tmp_path, monkeypatch)
    with bridge.open_store() as store:
        created = store.create_mission("Live", "objective", cwd=tmp_path, config={"native_tui": True})
        store.set_mission_status(created["id"], "RUNNING")
    _controller_is_live(monkeypatch)
    Recorder([curses.KEY_DOWN, "\n"], monkeypatch)
    calls = []
    assert launcher.run_launcher(lambda args: calls.append(args) or (1 if args[0] == "attach" else 0)) == 0
    assert [call[0] for call in calls] == ["attach", "control"]


# --- 16: after Save the home screen confirms and offers S / o ---------------------------------------------

def test_save_only_stays_on_the_list_with_a_confirmation_and_s_starts_it(tmp_path, monkeypatch) -> None:
    keys = ["n", *"Save me", "\t", "\t", "\t", "\t", "\n"]  # Save, then look at the screen
    status, calls, recorder = run_with(lambda args: pytest.fail("save-only started work"), tmp_path, monkeypatch, [*keys, "\x04"])
    assert status == 0 and [call[0] for call in calls] == ["mission"]
    with bridge.open_store() as store:
        saved = store.list_missions()[0]
    note = f"Saved {saved['id'][:8]} · S start · o controls"
    screen = recorder.screens[-1]
    assert note in screen and "Choose a mission" in screen and "Objective:" not in screen
    selected = next(line for line in screen.splitlines() if line.startswith("> ") and "CREATED" in line)
    assert saved["id"][:8] in selected  # the new mission is the selected row, with its id


def test_s_after_save_starts_the_selected_mission_and_o_opens_its_controls(tmp_path, monkeypatch) -> None:
    started = []

    def start(args):
        started.append(args)
        return 0

    keys = ["n", *"Save me", "\t", "\t", "\t", "\t", "\n", "S"]
    status, calls, _ = run_with(start, tmp_path, monkeypatch, keys)
    assert status == 0 and [call[0] for call in calls] == ["mission", "start", "control"]
    assert calls[1][:2] == ["start", "--mission"] and calls[2][:2] == ["control", "--mission"]
    # Controls announce the saved id once.
    assert bridge_tui.JUST_SAVED_MISSION == calls[2][2]
    bridge_tui.JUST_SAVED_MISSION = None


def test_o_after_save_opens_the_controls_without_starting(tmp_path, monkeypatch) -> None:
    status, calls, _ = run_with(lambda args: pytest.fail("o must not start"), tmp_path, monkeypatch, ["n", *"Save two", "\t", "\t", "\t", "\t", "\n", "o"])
    assert [call[0] for call in calls] == ["mission", "control"]
    assert bridge_tui.JUST_SAVED_MISSION == calls[1][2]
    bridge_tui.JUST_SAVED_MISSION = None


def test_notice_disappears_when_the_selection_moves(tmp_path) -> None:
    state = Launcher(str(tmp_path), missions=[mission("A", ident="a" * 16), mission("B", ident="b" * 16)], selected=1, installed=("codex",))
    state.notice, state.notice_id = "Saved aaaaaaaa · S start · o controls", "a" * 16
    assert "Saved aaaaaaaa" in draw(state).text()
    state.key(curses.KEY_DOWN)
    assert "Saved aaaaaaaa" not in draw(state).text()


# --- 10: the console says which id was saved ------------------------------------------------------

def test_console_strip_says_the_saved_id_once() -> None:
    state = make_state({"mission": {"id": "f292f491aaaa", "name": "Demo", "status": "CREATED"}})
    state.mission_id = "f292f491aaaa"
    state.saved_notice = True
    text = "".join(part for part, _role in bridge_tui._strip_segments(state, 100))
    assert text == "saved as f292f491 · type /start"
    state.handle_key("KEY_RESIZE")
    assert state.saved_notice
    state.handle_key("x")
    assert not state.saved_notice
    assert "saved as" not in "".join(part for part, _role in bridge_tui._strip_segments(state, 100))
    state.snapshot["mission"]["status"] = "RUNNING"  # type: ignore[index]
    state.saved_notice = True
    assert "".join(part for part, _role in bridge_tui._strip_segments(state, 100)) == "saved as f292f491"
