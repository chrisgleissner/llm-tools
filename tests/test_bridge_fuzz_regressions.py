"""Minimal regression tests for the defects the Bridge fuzzers found (see tests/fuzz and scripts/bridge_fuzz.py).

Each test names the input that used to fail. They run in the normal suite.
"""

from __future__ import annotations

import curses
import random
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_launcher, bridge_switcher, bridge_tui, bridge_viewer
from tests.fuzz.harness import FakeClock, FakeWindow, Patcher, StreamExhausted, fake_curses, quiet_stdout
from tests.fuzz.targets_ui import Audit, build_console, draw_state, screen_patches


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))


# --- terminal input ----------------------------------------------------------------------------

def _run_screen(runner: Any, feed: list[Any], *, size: tuple[int, int] = (30, 110)) -> FakeWindow:
    clock = FakeClock()
    window = FakeWindow(*size, feed=feed, clock=clock)
    with Patcher() as patch:
        with fake_curses(patch, window=window):
            screen_patches(patch)
            patch.setattr(curses, "wrapper", lambda callback, *a, **k: callback(window))
            patch.setattr(bridge_tui, "time", clock.module())
            patch.setattr(bridge_launcher, "time", clock.module())
            with quiet_stdout():
                try:
                    runner(window)
                except StreamExhausted:
                    pass
    return window


@pytest.mark.parametrize("prefix", [["\x1b", "[", "2", "0", "1"], ["\x1b", "[", "1", ";"], ["\x1b", "O"], ["\x1b", "[", "<", "0", ";"]])
def test_paste_after_an_unfinished_escape_sequence_is_still_a_paste(prefix: list[str]) -> None:
    """ESC inside an unfinished CSI was swallowed as part of it, so the paste marker that followed was missed and the
    pasted text, including its newline, was typed and executed as a command."""
    calls: list[str] = []
    feed = [*prefix, "\x1b", "[", "2", "0", "0", "~", *"cancel", "\n", "\x1b", "[", "2", "0", "1", "~", ("pause", 0.5)]
    _run_screen(lambda w: bridge_tui.run_control("M1", lambda mission, command: calls.append(command) or "ok", lambda *_a, **_k: {"mission": {"id": "M1", "status": "RUNNING"}}), feed)
    assert calls == []


@pytest.mark.parametrize("screen", ["console", "home", "viewer", "picker", "composer"])
def test_undecodable_terminal_input_is_ignored_not_fatal(screen: str) -> None:
    """ncurses reports a UTF-8 sequence beyond U+10FFFF (for example F7 BF BF BF) to get_wch as ValueError, not curses.error."""
    feed: list[Any] = [("invalid",), "a", ("invalid",), ("invalid",), "b"]
    if screen == "console":
        _run_screen(lambda w: bridge_tui.run_control("M1", lambda *_: "ok", lambda *_a, **_k: {"mission": {"id": "M1", "status": "RUNNING"}}), feed)
    elif screen == "home":
        _run_screen(lambda w: bridge_launcher.run_launcher(lambda args: 1), feed)
    elif screen == "viewer":
        _run_screen(lambda w: bridge_viewer.run_document("# title\ntext", title="doc"), feed)
    elif screen == "picker":
        entry = bridge_switcher.MissionEntry(mission_id="a" * 32, name="m", status="RUNNING", cwd="/tmp")
        _run_screen(lambda w: bridge_switcher.run_picker([entry]), feed)
    else:
        _run_screen(lambda w: bridge_tui.compose_document("seed", title="doc"), feed)


# --- command-line numbers ----------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["task", "verify", "--task", "99999999999999999999"], ["task", "resume", "--task-id", "99999999999999999999"], ["task", "priority", "--task", "1", "--value", "-99999999999999999999"],
    ["task", "list", "--offset", "9223372036854775808"], ["events", "--after", "99999999999999999999999"], ["task", "add", "--prompt", "x", "--priority", "9" * 30],
])
def test_numbers_beyond_the_database_range_are_a_usage_error_not_a_traceback(tmp_path: Path, argv: list[str]) -> None:
    """argparse's int accepts any size and SQLite refuses above 2**63 - 1, which escaped main() as OverflowError."""
    import contextlib
    import io

    from llm_tools import bridge
    from llm_tools.bridge_store import BridgeStore

    with BridgeStore(tmp_path / "state" / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "objective", cwd=tmp_path, initial_prompt="first")
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), pytest.raises(SystemExit) as exit_info:
        bridge.main([*argv, "--mission", mission["id"]])
    assert exit_info.value.code == 2
    assert "invalid int value" in err.getvalue() or "range" in err.getvalue()
    console = bridge.dispatch_console_command(mission["id"], " ".join(argv))
    assert isinstance(console, str) and console.startswith("error:")


# --- console command line ----------------------------------------------------------------------

def _console() -> bridge_tui._ConsoleState:
    return bridge_tui._ConsoleState("M1", lambda *_: "ok", lambda *_a, **_k: {"mission": {"id": "M1", "status": "RUNNING"}}, full=True)


def test_ctrl_u_resets_the_caret_with_the_cleared_command_line() -> None:
    """Ctrl-U emptied the line but kept the old caret, so Left/Right did nothing visible until it was walked back."""
    state = _console()
    for char in "status now":
        state.handle_key(char)
    state.handle_key("\x15")
    assert (state.command, state.command_cursor) == ("", 0)
    for char in "ab":
        state.handle_key(char)
    state.handle_key("KEY_LEFT")
    assert state.command_cursor == 1


def test_ctrl_d_leaving_task_search_resets_the_caret() -> None:
    state = _console()
    state.handle_key("\x06")  # task search
    for char in "find":
        state.handle_key(char)
    state.handle_key("\x04")
    assert not state.search_editing
    assert 0 <= state.command_cursor <= len(state.command)


# --- snapshot fields of the wrong type ---------------------------------------------------------

def _draw_snapshot(snapshot: dict[str, Any], *, view: str = "Overview", detail: bool = False) -> None:
    state, _holder = build_console(random.Random(0), Audit(random.Random(0)), full=True, snapshot=snapshot)
    state.view_index = bridge_tui.VIEWS.index(view)
    state.task_detail = detail
    with Patcher() as patch:
        with fake_curses(patch):
            screen_patches(patch)
            draw_state(state, 40, 120)


@pytest.mark.parametrize("amendments", [["not a mapping"], [None, 3], [{"revision": 2, "status": "APPLIED", "changes": {"prompt": "x"}}, "text"]])
def test_task_detail_skips_amendment_records_that_are_not_mappings(amendments: list[object]) -> None:
    """Amendment history assumed every record was a dict and raised AttributeError on a string."""
    snapshot = {"mission": {"id": "m", "status": "RUNNING"}, "tasks": [{"id": 1, "ordinal": 1, "title": "t", "status": "READY", "amendments": amendments}]}
    _draw_snapshot(snapshot, view="Tasks", detail=True)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), "inf", "-Infinity", "nan", "1e999"])
def test_int_or_none_treats_non_finite_numbers_as_unknown(value: object) -> None:
    """int(float("inf")) raises OverflowError, which _int_or_none did not catch; the Health view read criteria totals through it."""
    assert bridge_tui._int_or_none(value) is None
    assert bridge_tui._int_or_none("7") == 7
    _draw_snapshot({"mission": {"id": "m", "status": "RUNNING"}, "criteria": {"total": value, "verified": value}}, view="Health")


@pytest.mark.parametrize("count", [float("nan"), float("inf")])
def test_snapshot_projection_drops_non_finite_counters(count: float) -> None:
    """Proposal counters of inf or NaN reached int() in the Overview rows and the age scan."""
    bounded = bridge_tui._bounded_snapshot({"mission": {"id": "m"}, "task_counts": {"RUNNING": count, "QUEUED": 2}, "proposal_counts": {"PENDING": count}})
    assert bounded["task_counts"] == {"QUEUED": 2} and bounded["proposal_counts"] == {}
    _draw_snapshot({"mission": {"id": "m", "status": "RUNNING"}, "proposal_counts": {"PENDING": count}, "task_counts": {"QUEUED": count}})


@pytest.mark.parametrize("count", [float("nan"), float("inf"), float("-inf")])
def test_task_count_phrase_ignores_non_finite_counts(count: float) -> None:
    """int(nan) and int(inf) raised from the lifecycle summary line."""
    assert bridge_tui._count_phrase({"RUNNING": count, "QUEUED": 2}) == "2 queued"
    _draw_snapshot({"mission": {"id": "m", "status": "RUNNING"}, "task_counts": {"RUNNING": count, "QUEUED": 2}})


# --- waiting banner ----------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    {}, {"window": "5h"}, {"reset_epoch": "soon"}, {"name": "codex", "reset_epoch": float("nan")}, {"name": "codex", "reset_epoch": float("inf")},
    {"name": None, "window": 5, "reset_epoch": 4_000_000_000}, {"name": "codex", "reason": None},
])
def test_waiting_banner_tolerates_entries_with_missing_or_odd_fields(entry: dict[str, Any]) -> None:
    """The Overview waiting line read entry["name"] and subtracted entry["reset_epoch"] without checking either."""
    snapshot = {"mission": {"id": "m", "status": "RUNNING"}, "waiting": {"reason": "capacity", "entries": [entry], "until_epoch": float("inf")}}
    state, _holder = build_console(random.Random(0), Audit(random.Random(0)), full=True, snapshot=snapshot)
    with Patcher() as patch:
        with fake_curses(patch):
            screen_patches(patch)
            assert any("Waiting for" in row for row in state.rows())
            draw_state(state, 30, 100)


def test_countdown_of_a_non_finite_duration_is_a_dash() -> None:
    from llm_tools.bridge_format import countdown

    assert countdown(float("inf")) == "-"
    assert countdown(float("nan")) == "-"
    assert countdown(90) == "1m"


# --- event rows --------------------------------------------------------------------------------

@pytest.mark.parametrize("stamp", [10 ** 30, 1e300, float("inf"), -(10 ** 30), 2 ** 63])
def test_event_clock_survives_a_timestamp_outside_the_platform_range(stamp: object) -> None:
    """An event whose created_at is a huge number used to raise OverflowError from time.localtime."""
    assert bridge_tui._local_clock(stamp) == "--:--:--"
    entries = bridge_tui._activity_entries([{"id": 1, "kind": "task-added", "created_at": stamp, "data": {"title": "t"}}])
    assert entries[0].clock == "--:--:--"


def test_drawing_event_times_never_starts_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """common.parse_epoch falls back to `date -d` for text it cannot parse; a frame with odd timestamps ran one process per event."""
    import subprocess

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("a process was started while formatting a time")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert bridge_tui._local_clock("next tuesday") == "--:--:--"
    assert bridge_tui._relative_epoch("tomorrow noon") is None
    entries = bridge_tui._activity_entries([{"id": 1, "kind": "task-added", "created_at": "yesterday", "data": {}}])
    assert entries[0].clock == "--:--:--"
    assert bridge_tui._local_clock("2026-10-30T06:55:35+00:00") != "--:--:--"
    assert bridge_tui._local_clock(1_700_000_000) != "--:--:--"


def test_durations_and_epochs_that_overflow_are_unknown() -> None:
    """int(float(text)) overflowed for a 400-digit number and int(inf) for an infinite duration."""
    from llm_tools import common

    assert common.parse_epoch("9" * 400) is None
    assert common.parse_epoch("1700000000") == 1700000000
    assert common.fmt_duration(float("inf")) == "-"
    assert bridge_tui._span(float("inf")) == "-"
    assert bridge_tui._age("9" * 400) == "—"


def test_duration_text_parsing_ignores_text_too_long_to_be_a_duration() -> None:
    """The unit pattern restarts at every digit, so a 100 000-digit text took quadratic time before it was rejected."""
    from tests.fuzz.harness import watchdog

    with watchdog(5):
        assert bridge_tui._duration_seconds("1" * 100_000) is None
        assert bridge_tui._duration_seconds("2d 10h 35m") == 2 * 86400 + 10 * 3600 + 35 * 60
        assert bridge_tui._duration_seconds("~4h 59m") == 4 * 3600 + 59 * 60


def test_parse_epoch_treats_infinity_and_nan_as_unknown() -> None:
    from llm_tools import common

    assert common.parse_epoch(float("inf")) is None
    assert common.parse_epoch(float("-inf")) is None
    assert common.parse_epoch(float("nan")) is None
    assert common.parse_epoch(1700000000.9) == 1700000000


# --- markdown preview --------------------------------------------------------------------------

@pytest.mark.parametrize("width", [1, 8, 9, 12, 20, 40])
def test_reflowed_markdown_rows_fit_the_width_in_cells(width: int) -> None:
    """textwrap counts characters, so wide text overflowed the row; a deep list marker left no room at narrow widths."""
    from tests.fuzz.harness import display_width

    text = "\n".join([
        "丧" * 30, "", "- item " + "🧠 " * 30, "", "123. " + "wide ＷＩＤＥ " * 10, "", "      - nested item text that is long enough to wrap",
        "", "plain 丧丧丧丧 mixed text with wide characters inside a longer paragraph of words",
    ])
    rows = bridge_tui._reflow_markdown(text, width, 200)
    effective = max(8, width)
    assert rows and all(display_width(line) <= effective for line, _role in rows), [line for line, _ in rows if display_width(line) > effective]
    shown = "".join(line for line, _role in rows).replace(" ", "")
    assert "丧" * 30 in shown.replace("\n", "") or shown.count("丧") >= 30  # nothing was dropped


# --- documents ---------------------------------------------------------------------------------

@pytest.mark.parametrize("line", ["# a" + " " * 20_000 + "x", "## " + "# " * 20_000 + "x", "#\t" * 10_000 + "x", "# " + "a" * 50_000 + " " * 20_000])
def test_heading_detection_takes_linear_time(line: str) -> None:
    """The heading pattern backtracked cubically on a heading followed by a long run of blanks: one 1000-blank line froze the viewer for 14 s."""
    from llm_tools import bridge_documents
    from tests.fuzz.harness import watchdog

    with watchdog(10):
        assert bridge_documents.heading_label(line) is not None
        viewer = bridge_documents.DocumentViewer("doc", line + "\nnext")
        assert viewer.headings()
        rows = bridge_tui._reflow_markdown_rows(line, 80, 50)
        assert rows


def test_heading_label_drops_closing_marks_and_blanks() -> None:
    from llm_tools.bridge_documents import heading_label

    assert heading_label("# Title") == "Title"
    assert heading_label("   ## Title with  spaces  ##   ") == "Title with  spaces"
    assert heading_label("###### deep") == "deep"
    assert heading_label("####### seven") is None
    assert heading_label("#nospace") is None
    assert heading_label("    # indented four") is None
    assert heading_label("plain") is None
    assert heading_label("# ") == ""


def test_markdown_with_a_very_long_line_renders_in_the_background() -> None:
    """Rich needed 6 s for one 4 600-tab line of a 7 KB source, and the viewer rendered sources under 32 KB on the drawing thread."""
    from llm_tools.bridge_documents import DocumentViewer
    from tests.fuzz.harness import watchdog

    viewer = DocumentViewer("doc", "`" + "\t" * 4600 + "`\nnext line", rendered=True, wrap=False)
    with watchdog(3):
        rows = viewer.rich_lines(80)
    assert rows[0][0][0].startswith("[Rendering Markdown")
    assert viewer.rendering


def test_markdown_with_many_lines_renders_in_the_background() -> None:
    """Syntax highlighting costs about 4 ms per line of a fenced block: 1 600 short lines (10 KB) took 7 s on the drawing thread."""
    from llm_tools.bridge_documents import DocumentViewer
    from tests.fuzz.harness import watchdog

    viewer = DocumentViewer("doc", "```python\n" + "\n".join(f"x = {number}" for number in range(2500)), rendered=True)
    with watchdog(3):
        rows = viewer.rich_lines(80)
    assert rows[0][0][0].startswith("[Rendering Markdown")


def test_middle_clip_of_a_long_run_of_zero_width_characters_is_linear() -> None:
    """The tail was rebuilt and re-measured for every character, and zero-width marks never end the loop."""
    from llm_tools.bridge_switcher import middle_clip
    from tests.fuzz.harness import watchdog

    with watchdog(5):
        clipped = middle_clip("a" + "\u0301" * 60_000 + "z", 10)
    assert clipped.startswith("a") and clipped.endswith("z")


@pytest.mark.parametrize("width", [1, 2, 3, 5, 7, 8, 9])
def test_wrapped_rows_containing_tabs_fit_the_width(width: int) -> None:
    """A tab expanded to eight cells used to make a wrapped row wider than a viewport narrower than eight cells."""
    from llm_tools.bridge_documents import _wrap_cells
    from tests.fuzz.harness import display_width

    for line in ("\t", "a\tb", "\t\t\tx", "ab\t\tcd\t", "​\t", "\tçé"):
        rows = _wrap_cells(line, width)
        assert all(display_width(row) <= width for row in rows), (line, width, rows)
        assert "".join(rows).replace(" ", "") == line.replace("\t", "").replace(" ", "")


@pytest.mark.parametrize("value", ["15;\u00b2", "0;" + "9" * 5000, "0;\u0663", "15;"])
def test_colorfgbg_that_is_not_a_small_number_is_ignored(value: str) -> None:
    """str.isdigit accepts superscripts that int() rejects, and int() refuses more than 4300 digits."""
    from llm_tools import bridge_theme

    assert bridge_theme._background_is_light(value) in {None, True, False}
    resolved = bridge_theme.resolve({"COLORFGBG": value, "LLM_BRIDGE_THEME": "auto", "TERM": "xterm"}, probe=False)
    assert resolved.polarity in {"dark", "light"}


# --- theme overrides ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1;38;5;9999", "38;5;256", "48;5;300", "38;2;256;0;0", "38;2;0;999;0"])
def test_out_of_range_extended_colour_override_is_rejected(value: str) -> None:
    """A user override with a colour index or channel above 255 used to be clamped silently instead of warning."""
    from llm_tools import bridge_theme

    with pytest.raises(ValueError):
        bridge_theme.parse_style(value)
    resolved = bridge_theme.resolve({"LLM_BRIDGE_COLOR_ERROR": value, "LLM_BRIDGE_THEME": "dark"}, probe=False)
    assert any(warning.startswith("LLM_BRIDGE_COLOR_ERROR:") for warning in resolved.warnings)
    style = bridge_theme.parse_sgr(value)  # decoding of built-in palettes still clamps
    assert style.fg is not None or style.bg is not None
