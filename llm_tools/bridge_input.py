"""Small lossless input primitives shared by Bridge forms and documents."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata


def cell_width(text: str) -> int:
    """Return terminal cell width for common Unicode text (not grapheme shaping)."""
    width = 0
    for char in text:
        if char == "\t":
            width += 8 - width % 8
            continue
        if unicodedata.combining(char) or unicodedata.category(char) in {"Cf", "Mn", "Me"}:
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} or ord(char) >= 0x1F300 else 1
    return width


def cell_slice(text: str, start: int, width: int) -> str:
    """Clip display cells without splitting a wide character or combining mark."""
    result: list[str] = []
    position = 0
    included = False
    for char in text.expandtabs(8):
        amount = cell_width(char)
        if amount == 0:
            if included:
                result.append(char)
            continue
        if position >= start + width:
            break
        included = position >= start and position + amount <= start + width
        if included:
            result.append(char)
        elif position < start + width and position + amount > start:
            result.append(" " * (min(position + amount, start + width) - max(position, start)))
        position += amount
    return "".join(result)


def input_view(text: str, cursor: int, width: int) -> tuple[str, int]:
    """Keep the logical caret visible with one blank insertion cell reserved."""
    width = max(1, width)
    caret = cell_width(text[:max(0, cursor)])
    start = max(0, caret - width + 1)
    return cell_slice(text, start, width - 1), min(width - 1, caret - start)


def offset_for_cells(text: str, target: int) -> int:
    """Find an insertion offset at a preferred terminal column."""
    cells = 0
    for index, char in enumerate(text):
        amount = 8-cells % 8 if char == "\t" else cell_width(char)
        if cells+amount > target:
            return index
        cells += amount
    return len(text)


def wrapped_row_for_column(text: str, column: int, width: int) -> tuple[int, int]:
    """Map a source codepoint offset to (visual row, cell column)."""
    width = max(1, width)
    row = cells = 0
    for char in text[:max(0, column)]:
        if char == "\t":
            amount = 8 - cells % 8
        elif unicodedata.combining(char) or unicodedata.category(char) in {"Cf", "Mn", "Me"}:
            amount = 0
        else:
            amount = 2 if unicodedata.east_asian_width(char) in {"W", "F"} or ord(char) >= 0x1F300 else 1
        if cells + amount > width:
            row += 1
            cells = 0
        cells += amount
        if cells >= width:
            row += 1
            cells = 0
    return row, cells


class BracketedPaste:
    """Boundary-safe UTF-8 bracketed-paste transaction decoder."""

    START = b"\x1b[200~"
    END = b"\x1b[201~"

    def __init__(self, max_bytes: int = 1_048_576) -> None:
        self.max_bytes = max_bytes
        self.pending = bytearray()
        self.active = False
        self.discarding = False
        self.interrupted = False
        self.done = False
        self.scan_from = 0
        self.received_bytes = 0
        self.error = ""

    @property
    def waiting(self) -> bool:
        return bool(self.pending) or self.active

    def reset(self) -> None:
        self.pending.clear()
        self.active = False
        self.discarding = False
        self.interrupted = False
        self.done = False
        self.scan_from = 0
        self.received_bytes = 0
        self.error = ""

    def interrupt(self) -> None:
        """Quarantine unfinished payload until END; never reinterpret its tail."""
        self.discarding = True
        self.interrupted = True
        self.error = "Incomplete paste discarded"

    def feed(self, chunk: bytes) -> str | None:
        """Return source only after a complete valid transaction; otherwise None."""
        self.pending.extend(chunk)
        if not self.active:
            start = self.pending.find(self.START)
            if start < 0:
                keep = max(0, len(self.START) - 1)
                if len(self.pending) > keep:
                    del self.pending[:-keep]
                return None
            del self.pending[:start + len(self.START)]
            self.active = True
        search_from = max(0, self.scan_from - len(self.END) + 1)
        end = self.pending.find(self.END, search_from)
        if self.discarding:
            if end >= 0:
                self.received_bytes += end
                message = "Incomplete paste discarded" if self.interrupted else f"paste received {self.received_bytes} bytes; limit is {self.max_bytes}"
                self.reset()
                self.error = message
                self.done = True
            else:
                keep = max(0, len(self.END) - 1)
                if len(self.pending) > keep:
                    self.received_bytes += len(self.pending) - keep
                    del self.pending[:-keep]
                self.scan_from = len(self.pending)
            return None
        if end < 0:
            overlap = 0
            for count in range(1, min(len(self.END) - 1, len(self.pending)) + 1):
                if self.pending[-count:] == self.END[:count]:
                    overlap = count
            if len(self.pending) - overlap > self.max_bytes:
                keep = max(0, len(self.END) - 1)
                self.received_bytes = max(0, len(self.pending) - keep)
                self.error = ""
                self.discarding = True
                if len(self.pending) > keep:
                    del self.pending[:-keep]
                self.scan_from = len(self.pending)
                return None
            self.scan_from = len(self.pending)
            return None
        payload = bytes(self.pending[:end])
        tail = bytes(self.pending[end + len(self.END):])
        received = len(payload)
        self.reset()
        if received > self.max_bytes:
            self.error = f"paste received {received} bytes; limit is {self.max_bytes}"
            self.done = True
            return None
        try:
            result = payload.decode("utf-8", errors="strict")
            self.done = True
            return result
        except UnicodeDecodeError:
            self.error = "paste ended with incomplete or invalid UTF-8"
            self.done = True
            return None


@dataclass
class TextBuffer:
    """Editable Unicode source with a codepoint caret and atomic byte-limited paste."""

    text: str = ""
    cursor: int = 0
    max_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        self.cursor = min(max(0, self.cursor), len(self.text))

    @property
    def size_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def lines(self) -> int:
        return self.text.count("\n") + 1

    def insert(self, value: str) -> bool:
        candidate = self.text[:self.cursor] + value + self.text[self.cursor:]
        if len(candidate.encode("utf-8")) > self.max_bytes:
            return False
        self.text = candidate
        self.cursor += len(value)
        return True

    def paste(self, value: str) -> tuple[bool, int]:
        """Commit complete paste or retain the old source; return (accepted, received bytes)."""
        received = len(value.encode("utf-8"))
        candidate = self.text[:self.cursor] + value + self.text[self.cursor:]
        if len(candidate.encode("utf-8")) > self.max_bytes:
            return False, received
        self.text = candidate
        self.cursor += len(value)
        return True, received

    def move(self, offset: int) -> None:
        self.cursor = min(len(self.text), max(0, offset))

    def backspace(self) -> None:
        if self.cursor:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1

    def delete(self) -> None:
        if self.cursor < len(self.text):
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]

    def clear(self) -> None:
        self.text = ""
        self.cursor = 0
