"""Edge cases of the console's formatting helpers and key mapping."""

from __future__ import annotations

import pytest

from llm_tools import bridge_tui


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "2000000000")


class Screen:
    def __init__(self, height: int, width: int) -> None:
        self.height, self.width = height, width

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        pass

    def addnstr(self, *_args: object) -> None:
        pass

    def noutrefresh(self) -> None:
        pass

    def move(self, *_args: int) -> None:
        pass

    def keypad(self, _enabled: bool) -> None:
        pass

    def timeout(self, _milliseconds: int) -> None:
        pass


def test_formatting_helpers_handle_missing_and_odd_values() -> None:
    assert bridge_tui._clip("abcdef", 0) == "" and bridge_tui._clip("abcdef", 4) == "abc…" and bridge_tui._clip("abc", 4) == "abc"
    assert bridge_tui._age(None) == "—" and bridge_tui._age(7) == "7s" and bridge_tui._age(3700) == "1h 1m" and bridge_tui._age(-5) == "0s"
    assert bridge_tui._relative_epoch("junk") is None
    assert bridge_tui._relative_epoch(2_000_000_000 - 120) == "2m ago"
    assert bridge_tui._relative_epoch(2_000_000_000 + 90) == "in 1m"
    assert bridge_tui._short_id(None) == "-" and bridge_tui._short_id("0123456789") == "01234567"
    assert bridge_tui._local_clock("not a time") == "--:--:--"
    assert bridge_tui._count_phrase({"QUEUED": 2, "RUNNING": 1, "ZERO": 0, "ODD": "x", "Future": 3}) == "1 running · 2 queued · 3 future"
    assert bridge_tui._compact_pairs({"a": None, "b": "", "c": [], "d": {"e": [1, 2], "f": True}, "g": 3}) == ["d.e=2 items", "d.f=yes", "g=3"]
    assert len(bridge_tui._compact_pairs({str(i): i for i in range(20)})) == 8


def test_controller_line_uses_lifecycle_status_when_mission_is_absent() -> None:
    assert bridge_tui._controller_line({"lifecycle": {"status": "STOPPED"}}) == "stopped — type start or resume"


def test_systems_accepts_text_execution_and_unavailable_capacity() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda *_: {}, full=True)
    state.choose_view(bridge_tui.VIEWS.index("Health"))
    state.snapshot = {"execution": "idle", "suspend": {"status": "unknown", "target_epoch": None}, "capacity": {"source": "unavailable"}}
    assert state.rows() == ["Execution             idle", "Capacity              unavailable"]


def test_run_loop_maps_shift_arrows_for_usage_panning(monkeypatch) -> None:
    window = Screen(24, 80)
    keys = iter([bridge_tui.curses.KEY_F8, bridge_tui.curses.KEY_SRIGHT, bridge_tui.curses.KEY_SLEFT, "\x04"])
    window.get_wch = lambda: next(keys)  # type: ignore[attr-defined]
    seen: list[str] = []
    original = bridge_tui._ConsoleState.handle_key

    def spy(self, key):
        seen.append(str(key))
        return original(self, key)

    monkeypatch.setattr(bridge_tui._ConsoleState, "handle_key", spy)
    monkeypatch.setattr(bridge_tui.curses, "wrapper", lambda callback: callback(window))
    monkeypatch.setattr(bridge_tui.curses, "mousemask", lambda _mask: None)
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "curs_set", lambda _value: None)
    result = bridge_tui.run_control("M", lambda *_: "", lambda mission, **_kwargs: {"mission": {"id": mission, "status": "RUNNING"}})
    assert result == 0 and seen[:3] == ["KEY_F8", "KEY_SRIGHT", "KEY_SLEFT"]
