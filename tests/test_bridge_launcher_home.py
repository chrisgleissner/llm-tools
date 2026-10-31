"""Home screen: provider picker, per-field hints, mission list and post-start flow."""
from __future__ import annotations

import curses
from datetime import datetime, timedelta, timezone
import sys

import pytest

from llm_tools import bridge, bridge_launcher as launcher, bridge_tui as tui
from llm_tools.bridge_input import cell_width
from llm_tools.bridge_launcher import Launcher
from llm_tools.common import KNOWN_PROVIDERS


class Grid:
    """Minimal curses window that records the cells actually drawn."""

    def __init__(self, keys=(), size=(34, 120)):
        self.keys = iter(keys)
        self.size = size
        self.cells: dict[tuple[int, int], str] = {}
        self.cursor = None

    def keypad(self, value): pass
    def timeout(self, value): pass
    def getmaxyx(self): return self.size
    def get_wch(self): return next(self.keys)
    def erase(self): self.cells = {}
    def refresh(self): pass
    def move(self, y, x): self.cursor = (y, x)

    def addnstr(self, y, x, text, count, *attrs):
        column = x
        for char in text[:count]:
            self.cells[(y, column)] = char
            for extra in range(1, cell_width(char)):
                self.cells[(y, column + extra)] = ""  # continuation cell of a wide character
            column += max(1, cell_width(char))

    def row(self, y):
        width = self.size[1]
        return "".join(self.cells.get((y, x), " ") for x in range(width)).rstrip()

    def text(self):
        return "\n".join(self.row(y) for y in range(self.size[0]))


@pytest.fixture(autouse=True)
def _terminal(monkeypatch):
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(tui, "initialize_colors", lambda: False)


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def mission(name="Alpha", status="RUNNING", cwd="/work/a", age=timedelta(minutes=5), ident="0123456789abcdef", providers="codex"):
    return {"id": ident, "name": name, "status": status, "cwd": cwd, "updated_at": iso(age),
            "created_at": iso(age), "config": {"providers": providers}}


# --- provider detection and defaults ---------------------------------------

def test_provider_order_matches_launchable_known_providers():
    assert set(launcher.PROVIDER_ORDER) == KNOWN_PROVIDERS - {"zai"}
    assert launcher.provider_binary("minimax") == "mmx"
    assert launcher.provider_binary("codex") == "codex"


def test_detect_installed_uses_path_lookup_in_declared_order(monkeypatch):
    present = {"claude", "mmx", "codex"}
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/bin/{name}" if name in present else None)
    assert launcher.detect_installed() == ("codex", "claude", "minimax")
    assert launcher.provider_installed("minimax") and not launcher.provider_installed("kilo")


@pytest.mark.parametrize("installed,config,expected", [
    (("codex", "claude"), {"ralph": {"providers": ["claude", "codex", "kilo"]}}, "claude,codex"),
    (("codex", "claude"), {"defaults": {"providers": ["claude"]}}, "claude"),
    (("codex", "claude"), {"ralph": {"providers": ["kilo"]}}, "codex"),
    (("claude", "kilo"), {}, "claude"),
    (("codex",), None, "codex"),
    ((), {}, "codex"),
])
def test_default_providers(installed, config, expected):
    assert launcher.default_providers(installed, config) == expected


def test_broken_config_never_blocks_home(monkeypatch):
    from llm_tools import config
    monkeypatch.setattr(config, "load_config", lambda *_: (_ for _ in ()).throw(SystemExit(2)))
    assert launcher._load_config() == {}


# --- provider picker --------------------------------------------------------

def test_picker_moves_toggles_and_keeps_toggle_order():
    state = Launcher("/w", creating=True, focus=1, installed=("codex", "claude", "copilot"), providers="codex")
    state.key(curses.KEY_RIGHT)
    state.key(" ")
    assert state.providers == "codex,claude"
    state.key(curses.KEY_RIGHT)
    state.key(curses.KEY_RIGHT)  # clamped at the last provider
    state.key(" ")
    assert state.providers == "codex,claude,copilot"
    state.key(curses.KEY_LEFT)
    state.key(curses.KEY_LEFT)
    state.key(" ")  # deselect codex
    assert state.providers == "claude,copilot"
    assert [name for name, _ in state.choices()] == ["claude", "copilot", "codex"]  # selected first, rotation order
    state.key(" ")  # the cursor followed codex; re-adding appends: rotation order is toggle order
    assert state.providers == "claude,copilot,codex"
    state.key("x")  # no free text on this field
    assert state.providers == "claude,copilot,codex"


def test_picker_with_no_installed_provider_ignores_keys():
    state = Launcher("/w", creating=True, focus=1, installed=(), providers="codex")
    state.key(" ")
    state.key(curses.KEY_RIGHT)
    assert state.providers == "codex"


def test_validation_rejects_missing_cli_with_clear_message(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/bin/codex" if name == "codex" else None)
    state = Launcher(str(tmp_path), objective="o", providers="codex,claude", installed=("codex",))
    with pytest.raises(ValueError, match=r"claude is not installed \(not on PATH\)"):
        state.validate()
    state.providers = "minimax"
    with pytest.raises(ValueError, match="minimax is not installed"):
        state.validate()
    state.providers = ""
    with pytest.raises(ValueError, match="Select at least one provider"):
        state.validate()
    state.providers = "zai"
    with pytest.raises(ValueError, match="Unknown provider zai"):
        state.validate()
    state.providers = "codex"
    state.validate()
    assert state.providers == "codex"


def test_start_button_surfaces_missing_cli_in_form(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    state = Launcher(str(tmp_path), creating=True, focus=4, objective="o", installed=())
    assert state.key("\n") is None
    assert "codex is not installed" in state.error


def test_clicking_a_provider_toggles_it():
    state = Launcher("/w", creating=True, installed=("codex", "claude"), providers="codex")
    slots = state.provider_slots()
    assert slots[0][0] == 2 + len("Providers: ")
    x = slots[1][0] + 3
    state.click(7, x=x, toggle=True)
    assert state.focus == 1 and state.providers == "codex,claude" and state.provider_cursor == 1
    state.click(7, x=x, toggle=False)  # a bare press only focuses the row
    assert state.providers == "codex,claude"
    state.click(7, x=x, activate=True)
    assert state.providers == "codex"
    state.click(7, x=0, toggle=True)  # outside every provider
    assert state.providers == "codex"


# --- hints and footers ------------------------------------------------------

def test_hint_is_specific_to_the_focused_field():
    state = Launcher("/w", creating=True, objective="line one\nline two")
    assert field(state, 0) == "Type the task · Tab moves on to Start · Ctrl-E full editor · paste supported · 17 bytes · 2 lines"
    assert field(state, 1) == "←/→ choose · Space toggle · numbers show rotation order"
    assert field(state, 2) == "Path to work in · Tab next"
    assert field(state, 3).startswith("Space/←/→ switch: Native shows the provider terminal in a station; Headless runs it without a terminal")
    assert "bytes" not in field(state, 3, width=70) and field(state, 3, width=70).endswith("Headless: no terminal")
    assert field(state, 4) == "Enter to save · no provider is started"
    assert field(state, 5) == "Starts codex now · uses provider capacity"
    state.objective = ""
    assert field(state, 0) == "Type the task · Tab moves on to Start · Ctrl-E full editor · paste supported"


def field(state, focus, width=200):
    state.focus = focus
    return launcher.field_hint(state, width)


def test_form_footer_and_hint_rendering(tmp_path):
    state = Launcher(str(tmp_path), creating=True, objective="abc", installed=("codex", "claude"), providers="codex")
    for focus, expected in [(0, "3 bytes"), (1, "Space toggle"), (2, "Path to work in"), (3, "Native shows the provider terminal"), (4, "Enter to save"),
                            (5, "Starts codex now")]:
        state.focus = focus
        grid = Grid(size=(34, 120))
        launcher._draw(grid, state)
        assert expected in grid.row(31)
        assert grid.row(33) == f"Tab/↑↓ fields · Enter {'next' if focus < 4 else 'save' if focus == 4 else 'start'} · Esc back"
        assert "Space mode" not in grid.text()
    state.focus = 0
    state.error = "boom"
    grid = Grid()
    launcher._draw(grid, state)
    assert grid.row(31) == "boom" and "bytes" not in grid.text()


def test_provider_row_renders_numbers_cursor_and_missing_clis(tmp_path):
    state = Launcher(str(tmp_path), creating=True, focus=1, installed=("codex", "claude"), providers="claude,codex")
    grid = Grid()
    launcher._draw(grid, state)
    row = grid.row(7)
    assert row.startswith("> Providers: ▸[1] claude  [2] codex")
    assert "(copilot, kilo, opencode, minimax not installed)" in row
    assert grid.cursor is None  # no text caret on the picker row
    state.installed = ()
    grid = Grid()
    launcher._draw(grid, state)
    assert "no provider CLI found on PATH" in grid.row(7)


def test_small_terminal_message_reports_current_size(tmp_path):
    grid = Grid(size=(18, 50))
    launcher._draw(grid, Launcher(str(tmp_path)))
    text = grid.text()
    assert "Enlarge the terminal to at least 60 columns and 20 rows" in text.replace("\n", " ")
    assert "now 50×18" in text


# --- formatting helpers -----------------------------------------------------

def test_relative_age_buckets():
    now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    def age(**delta): return launcher.relative_age((now - timedelta(**delta)).isoformat(), now.timestamp())
    assert age(seconds=5) == "just now"
    assert age(minutes=2, seconds=10) == "2m ago"
    assert age(hours=3, minutes=1) == "3h ago"
    assert age(days=5, hours=2) == "5d ago"
    assert age(days=800) == "2y ago"
    assert launcher.relative_age((now + timedelta(hours=1)).isoformat(), now.timestamp()) == "just now"
    assert launcher.relative_age("2026-10-29T10:00:00Z", now.timestamp()) == "2h ago"
    assert launcher.relative_age("2026-10-29T10:00:00", now.timestamp()) == "2h ago"
    assert launcher.relative_age("garbage") == "-" and launcher.relative_age(None) == "-"
    assert launcher.relative_age("2026-10-29T10:00:00+00:00").endswith("ago")


def test_truncate_pad_and_middle_ellipsis_use_cell_widths():
    assert launcher.truncate("short", 10) == "short"
    assert launcher.truncate("日本語のミッション", 7) == "日本語…"
    assert cell_width(launcher.truncate("日本語のミッション", 7)) <= 7
    assert launcher.truncate("abc\ndef", 20) == "abc def"
    assert launcher.truncate("abcdef", 1) == "…" and launcher.truncate("abcdef", 0) == ""
    assert cell_width(launcher.pad("日本", 6)) == 6 and launcher.pad("abcdef", 3) == "abcdef"
    text = "/very/long/path/that/keeps/going/to/the/end"
    clipped = launcher.middle_ellipsis(text, 20)
    assert cell_width(clipped) <= 20 and "…" in clipped
    assert clipped.startswith("/very") and clipped.endswith("the/end")
    assert launcher.middle_ellipsis(text, 1) == "…" and launcher.middle_ellipsis(text, 0) == ""
    assert launcher.middle_ellipsis("short", 20) == "short"
    assert cell_width(launcher.middle_ellipsis("日本語/パス/ディレクトリ/名前", 11)) <= 11
    assert launcher.middle_ellipsis("á" * 30, 10).count("́") >= 1


def test_short_path_collapses_home():
    assert launcher.short_path("/home/me", 30, home="/home/me") == "~"
    assert launcher.short_path("/home/me/dev/x", 30, home="/home/me/") == "~/dev/x"
    assert launcher.short_path("/home/meow/x", 30, home="/home/me") == "/home/meow/x"
    assert launcher.short_path("/etc/x", 30, home="/") == "/etc/x"
    assert launcher.short_path("/home/me/" + "d/" * 30, 20, home="/home/me").startswith("~/")
    assert launcher.short_path("~x", 30) == "~x"


def test_order_missions_groups_live_then_waiting_then_finished_with_here_first():
    items = [
        mission("old-done", "COMPLETED", "/here", timedelta(days=1), "a"),
        mission("other-new", "RUNNING", "/other", timedelta(minutes=1), "b"),
        mission("here-old", "PAUSED", "/here", timedelta(days=2), "c"),
        mission("other-cancelled", "CANCELLED", "/other", timedelta(minutes=1), "d"),
        mission("here-new", "CREATED", "/here", timedelta(minutes=3), "e"),
    ]
    ordered = [m["name"] for m in launcher.order_missions(items, "/here")]
    assert ordered == ["here-old", "other-new", "here-new", "old-done", "other-cancelled"]
    assert launcher.order_missions([], "/here") == []
    assert launcher.order_missions([{"id": "x"}], "/here") == [{"id": "x"}]


def test_status_roles_window_and_detail():
    roles = {s: launcher.status_role(s) for s in ("RUNNING", "PAUSED", "STOPPING", "FAILED", "COMPLETED", "CANCELLED", "CREATED", "?")}
    assert roles == {"RUNNING": "state_running", "PAUSED": "state_paused", "STOPPING": "state_paused",
                     "FAILED": "state_failed", "COMPLETED": "state_done", "CANCELLED": "state_paused",
                     "CREATED": "state_waiting", "?": "info"}
    assert launcher.window_start(0, 5) == 0 and launcher.window_start(4, 5) == 0 and launcher.window_start(9, 5) == 5
    detail = launcher.mission_detail(mission(cwd="/a/" + "long/" * 30), 60)
    assert detail.endswith(" · 01234567 · codex") and cell_width(detail) <= 60 and "…" in detail
    assert launcher.mission_providers({"id": "x"}) == "-"


# --- rendering the list -----------------------------------------------------

def test_list_columns_alignment_markers_and_detail(tmp_path):
    here = str(tmp_path)
    items = launcher.order_missions([
        mission("Fix flaky login test", "RUNNING", "/srv/other", timedelta(minutes=2), "11111111aaaa"),
        mission("日本語のミッション 🚀", "PAUSED", here, timedelta(hours=3), "22222222bbbb"),
        mission("x" * 200, "COMPLETED", "/srv/z", timedelta(days=5), "33333333cccc"),
    ], here)
    state = Launcher(here, missions=items, selected=1, installed=("codex",))
    grid = Grid()
    launcher._draw(grid, state)
    rows = [grid.row(y) for y in range(5, 9)]
    assert rows[0].lstrip().startswith("New mission in this folder")
    assert rows[1].startswith("> ‖ PAUSED") and "3h ago" in rows[1]
    assert rows[2].startswith("  ● RUNNING") and "2m ago" in rows[2]
    assert rows[3].startswith("  ✓ COMPLETED") and "…" in rows[3] and "5d ago" in rows[3]
    # The "here" marker sits in the folder column, and only on the current folder's row.
    assert "● /" in rows[1] and "● /" not in rows[2] and "● /" not in rows[3]
    assert all("▌ " in row for row in rows[1:])  # every mission row carries its identity chip
    ages = [row.index(" ago") for row in rows[1:]]
    # The age column is aligned by terminal cell, not by character index (CJK is wide).
    cells = [cell_width(row[:row.index(" ago")]) for row in rows[1:]]
    assert len(set(cells)) == 1, (ages, rows)
    assert grid.row(31).startswith(here[:20]) and "22222222" in grid.row(31) and "codex" in grid.row(31)
    assert "Draft kept" not in grid.text() and "4 saved" not in grid.text() and "3 saved" in grid.text()
    assert grid.row(33) == "↑↓ select · Enter open · N new mission · ? help · Q quit"


def test_live_mission_explains_how_to_reach_it_and_error_wins(tmp_path):
    live = mission(status="RUNNING")
    live["config"]["native_tui"] = True
    state = Launcher(str(tmp_path), missions=[live, mission("Done", "COMPLETED", ident="b" * 16)], selected=1,
                     probe=lambda ident: True)
    grid = Grid()
    launcher._draw(grid, state)
    assert grid.row(32) == "Enter attaches to the provider station · o opens controls"
    assert grid.row(33) == "↑↓ select · Enter attach · o controls · N new mission · ? help · Q quit"
    state.selected = 2
    grid = Grid()
    launcher._draw(grid, state)
    assert "provider station is live" not in grid.text()
    state.selected = 1
    state.error = "start failed"
    grid = Grid()
    launcher._draw(grid, state)
    assert grid.row(32) == "start failed"


def test_empty_list_new_entry_and_draft_notice(tmp_path):
    state = Launcher(str(tmp_path), objective="half written", installed=("codex",))
    grid = Grid()
    launcher._draw(grid, state)
    assert grid.row(7) == ("A mission is a goal plus ordered tasks run by Codex/Claude in this folder"
                           " · N to create one")
    assert "Draft kept · N to continue" in grid.row(3)
    assert grid.row(31).startswith("Creates a mission in")


def test_many_missions_scroll_keeps_selection_visible_and_click_maps_rows(tmp_path):
    items = [mission(f"mission-{i:02d}", cwd=f"/w/{i}", ident=f"{i:08d}xxxx") for i in range(40)]
    state = Launcher(str(tmp_path), missions=items, selected=40)
    grid = Grid(size=(24, 80))
    launcher._draw(grid, state)
    visible = [grid.row(y) for y in range(5, 20)]
    assert any(row.startswith("> ") and "mission-39" in row for row in visible)
    assert state.click(5, height=24) is None and state.selected == 40 - 14
    assert state.click(6, height=24, activate=True) == "open"


def test_list_keys_clear_stale_error():
    state = Launcher("/w", missions=[mission()], error="old")
    state.key(curses.KEY_DOWN)
    assert state.error == "" and state.selected == 1


# --- post-create / post-start control flow ----------------------------------

class Recorder:
    """Curses stand-in that records each drawn screen for the final assertions."""

    def __init__(self, keys, monkeypatch):
        self.window = Grid(keys)
        self.screens: list[str] = []
        monkeypatch.setattr(curses, "wrapper", lambda callback: callback(self.window))
        original = launcher._draw

        def draw(window, state):
            original(window, state)
            self.screens.append(window.text())
        monkeypatch.setattr(launcher, "_draw", draw)


def start_keys(extra=()):
    return ["n", *"Do the thing", "\t", "\t", "\t", "\t", "\t", "\n", *extra]  # Tab to the last row, Start


def prepare(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(launcher, "detect_installed", lambda: ("codex",))
    monkeypatch.setattr(launcher, "provider_installed", lambda name: name == "codex")
    monkeypatch.setattr(launcher, "_load_config", lambda: {})


def run_with(dispatch_start, tmp_path, monkeypatch, keys):
    prepare(tmp_path, monkeypatch)
    recorder = Recorder(keys, monkeypatch)
    calls: list[list[str]] = []

    def dispatch(args):
        calls.append(args)
        if args[0] == "start":
            return dispatch_start(args)
        if args[0] == "control":
            return 0
        return bridge.main(args)

    return launcher.run_launcher(dispatch), calls, recorder


def set_status(mission_id, status):
    with bridge.open_store() as store:
        if status != "CREATED":
            store.set_mission_status(mission_id, status)


def test_native_start_returning_nonzero_after_launch_opens_controls_not_the_form(tmp_path, monkeypatch):
    def start(args):
        set_status(args[2], "RUNNING")
        print("station closed", file=sys.stderr)
        return 1  # e.g. the station was force-stopped while attached
    status, calls, recorder = run_with(start, tmp_path, monkeypatch, start_keys())
    assert status == 0
    assert [c[0] for c in calls] == ["mission", "start", "control"]
    assert calls[1][3] == "--native" and calls[2][:2] == ["control", "--mission"]
    assert not any("Mission was saved" in screen for screen in recorder.screens)
    with bridge.open_store() as store:
        assert len(store.list_missions()) == 1


def test_headless_start_failing_after_launch_also_opens_controls(tmp_path, monkeypatch):
    def start(args):
        set_status(args[2], "RUNNING")
        return 3
    keys = ["n", *"Go", "\t", "\t", "\t", " ", "\t", "\t", "\n"]  # Space on Mode selects Headless, then Tab to Start
    status, calls, _ = run_with(start, tmp_path, monkeypatch, keys)
    assert status == 0
    assert [c[0] for c in calls] == ["mission", "start", "control"]
    assert calls[1][3] == "--headless"


def test_start_failing_before_launch_returns_to_list_with_real_error(tmp_path, monkeypatch):
    def start(args):
        print("llm-bridge: codex session could not be created", file=sys.stderr)
        return 2
    status, calls, recorder = run_with(start, tmp_path, monkeypatch, start_keys(["n", "\x04"]))
    assert status == 0
    assert [c[0] for c in calls] == ["mission", "start"]  # no control, no second create
    final = recorder.screens
    failed = [s for s in final if "codex session could not be created" in s]
    assert failed and "Choose a mission" in failed[0] and "Objective:" not in failed[0]
    assert "> " in failed[0] and "CREATED" in failed[0]
    # Pressing N again opens an empty form: the objective was cleared after creation.
    assert recorder.window.row(5).rstrip() == "> Objective:"


def test_start_failure_without_message_gets_retry_guidance(tmp_path, monkeypatch):
    status, calls, recorder = run_with(lambda args: 2, tmp_path, monkeypatch, start_keys(["q"]))
    assert status == 0
    assert any("Mission was saved but could not start" in s for s in recorder.screens)
    assert [c[0] for c in calls] == ["mission", "start"]


def test_start_failure_with_unreadable_mission_status_is_treated_as_not_launched(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch)
    Recorder(start_keys(["\x04"]), monkeypatch)
    calls = []

    def dispatch(args):
        calls.append(args[0])
        if args[0] == "mission":
            print('{"mission": {"id": "ghost"}}')
        return 5 if args[0] == "start" else 0

    assert launcher.run_launcher(dispatch) == 0
    assert calls == ["mission", "start"]


def test_save_only_clears_objective_and_never_starts(tmp_path, monkeypatch):
    status, calls, recorder = run_with(lambda args: pytest.fail("save-only started work"), tmp_path, monkeypatch,
                                       ["n", *"Save me", "\t", "\t", "\t", "\t", "\n", "o"])
    assert status == 0
    assert [c[0] for c in calls] == ["mission", "control"]


def test_opening_an_existing_live_mission_only_opens_controls(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch)
    with bridge.open_store() as store:
        created = store.create_mission("Live", "objective", cwd=tmp_path)
        store.set_mission_status(created["id"], "RUNNING")
    Recorder([curses.KEY_DOWN, "\n"], monkeypatch)
    calls = []
    assert launcher.run_launcher(lambda args: calls.append(args) or 0) == 0
    assert calls == [["control", "--mission", created["id"]]]


def test_missing_cli_blocks_start_at_the_form(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "provider_installed", lambda name: False)
    monkeypatch.setattr(launcher, "detect_installed", lambda: ())
    recorder = Recorder(start_keys(["\x04"]), monkeypatch)
    calls = []
    assert launcher.run_launcher(lambda args: calls.append(args) or 0) == 0
    assert calls == []
    assert any("codex is not installed" in screen for screen in recorder.screens)


def test_defaults_come_from_config_and_ordering_from_current_folder(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "detect_installed", lambda: ("codex", "claude"))
    monkeypatch.setattr(launcher, "_load_config", lambda: {"ralph": {"providers": ["claude", "codex"]}})
    with bridge.open_store() as store:
        store.create_mission("Elsewhere", "o", cwd=tmp_path)
        store.create_mission("Here", "o", cwd=tmp_path / "work")
    recorder = Recorder(["n", "\x04"], monkeypatch)
    assert launcher.run_launcher(lambda args: pytest.fail(args)) == 0
    first = recorder.screens[0]
    assert first.index("Here") < first.index("Elsewhere")
    assert any("[1] claude" in s and "[2] codex" in s for s in recorder.screens)


def test_mouse_click_on_provider_toggles_it_in_the_running_launcher(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "detect_installed", lambda: ("codex", "claude"))
    probe = Launcher(str(tmp_path), installed=("codex", "claude"))
    x = probe.provider_slots()[1][0] + 2
    monkeypatch.setattr(curses, "getmouse", lambda: (0, x, 7, 0, curses.BUTTON1_CLICKED))
    recorder = Recorder(["n", curses.KEY_MOUSE, "\x04"], monkeypatch)
    assert launcher.run_launcher(lambda args: pytest.fail(args)) == 0
    row = [line for line in recorder.screens[-1].splitlines() if "Providers:" in line][0]
    assert "[1] codex" in row and "[2] claude" in row


class ScriptedWindow(Grid):
    """A window whose get_wch plays a script: a key, curses.error (no input), or a callable that may resize first."""

    def __init__(self, steps):
        super().__init__()
        self.steps = list(steps)

    def get_wch(self):
        step = self.steps.pop(0)
        if callable(step):
            step = step(self)
        if isinstance(step, Exception):
            raise step
        return step


def test_escape_pending_when_the_terminal_shrinks_does_not_time_out_into_quit(tmp_path, monkeypatch):
    """An Escape read while the terminal was large, followed by keys dropped because it became too small, is not a lone Escape."""
    prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "_ESCAPE_SECONDS", 0.0)  # the pending Escape counts as timed out at the next idle read

    def shrink(window):
        window.size = (10, 40)
        return "["

    def enlarge(window):
        window.size = (34, 120)
        return "j"  # harmless on the list; proves the home screen is still running

    window = ScriptedWindow(["\x1b", shrink, curses.error("no input"), enlarge, "q"])
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    assert launcher.run_launcher(lambda args: 0) == 0
    assert window.steps == [], "the home screen quit on the stale Escape before reading the remaining keys"
