"""Large-input performance of the Bridge console, document viewer and composer.

Speed is checked by counting work (characters classified, calls made) and with
generous wall-clock bounds. Output is checked against reference implementations
of the previous whole-string algorithms, so a faster path cannot change what is drawn.
"""

from __future__ import annotations

import curses
import random
import threading
import time
import unicodedata

import pytest

from llm_tools import bridge_documents as documents
from llm_tools import bridge_tui as tui
from llm_tools.bridge_input import cell_width
from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_store import BridgeStore

ALPHABET = ["a", "b", " ", "\n", "\t", "\x00", "\x07", "\x7f", "\x85", "é", "é", "́", "日", "本", "​", "‮",
            "\ud800", "😀", " ", " ", "\r", "x", "yy", "#", "- ", "| "]
RANDOM = random.Random(11)
SAMPLES = (
    ["", "plain ascii text", "a  b\tc\nd", "日本語テキスト wide", "combining éé", "ctrl\x00\x07chars\x7f",
     "bidi ‮ evil ​", "x" * 5000, " " * 3000 + "tail", "́" * 3000 + "z", "日" * 2000, "\n" * 500 + "x",
     "ab ​" * 2000]
    + ["".join(RANDOM.choice(ALPHABET) for _ in range(size)) for size in (10, 50, 300, 1000, 5000, 20000) for _ in range(4)]
)


# --- reference implementations: the whole-string algorithms before the speed-up ---------------

def old_text(value: object, limit: int = 1000) -> str:
    clean = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cs"} else char
        for char in str(value)
        if unicodedata.category(char) != "Cf"
    )
    return " ".join(clean.split())[:limit]


def old_task_prompt(value: str) -> str:
    clean = "".join(
        char if char == "\n" or unicodedata.category(char) not in {"Cc", "Cs"} else " "
        for char in value
    )
    return clean if len(clean) <= tui._MAX_DETAIL_TEXT else clean[: tui._MAX_DETAIL_TEXT - 1] + tui.glyph("ellipsis")


def old_put_text(value: object, width: int) -> str:
    safe = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cs"} else char
        for char in str(value).expandtabs(8)
        if unicodedata.category(char) != "Cf"
    )
    return tui.cell_slice(tui.localize(safe), 0, width)


def old_slice(value: str, start: int, width: int) -> str:
    output, cells, visible = [], 0, 0
    for char in value:
        if char == "\t":
            amount = 8 - cells % 8
            rendered = " " * amount
        else:
            amount, rendered = cell_width(char), char
        if cells + amount <= start or cells < start:
            cells += amount
            continue
        if visible + amount > width:
            break
        output.append(rendered)
        visible += amount
        cells += amount
    return "".join(output)


def old_caret_view(line: str, column: int, width: int) -> tuple[str, int]:
    caret = cell_width(line[:column])
    start = max(0, caret - max(0, width - 2))
    return old_slice(line, start, max(1, width - 1)), caret - start


def old_wrap(line: str, width: int) -> list[str]:
    width = max(1, width)
    rows: list[str] = []
    row, cells = "", 0
    for char in line:
        amount = min(8 - cells % 8, width) if char == "\t" else cell_width(char)  # a tab never outgrows the row
        if cells and cells + amount > width:
            rows.append(row)
            row, cells = "", 0
            if char == "\t":
                amount = min(8, width)
        chars = " " * amount if char == "\t" else char
        row += chars
        cells += amount
        if cells >= width:
            rows.append(row)
            row, cells = "", 0
    if row or not rows:
        rows.append(row)
    return rows


def old_find(source: str, query: str, last: int, reverse: bool) -> int:
    needle = query.casefold()
    if not needle:
        return -1
    pieces: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(source):
        piece = char.casefold()
        pieces.append(piece)
        offsets.extend([index] * len(piece))
    folded = "".join(pieces)
    if reverse:
        limit = len(folded) if last < 0 else next((i for i, source_index in enumerate(offsets) if source_index >= last), len(folded))
        pos = folded.rfind(needle, 0, limit)
    else:
        start = 0 if last < 0 else next((i for i, source_index in enumerate(offsets) if source_index > last), len(folded))
        pos = folded.find(needle, start)
    return offsets[pos] if pos >= 0 else -1


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str, int]] = []
        self.rows: dict[int, str] = {}

    def getmaxyx(self): return 24, 80
    def erase(self): self.rows = {}
    def move(self, *_a): pass
    def noutrefresh(self): pass

    def addnstr(self, y, x, value, count, *_attrs):
        self.calls.append((y, x, value, count))
        self.rows[y] = self.rows.get(y, "")[:x] + value[:count]


class CountingCategory:
    """Counts `unicodedata.category` calls made by the module under test."""

    def __init__(self, monkeypatch) -> None:
        self.calls = 0
        real = unicodedata.category

        def counted(char):
            self.calls += 1
            return real(char)

        monkeypatch.setattr(tui.unicodedata, "category", counted)


# --- 1. sanitising --------------------------------------------------------------------------

@pytest.mark.parametrize("limit", [5, 40, 1000])
def test_text_matches_the_whole_string_algorithm(limit):
    for sample in SAMPLES:
        assert tui._text(sample, limit) == old_text(sample, limit)


def test_text_keeps_a_short_result_when_whitespace_and_format_characters_dominate():
    # More than limit * 4 + 256 leading characters vanish; the result must still come from the whole string.
    sample = " ​\n\t" * 5000 + "tail of the text"
    assert tui._text(sample, 12) == old_text(sample, 12) == "tail of the "
    assert tui._text(sample, 50) == "tail of the text"


def test_task_prompt_cleaning_matches_the_whole_string_algorithm():
    for sample in SAMPLES:
        assert tui._bounded_task_value(sample, preserve_newlines=True) == old_task_prompt(sample)
    long = ("line one\n\x00tab\there ‮\n" * 3000)
    assert tui._bounded_task_value(long, preserve_newlines=True) == old_task_prompt(long)


def test_put_draws_what_the_whole_string_algorithm_drew():
    for sample in SAMPLES:
        for width in (1, 5, 20, 120):
            window = Recorder()
            used = tui._put(window, 0, 0, sample, width)
            expected = old_put_text(sample, width)
            assert window.calls == [(0, 0, expected, width)]
            assert used == cell_width(expected)


def test_sanitising_classifies_distinct_characters_not_every_character(monkeypatch):
    counter = CountingCategory(monkeypatch)
    huge = ("héllo wörld 日本語 é ‮ x\x07 " * 40000)[:1_000_000]
    tui._text(huge, 240)
    tui._bounded_task_value(huge, preserve_newlines=True)
    tui._put(Recorder(), 0, 0, huge, 120)
    assert counter.calls < 500  # the previous code made about 3 million calls here


def test_bounded_snapshot_of_a_one_mebibyte_objective_is_cheap():
    text = ("héllo wörld 日本語 é ‮ x " * 40000)[:1_000_000]
    data = {
        "mission": {"id": "m1", "name": "n", "objective": text, "status": "RUNNING", "cwd": "/", "updated_at": "now"},
        "tasks": [{"id": index, "ordinal": index, "title": "t", "status": "READY", "prompt": text} for index in range(10)],
    }
    started = time.perf_counter()
    projected = tui._bounded_snapshot(data)
    elapsed = time.perf_counter() - started
    assert len(projected["mission"]["objective"]) <= 240
    assert len(projected["tasks"][0]["prompt"]) <= tui._MAX_DETAIL_TEXT
    assert elapsed < 0.25  # about 5 s before: 0.5 s per megabyte of non-ASCII text


def test_console_projection_reads_only_the_head_of_each_task_prompt(tmp_path):
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("one", "objective", cwd=tmp_path, config={"providers": "codex"})
    store.add_task(mission["id"], "p" * 50_000, title="big")
    full = project_snapshot(store, mission["id"])["tasks"][0]
    head = project_snapshot(store, mission["id"], prompt_chars=100)["tasks"][0]
    assert len(full["prompt"]) == 50_000
    assert head["prompt"] == "p" * 100 and head["original_prompt"] == "p" * 100
    assert {key: value for key, value in head.items() if key not in {"prompt", "original_prompt"}} == \
        {key: value for key, value in full.items() if key not in {"prompt", "original_prompt"}}
    assert store.get_task(int(full["id"]))["prompt"] == "p" * 50_000  # `task show` still has all of it


def test_console_refresh_asks_the_reader_for_a_bounded_prompt_and_shows_the_same_task():
    prompt = "word " * 20_000
    seen: dict[str, object] = {}

    def reader(mission_id, **kwargs):
        seen.update(kwargs)
        return {"mission": {"id": mission_id}, "tasks": [{"id": 1, "ordinal": 1, "title": "t", "prompt": prompt[: kwargs["prompt_chars"]]}]}

    state = tui._ConsoleState("mission", lambda *_: "", reader, full=True)
    state.refresh(force=True)
    deadline = time.monotonic() + 5
    while state.snapshot.get("tasks") in (None, []) and time.monotonic() < deadline:
        state.refresh(time.monotonic() + 1, force=True)
        time.sleep(0.01)
    assert seen["prompt_chars"] == tui._MAX_DETAIL_TEXT + 1
    shown = state.snapshot["tasks"][0]["prompt"]
    assert shown == tui._bounded_task_value(prompt, preserve_newlines=True)
    assert len(shown) == tui._MAX_DETAIL_TEXT  # the cut is marked, exactly as for the full text


# --- 2. document viewer ---------------------------------------------------------------------

def test_wrap_cells_matches_the_per_character_algorithm():
    for sample in SAMPLES + ["x" * 7, "x" * 8, "", "a\tb", "日本" * 9]:
        for line in sample.split("\n")[:40]:
            for width in (1, 2, 7, 8, 40, 120):
                assert documents._wrap_cells(line, width) == old_wrap(line, width)
                assert documents._wrap_count(line, width) == len(old_wrap(line, width))


def test_find_casefold_matches_the_offset_table_algorithm():
    sources = SAMPLES[:16] + ["Straße STRASSE ß ẞ İi", "ǅ ǆ ﬃ ffi", "xxXxx" * 50]
    for source in sources:
        for query in ("a", "ss", "É", "x", "日", "é", "FFI", "i̇"):
            for last in (-1, 0, 3, 50, len(source), len(source) + 5):
                for reverse in (False, True):
                    assert documents._find_casefold(source, query, last, reverse) == old_find(source, query, last, reverse)


def test_find_casefold_does_not_build_per_character_tables_for_one_to_one_text():
    source = ("lorem ipsum dolor sit amet " * 40000)[:1_000_000] + "NEEDLE"
    started = time.perf_counter()
    assert documents._find_casefold(source, "needle", -1, False) == len(source) - 6
    assert documents._find_casefold(source, "lorem", len(source), True) == source.rfind("lorem")
    assert time.perf_counter() - started < 0.5  # the offset tables took about 0.6 s per search


@pytest.fixture(autouse=True)
def _no_curses_screen(monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda _v: None)


def _document_state(source: str, **kwargs):
    state = tui._ConsoleState("mission", lambda *_: "", lambda _: {}, full=True)
    state.document_viewer = documents.DocumentViewer("doc", source, **kwargs)
    return state


def _draw(state) -> Recorder:
    window = Recorder()
    tui._draw_document(window, state)
    return window


def test_headings_are_computed_only_when_the_heading_list_is_open(monkeypatch):
    source = "# Title\n" + "body text\n" * 200 + "## Next\n"
    state = _document_state(source)
    viewer = state.document_viewer
    calls = []
    real = viewer.headings
    monkeypatch.setattr(viewer, "headings", lambda: calls.append(1) or real())
    _draw(state)
    viewer.scroll(3, 10)
    _draw(state)
    state.handle_key("j")
    state.handle_key("KEY_NPAGE")
    assert calls == []
    state.document_heading_list = True
    window = _draw(state)
    assert calls == [1]
    assert "Title" in window.rows[1]


def test_headings_are_cached_per_width_and_wrap(monkeypatch):
    viewer = documents.DocumentViewer("doc", "# One\n" + "word " * 100 + "\n## Two\n")
    viewer.width = 20
    first = viewer.headings()
    matches = []
    real = documents.heading_label
    monkeypatch.setattr(documents, "heading_label", lambda line: matches.append(line) or real(line))
    assert viewer.headings() == first
    assert matches == []
    viewer.width = 40
    assert viewer.headings() != first or matches  # a new width is a new scan
    assert [label for _n, label, _row in first] == ["One", "Two"]
    assert first[1][2] == 1 + len(old_wrap("word " * 100, 20))


def test_headings_match_the_per_line_scan_for_wrapped_and_unwrapped_views():
    source = "# Title\n\nSome text " + "word " * 60 + "\n## Sub 日本\n\n```\ncode\n```\n   ### H3 ###\n\t# not\n" + "".join(RANDOM.choice(ALPHABET) for _ in range(500))
    for wrap in (True, False):
        for width in (20, 80):
            viewer = documents.DocumentViewer("t", source, wrap=wrap)
            viewer.width = width
            expected, visual = [], 0
            import re
            for number, line in enumerate(source.split("\n"), 1):
                match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
                if match:
                    expected.append((number, match.group(2), visual))
                visual += len(old_wrap(line, width)) if wrap else 1
            assert viewer.headings() == expected


def test_scrolling_a_large_document_costs_the_visible_rows_only():
    source = ("word " * 15 + "\n") * 14000
    state = _document_state(source)
    viewer = state.document_viewer
    _draw(state)
    deadline = time.monotonic() + 5
    while viewer.rendering and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not viewer.rendering
    started = time.perf_counter()
    for _ in range(20):
        viewer.scroll(3, 22)
        window = _draw(state)
    per_frame = (time.perf_counter() - started) / 20
    assert per_frame < 0.05  # 0.3 s per frame before
    assert window.rows[1].startswith("word word")


def test_large_document_layout_runs_off_the_ui_thread_and_matches_the_wrapped_result():
    source = ("日本語 text with wide characters and a long tail " * 3 + "\n") * 6000
    assert len(source) > documents.LAYOUT_ASYNC_CHARS
    viewer = documents.DocumentViewer("doc", source)
    started = time.perf_counter()
    first = viewer.visible(10, 40)
    assert time.perf_counter() - started < 0.5
    assert viewer.rendering and len(first) == 10  # unwrapped lines stand in until the layout lands
    deadline = time.monotonic() + 20
    while viewer.rendering and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not viewer.rendering
    expected = [part for line in source.split("\n") for part in old_wrap(line, 40)]
    assert viewer.layout_rows == len(expected)
    assert viewer.visible(10, 40) == expected[:10]
    viewer.end(10)
    assert viewer.top == len(expected) - 10
    assert viewer.visible(10, 40) == expected[-10:]


def test_released_viewer_ignores_a_layout_that_finishes_later():
    viewer = documents.DocumentViewer("doc", "word " * 100_000)
    viewer.visible(10, 40)
    viewer.release()
    deadline = time.monotonic() + 10
    while viewer.rendering and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not viewer._line_cache and not viewer.source


def test_search_after_an_anchor_wraps_only_the_rows_it_shows():
    source = "".join(f"line {index} " + "word " * 30 + "\n" for index in range(5000))
    viewer = documents.DocumentViewer("doc", source[:200_000])
    viewer.visible(10, 40)
    assert viewer.search("line 1000") is not None
    started = time.perf_counter()
    rows = viewer.visible(10, 40)
    assert time.perf_counter() - started < 0.1
    assert rows[0].startswith("line 1000")
    expected = [part for line in viewer.source_lines[viewer.anchor_line:] for part in old_wrap(line[viewer.anchor_column:] if line is viewer.source_lines[viewer.anchor_line] else line, 40)]
    assert rows == expected[:10]


def test_footer_values_are_measured_once():
    viewer = documents.DocumentViewer("doc", "héllo " * 1000)
    assert viewer.source_size == len("héllo ".encode("utf-8")) * 1000
    viewer.query = "LLO"
    assert viewer.search_count == 1000
    viewer.source = ""  # a second read must come from the cache, not from the source
    assert viewer.source_size == len("héllo ".encode("utf-8")) * 1000
    assert viewer.search_count == 1000
    viewer.query = "x"
    assert viewer.search_count == 0


# --- 3. composer ----------------------------------------------------------------------------

def test_caret_view_matches_the_whole_line_measurement():
    rng = random.Random(3)
    pieces = ["a", "b", " ", "\t", "\t", "日", "本", "é", "́", "​", "\x07", "😀", "x", "y", "zz"]
    for _ in range(4000):
        size = rng.choice([5, 30, 100, 400, 1500])
        line = "".join(rng.choice(pieces) for _ in range(size))
        if rng.random() < 0.3:
            line = "".join(rng.choice(["́", "a"]) for _ in range(size * 3))  # zero-width runs
        column = rng.randint(0, len(line))
        width = rng.choice([1, 2, 3, 5, 10, 40, 80])
        assert tui._caret_view(line, column, width) == old_caret_view(line, column, width)


@pytest.mark.parametrize("line", [
    "x" * 1_000_000,
    "日本語テキスト" * 100_000,
    "héllo wörld\t" * 80_000,
])
def test_caret_view_of_a_one_mebibyte_line_is_cheap(line):
    started = time.perf_counter()
    for column in (len(line), len(line) - 7, len(line) // 2):
        view, caret = tui._caret_view(line, column, 120)
        assert 0 <= caret <= 118 and cell_width(view) <= 119
    assert time.perf_counter() - started < 0.5  # 0.15 to 0.4 s per call before


def test_composer_frame_of_a_long_single_line_paste_is_fast(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    payload = "日本語 wide text " * 60_000
    payload = payload[: 1_000_000 // 3]

    class Window:
        timeout_ms = 0

        def __init__(self, keys):
            self.keys = iter(keys)
            self.frames = []
            self.rows = []

        def keypad(self, _e): pass
        def timeout(self, ms): self.timeout_ms = ms
        def getmaxyx(self): return 24, 80
        def erase(self): self.rows = []
        def clear(self): self.rows = []
        def addnstr(self, y, x, text, _w, *_a): self.rows.append((y, x, text))
        def move(self, y, x): self.frames.append(time.perf_counter())
        def refresh(self): pass
        def get_wch(self): return next(self.keys)

    window = Window(["\x1b[200~" + payload + "\x1b[201~", "x", "y", "\x13"])
    started = time.perf_counter()
    result = tui.compose_document("", window=window)
    elapsed = time.perf_counter() - started
    assert result == payload + "xy"
    assert elapsed < 3  # each frame measured the whole line before: seconds for this paste
    assert window.rows[-1][2].startswith("F2/Ctrl-S") or any("full source" in row[2] for row in window.rows)


def test_periodic_checkpoint_runs_off_the_ui_thread_and_keeps_atomic_fsynced_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    writers: list[str] = []
    real_fsync = tui.os.fsync
    ui = threading.current_thread()

    def spying_fsync(fd):
        writers.append("ui" if threading.current_thread() is ui else "worker")
        real_fsync(fd)

    monkeypatch.setattr(tui.os, "fsync", spying_fsync)
    replaced: list[str] = []
    real_replace = tui.os.replace
    monkeypatch.setattr(tui.os, "replace", lambda a, b: replaced.append(str(b)) or real_replace(a, b))

    class Window:
        def __init__(self): self.calls = 0
        def keypad(self, _e): pass
        def timeout(self, _ms): pass
        def getmaxyx(self): return 24, 80
        def erase(self): pass
        def clear(self): pass
        def addnstr(self, *_a): pass
        def move(self, *_a): pass
        def refresh(self): pass

        def get_wch(self):
            self.calls += 1
            if self.calls == 1:
                return "A"
            if self.calls == 2:
                time.sleep(0.9)
                raise curses.error()  # lets the 0.8 s periodic checkpoint fire
            if self.calls == 3:
                time.sleep(0.3)  # the worker finishes while the loop waits
                raise curses.error()
            if self.calls == 4:
                recovery = tui.LAST_RECOVERY_DRAFT
                assert recovery is not None and recovery.read_bytes() == b"A"
                return "\x04"  # discard
            raise AssertionError("loop continued after discard")

    assert tui.compose_document("", window=Window()) is None
    assert writers[0] == "ui"  # the initial checkpoint is synchronous
    assert "worker" in writers[1:]
    assert all(path == str(replaced[0]) for path in replaced)  # renamed over the one recovery file
    assert tui.LAST_RECOVERY_DRAFT is None
    assert not list((tmp_path / "state" / "drafts").iterdir())  # discard leaves neither draft nor temp file


def test_unchanged_text_is_not_written_again(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    writes: list[int] = []
    real_replace = tui.os.replace
    monkeypatch.setattr(tui.os, "replace", lambda a, b: writes.append(1) or real_replace(a, b))

    class Window:
        def __init__(self): self.calls = 0
        def keypad(self, _e): pass
        def timeout(self, _ms): pass
        def getmaxyx(self): return 24, 80
        def erase(self): pass
        def clear(self): pass
        def addnstr(self, *_a): pass
        def move(self, *_a): pass
        def refresh(self): pass

        def get_wch(self):
            self.calls += 1
            if self.calls <= 3:
                time.sleep(0.85)
                raise curses.error()
            return "\x13"

    assert tui.compose_document("unchanged", window=Window()) == "unchanged"
    assert len(writes) == 2  # the initial checkpoint and the one on save; idle ticks write nothing


# --- 4. Markdown reflow ---------------------------------------------------------------------

def test_reflow_is_remembered_per_text_width_and_limit(monkeypatch):
    text = "# Heading\n\n" + "A paragraph of words. " * 200 + "\n\n- item one\n- item two\n```\ncode\n```\n"
    expected = tui._reflow_markdown_rows(text, 60, 20)
    calls = []
    real = tui._reflow_markdown_rows
    monkeypatch.setattr(tui, "_reflow_markdown_rows", lambda *args: calls.append(args) or real(*args))
    tui._REFLOW_CACHE.clear()
    first = tui._reflow_markdown(text, 60, 20)
    again = tui._reflow_markdown(text, 60, 20)
    assert first == again == expected
    assert len(calls) == 1  # the second call came from the cache
    assert again is not first  # callers get their own list
    again.append(("mutated", "info"))
    assert tui._reflow_markdown(text, 60, 20) == expected
    tui._reflow_markdown(text, 80, 20)
    tui._reflow_markdown(text, 60, 5)
    assert len(calls) == 3  # a different width or limit is computed again
