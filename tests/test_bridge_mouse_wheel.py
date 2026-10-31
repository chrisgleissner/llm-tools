"""Mouse wheel scrolling on the home screen and the standalone document viewer."""
from __future__ import annotations

import curses
from datetime import timedelta
import fcntl
import importlib
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

from llm_tools import bridge_launcher as launcher, bridge_mouse as mouse, bridge_tui as tui, bridge_viewer
from llm_tools.bridge_launcher import Launcher
from llm_tools.bridge_store import BridgeStore

from tests import test_bridge_launcher_home as home
from tests.test_bridge_viewer import Window
from tests.conftest import scaled
from tests.process_hygiene import terminate_process_tree

Grid, mission = home.Grid, home.mission

UP, DOWN = mouse.WHEEL_UP, mouse.WHEEL_DOWN
SGR_UP, SGR_DOWN = b"\x1b[<64;10;8M", b"\x1b[<65;10;8M"
SGR_SHIFT_DOWN = b"\x1b[<69;10;8M"


@pytest.fixture(autouse=True)
def _terminal(monkeypatch):
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(tui, "initialize_colors", lambda: False)


def listing(count=40, **kw):
    items = [mission(f"Mission {i}", "COMPLETED", "/w", timedelta(minutes=i + 1), f"{i:08d}abcdef") for i in range(count)]
    return Launcher("/w", missions=items, installed=("codex",), **kw)


def routed(count_routes=8, **kw):
    routes = tuple(launcher.RouteChoice(f"route{i}", "codex") for i in range(count_routes))
    return Launcher("/w", creating=True, focus=0, installed=("codex", "claude"), routes=routes, providers="codex", **kw)


# --- decoding ---------------------------------------------------------------

def test_wheel_delta_maps_buttons_to_directions():
    assert mouse.wheel_delta(UP) == (-1, 0)
    assert mouse.wheel_delta(DOWN) == (1, 0)
    assert mouse.wheel_delta(DOWN | UP | curses.REPORT_MOUSE_POSITION) == (-1, 0)
    assert mouse.wheel_delta(UP | mouse.SHIFT) == (0, -1)
    assert mouse.wheel_delta(DOWN | mouse.SHIFT) == (0, 1)
    for other in (0, curses.BUTTON1_CLICKED, curses.BUTTON1_DOUBLE_CLICKED, curses.REPORT_MOUSE_POSITION):
        assert mouse.wheel_delta(other) is None


def test_horizontal_wheel_buttons_are_used_when_the_build_has_them(monkeypatch):
    monkeypatch.setattr(mouse, "WHEEL_LEFT", 0x1000000)
    monkeypatch.setattr(mouse, "WHEEL_RIGHT", 0x2000000)
    assert mouse.wheel_delta(0x1000000) == (0, -1)
    assert mouse.wheel_delta(0x2000000) == (0, 1)


def test_missing_button5_falls_back_to_the_ncurses_bit_value(monkeypatch):
    monkeypatch.delattr(curses, "BUTTON5_PRESSED")
    monkeypatch.delattr(curses, "BUTTON4_PRESSED")
    try:
        reloaded = importlib.reload(mouse)
        assert reloaded.WHEEL_DOWN == 0x200000 and reloaded.WHEEL_UP == 0x10000
    finally:
        monkeypatch.undo()
        importlib.reload(mouse)


def test_enable_mouse_requests_wheel_and_position_events_and_tolerates_failure(monkeypatch):
    seen = []
    monkeypatch.setattr(curses, "mousemask", lambda mask: seen.append(mask))
    assert mouse.enable_mouse()
    assert seen == [curses.ALL_MOUSE_EVENTS]
    monkeypatch.setattr(curses, "mousemask", lambda mask: (_ for _ in ()).throw(curses.error("no mouse")))
    assert not mouse.enable_mouse()


def test_read_mouse_returns_none_when_the_event_is_gone(monkeypatch):
    monkeypatch.setattr(curses, "getmouse", lambda: (0, 3, 4, 0, DOWN))
    assert mouse.read_mouse() == (3, 4, DOWN)
    monkeypatch.setattr(curses, "getmouse", lambda: (_ for _ in ()).throw(curses.error("empty")))
    assert mouse.read_mouse() is None


# --- mission list -----------------------------------------------------------

def test_wheel_moves_the_mission_selection_like_the_arrow_keys_and_clamps():
    state = listing(40)
    state.wheel(1)
    state.wheel(1)
    assert state.selected == 2
    state.wheel(-1)
    assert state.selected == 1
    for _ in range(5):
        state.wheel(-1)
    assert state.selected == 0  # the "New mission" row is the first stop, as with Up
    for _ in range(100):
        state.wheel(1)
    assert state.selected == 40


def test_wheel_selection_stays_inside_the_visible_window_of_a_long_list():
    state = listing(40)
    grid = Grid(size=(24, 80))
    for _ in range(30):
        state.wheel(1)
        launcher._draw(grid, state)
        assert f"Mission {state.selected - 1}" in grid.text()
    assert not state.creating  # scrolling never opens anything


def test_wheel_moves_the_selection_while_the_filter_is_being_typed():
    state = listing(5)
    state.key("/")
    state.key("m")
    state.wheel(1)
    state.wheel(1)
    assert state.filtering and state.filter_text == "m" and state.selected == 3
    state.wheel(-1)
    assert state.selected == 2


def test_wheel_on_an_empty_list_does_nothing_harmful():
    state = Launcher("/w", missions=[], installed=("codex",))
    state.wheel(1)
    state.wheel(-1)
    assert state.selected == 0 and not state.creating


# --- new-mission form -------------------------------------------------------

def test_wheel_scrolls_a_focused_checklist_and_stops_at_its_edges():
    state = routed()
    state.focus = 1
    state.vertical = True
    count = len(state.choices())
    for _ in range(count + 5):
        state.wheel(1)
    assert state.focus == 1 and state.provider_cursor == count - 1
    for _ in range(count + 5):
        state.wheel(-1)
    assert state.focus == 1 and state.provider_cursor == 0
    assert state.providers == "codex"  # the wheel never toggles a provider


def test_wheel_scrolls_a_clipped_checklist_into_view():
    state = routed(count_routes=14)
    state.focus = 1
    grid = Grid(size=(24, 80))
    launcher._draw(grid, state)
    last = state.choices()[-1][0]  # the unselected entries are alphabetical
    assert last not in grid.text()
    for _ in range(len(state.choices())):
        state.wheel(1)
    launcher._draw(grid, state)
    assert last in grid.text()


def test_wheel_over_the_checklist_focuses_and_scrolls_it():
    state = routed()
    state.width = 100
    layout = state.form_layout(34, 100)
    assert layout.vertical and state.focus == 0
    _index, row = layout.items[3]
    state.wheel(1, y=row, height=34)
    assert state.focus == 1 and state.provider_cursor == 1


def test_wheel_elsewhere_in_the_form_moves_between_fields_like_up_and_down():
    state = routed()
    state.width = 100
    state.wheel(1, y=0, height=34)
    assert state.focus == 1 and state.provider_cursor == 0  # keyboard Down also focuses the checklist first
    state.focus = 0
    state.wheel(-1, y=0, height=34)
    assert state.focus == 0
    plain = Launcher("/w", creating=True, installed=("codex",), providers="codex")
    plain.width = 100
    for _ in range(10):
        plain.wheel(1)
    assert plain.focus == 5 and plain.creating  # clamped on the last button, which it never presses
    plain.wheel(-1)
    assert plain.focus == 4


def test_wheel_never_presses_a_button_or_edits_text():
    state = Launcher("/w", creating=True, installed=("codex",), providers="codex", objective="keep")
    for _ in range(8):
        state.wheel(1)
    assert state.objective == "keep" and state.providers == "codex" and state.creating and state.focus == 5


def test_wheel_is_ignored_inside_the_burst_window_of_typed_input():
    state = routed(burst_window=10.0)
    state.key("a")
    state.wheel(1)
    assert state.focus == 0 and state.objective == "a"
    state.last_input = time.monotonic() - 11
    state.wheel(1)
    assert state.focus == 1


def test_wheel_does_not_restart_the_burst_window():
    state = Launcher("/w", creating=True, installed=("codex",), providers="codex", burst_window=10.0)
    state.last_input = time.monotonic() - 60
    before = state.last_input
    state.wheel(1)
    assert state.focus == 1 and state.last_input == before


# --- event dispatch ---------------------------------------------------------

def patch_mouse(monkeypatch, x=5, y=8, buttons=DOWN):
    monkeypatch.setattr(curses, "getmouse", lambda: (0, x, y, 0, buttons))


def test_handle_mouse_applies_wheel_and_leaves_clicks_unchanged(monkeypatch):
    state = listing(5)
    patch_mouse(monkeypatch, buttons=DOWN)
    assert launcher.handle_mouse(state, 24) is None and state.selected == 1
    patch_mouse(monkeypatch, buttons=UP | curses.REPORT_MOUSE_POSITION)
    assert launcher.handle_mouse(state, 24) is None and state.selected == 0
    patch_mouse(monkeypatch, y=launcher._BODY_Y + 2, buttons=curses.BUTTON1_CLICKED)
    assert launcher.handle_mouse(state, 24) is None and state.selected == 2
    patch_mouse(monkeypatch, y=launcher._BODY_Y + 3, buttons=curses.BUTTON1_DOUBLE_CLICKED)
    assert launcher.handle_mouse(state, 24) == "open" and state.selected == 3
    patch_mouse(monkeypatch, buttons=curses.REPORT_MOUSE_POSITION)
    assert launcher.handle_mouse(state, 24) is None and state.selected == 3


def test_handle_mouse_ignores_horizontal_wheel_and_a_vanished_event(monkeypatch):
    state = listing(5)
    patch_mouse(monkeypatch, buttons=DOWN | mouse.SHIFT)
    assert launcher.handle_mouse(state, 24) is None and state.selected == 0
    monkeypatch.setattr(curses, "getmouse", lambda: (_ for _ in ()).throw(curses.error("gone")))
    assert launcher.handle_mouse(state, 24) is None


def test_handle_mouse_consumes_but_ignores_events_when_told_to(monkeypatch):
    state = listing(5)
    calls = []
    monkeypatch.setattr(curses, "getmouse", lambda: calls.append(1) or (0, 5, 8, 0, DOWN))
    assert launcher.handle_mouse(state, 24, ignore=True) is None
    assert calls == [1] and state.selected == 0


def test_running_launcher_wheel_moves_selection_and_enter_opens_that_mission(tmp_path, monkeypatch):
    home.prepare(tmp_path, monkeypatch)
    work = tmp_path / "work"
    with BridgeStore(tmp_path / "state" / "bridge.sqlite3") as store:
        for index in range(6):
            store.create_mission(f"Wheel {index}", "obj", cwd=work)
    expected = [str(m["id"]) for m in launcher.order_missions(launcher._load_home(str(work))[0], str(work))]
    patch_mouse(monkeypatch, buttons=DOWN)
    home.Recorder([curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_MOUSE, "\n"], monkeypatch)
    calls = []
    assert launcher.run_launcher(lambda args: calls.append(args) or 0) == 0
    assert calls == [["control", "--mission", expected[2]]]


def test_mouse_event_inside_a_bracketed_paste_takes_no_action(tmp_path, monkeypatch):
    home.prepare(tmp_path, monkeypatch)
    consumed = []
    monkeypatch.setattr(curses, "getmouse", lambda: consumed.append(1) or (0, 5, 8, 0, DOWN))
    monkeypatch.setattr(Launcher, "wheel", lambda *_a, **_k: pytest.fail("wheel applied during a paste"))
    initial = []
    monkeypatch.setattr(tui, "compose_document", lambda text, **_k: initial.append(text))
    keys = ["n", "\x1b", "[200~", "a", "b", curses.KEY_MOUSE, "c", "\x1b", "[201~", "\x04"]
    home.Recorder(keys, monkeypatch)
    assert launcher.run_launcher(lambda args: pytest.fail(args)) == 0
    assert consumed == [1]  # the event was read and dropped, never replayed later
    assert initial == ["abc"]


# --- standalone viewer ------------------------------------------------------

def run_viewer(monkeypatch, tmp_path, actions, *, text, follow=False, wrap=True, size=(24, 80)):
    path = tmp_path / "doc.txt"
    path.write_text(text)
    seen = []

    def draw(_window, state):
        viewer = state.document_viewer
        seen.append((viewer.top, viewer.horizontal, state.document_notice))

    if not wrap:
        actions.insert(0, "w")
    monkeypatch.setattr(bridge_viewer.curses, "wrapper", lambda callback: callback(Window(actions, size=size)))
    monkeypatch.setattr("llm_tools.bridge_tui.initialize_colors", lambda: None)
    monkeypatch.setattr("llm_tools.bridge_tui._draw_document", draw)
    assert bridge_viewer.run_document(bridge_viewer.read_source(path), title="Doc", path=path, follow=follow) == 0
    return seen


LONG = "".join(f"line {i} " + "x" * 120 + "\n" for i in range(100))
LINES = "".join(f"l{i}\n" for i in range(100))


def test_viewer_wheel_scrolls_three_lines_per_notch(monkeypatch, tmp_path):
    patch_mouse(monkeypatch, buttons=DOWN)
    seen = run_viewer(monkeypatch, tmp_path, [curses.KEY_MOUSE, curses.KEY_MOUSE, "q"], text=LINES)
    assert seen[-1][0] == 6
    patch_mouse(monkeypatch, buttons=UP)
    seen = run_viewer(monkeypatch, tmp_path, [curses.KEY_MOUSE, "q"], text=LINES)
    assert seen[-1][0] == 0


def test_viewer_wheel_clamps_at_the_end_of_a_short_document(monkeypatch, tmp_path):
    patch_mouse(monkeypatch, buttons=DOWN)
    seen = run_viewer(monkeypatch, tmp_path, [curses.KEY_MOUSE] * 5 + ["q"], text="a\nb\nc\nd\n")
    assert seen[-1][0] <= 4


def test_viewer_shift_wheel_pans_only_when_wrap_is_off(monkeypatch, tmp_path):
    patch_mouse(monkeypatch, buttons=DOWN | mouse.SHIFT)
    seen = run_viewer(monkeypatch, tmp_path, [curses.KEY_MOUSE, "q"], text=LONG)
    assert seen[-1][1] == 0  # wrapped: nothing to pan
    seen = run_viewer(monkeypatch, tmp_path, [curses.KEY_MOUSE, curses.KEY_MOUSE, "q"], text=LONG, wrap=False)
    assert seen[-1][1] == 16
    patch_mouse(monkeypatch, buttons=UP | mouse.SHIFT)
    seen = run_viewer(monkeypatch, tmp_path, [curses.KEY_MOUSE, "q"], text=LONG, wrap=False)
    assert seen[-1][1] == 0


def test_viewer_wheel_is_ignored_while_typing_a_search_and_after_a_vanished_event(monkeypatch, tmp_path):
    patch_mouse(monkeypatch, buttons=DOWN)
    text = "".join(f"a{i}\n" for i in range(50))
    seen = run_viewer(monkeypatch, tmp_path, ["/", "a", curses.KEY_MOUSE, "\n", "q"], text=text)
    assert all(top == 0 for top, _h, _n in seen)
    monkeypatch.setattr(curses, "getmouse", lambda: (_ for _ in ()).throw(curses.error("gone")))
    seen = run_viewer(monkeypatch, tmp_path, [curses.KEY_MOUSE, "q"], text=text)
    assert seen[-1][0] == 0


def test_viewer_wheel_selects_one_heading_per_notch_in_the_heading_list(monkeypatch, tmp_path):
    patch_mouse(monkeypatch, buttons=DOWN)
    text = "".join(f"# Heading {i}\n\ntext\n\n" for i in range(10))
    seen = run_viewer(monkeypatch, tmp_path, ["h", curses.KEY_MOUSE, curses.KEY_MOUSE, "\n", "q"], text=text)
    assert seen[-1][0] > 0


def test_viewer_wheel_up_pauses_following_like_the_up_key(monkeypatch, tmp_path):
    patch_mouse(monkeypatch, buttons=UP)
    seen = run_viewer(monkeypatch, tmp_path, [curses.KEY_MOUSE, "q"], text=LINES, follow=True)
    assert any("Paused" in notice for _t, _h, notice in seen)


# --- real pseudo-terminal ---------------------------------------------------

def pty_session(code, env, keys, *, cwd, size=(30, 100), gap=0.15):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))
    root = str(Path(__file__).resolve().parents[1])
    env = dict(env, PYTHONPATH=os.pathsep.join(filter(None, (root, env.get("PYTHONPATH", "")))),
               TERM="xterm-256color", LLM_TOOLS_NO_INHIBIT="1", LANG="C.UTF-8", LC_ALL="C.UTF-8")
    process = subprocess.Popen([sys.executable, "-c", code], cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                               start_new_session=True)
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
            pump(gap)
        pump(0.5)
        try:
            process.wait(timeout=scaled(5))
        except subprocess.TimeoutExpired:
            pytest.fail(f"did not exit; output={bytes(output)!r}")
    finally:
        try:
            terminate_process_tree(process)  # SIGTERM, then SIGKILL, on every exit path
        finally:
            os.close(master)
    return bytes(output).decode("utf-8", "replace")


DRIVER = ("import os, sys\nfrom llm_tools import bridge_launcher as l\n"
          "def dispatch(a):\n"
          "    with open(os.environ['DISPATCH_LOG'], 'a') as log:\n"
          "        log.write(repr(a) + '\\n')\n"
          "    if a[:2] == ['mission', 'create']:\n"
          "        print('{\"mission\": {\"id\": \"m1\"}}')\n"
          "    return 0\n"
          "sys.exit(l.run_launcher(dispatch))\n")


def test_pty_sgr_wheel_moves_the_home_selection(tmp_path, env, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state_dir))
    with BridgeStore(state_dir / "bridge.sqlite3") as store:
        for index in range(30):
            store.create_mission(f"Wheel {index:02d}", "obj", cwd=work)
    ordered = [str(m["id"]) for m in launcher.order_missions(launcher._load_home(str(work))[0], str(work))]
    env.update(LLM_BRIDGE_STATE_DIR=str(state_dir), DISPATCH_LOG=str(tmp_path / "dispatch.log"))
    # Seven notches down and two up select the fifth mission; Enter opens it.
    output = pty_session(DRIVER, env, [SGR_DOWN] * 7 + [SGR_UP] * 2 + [b"\r"], cwd=work)
    assert (tmp_path / "dispatch.log").read_text().splitlines() == [repr(["control", "--mission", ordered[4]])], output


def test_pty_sgr_wheel_scrolls_the_provider_checklist(tmp_path, env):
    work = tmp_path / "work"
    work.mkdir()
    bins = tmp_path / "fakebin"
    bins.mkdir()
    for name in ("codex", "claude", "copilot", "kilo"):
        (bins / name).write_text("#!/bin/sh\nexit 0\n")
        (bins / name).chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text('[routes.claude-sonnet]\nprovider = "claude"\nmodel = "claude-sonnet-5-5"\n'
                      '[routes.kilo-glm]\nprovider = "kilo"\nmodel = "zai/glm-5.2"\n')
    env.update(LLM_BRIDGE_STATE_DIR=str(tmp_path / "state"), PATH=f"{bins}:/usr/bin:/bin", LLM_TOOLS_CONFIG=str(config),
               DISPATCH_LOG=str(tmp_path / "dispatch.log"))
    # Objective text, Tab to the checklist, wheel down three times (the selected codex is on top, then claude,
    # claude-sonnet and copilot alphabetically), Space ticks copilot, then Tab to Save and press Enter.
    keys = [b"n", b"Fix it", b"\t", SGR_DOWN, SGR_DOWN, SGR_DOWN, b" ", b"\t", b"\t", b"\t", b"\r", b"\x04"]
    output = pty_session(DRIVER, env, keys, cwd=work, gap=0.2)
    creation = (tmp_path / "dispatch.log").read_text().splitlines()[0]
    assert "mission" in creation and "'codex,copilot'" in creation, output


def test_pty_sgr_wheel_scrolls_the_standalone_viewer(tmp_path, env):
    doc = tmp_path / "doc.txt"
    doc.write_text("".join(f"unique-line-{i:03d}\n" for i in range(200)))
    code = ("import sys\nfrom pathlib import Path\nfrom llm_tools import bridge_viewer as v\n"
            f"sys.exit(v.run_document(v.read_source(Path({str(doc)!r})), title='Doc'))\n")
    output = pty_session(code, env, [SGR_DOWN] * 20 + [b"q"], cwd=tmp_path)
    assert "unique-line-055" in output and "unique-line-150" not in output  # 20 notches x 3 lines
    output = pty_session(code, env, [SGR_DOWN] * 20 + [SGR_UP] * 20 + [b"q"], cwd=tmp_path)
    assert "unique-line-055" in output


def test_pty_shift_wheel_pans_the_viewer_when_wrap_is_off(tmp_path, env):
    doc = tmp_path / "wide.txt"
    doc.write_text("".join(f"{'.' * 150}MARK-{i:02d}\n" for i in range(40)))
    code = ("import sys\nfrom pathlib import Path\nfrom llm_tools import bridge_viewer as v\n"
            f"sys.exit(v.run_document(v.read_source(Path({str(doc)!r})), title='Doc'))\n")
    output = pty_session(code, env, [b"w"] + [SGR_SHIFT_DOWN] * 20 + [b"q"], cwd=tmp_path)
    assert "MARK-00" in output
