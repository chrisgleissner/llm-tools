"""Source integrity acceptance independent of bounded screen projections."""

import hashlib
from pathlib import Path

import pytest

from llm_tools.bridge_input import BracketedPaste, TextBuffer, input_view, cell_width
from llm_tools.bridge_documents import DocumentViewer
from llm_tools.bridge_store import BridgeStore


@pytest.mark.parametrize("size", [30 * 1024, 32 * 1024, 64 * 1024, 256 * 1024])
@pytest.mark.parametrize("multiline", [False, True])
def test_large_composition_survives_storage_amendment_and_export(tmp_path: Path, size: int, multiline: bool) -> None:
    unit = "\t中 e\u0301 🙂  code  \n\n" if multiline else "中e\u0301🙂  "
    source = "FIRST\n" + unit * (size // len(unit.encode("utf-8"))) + "MIDDLE" + unit + "TAIL\n"
    buffer = TextBuffer()
    accepted, received = buffer.paste(source)
    assert accepted and received == len(source.encode("utf-8"))
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    database = tmp_path / "private" / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission = store.create_mission("Integrity", buffer.text, cwd=tmp_path, initial_prompt=buffer.text)
        task_id = mission["initial_task_id"]
        assert store.get_task(task_id)["prompt"] == source
        store.amend_task(task_id, prompt=buffer.text)
    with BridgeStore(database) as store:
        record = store.get_task(task_id)
        assert record["prompt"] == source and record["pending"] == {}
        exported = tmp_path / "export.md"
        exported.write_bytes(record["prompt"].encode("utf-8"))
    assert hashlib.sha256(exported.read_bytes()).hexdigest() == digest


def test_paste_ceiling_counts_utf8_and_rolls_back_every_byte() -> None:
    buffer = TextBuffer("retained  draft\n", 4)
    before = (buffer.text, buffer.cursor)
    source = "中" * (1_048_576 // 3 + 1)
    accepted, received = buffer.paste(source)
    assert not accepted and received > 1_048_576
    assert (buffer.text, buffer.cursor) == before


@pytest.mark.parametrize("unit", ["a", "中", "🙂", "e\u0301"])
def test_viewer_reaches_tail_of_one_large_line(unit: str) -> None:
    source = unit * (256 * 1024 // len(unit.encode("utf-8"))) + "TAIL_MARKER"
    viewer = DocumentViewer("Full prompt", source)
    viewer.visible(20, 79)
    viewer.end(20)
    assert "TAIL_MARKER" in "".join(viewer.visible(20, 79))
    viewer.home()
    assert viewer.search("TAIL_MARKER") is not None
    assert "TAIL_MARKER" in "".join(viewer.visible(20, 79))
    assert viewer.source == source


def test_paste_protocol_all_single_split_boundaries() -> None:
    source = "中🙂\n\t  /command\x13\x03TAIL\n"
    packet = BracketedPaste.START + source.encode() + BracketedPaste.END
    for boundary in range(1, len(packet)):
        parser = BracketedPaste()
        assert parser.feed(packet[:boundary]) is None
        assert parser.feed(packet[boundary:]) == source
        assert not parser.active


def test_oversize_paste_remains_owned_until_terminator() -> None:
    parser = BracketedPaste(max_bytes=10)
    assert parser.feed(BracketedPaste.START + b"x" * 11) is None
    assert parser.active, "oversize paste tail must not become commands or save keys"
    assert parser.feed(b"\x13\n/command") is None
    assert parser.active
    assert parser.feed(BracketedPaste.END) is None
    assert not parser.active and parser.error


@pytest.mark.parametrize("source", ["a  b " * 80, "中e\u0301🙂\t" * 80])
def test_input_view_follows_caret_home_middle_and_end(source: str) -> None:
    for cursor in (0, 3, len(source)//2, len(source)):
        visible, caret = input_view(source, cursor, 30)
        assert 0 <= caret < 30
        assert cell_width(visible) <= 29
        if cursor == 0:
            assert visible.startswith(source[:3].expandtabs(8))


def test_every_destination_visible_and_clickable_at_eighty_columns() -> None:
    from llm_tools.bridge_tui import VIEWS, _nav_layout, _nav_index_at
    for selected in range(8):
        text, entries = _nav_layout(selected, 80, "  ")
        assert len(entries) == 8 and len(text) <= 80
        for index, x, label in entries:
            assert VIEWS[index] in label
            assert _nav_index_at(x + 2, selected, 80, "  ") == index
        text, entries = _nav_layout(selected, 60, "  ")
        assert any(index == selected for index, _, _ in entries)
        assert len(text) <= 60


def test_scrolled_palette_click_and_keyboard_share_target_and_never_single_click_mutate() -> None:
    from llm_tools.bridge_tui import _ConsoleState, _scroll_start
    calls: list[str] = []
    state = _ConsoleState("mission", lambda _, command: calls.append(command), lambda _: {}, full=True)
    state.choose_view(6)
    state.handle_key("KEY_END")
    actions = state.palette_actions()
    start = _scroll_start(state.selected, len(actions), 6)
    state.handle_mouse(4, 2, 12, 80)
    assert state.selected == start+1
    assert not calls and not state.command
    assert not state.rows()[state.selected].startswith(">")
    state.handle_key("\x0b")  # Ctrl-K: letters filter the actions
    for char in "cancel":
        state.handle_key(char)
    assert len(state.palette_actions()) == 1
    state.handle_key("\n")
    assert state.confirm_command == "cancel" and not calls
    state.handle_key("n")
    assert not calls


def test_terminal_mission_palette_explains_disabled_edit() -> None:
    from llm_tools import bridge_actions
    from llm_tools.bridge_tui import _ConsoleState
    state = _ConsoleState("mission", lambda *_: None, lambda _: {}, full=True)
    state.snapshot = {"mission": {"status": "COMPLETED"}}
    assert "Add task" not in [item.label for item in state.palette_actions()]  # terminal missions list read-only actions
    action = next(item for item in bridge_actions.ACTIONS if item.label == "Add task")
    state.activate_action(action)
    assert not state.edit_document_requested
    assert "mission completed" in state.output[-1]


def test_history_and_search_restore_midline_draft_caret():
    from llm_tools.bridge_tui import _ConsoleState
    state = _ConsoleState('mission', lambda *_: '', lambda _: {}, full=True)
    state.history.append('previous command')
    for char in 'a  b ':
        state.handle_key(char)
    state.handle_key('KEY_LEFT')
    state.handle_key('KEY_LEFT')
    before = state.command, state.command_cursor
    state.handle_key('\x10')
    assert state.command == 'previous command'
    state.handle_key('\x0e')
    assert (state.command, state.command_cursor) == before
    state.handle_key('\x1b')
    state.handle_key('\x06')
    state.handle_key('q')
    state.handle_key('\n')
    assert state.search_query == 'q'
    assert (state.command, state.command_cursor) == before


def test_exact_interactive_ceiling_and_larger_file_import(tmp_path, monkeypatch, capsys):
    import json
    from llm_tools.bridge import main
    limit = 1_048_576
    buffer = TextBuffer()
    assert buffer.paste('é' * (limit//2))[0]
    assert buffer.size_bytes == limit
    assert not buffer.insert('x') and buffer.size_bytes == limit
    source = buffer.text + '\r\n\tTAIL\n'
    path = tmp_path / 'larger.md'
    path.write_bytes(source.encode())
    state = tmp_path / 'state'
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR', str(state))
    assert main(['mission', 'create', '--name', 'Large file', '--file', str(path), '--cwd', str(tmp_path), '--json']) == 0
    record = json.loads(capsys.readouterr().out)
    assert record['task']['prompt'].encode() == path.read_bytes()
    assert record['mission']['status'] == 'CREATED'
