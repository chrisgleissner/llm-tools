from __future__ import annotations

import time
import shlex
import os
import stat
import subprocess
from pathlib import Path

from llm_tools import bridge_tui


class FakeWindow:
    def __init__(self, height: int, width: int) -> None:
        self.height = height
        self.width = width
        self.lines = [""] * height

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.lines = [""] * self.height

    def addnstr(self, y: int, x: int, text: str, count: int) -> None:
        current = self.lines[y]
        self.lines[y] = (current[:x] + text[:count]).ljust(self.width)[: self.width]

    def noutrefresh(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def refresh(self) -> None:
        pass


def _wait_for(predicate, state: bridge_tui._ConsoleState, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state.refresh()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_console_state_dispatches_through_injected_facade_and_refreshes() -> None:
    calls: list[tuple[str, str]] = []
    snapshots: list[str] = []

    def snapshot(mission_id: str) -> dict[str, object]:
        snapshots.append(mission_id)
        return {
            "mission": {"id": mission_id, "status": "RUNNING", "objective": "build it"},
            "tasks": [{"id": "T1", "title": "First", "status": "READY"}],
            "task_counts": {"READY": 1},
        }

    def dispatch(mission_id: str, command: str) -> str:
        calls.append((mission_id, command))
        return "queued command"

    state = bridge_tui._ConsoleState("M1", dispatch, snapshot, full=False)
    state.refresh(force=True)
    _wait_for(lambda: bool(state.snapshot), state)
    assert state.rows()[0] == "Mission: M1 [RUNNING]"
    assert snapshots == ["M1"]
    last_refresh = state.last_refresh
    state.refresh(now=last_refresh + 0.1)
    assert snapshots == ["M1"]  # Refresh stays at or below four calls per second.

    for char in "status":
        state.handle_key(char)
    state.handle_key("KEY_ENTER")
    _wait_for(lambda: not state.command_pending and len(snapshots) == 2, state)
    assert calls == [("M1", "status")]
    assert state.command == ""
    assert state.output[-1] == "queued command"
    assert len(snapshots) == 2  # A command completion forces an immediate refresh.


def test_console_keyboard_mouse_views_and_palette_share_dispatch() -> None:
    calls: list[str] = []
    state = bridge_tui._ConsoleState("M2", lambda _mission, cmd: calls.append(cmd) or "ok", lambda _id: {}, full=True)

    state.handle_key("KEY_F3")
    assert state.view == "Stations"
    state.handle_key("\t")
    assert state.view == "Systems"
    nav = bridge_tui._nav_line(state.view_index, "  ")
    stations_x = nav.index("Stations") + 2
    state.handle_mouse(2, stations_x, 24, 80)
    assert state.view == "Stations"
    state.handle_mouse(2, 78, 24, 80)
    assert state.view == "Palette"
    state.handle_mouse(3, 2, 24, 80)
    assert state.command == "help"  # A click stages an action; Enter runs it.
    state.handle_key("\x15")
    state.selected = 4
    state.handle_key("KEY_ENTER")
    _wait_for(lambda: not state.command_pending, state)
    assert calls == ["pause"]


def test_full_view_mouse_selects_the_task_row_under_the_pointer() -> None:
    state = bridge_tui._ConsoleState(
        "M2",
        lambda _mission, _command: "",
        lambda _id: {},
        full=True,
        snapshot={
            "tasks": [
                {"id": "A", "title": "First", "status": "READY"},
                {"id": "B", "title": "Second", "status": "QUEUED"},
            ]
        },
    )
    state.choose_view(1)
    state.handle_mouse(4, 10, 24, 80)
    assert state.selected == 1
    assert state.rows()[state.selected].startswith("B  QUEUED")


def test_tui_compact_and_full_render_at_supported_sizes(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    state = bridge_tui._ConsoleState(
        "M3",
        lambda _mission, _command: "",
        lambda _mission: {"mission": {"name": "Build", "status": "PAUSED", "objective": "ship this"}},
        full=False,
    )
    state.refresh(force=True)
    _wait_for(lambda: bool(state.snapshot), state)
    compact = FakeWindow(6, 80)
    bridge_tui._draw_compact(compact, state)  # type: ignore[arg-type]
    assert "LLM BRIDGE" in compact.lines[0]
    assert "[1 Bridge]" in compact.lines[2]
    assert compact.lines[4].startswith("> ")
    assert "Ctrl-D exits" in compact.lines[5]

    state.full = True
    full = FakeWindow(24, 80)
    bridge_tui._draw_full(full, state)  # type: ignore[arg-type]
    assert "Bridge" in full.lines[2]
    assert full.lines[22].startswith("> ")
    assert len(full.lines) == 24


def test_snapshot_errors_and_terminal_controls_are_rendered_safely() -> None:
    state = bridge_tui._ConsoleState(
        "M4",
        lambda _mission, _command: "\x1b[31msecret-like output\x1b[0m",
        lambda _mission: (_ for _ in ()).throw(RuntimeError("read\x1b[2J failed")),
        full=False,
    )
    state.refresh(force=True)
    _wait_for(lambda: bool(state.snapshot_error), state)
    state.handle_key("s")
    state.handle_key("\n")
    _wait_for(lambda: not state.command_pending, state)
    assert "\x1b" not in state.output[-1]


def test_console_retains_only_bounded_task_projection() -> None:
    tasks = [
        {"id": index, "title": f"task {index}", "status": "QUEUED", "original_prompt": "p" * 10000}
        for index in range(10000)
    ]
    state = bridge_tui._ConsoleState(
        "M5",
        lambda _mission, _command: "",
        lambda _mission: {"mission": {"id": "M5"}, "tasks": tasks, "task_counts": {"QUEUED": 10000}},
        full=True,
    )
    state.refresh(force=True)
    _wait_for(lambda: bool(state.snapshot), state)
    assert len(state.snapshot["tasks"]) == 200  # type: ignore[arg-type]
    assert "original_prompt" not in state.snapshot["tasks"][0]  # type: ignore[index]
    state.choose_view(1)
    assert len(state.rows()) == 200


def test_console_printable_commands_are_never_navigation_shortcuts() -> None:
    expected = (
        "help",
        "verify 123 --approve",
        "priority 1 2",
        "add /tmp/task file.md",
        "amend 17 /tmp/amendment.md",
    )
    calls: list[str] = []
    state = bridge_tui._ConsoleState("M6", lambda _mission, command: calls.append(command) or "ok", lambda _id: {}, full=False)
    for command in expected:
        for char in command:
            state.handle_key(char)
        assert state.command == command
        state.handle_key("KEY_ENTER")
        _wait_for(lambda: not state.command_pending, state)
    assert calls == list(expected)
    state.handle_key("\x10")
    assert state.command == expected[-1]
    state.handle_key("\x0e")
    assert state.command == ""

    state.handle_key("KEY_F4")
    assert state.view == "Systems"


def test_slow_snapshot_and_dispatch_never_block_keyboard_input() -> None:
    def slow_snapshot(_mission: str) -> dict[str, object]:
        time.sleep(0.4)
        return {"mission": {"id": "slow"}}

    def slow_dispatch(_mission: str, command: str) -> str:
        time.sleep(0.4)
        return command

    state = bridge_tui._ConsoleState("slow", slow_dispatch, slow_snapshot, full=False)
    started = time.monotonic()
    state.refresh(force=True)
    for char in "help":
        state.handle_key(char)
    state.handle_key("KEY_ENTER")
    assert time.monotonic() - started < 0.25
    assert state.command_pending

    state.output.extend(str(index) for index in range(1000))
    assert len(state.output) == bridge_tui._MAX_OUTPUT_LINES
    _wait_for(lambda: bool(state.snapshot) and not state.command_pending, state, timeout=1.5)
    assert state.output[-1] == "help"


def test_private_editor_file_dispatches_multiline_task_and_cleans_up(monkeypatch) -> None:
    calls: list[tuple[list[str], str]] = []
    editor_argv: list[str] = []

    def run_editor(argv: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        editor_argv.extend(argv)
        path = argv[-1]
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        Path(path).write_text("first line\nsecond line\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("VISUAL", "fake-editor --wait")
    monkeypatch.setattr(bridge_tui.subprocess, "run", run_editor)
    monkeypatch.setattr(bridge_tui.curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "endwin", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "reset_prog_mode", lambda: None)

    def dispatch(_mission: str, command: str) -> str:
        argv = shlex.split(command)
        calls.append((argv, Path(argv[-1]).read_text(encoding="utf-8")))
        return "task queued"

    state = bridge_tui._ConsoleState("M7", dispatch, lambda _id: {}, full=False)
    bridge_tui._edit_task_prompt(FakeWindow(6, 80), state)  # type: ignore[arg-type]
    _wait_for(lambda: not state.command_pending, state)
    assert editor_argv[:2] == ["fake-editor", "--wait"]
    assert calls == [(["add", editor_argv[-1]], "first line\nsecond line\n")]
    assert not Path(editor_argv[-1]).exists()
    assert state.output[-1] == "task queued"


def test_editor_amends_selected_order_through_same_dispatch(monkeypatch) -> None:
    sent: list[list[str]] = []

    def run_editor(argv: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        Path(argv[-1]).write_text("replacement text", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(bridge_tui.subprocess, "run", run_editor)
    monkeypatch.setattr(bridge_tui.curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "endwin", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "reset_prog_mode", lambda: None)
    state = bridge_tui._ConsoleState(
        "M8",
        lambda _mission, command: sent.append(shlex.split(command)) or "amended",
        lambda _id: {},
        full=True,
        snapshot={"tasks": [{"id": 42, "title": "old", "status": "READY"}]},
    )
    bridge_tui._edit_task_prompt(FakeWindow(24, 80), state, bridge_tui._selected_task_id(state))  # type: ignore[arg-type]
    _wait_for(lambda: not state.command_pending, state)
    assert sent and sent[0][0:2] == ["amend", "42"]
    assert not Path(sent[0][-1]).exists()
