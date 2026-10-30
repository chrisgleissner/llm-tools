"""Mouse wheel, Esc latency and composer footer: console, overlays, document viewer and composer."""

from __future__ import annotations

import curses
import os
import shutil
import time

import pytest

from llm_tools import bridge_tui
from llm_tools.bridge_documents import DocumentViewer

from .test_bridge_tui_experience import Screen, event, full_snapshot, make_state
from tests.conftest import wait_for as _shared_wait_for


@pytest.fixture(autouse=True)
def _quiet_curses(monkeypatch):
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "curs_set", lambda _value: None)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "2000000000")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)


UP, DOWN = -1, 1


# --- constants and decoding -------------------------------------------------------------------

def test_escape_delay_and_shared_mouse_helper_tolerate_missing_curses_features(monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(bridge_tui.curses, "set_escdelay", lambda value: calls.append(value), raising=False)
    bridge_tui._set_escape_delay()
    assert calls == [50]
    monkeypatch.delattr(bridge_tui.curses, "set_escdelay", raising=False)
    bridge_tui._set_escape_delay()  # builds without set_escdelay keep ncurses' default

    def refuse(*_args):
        raise curses.error("no escdelay")

    monkeypatch.setattr(bridge_tui.curses, "set_escdelay", refuse, raising=False)
    bridge_tui._set_escape_delay()


# --- the state's wheel handling, per surface ------------------------------------------------------

def long_snapshot() -> dict[str, object]:
    snapshot = full_snapshot()
    snapshot["tasks"] = [{"id": number, "title": f"task {number}", "status": "READY", "prompt": f"prompt {number}"} for number in range(1, 13)]
    snapshot["stations"] = [{"id": f"p{i}", "name": f"p{i}", "model": "m"} for i in range(5)]
    snapshot["operations"] = [{"id": f"op{i:06d}", "task_id": i, "station": "p0", "status": "EXITED"} for i in range(40)]
    return snapshot


def test_wheel_scrolls_content_views_by_three_rows_and_never_changes_view() -> None:
    state = make_state(long_snapshot(), view="Sessions")
    screen = Screen(12, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    first_row = screen.lines[4]
    state.wheel(DOWN)
    assert state.selected == 3 and state.view == "Sessions"
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[4] != first_row and state._scroll_limit > 0
    state.wheel(UP)
    state.wheel(UP)
    assert state.selected == 0
    for _ in range(50):
        state.wheel(DOWN)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert state.selected == state._scroll_limit  # clamped to the last page by the next draw


def test_wheel_in_overview_with_everything_visible_is_harmless() -> None:
    state = make_state(full_snapshot())
    screen = Screen(34, 100)
    state.wheel(DOWN)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert state.selected == 0 and state.view == "Overview"


def test_wheel_moves_list_selection_and_keeps_it_visible() -> None:
    state = make_state(long_snapshot(), view="Tasks")
    state.wheel(DOWN)
    assert state.selected == 3 and state.selected_task()["id"] == "4"  # type: ignore[index]
    state.wheel(UP)
    state.wheel(UP)
    assert state.selected == 0
    for _ in range(10):
        state.wheel(DOWN)
    assert state.selected == 11  # clamped to the last task
    screen = Screen(16, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert any(line.startswith("> 12") for line in screen.lines)  # the selected row is on screen
    providers = make_state(long_snapshot(), view="Providers")
    providers.wheel(DOWN)
    assert providers.selected == 1  # one block per notch
    for _ in range(10):
        providers.wheel(DOWN)
    assert providers.selected == 4
    actions = make_state(long_snapshot(), view="Actions")
    actions.wheel(DOWN)
    assert actions.selected == 3
    for _ in range(40):
        actions.wheel(DOWN)
    assert actions.selected == len(actions.palette_actions()) - 1


def test_wheel_scrolls_the_task_detail() -> None:
    state = make_state(long_snapshot(), view="Tasks")
    state.handle_key("\n")
    assert state.task_detail
    state.wheel(DOWN)
    assert state.detail_scroll == 3
    state.wheel(UP)
    state.wheel(UP)
    assert state.detail_scroll == 0 and state.view == "Tasks"


def test_wheel_pages_through_activity_past_either_end() -> None:
    snapshot = full_snapshot()
    snapshot["events"] = [event("task-added", {"title": str(i)}, task_id=i, id_=i) for i in range(1, 21)]
    state = make_state(snapshot, view="Activity")
    screen = Screen(14, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert state._scroll_limit > 0
    state.wheel(DOWN)
    state.wheel(DOWN)
    assert state.selected == 6 and state.event_offset == 0
    state.wheel(UP)
    assert state.selected == 3
    state.wheel(UP)
    state.wheel(UP)  # scrolled past the top: older events, like PgUp
    assert state.event_offset == 20 and state.event_anchor == 20 and state.selected == 0
    state.wheel(UP)
    assert state.event_offset == 40
    state.selected = state._scroll_limit
    state.wheel(DOWN)  # past the end of an older page: back toward newer events
    assert state.event_offset == 20
    state.wheel(DOWN)
    state.selected = state._scroll_limit
    state.wheel(DOWN)
    assert state.event_offset == 0 and state.event_anchor is None
    state.selected = state._scroll_limit
    state.wheel(DOWN)  # already at the newest page
    assert state.event_offset == 0


def test_wheel_scrolls_the_response_overlay_but_not_a_confirmation() -> None:
    state = make_state(view="Overview")
    state.output.extend(f"line {i}" for i in range(60))
    state.response_overlay = True
    screen = Screen(10, 60)
    bridge_tui._draw_response_overlay(screen, state)  # type: ignore[arg-type]
    state.wheel(DOWN)
    assert state.response_scroll == 3
    bridge_tui._draw_response_overlay(screen, state)  # type: ignore[arg-type]
    assert any(line.startswith("line 3") for line in screen.lines)
    state.wheel(UP)
    state.wheel(UP)
    assert state.response_scroll == 0
    state.confirm_command = "cancel"
    state.wheel(DOWN)
    assert state.response_scroll == 0  # the question stays put until it is answered


def test_wheel_scrolls_the_document_viewer_and_its_heading_list() -> None:
    state = make_state(view="Tasks")
    state.document_viewer = DocumentViewer("t", "\n".join(f"line {i}" for i in range(100)))
    state.document_viewer.viewport_height = 10
    state.wheel(DOWN)
    assert state.document_viewer.top == 3
    state.wheel(UP)
    state.wheel(UP)
    assert state.document_viewer.top == 0
    for _ in range(100):
        state.wheel(DOWN)
    assert state.document_viewer.top == 90  # stops at the last page
    state.document_viewer = DocumentViewer("t", "# a\n\n# b\n\n# c")
    state.document_heading_list = True
    state.wheel(DOWN)
    state.wheel(DOWN)
    assert state.document_heading_selected == 2
    state.wheel(DOWN)
    assert state.document_heading_selected == 2
    state.wheel(UP)
    assert state.document_heading_selected == 1


# --- the console loop ---------------------------------------------------------------------------

class LoopWindow(Screen):
    def __init__(self, keys) -> None:
        super().__init__(24, 100)
        self.keys = iter(keys)

    def keypad(self, _enabled: bool) -> None:
        pass

    def timeout(self, _milliseconds: int) -> None:
        pass

    def get_wch(self):
        return next(self.keys)


def run_loop(monkeypatch, keys, mouse_events, view="Overview") -> list[tuple[str, object]]:
    seen: list[tuple[str, object]] = []
    window = LoopWindow(keys)
    events = iter(mouse_events)
    monkeypatch.setattr(bridge_tui.curses, "getmouse", lambda: next(events))
    monkeypatch.setattr(bridge_tui.curses, "wrapper", lambda callback: callback(window))
    masks: list[int] = []
    delays: list[int] = []
    monkeypatch.setattr(bridge_tui.curses, "mousemask", lambda mask: masks.append(mask))
    monkeypatch.setattr(bridge_tui.curses, "set_escdelay", lambda value: delays.append(value), raising=False)
    original_wheel = bridge_tui._ConsoleState.wheel
    original_hscroll = bridge_tui._ConsoleState.hscroll
    original_mouse = bridge_tui._ConsoleState.handle_mouse

    def wheel(self, direction):
        seen.append(("wheel", direction))
        original_wheel(self, direction)

    def hscroll(self, direction):
        seen.append(("hscroll", direction))
        original_hscroll(self, direction)

    def mouse(self, y, x, height, width):
        seen.append(("click", (y, x)))
        original_mouse(self, y, x, height, width)

    monkeypatch.setattr(bridge_tui._ConsoleState, "wheel", wheel)
    monkeypatch.setattr(bridge_tui._ConsoleState, "hscroll", hscroll)
    monkeypatch.setattr(bridge_tui._ConsoleState, "handle_mouse", mouse)
    result = bridge_tui.run_control("M", lambda *_: "", lambda mission, **_kw: {"mission": {"id": mission, "status": "RUNNING"}, "tasks": []}, initial_view=view)
    assert result == 0
    seen.append(("masks", tuple(masks)))
    seen.append(("delays", tuple(delays)))
    return seen


def test_console_loop_routes_wheel_and_clicks_with_the_shared_mouse_mask(monkeypatch) -> None:
    from llm_tools import bridge_mouse

    up, down, shift = bridge_mouse.WHEEL_UP, bridge_mouse.WHEEL_DOWN, bridge_mouse.SHIFT
    seen = run_loop(
        monkeypatch,
        [curses.KEY_MOUSE] * 6 + ["\x04"],
        [(0, 5, 5, 0, up), (0, 5, 5, 0, down), (0, 7, 2, 0, curses.BUTTON1_CLICKED),
         (0, 1, 1, 0, curses.BUTTON1_DOUBLE_CLICKED), (0, 5, 5, 0, down | shift), (0, 5, 5, 0, 0)],
    )
    assert seen[:2] == [("wheel", UP), ("wheel", DOWN)]
    assert ("click", (2, 7)) in seen and ("hscroll", 1) in seen
    assert ("masks", (bridge_mouse.MOUSE_MASK,)) in seen  # button events only: no pointer-motion flood
    assert not bridge_mouse.MOUSE_MASK & getattr(curses, "REPORT_MOUSE_POSITION", 0)
    assert ("delays", (50,)) in seen  # a lone Esc registers in 50 ms


def test_horizontal_wheel_pans_usage_and_documents_only() -> None:
    usage = make_state(view="Usage")
    usage.hscroll(1)
    usage.hscroll(1)
    usage.hscroll(-1)
    assert usage.usage_hscroll == 8
    usage.hscroll(-1)
    usage.hscroll(-1)
    assert usage.usage_hscroll == 0
    other = make_state(view="Tasks")
    other.hscroll(1)
    assert other.usage_hscroll == 0
    other.document_viewer = DocumentViewer("t", "x")
    other.hscroll(1)
    assert other.document_viewer.horizontal == 8
    other.hscroll(-1)
    other.hscroll(-1)
    assert other.document_viewer.horizontal == 0


def test_console_loop_ignores_a_mouse_event_it_cannot_read(monkeypatch) -> None:
    def broken():
        raise curses.error("stale")

    window = LoopWindow([curses.KEY_MOUSE, "\x04"])
    monkeypatch.setattr(bridge_tui.curses, "getmouse", broken)
    monkeypatch.setattr(bridge_tui.curses, "wrapper", lambda callback: callback(window))
    monkeypatch.setattr(bridge_tui.curses, "mousemask", lambda _mask: None)
    assert bridge_tui.run_control("M", lambda *_: "", lambda mission, **_kw: {"mission": {"id": mission}}) == 0


def test_alternate_scroll_arrow_keys_scroll_non_selectable_views() -> None:
    # Terminals in alternate-scroll mode send the wheel as cursor keys: they reach the same content scroll.
    state = make_state(long_snapshot(), view="Sessions")
    screen = Screen(12, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    state.handle_key("KEY_DOWN")
    state.handle_key("KEY_DOWN")
    assert state.selected == 2
    state.handle_key("KEY_UP")
    assert state.selected == 1


# --- the composer -----------------------------------------------------------------------------------

class ComposerWindow:
    def __init__(self, keys, size=(14, 80)) -> None:
        self.keys = iter(keys)
        self.size = size
        self.rows: dict[int, str] = {}
        self.frames: list[dict[int, str]] = []

    def keypad(self, _enabled): pass
    def timeout(self, _milliseconds): pass
    def getmaxyx(self): return self.size
    def erase(self):
        if self.rows:
            self.frames.append(dict(self.rows))
        self.rows = {}
    def clear(self): pass
    def refresh(self): pass
    def move(self, _y, _x): pass
    def addnstr(self, y, _x, value, _count, *_attrs): self.rows[y] = self.rows.get(y, "") + value

    def get_wch(self):
        return next(self.keys)


def numbered(count: int) -> str:
    return "\n".join(f"line {i}" for i in range(count))


def test_composer_wheel_moves_the_caret_three_lines_per_notch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path))
    events = iter([(0, 3, 3, 0, 0x10000), (0, 3, 3, 0, 0x10000), (0, 3, 3, 0, 0x200000), (0, 3, 3, 0, curses.BUTTON1_CLICKED)])
    monkeypatch.setattr(bridge_tui.curses, "getmouse", lambda: next(events))
    delays: list[int] = []
    monkeypatch.setattr(bridge_tui.curses, "set_escdelay", lambda value: delays.append(value), raising=False)
    monkeypatch.setattr(bridge_tui.curses, "mousemask", lambda _mask: None)
    source = numbered(20)  # the caret starts at the end, on line 19
    window = ComposerWindow([curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_MOUSE, "X", "\x13"])
    result = bridge_tui.compose_document(source, window=window)
    # up, up, down: three lines each, so the caret ends on line 19 - 3 - 3 + 3 = 16
    lines = result.split("\n")
    assert lines[16] == "line 16X" and result.count("X") == 1
    assert delays == [50]


def test_composer_ignores_wheel_during_a_paste_and_unreadable_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path))
    calls = iter([curses.error("stale")])

    def getmouse():
        value = next(calls)
        raise value

    monkeypatch.setattr(bridge_tui.curses, "getmouse", getmouse)
    monkeypatch.setattr(bridge_tui.curses, "mousemask", lambda _mask: None)
    window = ComposerWindow([curses.KEY_MOUSE, "x", "\x13"])
    assert bridge_tui.compose_document("a", window=window) == "ax"


def test_composer_footer_does_not_repeat_the_key_hints(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(bridge_tui.curses, "mousemask", lambda _mask: None)
    window = ComposerWindow(["a", "\n", "b", "\x13"])
    assert bridge_tui.compose_document("", window=window) == "a\nb"
    for frame in window.frames:
        footer = [frame[row] for row in (12, 13) if row in frame]
        assert not any("Enter newline" in line or "Enter inserts a newline" in line for line in footer)
        assert footer[-1].startswith("F2/Ctrl-S Save · Esc Recover · Ctrl-D Discard")
        assert sum("Ctrl-S" in line for line in footer) == 1
    assert window.frames[0][12] == ""  # the notice row is empty until something needs saying


# --- real terminal -------------------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required for rendered terminal assertions")
def test_real_control_view_wheel_moves_selection_with_sgr_sequences(tmp_path, env) -> None:
    import sys
    from pathlib import Path

    from llm_tools.bridge_store import BridgeStore
    from llm_tools.terminal import TmuxTerminalBackend

    state_dir = tmp_path / "state"
    with BridgeStore(state_dir / "bridge.sqlite3") as store:
        mission = store.create_mission("Wheel mission", "Scroll things", cwd=tmp_path, initial_prompt="first task")
        for number in range(2, 13):
            store.add_task(mission["id"], f"task body {number}", title=f"Task number {number}")
    root = str(Path(__file__).resolve().parents[1])
    python_path = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH", ""))))
    command = ["env", "-u", "NO_COLOR", "-u", "LLM_USAGE_NO_COLOR", f"LLM_BRIDGE_STATE_DIR={state_dir}", "TERM=xterm-256color",
               f"PYTHONPATH={python_path}"]
    for name in ("HOME", "PATH", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "COVERAGE_PROCESS_START", "COVERAGE_FILE",
                 "LLM_TOOLS_NO_INHIBIT", "LLM_USAGE_NO_SERVICE", "LLM_USAGE_DISABLE_CODEX_APP_SERVER",
                 "LLM_USAGE_DISABLE_COPILOT_ADDON", "LLM_USAGE_DISABLE_COPILOT_MONTHLY", "LLM_USAGE_LIVE_FETCH_RETRIES"):
        if name in env:
            command.append(f"{name}={env[name]}")
    command.extend([sys.executable, "-m", "llm_tools.bridge", "control", "--mission", mission["id"]])
    backend = TmuxTerminalBackend()
    handle = backend.start(provider_argv=command, bridge_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                           cwd=tmp_path, socket_path=tmp_path / "wheel.sock", session_name="wheel", size=(80, 24))

    def screen() -> list[str]:
        return backend.capture(handle, pane="provider").splitlines()

    def wait_for(predicate, message: str | None = None) -> None:
        if not _shared_wait_for(predicate, 8):
            raise AssertionError(message or "\n".join(screen()))

    try:
        wait_for(lambda: len(screen()) > 3 and "[Overview]" in screen()[2])
        backend.send_keys(handle, pane="provider", keys="\x1bOQ")  # F2 Tasks
        wait_for(lambda: len(screen()) > 3 and screen()[3].startswith("> 1 "))
        backend.send_keys(handle, pane="provider", keys="\x1b[<65;10;5M")  # wheel down
        wait_for(lambda: any(line.startswith("> 4 ") for line in screen()))
        backend.send_keys(handle, pane="provider", keys="\x1b[<65;10;5M")
        wait_for(lambda: any(line.startswith("> 7 ") for line in screen()))
        backend.send_keys(handle, pane="provider", keys="\x1b[<64;10;5M")  # wheel up
        wait_for(lambda: any(line.startswith("> 4 ") for line in screen()))
        backend.send_keys(handle, pane="provider", keys="\x1b[<65;10;2M")  # wheel over the tab bar changes nothing but the selection
        wait_for(lambda: any(line.startswith("> 7 ") for line in screen()))
        assert "[Tasks]" in screen()[2]
    finally:
        backend.stop(handle)
