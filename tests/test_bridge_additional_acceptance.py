"""Failure and recovery contracts for the Bridge's local input surfaces."""
from __future__ import annotations

import curses
from pathlib import Path
import subprocess

import pytest

from llm_tools import bridge_launcher, bridge_tui


class Window:
    def __init__(self, keys=(), size=(24, 80)):
        self.keys = iter(keys)
        self.size = size
        self.rows: list[str] = []
        self.keypad_states: list[bool] = []

    def keypad(self, enabled): self.keypad_states.append(enabled)
    def timeout(self, _milliseconds): pass
    def getmaxyx(self): return self.size
    def get_wch(self): return next(self.keys)
    def erase(self): self.rows.clear()
    def clear(self): self.rows.clear()
    def refresh(self): pass
    def addnstr(self, _y, _x, value, _count, *_attrs): self.rows.append(value)
    def move(self, _y, _x): pass


@pytest.mark.parametrize("size", [30 * 1024, 32 * 1024, 64 * 1024, 256 * 1024])
def test_composer_complete_paste_round_trips_at_acceptance_sizes(tmp_path, monkeypatch, size):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path))
    source = ("中  \tline\n" * ((size // len("中  \tline\n".encode())) + 1))[:size]
    window = Window(["\x1b[200~", source[:101], source[101:], "\x1b[201~", "\x13"])
    assert bridge_tui.compose_document("", window=window) == source
    assert bridge_tui.LAST_RECOVERY_DRAFT.read_bytes() == source.encode()
    assert False in window.keypad_states and window.keypad_states[-1] is True


def test_composer_unrecognized_escape_does_not_corrupt_source(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path))
    window = Window(["\x1b[999~", "\x13"])
    assert bridge_tui.compose_document("keep", window=window) == "keep"
    assert bridge_tui.LAST_RECOVERY_DRAFT.read_text() == "keep"


def test_composer_editor_success_preserves_exact_newlines_and_removes_editor_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("VISUAL", "fake-editor")
    for name in ("def_prog_mode", "endwin", "reset_prog_mode"):
        monkeypatch.setattr(curses, name, lambda: None)
    edited = "  \tfirst\r\nlast\n"
    paths = []

    def run(argv, *, check, timeout):
        path = Path(argv[-1]); paths.append(path)
        assert path.read_bytes() == b"original"
        path.write_bytes(edited.encode())
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(bridge_tui.subprocess, "run", run)
    assert bridge_tui.compose_document("original", window=Window(["\x0f", "\x13"])) == edited
    assert paths and not paths[0].exists()
    assert bridge_tui.LAST_RECOVERY_DRAFT.read_bytes() == edited.encode()


def test_composer_editor_timeout_keeps_both_private_recovery_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("VISUAL", "fake-editor")
    for name in ("def_prog_mode", "endwin", "reset_prog_mode"):
        monkeypatch.setattr(curses, name, lambda: None)
    paths = []

    def run(argv, *, check, timeout):
        paths.append(Path(argv[-1]))
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(bridge_tui.subprocess, "run", run)
    window = Window(["\x0f", "\x13"])
    assert bridge_tui.compose_document("secret", window=window) == "secret"
    assert paths[0].read_text() == "secret"
    assert paths[0].stat().st_mode & 0o777 == 0o600
    assert bridge_tui.LAST_RECOVERY_DRAFT.read_text() == "secret"
    assert any("Editor failed" in row for row in window.rows)


def test_launcher_form_rejects_invalid_start_and_preserves_fields(tmp_path):
    state = bridge_launcher.Launcher(str(tmp_path), creating=True, directory=str(tmp_path))
    state.focus = 4
    assert state.key("\n") is None
    assert "objective" in state.error.lower()
    state.objective = "Build this"
    state.providers = "codex,zai"
    assert state.key("\n") is None
    assert "provider" in state.error.lower()
    state.providers = "codex, claude"
    assert state.key("\n") == "start"
    assert state.providers == "codex,claude"
    assert state.objective == "Build this"


def test_launcher_click_selects_without_activation_then_explicitly_opens(tmp_path):
    state = bridge_launcher.Launcher(str(tmp_path), missions=[
        {"id": "one", "name": "One"}, {"id": "two", "name": "Two"}
    ])
    assert state.click(9, activate=False) is None
    assert state.selected == 2
    assert state.click(9, activate=True) == "open"
    assert state.click(2, activate=True) is None
    state.creating = True
    state.focus = 0
    assert state.click(17) is None
    assert state.focus == 5


def test_launcher_paste_rejection_preserves_unicode_source_and_position(tmp_path):
    state = bridge_launcher.Launcher(str(tmp_path), creating=True, objective="中tail", directory=str(tmp_path))
    state.cursors[0] = 1
    assert state.paste("  new\n") == "compose-objective"
    assert state.objective == "中  new\ntail"
    previous = state.objective, state.cursors[0]
    assert state.paste("🙂" * 300_000) is None
    assert (state.objective, state.cursors[0]) == previous
    assert "received 1200000 bytes" in state.error
    state.focus = 1
    assert state.paste("wrong field") is None
    assert state.objective == previous[0]


def test_response_overlay_blocks_accidental_command_and_allows_view_navigation():
    calls = []
    state = bridge_tui._ConsoleState("m", lambda _id, command: calls.append(command) or "ok", lambda *_: {}, full=True)
    state.command = "task cancel --task 9"
    state.command_cursor = len(state.command)
    state.response_overlay = True
    state.handle_key("y")
    state.handle_key("KEY_ENTER")
    assert calls == []
    assert not state.response_overlay
    assert state.command == "task cancel --task 9"
    state.response_overlay = True
    state.handle_key("KEY_NPAGE")
    assert state.response_scroll > 0
    state.handle_key("KEY_PPAGE")
    assert state.response_scroll == 0
    state.handle_key("KEY_F2")
    assert state.view == "Orders" and not state.response_overlay


def test_orders_paging_and_search_draft_restore_do_not_change_task_source():
    state = bridge_tui._ConsoleState("m", lambda *_: "", lambda *_: {}, full=True, snapshot={
        "tasks": [{"id": "t1", "title": "First", "prompt": "source"}], "task_total": 500,
    })
    state.choose_view(1)
    state.handle_key("KEY_NPAGE")
    assert state.task_offset == bridge_tui._TASK_PAGE_SIZE
    state.handle_key("KEY_PPAGE")
    assert state.task_offset == 0
    state.command = "status --json"
    state.command_cursor = 3
    state.input_focused = False
    state.handle_key("\x06")
    for char in "needle":
        state.handle_key(char)
    assert state.search_editing
    state.handle_key("\x1b")
    assert state.command == "status --json"
    assert state.command_cursor == 3
    assert not state.search_editing
    assert state.snapshot["tasks"][0]["prompt"] == "source"
