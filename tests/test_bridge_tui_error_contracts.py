"""Exercise TUI recovery paths whose failures must stay visible and bounded."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from threading import Event

from llm_tools import bridge_tui


class _Window:
    def clear(self) -> None: pass
    def refresh(self) -> None: pass


class _DrawWindow(_Window):
    def __init__(self, height: int, width: int, *, fail: bool = False) -> None:
        self.size = (height, width)
        self.fail = fail
        self.lines: list[tuple[int, int, str, int]] = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.lines.clear()

    def addnstr(self, y, x, text, count):
        if self.fail:
            raise bridge_tui.curses.error("fake draw failure")
        self.lines.append((y, x, text, count))

    def noutrefresh(self): pass


def _wait(predicate, timeout: float = 1.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_snapshot_reader_failure_preserves_last_good_projection_and_bounds_error() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda *_: {}, full=False)
    state.snapshot = bridge_tui._bounded_snapshot({"mission": {"id": "M", "name": "last good"}})
    state.snapshot_reader = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("x" * 500))
    state.refresh(force=True)
    _wait(lambda: state.refresh() is None and bool(state.snapshot_error))
    assert state.snapshot["mission"]["name"] == "last good"
    assert len(state.snapshot_error) <= 160

    state.snapshot_reader = lambda *_args, **_kwargs: None
    state.last_refresh = 0
    state.refresh(force=True)
    _wait(lambda: state.refresh() is None and "mapping" in state.snapshot_error)
    assert state.snapshot["mission"]["name"] == "last good"


def test_snapshot_projection_passes_only_supported_bounded_query_arguments() -> None:
    received: list[tuple[int, int, str, str]] = []

    def snapshot(_mission: str, *, task_offset: int, task_limit: int, search: str, view: str):
        received.append((task_offset, task_limit, search, view))
        return {"tasks": []}

    state = bridge_tui._ConsoleState("M", lambda *_: "", snapshot, full=True)
    state.task_offset = 200
    state.search_query = "needle"
    state.choose_view(1)
    state.refresh(force=True)
    _wait(lambda: received == [(200, bridge_tui._TASK_PAGE_SIZE, "needle", "Orders")])


def test_dispatch_errors_and_reentrant_command_are_reported_without_duplicate_work() -> None:
    entered, release = Event(), Event()
    calls: list[str] = []

    def dispatch(_mission: str, command: str) -> str:
        calls.append(command)
        entered.set()
        release.wait(1)
        raise RuntimeError("durable write failed")

    state = bridge_tui._ConsoleState("M", dispatch, lambda _id: {}, full=False)
    assert state._dispatch("priority 1 5")
    assert entered.wait(1)
    assert not state._dispatch("priority 1 6")
    assert calls == ["priority 1 5"]
    release.set()
    _wait(lambda: state.refresh() is None and not state.command_pending)
    assert any("still running" in line for line in state.output)
    assert any("Outcome unknown: durable write failed" in line for line in state.output)
    assert state.command == "priority 1 5"


def test_dispatch_cleanup_tolerates_missing_or_unlinkable_editor_files(monkeypatch, tmp_path) -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "done", lambda _id: {}, full=False)
    missing = tmp_path / "already-gone.md"
    assert state._dispatch("add FILE", cleanup_path=missing)
    _wait(lambda: state.refresh() is None and not state.command_pending)
    assert not missing.exists()

    existing = tmp_path / "locked.md"
    existing.write_text("contents", encoding="utf-8")
    real_unlink = Path.unlink

    def deny_one(path: Path, *args, **kwargs):
        if path == existing:
            raise PermissionError("cleanup denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_one)
    assert state._dispatch("add FILE", cleanup_path=existing)
    _wait(lambda: state.refresh() is None and not state.command_pending)
    assert existing.read_text(encoding="utf-8") == "contents"
    assert any("done" == line for line in state.output)
    assert state.rejected_draft_path == existing
    assert any(str(existing) in line for line in state.output)


def test_snapshot_reader_signature_failure_falls_back_to_no_projection_kwargs() -> None:
    calls: list[str] = []

    class CallableSnapshot:
        __signature__ = 42

        def __call__(self, mission: str) -> dict[str, object]:
            calls.append(mission)
            return {"mission": {"id": mission}}

    state = bridge_tui._ConsoleState("M", lambda *_: "", CallableSnapshot(), full=False)
    state.refresh(force=True)
    _wait(lambda: state.refresh() is None and bool(state.snapshot))
    assert calls == ["M"]


def test_usage_and_control_requests_are_local_view_actions() -> None:
    calls: list[str] = []
    state = bridge_tui._ConsoleState("M", lambda _mission, line: calls.append(line) or "ok", lambda _id: {}, full=False)
    assert state._dispatch("usage")
    assert state.view == "Usage" and state.expand_requested
    assert state._dispatch("control")
    assert state.view == "Bridge" and state.expand_requested
    assert calls == []


def test_editor_failure_and_empty_file_do_not_dispatch_and_remove_tempfile(monkeypatch) -> None:
    created: list[Path] = []
    calls: list[str] = []

    def failed_editor(argv: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        created.append(Path(argv[-1]))
        return subprocess.CompletedProcess(argv, 3)

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(bridge_tui.subprocess, "run", failed_editor)
    monkeypatch.setattr(bridge_tui.curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "endwin", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "reset_prog_mode", lambda: None)
    state = bridge_tui._ConsoleState("M", lambda _mission, line: calls.append(line) or "ok", lambda _id: {}, full=False)
    bridge_tui._edit_task_prompt(_Window(), state)  # type: ignore[arg-type]
    assert "status 3" in state.output[-1]
    assert created and not created[-1].exists()

    def empty_editor(argv: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        created.append(Path(argv[-1]))
        Path(argv[-1]).write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(bridge_tui.subprocess, "run", empty_editor)
    bridge_tui._edit_task_prompt(_Window(), state)  # type: ignore[arg-type]
    assert "Empty task text" in state.output[-1]
    assert not created[-1].exists() and calls == []


def test_invalid_editor_setting_cleans_private_file_without_leaking_exception(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VISUAL", "   ")
    monkeypatch.delenv("EDITOR", raising=False)
    created: list[Path] = []
    real_mkstemp = bridge_tui.tempfile.mkstemp

    def private_temp(*, prefix: str, suffix: str):
        fd, raw_path = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created.append(Path(raw_path))
        return fd, raw_path

    monkeypatch.setattr(bridge_tui.tempfile, "mkstemp", private_temp)
    state = bridge_tui._ConsoleState("M", lambda *_: "unexpected", lambda _id: {}, full=False)
    bridge_tui._edit_task_prompt(_Window(), state)  # type: ignore[arg-type]
    assert "must name an executable" in state.output[-1]
    assert created and not created[0].exists()


def test_empty_dispatch_result_and_pending_enter_are_non_destructive() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda _id: {}, full=False)
    state.command = "status"
    state.command_pending = True
    state.handle_key("KEY_ENTER")
    assert state.command == "status" and state.command_pending
    state.command_pending = False
    assert state._dispatch("status")
    _wait(lambda: state.refresh() is None and not state.command_pending)
    assert not state.output or state.output[-1] != ""


def test_usage_and_lifecycle_empty_branches_render_without_claims() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda _id: {}, full=True, snapshot={
        "usage": {"status": "ready", "age_seconds": 0, "text": "one\ntwo"},
        "lifecycle": {"status": "RUNNING", "heartbeat": {}},
    })
    state.choose_view(7)
    assert state.rows() == ["Usage: ready | age 0s | background refresh", "one", "two"]
    state.choose_view(3)
    rows = state.rows()
    assert rows == ["lifecycle: status=RUNNING"]


def test_arrow_navigation_during_command_entry_does_not_change_view() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda _id: {}, full=False)
    state.command = "status"
    for key in ("KEY_LEFT", "KEY_RIGHT"):
        state.handle_key(key)
    assert state.view == "Bridge" and state.command == "status"


def test_compact_full_and_usage_drawing_handles_small_terminals_and_curses_errors(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda _id: {}, full=False)
    state.choose_view(7)
    state.snapshot = bridge_tui._bounded_snapshot({"usage": {"text": "long dashboard row\nsecond"}})
    compact = _DrawWindow(1, 40)
    bridge_tui._draw_compact(compact, state)  # only header fits
    assert compact.lines and all(y == 0 for y, *_ in compact.lines)

    full = _DrawWindow(20, 80)
    bridge_tui._draw_full(full, state)
    assert full.lines and any("long dashboard" in text for _y, _x, text, _n in full.lines)

    no_width = _DrawWindow(1, 0)
    bridge_tui._put(no_width, 0, 0, "ignored", 0)
    assert no_width.lines == []
    failed = _DrawWindow(1, 40, fail=True)
    bridge_tui._put(failed, 0, 0, "ignored", 40)  # curses.error is swallowed during resize races
