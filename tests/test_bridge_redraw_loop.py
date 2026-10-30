"""The console loop paints on change and sleeps until its next deadline instead of ticking."""

from __future__ import annotations

import curses

import pytest

from llm_tools import bridge_tui


class LoopWindow:
    """Scripted window: each entry is a key, a callable run in place of input, or None for "no input"."""

    def __init__(self, keys: list[object], size: tuple[int, int] = (24, 80)) -> None:
        self.keys = iter(keys)
        self.size = size
        self.timeouts: list[int] = []

    def keypad(self, _value: bool) -> None: ...
    def timeout(self, value: int) -> None: self.timeouts.append(value)
    def getmaxyx(self) -> tuple[int, int]: return self.size
    def erase(self) -> None: ...
    def clear(self) -> None: ...
    def refresh(self) -> None: ...
    def noutrefresh(self) -> None: ...
    def move(self, *_args: object) -> None: ...
    def addnstr(self, *_args: object) -> None: ...

    def get_wch(self) -> object:
        key = next(self.keys)
        if callable(key):
            key = key()
        if key is None:
            raise curses.error("no input")
        return key


def drive_console(
    monkeypatch: pytest.MonkeyPatch,
    keys: list[object],
    snapshot: dict[str, object],
    window: LoopWindow | None = None,
    frames: list[int] | None = None,
) -> tuple[LoopWindow, list[int]]:
    window = window or LoopWindow(keys)
    frames = [] if frames is None else frames
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    monkeypatch.setattr(curses, "curs_set", lambda _n: None)
    monkeypatch.setattr(curses, "mousemask", lambda _n: None)
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(bridge_tui, "initialize_colors", lambda: False)
    monkeypatch.setattr(bridge_tui, "sync_window_size", lambda _window: None)
    monkeypatch.setattr(bridge_tui, "_draw_full", lambda _window, _state: frames.append(1))
    assert bridge_tui.run_control("M", lambda *_: "", lambda *_a, **_k: dict(snapshot)) == 0
    return window, frames


SNAPSHOT = {"mission": {"id": "M", "status": "STOPPED"}, "tasks": []}


def test_idle_console_paints_almost_nothing_and_has_no_fixed_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    window, frames = drive_console(monkeypatch, [None] * 40 + ["\x04"], SNAPSHOT)
    assert len(frames) <= 3  # the first frame plus the one the first snapshot read triggers
    assert max(window.timeouts[1:]) <= 1000 and min(window.timeouts) >= 1
    assert window.timeouts.count(100) <= 1  # only the initial setting; waits are computed per pass


def test_a_keystroke_paints_on_the_next_pass_and_the_screen_then_goes_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    frames: list[int] = []

    def note() -> None:
        seen.append(len(frames))

    keys: list[object] = [None] * 10 + [note, curses.KEY_DOWN, note, note, "\x04"]
    drive_console(monkeypatch, keys, SNAPSHOT, frames=frames)
    assert len(seen) == 3
    assert seen[1] > seen[0]  # the typed key was painted before the next wait
    assert seen[2] == seen[1]  # nothing further changed


def test_resize_repaints(monkeypatch: pytest.MonkeyPatch) -> None:
    window = LoopWindow([])
    frames: list[int] = []
    counts: list[int] = []

    def grow() -> None:
        counts.append(len(frames))
        window.size = (30, 100)

    def after() -> None:
        counts.append(len(frames))

    window.keys = iter([None] * 10 + [grow, None, after, "\x04"])
    drive_console(monkeypatch, [], SNAPSHOT, window, frames)
    assert counts[1] > counts[0]
