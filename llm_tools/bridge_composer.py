"""Full-screen UTF-8 composer for Bridge text entry."""

from __future__ import annotations

import collections
import curses
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from .bridge_input import BracketedPaste, TextBuffer, cell_width, offset_for_cells
from .bridge_documents import wrap_plain_lines
from . import bridge_hangup, bridge_mouse
from .bridge_glyphs import localize
from .bridge_event_text import _SEP, _text
from .bridge_draw import _put, sync_window_size


LAST_RECOVERY_DRAFT: Path | None = None


_ESCDELAY_MS = 50


_WHEEL_ROWS = 3


def _set_escape_delay() -> None:
    """Make a lone Esc register in 50 ms instead of ncurses' default one second."""
    try:
        curses.set_escdelay(_ESCDELAY_MS)
    except (AttributeError, curses.error):
        pass


def compose_document(
    initial: str = "",
    *,
    title: str = "Bridge document",
    window: curses.window | None = None,
    placeholder: str = "",
    restored_from: Path | None = None,
    draft_prefix: str = "composer-recovery-",
) -> str | None:
    """Full-screen UTF-8 composer with a private crash checkpoint and recovery action.

    ``placeholder`` is dim guidance shown while the buffer is empty. ``restored_from``
    names a retained draft whose text is ``initial``: it is deleted once this
    composer has its own checkpoint, and the notice line says when it was written.
    ``draft_prefix`` names the kept draft's file, so a kind of draft can be found again.
    """
    curses.use_env(False)
    global LAST_RECOVERY_DRAFT
    import termios

    from .bridge_store import default_state_dir

    buffer = TextBuffer(initial, len(initial))
    paste_decoder = BracketedPaste(buffer.max_bytes)
    escape_pending = False
    escape_at = 0.0
    paste_activity = 0.0
    preferred_column: int | None = None
    notice = ""  # transient warnings only; the status line below carries the keys
    if restored_from is not None:
        try:
            notice = f"Restored draft from {time.strftime('%H:%M', time.localtime(restored_from.stat().st_mtime))} · Ctrl-X discard"
        except OSError:
            notice = "Restored draft · Ctrl-X discard"
    draft_dir = default_state_dir() / "drafts"
    draft_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(draft_dir, 0o700)
    checkpoint_fd, checkpoint_name = tempfile.mkstemp(prefix=draft_prefix, suffix=".md", dir=draft_dir)
    os.close(checkpoint_fd)
    checkpoint_path = Path(checkpoint_name)
    os.chmod(checkpoint_path, 0o600)
    LAST_RECOVERY_DRAFT = checkpoint_path
    last_checkpoint = 0.0
    checkpointed: str | None = None  # text last written to the recovery file
    write_lock = threading.Lock()  # one recovery write at a time, newest snapshot wins
    snapshot_seq = 0  # numbers the snapshots handed to a write
    written_seq = 0  # the newest snapshot already on disk
    discarded = False
    background: list[threading.Thread] = []  # at most one periodic write in flight
    background_errors: list[OSError] = []

    def write_recovery(text: str, seq: int) -> None:
        """Atomically replace the recovery file with `text` (temp file, fsync, rename)."""
        nonlocal checkpointed, written_seq
        with write_lock:
            if discarded or seq < written_seq:
                return  # the draft was discarded, or a newer snapshot is already on disk
            fd, name = tempfile.mkstemp(prefix=".composer-", suffix=".tmp", dir=draft_dir)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(text.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(name, 0o600)
                os.replace(name, checkpoint_path)
                os.chmod(checkpoint_path, 0o600)
                checkpointed = text
                written_seq = seq
            except BaseException:
                try:
                    os.unlink(name)
                except OSError:
                    pass
                raise

    def checkpoint() -> None:
        """Atomically refresh the bounded private crash-recovery source, before returning."""
        nonlocal snapshot_seq
        snapshot_seq += 1
        write_recovery(buffer.text, snapshot_seq)

    def checkpoint_in_background() -> bool:
        """Start the same write on a worker thread unless one is still running."""
        nonlocal snapshot_seq
        if background and background[0].is_alive():
            return False
        snapshot_seq += 1
        text, seq = buffer.text, snapshot_seq

        def run() -> None:
            try:
                write_recovery(text, seq)
            except OSError as exc:
                background_errors.append(exc)

        background[:] = [threading.Thread(target=run, name="llm-bridge-composer-checkpoint", daemon=True)]
        background[0].start()
        return True

    def retain_draft() -> Path:
        checkpoint()
        return checkpoint_path

    checkpoint()
    if restored_from is not None and restored_from != checkpoint_path:
        restored_from.unlink(missing_ok=True)  # the new checkpoint holds the same text

    def loop(window: curses.window) -> str | None:
        nonlocal preferred_column, notice, escape_pending, escape_at, paste_activity, buffer, last_checkpoint, discarded
        global LAST_RECOVERY_DRAFT
        window.keypad(True)
        paste_keypad = True
        _set_escape_delay()
        bridge_mouse.enable_mouse()
        replay: deque[object] = deque()
        try:
            curses.nonl()
        except curses.error:
            pass
        bridge_hangup.set_timeout(window, 30)
        redraw = True
        drawn_size: tuple[int, int] | None = None
        try:
            fd = sys.stdin.fileno()
            original = termios.tcgetattr(fd)
            changed = original.copy()
            changed[0] &= ~(
                getattr(termios, "IXON", 0)
                | getattr(termios, "ICRNL", 0)
                | getattr(termios, "INLCR", 0)
                | getattr(termios, "IGNCR", 0)
            )
            termios.tcsetattr(fd, termios.TCSANOW, changed)
        except (OSError, termios.error, AttributeError):
            original = None
        try:
            while True:
                wanted_keypad = not paste_decoder.active
                if wanted_keypad != paste_keypad:
                    window.keypad(wanted_keypad)
                    paste_keypad = wanted_keypad
                now = time.monotonic()
                if background_errors:
                    notice = f"Recovery checkpoint failed: {_text(background_errors.pop(), 100)}"
                    background_errors.clear()
                    redraw = True
                if now - last_checkpoint >= 0.8 and buffer.text != checkpointed and checkpoint_in_background():
                    last_checkpoint = now
                sync_window_size(window)
                height, width = window.getmaxyx()
                if (height, width) != drawn_size:
                    redraw = True
                if redraw and (not paste_decoder.active or paste_decoder.interrupted):
                    lines = buffer.text.split("\n")
                    before = buffer.text[:buffer.cursor]
                    line_no = before.count("\n")
                    column = len(before.rsplit("\n", 1)[-1])
                    top = max(0, min(line_no, max(0, len(lines) - max(1, height - 3))))
                    window.erase()
                    _put(window, 0, 0, f"{title} · full source", width, role="heading")
                    caret_x = 0
                    for row, value in enumerate(lines[top:top + max(1, height - 3)], 1):
                        if row - 1 + top == line_no:
                            value, caret_x = _caret_view(value, column, width)
                        _put(window, row, 0, value, width, preserve_spacing=True)
                    _put(window, height - 2, 0, notice, width,
                         role="warn" if notice.startswith(("Paste rejected", "Input rejected", "Incomplete", "Recovery")) else "dim")
                    if not buffer.text and placeholder:
                        # A no-break space keeps a quoted example such as "- criterion" on one row.
                        hint_rows = wrap_plain_lines((localize(placeholder).replace('"- ', '"-\xa0'),), max(1, width))
                        for offset, hint_row in enumerate(hint_rows[:max(1, height - 3)]):
                            _put(window, 1 + offset, 0, hint_row.replace("\xa0", " "), width, role="dim", attr=curses.A_DIM)
                    _put(window, height - 1, 0, _composer_status(buffer.size_bytes, buffer.max_bytes, len(lines)), width, role="dim")
                    visible_row = line_no - top + 1
                    try:
                        curses.curs_set(1)
                        window.move(min(height - 2, visible_row), min(max(0, width - 1), caret_x))
                    except curses.error:
                        pass
                    window.refresh()
                    redraw = False
                    drawn_size = (height, width)
                if not replay:
                    # Wake for input, the escape/paste timers, or the next recovery checkpoint; nothing else changes.
                    busy = escape_pending or paste_decoder.active or paste_decoder.waiting
                    bridge_hangup.set_timeout(window, 30 if busy else 250)
                try:
                    key = replay.popleft() if replay else bridge_hangup.read_wch(window)
                except curses.error:
                    if paste_decoder.active and time.monotonic() - paste_activity >= 2.0:
                        paste_decoder.interrupt()
                        notice = "Incomplete paste quarantined; source unchanged. Ctrl-D keeps recovery and closes."
                        redraw = True
                    if paste_decoder.waiting and not paste_decoder.active and not escape_pending:
                        paste_decoder.reset()
                        notice = "Unknown or incomplete key sequence ignored."
                        redraw = True
                    if escape_pending and time.monotonic() - escape_at >= 0.06:
                        escape_pending = False
                        if paste_decoder.active:
                            paste_decoder.reset()
                            notice = "Incomplete paste discarded; source buffer unchanged."
                            redraw = True
                        else:
                            retain_draft()
                            return None
                    continue
                redraw = True  # any key may change the buffer, the caret or a notice
                if paste_decoder.interrupted and key == "\x04":
                    checkpoint()
                    return None
                if key == curses.KEY_MOUSE:
                    mouse = bridge_mouse.read_mouse()
                    delta = bridge_mouse.wheel_delta(mouse[2]) if mouse else None
                    if delta and delta[0] and not paste_decoder.active:
                        # The wheel moves the caret, which scrolls the view: three lines per notch.
                        replay.extend([curses.KEY_UP if delta[0] < 0 else curses.KEY_DOWN] * _WHEEL_ROWS)
                    continue
                if key == "\x1b" and not paste_decoder.active:
                    escape_pending = True
                    escape_at = time.monotonic()
                    continue
                if escape_pending:
                    paste_decoder.feed(b"\x1b")
                    escape_pending = False
                if paste_decoder.active or paste_decoder.waiting or (isinstance(key, str) and key.startswith(("[200~", "\x1b[200~"))):
                    if paste_decoder.active or (isinstance(key, str) and key.startswith(("[200~", "\x1b[200~"))):
                        paste_activity = time.monotonic()
                    chunk = key.encode("utf-8") if isinstance(key, str) else b""
                    pasted = paste_decoder.feed(chunk)
                    if pasted is not None:
                        accepted, received = buffer.paste(pasted)
                        notice = "" if accepted else f"Paste rejected: received {received} bytes; limit is 1 MiB. Previous source is unchanged. Use an editor or import a file."
                        if accepted:
                            try:
                                checkpoint()
                                last_checkpoint = time.monotonic()
                            except OSError as exc:
                                notice = f"Paste loaded, but recovery checkpoint failed: {_text(exc, 100)}"
                    elif paste_decoder.done and paste_decoder.error:
                        notice = paste_decoder.error + "; previous source is unchanged"
                        paste_decoder.reset()
                    preferred_column = None
                    continue
                if key == "\x13":  # F-keys only switch views, so F2 does not save here
                    checkpoint()
                    return buffer.text
                if key == "\x0f":
                    editor_setting = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
                    editor = shlex.split(editor_setting)
                    if not editor:
                        notice = "VISUAL/EDITOR must name an executable."
                        continue
                    draft_dir = default_state_dir() / "drafts"
                    draft_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.chmod(draft_dir, 0o700)
                    fd, editor_path = tempfile.mkstemp(prefix="composer-", suffix=".md", dir=draft_dir)
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                        stream.write(buffer.text)
                    os.chmod(editor_path, 0o600)
                    curses.def_prog_mode()
                    curses.endwin()
                    try:
                        edited = subprocess.run([*editor, editor_path], check=False, timeout=300, cwd=draft_dir)
                    except (OSError, subprocess.SubprocessError) as exc:
                        notice = f"Editor failed; source retained at {editor_path}: {_text(exc, 100)}"
                        continue
                    finally:
                        curses.reset_prog_mode()
                        window.clear()
                        window.refresh()
                    loaded = False
                    if edited.returncode == 0:
                        with Path(editor_path).open("r", encoding="utf-8", newline="") as editor_file:
                            candidate = editor_file.read()
                        if len(candidate.encode("utf-8")) <= buffer.max_bytes:
                            buffer = TextBuffer(candidate, len(candidate))
                            loaded = True
                            notice = "Editor changes loaded; Ctrl-S saves to the caller."
                        else:
                            notice = f"Editor result exceeds 1 MiB; original source retained at {editor_path}."
                    else:
                        notice = f"Editor exited {edited.returncode}; source retained at {editor_path}."
                    if loaded:
                        Path(editor_path).unlink(missing_ok=True)
                    continue
                if key == "\x1b":
                    name = retain_draft()
                    notice = f"Draft retained at {name}"
                    return None
                if key == "\x04":  # Ctrl-D closes like Esc and never discards; Ctrl-X discards
                    name = retain_draft()
                    notice = f"Draft retained at {name}"
                    return None
                if key == "\x18":
                    with write_lock:  # an in-flight periodic write must not recreate the discarded draft
                        discarded = True
                        checkpoint_path.unlink(missing_ok=True)
                    if LAST_RECOVERY_DRAFT == checkpoint_path:
                        LAST_RECOVERY_DRAFT = None
                    return None
                if key in {curses.KEY_LEFT, "KEY_LEFT"}:
                    buffer.move(buffer.cursor - 1)
                elif key in {curses.KEY_RIGHT, "KEY_RIGHT"}:
                    buffer.move(buffer.cursor + 1)
                elif key in {curses.KEY_HOME, "\x01"}:
                    buffer.move(buffer.text.rfind("\n", 0, buffer.cursor) + 1)
                elif key in {curses.KEY_END, "\x05"}:
                    end = buffer.text.find("\n", buffer.cursor)
                    buffer.move(len(buffer.text) if end < 0 else end)
                elif key in {curses.KEY_UP, "KEY_UP", curses.KEY_DOWN, "KEY_DOWN", curses.KEY_PPAGE, curses.KEY_NPAGE}:
                    preferred_column = _advance_cells(0, lines[line_no][:column]) if preferred_column is None else preferred_column
                    direction = -1 if key in {curses.KEY_UP, "KEY_UP", curses.KEY_PPAGE} else 1
                    step = max(1, height-3) if key in {curses.KEY_PPAGE, curses.KEY_NPAGE} else 1
                    target_line = min(len(lines)-1, max(0, line_no+direction*step))
                    start = sum(len(item)+1 for item in lines[:target_line])
                    buffer.move(start+offset_for_cells(lines[target_line], preferred_column))
                elif key in {curses.KEY_BACKSPACE, "KEY_BACKSPACE", "\x7f", "\b"}:
                    buffer.backspace()
                elif key in {curses.KEY_DC, "KEY_DC"}:
                    buffer.delete()
                elif key == "\x15":
                    buffer.clear()
                elif key == "\x0b":
                    end = buffer.text.find("\n", buffer.cursor)
                    if end < 0:
                        buffer.text = buffer.text[:buffer.cursor]
                    else:
                        buffer.text = buffer.text[:buffer.cursor] + buffer.text[end:]
                elif key in {"\n", "\r", curses.KEY_ENTER}:
                    notice = ""
                    if not buffer.insert("\n"):
                        notice = "Paste rejected: document exceeds the 1 MiB interactive limit"
                elif key == "\t":
                    if not buffer.insert("\t"):
                        notice = "Input rejected: document exceeds the 1 MiB interactive limit"
                elif isinstance(key, str) and key and key.isprintable():
                    if len(key) > 1:
                        accepted, received = buffer.paste(key)
                        notice = "" if accepted else f"Paste rejected: received {received} bytes; limit is 1 MiB. Use an editor or import a file."
                    elif not buffer.insert(key):
                        notice = "Input rejected: document exceeds the 1 MiB interactive limit"
                if key not in {curses.KEY_UP, "KEY_UP", curses.KEY_DOWN, "KEY_DOWN", curses.KEY_PPAGE, curses.KEY_NPAGE}:
                    preferred_column = None
        finally:
            if original is not None:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, original)
                except (OSError, termios.error):
                    pass

    try:
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()
        return bridge_hangup.run_curses(loop) if window is None else loop(window)
    except (KeyboardInterrupt, EOFError, curses.error, OSError):
        retain_draft()
        return None
    finally:
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()


def _composer_status(size_bytes: int, max_bytes: int, line_count: int) -> str:
    """Composer footer: the keys, the line count, and the byte count only when the limit is near."""
    parts = ["Ctrl-S Save", "Esc close (draft kept)", "Ctrl-X Discard", f"{line_count} {'line' if line_count == 1 else 'lines'}"]
    if size_bytes * 5 > max_bytes * 4:
        parts.append(f"{size_bytes}/{max_bytes} bytes")
    return _SEP.join(parts)


_NOT_PRINTABLE_ASCII = re.compile(r"[^ -~]")


def _plain_cells(text: str) -> int:
    """Cell width of `text` that has no tab; the sum of its characters' widths."""
    if text.isascii() and text.isprintable():
        return len(text)
    odd = _NOT_PRINTABLE_ASCII.findall(text)
    return len(text) - len(odd) + sum(cell_width(char) * count for char, count in collections.Counter(odd).items())


def _advance_cells(cells: int, text: str) -> int:
    """The cell column after drawing `text` from column `cells` (tab stops every 8 cells)."""
    if text.isascii() and text.isprintable():
        return cells + len(text)
    for index, part in enumerate(text.split("\t")):
        if index:
            cells += 8 - cells % 8
        cells += _plain_cells(part)
    return cells


def _caret_view(line: str, column: int, width: int) -> tuple[str, int]:
    """The part of a composer line that is drawn, and the caret's column in it.

    The view ends at the caret when the line is wider than the window, as before,
    but only the text near the caret is measured: a 1 MiB single-line paste costs
    the same as a short line. Tab stops repeat every 8 cells, so the column of
    the first measured character only matters modulo 8.
    """
    room = max(0, width - 2)
    back = width * 4 + 256
    while True:
        first = max(0, column - back)
        offset = 0
        if first and "\t" in line:
            tab = line.rfind("\t", 0, first)
            offset = _advance_cells(0, line[tab + 1:first] if tab >= 0 else line[:first]) % 8
        caret = _advance_cells(offset, line[first:column])
        if first == 0 or caret - offset >= room:
            break  # the characters before `first` all end before the visible start
        back *= 4  # zero-width characters left the measured text too narrow
    start = max(offset, caret - room)
    return _slice_display_cells(line[first:], start, max(1, width - 1), offset), caret - start


def _slice_display_cells(value: str, start: int, width: int, cells: int = 0) -> str:
    """Display a cell-width slice without splitting a wide UTF-8 character.

    `cells` is the display column of the first character of `value`.
    """
    output: list[str] = []
    visible = 0
    for char in value:
        if char == "\t":
            amount = 8 - cells % 8
            rendered = " " * amount
        else:
            amount = cell_width(char)
            rendered = char
        if cells + amount <= start:
            cells += amount
            continue
        if cells < start:
            cells += amount
            continue
        if visible + amount > width:
            break
        output.append(rendered)
        visible += amount
        cells += amount
    return "".join(output)


def _clipboard_argv() -> list[str] | None:
    """Return a supported local clipboard command; never use OSC 52."""
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    if shutil.which("pbcopy"):
        return ["pbcopy"]
    return None
