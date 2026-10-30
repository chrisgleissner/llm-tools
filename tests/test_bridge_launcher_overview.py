"""Home screen overview: header, mission rows, grouping, filter, objective summary and ASCII mode."""
from __future__ import annotations

import curses
from datetime import timedelta
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import sqlite3
import struct
import subprocess
import sys
import termios
import time

import pytest

from llm_tools import bridge_glyphs as glyphs, bridge_launcher as launcher, bridge_redraw, bridge_tui as tui
from llm_tools.bridge_input import cell_width
from llm_tools.bridge_launcher import Launcher, MissionStats
from llm_tools.bridge_store import BridgeStore

from tests import test_bridge_launcher_home as home
from tests.conftest import scaled

Grid, mission = home.Grid, home.mission


@pytest.fixture(autouse=True)
def _terminal(monkeypatch):
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(tui, "initialize_colors", lambda: False)


def ascii_locale(monkeypatch, codeset="ANSI_X3.4-1968"):
    monkeypatch.setattr(glyphs, "_locale_codeset", lambda: codeset)
    glyphs.ascii_mode.cache_clear()


def age_columns(grid, rows):
    """Terminal cell at which each row's age text starts."""
    found = {}
    for y in rows:
        match = re.search(r"(\d+[mhdy] ago|just now)", grid.row(y))
        found[y] = cell_width(grid.row(y)[:match.start()])
    return set(found.values())


def draw(state, size=(24, 80)):
    grid = Grid(size=size)
    launcher._draw(grid, state)
    return grid


def listing(count=3, **kw):
    items = [mission(f"Mission {i}", "RUNNING" if i == 0 else "COMPLETED", f"/w/{i}", timedelta(minutes=i + 1), f"{i:08d}abcdef")
             for i in range(count)]
    return Launcher("/w/0", missions=launcher.order_missions(items, "/w/0"), installed=("codex",), **kw)


# --- 1. header --------------------------------------------------------------

def test_header_states_the_value_and_shows_the_folder_on_the_title_line(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    work = tmp_path / "dev" / "c64" / "c64bridge"
    work.mkdir(parents=True)
    grid = draw(Launcher(str(work), installed=("codex",)))
    assert grid.row(0) == "LLM BRIDGE  ● here: ~/dev/c64/c64bridge"
    assert grid.row(1) == "Run Codex on your tasks"  # only the installed providers are named
    assert cell_width(launcher._SUBTITLE) <= 80
    assert grid.row(2) == "" and grid.row(3).startswith("Choose a mission")
    assert "Working directory" not in grid.text() and "Durable missions" not in grid.text()


def test_header_clips_a_very_long_folder_to_the_terminal_width(tmp_path):
    work = tmp_path / ("deep-" * 40)
    work.mkdir()
    grid = draw(Launcher(str(work), installed=("codex",)))
    assert cell_width(grid.row(0)) < 80 and "…" in grid.row(0) and grid.row(0).startswith("LLM BRIDGE  ● here: ")


def test_header_is_the_same_on_the_creation_form(tmp_path):
    grid = draw(Launcher(str(tmp_path), creating=True, installed=("codex",)))
    assert grid.row(0).startswith("LLM BRIDGE  ● here: ") and grid.row(3) == "New mission"


# --- 2. mission rows --------------------------------------------------------

def stats_for(items):
    return {"a" * 16: MissionStats(2, 5, "codex"), "b" * 16: MissionStats(0, 0, ""), "c" * 16: MissionStats(5, 5, "claude")}


def sample_state(tmp_path, **kw):
    here = str(tmp_path)
    items = launcher.order_missions([
        mission("Port the bridge", "RUNNING", here, timedelta(minutes=4), "a" * 16),
        mission("Draft the docs", "CREATED", "/srv/docs", timedelta(minutes=30), "b" * 16),
        mission("Ship the mobile app", "COMPLETED", "/srv/mobile", timedelta(days=5), "c" * 16),
    ], here)
    return Launcher(here, missions=items, stats=stats_for(items), installed=("codex",), probe=lambda ident: True, **kw)


def test_rows_show_progress_active_provider_age_and_folder(tmp_path):
    grid = draw(sample_state(tmp_path), size=(34, 120))
    header, new, running, created, done = (grid.row(y) for y in range(4, 9))
    for label in ("STATUS", "MISSION", "TASKS", "PROVIDER", "UPDATED", "FOLDER"):
        assert label in header
    assert new.strip() == "> New mission in this folder"
    assert "RUNNING" in running and "Port the bridge" in running and "2/5 done" in running
    assert "codex" in running and "4m ago" in running
    assert "no tasks" in created and "30m ago" in created and "/srv/docs" in created
    assert "5/5 done" in done and "5d ago" in done and "/srv/mobile" in done
    # Only a running mission names a provider; finished and waiting rows leave the cell blank.
    assert "claude" not in done and "codex" not in created
    # Columns line up by terminal cell with their header labels.
    for label, sample in (("TASKS", "2/5 done"), ("PROVIDER", "codex"), ("UPDATED", "4m ago"), ("FOLDER", "/tmp")):
        assert cell_width(header[:header.index(label)]) == cell_width(running[:running.index(sample)]), label


def test_unknown_stats_render_a_dash_and_a_stale_controller_shows_no_provider(tmp_path):
    state = sample_state(tmp_path)
    state.stats = {}
    state.probe = lambda ident: False
    grid = draw(state, size=(34, 120))
    running = grid.row(6)
    assert "stale" in running and "2/5" not in running
    assert re.search(r"stale\s+aaaaaaaa\s+▌ Port the bridge\s+-\s+-\s+4m ago", running), running


def test_progress_text_variants():
    assert launcher.progress_text(MissionStats(2, 5)) == "2/5 done"
    assert launcher.progress_text(MissionStats(0, 0)) == "no tasks"
    assert launcher.progress_text(None) == "-"
    assert launcher.row_provider({"status": "RUNNING"}, MissionStats(route="codex"), "live") == "codex"
    assert launcher.row_provider({"status": "RUNNING"}, MissionStats(), None) == "-"
    assert launcher.row_provider({"status": "RUNNING"}, MissionStats(route="codex"), "stale") == "-"
    assert launcher.row_provider({"status": "CREATED"}, MissionStats(route="codex"), None) == ""
    assert launcher.row_provider({"status": "PAUSED"}, None, "live") == "-"


def test_narrow_widths_drop_folder_then_provider_then_age_then_progress():
    items = [mission("A fairly long mission name for width tests", "RUNNING", "/srv/a" * 5, ident="a" * 16)]
    stats = {"a" * 16: MissionStats(1, 3, "codex")}
    plans = {w: launcher.plan_columns(items, stats, w) for w in range(60, 161)}
    full = plans[160]
    assert full.folder > 14 and full.provider and full.age and full.progress
    at80 = plans[80]
    assert at80.progress and at80.ident and at80.age and at80.provider and not at80.folder and at80.name >= 20
    assert not plans[60].folder and plans[60].name >= 12
    # A narrower terminal never shows a column that a wider one drops, and the
    # columns disappear in the order folder, provider, age, progress.
    for column in ("folder", "provider", "age", "ident", "progress"):
        shown = [w for w in plans if getattr(plans[w], column)]
        assert shown == list(range(min(shown, default=161), 161)), column
    first = {c: min(w for w in plans if getattr(plans[w], c)) for c in ("folder", "provider", "age", "ident", "progress")}
    assert first["folder"] >= first["provider"] >= first["age"] >= first["ident"] >= first["progress"]
    squeezed = launcher.plan_columns(items, stats, 40)
    assert squeezed.name >= 12 and not (squeezed.progress or squeezed.provider or squeezed.age or squeezed.folder)
    for width, cols in plans.items():
        used = 4 + launcher._STATUS_WIDTH + 2 + cols.name + sum(2 + c for c in (cols.progress, cols.provider, cols.age, cols.folder) if c)
        assert used <= width - 1, (width, cols)


def test_short_names_leave_room_for_the_folder_sooner():
    items = [mission("Short", "COMPLETED", "/srv/a", ident="a" * 16)]
    assert launcher.plan_columns(items, {}, 80).folder > 14


def test_no_provider_column_when_nothing_is_running(tmp_path):
    items = [mission("Done", "COMPLETED", "/a", ident="a" * 16), mission("Wait", "CREATED", "/b", ident="b" * 16)]
    cols = launcher.plan_columns(items, {}, 120)
    assert cols.provider == 0 and cols.progress and cols.age and cols.folder


def test_rows_never_exceed_the_terminal_width_and_stay_aligned_at_80_columns(tmp_path):
    state = sample_state(tmp_path)
    state.missions[0]["name"] = "日本語のミッション " * 6
    grid = draw(state)
    for y in range(4, 9):
        assert cell_width(grid.row(y)) <= 79, grid.row(y)
    assert len(age_columns(grid, range(6, 9))) == 1


# --- 3. grouping ------------------------------------------------------------

def test_rows_are_grouped_live_then_waiting_then_finished_and_finished_rows_are_dim(tmp_path, monkeypatch):
    roles = {}
    monkeypatch.setattr(tui, "role_attr", lambda role: roles.setdefault(role, len(roles) + 1))

    class Recording(Grid):
        def addnstr(self, y, x, text, count, *attrs):
            seen[(y, text.strip())] = attrs[0]
            super().addnstr(y, x, text, count, *attrs)

    seen = {}
    here = str(tmp_path)
    items = launcher.order_missions([
        mission("done", "COMPLETED", "/x", ident="d" * 16), mission("failed", "FAILED", "/x2", ident="f" * 16),
        mission("live-elsewhere", "RUNNING", "/y", ident="l" * 16), mission("fresh", "CREATED", "/z", ident="c" * 16),
        mission("paused-here", "PAUSED", here, ident="p" * 16), mission("cancelled", "CANCELLED", "/q", ident="x" * 16)], here)
    state = Launcher(here, missions=items, installed=("codex",), probe=lambda ident: True)
    launcher._draw(Recording(), state)
    grid = Grid()
    launcher._draw(grid, state)
    shown = [re.search(r"(RUNNING|PAUSED|FAILED|CREATED|COMPLETED|CANCELLED)\s+\w{8}\s+▌ (\S+)", grid.row(y)).groups() for y in range(6, 12)]
    assert shown == [("PAUSED", "paused-here"), ("RUNNING", "live-elsewhere"), ("FAILED", "failed"),
                     ("CREATED", "fresh"), ("COMPLETED", "done"), ("CANCELLED", "cancelled")]
    assert "● /" in grid.row(6) and "● /" not in grid.row(7)  # the current folder keeps its marker, in the folder column
    # Finished rows recede (muted) except for the state word, which keeps its own colour.
    assert seen[(10, "done")] == roles["muted"] and seen[(11, "cancelled")] == roles["muted"]
    assert seen[(10, "✓ COMPLETED")] == roles["state_done"] and seen[(11, "✕ CANCELLED")] == roles["state_paused"]
    assert seen[(8, "failed")] == roles["info"] and seen[(9, "fresh")] == roles["info"]
    assert seen[(6, "paused-here")] == roles["info"] and seen[(6, "‖ PAUSED")] == roles["state_paused"]
    assert launcher.status_group("STOPPED") == 1 and launcher.status_group("STOPPING") == 0


# --- 4. footer hints match real keys, filter --------------------------------

def test_every_list_footer_hint_is_a_working_key():
    state = listing(8)
    footer = launcher.list_footer(state)
    assert footer == "↑↓ select · Enter open · N new mission · / filter · ? help · Q quit"
    assert state.key(curses.KEY_DOWN) is None and state.selected == 1
    assert state.key("\n") == "open"
    assert state.key("?") == "help" and state.key("q") == "quit" and state.key("Q") == "quit"
    assert state.key("n") is None and state.creating
    state.creating = False
    assert state.key("/") is None and state.filtering


def test_filter_hint_is_only_advertised_for_longer_lists_or_an_active_filter():
    assert "/ filter" not in launcher.list_footer(listing(5))
    assert "/ filter" in launcher.list_footer(listing(6))
    state = listing(2)
    state.filter_text = "x"
    assert launcher.list_footer(state) == "↑↓ select · Enter open · N new mission · / filter · ? help · Esc clear filter · Q quit"


def type_text(state, text):
    for char in text:
        state.key(char)


def test_slash_and_ctrl_f_start_filtering_and_typing_narrows_the_list():
    items = [mission("Fix login", "RUNNING", "/srv/web", ident="aaaa1111"), mission("Docs", "CREATED", "/srv/docs", ident="bbbb2222"),
             mission("Login docs", "CREATED", "/home/x/api", ident="cccc3333")]
    for opener in ("/", "\x06"):
        state = Launcher("/w", missions=list(items), installed=("codex",))
        state.key(opener)
        assert state.filtering
        type_text(state, "LOGIN")  # case-insensitive, letters like q/j/n are text now
        assert [m["name"] for m in state.view()] == ["Fix login", "Login docs"] and state.selected == 1
        assert state.key(curses.KEY_BACKSPACE) is None and state.filter_text == "LOGI"
        state.key("\x15")
        assert state.filter_text == "" and len(state.view()) == 3
    state = Launcher("/w", missions=list(items), installed=("codex",))
    state.key("/")
    type_text(state, "quit")  # q is typed, not an exit
    assert state.filtering and state.filter_text == "quit" and state.view() == [] and state.selected == 0


def test_filter_matches_folder_and_id_prefix_but_not_id_middle():
    item = mission("Alpha", "RUNNING", "/srv/customer-portal", ident="abcd1234ef")
    assert launcher.mission_matches(item, "portal") and launcher.mission_matches(item, "ABCD12")
    assert not launcher.mission_matches(item, "cd1234") and launcher.mission_matches(item, "  ")
    home_item = mission("Alpha", "RUNNING", os.path.expanduser("~/dev/thing"), ident="z")
    assert launcher.mission_matches(home_item, "~/dev")


def test_enter_opens_the_selected_match_and_esc_clears_before_quitting():
    items = [mission("Alpha", "RUNNING", "/a", ident="a1"), mission("Beta", "RUNNING", "/b", ident="b1")]
    state = Launcher("/w", missions=items, installed=("codex",))
    state.key("/")
    type_text(state, "bet")
    assert state.key("\n") == "open" and not state.filtering and state.view()[state.selected - 1]["name"] == "Beta"
    state = Launcher("/w", missions=items, installed=("codex",))
    state.key("/")
    type_text(state, "zzz")
    assert state.key("\n") is None and not state.filtering  # nothing selected: Enter only leaves the filter line
    state.key("/")
    state.key("\x1b")
    assert state.filter_text == "" and not state.filtering and len(state.view()) == 2
    state.key("/")
    type_text(state, "a")
    state.key("\n")  # leave the filter line with the filter applied
    assert state.filter_text == "a"
    assert state.key("\x1b") is None and state.filter_text == ""
    assert state.key("\x1b") == "quit"


def test_filter_line_navigation_and_backspace_leaving_the_line():
    items = [mission(f"Item {i}", "RUNNING", f"/w{i}", ident=f"id{i}") for i in range(3)]
    state = Launcher("/w", missions=items, installed=("codex",))
    state.key("/")
    type_text(state, "item")
    state.key(curses.KEY_DOWN)
    state.key("\t")
    assert state.selected == 3
    state.key(curses.KEY_UP)
    assert state.selected == 2
    state.key(curses.KEY_RIGHT)  # ignored while typing
    type_text(state, "\x01")
    assert state.filter_text == "item"
    for _ in range(4):
        state.key("\x7f")
    assert state.filter_text == "" and state.filtering
    state.key("\x7f")
    assert not state.filtering


def test_filter_rendering_heading_counts_footer_and_no_match_message(tmp_path):
    items = [mission(f"Item {i}", "RUNNING", f"/w{i}", ident=f"id{i}") for i in range(8)]
    state = Launcher(str(tmp_path), missions=items, installed=("codex",), probe=lambda ident: True)
    state.key("/")
    type_text(state, "item 3")
    grid = draw(state)
    assert grid.row(3) == "Filter: item 3  1 of 8"
    assert "Item 3" in grid.row(6) and grid.row(5).strip() == "New mission in this folder"
    assert grid.row(23) == "Type to filter · ↑↓ select · Enter open · Esc clear filter"
    assert grid.cursor == (3, len("Filter: item 3"))
    type_text(state, "x")
    grid = draw(state)
    assert grid.row(3) == "Filter: item 3x  no match" and 'No mission matches "item 3x"' in grid.row(7)
    assert "STATUS" not in grid.text()
    state.filtering = False
    grid = draw(state)
    assert grid.cursor is None and "Esc clear filter" in grid.row(23)


def test_click_maps_to_the_filtered_rows():
    items = [mission(f"Item {i}", "RUNNING", f"/w{i}", ident=f"id{i}") for i in range(8)]
    state = Launcher("/w", missions=items, installed=("codex",), filter_text="item 6")
    assert state.click(6, activate=True) == "open" and state.view()[state.selected - 1]["name"] == "Item 6"
    assert state.click(7) is None and state.selected == 1


# --- background refresh -----------------------------------------------------

def test_reload_follows_the_selected_mission():
    first = [mission("A", "RUNNING", "/a", ident="a1"), mission("B", "RUNNING", "/b", ident="b1")]
    second = [mission("New", "RUNNING", "/n", ident="n1"), *first]
    state = Launcher("/w", missions=first, selected=2, installed=("codex",), loading=True)
    state.reload(second, {"b1": MissionStats(1, 2, "claude")})
    assert state.selected == 3 and state.stats["b1"].route == "claude" and not state.loading


def test_refresh_clamps_a_selection_whose_mission_disappeared():
    state = Launcher("/w", missions=[mission("A", "RUNNING", "/a", ident="a1"), mission("B", "RUNNING", "/b", ident="b1")],
                     selected=2, installed=("codex",))
    state.reload([mission("A", "RUNNING", "/a", ident="a1")], {})
    assert state.selected == 1
    state.reload([], {})
    assert state.selected == 0


def test_drawing_never_touches_the_database(tmp_path):
    calls = []
    state = sample_state(tmp_path, reloader=bridge_redraw.BackgroundReload(lambda: calls.append(1) or ([], {})))
    for _ in range(30):
        draw(state)
    assert calls == []


def test_running_launcher_reloads_the_list_when_the_screen_opens(tmp_path, monkeypatch):
    home.prepare(tmp_path, monkeypatch)
    fresh = [mission("Refreshed mission", "RUNNING", "/elsewhere", ident="r" * 16)]
    monkeypatch.setattr(launcher, "_load_home", lambda cwd: (fresh, {"r" * 16: MissionStats(1, 4, "kilo-zai-glm-52")}))
    recorder = home.Recorder(["\x04"], monkeypatch)
    assert launcher.run_launcher(lambda args: pytest.fail(args)) == 0
    screen = recorder.screens[-1]
    assert "Refreshed mission" in screen and "1/4 done" in screen and "/elsewhere" in screen


def test_running_launcher_shortens_the_escape_delay_so_esc_does_not_lag(tmp_path, monkeypatch):
    home.prepare(tmp_path, monkeypatch)
    delays = []
    monkeypatch.setattr(curses, "set_escdelay", delays.append)
    home.Recorder(["\x04"], monkeypatch)
    assert launcher.run_launcher(lambda args: pytest.fail(args)) == 0
    assert delays == [launcher._ESCDELAY_MS] and 0 < launcher._ESCDELAY_MS <= 100
    monkeypatch.delattr(curses, "set_escdelay")  # older curses: the loop still runs
    home.Recorder(["\x04"], monkeypatch)
    assert launcher.run_launcher(lambda args: pytest.fail(args)) == 0


def test_running_launcher_opens_the_mission_chosen_through_the_filter(tmp_path, monkeypatch):
    home.prepare(tmp_path, monkeypatch)
    monkeypatch.setenv("COLUMNS", "120")
    work = tmp_path / "work"
    with BridgeStore(tmp_path / "state" / "bridge.sqlite3") as store:
        first = store.create_mission("Alpha job", "obj", cwd=work)
        second = store.create_mission("Beta job", "obj", cwd=tmp_path)
    keys = ["/", *"beta", "\n"]
    recorder = home.Recorder(keys, monkeypatch)
    calls = []
    assert launcher.run_launcher(lambda args: calls.append(args) or 0) == 0
    assert calls == [["control", "--mission", second["id"]]] and first["id"] != second["id"]
    assert recorder.screens


# --- loading ----------------------------------------------------------------

def seed_store(tmp_path):
    state_dir = tmp_path / "state"
    (tmp_path / "w1").mkdir()
    (tmp_path / "w2").mkdir()
    (tmp_path / "w3").mkdir()
    with BridgeStore(state_dir / "bridge.sqlite3") as store:
        one = store.create_mission("One", "obj", cwd=tmp_path / "w1")["id"]
        two = store.create_mission("Two", "obj", cwd=tmp_path / "w2")["id"]
        three = store.create_mission("Three", "obj", cwd=tmp_path / "w3")["id"]
        tasks = [store.add_task(one, f"t{i}") for i in range(3)]
        proposal = store.add_task(one, "proposed")
        store.db.execute("UPDATE tasks SET origin='agent-proposal', proposal_status='PENDING' WHERE id=?", (proposal,))
        store.db.execute("UPDATE tasks SET status='COMPLETED' WHERE id=?", (tasks[0],))
        store.db.execute("UPDATE missions SET status='RUNNING' WHERE id=?", (one,))
        store.begin_operation(tasks[1], iteration=1, station="codex")
        store.db.execute("UPDATE operations SET route_id='' WHERE mission_id=?", (one,))
        second_task = store.add_task(two, "x")
        store.db.execute("UPDATE missions SET status='RUNNING' WHERE id=?", (two,))
        store.begin_operation(second_task, iteration=1, station="claude")
        store.db.execute("UPDATE operations SET route_id='claude-sonnet', created_at='2999-01-01T00:00:00+00:00' WHERE mission_id=?", (two,))
        store.db.execute("UPDATE missions SET status='RUNNING' WHERE id=?", (three,))
    return state_dir, one, two, three


def test_home_load_counts_operator_tasks_and_names_the_latest_provider(tmp_path, monkeypatch):
    state_dir, one, two, three = seed_store(tmp_path)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state_dir))
    missions, stats = launcher._load_home(str(tmp_path / "w1"))
    assert {m["id"] for m in missions} == {one, two, three} and missions[0]["id"] == one  # live and in this folder
    assert stats[one] == MissionStats(1, 3, "codex")  # the pending proposal does not count
    assert stats[two] == MissionStats(0, 1, "claude-sonnet")  # the route id wins over the station
    assert stats[three] == MissionStats(0, 0, "")
    assert launcher._load_missions(str(tmp_path))[0]["config"] is not None


def test_periodic_refresh_reads_without_waiting_for_a_writer(tmp_path, monkeypatch):
    state_dir, one, two, _three = seed_store(tmp_path)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state_dir))
    writer = sqlite3.connect(state_dir / "bridge.sqlite3", isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE missions SET name='Held' WHERE id=?", (one,))
    try:
        started = time.monotonic()
        missions, stats = launcher._refresh_home(str(tmp_path))
        assert time.monotonic() - started < 2.0
        assert stats[one].done == 1 and {m["name"] for m in missions} >= {"One"}  # committed state only
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_periodic_refresh_without_a_database_raises_for_the_background_reader_to_report(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "missing"))
    with pytest.raises(sqlite3.Error):
        launcher._refresh_home(str(tmp_path))


def test_read_home_degrades_when_stats_tables_or_config_are_unusable():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE missions (id TEXT, name TEXT, cwd TEXT, status TEXT, config_json TEXT, created_at TEXT, updated_at TEXT)")
    db.execute("INSERT INTO missions VALUES ('m1','Broken','/a','RUNNING','not json','2026-01-01','2026-01-01')")
    missions, stats = launcher._read_home(db, "/a")
    assert missions[0]["config"] == {} and stats == {"m1": MissionStats()}


# --- 6. objective summary ---------------------------------------------------

def test_objective_summary_shows_first_line_without_heading_markers_and_line_count():
    assert launcher.objective_summary("# Fix the login flow\nThen tests\n\nAnd docs\n", 60) == "Fix the login flow (+2 lines)"
    assert launcher.objective_summary("\n\n## Second level  \nonly one more", 60) == "Second level (+1 line)"
    assert launcher.objective_summary("Single line", 60) == "Single line"
    assert launcher.objective_summary("#hashtag stays\nmore", 60) == "#hashtag stays (+1 line)"
    assert launcher.objective_summary("#\nbody", 60) == "# (+1 line)"
    assert launcher.objective_summary(" \n \n", 60) == ""
    clipped = launcher.objective_summary("A very long first line " * 5 + "\nsecond", 30)
    assert clipped.endswith("… (+1 line)") and cell_width(clipped) == 30


OBJECTIVE = "# Fix the login flow\nThen add tests for the retry path\nAnd update the docs"


def test_unfocused_objective_shows_the_beginning_and_focused_follows_the_caret(tmp_path):
    state = Launcher(str(tmp_path), creating=True, installed=("codex",), objective=OBJECTIVE, focus=1)
    state.cursors[0] = len(OBJECTIVE)
    grid = draw(state)
    assert grid.row(5) == "  Objective: Fix the login flow (+2 lines)"
    state.focus = 0
    grid = draw(state)
    row = grid.row(5)
    assert row.startswith("> Objective: ") and "↵" in row and row.rstrip().endswith("And update the docs")
    assert "# Fix" not in row and "Fix the login" not in row  # the caret is at the end, so the view shows the tail
    assert grid.cursor[0] == 5
    state.objective = "   "
    state.focus = 1
    assert draw(state).row(5).rstrip() == "  Objective:"


def test_unfocused_objective_is_clipped_with_its_line_count_in_a_narrow_terminal(tmp_path):
    state = Launcher(str(tmp_path), creating=True, installed=("codex",), focus=2, objective="word " * 40 + "\nmore\nmore")
    row = draw(state, size=(24, 60)).row(5)
    assert row.endswith("… (+2 lines)") and cell_width(row) <= 59


# --- 7. ASCII mode ----------------------------------------------------------

@pytest.mark.parametrize("codeset,expected", [("UTF-8", False), ("utf8", False), ("UTF_8", False),
                                              ("ANSI_X3.4-1968", True), ("ISO-8859-1", True), ("", True)])
def test_ascii_mode_follows_the_locale_codeset(monkeypatch, codeset, expected):
    ascii_locale(monkeypatch, codeset)
    assert launcher.ascii_mode() is expected


def test_codeset_falls_back_to_stdout_encoding_when_langinfo_is_unavailable(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(glyphs.locale, "nl_langinfo", lambda _: (_ for _ in ()).throw(ValueError("no codeset")))
    monkeypatch.setattr(glyphs.sys, "stdout", type("Out", (), {"encoding": "latin-1"})())
    assert glyphs._locale_codeset() == "latin-1"
    monkeypatch.setattr(glyphs.sys, "stdout", object())
    assert glyphs._locale_codeset() == ""
    monkeypatch.undo()
    assert isinstance(glyphs._locale_codeset(), str)


def test_glyph_table_and_localize_have_ascii_equivalents(monkeypatch):
    assert launcher.glyph("sep") == "·" and launcher.glyph("updown") == "↑↓"
    expected = {"sep": "|", "updown": "Up/Down", "dash": "-", "here": "*", "cursor": ">", "newline": "\\n",
                "ellipsis": "...", "arrow": "->", "times": "x", "updown_slash": "Up/Down", "leftright": "Left/Right"}
    assert {name: plain for name, (_utf8, plain) in launcher.GLYPHS.items() if name in expected} == expected
    ascii_locale(monkeypatch)
    assert {name: launcher.glyph(name) for name in expected} == expected
    assert launcher.localize("a · b ↑/↓ ←/→ → … ● ▸ ↵ — ×") == "a | b Up/Down Left/Right -> ... * > \\n - x"
    assert launcher.localize("↑/↓ choose · Space/←/→") == "Up/Down choose | Space/Left/Right"
    assert launcher.localize("日本 café ⚡") == "?? cafe ?"
    assert launcher.localize("plain") == "plain"


def non_ascii(text):
    return sorted({c for c in text if ord(c) > 127})


def test_every_home_screen_is_pure_ascii_in_a_non_utf8_locale(tmp_path, monkeypatch):
    ascii_locale(monkeypatch)
    state = sample_state(tmp_path)
    state.missions[0]["name"] = "日本語 Ünïcode ミッション " * 3
    state.missions[1]["cwd"] = "/srv/docs/日本語"
    state.error = "Paste rejected · limit is 1 MiB … see ↵"
    screens = []
    for selected in range(len(state.missions) + 1):
        state.selected = selected
        screens.append(draw(state).text())
        screens.append(draw(state, size=(34, 120)).text())
    state.objective = OBJECTIVE
    state.filter_text, state.filtering = "mission", True
    screens.append(draw(state).text())
    state.filter_text, state.filtering = "zzz", False
    screens.append(draw(state).text())
    state.filter_text = ""
    for focus in range(6):
        state.creating, state.focus = True, focus
        screens.append(draw(state).text())
    state.routes = (launcher.RouteChoice("claude-sonnet", "claude", "claude-sonnet-5-5"),)
    state.installed = ("codex", "claude")
    screens.append(draw(state).text())
    state.creating = False
    screens.append(draw(Launcher(str(tmp_path), missions=[], installed=("codex",))).text())
    screens.append(draw(state, size=(18, 50)).text())
    for text in screens:
        assert non_ascii(text) == [], (non_ascii(text), text)
    assert "Up/Down select | Enter open" in screens[0]
    assert "| 3 saved" in screens[0]
    assert screens[-1].count("now 50x18") == 1


def test_ascii_mode_widths_and_columns_stay_aligned(tmp_path, monkeypatch):
    ascii_locale(monkeypatch)
    state = sample_state(tmp_path)
    state.missions[0]["name"] = "日本語のミッション " * 4
    grid = draw(state)
    assert len(age_columns(grid, range(6, 9))) == 1
    assert "..." in grid.row(6)
    assert launcher.mission_detail(mission(cwd="/a/" + "long/" * 30), 60).endswith(" | 01234567 | codex")
    assert launcher.mission_providers(mission(providers="codex,claude")) == "codex -> claude"
    assert launcher.truncate("abcdefghij", 6) == "abc..." and launcher.middle_ellipsis("abcdefghijklmnop", 9).count("...") == 1
    assert launcher.truncate("abcdefghij", 2) == ".." and launcher.middle_ellipsis("abcdefghij", 2) == ".."


def test_ascii_mode_creation_form_uses_plain_markers(tmp_path, monkeypatch):
    ascii_locale(monkeypatch)
    state = Launcher(str(tmp_path), creating=True, installed=("codex", "claude"), providers="codex", focus=0,
                     objective="one\ntwo")
    state.cursors[0] = 7
    grid = draw(state, size=(34, 120))
    assert "one \\n two" in grid.row(5) and "Mode: Native - provider terminal in a station" in grid.text()
    assert "Tab/Up/Down fields | Enter next | Esc back" == grid.row(33)
    assert ">[1] codex" not in grid.text()  # the cursor marker only appears on the focused picker row
    state.focus = 1
    assert ">[1] codex" in draw(state, size=(34, 120)).row(7)


# --- footers ----------------------------------------------------------------

def test_form_footer_matches_the_keys_that_work(tmp_path):
    state = Launcher(str(tmp_path), creating=True, installed=("codex", "claude"), providers="codex")
    assert launcher.form_footer(state) == "Tab/↑↓ fields · Enter next · Esc back"
    assert state.key("\t") is None and state.focus == 1
    assert state.key(curses.KEY_DOWN) is None and state.focus == 2  # horizontal picker: up/down leave the field
    state.routes = (launcher.RouteChoice("r1", "codex"),)
    state.focus, state.vertical = 1, True
    assert launcher.form_footer(state) == "Tab next field · Enter next · Esc back"
    assert state.key("\t") is None and state.focus == 2
    assert state.key("\x1b") is None and not state.creating


# --- real terminal ----------------------------------------------------------

def run_in_pty(env, work, keys, size=(24, 80)):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH", ""))))
    process = subprocess.Popen([sys.executable, "-m", "llm_tools.bridge"], cwd=work, env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()

    def pump(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([master], [], [], .03)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    return

    try:
        pump(1.5)
        for key in keys:
            os.write(master, key)
            pump(0.4)
        os.write(master, b"\x04")
        pump(0.5)
        try:
            code = process.wait(timeout=scaled(5))
        except subprocess.TimeoutExpired:
            pytest.fail(f"launcher did not exit; output={bytes(output)!r}")
        assert code == 0, bytes(output)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=scaled(3))
        os.close(master)
    return bytes(output)


def test_c_locale_terminal_session_emits_no_non_ascii_bytes(tmp_path, env):
    work = tmp_path / "работа"
    work.mkdir()
    other = tmp_path / "日本語"
    other.mkdir()
    state_dir = tmp_path / "state"
    with BridgeStore(state_dir / "bridge.sqlite3") as store:
        store.create_mission("Überprüfung des Logins 日本語", "obj", cwd=work)
        store.create_mission("Second mission", "obj", cwd=other)
    env.update(LLM_BRIDGE_STATE_DIR=str(state_dir), TERM="xterm-256color", LANG="C", LC_ALL="C", LLM_TOOLS_NO_INHIBIT="1")
    for name in ("LC_CTYPE", "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONCOERCECLOCALE"):
        env.pop(name, None)
    output = run_in_pty(env, work, [b"/", b"mis", b"\x1b", b"n", b"Fix it", b"\x1b"])
    assert [b for b in output if b > 0x7f] == [], output
    assert b"Up/Down select" in output and b"here:" in output
    assert b"Run " in output and b" on your tasks" in output
    assert b"Filter:" in output and b"no match" not in output and b"New mission" in output and b"Native - provider terminal in a" in output


def test_utf8_terminal_session_draws_the_unicode_glyphs(tmp_path, env):
    work = tmp_path / "work"
    work.mkdir()
    state_dir = tmp_path / "state"
    with BridgeStore(state_dir / "bridge.sqlite3") as store:
        store.create_mission("A mission", "obj", cwd=work)
    env.update(LLM_BRIDGE_STATE_DIR=str(state_dir), TERM="xterm-256color", LANG="C.UTF-8", LC_ALL="C.UTF-8", LLM_TOOLS_NO_INHIBIT="1")
    output = run_in_pty(env, work, [])
    plain = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\([A-Z]|\x1b[=>]", b"", output)
    assert "↑↓ select · Enter open".encode() in plain and "● here:".encode() in plain
    assert "▌".encode() in plain and "● RUNNING".encode() not in plain  # a fresh mission is not running
