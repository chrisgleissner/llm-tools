"""CLIUX-012, CLIUX-013, CLIUX-027 and CLIUX-050: slash-command list, composer, help pages."""
from __future__ import annotations

import curses
import os
import time
from pathlib import Path

import pytest

from llm_tools import bridge_glyphs, bridge_tui, bridge_viewer
from llm_tools.bridge_documents import DocumentViewer
from llm_tools.bridge_help import HOME_HELP, topic_text, view_help
from tests.fakes import FakeScreen, ScriptExhausted
from tests.test_bridge_tui_experience import full_snapshot, make_state

CODESETS = {"utf8": "UTF-8", "ascii": "ANSI_X3.4-1968"}


@pytest.fixture(params=["utf8", "ascii"])
def glyph_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(bridge_glyphs, "_locale_codeset", lambda: CODESETS[request.param])
    bridge_glyphs.ascii_mode.cache_clear()
    yield request.param
    bridge_glyphs.ascii_mode.cache_clear()


def _screen(monkeypatch: pytest.MonkeyPatch, rows: int = 24, columns: int = 80, keys: tuple[object, ...] = ()) -> FakeScreen:
    return FakeScreen(rows, columns, keys=keys).install(monkeypatch)


def _typed(state: bridge_tui._ConsoleState, text: str) -> None:
    for char in text:
        state.handle_key(char)


# --- CLIUX-012: slash-command list ---------------------------------------------------------------------


def test_typing_slash_lists_commands_with_descriptions_and_filters_as_typed(monkeypatch, glyph_mode) -> None:
    state = make_state(full_snapshot(), view="Tasks")
    screen = _screen(monkeypatch)
    _typed(state, "/")
    bridge_tui._draw_full(screen, state)
    rows = screen.rows()
    assert any(row.startswith("Commands") for row in rows)
    start = next(row for row in rows if row.lstrip().startswith("/start"))
    assert "Launch the provider on the first ready task" in start
    assert any(row.lstrip().startswith("/approve") for row in rows)
    assert any("more" in row and "keep typing" in row for row in rows)  # 24 rows cannot hold every command
    assert rows[22].startswith("› /") or rows[22].startswith("> /")

    _typed(state, "s")
    screen = _screen(monkeypatch)
    bridge_tui._draw_full(screen, state)
    listed = [row.split()[0] for row in screen.rows() if row.lstrip().startswith("/s")]
    assert {"/start", "/status", "/steer", "/stop", "/sessions"} <= set(listed)
    assert not any(row.lstrip().startswith("/pause") for row in screen.rows())
    for row in screen.rows():
        assert len(row) <= 80
        if glyph_mode == "ascii":
            assert row.isascii()


def test_slash_list_says_when_nothing_matches_and_is_absent_for_other_input(monkeypatch) -> None:
    state = make_state(full_snapshot(), view="Tasks")
    _typed(state, "/zzz")
    screen = _screen(monkeypatch)
    bridge_tui._draw_full(screen, state)
    assert any("No command starts with /zzz" in row for row in screen.rows())
    state.handle_key("\x15")
    _typed(state, "status")
    screen = _screen(monkeypatch)
    bridge_tui._draw_full(screen, state)
    assert not any(row.startswith("Commands") for row in screen.rows())


def test_slash_list_fits_a_short_window(monkeypatch) -> None:
    state = make_state(full_snapshot(), view="Tasks")
    _typed(state, "/")
    for rows in (9, 12):
        screen = _screen(monkeypatch, rows=rows)
        bridge_tui._draw_full(screen, state)
        assert len(screen.rows()) <= rows


def test_compact_pane_names_the_matches_in_the_hint_row(monkeypatch) -> None:
    state = make_state(full_snapshot(), full=False, view="Tasks")
    _typed(state, "/pa")
    assert bridge_tui._hint_items(state)[0] == ["/pause"]
    state.handle_key("\x15")
    _typed(state, "/s")
    items, _closing = bridge_tui._hint_items(state)
    assert "/start" in items and "/stop" in items


def test_tab_completes_only_the_common_prefix_then_lists_the_matches() -> None:
    state = make_state(full_snapshot(), view="Tasks")
    _typed(state, "/pr")
    state.handle_key("\t")
    assert state.command == "/pro"  # /proposal and /providers share /pro
    assert not state.output
    state.handle_key("\t")
    assert state.command == "/pro"
    assert state.output[-1].startswith("Matches: ") and "/proposal" in state.output[-1] and "/providers" in state.output[-1]
    state.handle_key("\t")
    assert state.command == "/pro" and len(state.output) == 1  # no repeated line, no silent first-match pick
    _typed(state, "p")
    state.handle_key("\t")
    assert state.command == "/proposal"  # one match left


def test_tab_never_picks_the_first_of_several_matches() -> None:
    state = make_state(full_snapshot(), view="Tasks")
    _typed(state, "/st")
    before = state.command
    state.handle_key("\t")
    assert state.command == before  # /start /status /station /steer /stop stay ambiguous
    assert "Matches: " in state.output[-1]


def test_hidden_commands_are_not_offered() -> None:
    assert not any(name.startswith("/_") for name, _ in bridge_tui._slash_table())
    assert "/_station-gc" not in bridge_tui._command_completions("/")


# --- CLIUX-013 and CLIUX-050: composer -----------------------------------------------------------------


class _EscWindow(FakeScreen):
    """Delivers `ESC`, then a read timeout, as a terminal does after a lone Escape."""

    def get_wch(self) -> object:
        key = self._next_key()
        if key == "TIMEOUT":
            time.sleep(0.08)
            raise curses.error()
        return key


def test_composer_footer_labels_escape_and_counts_lines() -> None:
    assert bridge_tui._composer_status(0, 1_048_576, 1) == "F2/Ctrl-S Save · Esc close (draft kept) · Ctrl-D Discard · 1 line"
    assert bridge_tui._composer_status(40, 1_048_576, 3).endswith("· 3 lines")
    assert "bytes" not in bridge_tui._composer_status(838_860, 1_048_576, 3)  # exactly 80 percent
    assert bridge_tui._composer_status(838_861, 1_048_576, 3).endswith("· 838861/1048576 bytes")


def test_composer_shows_the_new_footer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    screen = _screen(monkeypatch, keys=("a",))
    with pytest.raises(ScriptExhausted):
        bridge_tui.compose_document("", title="Add task prompt", window=screen)
    assert screen.rows()[-1] == "F2/Ctrl-S Save · Esc close (draft kept) · Ctrl-D Discard · 1 line"


def test_empty_composer_shows_a_dim_placeholder_until_the_first_keystroke(monkeypatch, tmp_path, glyph_mode) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    screen = _screen(monkeypatch, keys=())
    with pytest.raises(ScriptExhausted):
        bridge_tui.compose_document("", title="Add task prompt", window=screen, placeholder=bridge_tui.TASK_PLACEHOLDER)
    rows = screen.rows()
    assert rows[1].startswith("First line = title. Describe the change.")
    assert "Acceptance:" in " ".join(rows[1:3]) and '"- criterion"' in " ".join(rows[1:3])
    assert any(call.text.startswith("First line = title") and call.attr & curses.A_DIM for call in screen.calls)

    screen = _screen(monkeypatch, keys=("x",))
    with pytest.raises(ScriptExhausted):
        bridge_tui.compose_document("", title="Add task prompt", window=screen, placeholder=bridge_tui.TASK_PLACEHOLDER)
    assert rows_without_placeholder(screen)
    assert screen.rows()[1] == "x"


def rows_without_placeholder(screen: FakeScreen) -> bool:
    return not any("First line" in row for row in screen.rows())


def test_a_non_empty_composer_has_no_placeholder(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    screen = _screen(monkeypatch)
    with pytest.raises(ScriptExhausted):
        bridge_tui.compose_document("Fix it", title="Add task prompt", window=screen, placeholder=bridge_tui.TASK_PLACEHOLDER)
    assert rows_without_placeholder(screen)


def test_esc_keeps_the_draft_and_ctrl_e_reopens_it_with_its_time(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    first = _EscWindow(24, 80, keys=(*"Add a fuzz test", "\x1b", "TIMEOUT")).install(monkeypatch)
    assert bridge_tui.compose_document("", title="Add task prompt", window=first, draft_prefix=bridge_tui.TASK_DRAFT_PREFIX) is None
    kept = bridge_tui.retained_composer_draft()
    assert kept is not None and kept[1] == "Add a fuzz test"
    assert kept[0].name.startswith(bridge_tui.TASK_DRAFT_PREFIX)

    # Ctrl-E in the console: the retained text comes back, with the notice.
    state = make_state(full_snapshot(), view="Tasks")
    seen: dict[str, object] = {}

    def fake_compose(initial: str, **options: object) -> None:
        seen["initial"], seen["options"] = initial, options
        return None

    monkeypatch.setattr(bridge_tui, "compose_document", fake_compose)
    window = _screen(monkeypatch)
    bridge_tui._compose_task_prompt(window, state)
    assert seen["initial"] == "Add a fuzz test"
    assert seen["options"]["restored_from"] == kept[0]  # type: ignore[index]
    assert seen["options"]["placeholder"] == bridge_tui.TASK_PLACEHOLDER  # type: ignore[index]


def test_restored_composer_says_when_the_draft_was_written_and_removes_the_old_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    drafts = tmp_path / "state" / "drafts"
    drafts.mkdir(parents=True, mode=0o700)
    old = drafts / (bridge_tui.TASK_DRAFT_PREFIX + "abc.md")
    old.write_text("Add a fuzz test", encoding="utf-8")
    written = time.mktime((2026, 11, 6, 18, 42, 0, 0, 0, -1))
    os.utime(old, (written, written))
    screen = _screen(monkeypatch)
    with pytest.raises(ScriptExhausted):
        bridge_tui.compose_document("Add a fuzz test", title="Add task prompt", window=screen, restored_from=old,
                                    draft_prefix=bridge_tui.TASK_DRAFT_PREFIX)
    assert screen.rows()[-2] == "Restored draft from 18:42 · Ctrl-D discard"
    assert not old.exists()
    assert bridge_tui.LAST_RECOVERY_DRAFT is not None and bridge_tui.LAST_RECOVERY_DRAFT.read_text() == "Add a fuzz test"


def test_only_recent_non_empty_task_drafts_are_offered(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    drafts = tmp_path / "state" / "drafts"
    drafts.mkdir(parents=True, mode=0o700)
    assert bridge_tui.retained_composer_draft() is None
    (drafts / (bridge_tui.TASK_DRAFT_PREFIX + "empty.md")).write_text("")
    (drafts / "composer-recovery-objective.md").write_text("an objective draft")  # another kind of draft
    assert bridge_tui.retained_composer_draft() is None
    stale = drafts / (bridge_tui.TASK_DRAFT_PREFIX + "stale.md")
    stale.write_text("old")
    long_ago = time.time() - 3 * 86400
    os.utime(stale, (long_ago, long_ago))
    assert bridge_tui.retained_composer_draft() is None
    fresh = drafts / (bridge_tui.TASK_DRAFT_PREFIX + "fresh.md")
    fresh.write_text("new")
    assert bridge_tui.retained_composer_draft() == (fresh, "new")


def test_closing_a_composer_with_text_says_the_draft_is_kept(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    state = make_state(full_snapshot(), view="Tasks")
    kept = tmp_path / "state" / "drafts" / (bridge_tui.TASK_DRAFT_PREFIX + "x.md")
    kept.parent.mkdir(parents=True, mode=0o700)
    kept.write_text("text")
    monkeypatch.setattr(bridge_tui, "LAST_RECOVERY_DRAFT", kept)
    monkeypatch.setattr(bridge_tui, "compose_document", lambda *_a, **_k: None)
    bridge_tui._compose_task_prompt(_screen(monkeypatch), state)
    assert state.output[-1] == "Draft kept · Ctrl-E reopens it"
    assert str(kept.parent) not in " ".join(state.output)  # no truncated path


# --- CLIUX-027: help pages -----------------------------------------------------------------------------


def _words_unbroken(source: str, rows: list[str]) -> bool:
    return " ".join(rows).split() == source.split()


@pytest.mark.parametrize("width", [40, 60, 80, 100])
def test_help_pages_wrap_at_word_boundaries(width: int) -> None:
    for text in (topic_text("quickstart"), topic_text("keys"), view_help("Tasks"), HOME_HELP):
        viewer = DocumentViewer("help", text, rendered=True, plain=True)
        viewer.resize(width)
        rows = ["".join(part for part, _role in row) for row in viewer.rich_lines(width)]
        assert all(len(row) <= width for row in rows)
        assert _words_unbroken(text, rows), f"a word was split at width {width}"


def test_help_opens_rendered_with_pager_footer_and_no_file_viewer_controls(monkeypatch, glyph_mode) -> None:
    state = make_state(full_snapshot(), view="Tasks")
    state.handle_key("?")
    viewer = state.document_viewer
    assert viewer is not None and viewer.plain and viewer.rendered
    screen = _screen(monkeypatch)
    bridge_tui._draw_document(screen, state)
    rows = screen.rows()
    assert rows[0] == ("Bridge help · Tasks" if glyph_mode == "utf8" else "Bridge help | Tasks")  # no "read-only · source"
    expected = "↑↓ scroll · / find · Esc close" if glyph_mode == "utf8" else "Up/Down scroll | / find | Esc close"
    assert rows[-1] == expected
    for forbidden in ("export", "mode", "wrap", "matches", "q close"):
        assert forbidden not in rows[-1]
    assert not any(row.rstrip().endswith("B") and "/" in row and row.split()[-1][:-1].isdigit() for row in rows)
    for key in ("m", "w", "s", "c", "e", "h", "KEY_LEFT", "KEY_RIGHT"):
        state.handle_key(key)
        assert state.document_viewer is viewer and viewer.rendered and not state.document_export_editing
    assert not state.document_heading_list
    state.handle_key("\x1b")
    assert state.document_viewer is None


def test_slash_help_and_the_quickstart_topic_also_open_as_help_pages(monkeypatch) -> None:
    state = make_state(full_snapshot(), view="Tasks")
    assert state._dispatch("/help")
    assert state.document_viewer is not None and state.document_viewer.plain
    state.handle_key("\x1b")
    assert state._dispatch("/help quickstart")
    assert state.document_viewer is not None and state.document_viewer.plain


def test_help_search_finds_text_in_the_reflowed_page() -> None:
    viewer = DocumentViewer("help", topic_text("quickstart"), rendered=True, plain=True)
    viewer.resize(60)
    assert viewer.search("approve") is not None


def test_home_help_is_a_home_screen_summary_that_links_to_the_quickstart() -> None:
    assert HOME_HELP.splitlines()[0].startswith("Home screen")
    assert "Run llm-bridge, then press N" not in HOME_HELP
    for item in ("N ", "new mission", "Enter", "/ ", "filter", "Q or Esc", "quickstart"):
        assert item in HOME_HELP


def test_home_help_switches_to_the_quickstart_on_t(monkeypatch) -> None:
    pages: list[tuple[str, str]] = []
    screen = _screen(monkeypatch, keys=("T", "q"))
    real_draw = bridge_tui._draw_document

    def spy(window: object, state: bridge_tui._ConsoleState) -> None:
        viewer = state.document_viewer
        assert viewer is not None
        pages.append((viewer.title, viewer.footer_extra))
        real_draw(window, state)  # type: ignore[arg-type]

    monkeypatch.setattr(bridge_tui, "_draw_document", spy)
    monkeypatch.setattr(bridge_viewer.bridge_hangup, "run_curses", lambda loop: loop(screen))
    assert bridge_viewer.run_document(
        HOME_HELP, title="Bridge help · home", plain=True,
        switch=("T", "quickstart", "Bridge help · quickstart", topic_text("quickstart")),
    ) == 0
    assert pages[0] == ("Bridge help · home", "T quickstart")
    assert pages[-1] == ("Bridge help · quickstart", "")
