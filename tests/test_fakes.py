"""Behaviour of the shared curses doubles in ``tests/fakes.py``."""

from __future__ import annotations

import curses

import pytest

from .fakes import FakeScreen, FakeWindow, RoleAttrs, ScriptExhausted


def test_draw_calls_and_rows_are_recorded() -> None:
    window = FakeWindow(3, 10)
    window.addnstr(0, 0, "hello world", 5)
    window.addstr(1, 2, "ab", 7)
    window.move(2, 4)
    assert window.rows() == ["hello", "  ab", ""]
    assert window.writes == [(0, 0, "hello", 0), (1, 2, "ab", 7)]
    assert [tuple(call) for call in window.calls] == window.writes
    assert window.cursor == (2, 4)
    assert window.lines[1] == "  ab      "


def test_wide_and_combining_characters_take_their_cells() -> None:
    window = FakeWindow(1, 8)
    window.addnstr(0, 0, "a文éb", 10)
    assert window.row(0) == "a文éb"
    assert [(column, text) for column, text, _role in window.segments()[0]] == [(0, "a文éb")]
    window.addnstr(0, 6, "x", 1)
    assert window.segments()[0][-1][:2] == (6, "x")


def test_writes_outside_the_window_raise_like_curses_and_overlong_text_is_clipped() -> None:
    window = FakeWindow(2, 4)
    with pytest.raises(curses.error):
        window.addnstr(2, 0, "x", 1)
    with pytest.raises(curses.error):
        window.addnstr(0, 4, "x", 1)
    window.addnstr(0, 2, "abcdef", 6)
    assert window.row(0) == "  ab" and window.overflows == 1


def test_erase_clears_the_grid_and_refresh_captures_a_frame() -> None:
    window = FakeWindow(2, 6)
    window.addnstr(0, 0, "one", 3)
    window.refresh()
    window.erase()
    window.addnstr(1, 0, "two", 3)
    window.noutrefresh()
    assert [frame.rows for frame in window.frames] == [("one", ""), ("", "two")]
    assert window.refreshes == 2


def test_resize_and_scripted_keys() -> None:
    window = FakeWindow(2, 6, keys=["a", 258])
    window.resize(3, 9)
    assert window.getmaxyx() == (3, 9)
    assert window.get_wch() == "a" and window.getch() == 258
    window.push_keys("q")
    assert window.getkey() == "q"
    with pytest.raises(ScriptExhausted):
        window.get_wch()


def test_roles_decode_through_an_installed_table(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools import bridge_tui

    screen = FakeScreen(2, 20)
    screen.install(monkeypatch)
    bridge_tui._put(screen, 0, 0, "Title", 20, role="heading")
    bridge_tui._put(screen, 0, 6, "key", 10, role="key", attr=curses.A_UNDERLINE)
    bridge_tui._put(screen, 1, 0, "quiet", 10, role="error.quiet")
    bridge_tui._put(screen, 1, 6, "plain", 10)
    assert [(call.role, call.extra) for call in screen.calls] == [("heading", ()), ("key", ("underline",)), ("error.quiet", ()), ("none", ())]
    assert screen.role_at(0, 0) == "heading" and screen.role_at(0, 19) is None
    assert screen.segments()[0] == [(0, "Title", "heading"), (6, "key", "key+underline")]
    assert screen.frame().segments[1] == ((0, "quiet", "error.quiet"), (6, "plain", "none"))
    bridge_tui.initialize_colors({"TERM": "dumb"}, is_tty=True)  # installed no-op: the role table survives
    assert bridge_tui.role_attr("heading") == RoleAttrs().table["heading"]


def test_install_stubs_curses_and_records_cursor_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = FakeScreen(2, 4)
    screen.install(monkeypatch, wrapper=True)
    assert screen.cursor_visible is None
    curses.curs_set(1)
    assert screen.cursor_visible is True
    curses.curs_set(0)
    assert screen.cursor_visible is False
    assert curses.wrapper(lambda window: window) is screen
