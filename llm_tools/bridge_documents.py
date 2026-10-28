"""Full-source text document navigation and lazy Markdown rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import threading

from .bridge_input import wrapped_row_for_column, cell_width


@dataclass
class DocumentViewer:
    title: str
    source: str
    rendered: bool = False
    wrap: bool = True
    top: int = 0
    horizontal: int = 0
    query: str = ""
    match: int = -1
    match_offset: int = -1
    viewport_height: int = 20
    anchor_line: int = 0
    anchor_column: int = 0
    anchor_active: bool = False
    source_anchor: int = 0
    source_lines: tuple[str, ...] = field(init=False, repr=False)
    _line_cache: dict[tuple[bool, bool, int], tuple[str, ...]] = field(default_factory=dict, init=False, repr=False)
    _rich_cache: dict[tuple[int, bool], tuple[tuple[tuple[str, str], ...], ...]] = field(default_factory=dict, init=False, repr=False)
    _rich_pending: set[tuple[int, bool]] = field(default_factory=set, init=False, repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.source_lines = tuple(self.source.split("\n"))

    @property
    def rendering(self) -> bool:
        return bool(self._rich_pending)

    def rich_lines(self, width: int | None = None) -> tuple[tuple[tuple[str, str], ...], ...]:
        key = (max(20, width or self.width), self.wrap)
        if key not in self._rich_cache:
            if len(self.source.encode("utf-8")) > 32768:
                if not self._rich_pending and not self._released:
                    self._rich_pending.add(key)
                    source = self.source
                    def render() -> None:
                        rows = render_markdown_segments(source, key[0], key[1])
                        if not self._released:
                            if len(self._rich_cache) >= 4:
                                self._rich_cache.pop(next(iter(self._rich_cache)))
                            self._rich_cache[key] = rows
                            self._line_cache.clear()
                            if self.rendered and self.anchor_active:
                                self._seek_source_anchor(self.source_anchor)
                        self._rich_pending.discard(key)
                    threading.Thread(target=render, name="llm-bridge-markdown", daemon=True).start()
                return tuple(((line, "info"),) for line in ("[Rendering Markdown · source remains available]", *self.source_lines))
            if len(self._rich_cache) >= 4:
                self._rich_cache.pop(next(iter(self._rich_cache)))
            self._rich_cache[key] = render_markdown_segments(self.source, key[0], key[1])
        return self._rich_cache[key]

    def release(self) -> None:
        """Drop parsed/layout caches and source references when the viewer closes."""
        self._line_cache.clear()
        self._rich_cache.clear()
        self.source_lines = ()
        self.source = ""
        self._released = True

    def headings(self) -> list[tuple[int, str, int]]:
        """Return source heading line, plain label, and approximate visual row."""
        result = []
        visual = 0
        for number, line in enumerate(self.source_lines, 1):
            match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if match:
                result.append((number, match.group(2), visual))
            visual += len(_wrap_cells(line, max(1, self.width))) if self.wrap else 1
        return result

    def _capture_anchor(self) -> int:
        if self.match_offset >= 0 and not self.rendered:
            return self.match_offset
        if self.rendered:
            if self.match_offset >= 0 and self.query and any(self.query.casefold() in line.casefold() for line in self._layout()[self.top:self.top+self.viewport_height]):
                return self.match_offset
            return int(len(self.source) * self.top / max(1, len(self._layout())-1))
        visual = max(0, self.top)
        if not self.wrap:
            logical = min(visual, max(0, len(self.source_lines) - 1))
            return sum(len(line) + 1 for line in self.source_lines[:logical]) + _column_for_cells(self.source_lines[logical], self.horizontal)
        consumed = 0
        for logical, line in enumerate(self.source_lines):
            rows = _wrap_cells(line, max(1, self.width))
            if visual < consumed + len(rows):
                row = visual - consumed
                return sum(len(item) + 1 for item in self.source_lines[:logical]) + _column_for_wrapped_row(line, row, self.width)
            consumed += len(rows)
        return len(self.source)

    def _seek_source_anchor(self, anchor: int) -> None:
        anchor = min(len(self.source), max(0, anchor))
        prefix = self.source[:anchor]
        logical = min(prefix.count("\n"), max(0, len(self.source_lines) - 1))
        line_start = prefix.rfind("\n") + 1
        column = anchor - line_start
        if self.rendered:
            rows = self._layout()
            self.top = min(len(rows)-1, int((len(rows)-1) * anchor / max(1, len(self.source))))
            needle = self.query if anchor == self.match_offset else self.source[anchor:anchor+40].split("\n")[0].strip()
            if needle:
                matches = [index for index, row in enumerate(rows) if needle.casefold() in row.casefold()]
                if matches:
                    self.top = min(matches, key=lambda index: abs(index-self.top))
        elif self.wrap:
            preceding = sum(len(_wrap_cells(line, max(1, self.width))) for line in self.source_lines[:logical])
            row, _ = wrapped_row_for_column(self.source_lines[logical], column, self.width)
            self.top = preceding + row
        else:
            self.top = logical
            self.horizontal = cell_width(self.source_lines[logical][:column].expandtabs(8))

    def toggle_rendered(self) -> None:
        self.source_anchor = self._capture_anchor()
        self.rendered = not self.rendered
        self._seek_source_anchor(self.source_anchor)

    def toggle_wrap(self) -> None:
        self.source_anchor = self._capture_anchor()
        self.wrap = not self.wrap
        self._seek_source_anchor(self.source_anchor)

    def resize(self, width: int) -> None:
        next_width = max(1, width)
        if next_width != self.width:
            self.source_anchor = self._capture_anchor()
            self.width = next_width
            self._seek_source_anchor(self.source_anchor)

    def _layout(self, width: int | None = None) -> tuple[str, ...]:
        actual_width = max(1, width or self.width)
        key = (self.rendered, self.wrap, actual_width)
        if key not in self._line_cache:
            if len(self._line_cache) >= 4:
                self._line_cache.pop(next(iter(self._line_cache)))
            if self.rendered:
                source_rows = self.rich_lines(actual_width)
                rows = tuple("".join(text for text, _role in row) for row in source_rows)
            else:
                rows = self.source_lines
            if self.wrap and not self.rendered:
                rows = tuple(part for line in rows for part in _wrap_cells(line, actual_width))
            if self.rendered and self._rich_cache.get((max(20, actual_width), self.wrap)) is not source_rows:
                return rows or ("",)
            self._line_cache[key] = rows or ("",)
        return self._line_cache[key]

    @property
    def lines(self) -> list[str]:
        return list(self._layout())

    def display_lines(self) -> list[str]:
        if self.rendered:
            return ["".join(text for text, _role in row) for row in self.rich_lines()]
        return list(self.source_lines)

    def page(self, delta: int, height: int) -> None:
        self.anchor_active = False
        self.top = max(0, min(max(0, len(self.lines) - height), self.top + delta * max(1, height)))

    def scroll(self, delta: int, height: int) -> None:
        self.anchor_active = False
        self.top = max(0, min(max(0, len(self.lines) - height), self.top + delta))

    def home(self) -> None:
        self.anchor_active = False
        self.top = 0

    def end(self, height: int) -> None:
        self.anchor_active = False
        self.top = max(0, len(self.lines) - max(1, height))

    def search(self, query: str, *, reverse: bool = False) -> int | None:
        self.query = query
        if not query:
            self.match = -1
            return None
        if not self.rendered:
            position = _find_casefold(self.source, query, self.match_offset, reverse)
            if position >= 0:
                self.match_offset = position
                self.source_anchor = position
                prefix = self.source[:position]
                logical = prefix.count("\n")
                line_start = prefix.rfind("\n") + 1
                column = position - line_start
                line = self.source_lines[logical]
                self.anchor_line = logical
                self.anchor_column = column
                self.anchor_active = True
                if self.wrap:
                    visual_before = sum(len(_wrap_cells(item, self.width)) for item in self.source_lines[:logical])
                    row, cell = wrapped_row_for_column(line, column, self.width)
                    if cell and cell + cell_width(query) > self.width:
                        row += 1
                    self.top = visual_before + row
                else:
                    self.top = logical
                    self.horizontal = cell_width(line[:column])
                self.match = self.top
                return self.match
        lines = self.display_lines()
        matches = [idx for idx, line in enumerate(lines) if query.casefold() in line.casefold()]
        if matches:
            after = [idx for idx in matches if idx < self.match] if reverse and self.match >= 0 else [idx for idx in matches if idx > self.match] if self.match >= 0 else []
            index = (after[-1] if reverse and after else after[0] if after else matches[-1] if reverse else matches[0])
            self.match = index
            self.top = index
            self.match_offset = -1
            return index
        return None

    width: int = 80

    def visible(self, height: int, width: int) -> list[str]:
        self.width = max(1, width)
        self.viewport_height = max(1, height)
        if self.anchor_active and not self.rendered and self.wrap:
            source = [self.source_lines[self.anchor_line][self.anchor_column:], *self.source_lines[self.anchor_line + 1:]]
            rows = [part for line in source for part in _wrap_cells(line, self.width)]
            return rows[:self.viewport_height]
        rows = self._layout(self.width)
        return [_slice_cells(row, self.horizontal) if not self.wrap else row for row in rows[self.top:self.top + self.viewport_height]]

    @property
    def search_count(self) -> int:
        return self.source.casefold().count(self.query.casefold()) if self.query else 0


def _slice_cells(value: str, offset: int) -> str:
    cells = 0
    index = 0
    while index < len(value) and cells < offset:
        char = value[index]
        amount = cell_width(char)
        if cells + amount > offset:
            break
        cells += amount
        index += 1
    return value[index:]


def _column_for_cells(text: str, cells_target: int) -> int:
    cells = 0
    for index, char in enumerate(text):
        amount = 8 - cells % 8 if char == "\t" else cell_width(char)
        if cells + amount > cells_target:
            return index
        cells += amount
    return len(text)


def _column_for_wrapped_row(text: str, row: int, width: int) -> int:
    cells = 0
    current_row = 0
    for index, char in enumerate(text):
        if current_row >= row:
            return index
        amount = 8 - cells % 8 if char == "\t" else cell_width(char)
        if cells and cells + amount > width:
            current_row += 1
            cells = 0
            if current_row >= row:
                return index
            if char == "\t":
                amount = 8
        cells += amount
        if cells >= width:
            current_row += 1
            cells = 0
    return len(text)


def _wrap_cells(line: str, width: int) -> list[str]:
    width = max(1, width)
    rows: list[str] = []
    row = ""
    cells = 0
    for char in line:
        if char == "\t":
            amount = 8 - cells % 8
            chars = " " * amount
        else:
            amount = cell_width(char)
            chars = char
        if cells and cells + amount > width:
            rows.append(row)
            row, cells = "", 0
            if char == "\t":
                amount = 8
        row += chars
        cells += amount
        if cells >= width:
            rows.append(row)
            row, cells = "", 0
    if row or not rows:
        rows.append(row)
    return rows


def _find_casefold(source: str, query: str, last: int, reverse: bool) -> int:
    """Casefold search while keeping offsets in the original source string."""
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


def render_markdown(source: str) -> str:
    """Render Markdown with Rich when installed; degrade to readable source."""
    try:
        return "\n".join("".join(text for text, _role in line) for line in render_markdown_segments(source, 120)).rstrip("\n")
    except Exception:
        return "[plain source · Markdown renderer unavailable]\n" + source


def render_markdown_segments(source: str, width: int, wrap: bool = True) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Return Rich's public structured lines as Bridge semantic text spans."""
    try:
        from rich.console import Console
        from rich.markdown import Markdown

        console = Console(width=max(20, width), force_terminal=False, color_system=None, markup=False, highlight=False)
        options = console.options.update(width=max(20, width), no_wrap=not wrap, overflow="fold" if wrap else "ignore")
        lines = console.render_lines(Markdown(source), options, pad=False)
        rendered = []
        for line in lines:
            spans = []
            for segment in line:
                if not segment.text:
                    continue
                style = segment.style
                role = "brand" if style is not None and getattr(style, "bold", False) else "info"
                if style is not None and getattr(style, "bgcolor", None) is not None:
                    role = "secondary"
                elif style is not None and getattr(style, "italic", False):
                    role = "secondary"
                spans.append((segment.text, role))
                link = getattr(style, "link", None) if style is not None else None
                if link:
                    spans.append((f" ({link})", "secondary"))
            rendered.append(tuple(spans or [("", "info")]))
        return tuple(rendered or ((("", "info"),),))
    except Exception:
        return tuple(tuple((part, "info") for part in row) for row in ("[plain source · Markdown renderer unavailable]", *source.splitlines()))
