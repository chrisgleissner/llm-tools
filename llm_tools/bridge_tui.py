"""Small curses console for LLM Bridge's tmux command pane and control view.

The UI knows no persistence or provider APIs. It asks the injected snapshot
callback for a bounded projection and sends operator text to the same command
dispatcher used by the CLI.
"""

from __future__ import annotations

import ast
import collections
import curses
import functools
import inspect
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import textwrap
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .bridge_input import BracketedPaste, TextBuffer, cell_width, cell_slice, input_view, offset_for_cells
from .bridge_documents import DocumentViewer, heading_label
from .bridge_states import ACTIVE_OPERATION_STATUSES, ENDED_MISSION_STATUSES, FINISHED_MISSION_STATUSES, STARTED_MISSION_STATUSES
from . import bridge_events, bridge_hangup, bridge_mouse, bridge_redraw, bridge_theme
from .bridge_projection import ProjectionParams
from .bridge_vocab import EventKind
from .bridge_format import plain_reason
from .bridge_glyphs import FINISHED_STATES, glyph, localize, localize_cells, provider_role, spinner_frames, state_look, state_text
from .common import fmt_duration
from . import terminal
from .terminal import TerminalAction
from .bridge_actions import ACTIONS, Action, actions_for
from . import bridge_draw
from .bridge_draw import (  # noqa: F401  (re-exported: other modules and tests reach these through bridge_tui)
    QUIET,
    VIEWS,
    ControlViewAction,
    Segment,
    _clip_segments,
    _merge_segments,
    _put,
    _put_segments,
    _scrub,
    _segments_text,
    _segments_width,
    _split_hint,
    _VIEW_ALIASES,
    initialize_colors,
    normalize_view,
    role_attr,
    sync_window_size,
)


def __getattr__(name: str) -> object:
    """Answer reads of the colour state, which lives in ``bridge_draw``, so ``bridge_tui._CURRENT_THEME`` still resolves."""
    if name in {"_ROLE_ATTRS", "_ROLE_PAIRS", "_CURRENT_THEME", "_COLOR_ROLES"}:
        return getattr(bridge_draw, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


Snapshot = Callable[..., dict[str, object]]
Dispatch = Callable[[str, str], object]
LAST_RECOVERY_DRAFT: Path | None = None
# Set by the home screen when it opens the controls of a mission it just saved,
# so the console can say once which id to type on the command line.
JUST_SAVED_MISSION: str | None = None


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
) -> str | None:
    """Full-screen UTF-8 composer with a private crash checkpoint and recovery action."""
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
    draft_dir = default_state_dir() / "drafts"
    draft_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(draft_dir, 0o700)
    checkpoint_fd, checkpoint_name = tempfile.mkstemp(prefix="composer-recovery-", suffix=".md", dir=draft_dir)
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
                    status = f"F2/Ctrl-S Save · Esc Recover · Ctrl-D Discard · {buffer.size_bytes}/{buffer.max_bytes} bytes · {len(lines)} lines"
                    _put(window, height - 1, 0, status, width, role="dim")
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
                if key == "\x13" or key == curses.KEY_F2:
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
                if key == "\x04":
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


PALETTE_COMMANDS = tuple(action.command for action in ACTIONS)
# F1-F8 select the views in this order. Activity and Sessions (history) sit
# together after the working views; Health (diagnostics) is next to Actions.
# Names used before the 1.3 rename stay accepted everywhere a view is named.
# Words typed in the command line that switch view (the rest are CLI commands).
_VIEW_COMMANDS = {
    "overview": "Overview", "control": "Overview", "providers": "Providers", "activity": "Activity",
    "sessions": "Sessions", "health": "Health", "actions": "Actions", "usage": "Usage",
    "palette": "Actions", "log": "Activity", "systems": "Health", "stations": "Providers",
}


_REFRESH_SECONDS = 0.25
_ACTIVITY_POLL_SECONDS = 2.0
_FULL_REFRESH_SECONDS = 5.0  # a full snapshot at least this often: lock files, usage and clocks change without a database commit
_IDLE_BACKOFF_SECONDS = 10.0  # without input for this long the change probe slows to once a second
_IDLE_REFRESH_SECONDS = 1.0
_STATUS_SECONDS = 8.0  # how long a command result stays in the bottom row
_OUTPUT_IDLE_THRESHOLD_SECONDS = 30
_MAX_COMMAND_CHARS = 4096
_MAX_OUTPUT_LINES = 80
_MAX_OUTPUT_LINE_CHARS = 2000
_MAX_RESPONSE_CHARS = 24000
_MAX_RESPONSE_LINES = _MAX_OUTPUT_LINES
_MAX_TASK_ROWS = 200
_TASK_PAGE_SIZE = _MAX_TASK_ROWS
_MAX_STATION_ROWS = 32
_MAX_EVENT_ROWS = 80
_MAX_DETAIL_TEXT = 16000
_MAX_DETAIL_ITEMS = 12
_SELECTABLE_VIEWS = frozenset({"Tasks", "Providers", "Actions"})
_BRIDGE_LABEL = 12
_SYSTEM_LABEL = 22
_SESSION_COLUMNS = ("op", "task", "provider", "model", "status", "native session", "restoration")


def _text(value: object, limit: int = 1000) -> str:
    """Make untrusted snapshot/result text safe for direct curses rendering."""
    rendered = value if isinstance(value, str) else str(value)
    # Sanitising and whitespace folding act on single characters, so the cleaned
    # prefix of the text is a prefix of the cleaned whole. Clean a bounded prefix
    # and only grow it when whitespace or removed characters left too little.
    size = max(0, limit) * 4 + 256
    while True:
        clean = " ".join(_scrub(rendered[:size]).split())
        if len(clean) >= limit or size >= len(rendered):
            return clean[:limit]
        size *= 4


_TASK_VERB_PHRASES = {
    "add": "added", "amend": "amended", "priority": "priority changed", "reorder": "reordered",
    "dependencies": "dependencies updated", "pause": "paused", "resume": "resumed", "cancel": "cancelled",
    "retry": "queued for another attempt", "verify": "verified", "reject": "sent back",
}
_TASK_ACK = re.compile(r"^task(?: \S+)? (?:" + "|".join(re.escape(item) for item in _TASK_VERB_PHRASES.values()) + r")")
# Options whose value is a private path or an id the operator never typed.
_PRIVATE_OPTIONS = frozenset({"--file", "--prompt-file", "--mission"})


def task_number(task: dict[str, object]) -> object:
    """The number the operator knows a task by: the per-mission ordinal when present, else its id."""
    return task.get("ordinal", task.get("id"))


def _task_ref(snapshot: dict[str, object], number: object) -> list[str]:
    """CLI arguments naming the task shown as `number`.

    `--task N` is the mission's own task number (the snapshot's `ordinal`). A
    snapshot without ordinals can only name a task by its global id, so those
    tasks are addressed with `--task-id`.
    """
    for task in _items(snapshot.get("tasks")):
        if str(task_number(task)) == str(number):
            if "ordinal" not in task and task.get("id") is not None:
                return ["--task-id", str(task["id"])]
            break
    return ["--task", str(number)]


def _applied_label(command: str, result: dict[str, object]) -> str:
    """What was applied, in operator words; never a draft path or the mission id.

    `task add --file /…/drafts/task-x.md` becomes `Task 4 added`, and an amend
    becomes `Task 2 amended (revision 3)`. Other commands keep their words with
    file and mission options removed.
    """
    try:
        words = shlex.split(_strip_slash(command))
    except ValueError:
        words = _strip_slash(command).split()
    if len(words) >= 2 and words[0] == "task" and words[1] in _TASK_VERB_PHRASES:
        # The command's own `--task N` is the mission's number; a result's `task_id` or `id` is the global id.
        number = words[words.index("--task") + 1] if "--task" in words and words.index("--task") + 1 < len(words) else result.get("ordinal")
        if number is None and "--task-id" in words and words.index("--task-id") + 1 < len(words):
            number = words[words.index("--task-id") + 1]
        if number is None:
            number = result.get("task_id", result.get("id"))
        text = "Task" + (f" {_text(number, 12)}" if number is not None else "") + f" {_TASK_VERB_PHRASES[words[1]]}"
        revision = result.get("revision")
        if words[1] == "amend" and isinstance(revision, int) and not isinstance(revision, bool):
            text += f" (revision {revision})"
        return text
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word in _PRIVATE_OPTIONS:
            skip = True
        elif not word.startswith(tuple(f"{name}=" for name in _PRIVATE_OPTIONS)):
            kept.append(word)
    return " ".join(kept) or _text(command, 120)


def _cut_text(value: object, limit: int) -> str:
    """`_text` that marks a cut with an ellipsis, so clipped detail is never mistaken for the whole."""
    clean = _text(value, limit + 1)
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + glyph("ellipsis")


def _command_feedback(command: str, response: str) -> str:
    """Turn known durable command envelopes into compact human acknowledgments."""
    try:
        payload = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and "result" in payload and payload.get("status") in {"APPLIED", "REJECTED", "FAILED"}:
        status = str(payload["status"])
        result = _dict(payload.get("result"))
        if status == "APPLIED":
            detail = result.get("detail")
            label = _text(_applied_label(command, result), 120)
            if label.startswith("Task ") and not result.get("kind"):
                return _text(f"{label} — {detail}" if detail else label, _MAX_OUTPUT_LINE_CHARS)
            if result.get("kind"):
                label = _text(result.get("kind"), 80) + " steering"
            if detail:
                return _text(f"Applied: {label} — {detail}", _MAX_OUTPUT_LINE_CHARS)
            return _text(f"Applied: {label}", _MAX_OUTPUT_LINE_CHARS)
        reason = result.get("error") or result.get("detail") or status.lower()
        return _text(f"Rejected: {reason}", _MAX_OUTPUT_LINE_CHARS)

    rendered = json.dumps(payload, ensure_ascii=False, indent=2) if payload is not None else response
    lines = rendered.splitlines() or [""]
    bounded: list[str] = []
    char_count = 0
    truncated = False
    for line in lines:
        if len(bounded) >= _MAX_RESPONSE_LINES or char_count >= _MAX_RESPONSE_CHARS:
            truncated = True
            break
        clean = "".join(
            char if unicodedata.category(char) not in {"Cc", "Cs"} or char == "\t" else " "
            for char in line
        ).replace("\t", "    ")
        remaining = _MAX_RESPONSE_CHARS - char_count
        limit = min(_MAX_OUTPUT_LINE_CHARS, remaining)
        if len(clean) > limit:
            clean = clean[: max(0, limit - 1)] + "…"
            truncated = True
        bounded.append(clean)
        char_count += len(clean) + 1
        if truncated:
            break
    if len(bounded) < len(lines):
        truncated = True
    if truncated:
        notice = "[response truncated to fit the Bridge console limit]"
        if len(bounded) >= _MAX_RESPONSE_LINES:
            bounded[-1] = notice
        else:
            bounded.append(notice)
    return "\n".join(bounded)


def console_words(command_line: str) -> list[str]:
    """`bridge.console_words`, imported when first used (bridge imports this module lazily)."""
    from .bridge import console_words as split

    return split(command_line)


def expand_reject_alias(words: list[str], waiting: Callable[[], list[int]]) -> tuple[list[str] | None, str | None]:
    from .bridge import expand_reject_alias as expand

    return expand(words, waiting)


def _strip_slash(command: str) -> str:
    """Drop the Codex/Claude-style `/` from a leading slash command."""
    text = command.strip()
    if len(text) > 1 and text[0] == "/" and (text[1].isalpha() or text[1] == "?"):
        return text[1:]
    return text


def _confirmation_reason(command: str) -> str | None:
    """Return the consequence that needs a deliberate second keystroke."""
    try:
        words = console_words(_strip_slash(command))
    except ValueError:
        return None
    if words and words[0] == "cancel":
        return "Cancel this mission and its unfinished tasks?"
    if words and words[0] == "stop" and "--force" in words:
        return "Force stop this mission's owned active work?"
    if words and words[0] == "finish":
        return "Ask the provider to exit now? Unsaved provider state is lost; the task moves to verification."
    if words[:2] == ["task", "cancel"]:
        return "Cancel this task and discard its remaining work?"
    if words[:2] == ["task", "reject"] and "--task" in words:
        reason = next((word[len("--reason="):] for word in words if word.startswith("--reason=")), "")
        return f"Send task {words[words.index('--task')+1]} back for another attempt? Reason: {reason[:120]}"
    if words[:2] == ["task", "verify"] and "--approve" in words:
        kind, value = _approval_target(words)
        label = f"task {value}" if kind == "task" else f"task with id {value}" if kind == "id" else "(select a task)"
        return f"Approve verification and mark {label} complete?"
    return None


def _approval_target(words: list[str]) -> tuple[str, str]:
    """(`task`, number), (`id`, global id) or (``, ``) for the task an approval command names."""
    for option, kind in (("--task", "task"), ("--task-id", "id")):
        if option in words and words.index(option) + 1 < len(words):
            return kind, words[words.index(option) + 1]
    return "", ""


def _approval_task(command: str) -> tuple[str, str] | None:
    """(kind, value) of the task an approval command (`task verify … --approve`) names, else None."""
    try:
        words = console_words(_strip_slash(command))
    except ValueError:
        return None
    if words[:2] == ["task", "verify"] and "--approve" in words:
        target = _approval_target(words)
        return target if target[0] else None
    return None


def _approval_summary(snapshot: dict[str, object], target: tuple[str, str], width: int = 100) -> list[str]:
    """What the operator is about to approve: title, latest evidence, the last run's exit status and workspace change.

    Only facts the projection holds are shown. The workspace line comes from the
    repository fingerprints recorded before and after the task's last provider
    run; no list of changed files is recorded, so none is shown.
    """
    kind, value = target
    task = next((item for item in _items(snapshot.get("tasks"))
                 if (str(item.get("id")) if kind == "id" else str(task_number(item))) == value), None)
    if task is None:
        return [f"Task {value} is not in the loaded list; check Tasks before approving."]
    number = str(task_number(task))
    lines = [f"Task {number}: {_cut_text(task.get('title') or task.get('prompt') or 'Untitled task', width)}"]
    raw_evidence = task.get("evidence")
    evidence = [item for item in raw_evidence if _known(item)] if isinstance(raw_evidence, list) else []
    if evidence:
        shown = evidence[-3:]
        lines.append(f"Evidence ({len(evidence)}):" if len(evidence) == len(shown) else f"Evidence (latest {len(shown)} of {len(evidence)}):")
        lines += [f"  {_cut_text(_evidence_text(item), width)}" for item in shown]
    else:
        lines.append("Evidence: none recorded; read the provider output before approving.")
    task_id = str(task.get("id"))
    runs = [op for op in _items(snapshot.get("operations")) if str(op.get("task_id")) == task_id]
    if runs:
        last = max(runs, key=lambda op: str(op.get("created_at") or ""))
        who = _text(last.get("route_id") or last.get("station") or last.get("provider") or "provider", 40)
        code = last.get("exit_code")
        lines.append(f"Last run: {who}" + (f", exit {_text(code, 8)}" if code is not None else "") + (f", {_text(last['status'], 20).lower()}" if _known(last.get("status")) else ""))
    progress = [
        _dict(event.get("data")) for event in _items(snapshot.get("events"))
        if _event_kind(event) == "operation-progress"
        and task_id in {str(event.get("task_id")), str(_dict(event.get("data")).get("task_id"))}
    ]
    known = [data for data in progress if data.get("before") and data.get("after")]
    if known:
        lines.append("Workspace: " + ("changed during the run" if known[-1]["before"] != known[-1]["after"] else "unchanged by the run"))
    return lines


# Console-only shortcuts that expand to public commands.
_CONSOLE_ALIASES = ("approve", "reject", "overview", "providers", "activity", "health", "actions", "home")


def _expand_console_alias(command: str, snapshot: dict[str, object]) -> tuple[str, str | None]:
    """Expand `approve [N]` to `task verify --task N --approve`.

    Returns the command to run and an error message. Without N the single
    task that awaits verification is used.
    """
    try:
        words = console_words(_strip_slash(command))
    except ValueError:
        return command, None
    if words and words[0] == "reject":
        expanded, problem = expand_reject_alias(
            words, lambda: [int(str(task_number(task))) for task in _items(snapshot.get("tasks")) if str(task.get("status", "")).upper() == "VERIFYING"]
        )
        if expanded is None:
            return command, problem
        if "--task" in expanded:  # name the task by id when the snapshot has no task numbers
            position = expanded.index("--task")
            expanded[position:position + 2] = _task_ref(snapshot, expanded[position + 1])
        return shlex.join(expanded), None
    if not words or words[0] != "approve":
        return command, None
    if len(words) > 2 or (len(words) == 2 and not words[1].isdecimal()):
        return command, "error: usage: approve [TASK-NUMBER]"
    if len(words) == 2:
        task_id = words[1]
    else:
        waiting = [str(task_number(task)) for task in _items(snapshot.get("tasks")) if str(task.get("status", "")).upper() == "VERIFYING"]
        if len(waiting) != 1:
            hint = f"tasks awaiting approval: {', '.join(waiting)}" if waiting else "no task awaits approval"
            return command, f"error: approve needs a task number · {hint}"
        task_id = waiting[0]
    return shlex.join(["task", "verify", *_task_ref(snapshot, task_id), "--approve"]), None


def _command_completions(command: str) -> list[str]:
    """Complete public command/option names from the authoritative parser.

    A leading `/` (Codex/Claude style) is accepted and kept: `/pa` completes
    to `/pause`.
    """
    if command.startswith("/"):
        return ["/" + item for item in _command_completions(command[1:])]
    import argparse
    from .bridge import _build_parser
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    trailing = bool(command) and command[-1].isspace()
    prefix = "" if trailing or not words else words[-1]
    complete = words if trailing else words[:-1]
    parser = _build_parser(default_mission="current")
    for word in complete:
        groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        if groups and word in groups[0].choices:
            parser = groups[0].choices[word]
        elif word.startswith("-"):
            break
    groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    candidates = list(groups[0].choices) if groups and not prefix.startswith("-") else list(parser._option_string_actions)
    if not complete and groups and not prefix.startswith("-"):
        candidates.extend(name for name in _CONSOLE_ALIASES if name not in candidates)
    base = command if trailing else command[:-len(prefix)] if prefix else command
    return [base + value for value in candidates if value.startswith(prefix) and value not in {"console", "_controller"}]


@dataclass(frozen=True)
class _CommandResult:
    """Dispatcher feedback plus a positive durable-ack bit for draft cleanup."""

    text: str
    outcome: str = "unknown"


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_dict(item) for item in value if isinstance(item, dict)]


def _station_projection(value: object) -> tuple[list[dict[str, object]], dict[str, object]]:
    if isinstance(value, list):
        return (
            _projected_rows(value, _MAX_STATION_ROWS, ("id", "name", "provider", "model", "observed_model", "status", "restoration_state")),
            {},
        )
    station_data = _dict(value)
    if not station_data:
        return [], {}
    configured = _dict(station_data.get("configured"))
    observed = _dict(station_data.get("observed"))
    providers = configured.get("providers") if isinstance(configured.get("providers"), list) else []
    routes = configured.get("routes") if isinstance(configured.get("routes"), list) else []
    models = _dict(configured.get("models"))
    efforts = _dict(configured.get("efforts"))
    names = list(dict.fromkeys(str(name) for name in [*providers, *routes]))  # type: ignore[misc]  # providers and routes come from a projection typed object
    observed_name = _text(observed.get("station") or "unknown", 80)
    if observed_name not in names and observed_name != "unknown":
        names.append(observed_name)
    rows = [
        {
            "name": name,
            "id": name,
            "model": _text(models.get(name) or "unknown", 80),
            "effort": _text(efforts.get(name) or "unknown", 24),
            "observed_model": _text(observed.get("model") or "unknown", 80) if name == observed_name else "unknown",
            "status": _text(observed.get("status") or "configured", 24) if name == observed_name else "configured",
        }
        for name in names[:_MAX_STATION_ROWS]
    ]
    context = {
        "mode": _text(configured.get("mode") or "unknown", 24),
        "executing_operation": _text(station_data.get("executing_operation") or "none", 80),
        "viewed_station": _text(station_data.get("viewed_station") or "unknown", 80),
        "observed_station": observed_name,
        "last_launched_station": _text(station_data.get("last_launched_station") or "unknown", 80),
        "observed_model": _text(observed.get("model") or "unknown", 80),
        "observed_effort": _text(observed.get("effort") or "unknown", 24),
        "operation_status": _text(observed.get("operation_status") or "unknown", 24),
        "provider_liveness": _text(station_data.get("provider_liveness") or "unknown", 24),
    }
    return rows, context  # type: ignore[return-value]  # dict[str, str] rows are accepted as dict[str, object] (invariant value type only)


def _projected_rows(value: object, limit: int, fields: tuple[str, ...]) -> list[dict[str, object]]:
    rows = _items(value)
    return [{key: _text(row[key], 200) for key in fields if row.get(key) is not None} for row in rows[:limit]]


def _bounded_task_value(value: object, *, limit: int = _MAX_DETAIL_ITEMS, preserve_newlines: bool = False) -> object:
    """Keep useful task detail while preventing arbitrary nested payload growth."""
    if isinstance(value, str):
        if preserve_newlines:
            # Per-character cleaning keeps the length, so only the first limit + 1 characters matter.
            clean = _scrub(value[: _MAX_DETAIL_TEXT + 1], keep_newline=True, drop_format=False)
            return clean if len(value) <= _MAX_DETAIL_TEXT else clean[: _MAX_DETAIL_TEXT - 1] + glyph("ellipsis")
        return _cut_text(value, _MAX_DETAIL_TEXT)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_bounded_task_value(item) for item in value[:limit]]
    if isinstance(value, dict):
        allowed = {"prompt", "title", "priority", "acceptance", "evidence", "revision", "status", "changes", "created_at", "text", "source", "at"}
        return {
            _text(key, 48): _bounded_task_value(item, preserve_newlines=key == "prompt")
            for key, item in list(value.items())[:limit]
            if key in allowed
        }
    return _text(value, 200)


def _project_tasks(value: object, limit: int = _MAX_TASK_ROWS) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for task in _items(value)[:limit]:
        item: dict[str, object] = {}
        for key in ("id", "ordinal", "title", "status", "priority", "ordering", "revision", "updated_at", "origin", "proposal_status", "proposed_by_operation"):
            if key in task:
                item[key] = _text(task[key], 120)
        for key in ("prompt", "acceptance", "evidence", "pending", "depends_on", "amendments"):
            if key in task:
                item[key] = _bounded_task_value(task[key], preserve_newlines=key == "prompt")
        output.append(item)
    return output


def _scalar(value: object, limit: int) -> str:
    """Project a scalar for display; a missing value is empty text, never the string "None"."""
    return "" if value is None else _text(value, limit)


def _project_capacity(value: object) -> dict[str, object]:
    capacity = _dict(value)
    selection = _dict(capacity.get("selection"))
    result: dict[str, object] = {
        _text(key, 48): _scalar(item, 180)
        for key, item in capacity.items()
        if isinstance(item, (str, int, float, bool, type(None)))
    }
    if selection:
        selected: dict[str, object] = {
            _text(key, 48): _scalar(item, 180)
            for key, item in selection.items()
            if key != "decisions" and isinstance(item, (str, int, float, bool, type(None)))
        }
        raw_decision = _dict(selection.get("decision"))
        selected["decision"] = {
            _text(key, 48): _scalar(item, 120)
            for key, item in raw_decision.items()
            if isinstance(item, (str, int, float, bool, type(None)))
        }
        decisions: list[dict[str, object]] = []
        raw_decisions = selection.get("decisions")
        for raw in raw_decisions[:8] if isinstance(raw_decisions, list) else []:
            row = _dict(raw)
            decision: dict[str, object] = {
                _text(key, 48): _scalar(item, 120)
                for key, item in row.items()
                if key != "windows" and isinstance(item, (str, int, float, bool, type(None)))
            }
            windows: list[dict[str, object]] = []
            raw_windows = row.get("windows")
            for raw_window in raw_windows[:8] if isinstance(raw_windows, list) else []:
                window = _dict(raw_window)
                windows.append({
                    _text(key, 48): _scalar(item, 120)
                    for key, item in window.items()
                    if isinstance(item, (str, int, float, bool, type(None)))
                })
            decision["windows"] = windows
            decisions.append(decision)
        selected["decisions"] = decisions
        result["selection"] = selected
    wait = _dict(capacity.get("wait"))
    if wait:
        result["wait"] = {
            _text(key, 48): _scalar(item, 120)
            for key, item in wait.items()
            if isinstance(item, (str, int, float, bool, type(None)))
        }
    return result


def _bounded_snapshot(data: dict[str, object]) -> dict[str, object]:
    """Retain only the small projection needed for one UI refresh."""
    result: dict[str, object] = {}
    mission = _dict(data.get("mission"))
    result["mission"] = {
        key: _text(mission.get(key, ""), 240 if key == "objective" else 100)
        for key in ("id", "name", "objective", "status", "cwd", "updated_at")
        if key in mission
    }
    result["mission"]["role"] = bridge_theme.mission_role(mission.get("id"), _dict(mission.get("config")))  # type: ignore[index]  # projection values are typed object; the surrounding code builds them as dicts
    result["tasks"] = _project_tasks(data.get("tasks"))
    current_task = _dict(data.get("current_task"))
    if current_task:
        result["current_task"] = {
            key: _text(current_task.get(key, ""), 180)
            for key in ("id", "ordinal", "title", "status", "revision")
            if key in current_task
        }
    station_rows, station_context = _station_projection(data.get("stations"))
    result["stations"] = station_rows
    if not station_context:
        station_config = _dict(data.get("station_config"))
        station_observation = _dict(data.get("station_observation"))
        raw_station_context = _dict(data.get("station_context"))
        station_context = {
            "mode": _text(station_config.get("configured_mode") or station_config.get("mode") or "unknown", 24),
            "executing_operation": _text(raw_station_context.get("executing_operation") or "none", 80),
            "operation_status": _text(raw_station_context.get("operation_status") or "unknown", 24),
            "last_launched_station": _text(raw_station_context.get("last_launched_station") or "unknown", 80),
            "provider_liveness": _text(raw_station_context.get("provider_liveness") or "unknown", 24),
            "viewed_station": _text(raw_station_context.get("viewed_station") or "unknown", 80),
            "observed_station": _text(station_observation.get("station") or "unknown", 80),
            "observed_model": _text(station_observation.get("model") or "unknown", 80),
            "observed_effort": _text(station_observation.get("effort") or "unknown", 24),
        }
    result["station_context"] = station_context
    result["events"] = _project_events(data.get("events"))
    if "operator_events" in data:
        result["operator_events"] = _project_events(data.get("operator_events"))
    waiting = _dict(data.get("waiting"))
    if waiting:
        result["waiting"] = {
            "reason": _text(waiting.get("reason"), 24),
            "providers": [_text(item, 60) for item in waiting.get("providers", [])[:8]] if isinstance(waiting.get("providers"), list) else [],  # type: ignore[index]  # projection values are typed object; this line checks the list
            "entries": [
                {key: item.get(key) if isinstance(item.get(key), (int, float, type(None))) else _text(item.get(key), 100)
                 for key in ("name", "window", "remaining_percent", "reset_epoch", "reason", "provider") if key in item}
                for item in _items(waiting.get("entries"))[:8]
            ],
            "until_epoch": waiting.get("until_epoch") if isinstance(waiting.get("until_epoch"), (int, float)) else None,
            "detail": _text(waiting.get("detail"), 240),
        }
    result["operations"] = _projected_rows(
        data.get("operations"),
        20,
        ("id", "operation_id", "task_id", "task_ordinal", "provider", "station", "route_id", "model", "native_session_id", "restoration_state", "phase", "status", "exit_code", "created_at"),
    )
    pending = data.get("pending_commands")
    result["pending_command_count"] = len(pending) if isinstance(pending, list) else 0
    result["task_counts"] = {
        _text(key, 32): count
        for key, count in _dict(data.get("task_counts")).items()
        if isinstance(count, (int, float)) and math.isfinite(count)
    }
    result["proposal_counts"] = {
        _text(key, 32): count
        for key, count in _dict(data.get("proposal_counts")).items()
        if isinstance(count, (int, float)) and math.isfinite(count)
    }
    total_tasks = data.get("task_total")
    if isinstance(total_tasks, int):
        result["task_total"] = total_tasks
    for key in ("controller", "routes", "execution"):
        if key in data:
            if key == "execution" and isinstance(data[key], dict):
                result[key] = {
                    _text(field, 48): _text(item, 180) if isinstance(item, str) else item
                    for field, item in data[key].items()  # type: ignore[attr-defined]  # projection values are typed object; the shape is checked by the surrounding code
                    if isinstance(item, (str, int, float, bool, type(None)))
                }
            else:
                result[key] = _text(data[key], 240)
    station_mode = _dict(data.get("station_mode"))
    if station_mode:
        result["station_mode"] = {
            _text(field, 48): _text(value, 120)
            for field, value in station_mode.items()
            if isinstance(value, (str, int, float, bool, type(None)))
        }
    lifecycle = _dict(data.get("lifecycle"))
    if lifecycle:
        lifecycle_projection: dict[str, object] = {
            _text(field, 48): value if isinstance(value, bool) else _text(value, 180)
            for field, value in lifecycle.items()
            if field != "heartbeat"
            if isinstance(value, (str, int, float, bool, type(None)))
        }
        heartbeat = _dict(lifecycle.get("heartbeat"))
        heartbeat_fields = (
            "reported_state",
            "observed_at",
            "age_seconds",
            "owner_lock_held",
            "desired_mission_status",
            "current_liveness",
            "source",
        )
        if heartbeat:
            lifecycle_projection["heartbeat"] = {
                field: _text(heartbeat[field], 100)
                for field in heartbeat_fields
                if field in heartbeat and isinstance(heartbeat[field], (str, int, float, bool, type(None)))
            }
        result["lifecycle"] = lifecycle_projection
    capacity = _project_capacity(data.get("capacity"))
    if capacity:
        result["capacity"] = capacity
    for key in ("health", "persistence", "criteria", "last_meaningful_progress", "suspend"):
        value = _dict(data.get(key))
        if value:
            result[key] = {
                _text(field, 48): _text(item, 180)
                for field, item in value.items()
                if isinstance(item, (str, int, float, bool, type(None)))
            }
    usage = _dict(data.get("usage"))
    if usage:
        result["usage"] = usage
    return result


def _mission(snapshot: dict[str, object]) -> dict[str, object]:
    nested = _dict(snapshot.get("mission"))
    return nested or snapshot


def _mission_role_of(snapshot: dict[str, object], mission_id: str = "") -> str:
    """`mission_N` identity role of the mission in a snapshot (pinned colour, else a stable hash)."""
    mission = _mission(snapshot)
    explicit = mission.get("role")
    if isinstance(explicit, str) and explicit in bridge_theme.MISSION_ROLES:
        return explicit
    return bridge_theme.mission_role(mission.get("id") or mission_id, _dict(mission.get("config")))


_SEP = " · "
_NO_VALUE = {"", "none", "unknown", "null", "-", "—"}
_ESTIMATE_MISSING = _NO_VALUE | {"no rate data"}


def _known(value: object) -> bool:
    """True for a value worth showing: not None/empty/unknown placeholder."""
    return value is not None and str(value).strip().lower() not in _NO_VALUE


def _clip(text: str, width: int) -> str:
    """Clip to terminal cells, marking the cut with an ellipsis."""
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    mark = glyph("ellipsis")
    return cell_slice(text, 0, max(0, width - cell_width(mark))) + mark


def _int_or_none(value: object) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError, OverflowError):  # not a number, or infinite
        return None


def _span(seconds: object, units: int = 2) -> str:
    """Human duration limited to the largest units, e.g. 5d 19h or 4h 59m."""
    from . import common

    return " ".join(common.fmt_duration(seconds).split()[:units])


def _age(seconds: object, *, note: bool = True) -> str:
    """Compact age text with seconds resolution below a minute."""
    value = _int_or_none(seconds)
    if value is None:
        return "—"
    if note:
        bridge_redraw.note_age(max(0, value))
    return f"{max(0, value)}s" if value < 60 else _span(value)


def _relative_epoch(value: object, now: float | None = None) -> str | None:
    """`in 4h 59m` / `3m ago` for an epoch or ISO timestamp, else None."""
    from . import common

    epoch = common.parse_epoch(value, use_date=False)
    if epoch is None:
        return None
    current = common.now_epoch() if now is None else now
    delta = int(epoch - current)
    remaining = epoch - current
    if delta >= 0:
        # Whole minutes: the text changes as the remaining time drops below the next minute mark,
        # and a past-due target is shown as seconds from one second after it.
        bridge_redraw.note_wall(remaining + 1.01 if remaining < 60 else remaining % 60 + 0.01)
        return f"in {_span(delta)}"
    elapsed = current - epoch
    bridge_redraw.note_wall(int(elapsed) + 1 - elapsed if elapsed < 60 else 60 * (elapsed // 60 + 1) - elapsed)
    return f"{_age(-delta, note=False)} ago"


def _clock_epoch() -> float:
    """Wall-clock epoch for text that is re-derived from the current time on every frame.

    The caller cannot say how soon that text changes, so the frame is repainted each second.
    """
    from . import common

    bridge_redraw.note_wall(1.0)
    return common.now_epoch()


def _local_clock(value: object) -> str:
    """Local HH:MM:SS for an ISO/epoch timestamp."""
    from . import common

    epoch = common.parse_epoch(value, use_date=False)
    if epoch is None:
        return "--:--:--"
    try:
        return time.strftime("%H:%M:%S", time.localtime(epoch))
    except (OverflowError, OSError, ValueError):  # a timestamp outside the platform's range
        return "--:--:--"


def _short_id(value: object, length: int = 8) -> str:
    return _text(value, 64)[:length] if _known(value) else "-"


def _percent_value(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _percent_left(value: object) -> str | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return f"{int(number + 0.5)}%"


_DURATION_PART = re.compile(r"(\d+)\s*([dhms])\b")
_DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _duration_seconds(value: object) -> int | None:
    """Seconds in text like `2d 10h 35m`, or None when it is not a plain duration."""
    text = str(value).strip().lstrip("~").strip()
    if len(text) > 80:  # far longer than any duration; the pattern is quadratic on a long run of digits
        return None
    parts = _DURATION_PART.findall(text)
    if not parts or _DURATION_PART.sub("", text).strip():
        return None
    return sum(int(number) * _DURATION_UNITS[unit] for number, unit in parts)


def _window_summary(window: dict[str, object], now: float) -> str:
    """One capacity scope in plain words, without developer placeholders."""
    from . import common

    name = _text(window.get("name") or window.get("kind") or "scope", 20)
    if window.get("kind") == "opaque":
        return f"{name} quota unmeasured"
    percent = _percent_left(window.get("remaining_percent"))
    amount = window.get("remaining_amount")
    if percent is not None:
        text = f"{name} {percent} left"
    elif _known(amount):
        text = f"{name} {_text(amount, 20)} {_text(window.get('currency') or '', 8)}".rstrip() + " left"
    else:
        text = f"{name} remaining unknown"
    estimate = window.get("estimated_remaining")
    reset = common.parse_epoch(window.get("reset_epoch"), use_date=False)
    if _known(estimate) and str(estimate).strip().lower() not in _ESTIMATE_MISSING and any(char.isdigit() for char in str(estimate)):
        runout = _duration_seconds(estimate)
        if runout is not None and reset is not None and reset > now and runout > reset - now:
            # A runout after the window resets is not a forecast of exhaustion.
            text += ", lasts past reset"
        else:
            text += f", empty in ~{_text(estimate, 24)} at this pace"
    if reset is not None:
        text += ", resets now" if reset <= now else f", resets {_span(reset - now)}"
    if _known(window.get("reason")):
        text += f" ({_text(window.get('reason'), 60)})"
    return text


def _capacity_summaries(snapshot: dict[str, object]) -> list[str]:
    """Plain-language capacity per route; the active route is listed first."""

    capacity = _dict(snapshot.get("capacity"))
    selection = _dict(capacity.get("selection"))
    station_context = _dict(snapshot.get("station_context"))
    operation_id = str(station_context.get("executing_operation") or "")
    operation_status = str(station_context.get("operation_status") or "").upper()
    active_owner = ""
    if operation_id and operation_status == "RUNNING":
        active_owner = str(station_context.get("last_launched_station") or "")
    now = _clock_epoch()
    selected_owner = str(selection.get("route") or selection.get("provider") or "")
    rows = _items(selection.get("decisions"))
    owners = [str(row.get("route") or row.get("provider") or "unknown") for row in rows]
    # "last selected" needs a recorded operation for that owner; the snapshot lists the recent ones.
    ran = {str(op.get(key) or "") for op in _items(snapshot.get("operations")) for key in ("route_id", "station", "provider")}
    # Rotation order is the decisions order; "next" is the first other entry that is not known to be unusable.
    next_owner = next((owner for row, owner in zip(rows, owners) if owner != active_owner and str(row.get("usable")).lower() != "false"), "")
    summaries: list[tuple[bool, str]] = []
    for row, owner in zip(rows, owners):
        scopes = [_window_summary(window, now) for window in _items(row.get("windows"))]
        if active_owner and owner == active_owner:
            label = "active"
        elif owner == selected_owner and owner in ran:
            label = "last selected"
        elif str(row.get("usable")).lower() == "false":
            label = "not ready"
        elif owner == next_owner:
            label = "next"
        else:
            label = ""
        label_text = f" ({label})" if label else ""
        model = f" / {_text(row['selected_model'], 40)}" if _known(row.get("selected_model")) else ""
        summaries.append((label == "active", f"{_text(owner, 40)}{model}{label_text}{_SEP}" + (_SEP.join(scopes) or "capacity unavailable")))
    return [text for _, text in sorted(summaries, key=lambda item: not item[0])]


def _usage_rows_for(snapshot: dict[str, object], provider: str, model: str = "") -> list[dict[str, object]]:
    """Rows of the embedded llm-usage dashboard that describe `provider` (and `model`, when both name one)."""
    wanted = provider.strip().lower()
    chosen = []
    for row in _items(_dict(snapshot.get("usage")).get("rows")):
        if str(row.get("provider") or "").strip().lower() != wanted:
            continue
        row_model = str(row.get("model") or "").strip().lower()
        if row_model and model and row_model not in model.lower() and model.lower() not in row_model:
            continue
        chosen.append(row)
    return chosen


def _usage_window_lines(rows: list[dict[str, object]], now: float) -> list[str]:
    """One plain-words capacity line per usage scope (`5h 48% left, resets 3h 43m`)."""
    return [
        _window_summary({
            "name": row.get("scope"), "kind": "reset_window", "remaining_percent": row.get("remaining_percent"),
            "estimated_remaining": row.get("estimated_remaining"), "reset_epoch": row.get("reset_epoch"),
        }, now)
        for row in rows
    ]


def _usage_capacity_lines(snapshot: dict[str, object]) -> list[str]:
    """`■ claude · 5h 48% left, … · week 79% left, …` per provider, from the embedded usage dashboard."""

    now = _clock_epoch()
    providers: dict[str, list[dict[str, object]]] = {}
    for row in _items(_dict(snapshot.get("usage")).get("rows")):
        providers.setdefault(str(row.get("provider") or "").strip().lower(), []).append(row)
    return [
        _with_square(f"{_text(name, 40)}{_SEP}" + _SEP.join(_usage_window_lines(rows, now)))
        for name, rows in providers.items() if name
    ]


def _with_square(text: str) -> str:
    """Put the provider square in front of a capacity line (`■ codex (active) · 5h 95% left`)."""
    return f"{glyph('square')} {text}"


def _task_label(task: dict[str, object]) -> str:
    identifier = _text(task_number(task) if task_number(task) is not None else "?", 40)
    title = _cut_text(task.get("title") or task.get("prompt") or "Untitled task", 90)
    state = _text(task.get("status", "unknown"), 24)
    return f"{identifier}  {_state_text(state)}  {title}"


_ORDER_STATE_RANK = {
    name: rank
    for rank, name in enumerate(("RUNNING", "VERIFYING", "READY", "QUEUED", "BLOCKED", "PAUSED", "FAILED", "COMPLETED", "CANCELLED"))
}


def _count_phrase(counts: dict[str, object]) -> str:
    """`1 running · 2 queued` in lifecycle order, skipping zero counts."""
    pairs = [
        (str(state).upper(), count)
        for state, count in counts.items()
        if isinstance(count, (int, float)) and count and math.isfinite(count)
    ]
    pairs.sort(key=lambda item: (_ORDER_STATE_RANK.get(item[0], 99), item[0]))
    return _SEP.join(f"{int(count)} {_text(state, 12).lower()}" for state, count in pairs)


def _controller_line(data: dict[str, object]) -> str:
    """What the controller is doing, derived from lifecycle + heartbeat."""
    mission = _mission(data)
    status = str(mission.get("status") or "").upper()
    lifecycle = _dict(data.get("lifecycle"))
    if not status and lifecycle:
        status = str(lifecycle.get("status") or "").upper()
    heartbeat = _dict(lifecycle.get("heartbeat"))
    if not status:
        return "status not loaded yet"
    if status == "FAILED" and _known(lifecycle.get("failure_reason")):
        return f"failed — {_text(lifecycle.get('failure_reason'), 160)}"
    if status in ENDED_MISSION_STATUSES:
        return f"mission {status.lower()} — controller finished"
    if status == "CREATED":
        return "not started — type start"
    if status == "STOPPING":
        return "stopping after the current step"
    if status == "STOPPED":
        return "stopped — type start or resume"
    if status in STARTED_MISSION_STATUSES and lifecycle.get("controller_running") is False:
        return "not running — type resume"
    if status == "PAUSED":
        context = _dict(data.get("station_context"))
        if context.get("executing_operation") and str(context.get("operation_status", "")).upper() in ACTIVE_OPERATION_STATUSES:
            return "pausing after the current provider invocation"
    age = _int_or_none(heartbeat.get("age_seconds"))
    reported = str(heartbeat.get("reported_state") or "").upper()
    lead = "paused, controller " if status == "PAUSED" else ""
    if reported == "ACTIVE" and age is not None and age <= 30:
        return f"{lead}active · heartbeat {_age(age)} ago"
    if age is not None:
        return f"silent for {_age(age)} — controller may have stopped (type status or resume)"
    return "no heartbeat recorded yet"


def _review_hold_task(data: dict[str, object]) -> str | None:
    """Number of the reviewed task the mission is holding the next task for, else None."""
    hold = _dict(data.get("hold"))
    if hold.get("reason") != "review" or not _known(hold.get("task_ordinal")):
        return None
    return _text(hold["task_ordinal"], 12)


def _review_hold_text(number: str) -> str:
    return f"task {number} awaits your review{_SEP}/approve {number}{_SEP}/reject {number} reason{_SEP}/continue"


def _needs_you_rows(data: dict[str, object]) -> list[str]:
    """Items that wait for the operator, most urgent first."""
    tasks = _items(data.get("tasks"))
    rows: list[str] = []
    held = _review_hold_task(data)
    waiting = _dict(data.get("waiting"))
    if waiting:
        from .bridge_monitor import waiting_message

        rows.append(waiting_message(waiting, _clock_epoch()))
    for task in tasks:
        if str(task.get("status", "")).upper() == "VERIFYING":
            number = _text(task_number(task) if task_number(task) is not None else "?", 12)
            if number == held:
                rows.append(_review_hold_text(number))
                continue
            rows.append(f"task {number} awaits your approval — type /approve {number} (or F7 Actions)")
    pending = _dict(data.get("proposal_counts")).get("PENDING")
    if isinstance(pending, (int, float)) and pending:
        rows.append(f"{int(pending)} agent task proposal{'s' if pending != 1 else ''} to review — type proposals")
    from .bridge_format import declined_message, declined_provider

    stuck = []
    for task in tasks:
        if str(task.get("status", "")).upper() not in {"BLOCKED", "FAILED"}:
            continue
        declined = declined_provider(data, task) if str(task.get("status", "")).upper() == "BLOCKED" else ""
        if declined:
            rows.append(declined_message(declined))
            continue
        stuck.append(f"task {_text(task_number(task) if task_number(task) is not None else '?', 12)} {str(task.get('status')).upper()}")
    if stuck:
        rows.append(_SEP.join(stuck[:3]) + (f" (+{len(stuck) - 3} more)" if len(stuck) > 3 else "") + " — inspect in Tasks")
    return rows[:5]


# --- tmux policy lives in ``terminal``; these thin names keep the console's call sites and the
# tests that replace them (``_execute_terminal_action``, ``_read_provider_activity``) unchanged.
_ACTIVITY_SEEN = terminal.ACTIVITY_SEEN
_inside_station = terminal.inside_station
_outside_tmux_client = terminal.outside_tmux_client
_is_bridge_station_socket = terminal.is_bridge_station_socket


def _read_provider_activity(mission_id: str) -> tuple[int, str] | None:
    """The provider pane's tmux activity epoch and screen digest, or None when it cannot be observed."""
    try:
        from .bridge import _load_handle

        return terminal.read_provider_activity(mission_id, _load_handle)
    except Exception:  # Observation is best effort; never surface an error.
        return None


def _execute_terminal_action(source_mission: str, action: TerminalAction) -> None:
    """Perform a typed terminal action after curses restores the user's terminal."""
    from .bridge import _load_handle

    terminal.execute_terminal_action(source_mission, action, _load_handle)


def _idle_phrase(snapshot: dict[str, object]) -> str:
    """Observation text such as `no output for 2m`, or empty when output is recent."""
    idle = snapshot.get("output_idle_seconds")
    if not isinstance(idle, (int, float)) or isinstance(idle, bool) or idle < _OUTPUT_IDLE_THRESHOLD_SECONDS:
        return ""
    if idle < 60:
        return f"no output for {int(idle)}s"
    # Whole minutes rounded down, so the indicator never overstates.
    return f"no output for {fmt_duration(int(idle // 60) * 60)}"


def _station_idle_phrase(snapshot: dict[str, object]) -> str:
    """The idle phrase for the tmux status line, in coarser steps than the console's own.

    Every change of the status text repaints every attached client, so the
    station shows `no output for 30s`, then 40s and 50s, then whole minutes,
    instead of a new value each second.
    """
    idle = snapshot.get("output_idle_seconds")
    if isinstance(idle, (int, float)) and not isinstance(idle, bool) and _OUTPUT_IDLE_THRESHOLD_SECONDS <= idle < 60:
        snapshot = {**snapshot, "output_idle_seconds": int(idle // 10) * 10}
    return _idle_phrase(snapshot)


def _station_status_parts(snapshot: dict[str, object]) -> tuple[str, str]:
    """`(mission label, task/state text)` for the tmux status line.

    The task comes first in the state text; the mission state is appended unless
    a task is running in an ordinary RUNNING mission. The label (repository
    folder or mission name, at most 14 cells) says which mission this station is.
    """
    mission_status = _text(_mission(snapshot).get("status") or "unknown", 16).upper()
    task = _dict(snapshot.get("current_task"))
    if task:
        idle = _station_idle_phrase(snapshot) if str(task.get("status") or "").upper() == "RUNNING" else ""
        label = f"Task {_text(task_number(task) or '?', 8)} " + (idle or _text(task.get('status') or 'unknown', 12).upper())
    else:
        label = "No task"
    if not (mission_status == "UNKNOWN" or (task and mission_status == "RUNNING")):
        label = f"{label}{_SEP}{mission_status}"
    from .bridge_switcher import mission_label

    mission = _mission(snapshot)
    return mission_label(mission.get("name"), mission.get("cwd")), label


def _station_status_segments(snapshot: dict[str, object]) -> list[Segment]:
    """The state text of the tmux status line with the state words in their theme colours.

    Same content as `_station_status_parts` plus the state glyph: `Task 6 ● RUNNING`.
    A silent provider shows its idle phrase (`muted`, `warn` after ten minutes) instead.
    """
    mission_status = _text(_mission(snapshot).get("status") or "unknown", 16).upper()
    task = _dict(snapshot.get("current_task"))
    out: list[Segment] = []
    if task:
        status = str(task.get("status") or "unknown")
        idle = _station_idle_phrase(snapshot) if status.upper() == "RUNNING" else ""
        out.append((f"Task {_text(task_number(task) or '?', 8)} ", "info"))
        out.append((idle, _idle_role(idle)) if idle else _state_segment(_text(status, 12)))
    else:
        out.append(("No task", "muted"))
    if not (mission_status == "UNKNOWN" or (task and mission_status == "RUNNING")):
        out += [(_SEP, "dim"), _state_segment(mission_status)]
    return out


def _station_status_context(snapshot: dict[str, object]) -> str:
    """Compact, most-important-first state for the tmux status line, led by the mission label."""
    where, label = _station_status_parts(snapshot)
    return f"{where}{_SEP}{label}" if where else label


def _station_label(station: dict[str, object]) -> str:
    name = _text(station.get("name") or station.get("provider") or station.get("id") or "Station", 60)
    configured_model = _text(station.get("model"), 50) if _known(station.get("model")) else "default"
    observed_model = _text(station.get("observed_model"), 50) if _known(station.get("observed_model")) else "-"
    state = _text(station.get("restoration_state") or station.get("status") or "", 24)
    state = "" if not _known(state) else f"  {state}"
    return f"{name}  model {configured_model}  observed {observed_model}{state}"


def _bounded_event_data(value: object, depth: int = 3) -> object:
    """Keep event data structured but bounded in depth, width and text size."""
    if isinstance(value, dict):
        if depth <= 0:
            return f"{len(value)} fields"
        return {
            _text(key, 48): _bounded_event_data(item, depth - 1)
            for key, item in list(value.items())[:_MAX_DETAIL_ITEMS]
        }
    if isinstance(value, list):
        if depth <= 0:
            return f"{len(value)} items"
        return [_bounded_event_data(item, depth - 1) for item in value[:6]]
    if isinstance(value, str):
        return _text(value, 160)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _text(value, 80)


def _project_events(value: object, limit: int = _MAX_EVENT_ROWS) -> list[dict[str, object]]:
    """Project events with their data kept as a bounded dict (never a repr)."""
    projected: list[dict[str, object]] = []
    for event in _items(value)[:limit]:
        row: dict[str, object] = {
            key: _text(event[key], 200)
            for key in ("id", "event_type", "type", "kind", "summary", "created_at", "task_id", "task_ordinal", "operation_id")
            if key in event
        }
        for key in ("data", "payload"):
            if key not in event:
                continue
            data = _bounded_event_data(event[key])
            if len(json.dumps(data, default=str)) > 2400 and isinstance(data, dict):
                data = {name: item for name, item in data.items() if isinstance(item, (str, int, float, bool, type(None)))}
            source = event[key]
            if isinstance(data, dict) and isinstance(source, dict) and isinstance(source.get("decisions"), list):
                # Window lists fall below the depth bound; keep the remaining percentages as text.
                brief = _capacity_brief(source["decisions"], str(source.get("route") or source.get("provider") or ""))
                if brief:
                    data["capacity_brief"] = brief
            row["data"] = data
            break
        projected.append(row)
    return projected


def _compact_pairs(value: dict[str, object], prefix: str = "", limit: int = 8) -> list[str]:
    """Flatten a dict to `k=v` words: no braces or quotes, nulls skipped."""
    pairs: list[str] = []
    for key, item in value.items():
        name = prefix + _text(key, 32)
        if item is None or item == "" or item == []:
            continue
        if isinstance(item, dict):
            pairs.extend(_compact_pairs(item, name + ".", limit))
        elif isinstance(item, list):
            pairs.append(f"{name}={len(item)} items")
        elif isinstance(item, bool):
            pairs.append(f"{name}={'yes' if item else 'no'}")
        else:
            pairs.append(f"{name}={_text(item, 40)}")
    return pairs[:limit]


_REVIEW_HOLD_EVENTS = frozenset({"review-hold", "review-hold-released"})  # operator-meaningful, never bookkeeping


def _event_summary(event: dict[str, object]) -> str:
    """One human line for a durable event."""
    kind = str(event.get("kind") or event.get("event_type") or event.get("type") or "event")
    raw = event.get("data")
    data = raw if isinstance(raw, dict) else {}
    task = f"task {_text(event['task_id'], 12)}" if _known(event.get("task_id")) else ""
    op = f"op {_short_id(event.get('operation_id'))}" if _known(event.get("operation_id")) else ""
    transition = ""
    if _known(data.get("from")) or _known(data.get("to")):
        transition = f"{_text(data.get('from') or '?', 20)} → {_text(data.get('to') or '?', 20)}"
    reason = f" ({_text(data['reason'], 60)})" if _known(data.get("reason")) else ""
    if kind in _REVIEW_HOLD_EVENTS and _known(data.get("summary")):
        return _text(data["summary"], 160)
    if kind == "mission-created":
        return f"mission created · {_text(data.get('name') or '', 60)}".rstrip(" ·")
    if kind == "task-added":
        title = _text(data.get("title") or "", 60)
        return f"{task or 'task'} added" + (f" · {title}" if title else "")
    if kind in {"task-ready", "task-status"}:
        return f"{task or 'task'} {transition}{reason}".strip()
    if kind == "mission-status":
        return f"mission {transition}{reason}".strip()
    if kind == "operation-prepared":
        return _SEP.join(part for part in (f"{op} prepared", task, _text(data.get("station") or "", 40)) if part)
    if kind == "operation-running":
        who = _text(data.get("route_id") or "", 40)
        model = _text(data.get("model") or "", 40)
        return _SEP.join(part for part in (f"{op} running", task, " ".join(item for item in (who, model) if item), f"pid {_text(data['process_pid'], 12)}" if _known(data.get("process_pid")) else "") if part)
    if kind == "operation-exited":
        state = f"{op} exited {_text(data.get('exit_code', '?'), 8)}"
        return _SEP.join(part for part in (state, task, f"task now {_text(data['task_status'], 20)}" if _known(data.get("task_status")) else "") if part)
    if kind in {"provider-selection", "capacity-refresh"}:
        owner = _text(data.get("route") or data.get("provider") or "", 40)
        model = _text(data.get("selected_model") if _known(data.get("selected_model")) else "", 40)
        why = _text(plain_reason(data.get("rotation_reason") or ""), 40)
        verb = "selected" if kind == "provider-selection" else "capacity refreshed"
        return _SEP.join(part for part in (f"{verb} {owner}".rstrip(), model, why) if part)
    if kind.startswith("command-"):
        return f"command {kind[8:]}: {_text(data.get('command') or '', 60)}".rstrip(": ")
    if kind == "native-session-restoration":
        session = _short_id(data.get("session_id"), 13)
        return _SEP.join(part for part in (f"session {_text(data.get('restoration_state') or 'unknown', 30).lower()}", _text(data.get("provider") or "", 20), session if session != "-" else "", op) if part)
    if kind == "operation-uncertain":
        return _SEP.join(part for part in (f"{op} outcome unknown".strip(), _text(data.get("reason") or "", 100)) if part)
    if kind == "repository-checkpoint":
        return _SEP.join(part for part in ("repository checkpoint saved", f"task {_text(data['task_id'], 12)}" if _known(data.get("task_id")) else task, f"revision {_text(data['revision'], 8)}" if _known(data.get("revision")) else "") if part)
    if kind == "task-rejected":
        return _SEP.join(part for part in (f"{task or 'task'} sent back", _text(data.get("reason") or "", 80)) if part)
    if kind == "rotation-changed":
        return f"rotation {transition}".strip()
    if kind == "task-verified":
        approved = " approved by operator" if data.get("approved") else ""
        return f"{task or 'task'} verified · {_text(data.get('evidence_count', 0), 8)} evidence{approved}"
    if kind == "verification-failed":
        return _SEP.join(part for part in (f"{task or 'task'} verification failed", " ".join(_compact_pairs(data, limit=4))) if part)
    if kind == "provider-handoff":
        return _SEP.join(part for part in (
            f"{task or 'task'} handed off after exit {_text(data.get('exit_code', '?'), 8)}",
            f"failure {_text(data.get('failure_count', '?'), 6)}/{_text(data.get('max_failures', '?'), 6)}",
        ) if part)
    if kind == "provider-handoff-selected":
        return f"handoff to {_text(data.get('station') or '?', 40)}"
    if kind == "provider-handoff-exhausted":
        return f"{task or 'task'} out of handoffs · {_text(data.get('failures', '?'), 6)} failures, limit {_text(data.get('max_failures', '?'), 6)}"
    if kind == "provider-wait":
        until = _relative_epoch(data.get("target_epoch"))
        return _SEP.join(part for part in (
            "waiting for capacity",
            f"resumes {until}" if until else "",
            _text(data.get("reason") or "", 60),
        ) if part)
    if kind == "controller-heartbeat":
        return _text(data.get("controller_state") or "", 24)
    pairs = _compact_pairs(data) if data else []
    if not pairs:
        fallback = event.get("summary")
        return _text(fallback, 100) if _known(fallback) else ""
    return " ".join(pairs)


# Derived from the event registry (`bridge_events.EVENTS`), which holds the classification of every kind.
_COLLAPSED_EVENTS = bridge_events.collapsed_kinds()
_PLUMBING_EVENTS = bridge_events.plumbing_kinds()

_TASK_STATE_WORDS = {
    "READY": "ready", "QUEUED": "waiting", "RUNNING": "running", "VERIFYING": "awaiting approval",
    "COMPLETED": "completed", "FAILED": "failed", "BLOCKED": "blocked", "PAUSED": "paused",
    "CANCELLED": "cancelled",
}
_MISSION_STATE_WORDS = {
    "PAUSED": "Paused", "STOPPING": "Stopping", "STOPPED": "Stopped", "COMPLETED": "Mission completed",
    "CANCELLED": "Mission cancelled", "FAILED": "Mission failed",
}
_WINDOW_NAMES = {"5h": "5h", "weekly": "week", "monthly": "month"}


def _window_brief(window: dict[str, object]) -> str:
    """`5h 95%` (percent left) for one measurable window, else empty."""
    if window.get("kind") == "opaque":
        return ""
    percent = _percent_left(window.get("remaining_percent"))
    if percent is None:
        return ""
    name = _text(window.get("name") or window.get("kind") or "", 12)
    return f"{_WINDOW_NAMES.get(name, name)} {percent}".strip()


def _decision_owner(row: dict[str, object]) -> str:
    return str(row.get("route") or row.get("provider") or "")


def _capacity_brief(decisions: object, owner: str = "") -> str:
    """`5h 95%, week 21%` for `owner` (else the first decision with measurable windows)."""
    rows = [_dict(row) for row in decisions] if isinstance(decisions, list) else []
    chosen = [row for row in rows if _decision_owner(row) == owner] or rows
    for row in chosen:
        windows = row.get("windows")
        parts = [_window_brief(_dict(window)) for window in windows] if isinstance(windows, list) else []
        brief = ", ".join(part for part in parts if part)
        if brief:
            return brief
    return ""


def _event_kind(event: dict[str, object]) -> str:
    return str(event.get("kind") or event.get("event_type") or event.get("type") or "event")


def _rotation_text(value: object) -> str:
    """`codex,claude` as `codex → claude`."""
    names = [item.strip() for item in str(value or "").split(",") if item.strip()]
    return f" {glyph('arrow')} ".join(_text(item, 30) for item in names) or "default"


def _task_numbers(snapshot: dict[str, object]) -> dict[str, str]:
    """Task id to displayed number for the tasks on this page (differs once tasks carry an ordinal)."""
    return {str(task["id"]): str(task_number(task)) for task in _items(snapshot.get("tasks")) if task.get("id") is not None}


@dataclass(frozen=True)
class _EventContext:
    """What a humaniser needs to phrase one event."""

    kind: str
    data: dict[str, object]
    event: dict[str, object]
    task: str  # `Task 3`, or `Task` when the event names none
    on_task: str  # ` on task 3`, or empty
    reason: str  # ` (reason)`, or empty
    owner: str  # route, provider or station named by the data
    model: str
    new_state: str
    old_state: str


# One phrase function per event kind. A handler returns the phrase, or `(phrase, is_plumbing)` when the
# flag depends on the event; otherwise the flag comes from `bridge_events.EVENTS`.
_HUMANIZERS: dict[str, Callable[[_EventContext], str | tuple[str, bool]]] = {}


def _humanizer(*kinds: EventKind) -> Callable[[Callable[[_EventContext], str | tuple[str, bool]]], Callable[[_EventContext], str | tuple[str, bool]]]:
    def register(function: Callable[[_EventContext], str | tuple[str, bool]]) -> Callable[[_EventContext], str | tuple[str, bool]]:
        for kind in kinds:
            _HUMANIZERS[str(kind)] = function
        return function

    return register


def _with_reason(label: str, data: dict[str, object], limit: int = 100) -> str:
    return label + (f" · {_text(data['reason'], limit)}" if _known(data.get("reason")) else "")


@_humanizer(EventKind.REVIEW_HOLD, EventKind.REVIEW_HOLD_RELEASED)
def _h_review_hold(ctx: _EventContext) -> str:
    summary = _text(ctx.data.get("summary") or "", 160)
    fallback = "Holding the next task for your review" if ctx.kind == "review-hold" else "Review hold released"
    return summary[:1].upper() + summary[1:] if summary else fallback


@_humanizer(EventKind.MISSION_COLOUR)
def _h_mission_colour(ctx: _EventContext) -> str:
    value = ctx.data.get("value")
    if isinstance(value, int) and 1 <= value <= bridge_theme.MISSION_COUNT:
        return f"Mission colour set to {value} ({bridge_theme.MISSION_HUE_NAMES[value - 1]})"
    return "Mission colour back to automatic"


@_humanizer(EventKind.MISSION_CREATED)
def _h_mission_created(ctx: _EventContext) -> str:
    return f"Mission created · {_text(ctx.data.get('name') or '', 60)}".rstrip(" ·")


@_humanizer(EventKind.MISSION_STATUS)
def _h_mission_status(ctx: _EventContext) -> str:
    if ctx.new_state == "RUNNING":
        return "Resumed" if ctx.old_state == "PAUSED" else "Mission started"
    return f"{_MISSION_STATE_WORDS.get(ctx.new_state, 'Mission ' + (ctx.new_state.lower() or 'changed'))}{ctx.reason}"


@_humanizer(EventKind.TASK_ADDED)
def _h_task_added(ctx: _EventContext) -> str:
    title = _text(ctx.data.get("title") or "", 60)
    return f"{ctx.task} added" + (f" · {title}" if title else "")


@_humanizer(EventKind.TASK_READY, EventKind.TASK_STATUS)
def _h_task_status(ctx: _EventContext) -> tuple[str, bool]:
    if ctx.new_state == "QUEUED" and ctx.data.get("reason") == "dependency-added":
        return f"{ctx.task} waiting on a new dependency", False
    word = _TASK_STATE_WORDS.get(ctx.new_state, ctx.new_state.lower() or "updated")
    return f"{ctx.task} {word}{ctx.reason}", ctx.new_state == "RUNNING"


@_humanizer(EventKind.OPERATION_RUNNING)
def _h_operation_running(ctx: _EventContext) -> str:
    return f"Started {ctx.owner or 'provider'}" + (f" ({ctx.model})" if ctx.model else "") + ctx.on_task


@_humanizer(EventKind.OPERATION_EXITED)
def _h_operation_exited(ctx: _EventContext) -> str:
    return f"Provider exited ({_text(ctx.data.get('exit_code', '?'), 8)}){ctx.on_task}"


@_humanizer(EventKind.OPERATION_UNCERTAIN)
def _h_operation_uncertain(ctx: _EventContext) -> str:
    return "Provider outcome unknown" + (f" · {_text(ctx.data['reason'], 100)}" if _known(ctx.data.get("reason")) else "")


@_humanizer(EventKind.OPERATION_ABORTED_BEFORE_LAUNCH)
def _h_operation_aborted(ctx: _EventContext) -> str:
    return "Launch aborted" + (f" · {_text(ctx.data['reason'], 100)}" if _known(ctx.data.get("reason")) else "")


@_humanizer(EventKind.OPERATION_PREPARED)
def _h_operation_prepared(ctx: _EventContext) -> str:
    return f"Preparing {ctx.owner or 'provider'}{ctx.on_task}"


@_humanizer(EventKind.PROVIDER_SELECTION)
def _h_provider_selection(ctx: _EventContext) -> str:
    return f"Selected {ctx.owner}".rstrip() + (f" ({ctx.model})" if ctx.model else "")


@_humanizer(EventKind.CAPACITY_REFRESH)
def _h_capacity_refresh(ctx: _EventContext) -> str:
    brief = _text(ctx.data.get("capacity_brief") or "", 80)
    return f"Capacity: {ctx.owner or 'providers'} {brief}".rstrip() if brief else f"Capacity refreshed: {ctx.owner or 'providers'}"


@_humanizer(EventKind.CAPACITY_REFRESH_ERROR)
def _h_capacity_refresh_error(ctx: _EventContext) -> str:
    return "Capacity refresh failed" + (f" ({_text(ctx.data['reason'], 60)})" if _known(ctx.data.get("reason")) else "")


@_humanizer(EventKind.CONTROLLER_HEARTBEAT)
def _h_controller_heartbeat(ctx: _EventContext) -> str:
    return "controller alive"


@_humanizer(EventKind.CONTROLLER_HEARTBEAT_ERROR)
def _h_controller_heartbeat_error(ctx: _EventContext) -> str:
    return "controller heartbeat failed" + (f" ({_text(ctx.data['reason'], 40)})" if _known(ctx.data.get("reason")) else "")


@_humanizer(EventKind.CONTROLLER_EXITED_UNEXPECTEDLY)
def _h_controller_exited(ctx: _EventContext) -> str:
    return "Controller exited unexpectedly" + ctx.reason


@_humanizer(EventKind.COMMAND_APPLIED, EventKind.COMMAND_FAILED, EventKind.COMMAND_QUEUED, EventKind.COMMAND_REJECTED)
def _h_command(ctx: _EventContext) -> str:
    return f"Command {ctx.kind[len('command-'):]}: {_text(ctx.data.get('command') or '', 60)}".rstrip(": ")


@_humanizer(EventKind.STATION_HANDOFF, EventKind.PROVIDER_SWITCHED)
def _h_provider_switched(ctx: _EventContext) -> str:
    return f"Switched provider {_text(ctx.data.get('from') or '?', 30)} → {_text(ctx.data.get('to') or '?', 30)}{ctx.reason}"


@_humanizer(EventKind.ROTATION_CHANGED)
def _h_rotation_changed(ctx: _EventContext) -> str:
    return f"Rotation changed to {_rotation_text(ctx.data.get('to'))}"


@_humanizer(EventKind.ROTATION_APPLIED)
def _h_rotation_applied(ctx: _EventContext) -> str:
    return f"Rotation applied: {_rotation_text(ctx.data.get('routes') or ctx.data.get('providers'))}"


@_humanizer(EventKind.ROTATION_REJECTED)
def _h_rotation_rejected(ctx: _EventContext) -> str:
    return "Rotation change rejected" + (f" · {_text(ctx.data['error'], 100)}" if _known(ctx.data.get("error")) else "")


@_humanizer(EventKind.TASK_REJECTED)
def _h_task_rejected(ctx: _EventContext) -> str:
    return f"{ctx.task} sent back" + (f" · {_text(ctx.data['reason'], 100)}" if _known(ctx.data.get("reason")) else "")


@_humanizer(EventKind.MISSION_REOPENED)
def _h_mission_reopened(ctx: _EventContext) -> str:
    return _with_reason("Mission reopened", ctx.data)


@_humanizer(EventKind.CONTROLLER_FAILED)
def _h_controller_failed(ctx: _EventContext) -> str:
    return _with_reason("Controller failed", ctx.data)


@_humanizer(EventKind.STATION_STOPPED)
def _h_station_stopped(ctx: _EventContext) -> str:
    return "Provider station stopped"


@_humanizer(EventKind.PROVIDER_HANDOFF)
def _h_provider_handoff(ctx: _EventContext) -> str:
    return (f"{ctx.task} handed off after exit {_text(ctx.data.get('exit_code', '?'), 8)} "
            f"(failure {_text(ctx.data.get('failure_count', '?'), 6)}/{_text(ctx.data.get('max_failures', '?'), 6)})")


@_humanizer(EventKind.PROVIDER_HANDOFF_SELECTED)
def _h_provider_handoff_selected(ctx: _EventContext) -> str:
    return f"Handing off to {_text(ctx.data.get('station') or '?', 40)}"


@_humanizer(EventKind.PROVIDER_HANDOFF_EXHAUSTED)
def _h_provider_handoff_exhausted(ctx: _EventContext) -> str:
    return f"{ctx.task} out of handoffs ({_text(ctx.data.get('failures', '?'), 6)} failures, limit {_text(ctx.data.get('max_failures', '?'), 6)})"


@_humanizer(EventKind.PROVIDER_WAIT)
def _h_provider_wait(ctx: _EventContext) -> str:
    until = _relative_epoch(ctx.data.get("target_epoch"))
    return "Waiting for capacity" + (f", resumes {until}" if until else "") + (f" · {_text(ctx.data['reason'], 60)}" if _known(ctx.data.get("reason")) else "")


@_humanizer(EventKind.TASK_VERIFIED)
def _h_task_verified(ctx: _EventContext) -> str:
    return f"{ctx.task} verified" + (" (approved by you)" if ctx.data.get("approved") else "")


@_humanizer(EventKind.VERIFICATION_FAILED)
def _h_verification_failed(ctx: _EventContext) -> str:
    return f"{ctx.task} verification failed"


@_humanizer(EventKind.TASK_EVIDENCE_RECORDED)
def _h_task_evidence(ctx: _EventContext) -> str:
    count = ctx.data.get("evidence_count")
    return f"{ctx.task} evidence recorded" + (f" ({_text(count, 6)} item{'s' if count != 1 else ''})" if _known(count) else "")


@_humanizer(EventKind.TASK_AMENDED)
def _h_task_amended(ctx: _EventContext) -> str:
    return f"{ctx.task} amended"


@_humanizer(EventKind.TASK_AMENDMENT_APPLIED)
def _h_task_amendment_applied(ctx: _EventContext) -> str:
    return f"{ctx.task} amendment applied"


@_humanizer(EventKind.TASK_PRIORITY)
def _h_task_priority(ctx: _EventContext) -> str:
    return f"{ctx.task} priority changed"


@_humanizer(EventKind.TASK_REORDERED)
def _h_task_reordered(ctx: _EventContext) -> str:
    return f"{ctx.task} reordered"


@_humanizer(EventKind.DEPENDENCY_ADDED)
def _h_dependency_added(ctx: _EventContext) -> str:
    deps = ctx.data.get("depends_on")
    wait = ", ".join(_text(item, 12) for item in deps) if isinstance(deps, list) else _text(deps or "", 40)
    return f"{ctx.task} now waits for {wait}".rstrip()


@_humanizer(EventKind.TASK_PROPOSED)
def _h_task_proposed(ctx: _EventContext) -> str:
    return f"Agent proposed {ctx.task.lower()}"


@_humanizer(EventKind.TASK_PROPOSAL_ACCEPTED)
def _h_task_proposal_accepted(ctx: _EventContext) -> str:
    return f"Proposal accepted: {ctx.task.lower()}"


@_humanizer(EventKind.TASK_PROPOSAL_REJECTED)
def _h_task_proposal_rejected(ctx: _EventContext) -> str:
    return f"Proposal rejected: {ctx.task.lower()}"


@_humanizer(EventKind.NATIVE_SESSION_RESTORATION)
def _h_native_session(ctx: _EventContext) -> str:
    state = _text(ctx.data.get("restoration_state") or "unknown", 30).lower().replace("_", " ")
    return f"Session {state}" + (f" · {_text(ctx.data['provider'], 20)}" if _known(ctx.data.get("provider")) else "")


@_humanizer(EventKind.REPOSITORY_CHECKPOINT)
def _h_repository_checkpoint(ctx: _EventContext) -> str:
    return "Repository checkpoint saved"


@_humanizer(EventKind.OPERATION_PROCESS)
def _h_operation_process(ctx: _EventContext) -> str:
    return "Provider process started"


@_humanizer(EventKind.OPERATION_PROGRESS)
def _h_operation_progress(ctx: _EventContext) -> str:
    return "Provider progress noted"


@_humanizer(EventKind.CONTROLLER_MODE)
def _h_controller_mode(ctx: _EventContext) -> str:
    return f"Controller mode: {_text(ctx.data.get('actual_mode') or 'unknown', 20)}"


@_humanizer(EventKind.CONTROLLER_CONFIG)
def _h_controller_config(ctx: _EventContext) -> str:
    return "Controller configured"


@_humanizer(EventKind.CONTROLLER_OWNERSHIP)
def _h_controller_ownership(ctx: _EventContext) -> str:
    return "Controller ownership verified"


@_humanizer(EventKind.STATION_OBSERVATION)
def _h_station_observation(ctx: _EventContext) -> str:
    return "Provider screen observed"


@_humanizer(
    EventKind.STEERING_HANDOFF_CONSUMED, EventKind.STEERING_HANDOFF_REQUESTED, EventKind.STEERING_PAUSE_APPLIED,
    EventKind.STEERING_PAUSE_REQUESTED, EventKind.STEERING_STOP_TASK_APPLIED, EventKind.STEERING_STOP_TASK_REQUESTED,
    EventKind.STEERING_VERIFY_APPLIED, EventKind.STEERING_VERIFY_DRAINED, EventKind.STEERING_VERIFY_REQUESTED,
)
def _h_steering(ctx: _EventContext) -> str:
    return "Steering " + ctx.kind[len("steering-"):].replace("-", " ")


def _humanize_event(event: dict[str, object], numbers: dict[str, str] | None = None) -> tuple[str, bool]:
    """(operator-facing phrase, is_plumbing) for one durable event.

    `numbers` maps task ids to the number shown for them, when that differs. The phrase comes from
    the kind's entry in `_HUMANIZERS`; the flag from `bridge_events.EVENTS` unless the handler
    returns its own. A kind without a handler (including a stored kind this build does not know)
    reads `Label · detail`.
    """
    kind = _event_kind(event)
    raw = event.get("data")
    data = raw if isinstance(raw, dict) else {}
    task_id = event.get("task_id") if _known(event.get("task_id")) else data.get("task_id")
    if _known(event.get("task_ordinal")):
        task_id = event["task_ordinal"]
    elif numbers and _known(task_id):
        task_id = numbers.get(str(task_id), task_id)
    model_value = data.get("model") if _known(data.get("model")) else data.get("selected_model")
    context = _EventContext(
        kind=kind,
        data=data,
        event=event,
        task=f"Task {_text(task_id, 12)}" if _known(task_id) else "Task",
        on_task=f" on task {_text(task_id, 12)}" if _known(task_id) else "",
        reason=f" ({_text(plain_reason(data['reason']), 60)})" if _known(data.get("reason")) else "",
        owner=_text(data.get("route_id") or data.get("route") or data.get("provider") or data.get("station") or "", 40),
        model=_text(model_value, 40) if _known(model_value) else "",
        new_state=str(data.get("to") or "").upper(),
        old_state=str(data.get("from") or "").upper(),
    )
    plumbing = bridge_events.is_plumbing(kind)
    handler = _HUMANIZERS.get(kind)
    if handler is None and kind.startswith("command-"):
        handler = _h_command
    elif handler is None and kind.startswith("steering-"):
        handler = _h_steering
    if handler is not None:
        result = handler(context)
        return result if isinstance(result, tuple) else (result, plumbing)
    detail = _event_summary(event)
    label = kind.replace("-", " ").capitalize()
    return (f"{label} · {detail}" if detail else label), plumbing


@dataclass(frozen=True)
class _ActivityEntry:
    clock: str
    text: str
    plumbing: bool
    kind: str = ""


def _activity_entries(events: list[dict[str, object]], numbers: dict[str, str] | None = None) -> list[_ActivityEntry]:
    """Chronological rows. Runs of heartbeat/refresh events collapse to one row per kind.

    A collapsed row sits at the time of the first event it replaces and reports
    the count and the time of the last one. Other events between runs split them.
    """
    entries: list[_ActivityEntry] = []
    index = 0
    while index < len(events):
        event = events[index]
        kind = _event_kind(event)
        if kind not in _COLLAPSED_EVENTS:
            text, plumbing = _humanize_event(event, numbers)
            entries.append(_ActivityEntry(_local_clock(event.get("created_at")), text, plumbing, kind))
            index += 1
            continue
        end = index
        while end + 1 < len(events) and _event_kind(events[end + 1]) in _COLLAPSED_EVENTS:
            end += 1
        groups: dict[str, list[dict[str, object]]] = {}
        for item in events[index:end + 1]:
            groups.setdefault(_event_kind(item), []).append(item)
        for group_kind, items in groups.items():
            base, _ = _humanize_event(items[-1], numbers)
            count = len(items)
            first, last = _local_clock(items[0].get("created_at")), _local_clock(items[-1].get("created_at"))
            if count > 1:
                noun = "heartbeats" if group_kind == "controller-heartbeat" else "times" if group_kind.endswith("error") else "refreshes"
                base += f" ({count} {noun}, last {last})"
            entries.append(_ActivityEntry(first, base, True, group_kind))
        index = end + 1
    return entries


def _active_owner(snapshot: dict[str, object]) -> str:
    """Name of the provider/route whose operation is running now, else empty."""
    context = _dict(snapshot.get("station_context"))
    if _known(context.get("executing_operation")) and str(context.get("operation_status") or "").upper() == "RUNNING":
        owner = context.get("last_launched_station")
        return _text(owner, 60) if _known(owner) else ""
    return ""


def _station_model(snapshot: dict[str, object], owner: str) -> str:
    """The model seen live for `owner`, else its configured model, else empty."""
    for station in _items(snapshot.get("stations")):
        if owner in {str(station.get("name") or ""), str(station.get("provider") or ""), str(station.get("id") or "")}:
            for key in ("observed_model", "model"):
                if _known(station.get(key)):
                    return _text(station[key], 40)
    return ""


def _owner_windows(snapshot: dict[str, object], owner: str) -> list[str]:
    """`['5h 95%', 'week 21%']` for the owner's measurable capacity windows."""
    selection = _dict(_dict(snapshot.get("capacity")).get("selection"))
    for row in _items(selection.get("decisions")):
        if _decision_owner(row) == owner:
            return [brief for brief in (_window_brief(window) for window in _items(row.get("windows"))) if brief]
    return []


def _next_task(snapshot: dict[str, object]) -> dict[str, object] | None:
    current = _dict(snapshot.get("current_task"))
    return next(
        (task for task in _items(snapshot.get("tasks"))
         if str(task.get("status", "")).upper() in {"READY", "QUEUED"} and task.get("id") != current.get("id")),
        None,
    )


def _owner_label(owner: str, model: str) -> str:
    """The route and model once: the model alone when it already names the route, else `route (model)`."""
    if not model or model == owner or owner in model:
        return model or owner
    return owner if model in owner else f"{owner} ({model})"


def _waiting_parts(waiting: dict[str, object]) -> list[str]:
    """Strip parts for a mission waiting on providers: why, which quota, until when."""
    from . import common

    subject = "capacity" if waiting.get("reason") == "capacity" else "a usable provider"
    blocked = []
    for entry in _items(waiting.get("entries"))[:2]:
        text = _text(entry.get("name"), 30)
        if _known(entry.get("window")):
            text += f" {_text(entry['window'], 12)}"
            if isinstance(entry.get("remaining_percent"), (int, float)):
                text += f" {entry['remaining_percent']:g}%"
        blocked.append(text)
    until = waiting.get("until_epoch")
    resume = _relative_epoch(until, common.now_epoch()) if isinstance(until, (int, float)) else None
    return [part for part in (f"waiting for {subject}", ", ".join(blocked), f"resumes {resume}" if resume else "") if part]


def _status_strip(snapshot: dict[str, object], width: int = 0) -> tuple[str, str]:
    """(text, role) for line 1 of the control view: what is happening, most important first.

    Running: `codex gpt-5 · task 6 running 4m · no output 1m · 5h 95% left · week 21% left`.
    Otherwise the state, the next task and the command that moves it forward.
    Lower-value parts are dropped, then clipped, to fit `width` (0 = no limit).
    """
    if not snapshot:
        return "loading…", "dim"
    status = str(_mission(snapshot).get("status") or "").upper()
    if status in ENDED_MISSION_STATUSES:
        tasks = _items(snapshot.get("tasks"))
        done = sum(1 for task in tasks if str(task.get("status", "")).upper() == "COMPLETED")
        reason = _dict(snapshot.get("lifecycle")).get("failure_reason")
        if status == "FAILED" and _known(reason):
            return _fit_strip([f"mission failed — {_text(reason, 160)}"], width), "error"
        summary = f"mission {status.lower()}" + (f"{_SEP}{done}/{len(tasks)} tasks completed" if tasks else "")
        return _fit_strip([summary], width), "ok" if status == "COMPLETED" else "error"
    current = _dict(snapshot.get("current_task"))
    if str(current.get("status") or "").upper() == "RUNNING":
        owner = _active_owner(snapshot)
        model = _station_model(snapshot, owner) if owner else ""
        elapsed = _dict(snapshot.get("execution")).get("operation_elapsed_seconds")
        seconds = _int_or_none(elapsed)
        task = f"task {_text(task_number(current) if task_number(current) is not None else '?', 12)} running" + (
            "" if seconds is None else f" {max(0, seconds)}s" if seconds < 60 else f" {_span(seconds, 1)}"
        )
        idle = _idle_phrase(snapshot).replace("no output for ", "no output ")
        windows = _owner_windows(snapshot, owner) if owner else []
        variants = []
        for with_model, with_left, with_capacity, with_idle in (
            (True, True, True, True), (False, True, True, True), (False, False, True, True),
            (False, False, False, True), (False, False, False, False),
        ):
            who = _owner_label(owner, model if with_model else "")
            capacity = [f"{item} left" if with_left else item for item in windows] if with_capacity else []
            variants.append([part for part in (who, task, idle if with_idle else "", *capacity) if part])
        text = _fit_strip_variants(variants, width)
        return text, "warn" if idle else "ok"
    blocked_on = _dict(snapshot.get("waiting"))
    if blocked_on and status == "RUNNING":
        return _fit_strip(_waiting_parts(blocked_on), width), "warn"
    waiting = [task for task in _items(snapshot.get("tasks")) if str(task.get("status", "")).upper() == "VERIFYING"]
    if waiting and status != "CREATED":
        number = _text(task_number(waiting[0]) if task_number(waiting[0]) is not None else "?", 12)
        more = f" (+{len(waiting) - 1} more)" if len(waiting) > 1 else ""
        held = _review_hold_task(snapshot)
        if held is not None:
            return _fit_strip_variants([
                [f"task {held} awaits your review{more}", f"/approve {held}", f"/reject {held} reason", "/continue"],
                [f"task {held} awaits your review", f"/approve {held}", "/continue"],
                [f"task {held} awaits your review", "/continue"],
            ], width), "warn"
        return _fit_strip([f"task {number} awaits approval{more}", f"type /approve {number}"], width), "warn"
    following = _next_task(snapshot)
    upcoming = f"next: task {_text(task_number(following) if task_number(following) is not None else '?', 12)}" if following else "no task waiting · Ctrl-E adds one"
    if status == "STOPPING":
        return _fit_strip(["stopping after the current step"], width), "warn"
    if status in {"CREATED", "STOPPED"}:
        label = "not started" if status == "CREATED" else "stopped"
        return _fit_strip([label, upcoming, "type /start"] if following else [label, upcoming], width), "dim"
    if status == "PAUSED":
        return _fit_strip(["paused", upcoming, "type /resume"] if following else ["paused", upcoming], width), "warn"
    wait = _dict(_dict(snapshot.get("capacity")).get("wait"))
    if wait:
        until = _relative_epoch(wait.get("target_epoch"))
        return _fit_strip(["waiting for capacity"] + ([f"resumes {until}"] if until else []), width), "warn"
    return _fit_strip(["idle", upcoming], width), "dim"


def _fit_strip(parts: list[str], width: int) -> str:
    return _fit_strip_variants([parts], width)


def _fit_strip_variants(variants: list[list[str]], width: int) -> str:
    """The first variant that fits `width`; otherwise the last one, clipped."""
    for parts in variants:
        text = _SEP.join(parts)
        if width <= 0 or cell_width(text) <= width:
            return text
    return _clip(_SEP.join(variants[-1]), width)


_PALETTE_NOTE = "No actions match; / edits the filter, Backspace clears it."

_USAGE_HEADER = re.compile(r"^Provider\s{2,}\S")
# Columns dropped, in this order, before the table has to be panned.
_USAGE_OPTIONAL_COLUMNS = ("Source", "Remaining Time")


def _split_usage_text(lines: list[str]) -> tuple[list[str] | None, list[str]]:
    """(table lines, legend lines) of the llm-usage dashboard text; the table is None if it has no header row.

    The dashboard prints a title and a legend before the table. The table comes
    first in the view, so the legend moves behind a toggle.
    """
    for index, line in enumerate(lines):
        if _USAGE_HEADER.match(line):
            legend = [item for item in lines[1:index] if item.strip()]
            table = list(lines[index:])
            while table and not table[-1].strip():
                table.pop()
            return table, legend
    return None, []


def _fit_usage_table(table: list[str], width: int) -> list[str]:
    """The table within `width` cells when dropping the optional columns (Source, then Remaining Time) is enough.

    Columns are cut by their start positions in the header row, so every row
    stays aligned. With `width` 0, or when the table still does not fit, the
    remaining columns are kept whole and the view pans.
    """
    lines = list(table)

    def widest() -> int:
        return max((cell_width(line.rstrip()) for line in lines), default=0)

    if width > 0 and widest() > width:
        for name in _USAGE_OPTIONAL_COLUMNS:
            columns = [(match.start(), match.group()) for match in re.finditer(r"\S+(?: \S+)*", lines[0])]
            names = [label for _start, label in columns]
            if name not in names:
                continue
            position = names.index(name)
            begin = columns[position][0]
            end = columns[position + 1][0] if position + 1 < len(columns) else None
            lines = [line[:begin] + (line[end:] if end is not None else "") for line in lines]
            if widest() <= width:
                break
        if len(lines) > 1 and lines[1].strip() and set(lines[1].strip()) <= {"─", "-", " "}:
            lines[1] = lines[1][: len(lines[0].rstrip())]  # the rule ends where the header text ends
    return [line.rstrip() for line in lines]


def _nav_labels(view_index: int, numbered: bool = True) -> list[str]:
    """Tab labels; the active one in brackets. Without numbers the F-key order still applies."""
    return [
        f"[{index + 1} {name}]" if index == view_index and numbered else f"[{name}]" if index == view_index
        else f"{index + 1} {name}" if numbered else name
        for index, name in enumerate(VIEWS)
    ]


def _nav_line(view_index: int, separator: str) -> str:
    return separator.join(_nav_labels(view_index))


def _nav_index_at(x: int, view_index: int, width: int, separator: str) -> int:
    _, entries = _nav_layout(view_index, width, separator)
    return min(entries, key=lambda item: min(abs(x-item[1]), abs(x-item[1]-len(item[2])+1)))[0] if entries else view_index


def _nav_layout(view_index: int, width: int, separator: str) -> tuple[str, list[tuple[int, int, str]]]:
    """The tab row and the (view, column, label) of each visible tab.

    Tabs are always at least two spaces apart, so neighbours never read as one
    label. The numbered form needs about 86 columns. Narrower rows drop the
    numbers (F1-F8 and Tab still follow the same order); below about 72 columns
    the row scrolls around the active tab and shows `<` / `>` for hidden tabs.
    """
    gap = separator if len(separator) >= 2 else "  "
    for numbered in (True, False):
        labels = _nav_labels(view_index, numbered)
        if cell_width(gap.join(labels)) <= width:
            entries: list[tuple[int, int, str]] = []
            text = ""
            for index, label in enumerate(labels):
                if entries:
                    text += gap
                entries.append((index, len(text), label))
                text += label
            return text, entries
    labels = _nav_labels(view_index, False)

    def render(first: int) -> tuple[str, list[tuple[int, int, str]]]:
        text = "< " if first else ""
        shown: list[tuple[int, int, str]] = []
        for index in range(first, len(labels)):
            label = labels[index]
            reserve = 2 if index < len(labels) - 1 else 0  # room for the closing " >"
            if shown and len(text) + len(gap) + len(label) + reserve > width:
                text += " >"
                break
            if shown:
                text += gap
            shown.append((index, len(text), label))
            text += label
        return text, shown

    for first in range(view_index + 1):
        text, shown = render(first)
        if any(index == view_index for index, _offset, _label in shown):
            return text, shown
    return render(view_index)


def _waiting_on(task: dict[str, object], tasks: list[dict[str, object]], status: dict[str, str] | None = None) -> str:
    """`after 6, 8` for prerequisites that are not completed (unknown ones count as waiting).

    ``status`` is the id-to-status map of ``tasks``; callers rendering many rows build it once.
    """
    dependencies = task.get("depends_on")
    if not isinstance(dependencies, list) or not dependencies:
        return ""
    if status is None:
        status = {str(item.get("id")): str(item.get("status", "")).upper() for item in tasks}
    pending = [_text(item, 12) for item in dependencies if status.get(str(item)) != "COMPLETED"]
    return "after " + ", ".join(pending) if pending else ""


def _order_list_segments(tasks: list[dict[str, object]]) -> list[list[Segment]]:
    """`id  ● STATE  P5  after 6  title` as segments, with the columns aligned.

    The id, priority and dependencies are secondary (`muted`); the state is the
    one coloured element; the title stays in the default foreground. Rows of
    finished tasks recede: everything but the state word is `muted`.
    """
    ids = [_text(task_number(task) if task_number(task) is not None else "?", 40) for task in tasks]
    states = [_text(task.get("status", "unknown"), 24).upper() for task in tasks]
    shown = [_state_text(state) for state in states]
    priorities = [f"P{_text(task['priority'], 8)}" if _known(task.get("priority")) else "" for task in tasks]
    task_status = {str(item.get("id")): str(item.get("status", "")).upper() for item in tasks}
    waits = [_waiting_on(task, tasks, task_status) for task in tasks]
    id_width = max((len(item) for item in ids), default=0)
    state_width = max((len(item) for item in shown), default=0)
    priority_width = max((len(item) for item in priorities), default=0)
    wait_width = max((len(item) for item in waits), default=0)
    rows: list[list[Segment]] = []
    for task, identifier, state, text, priority, wait in zip(tasks, ids, states, shown, priorities, waits):
        title = _cut_text(task.get("title") or task.get("prompt") or "Untitled task", 90)
        finished = state in _FINISHED_STATES
        look = _state_look(state)
        row: list[Segment] = [
            (f"{identifier:<{id_width}}", "muted"), ("  ", "info"),
            (f"{text:<{state_width}}", look[1] if look else "muted" if finished else "info"),
        ]
        if priority_width:
            row += [("  ", "info"), (f"{priority:<{priority_width}}", "muted")]
        if wait_width:
            row += [("  ", "info"), (f"{wait:<{wait_width}}", "muted")]
        row += [("  ", "info"), (title, "muted" if finished else "info")]
        rows.append(row)
    return rows


def _order_list_rows(tasks: list[dict[str, object]]) -> list[str]:
    """`id  ● STATE  P5  after 6  title` with the id, state, priority and dependency columns aligned."""
    return [_segments_text(row) for row in _order_list_segments(tasks)]


_ORDERS_MIN_DETAIL = 8
_ORDERS_MIN_WIDTH = 60


def _orders_list_height(content_height: int, width: int, count: int) -> int | None:
    """Rows for the Orders list when a live preview fits below it, else None.

    The list takes the rows it needs, but always leaves the preview at least
    `_ORDERS_MIN_DETAIL` rows plus the separator line.
    """
    if count <= 0 or width < _ORDERS_MIN_WIDTH:
        return None
    list_height = min(count, content_height - _ORDERS_MIN_DETAIL - 1)
    return list_height if list_height >= min(count, 3) else None


_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")


_REFLOW_CACHE: dict[tuple[str, int, int, str], tuple[tuple[str, str], ...]] = {}


def _reflow_markdown(text: str, width: int, limit: int) -> list[tuple[str, str]]:
    """`_reflow_markdown_rows`, remembered for the latest few (text, width, limit) because a frame repeats the call."""
    key = (text, width, limit, glyph("ellipsis"))  # the ellipsis follows the locale, which changes the clipped rows
    rows = _REFLOW_CACHE.get(key)
    if rows is None:
        if len(_REFLOW_CACHE) >= 16:
            _REFLOW_CACHE.pop(next(iter(_REFLOW_CACHE)))
        rows = _REFLOW_CACHE[key] = tuple(_reflow_markdown_rows(text, width, limit))
    return list(rows)


def _fit_cells(row: str, width: int, hang: str) -> list[str]:
    """Split a row wider than `width` cells at a cell boundary; `textwrap` counts characters, so wide text overflows it."""
    if cell_width(row) <= width:
        return [row]
    pieces: list[str] = []
    current, used = "", 0
    for char in row:
        amount = cell_width(char)
        if used and used + amount > width:
            pieces.append(current)
            current, used = hang, cell_width(hang)
        current += char
        used += amount
    pieces.append(current)
    return pieces


def _reflow_markdown_rows(text: str, width: int, limit: int) -> list[tuple[str, str]]:
    """Reflow Markdown into at most `limit` (line, role) rows of `width` cells.

    Consecutive plain lines join into one paragraph and re-wrap. List items
    keep their marker and wrap with a hanging indent. Headings lose their `#`
    markers and use the heading role. Fenced code and table rows are never
    re-wrapped; they are clipped instead. Blank lines only separate blocks.
    More than `limit` rows ends in an ellipsis.
    """
    width = max(8, width)
    out: list[tuple[str, str]] = []
    paragraph: list[str] = []
    item_prefix = ""

    def flush() -> None:
        nonlocal item_prefix
        if paragraph:
            joined = " ".join(paragraph)
            hang = " " * len(item_prefix)
            wrapped = textwrap.wrap(
                joined, width=width, initial_indent=item_prefix, subsequent_indent=hang,
                break_long_words=True, break_on_hyphens=False,
            ) or [item_prefix + joined]
            out.extend((piece, "info") for line in wrapped for piece in _fit_cells(line, width, hang))
        paragraph.clear()
        item_prefix = ""

    in_fence = False
    for raw in text.splitlines():
        line = raw.expandtabs(4).rstrip()
        if _FENCE.match(line):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            out.append((_clip(line, width), "secondary"))
            continue
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith("|"):
            flush()
            out.append((_clip(stripped, width), "secondary"))
            continue
        heading = heading_label(line)
        if heading is not None:
            flush()
            out.append((_clip(heading, width), "heading"))
            continue
        item = _LIST_ITEM.match(line)
        if item:
            flush()
            room = max(2, width // 2)  # at least half the row stays for the item's text
            marker = item.group(2) + " "
            item_prefix = (" " * min(len(item.group(1)), 6, max(0, room - len(marker))) + marker)[:room]
            paragraph.append(item.group(3).strip())
            continue
        paragraph.append(stripped)
    flush()
    if len(out) > limit:
        out = out[:limit]
        if out:
            out[-1] = (_clip(out[-1][0] + "…", width), out[-1][1])
    return out


def _evidence_text(item: object) -> str:
    """One evidence item as text: its `text` when it is a record, else `key=value` words, else the text itself."""
    value = item if isinstance(item, dict) else None
    if value is None:
        text = _cut_text(item, 240)
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except ValueError:
                try:
                    parsed = ast.literal_eval(text)
                except Exception:  # any parser failure (MemoryError, RecursionError, TypeError, ...) leaves the text as is
                    return text
            value = parsed if isinstance(parsed, dict) else None
        if value is None:
            return text
    recorded = value.get("text")
    if isinstance(recorded, str) and recorded.strip():
        return _cut_text(recorded, 240)
    return " ".join(_compact_pairs(value, limit=6)) or _cut_text(item, 240)


def _order_separator_segments(task: dict[str, object], width: int) -> list[Segment]:
    """`── task 6 · ● RUNNING · priority 5 ────`: rule in `dim`, state in its own colour."""
    rule = glyph("hbar")
    sep = (_SEP, "dim")
    status = _text(task.get("status", "unknown"), 24)
    parts: list[Segment] = [
        (f"{rule}{rule} ", "dim"), (f"task {_text(task_number(task) if task_number(task) is not None else '?', 12)}", "muted"), sep, _state_segment(status),
    ]
    if _known(task.get("priority")):
        parts += [sep, (f"priority {_text(task['priority'], 8)}", "muted")]
    parts.append((" ", "dim"))
    used = _segments_width(parts)
    parts.append((rule * max(2, width - used), "dim"))
    return _clip_segments(parts, width, mark=False)


def _order_separator(task: dict[str, object], width: int) -> str:
    return _segments_text(_order_separator_segments(task, width))


def _order_preview_lines(task: dict[str, object], width: int, height: int) -> list[tuple[str, str, str]]:
    """(label, value, value role) rows previewing one task, at most `height` rows.

    The title is not repeated: the list row above and the separator name the task.
    """
    if height <= 0:
        return []
    room = max(10, width - _BRIDGE_LABEL)
    rows: list[tuple[str, str, str]] = []
    if _known(task.get("revision")):
        rows.append(("Revision", _text(task["revision"], 16), "info"))
    dependencies = task.get("depends_on")
    if isinstance(dependencies, list) and dependencies:
        rows.append(("Depends on", _clip(", ".join(_text(item, 12) for item in dependencies), room), "info"))
    raw_acceptance = task.get("acceptance")
    acceptance = [item for item in raw_acceptance if _known(item)] if isinstance(raw_acceptance, list) else []
    if acceptance:
        shown = acceptance[:5]
        for index, item in enumerate(shown):
            rows.append(("Acceptance" if index == 0 else "", _clip(f"{index + 1}. {_cut_text(item, 240)}", room), "info"))
        if len(acceptance) > len(shown):
            rows.append(("", f"+{len(acceptance) - len(shown)} more", "info"))
    else:
        rows.append(("Acceptance", "none recorded", "info"))
    raw_evidence = task.get("evidence")
    evidence = [item for item in raw_evidence if _known(item)] if isinstance(raw_evidence, list) else []
    if evidence:
        count = f"{len(evidence)} item{'s' if len(evidence) != 1 else ''}"
        rows.append(("Evidence", _clip(f"{count}{_SEP}latest: {_evidence_text(evidence[-1])}", room), "info"))
    else:
        rows.append(("Evidence", "none yet", "info"))
    prompt = task.get("prompt")
    if _known(prompt) and height > len(rows):
        # The prompt gets every row that is left; the fixed fields above win when space is short.
        for index, (line, role) in enumerate(_reflow_markdown(str(prompt), room, height - len(rows))):
            rows.append(("Prompt" if index == 0 else "", line, role))
    return rows[:height]


def _order_preview_rows(task: dict[str, object], width: int, height: int) -> list[str]:
    """Plain-text form of `_order_preview_lines`: label column plus value."""
    return [f"{label:<{_BRIDGE_LABEL}}{value}" for label, value, _role in _order_preview_lines(task, width, height)]


def _recent_event_rows(events: list[dict[str, object]], width: int, limit: int, numbers: dict[str, str] | None = None) -> list[str]:
    """Newest-last `HH:MM:SS  what happened` lines of operator-level events for the Overview."""
    if limit <= 0 or not events:
        return []
    entries = [entry for entry in _activity_entries(events, numbers) if not entry.plumbing][-limit:]
    rows = []
    for index, entry in enumerate(entries):
        text = f"{entry.clock}  {entry.text}".rstrip()
        rows.append(f"{'Recent' if index == 0 else '':<{_BRIDGE_LABEL}}" + _clip(text, max(10, width - _BRIDGE_LABEL)))
    return rows


def _provider_state(
    name: str,
    active: str,
    current: dict[str, object],
    elapsed: object,
    decision: dict[str, object] | None,
    now: float,
    usage_rows: list[dict[str, object]] | None = None,
) -> tuple[str, str]:
    """(live state text, role) for one provider block header.

    Before any capacity decision exists, the embedded usage dashboard's rows
    (`usage_rows`) tell whether the provider has capacity left.
    """
    if name and name == active:
        text = "running"
        if current:
            text += f" task {_text(task_number(current) if task_number(current) is not None else '?', 12)}"
            if _int_or_none(elapsed) is not None:
                text += f" {_span(elapsed, 1)}"
        return text, "ok"
    if decision is None and usage_rows:
        percents = [_percent_value(row.get("remaining_percent")) for row in usage_rows]
        empty = any(value is not None and value <= 0 for value in percents)
        return ("not ready", "warn") if empty else ("ready", "ok")
    if decision is None:
        return "capacity not observed", "dim"
    usable = str(decision.get("usable")).lower()
    if usable == "true":
        return "ready", "ok"
    if usable != "false":
        return "state unknown", "dim"
    from . import common

    resets = []
    for window in _items(decision.get("windows")):
        try:
            exhausted = float(str(window.get("remaining_percent"))) <= 0
        except ValueError:
            exhausted = False
        reset = common.parse_epoch(window.get("reset_epoch"), use_date=False)
        if exhausted and reset is not None and reset > now:
            resets.append(reset)
    if resets:
        return f"rate-limited, resets {_relative_epoch(max(resets), now)}", "warn"
    reason = f" ({_text(decision['reason'], 60)})" if _known(decision.get("reason")) else ""
    return f"not ready{reason}", "warn"


_CLOCK_ROW = re.compile(r"^(?P<clock>[\d-]{2}:[\d-]{2}:[\d-]{2})  (?P<text>.*)$")


def _activity_row_segments(row: str, role: str) -> list[Segment]:
    """Clock `muted`; plumbing `dim`; operator events in the default colour; failures `error`."""
    match = _CLOCK_ROW.match(row)
    text_role = "error" + QUIET if role == "error" else "dim" if role == "dim" else "info"
    if not match:
        return [(row, text_role)]
    return [
        (match.group("clock"), "dim" if role == "dim" else "muted"), ("  ", "info"),
        *([(match.group("text"), text_role)] if text_role != "info" else _inline_segments(match.group("text"), "info")),
    ]


def _activity_role(text: str) -> str:
    """Role for an operator-level Activity row: problems stand out."""
    lowered = text.lower()
    if any(word in lowered for word in ("failed", "unknown", "out of handoffs", "unexpectedly", "rejected", "aborted")):
        return "error"
    if lowered.startswith("provider exited (") and not lowered.startswith("provider exited (0)"):
        return "error"
    if any(word in lowered for word in ("awaiting approval", "waiting for capacity", "paused", "blocked")):
        return "warn"
    return "info"


def _provider_header_segments(name: str, model: str, state: str, role: str, flags: list[str]) -> list[Segment]:
    """`■ codex  model gpt-5  ● running task 2 4m  [viewed]`: provider square, name, model, live state."""
    state_role = {"ok": "state_running", "dim": "muted"}.get(role, role)
    glyph_name = ""
    if state.startswith("running"):
        glyph_name = "running"
    elif state == "ready":
        glyph_name, state_role = "waiting", "state_waiting"
    elif state.startswith("rate-limited"):
        state_role = "capacity_empty"
    state_text = f"{glyph(glyph_name)} {state}" if glyph_name else state
    out: list[Segment] = [(glyph("square"), _provider_role(name)), (" " + name, "info"), ("  " + model, "muted"), ("  ", "info")]
    if state.startswith("rate-limited"):  # only the word is capacity-coloured; the reset time is a muted fact
        out += [("rate-limited", "capacity_empty"), (state[len("rate-limited"):], "muted")]
    else:
        out.append((state_text, state_role))
    for flag in flags:
        out += [("  ", "info"), (flag, "accent" if flag == "[viewed]" else "muted")]
    return out


def _recent_operation_segments(op: dict[str, object], age: str | None) -> list[Segment]:
    """`  recent: task 3 · succeeded · exit 0 · 2m ago`: plumbing in `dim`, a failure in `error`."""
    status = _text(op.get("status") or "-", 20).lower()
    exit_code = _text(op.get("exit_code"), 16) if op.get("exit_code") is not None else "-"
    failed = status in {"failed", "error"} or exit_code not in {"-", "0"}
    return [
        (f"  recent: task {_text(op.get('task_ordinal') or op.get('task_id') or '-', 12)}{_SEP}", "dim"),
        (status, "error" + QUIET if failed else "dim"),
        (_SEP, "dim"), (f"exit {exit_code}", "error" + QUIET if exit_code not in {"-", "0"} else "dim"),
        *([(f"{_SEP}{age}", "dim")] if age else []),
    ]


# Snapshot fields holding "seconds since X" computed when the snapshot was read. Between reads the
# console advances them itself, so "heartbeat 3s ago" keeps counting without re-reading the database.
_AGE_PATHS: tuple[tuple[str, ...], ...] = (
    ("lifecycle", "heartbeat", "age_seconds"),
    ("usage", "age_seconds"),
    ("capacity", "source_age_seconds"),
    ("execution", "seconds_since_verified_progress"),
    ("execution", "operation_elapsed_seconds"),
)
_RUNNING_AGE_PATH = ("execution", "operation_elapsed_seconds")  # grows only while an operation executes


def _age_paths_for(snapshot: dict[str, object]) -> list[tuple[str, ...]]:
    """The age fields present in ``snapshot`` that advance with time."""
    running = bool(_dict(snapshot.get("station_context")).get("executing_operation"))
    found: list[tuple[str, ...]] = []
    for path in _AGE_PATHS:
        if path == _RUNNING_AGE_PATH and not running:
            continue
        node: object = snapshot
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if _int_or_none(node) is not None and not isinstance(node, bool):
            found.append(path)
    return found


def _aged_snapshot(base: dict[str, object], seconds: int, paths: list[tuple[str, ...]] | None = None) -> dict[str, object]:
    """A copy of ``base`` with its age fields advanced by ``seconds``; ``base`` is never modified."""
    result = dict(base)
    if seconds <= 0:
        return result
    for path in _age_paths_for(base) if paths is None else paths:
        node = result
        for key in path[:-1]:
            node[key] = child = dict(_dict(node.get(key)))
            node = child
        value = node.get(path[-1])
        advanced = _int_or_none(value)
        if advanced is not None and not isinstance(value, bool):
            # The bounded projection keeps scalars as text; keep whichever type the reader supplied.
            node[path[-1]] = str(advanced + seconds) if isinstance(value, str) else advanced + seconds
    return result


@dataclass
class _ConsoleState:
    mission_id: str
    dispatch: Dispatch
    snapshot_reader: Snapshot
    full: bool
    view_index: int = 0
    selected: int = 0
    selected_task_anchor: str | None = None
    palette_filter: str = ""
    palette_filter_editing: bool = False
    palette_slash_pending: bool = False
    task_offset: int = 0
    event_offset: int = 0
    event_anchor: int | None = None
    command: str = ""
    command_cursor: int = 0
    input_focused: bool = False
    command_draft: str | None = None
    command_draft_cursor: int = 0
    search_draft: str = ""
    search_draft_cursor: int = 0
    search_was_focused: bool = False
    search_query: str = ""
    search_editing: bool = False
    completion_index: int = 0
    completion_prefix: str = ""
    completion_candidates: list[str] = field(default_factory=list)
    task_detail: bool = False
    detail_scroll: int = 0
    history: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    history_index: int | None = None
    output: deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_OUTPUT_LINES))
    snapshot: dict[str, object] = field(default_factory=dict)
    snapshot_error: str = ""
    last_refresh: float = 0.0
    _result_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _snapshot_thread: threading.Thread | None = field(default=None, repr=False)
    _snapshot_result: tuple[dict[str, object] | None, str] | None = field(default=None, repr=False)
    _command_result: _CommandResult | TerminalAction | ControlViewAction | None = field(default=None, repr=False)
    terminal_action: TerminalAction | ControlViewAction | None = field(default=None, repr=False)
    command_pending: bool = False
    working_since: float = 0.0
    hide_plumbing: bool = False
    _scroll_limit: int = field(default=0, repr=False)
    palette_quick: bool = False
    pending_cleanup_path: Path | None = None
    pending_cleanup_paths: tuple[Path, ...] = ()
    submitted_draft: str | None = None
    rejected_draft_path: Path | None = None
    rejected_draft_paths: tuple[Path, ...] = ()
    rejected_submission: str | None = None
    expand_requested: bool = False
    usage_hscroll: int = 0
    usage_legend: bool = False
    saved_notice: bool = False  # show "saved as ID" in the status strip until the first key
    usage_reader: Callable[[], dict[str, object]] | None = field(default=None, repr=False)
    content_width: int = 0  # columns available to view content; set by the last draw (0 = unknown)
    response_overlay: bool = False
    confirm_command: str | None = None
    _status_dismissed: tuple[int, str] | None = field(default=None, repr=False)
    _status_view: int | None = field(default=None, repr=False)
    _status_key: tuple[int, str] | None = field(default=None, repr=False)
    _status_since: float = field(default=0.0, repr=False)
    status_clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    response_scroll: int = 0
    response_page_size: int = 10
    document_viewer: DocumentViewer | None = None
    document_search_editing: bool = False
    document_export_editing: bool = False
    document_notice: str = ""
    document_previous_query: str = ""
    document_entry: str = ""
    document_heading_list: bool = False
    document_heading_selected: int = 0
    document_pending: bool = False
    document_task_id: str | None = None
    edit_document_requested: bool = False
    document_edit_pending: bool = False
    edit_initial_text: str = ""
    paste_source: str | None = None
    _document_result: tuple[str, str, str | None, str] | None = field(default=None, repr=False)
    _document_generation: int = 0
    activity_reader: Callable[[str], tuple[int, str] | None] | None = field(default=None, repr=False)
    wall_clock: Callable[[], float] = field(default=time.time, repr=False)
    _activity_thread: threading.Thread | None = field(default=None, repr=False)
    _activity_result: tuple[tuple[int, str] | None] | None = field(default=None, repr=False)
    _activity_polled: float | None = field(default=None, repr=False)
    _activity_digest: str | None = field(default=None, repr=False)
    _activity_epoch: float | None = field(default=None, repr=False)
    _draw_cache: dict[str, object] | None = field(default=None, repr=False)  # per-frame memo; only set while drawing
    change_probe: Callable[[], object] | None = field(default=None, repr=False)  # cheap "did the database change?" reading
    dirty: bool = field(default=True, repr=False)  # something drawn may have changed since the last frame
    _last_input: float | None = field(default=None, repr=False)
    _snapshot_base: dict[str, object] | None = field(default=None, repr=False)  # the last read, unmodified
    _snapshot_taken: float = field(default=0.0, repr=False)  # monotonic time that read started
    _age_paths: list[tuple[str, ...]] = field(default_factory=list, repr=False)
    _aged_ref: dict[str, object] | None = field(default=None, repr=False)  # identity check: nobody replaced `snapshot`
    _aged_steps: int = field(default=0, repr=False)
    _snapshot_token: object = field(default=None, repr=False)  # change token of the last read
    _pending_token: object = field(default=None, repr=False)
    _pending_started: float = field(default=0.0, repr=False)
    _idle_phrase_seen: str = field(default="", repr=False)

    @property
    def view(self) -> str:
        return VIEWS[self.view_index]

    def _observe_output_activity(self, current: float) -> None:
        """Poll provider-pane activity in the background and publish the idle time in the snapshot."""
        if not self._activity_wanted():
            with self._result_lock:
                self._activity_result = None
            self._activity_digest = None
            self._activity_epoch = None
            self._activity_polled = None
            self.snapshot.pop("output_idle_seconds", None)
            self._note_idle_phrase()
            return
        with self._result_lock:
            result, self._activity_result = self._activity_result, None
            thread = self._activity_thread
        if result is not None and result[0] is None:
            self._activity_digest = None
            self._activity_epoch = None
        elif result is not None and result[0] is not None:
            epoch, digest = result[0]
            # Window activity also moves when the Bridge pane redraws; only provider screen changes count.
            if self._activity_digest is None or digest != self._activity_digest or self._activity_epoch is None:
                self._activity_epoch = float(epoch)
            self._activity_digest = digest
        # The worker clears _activity_thread when it publishes, so a finished read never delays the next poll.
        if (self._activity_polled is None or current - self._activity_polled >= _ACTIVITY_POLL_SECONDS) and thread is None:
            self._activity_polled = current
            reader = self.activity_reader or _read_provider_activity
            mission_id = self.mission_id

            def read_activity() -> None:
                try:
                    value = reader(mission_id)
                except Exception:
                    value = None
                with self._result_lock:
                    self._activity_result = (value,)
                    self._activity_thread = None

            worker = threading.Thread(target=read_activity, name="llm-bridge-activity", daemon=True)
            with self._result_lock:
                self._activity_thread = worker
            worker.start()
        if self._activity_epoch is None:
            self.snapshot.pop("output_idle_seconds", None)
        else:
            self.snapshot["output_idle_seconds"] = max(0, int(self.wall_clock() - self._activity_epoch))
        self._note_idle_phrase()

    def _activity_wanted(self) -> bool:
        """Provider-pane activity is observed only while a native provider operation executes."""
        if str(_dict(self.snapshot.get("current_task")).get("status") or "").upper() != "RUNNING":
            return False
        context = self.snapshot.get("station_context")
        # A projection that describes the station must show an executing operation; a bare one (older
        # projections, test doubles) keeps the task-status rule.
        return not isinstance(context, dict) or bool(context.get("executing_operation"))

    def _note_idle_phrase(self) -> None:
        """Repaint when the `no output for …` text changes."""
        phrase = _idle_phrase(self.snapshot)
        if phrase != self._idle_phrase_seen:
            self._idle_phrase_seen = phrase
            self.dirty = True

    def note_input(self, now: float | None = None) -> None:
        """An operator key or click: repaint, and return the data probe to its fast cadence."""
        self._last_input = time.monotonic() if now is None else now
        self.dirty = True

    def consume_dirty(self) -> bool:
        """Whether anything drawn may have changed since the last call."""
        changed, self.dirty = self.dirty, False
        return changed

    def probe_interval(self, current: float) -> float:
        if self._last_input is None:
            self._last_input = current
        return bridge_redraw.probe_interval(current, self._last_input, fast=_REFRESH_SECONDS, slow=_IDLE_REFRESH_SECONDS, after=_IDLE_BACKOFF_SECONDS)

    def wake_in(self, current: float) -> float | None:
        """Seconds until the console must look at its state again with no input: workers, probes, clocks."""
        waits: list[float] = []
        with self._result_lock:
            pending = self._snapshot_result is not None or self._command_result is not None or self._document_result is not None
            threads = (self._snapshot_thread, self._activity_thread)
        if pending:
            return 0.0
        if self.command_pending or self.document_pending or any(thread is not None and thread.is_alive() for thread in threads):
            waits.append(0.05)  # a worker is about to publish a result
        waits.append(max(0.0, self.last_refresh + self.probe_interval(current) - current) if self.last_refresh else 0.0)
        if self._activity_wanted():
            if self._activity_polled is not None:
                waits.append(max(0.0, self._activity_polled + _ACTIVITY_POLL_SECONDS - current))
            if self._activity_epoch is not None:
                elapsed = self.wall_clock() - self._activity_epoch
                if elapsed < _OUTPUT_IDLE_THRESHOLD_SECONDS:
                    waits.append(_OUTPUT_IDLE_THRESHOLD_SECONDS - elapsed)
                elif elapsed < 60:
                    waits.append(int(elapsed) + 1 - elapsed)
                else:
                    waits.append(60 * (elapsed // 60 + 1) - elapsed)
        return min(waits) if waits else None

    def age_origin(self) -> float | None:
        """Monotonic time the displayed snapshot's ages are counted from, or None when they do not advance."""
        return self._snapshot_taken if self._snapshot_base is not None and self.snapshot is self._aged_ref else None

    def _change_token(self) -> object:
        """Database change reading plus the projection parameters, or None when it cannot be taken."""
        if self.change_probe is None:
            return None
        reading = self.change_probe()
        if reading is None:
            return None
        return (reading, self.projection_params(), self.view)

    def projection_params(self) -> ProjectionParams:
        """The slice of the mission this console is showing: task page, event page and search text."""
        return ProjectionParams(
            task_limit=_TASK_PAGE_SIZE,
            task_offset=self.task_offset,
            search=self.search_query,
            event_offset=self.event_offset,
            event_anchor=self.event_anchor,
            prompt_chars=_MAX_DETAIL_TEXT + 1,  # the console previews prompts; `task show` has the full text
        )

    def refresh(self, now: float | None = None, *, force: bool = False) -> None:
        global LAST_RECOVERY_DRAFT
        current = time.monotonic() if now is None else now
        with self._result_lock:
            snapshot_result, self._snapshot_result = self._snapshot_result, None
            command_result, self._command_result = self._command_result, None
            document_result, self._document_result = self._document_result, None
            snapshot_thread = self._snapshot_thread
        if document_result is not None:
            self.dirty = True
            self.document_notice = ""
            task_id, revision, prompt, error = document_result
            self.document_pending = False
            self.document_task_id = None if task_id == "objective" else task_id
            if prompt is None:
                self.document_edit_pending = False
                self.output.append(error or "Full source unavailable; edit cancelled. Retry the full read.")
            elif self.document_edit_pending:
                self.edit_initial_text = prompt
                self.edit_document_requested = True
                self.document_edit_pending = False
            else:
                if self.document_viewer is not None:
                    self.document_viewer.release()
                title = f"{self.mission_id[:12]} · objective" if task_id == "objective" else f"{self.mission_id[:12]} · task {task_id} · r{revision} · prompt"
                self.document_viewer = DocumentViewer(title, prompt)
        if snapshot_result is not None:
            data, error = snapshot_result
            if data is not None:
                # The selection follows its task, not its row: an add, amend or
                # reorder changes which task sits at an index.
                anchor = self._selected_task_key() if self.view == "Tasks" and self.snapshot.get("tasks") else None
                previous = self._snapshot_base
                if previous is not None and self.snapshot is self._aged_ref:
                    previous = _aged_snapshot(previous, int(self._pending_started - self._snapshot_taken), self._age_paths)
                if previous is None or data != previous or self.snapshot_error or self.snapshot is not self._aged_ref:
                    self.dirty = True
                self._snapshot_base = data
                self._snapshot_taken = self._pending_started
                self._snapshot_token = self._pending_token
                self._age_paths = _age_paths_for(data)
                self.snapshot = _aged_snapshot(data, 0)
                self._aged_ref = self.snapshot
                self._aged_steps = 0
                self.snapshot_error = ""
                if anchor is not None:
                    self._reselect_task(anchor)
            else:
                if error != self.snapshot_error:
                    self.dirty = True
                self.snapshot_error = error
                self._snapshot_token = None
        if command_result is not None:
            self.dirty = True
        if isinstance(command_result, ControlViewAction) and command_result.mission_id == self.mission_id:
            self.choose_view(VIEWS.index(normalize_view(command_result.view)))
            self.expand_requested = True
            self.command_pending = False
            self.last_refresh = 0.0
        elif isinstance(command_result, (TerminalAction, ControlViewAction)):
            self.terminal_action = command_result
            self.command_pending = False
            self.last_refresh = 0.0
        elif command_result is not None:
            self.dirty = True
            result_text = command_result.text if isinstance(command_result, _CommandResult) else str(command_result)
            acknowledged = isinstance(command_result, _CommandResult) and command_result.outcome == "applied"
            failed = isinstance(command_result, _CommandResult) and (
                command_result.outcome in {"rejected", "failed"}
                or result_text.lower().startswith(("outcome unknown:", "error:"))
            )
            retained_paths: tuple[Path, ...] = ()
            cleanup_paths = self.pending_cleanup_paths or ((self.pending_cleanup_path,) if self.pending_cleanup_path is not None else ())
            if cleanup_paths:
                if not acknowledged:
                    self.rejected_draft_paths = cleanup_paths
                    self.rejected_draft_path = cleanup_paths[0]
                    retained_paths = cleanup_paths
                else:
                    for cleanup_path in cleanup_paths:
                        try:
                            cleanup_path.unlink(missing_ok=True)
                        except OSError as exc:
                            self.output.append(f"Could not remove accepted private draft: {_text(cleanup_path, 80)} · {_text(exc, 120)}")
                    if LAST_RECOVERY_DRAFT in cleanup_paths:
                        LAST_RECOVERY_DRAFT = None
                self.pending_cleanup_path = None
                self.pending_cleanup_paths = ()
            recall_hint = ""
            if failed and self.submitted_draft:
                # Like Codex CLI and Claude Code, the command line stays empty after a
                # failure; the failed command is the newest history entry (Up / Ctrl-P).
                self.rejected_submission = self.submitted_draft
                recall_hint = "↑ recalls the command"
            self.submitted_draft = None
            if result_text:
                response_lines = result_text.splitlines() or [result_text]
                if recall_hint:
                    if len(response_lines) > 1:
                        response_lines.append(recall_hint)
                    else:
                        response_lines[0] = f"{response_lines[0]}{_SEP}{recall_hint}"
                if len(response_lines) > 1:
                    self.output.clear()
                    self.output.extend(response_lines)
                    self.response_overlay = True
                    self.response_scroll = 0
                    self.expand_requested = True
                else:
                    self.output.append(response_lines[0])
            for retained_path in retained_paths:
                self.output.append(f"Draft retained for recovery: {retained_path}")
            if self.rejected_submission and isinstance(command_result, _CommandResult) and command_result.outcome == "unknown":
                self.output.append("Command not confirmed; it remains available in history and was not retried.")
            self.command_pending = False
            self.last_refresh = 0.0
        if self._age_paths and self.snapshot is self._aged_ref and self._snapshot_base is not None:
            # Between reads the console advances the "seconds ago" fields itself; the draw schedules
            # its own repaint for the moment a displayed value changes.
            steps = max(0, int(current - self._snapshot_taken))
            if steps != self._aged_steps:
                idle = self.snapshot.get("output_idle_seconds")
                self.snapshot = _aged_snapshot(self._snapshot_base, steps, self._age_paths)
                if idle is not None:
                    self.snapshot["output_idle_seconds"] = idle
                self._aged_ref = self.snapshot
                self._aged_steps = steps
        self._observe_output_activity(current)
        if not force and current - self.last_refresh < self.probe_interval(current):
            return
        token: object = None
        if self.change_probe is not None:
            token = self._change_token()
            if (
                not force
                and self.last_refresh != 0.0
                and token is not None
                and token == self._snapshot_token
                and self._snapshot_base is not None
                and current - self._pending_started < _FULL_REFRESH_SECONDS
            ):
                self.last_refresh = current  # nothing was committed since the last read
                return

        def read_snapshot() -> None:
            try:
                try:
                    parameters = inspect.signature(self.snapshot_reader).parameters.values()
                    parameter_list = tuple(parameters)
                    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameter_list)
                    parameter_names = {parameter.name for parameter in parameter_list}
                except (TypeError, ValueError):
                    accepts_kwargs = False
                    parameter_names = set()
                projection = {**self.projection_params().as_kwargs(), "view": self.view}
                kwargs = {
                    key: value
                    for key, value in projection.items()
                    if accepts_kwargs or key in parameter_names
                }
                data = self.snapshot_reader(self.mission_id, **kwargs)
                if not isinstance(data, dict):
                    raise TypeError("status snapshot must be a mapping")
                if self.usage_reader is not None and self.view in {"Providers", "Health"} and not data.get("usage"):
                    # Before a first run nothing else knows the capacity: show the Usage dashboard's numbers.
                    try:
                        data = {**data, "usage": self.usage_reader()}
                    except Exception:
                        pass  # advisory data; the view keeps its own wording
                result = (_bounded_snapshot(data), "")
            except Exception as exc:  # UI remains usable while a projection is unavailable.
                result = (None, _text(exc, 160) or type(exc).__name__)  # type: ignore[assignment]  # None marks a failed read, which the reader of `result` tests
            with self._result_lock:
                self._snapshot_result = result

        thread = threading.Thread(target=read_snapshot, name="llm-bridge-snapshot", daemon=True)
        with self._result_lock:
            if self._snapshot_result is not None:
                return
            if snapshot_thread is not None and snapshot_thread.is_alive():
                return
            self.last_refresh = current
            self._pending_started = current
            self._pending_token = token
            self._snapshot_thread = thread
        thread.start()

    def _dispatch(
        self,
        line: str,
        *,
        cleanup_path: Path | None = None,
        cleanup_paths: tuple[Path, ...] = (),
    ) -> bool:
        typed = line.strip()
        if not typed:
            return True
        text = _strip_slash(typed)
        try:
            help_words = shlex.split(text)
        except ValueError:
            help_words = []
        if help_words and help_words[0] in {"help", "?"} and "--help" not in help_words:
            self.open_help(" ".join(help_words[1:]) or "console")
            return True
        if text == "home":
            text = "missions"  # the home screen is the mission picker
        if text in _VIEW_COMMANDS:
            self.choose_view(VIEWS.index(_VIEW_COMMANDS[text]))
            self.expand_requested = True
            self.last_refresh = 0.0
            return True
        if self.command_pending:
            self.output.append("A command is still running; wait for its result before dispatching another.")
            return False
        self.history.append(typed)
        self.history_index = None
        self.command_pending = True
        paths = tuple(dict.fromkeys((*cleanup_paths, *((cleanup_path,) if cleanup_path is not None else ()))))
        self.pending_cleanup_paths = paths
        self.pending_cleanup_path = paths[0] if paths else None
        self.submitted_draft = typed
        self.dismiss_status()
        # The status row shows a `working…` indicator (see _working_text) while
        # command_pending; nothing is added to the history that could go stale.
        self.working_since = time.monotonic()

        def run_command() -> None:
            try:
                raw_result = self.dispatch(self.mission_id, text)
                if isinstance(raw_result, (TerminalAction, ControlViewAction)):
                    result = raw_result
                else:
                    raw_text = str(raw_result)
                    try:
                        envelope = json.loads(raw_text)
                    except (TypeError, json.JSONDecodeError):
                        envelope = None
                    status = envelope.get("status") if isinstance(envelope, dict) else None
                    outcome = {"APPLIED": "applied", "REJECTED": "rejected", "FAILED": "failed"}.get(status, "unknown")  # type: ignore[arg-type]  # status comes from a projected row typed Any | None
                    if raw_text.lstrip().lower().startswith("error:"):
                        outcome = "rejected"
                    result = _CommandResult(_command_feedback(text, raw_text), outcome)  # type: ignore[assignment]  # `result` is reused in this method with a different type
            except Exception as exc:
                result = _CommandResult(f"Outcome unknown: {_text(exc, 180)}", "unknown")  # type: ignore[assignment]  # `result` is reused in this method with a different type
            with self._result_lock:
                self._command_result = result
        thread = threading.Thread(target=run_command, name="llm-bridge-command", daemon=False)
        thread.start()
        return True

    def open_help(self, topic: str) -> None:
        """Show a help topic, or with `view:NAME` the short help of one view, in the document viewer."""
        from .bridge_help import topic_text, view_help

        by_view = topic.startswith("view:")
        text = view_help(topic[len("view:"):]) if by_view else topic_text(topic)
        self.close_document()
        self.document_task_id = None
        self.document_notice = ""
        self.document_viewer = DocumentViewer(f"Bridge help · {topic[len('view:'):] if by_view else topic}", text)

    def _confirm_or_dispatch(self, command: str) -> bool:
        command, problem = _expand_console_alias(command, self.snapshot)
        if problem is not None:
            self.output.append(problem)
            return False
        reason = _confirmation_reason(command)
        if reason:
            self.confirm_command = command
            self.output.clear()
            approving = _approval_task(command)
            lines = [f"{reason} · mission {_short_id(self.mission_id)}"]
            if approving is not None:
                lines += _approval_summary(self.snapshot, approving)
            self.output.extend([*lines, "Y approve · N or Esc cancel" if approving is not None else "Y confirm · N or Esc cancel"])
            self.response_overlay = True
            return True
        return self._dispatch(command)

    def handle_key(self, key: object) -> bool:
        """Apply one semantic key; return False when the console should exit."""
        if self.saved_notice and key not in {"KEY_RESIZE", "KEY_MOUSE"}:
            self.saved_notice = False
        if self.palette_filter_editing:
            slash_opened, self.palette_slash_pending = self.palette_slash_pending, False
            if slash_opened and not self.palette_filter and isinstance(key, str) and len(key) == 1 and key.isalpha():
                # `/reject 3 …`: a slash followed by a letter is a command, not a filter.
                self.palette_filter_editing = False
                self.palette_quick = False
                self.command = "/" + key
                self.command_cursor = len(self.command)
                self.input_focused = True
                self.history_index = None
                return True
            if self.palette_quick and key in {"KEY_UP", "KEY_DOWN"}:
                count = len(self.palette_actions())
                self.selected = min(max(0, count - 1), max(0, self.selected + (-1 if key == "KEY_UP" else 1)))
                return True
            if key == "\x1b":
                self.palette_filter_editing = False
                if self.palette_quick:
                    self.palette_filter = ""
                self.palette_quick = False
            elif key in {"\n", "\r", "KEY_ENTER"}:
                self.palette_filter_editing = False
                if self.palette_quick:
                    self.palette_quick = False
                    actions = self.palette_actions()
                    if actions:
                        self.activate_action(actions[min(max(0, self.selected), len(actions) - 1)])
                    self.palette_filter = ""
                    return True
            elif key in {"KEY_BACKSPACE", "\x7f", "\b"}:
                self.palette_filter = self.palette_filter[:-1]
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                self.palette_filter += key
            self.selected = 0
            return True
        if key == "\x0b" and self.document_viewer is None and not self.response_overlay:
            self.open_actions()  # Ctrl-K from anywhere in the console
            return True
        if self.view == "Actions" and not self.input_focused and key == "/":
            self.palette_filter_editing = True
            self.palette_slash_pending = True
            return True
        viewer = self.document_viewer
        if viewer is not None:
            if self.document_heading_list:
                headings = viewer.headings()
                if key in {"\x1b", "h"}:
                    self.document_heading_list = False
                elif key in {"KEY_UP", "k"}:
                    self.document_heading_selected = max(0, self.document_heading_selected - 1)
                elif key in {"KEY_DOWN", "j"}:
                    self.document_heading_selected = min(max(0, len(headings) - 1), self.document_heading_selected + 1)
                elif key in {"\n", "\r", "KEY_ENTER"} and headings:
                    viewer.top = headings[min(self.document_heading_selected, len(headings) - 1)][2]
                    viewer.anchor_active = False
                    self.document_heading_list = False
                return True
            if self.document_export_editing:
                if key in {"\x1b", "KEY_ESCAPE"}:
                    self.document_export_editing = False
                    self.document_entry = ""
                    return True
                if key in {"\n", "\r", "KEY_ENTER"}:
                    try:
                        target = Path(self.document_entry).expanduser()
                        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                            stream.write(viewer.source.encode("utf-8"))
                        self.output.append(f"Exported original source to {target}")
                    except FileExistsError:
                        self.output.append(f"Export target exists; choose a new path: {self.document_entry}")
                    except OSError as exc:
                        self.output.append(f"Could not export source: {_text(exc, 140)}")
                    self.document_notice = self.output[-1]
                    if not self.document_notice.startswith("Exported original"):
                        return True
                    self.document_export_editing = False
                    self.document_entry = ""
                    return True
                if key in {"KEY_BACKSPACE", "\x7f", "\b"}:
                    self.document_entry = self.document_entry[:-1]
                elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                    self.document_entry += key
                return True
            if self.document_search_editing:
                if key == "\x1b":
                    self.document_search_editing = False
                    viewer.query = self.document_previous_query
                    return True
                if key in {"\n", "\r", "KEY_ENTER"}:
                    viewer.search(viewer.query)
                    self.document_search_editing = False
                    return True
                if key in {"KEY_BACKSPACE", "\x7f", "\b"}:
                    viewer.query = viewer.query[:-1]
                elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                    viewer.query += key
                return True
            if key == "\x1b":
                self.close_document()
                return True
            if key in {"q", "KEY_ENTER", "\n", "\r"}:
                self.close_document()
            elif key in {"KEY_UP", "k"}:
                viewer.scroll(-1, 1)
            elif key in {"KEY_DOWN", "j"}:
                viewer.scroll(1, 1)
            elif key == "KEY_PPAGE":
                viewer.page(-1, viewer.viewport_height)
            elif key in {"KEY_NPAGE", " "}:
                viewer.page(1, viewer.viewport_height)
            elif key in {"KEY_HOME", "g"}:
                viewer.home()
            elif key in {"KEY_END", "G"}:
                viewer.end(viewer.viewport_height)
            elif key == "/":
                self.document_notice = ""
                self.document_search_editing = True
                self.document_previous_query = viewer.query
                viewer.query = ""
            elif key == "n":
                viewer.search(viewer.query)
            elif key == "N":
                viewer.search(viewer.query, reverse=True)
            elif key == "s":
                self.document_notice = ""
                self.document_export_editing = True
                self.document_entry = ""
            elif key == "c":
                argv = _clipboard_argv()
                if argv is None:
                    self.output.append("No supported local clipboard helper; use s to export original source.")
                else:
                    try:
                        subprocess.run(argv, input=viewer.source.encode("utf-8"), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, timeout=3, check=True)
                        self.output.append("Copied complete original source to the system clipboard.")
                    except (OSError, subprocess.SubprocessError) as exc:
                        self.output.append(f"Clipboard copy failed: {_text(exc, 120)}; use s to export source.")
                self.document_notice = self.output[-1]
            elif key == "e":
                if self.document_task_id is not None and str(_mission(self.snapshot).get("status", "")).upper() not in ENDED_MISSION_STATUSES:
                    self.edit_document_requested = True
                    self.close_document()
                else:
                    self.document_notice = "Task is not editable in a finished mission; export its source." if self.document_task_id is not None else "Objective is read-only; export its source to reuse it in a new mission."
                    self.output.append(self.document_notice)
            elif key == "m":
                viewer.toggle_rendered()
            elif key == "w":
                viewer.toggle_wrap()
            elif key == "h":
                self.document_heading_list = True
                self.document_heading_selected = min(self.document_heading_selected, max(0, len(viewer.headings()) - 1))
            elif key == "KEY_LEFT":
                viewer.horizontal = max(0, viewer.horizontal - 8)
            elif key == "KEY_RIGHT":
                viewer.horizontal += 8
            return True
        if self.response_overlay:
            if self.confirm_command is not None:
                if isinstance(key, str) and key.lower() == "y":
                    command, self.confirm_command = self.confirm_command, None
                    self.response_overlay = False
                    self._dispatch(command)
                elif isinstance(key, str) and key.lower() == "n" or key in {"\x1b", "KEY_ESCAPE"}:
                    self.confirm_command = None
                    self.response_overlay = False
                    self.output.append("Confirmation cancelled.")
                return True
            if key in {"\x1b", "\n", "\r", "KEY_ENTER", "\x03", "\x04"}:
                self.response_overlay = False
                return True
            if key == "\t":
                self.response_overlay = False
                self.view_index = (self.view_index + 1) % len(VIEWS)
                self.selected = 0
                return True
            if key == "KEY_BTAB":
                self.response_overlay = False
                self.view_index = (self.view_index - 1) % len(VIEWS)
                self.selected = 0
                return True
            if isinstance(key, str) and key.startswith("KEY_F"):
                try:
                    self.choose_view(int(key[5:]) - 1)
                except ValueError:
                    pass
                self.response_overlay = False
                return True
            if key in {"KEY_UP", "KEY_DOWN"}:
                self.response_scroll = max(0, self.response_scroll - 1) if key == "KEY_UP" else self.response_scroll + 1
                return True
            if key in {"KEY_PPAGE", "KEY_NPAGE"}:
                delta = max(1, self.response_page_size)
                self.response_scroll = max(0, self.response_scroll - delta) if key == "KEY_PPAGE" else self.response_scroll + delta
                return True
            # While a response is open, printable keys cannot enter or execute
            # commands. Escape, Enter, Tab and view/function keys dismiss it.
            return True
        if key == "?" and not self.input_focused:
            self.open_help("view:" + self.view)
            self.expand_requested = True
            return True
        if key == "\t" and not self.input_focused:
            self.choose_view((self.view_index + 1) % len(VIEWS))
            return True
        if key == "\t" and self.input_focused:
            if not self.completion_candidates or self.command not in self.completion_candidates:
                self.completion_prefix = self.command
                self.completion_candidates = _command_completions(self.command)
                self.completion_index = 0
            if self.completion_candidates:
                self.command = self.completion_candidates[self.completion_index % len(self.completion_candidates)]
                self.command_cursor = len(self.command)
                self.completion_index += 1
            return True
        if key == "KEY_BTAB" and not self.input_focused:
            self.choose_view((self.view_index - 1) % len(VIEWS))
            return True
        if key == "\x10":  # Ctrl-P recalls the previous command.
            if self.search_editing:
                return True
            self._history(-1)
            return True
        if key == "\x0e":  # Ctrl-N advances through command history.
            if self.search_editing:
                return True
            self._history(1)
            return True
        if key == "\x06" and not self.input_focused:  # Body-only task search.
            if not self.search_editing:
                self.search_draft = self.command
                self.search_draft_cursor = self.command_cursor
                self.search_was_focused = self.input_focused
            self.command = self.search_query
            self.command_cursor = len(self.command)
            self.search_editing = True
            self.input_focused = True
            return True
        if key == "\x14" and self.view == "Activity":  # Ctrl-T hides/shows controller bookkeeping rows.
            self.hide_plumbing = not self.hide_plumbing
            self.selected = 0
            return True
        if key == "\x07":  # Ctrl-G focuses the command draft.
            self.input_focused = True
            self.command_cursor = min(len(self.command), self.command_cursor)
            return True
        if key == "l" and self.view == "Usage" and not self.input_focused and not self.command:
            self.usage_legend = not self.usage_legend
            return True
        if key == "v" and self.view == "Tasks" and not self.input_focused:
            task = self.selected_task()
            if task is not None:
                self.request_task_document(str(task_number(task) or ""))
            return True
        if key == "o" and self.view == "Overview" and not self.input_focused:
            if not self.document_pending:
                self._invalidate_document_read()
                generation = self._document_generation
                self.document_pending = True
                def read_objective() -> None:
                    try:
                        from .bridge import open_store
                        with open_store() as store:
                            source = str(store.get_mission(self.mission_id)["objective"])
                        result = ("objective", "1", source, "")
                    except Exception as exc:
                        result = ("objective", "?", None, f"Could not read full objective: {_text(exc, 160)}")  # type: ignore[assignment]  # None stands for a missing value in this row
                    with self._result_lock:
                        if generation == self._document_generation:
                            self._document_result = result
                threading.Thread(target=read_objective, name="llm-bridge-objective", daemon=True).start()
            return True
        if self.input_focused and key in {"KEY_HOME", "\x01"}:
            self.command_cursor = 0
            return True
        if self.input_focused and key in {"KEY_END", "\x05"}:
            self.command_cursor = len(self.command)
            return True
        if self.input_focused and key == "KEY_LEFT":
            self.command_cursor = max(0, self.command_cursor - 1)
            return True
        if self.input_focused and key == "KEY_RIGHT":
            self.command_cursor = min(len(self.command), self.command_cursor + 1)
            return True
        if isinstance(key, str) and key.startswith("KEY_F"):
            try:
                self.choose_view(int(key[5:]) - 1)
            except ValueError:
                pass
            return True
        if key in {"KEY_SLEFT", "KEY_SRIGHT"} and not self.input_focused:
            if self.view == "Usage":
                self.usage_hscroll = max(0, self.usage_hscroll + (-8 if key == "KEY_SLEFT" else 8))
            return True
        if key in {"KEY_LEFT", "KEY_RIGHT"} and not self.input_focused:
            direction = -1 if key == "KEY_LEFT" else 1
            self.view_index = (self.view_index + direction) % len(VIEWS)
            self.selected = 0
            return True
        if key in {"KEY_UP", "KEY_DOWN"} and not self.input_focused:
            if self.view == "Tasks" and self.task_detail:
                self.detail_scroll = max(0, self.detail_scroll - 1) if key == "KEY_UP" else self.detail_scroll + 1
            else:
                self.selected = min(self._selection_max(), max(0, self.selected + (-1 if key == "KEY_UP" else 1)))
            return True
        if key in {"KEY_HOME", "KEY_END"} and not self.input_focused:
            self.selected = 0 if key == "KEY_HOME" else self._selection_max()
            return True
        if key in {"KEY_PPAGE", "KEY_NPAGE"} and not self.input_focused and not self.search_editing:
            if self.view == "Tasks" and self.task_detail:
                delta = 10 if self.full else 5
                self.detail_scroll = max(0, self.detail_scroll - delta) if key == "KEY_PPAGE" else self.detail_scroll + delta
            elif self.view == "Activity":
                self._page_events(older=key == "KEY_PPAGE")
            elif self.view == "Tasks":
                total = self.snapshot.get("task_total")
                if key == "KEY_PPAGE":
                    self.task_offset = max(0, self.task_offset - _TASK_PAGE_SIZE)
                elif not isinstance(total, int) or self.task_offset + _TASK_PAGE_SIZE < total:
                    self.task_offset += _TASK_PAGE_SIZE
                self.selected = 0
                self.last_refresh = 0.0
            else:
                delta = 10 if self.full else 5
                self.selected = min(self._selection_max(), max(0, self.selected + (-delta if key == "KEY_PPAGE" else delta)))
            return True
        if key in {"KEY_BACKSPACE", "\x7f", "\b"}:
            if self.command_cursor:
                self.command = self.command[:self.command_cursor - 1] + self.command[self.command_cursor:]
                self.command_cursor -= 1
            self.input_focused = True
            return True
        if key == "KEY_DC" and self.command_cursor < len(self.command):
            self.input_focused = True
            self.command = self.command[:self.command_cursor] + self.command[self.command_cursor + 1:]
            return True
        if key == "\x15":  # Ctrl-U clears the current input line.
            self.input_focused = True
            self.command = ""
            self.command_cursor = 0
            if self.search_editing or self.search_query:
                self.search_query = ""
                self.search_editing = False
                self.task_offset = 0
                self.selected = 0
                self.last_refresh = 0.0
            return True
        if key == "\x1b":
            if self.task_detail:
                self.task_detail = False
                return True
            if self.search_editing:
                self.command = self.search_draft
                self.command_cursor = min(len(self.command), self.search_draft_cursor)
                self.search_editing = False
                self.input_focused = self.search_was_focused
            elif self.input_focused or not self.search_query:
                self.input_focused = False
            else:  # Esc in the body clears an active task filter
                self.search_query = ""
                self.task_offset = 0
                self.selected = 0
                self.last_refresh = 0.0
            return True
        if key in {"\n", "\r", "KEY_ENTER"}:
            if self.search_editing:
                self.search_query = self.command.strip()
                self.command = self.search_draft
                self.command_cursor = min(len(self.command), self.search_draft_cursor)
                self.search_editing = False
                self.input_focused = self.search_was_focused
                self.task_offset = 0
                self.selected = 0
                self.last_refresh = 0.0
                return True
            if self.view == "Actions" and not self.input_focused:
                actions = self.palette_actions()
                if actions:
                    self.selected = min(max(0, self.selected), len(actions)-1)
                    self.activate_action(actions[self.selected])
            elif self.view == "Tasks" and not self.input_focused:
                self.task_detail = not self.task_detail
                self.detail_scroll = 0
            elif self.view == "Providers" and not self.input_focused:
                station = self.selected_station_name()
                if station:
                    self._dispatch("station " + shlex.quote(station))
            else:
                command = self.command
                if self._confirm_or_dispatch(command):
                    self.command = ""
                    self.command_cursor = 0
                    self.input_focused = False
            return True
        if key in {"KEY_RESIZE", "KEY_MOUSE"}:
            return True
        if key in {"\x03", "\x04"}:
            if self.search_editing:
                self.command = ""
                self.command_cursor = 0
                self.search_editing = False
                return True
            if self.command:
                if key == "\x04":
                    self.output.append("Draft retained. Ctrl-U clears it; Escape returns to the body.")
                    return True
                self.command = ""
                self.command_cursor = 0
                self.input_focused = False
                return True
            if self.command_pending:
                self.output.append("Wait for the command result before exiting.")
                return True
            return False
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.input_focused = True
            self.history_index = None
            if len(self.command) < _MAX_COMMAND_CHARS:
                self.command = self.command[:self.command_cursor] + key + self.command[self.command_cursor:]
                self.command_cursor += 1
            else:
                self.output.append("Command limit reached (4096 characters); use the document composer or --file for prompts.")
        elif isinstance(key, str) and (key.startswith("[") or key.startswith("\x1b")):
            return True
        return True

    def _page_events(self, *, older: bool) -> None:
        """Request the previous (older) or next (newer) page of 20 events for Activity."""
        if older and self.event_anchor is None:
            ids = [int(str(event["id"])) for event in _items(self.snapshot.get("events"))
                   if str(event.get("id", "")).isdigit()]
            self.event_anchor = max(ids) if ids else 0
        self.event_offset = self.event_offset + 20 if older else max(0, self.event_offset - 20)
        if self.event_offset == 0:
            self.event_anchor = None
        self.selected = 0
        self.last_refresh = 0.0

    def wheel(self, direction: int) -> None:
        """Mouse wheel (-1 up, +1 down) in whatever is on screen, three rows per notch.

        Overlays and the document viewer scroll; a task detail scrolls; lists move
        their selection (the view follows it); other views scroll their content,
        and Activity continues onto older/newer event pages past either end.
        """
        step = _WHEEL_ROWS * direction
        viewer = self.document_viewer
        if viewer is not None:
            if self.document_heading_list:
                count = len(viewer.headings())
                self.document_heading_selected = min(max(0, count - 1), max(0, self.document_heading_selected + direction))
            else:
                viewer.scroll(step, viewer.viewport_height)
            return
        if self.response_overlay:
            if self.confirm_command is None:
                self.response_scroll = max(0, self.response_scroll + step)
            return
        if self.view == "Tasks" and self.task_detail:
            self.detail_scroll = max(0, self.detail_scroll + step)
            return
        if self.selectable:
            stride = 1 if self.view == "Providers" else _WHEEL_ROWS
            self.selected = min(self._selection_max(), max(0, self.selected + direction * stride))
            return
        if self.view == "Activity":
            if direction < 0 and self.selected == 0:
                self._page_events(older=True)
                return
            if direction > 0 and self.event_offset > 0 and self.selected >= self._scroll_limit:
                self._page_events(older=False)
                return
        self.selected = max(0, self.selected + step)

    def hscroll(self, direction: int) -> None:
        """Shift+wheel (or a horizontal wheel): pan the Usage dashboard or a long document line by 8 cells."""
        viewer = self.document_viewer
        if viewer is not None:
            viewer.horizontal = max(0, viewer.horizontal + 8 * direction)
        elif self.view == "Usage":
            self.usage_hscroll = max(0, self.usage_hscroll + 8 * direction)

    def _history(self, direction: int) -> None:
        if not self.history:
            return
        if self.history_index is None:
            self.command_draft = self.command
            self.command_draft_cursor = self.command_cursor
            self.history_index = len(self.history)
        self.history_index = min(len(self.history), max(0, self.history_index + direction))
        self.command = "" if self.history_index == len(self.history) else self.history[self.history_index]
        if self.history_index == len(self.history):
            self.command = self.command_draft or ""
            self.command_cursor = min(len(self.command), self.command_draft_cursor)
            self.history_index = None
        else:
            self.command_cursor = len(self.command)
        self.input_focused = True

    def dismiss_status(self) -> None:
        """Stop showing the latest output line in the status row; history stays."""
        self._status_dismissed = (len(self.output), self.output[-1]) if self.output else None

    def status_message(self) -> str | None:
        """Latest transient output line unless a view change, a new command or the clock retired it.

        A message stays for `_STATUS_SECONDS` after it first appears; the full
        text remains in the response history.
        """
        if self._status_view is None:
            self._status_view = self.view_index
        elif self._status_view != self.view_index:
            self._status_view = self.view_index
            self.dismiss_status()
        if not self.output:
            return None
        key = (len(self.output), self.output[-1])
        if key == self._status_dismissed:
            return None
        current = self.status_clock()
        if key != self._status_key:
            self._status_key, self._status_since = key, current
        if current - self._status_since >= _STATUS_SECONDS:
            return None
        bridge_redraw.note_wall(_STATUS_SECONDS - (current - self._status_since) + 0.01)  # the message expires
        return self.output[-1]

    def choose_view(self, index: int) -> None:
        if 0 <= index < len(VIEWS):
            if self.view == "Tasks" and self.selected_task() is not None:
                self.selected_task_anchor = str(task_number(self.selected_task() or {}))
            self.view_index = index
            self.selected = 0

    def palette_actions(self) -> list[Action]:
        """Actions for the current mission and task state, most relevant first, narrowed by the filter."""
        verifying = tuple(
            str(task_number(task)) for task in _items(self.snapshot.get("tasks"))
            if str(task.get("status", "")).upper() == "VERIFYING"
        )
        pending = _dict(self.snapshot.get("proposal_counts")).get("PENDING")
        status = str(_mission(self.snapshot).get("status") or "").upper()
        if not status:
            status = "CREATED"
        actions = actions_for(
            status,
            verifying=verifying,
            pending_proposals=int(pending) if isinstance(pending, (int, float)) and not isinstance(pending, bool) else 0,
            selected_task=self.selected_task_anchor,
            holding=_review_hold_task(self.snapshot) is not None,
        )
        query = self.palette_filter.casefold()
        return [action for action in actions if query in f"{action.label} {action.effect} {action.typed}".casefold()]

    def action_unavailable(self, action: Action) -> str:
        status = str(_mission(self.snapshot).get("status", "")).upper()
        if action.mutable and status in ENDED_MISSION_STATUSES:
            return f"mission {status.lower()}"
        if action.task and self.selected_task_anchor is None:
            return "select a task in Tasks first"
        return ""

    def open_actions(self) -> None:
        """Ctrl-K: jump to Actions with the filter ready for typing."""
        self.choose_view(VIEWS.index("Actions"))
        self.palette_filter = ""
        self.palette_filter_editing = True
        self.palette_slash_pending = False
        self.input_focused = False
        self.task_detail = False
        self.palette_quick = True

    def activate_action(self, action: Action) -> None:
        unavailable = self.action_unavailable(action)
        if unavailable:
            self.output.append(f"{action.label} unavailable: {unavailable}")
        elif action.handler == "compose":
            self.document_task_id = None
            self.edit_initial_text = ""
            self.edit_document_requested = True
        elif action.handler == "amend":
            self.request_task_document(str(self.selected_task_anchor), for_edit=True)
        elif action.handler == "prefill":
            self.command = action.command  # the operator adds the rest (for example a reason) and presses Enter
            self.command_cursor = len(self.command)
            self.input_focused = True
            self.palette_filter_editing = False
        else:
            self._confirm_or_dispatch(action.command.format(task=self.selected_task_anchor))

    def close_document(self) -> None:
        self._invalidate_document_read()
        if self.document_viewer is not None:
            self.document_viewer.release()
            self.document_viewer = None
        self.document_search_editing = False
        self.document_export_editing = False
        self.document_heading_list = False

    def _invalidate_document_read(self) -> None:
        with self._result_lock:
            self._document_generation += 1
            self._document_result = None
        self.document_pending = False
        self.document_edit_pending = False

    def request_task_document(self, task_id: str, *, for_edit: bool = False) -> None:
        if self.document_pending:
            self.output.append("A full task read is already in progress.")
            return
        task = next((item for item in _items(self.snapshot.get("tasks")) if str(task_number(item)) == task_id), {})
        self._invalidate_document_read()
        generation = self._document_generation
        self.document_pending = True
        self.document_edit_pending = for_edit

        def read_document() -> None:
            try:
                raw = self.dispatch(self.mission_id, shlex.join(["task", "show", *_task_ref(self.snapshot, task_id), "--json"]))
                record = json.loads(str(raw))
                prompt = record.get("prompt") if isinstance(record, dict) else None
                pending = _dict(record.get("pending")) if isinstance(record, dict) else {}
                if for_edit and isinstance(pending.get("prompt"), str):
                    prompt = pending["prompt"]
                revision = str(record.get("revision", task.get("revision", "?"))) if isinstance(record, dict) else "?"
                result = (task_id, revision, prompt, "") if isinstance(prompt, str) else (task_id, revision, None, "Full prompt unavailable; edit cancelled. Retry task show.")
            except Exception as exc:
                result = (task_id, "?", None, f"Could not read full prompt: {_text(exc, 160)}")
            with self._result_lock:
                if generation == self._document_generation:
                    self._document_result = result

        threading.Thread(target=read_document, name="llm-bridge-document", daemon=True).start()

    def selected_station_name(self) -> str | None:
        stations = _items(self.snapshot.get("stations"))
        if not stations:
            return None
        item = stations[min(max(0, self.selected), len(stations) - 1)]
        value = item.get("name") or item.get("provider") or item.get("id")
        return str(value) if value else None

    def _selected_task_key(self) -> str | None:
        """Identity (the task id) of the highlighted task, stable across list changes."""
        task = self.selected_task()
        return None if task is None or task.get("id") is None else str(task.get("id"))

    def _reselect_task(self, key: str) -> None:
        """Move the highlight to the task with this id when the new list still has it."""
        for index, task in enumerate(_items(self.snapshot.get("tasks"))):
            if str(task.get("id")) == key:
                self.selected = index
                return

    def selected_task(self) -> dict[str, object] | None:
        tasks = _items(self.snapshot.get("tasks"))
        if not tasks:
            return None
        return tasks[min(max(0, self.selected), len(tasks) - 1)]

    def selection_rows(self) -> list[int]:
        """Row index of each selectable item: a task, a provider block or an action."""
        if self.view == "Providers":
            return self._provider_layout()[1]
        if self.view == "Tasks" and not self.task_detail:
            return list(range(max(1, len(_items(self.snapshot.get("tasks"))))))  # without rendering every row
        return list(range(len(self.rows())))

    def _selection_max(self) -> int:
        return max(0, len(self.selection_rows()) - 1)

    def selected_row(self) -> int:
        """Display row of the highlighted item."""
        rows = self.selection_rows()
        return rows[min(max(0, self.selected), len(rows) - 1)] if rows else 0

    @property
    def selectable(self) -> bool:
        """Only these views have actionable rows; the rest scroll as text."""
        return self.view in _SELECTABLE_VIEWS

    def column_header(self) -> str | None:
        """Sticky column titles for table views (drawn above the rows)."""
        if self.view == "Sessions":
            return self._session_table()[0]
        return None

    def rows(self) -> list[str]:
        view = self.view
        if view == "Usage":
            return self._usage_rows()
        if view == "Overview":
            return self._bridge_rows()
        if view == "Tasks":
            return self._order_rows()
        if view == "Providers":
            return self._station_rows()
        if view == "Health":
            return self._system_rows()
        if view == "Activity":
            return self._log_rows()
        if view == "Sessions":
            return self._session_rows() or [_station_label(station) for station in _items(self.snapshot.get("stations"))] or ["No session activity recorded."]
        if view == "Actions":
            return self._palette_rows()
        return []

    def row_segments(self) -> list[list[Segment]] | None:
        """Per-row coloured segments for the views that style elements, aligned with `rows()`."""
        view = self.view
        if view == "Tasks" and not self.task_detail:
            return self._order_list()[0] or None
        if view == "Providers":
            return self._provider_segments()[0]
        if view == "Sessions":
            return self._session_cells()[1] or None
        if view == "Actions":
            return self._palette_segments()
        if view == "Activity":
            rows, roles = self._activity_layout()
            return [_activity_row_segments(row, role) for row, role in zip(rows, roles)]
        return None

    def row_roles(self) -> list[str] | None:
        """Semantic role per row for views that style rows individually."""
        if self.view == "Providers":
            return self._provider_layout()[2]
        if self.view == "Activity":
            return self._activity_layout()[1]
        return None

    def _usage_rows(self) -> list[str]:
        usage = _dict(self.snapshot.get("usage"))
        status = str(usage.get("status") or "refreshing")
        age = _int_or_none(usage.get("age_seconds"))
        parts = ["Usage", status]
        parts.append(f"updated {_age(age)} ago" if age is not None else "first sample pending")
        if usage.get("error"):
            parts.append(f"last refresh failed: {_text(usage.get('error'), 60)}")
        # One cell per character before the columns are cut, so an ASCII locale keeps them aligned.
        lines = localize_cells(str(usage.get("text") or "Loading full llm-usage dashboard...")).splitlines()
        table, legend = _split_usage_text(lines)
        if table is None:
            return [_SEP.join(parts), *lines]
        rows = [_SEP.join(parts), *_fit_usage_table(table, self.content_width)]
        if legend:
            rows += ["", *(legend if self.usage_legend else ["l shows the legend"])]
        return rows

    def _bridge_rows(self) -> list[str]:
        data = self.snapshot
        tasks = _items(data.get("tasks"))
        current_task = _dict(data.get("current_task"))

        def row(label: str, value: str) -> str:
            return f"{label:<{_BRIDGE_LABEL}}{value}"

        next_task = next(
            (task for task in tasks if task.get("status") in {"READY", "QUEUED"} and task.get("id") != current_task.get("id")),
            None,
        )
        order_counts = _count_phrase(_dict(data.get("task_counts")))
        proposals = _dict(data.get("proposal_counts"))
        proposal_total = sum(count for count in proposals.values() if isinstance(count, (int, float)))
        orders = order_counts or (f"{len(tasks)} visible" if tasks else "none yet — Ctrl-E adds a task")
        if proposal_total:
            orders += f"{_SEP}{int(proposal_total)} proposal{'s' if proposal_total != 1 else ''}"
        rows = [
            row("Controller", _controller_line(data)),
            row("Now", ("task " + _task_label(current_task) + (_SEP + _idle_phrase(data) if _idle_phrase(data) else "")) if current_task else "idle"),
            row("Next", "task " + _task_label(next_task) if next_task else "nothing ready"),
            row("Tasks", orders),
        ]
        needs = _needs_you_rows(data)
        for index, text in enumerate(needs):
            rows.append(row("Needs you" if index == 0 else "", text))
        queued = data.get("pending_command_count") or 0
        if isinstance(queued, int) and queued:
            rows.append(row("Queue", f"{queued} command{'s' if queued != 1 else ''} waiting for the controller"))
        criteria = _dict(data.get("criteria"))
        total = _int_or_none(criteria.get("total"))
        if total:
            rows.append(row("Criteria", f"{_int_or_none(criteria.get('verified')) or 0}/{total} verified"))
        for index, text in enumerate(_capacity_summaries(data)):
            rows.append(row("Capacity" if index == 0 else "", _with_square(text)))
        return rows

    def _order_list(self) -> tuple[list[list[Segment]], list[str]]:
        """Segments and text of the Tasks list; computed once per drawn frame."""
        cache = self._draw_cache
        if cache is not None and "orders" in cache:
            return cache["orders"]  # type: ignore[return-value]
        segments = _order_list_segments(_items(self.snapshot.get("tasks")))
        result = (segments, [_segments_text(row) for row in segments])
        if cache is not None:
            cache["orders"] = result
        return result

    def _order_rows(self) -> list[str]:
        if not self.task_detail:
            return self._order_list()[1] or ["No tasks recorded."]
        task = self.selected_task()
        if task is None:
            return ["No task selected."]
        dependencies = task.get("depends_on", [])
        acceptance = task.get("acceptance", [])
        evidence = task.get("evidence", [])
        pending = _dict(task.get("pending"))
        amendments = task.get("amendments")
        return [
            f"Task {_text(task_number(task) if task_number(task) is not None else '?', 32)}: {_cut_text(task.get('title') or 'Untitled', 100)}",
            f"Status {_text(task.get('status', 'unknown'), 24)} | priority {_text(task.get('priority', '?'), 16)} | revision {_text(task.get('revision', '?'), 16)}",
            f"Origin {_text(task.get('origin') or 'operator', 24)} | proposal {_text(task.get('proposal_status') or 'not a proposal', 32)} | proposed by {_text(task.get('proposed_by_operation') or 'unknown', 32)}",
            "Dependencies: " + (", ".join(_text(item, 32) for item in dependencies) if isinstance(dependencies, list) and dependencies else "none"),
            "Acceptance criteria:",
            *([f"  {index + 1}. {_cut_text(item, 240)}" for index, item in enumerate(acceptance[:_MAX_DETAIL_ITEMS])] if isinstance(acceptance, list) and acceptance else ["  none recorded"]),
            "Verification evidence:",
            *([f"  {index + 1}. {_evidence_text(item)}" for index, item in enumerate(evidence[:_MAX_DETAIL_ITEMS])] if isinstance(evidence, list) and evidence else ["  none recorded"]),
            "Pending amendment: " + (_cut_text(json.dumps(pending, ensure_ascii=False, sort_keys=True), 400) if pending else "none"),
            "Amendment history:",
            *([
                f"  revision {_text(item.get('revision', '?'), 16)} {_text(item.get('status', 'unknown'), 20)}: {_cut_text(json.dumps(item.get('changes', {}), ensure_ascii=False, sort_keys=True), 240)}"
                for item in amendments[:5] if isinstance(item, dict)
            ] if isinstance(amendments, list) and amendments else ["  none recorded"]),
            "Prompt preview (v opens full prompt): " + _cut_text(task.get("prompt") or "", 800),
        ]

    def _station_rows(self) -> list[str]:
        return self._provider_layout()[0]

    def _provider_layout(self) -> tuple[list[str], list[int], list[str]]:
        """Rows of the Providers view, the first row of each selectable block, and a role per row."""
        segments, starts, roles = self._provider_segments()
        return [_segments_text(row) for row in segments], starts, roles

    def _provider_segments(self) -> tuple[list[list[Segment]], list[int], list[str]]:
        """Segments of each Providers row, the first row of each selectable block, and a role per row.

        Each configured provider or route is one block: a header (name, model,
        live state), one line per capacity window with remaining share, pace and
        reset, then its most recent operations.
        """

        data = self.snapshot
        stations = _items(data.get("stations"))
        operations = _items(data.get("operations"))
        if not stations:
            observed = {
                (_text(item.get("station") or item.get("provider") or "unknown", 60),
                 _text(item.get("model"), 60) if _known(item.get("model")) else "default model",
                 _text(item.get("status") or "unknown", 24))
                for item in operations
            }
            fallback = [f"{station}  {model}  {status}" for station, model, status in sorted(observed)] or ["No providers recorded."]
            return [[(row, "info")] for row in fallback], [], ["info"] * len(fallback)
        context = _dict(data.get("station_context"))
        viewed = _text(context.get("viewed_station") or "unknown", 80)
        last_launched = _text(context.get("last_launched_station") or "unknown", 80)
        executing = context.get("executing_operation")
        summary = [
            f"Viewing {viewed}" if _known(viewed) else "",
            f"last launched {last_launched}" if _known(last_launched) else "",
            f"operation {_short_id(executing)} {_text(context.get('operation_status') or '', 20)}".rstrip() if _known(executing) else "",
        ]
        header = _SEP.join(part for part in summary if part) or "No provider launched yet"
        details = [
            f"mode {_text(context.get('mode'), 20)}" if _known(context.get("mode")) else "",
            f"liveness {_text(context.get('provider_liveness'), 20)}" if _known(context.get("provider_liveness")) else "",
            f"effort {_text(context.get('observed_effort'), 20)}" if _known(context.get("observed_effort")) else "",
        ]
        detail_line = _SEP.join(part for part in details if part) or "Enter marks the highlighted provider as viewed"
        rows: list[list[Segment]] = [_inline_segments(header, "muted"), _inline_segments(detail_line, "muted")]
        roles: list[str] = ["dim", "dim"]
        starts: list[int] = []
        now = _clock_epoch()
        selection = _dict(_dict(data.get("capacity")).get("selection"))
        decisions = {_decision_owner(row): row for row in _items(selection.get("decisions"))}
        active = _active_owner(data)
        current = _dict(data.get("current_task"))
        elapsed = _dict(data.get("execution")).get("operation_elapsed_seconds")
        for item in stations:
            name = _text(item.get("name") or item.get("provider") or item.get("id") or "Provider", 80)
            decision = decisions.get(name) or decisions.get(_text(item.get("provider") or "", 80))
            usage_rows = _usage_rows_for(data, _text(item.get("provider") or name, 60), _text(item.get("model"), 60) if _known(item.get("model")) else "")
            state, role = _provider_state(name, active, current, elapsed, decision, now, usage_rows)
            model = f"model {_text(item.get('model'), 50)}" if _known(item.get("model")) else "default model"
            if _known(item.get("observed_model")) and item.get("observed_model") != item.get("model"):
                model += f" (live {_text(item['observed_model'], 50)})"
            flags = [flag for flag, shown in (("[viewed]", name == viewed), ("[historical]", item.get("status") == "historical")) if shown]
            starts.append(len(rows))
            rows.append(_provider_header_segments(name, model, state, role, flags))
            roles.append(role)
            windows = _items(decision.get("windows")) if decision else []
            lines = [_window_summary(window, now) for window in windows] or _usage_window_lines(usage_rows, now) or [
                "capacity: no observation yet" if decision is None else "capacity unavailable"]
            for line in lines:
                rows.append([("  ", "info"), *_inline_segments(line, "info")])
                roles.append("info")
            recent = [op for op in operations if name in {str(op.get("station") or ""), str(op.get("route_id") or "")}][-3:]
            for op in reversed(recent):
                age = _relative_epoch(op.get("created_at"), now)
                rows.append(_recent_operation_segments(op, age))
                roles.append("dim")
            if not recent:
                rows.append([("  recent: no operations yet", "dim")])
                roles.append("dim")
        return rows, starts, roles

    def _session_rows(self) -> list[str]:
        return self._session_table()[1]

    def _session_table(self) -> tuple[str | None, list[str]]:
        header, rows = self._session_cells()
        return header, [_segments_text(row) for row in rows]

    def _session_cells(self) -> tuple[str | None, list[list[Segment]]]:
        operations = _items(self.snapshot.get("operations"))
        if not operations:
            return None, []

        def cell(value: object, limit: int) -> str:
            return _text(value, limit) if _known(value) else "-"

        table = [
            (
                _short_id(item.get("id") or item.get("operation_id")),
                cell(item.get("task_ordinal") if _known(item.get("task_ordinal")) else item.get("task_id"), 12),
                cell(item.get("route_id") or item.get("route") or item.get("station") or item.get("provider"), 24),
                cell(item.get("model"), 28),
                _state_text(cell(item.get("status"), 20)),
                _short_id(item.get("native_session_id")),
                cell(item.get("restoration_state"), 24),
            )
            for item in operations
        ]
        widths = [
            max(len(title), *(len(line[index]) for line in table))
            for index, title in enumerate(_SESSION_COLUMNS)
        ]
        header = "  ".join(title.ljust(widths[index]) for index, title in enumerate(_SESSION_COLUMNS)).rstrip()
        roles = ["muted", "muted", "info", "muted", "", "muted", "muted"]
        rows: list[list[Segment]] = []
        for line in table:
            row: list[Segment] = []
            for index, value in enumerate(line):
                role = roles[index] or _state_segment(value.split()[-1])[1]
                row.append((value.ljust(widths[index]) if index < len(line) - 1 else value, role))
                if index < len(line) - 1:
                    row.append(("  ", "info"))
            rows.append(row)
        return header, rows

    def _system_rows(self) -> list[str]:
        data = self.snapshot
        out: list[str] = []

        def add(label: str, value: str) -> None:
            if value:
                out.append(f"{label:<{_SYSTEM_LABEL}}{value}")

        mode = _dict(data.get("station_mode"))
        if mode:
            actual = mode.get("actual_mode")
            add("Mode", _SEP.join(part for part in (
                f"configured {_text(mode['configured_mode'], 24)}" if _known(mode.get("configured_mode")) else "",
                f"actual {_text(actual, 24)}" if _known(actual) else "actual unverified",
            ) if part))
        lifecycle = _dict(data.get("lifecycle"))
        if lifecycle:
            add("Lifecycle", _SEP.join(part for part in (
                _text(lifecycle.get("status"), 20) if _known(lifecycle.get("status")) else "",
                f"controller {_text(lifecycle['controller'], 24)}"
                + (f" (verified {_relative_epoch(lifecycle.get('controller_event_at'))})" if _known(lifecycle.get("controller_event_at")) and _relative_epoch(lifecycle.get("controller_event_at")) else "")
                if _known(lifecycle.get("controller")) else "",
            ) if part))
            heartbeat = _dict(lifecycle.get("heartbeat"))
            if heartbeat:
                age = _int_or_none(heartbeat.get("age_seconds"))
                lock = heartbeat.get("owner_lock_held")
                add("Controller heartbeat", _SEP.join(part for part in (
                    _text(heartbeat["reported_state"], 20).lower() if _known(heartbeat.get("reported_state")) else "",
                    f"{_age(age)} ago" if age is not None else "",
                    "owner lock held" if str(lock).lower() == "true" else "owner lock not held" if str(lock).lower() == "false" else "",
                    f"mission wants {_text(heartbeat['desired_mission_status'], 20)}" if _known(heartbeat.get("desired_mission_status")) else "",
                    f"liveness {_text(heartbeat['current_liveness'], 20)}" if _known(heartbeat.get("current_liveness")) else "",
                ) if part) or "none recorded")
        add("Routes", _text(data["routes"], 220) if _known(data.get("routes")) else "")
        controller = data.get("controller")
        add("Controller", _text(controller, 220) if _known(controller) else "")
        execution = data.get("execution")
        if isinstance(execution, dict):
            elapsed = _int_or_none(execution.get("operation_elapsed_seconds"))
            verified = _int_or_none(execution.get("seconds_since_verified_progress"))
            add("Execution", _SEP.join(part for part in (
                f"iteration {_text(execution['iteration'], 8)}" if _known(execution.get("iteration")) else "",
                f"operation running {_span(elapsed)}" if elapsed is not None else "",
                f"last verified progress {_age(verified)} ago" if verified is not None else "no verified progress yet",
            ) if part))
        elif _known(execution):
            add("Execution", _text(execution, 220))
        health = data.get("health")
        if isinstance(health, dict):
            failed = _int_or_none(health.get("consecutive_failed_operations")) or 0
            limit = _int_or_none(health.get("max_provider_failures"))
            warning = health.get("stall_warning")
            add("Health", _SEP.join(part for part in (
                _text(health["status"], 24) if _known(health.get("status")) else "",
                f"{failed} consecutive failed operation{'s' if failed != 1 else ''}" + (f" (limit {limit})" if limit is not None else "") if failed else "",
                _text(warning, 200) if _known(warning) else "",
            ) if part) or "no problems reported")
        elif _known(health):
            add("Health", _text(health, 220))
        persistence = _dict(data.get("persistence"))
        if persistence:
            add("Persistence", _SEP.join(part for part in (
                f"sqlite schema {_text(persistence['schema_version'], 8)}" if _known(persistence.get("schema_version")) else "",
                f"{_text(persistence['journal_mode'], 12)} journal" if _known(persistence.get("journal_mode")) else "",
                _text(persistence["database"], 300) if _known(persistence.get("database")) else "",
            ) if part))
        criteria = _dict(data.get("criteria"))
        if criteria and _int_or_none(criteria.get("total")):
            add("Criteria", f"{_int_or_none(criteria.get('verified')) or 0}/{_int_or_none(criteria.get('total'))} verified")
        progress = _dict(data.get("last_meaningful_progress"))
        if progress:
            when = _relative_epoch(progress.get("at"))
            add("Last progress", _SEP.join(part for part in (
                _text(progress["kind"], 40) if _known(progress.get("kind")) else "",
                f"{_local_clock(progress.get('at'))} ({when})" if when else "",
            ) if part))
        suspend = _dict(data.get("suspend"))
        if suspend and (_known(suspend.get("status")) or _known(suspend.get("target_epoch"))):
            until = _relative_epoch(suspend.get("target_epoch"))
            add("Suspend", _SEP.join(part for part in (
                _text(suspend["status"], 40) if _known(suspend.get("status")) else "",
                f"until {until[3:] if until and until.startswith('in ') else until}" if until else "",
                _text(suspend["reason"], 80) if _known(suspend.get("reason")) else "",
            ) if part))
        capacity = _dict(data.get("capacity"))
        summaries = _capacity_summaries(data)
        fallback = [] if summaries else _usage_capacity_lines(data)
        if fallback and not any(_dict(capacity.get("selection")).get(key) for key in ("route", "provider")):
            # Nothing has run yet: show what llm-usage reports instead of "no capacity observation".
            age = _int_or_none(_dict(data.get("usage")).get("age_seconds"))
            add("Capacity", "from llm-usage" + (f", updated {_age(age)} ago" if age is not None else ""))
            for line in fallback:
                out.append(f"{'':<{_SYSTEM_LABEL}}{line}")
        elif capacity:
            selection = _dict(capacity.get("selection"))
            owner = selection.get("route") or selection.get("provider")
            age = _int_or_none(capacity.get("source_age_seconds"))
            header = _SEP.join(part for part in (
                f"{_text(capacity['source'], 40)}" + (f" ({_age(age)} old)" if age is not None else "") if _known(capacity.get("source")) else "",
                f"selected {_text(owner, 40)}" + (f" / {_text(selection['selected_model'], 40)}" if _known(selection.get("selected_model")) else "") if _known(owner) else "",
                _text(plain_reason(selection["rotation_reason"]), 60) if _known(selection.get("rotation_reason")) else "",
                "all routes rate-limited" if str(selection.get("all_rate_limited")).lower() == "true" else "",
            ) if part)
            wait = _dict(capacity.get("wait"))
            if wait:
                until = _relative_epoch(wait.get("target_epoch"))
                header = _SEP.join(part for part in (header, f"waiting {until}" if until else "waiting", _text(wait["reason"], 60) if _known(wait.get("reason")) else "") if part)
            add("Capacity", header or "no capacity observation")
            for line in summaries:
                out.append(f"{'':<{_SYSTEM_LABEL}}{_with_square(line)}")
        return out or ["System and capacity status is not available in this snapshot."]

    def _log_rows(self) -> list[str]:
        return self._activity_layout()[0]

    def _activity_layout(self) -> tuple[list[str], list[str]]:
        """Rows of the Activity view and a role per row (plumbing is dim, problems are errors)."""
        events = _items(self.snapshot.get("events"))
        if not events:
            return ["No recent mission events."], ["dim"]
        entries = _activity_entries(events, _task_numbers(self.snapshot))
        shown = [entry for entry in entries if not (self.hide_plumbing and entry.plumbing)]
        if not shown:
            return [f"Only controller bookkeeping on this page ({len(entries)} rows); Ctrl-T shows it."], ["dim"]
        rows = [f"{entry.clock}  {entry.text}".rstrip() for entry in shown]
        roles = ["dim" if entry.plumbing else _activity_role(entry.text) for entry in shown]
        return rows, roles

    def _palette_rows(self) -> list[str]:
        return [_segments_text(row) for row in self._palette_segments()]

    def _palette_segments(self) -> list[list[Segment]]:
        """Action rows: the name in default text, its typed command in `key`, the effect `muted`."""
        actions = self.palette_actions()
        self.selected = min(max(0, self.selected), max(0, len(actions) - 1))
        if not actions:
            return [[(_PALETTE_NOTE, "info")]]
        label_width = max(len(action.label) for action in actions)
        typed_width = max(len(action.typed) for action in actions)
        rows: list[list[Segment]] = []
        for action in actions:
            unavailable = self.action_unavailable(action)
            if unavailable:  # disabled items recede
                text = f"{action.label:<{label_width}}  {action.typed:<{typed_width}}  {action.effect}"
                rows.append([(text + f"{_SEP}unavailable: {unavailable}", "dim")])
                continue
            rows.append([
                (f"{action.label:<{label_width}}", "info"), ("  ", "info"),
                (f"{action.typed:<{typed_width}}", "key"), ("  ", "info"), (action.effect, "muted"),
            ])
        return rows

    def handle_mouse(self, y: int, x: int, height: int, width: int) -> None:
        if self.response_overlay:
            return
        if height <= 0 or width <= 0:
            return
        if y == height - 2:
            self.input_focused = True
            self.command_cursor = len(self.command)
            return
        nav_y = 2
        if y == nav_y:
            x = max(0, min(width - 1, x))
            self.choose_view(_nav_index_at(x, self.view_index, width, "  " if self.full else " "))
        elif self.view == "Actions" and y >= nav_y + 1:
            count = len(self.palette_actions())
            start = _scroll_start(self.selected, max(1, count), max(1, height-6))
            self.selected = max(0, min(count-1, start + y - nav_y - 1))
        elif self.full and y >= 3 and self.selectable:
            rows = self.rows()
            content_height = max(1, height - 6)
            if self.view == "Tasks" and not self.task_detail:
                list_height = _orders_list_height(content_height, width, len(rows))
                if list_height is not None:
                    if y - 3 >= list_height:
                        return  # separator and preview are not selectable
                    content_height = list_height
            selected_row = self.detail_scroll if self.view == "Tasks" and self.task_detail else self.selected_row()
            start = _scroll_start(selected_row, len(rows), content_height)
            row_index = min(max(0, len(rows) - 1), start + y - 3)
            if self.view == "Tasks" and self.task_detail:
                self.detail_scroll = row_index
            elif self.view == "Providers":
                starts = [begin for begin in self.selection_rows() if begin <= row_index]
                if starts:
                    self.selected = len(starts) - 1
            else:
                self.selected = row_index


_FINISHED_STATES = FINISHED_STATES
_state_look = state_look
_state_text = state_text


def _state_segment(status: object) -> Segment:
    look = _state_look(status)
    return (_state_text(status), look[1] if look else "info")


_provider_role = provider_role


def _percent_role(percent: float) -> str:
    """Capacity role for a share of quota remaining: 0 is empty, up to 25% is low."""
    if percent <= 0:
        return "capacity_empty"
    return "capacity_low" if percent <= 25 else "capacity_ok"


_IDLE_WARN_SECONDS = 600
_DURATION = r"\d+[dhms](?: \d+[dhms])*"
_STATE_WORDS = "RUNNING|READY|QUEUED|VERIFYING|COMPLETED|FAILED|BLOCKED|CANCELLED|PAUSED|PAUSING|STOPPED|STOPPING|CREATED|ACTIVE"


@functools.lru_cache(maxsize=4)
def _token_pattern(square: str, glyphs: tuple[str, ...]) -> re.Pattern[str]:
    state_glyphs = "|".join(re.escape(item) for item in glyphs)
    return re.compile(
        "|".join([
            rf"(?P<square>{re.escape(square)} )(?P<owner>[^\s/,;()]+)",
            r"(?P<cmd>(?<![\w/])/[a-z][a-z-]*(?![\w/.]))",
            r"(?P<key>\bCtrl-\S|\bF\d{1,2}\b)",
            rf"(?P<idle>\bno output(?: for)? {_DURATION})",
            rf"(?P<time>\b{_DURATION} ago\b|\bin {_DURATION}|\bresets (?:now|{_DURATION})|empty in ~{_DURATION} at this pace)",
            r"(?P<pct>\b\d+(?:\.\d+)?%)",
            r"(?P<past>\blasts past reset\b)",
            r"(?P<limited>\brate-limited\b)",
            rf"(?P<state>(?:(?:{state_glyphs}) )?\b(?:{_STATE_WORDS})\b)",
            r"(?P<taskid>(?<=\btask )\d+\b)",
            r"(?P<hexid>\b[0-9a-f]{8}\b)",
        ])
    )


_LOWER_STATES = r"(?<!not )\b(?:running|queued|ready|verifying|blocked|failed|completed|cancelled|paused|stopped|stopping)\b"
_CONTROLLER_WARN = r"silent for|not running|not started|no heartbeat recorded yet|pausing after the current provider invocation"
_PLAIN_PATTERNS = {
    "tasks": re.compile(_LOWER_STATES),
    "controller": re.compile(rf"{_LOWER_STATES}|\bactive\b|{_CONTROLLER_WARN}"),
    "strip": re.compile(rf"{_LOWER_STATES}|awaits approval|awaits your review|waiting for capacity|stopping after the current step"),
}


def _idle_role(text: str) -> str:
    """`muted`, or `warn` once the provider has been silent for ten minutes."""
    tail = text.split("no output", 1)[1].replace("for ", "", 1).strip()
    seconds = _duration_seconds(tail)
    return "warn" if seconds is not None and seconds >= _IDLE_WARN_SECONDS else "muted"


def _plain_words(text: str, base: str, context: str) -> list[Segment]:
    """Colour the lowercase state words and phrases that only some contexts use."""
    pattern = _PLAIN_PATTERNS.get(context)
    if pattern is None or not text:
        return [(text, base)]
    out: list[Segment] = []
    position = 0
    for match in pattern.finditer(text):
        out.append((text[position:match.start()], base))
        word = match.group(0)
        if word == "active":
            role = "state_running"
        elif word in {"awaits approval", "awaits your review"}:
            role = "state_review"
        elif word in {"waiting for capacity", "stopping after the current step"} or re.fullmatch(_CONTROLLER_WARN, word):
            role = "warn"
        else:
            look = _state_look(word)
            role = look[1] if look else base
            if context == "tasks" and role in {"state_review", "state_failed"}:
                role = base  # the Needs-you row is the one place that draws the eye
        out.append((word, role))
        position = match.end()
    out.append((text[position:], base))
    return out


def _inline_segments(text: str, base: str = "info", *, context: str = "generic") -> list[Segment]:
    """Colour the machine-generated parts of an informational line.

    Only system-written phrasing is tokenised (percentages, ages, state words,
    `/commands`, key names, ids and the provider square); user-written text such
    as task titles never goes through here.
    """
    glyphs = tuple(dict.fromkeys(glyph(name) for name in ("running", "waiting", "review", "done", "failed", "paused")))
    pattern = _token_pattern(glyph("square"), glyphs)
    out: list[Segment] = []
    position = 0
    for match in pattern.finditer(text):
        out.extend(_plain_words(text[position:match.start()], base, context))
        kind = match.lastgroup
        token = match.group(0)
        if match.group("square"):
            out.append((match.group("square").rstrip(), _provider_role(match.group("owner"))))
            out.append((" " + match.group("owner"), base))
        elif kind in {"cmd", "key"}:
            out.append((token, "key"))
        elif kind == "idle":
            out.append((token, _idle_role(token)))
        elif kind in {"time", "taskid", "hexid"}:
            out.append((token, "muted"))
        elif kind == "pct":
            out.append((token, _percent_role(float(token.rstrip("%")))))
        elif kind == "past":
            out.append((token, "capacity_ok"))
        elif kind == "limited":
            out.append((token, "capacity_empty"))
        else:  # state word, optionally behind its glyph
            look = _state_look(token.split()[-1])
            role = look[1] if look else base
            if context == "needs" and role in {"state_review", "state_failed"}:
                role += QUIET
            out.append((token, role))
        position = match.end()
    out.extend(_plain_words(text[position:], base, context))
    return _merge_segments(out)


def _status_role(value: str) -> str:
    """Pick a semantic role for short status and response lines."""
    lowered = value.lower()
    if any(token in lowered for token in ("none ready", "no tasks ready")):
        return "dim"
    if any(token in lowered for token in ("not ready", "ready=false")):
        return "warn"
    if any(token in lowered for token in ("error", "failed", "rejected", "could not", "unavailable")):
        return "error"
    if any(token in lowered for token in ("warning", "blocked", "waiting", "rate limit", "paused", "retry")):
        return "warn"
    if _TASK_ACK.match(lowered) or any(token in lowered for token in ("applied:", "completed", "success", "usable", "ready", "passed")):
        return "ok"
    return "dim"


def _draw_navigation(window: curses.window, index: int, separator: str, width: int) -> None:
    """Tabs in `muted`; the active tab is the one `accent` element of the row."""
    text, entries = _nav_layout(index, width, separator)
    _put(window, 2, 0, text, width, role="muted", preserve_spacing=True)
    for item, offset, label in entries:
        if item == index:
            _put(window, 2, offset, label, min(len(label), width-offset), role="accent")


_LABEL_WIDTHS = {"Overview": _BRIDGE_LABEL, "Health": _SYSTEM_LABEL}


def _mission_status_role(status: str) -> str:
    """State role for a mission status word in the header."""
    look = _state_look(status)
    return look[1] if look else "muted" if status.upper() in {"LOADING"} else "info"


def _header_segments(state: _ConsoleState, width: int) -> list[Segment]:
    """`LLM BRIDGE · ▌ name · ● STATE`: wordmark, mission chip and name, state glyph and word."""
    mission = _mission(state.snapshot)
    if state.snapshot:
        status = _text(mission.get("status", "unknown"), 20).upper()
    else:
        status = "UNAVAILABLE" if state.snapshot_error else "LOADING"
    context = _dict(state.snapshot.get("station_context"))
    if status == "PAUSED" and context.get("executing_operation") and str(context.get("operation_status", "")).upper() in ACTIVE_OPERATION_STATUSES:
        status = "PAUSING"
    shown = _state_text(status)
    room = width - len("LLM BRIDGE") - 2 * len(_SEP) - len(shown) - 2
    name = _clip(_text(mission.get("name") or state.mission_id, 120), max(8, room))
    chip_role = _mission_role_of(state.snapshot, state.mission_id)
    return [
        ("LLM BRIDGE", "brand"), (_SEP, "dim"), (glyph("chip"), chip_role), (" " + name, "info"), (_SEP, "dim"),
        (shown, _mission_status_role(status)),
    ]


def _header(state: _ConsoleState, width: int) -> str:
    return _segments_text(_header_segments(state, width))


def _draw_header(window: curses.window, state: _ConsoleState, width: int) -> None:
    _put_segments(window, 0, 0, _header_segments(state, width), width)


def _scroll_start(selected: int, row_count: int, visible_rows: int) -> int:
    return max(0, min(selected - visible_rows // 2, max(0, row_count - visible_rows)))


def _orders_summary(state: _ConsoleState) -> str:
    tasks = _items(state.snapshot.get("tasks"))
    total = state.snapshot.get("task_total")
    filtered = f"{_SEP}filter: {_text(state.search_query, 40)}{_SEP}Esc clears" if state.search_query else ""
    if isinstance(total, int):
        first = state.task_offset + 1 if tasks else 0
        last = state.task_offset + len(tasks)
        return f"Tasks: {total} total{filtered}{_SEP}showing {first}-{last}"
    return f"Tasks: {len(tasks)} shown{filtered}"


def _command_display(command: str, width: int) -> str:
    if width <= 0:
        return ""
    start = len(command)
    while start > 0 and cell_width(command[start - 1:]) <= width:
        start -= 1
    return command[start:]


def _input_prompt(state: _ConsoleState) -> str:
    return "Filter: " if state.palette_filter_editing else "/? " if state.search_editing else glyph("prompt") + " "


def _placeholder(state: _ConsoleState) -> str:
    """Dim guidance shown in an empty, unfocused command line."""
    if state.command_pending:
        return "running a command — wait for its result"
    if state.view == "Actions":
        return "type a command · Ctrl-K filters actions"
    return "type a command · /help · Ctrl-K actions"


def _display_input(state: _ConsoleState, width: int) -> tuple[str, int]:
    source = state.palette_filter if state.palette_filter_editing else state.command
    cursor = len(source) if state.palette_filter_editing else state.command_cursor
    return input_view(source, cursor, width)


def _draw_command_line(window: curses.window, y: int, state: _ConsoleState, width: int) -> None:
    """`›` prompt: `accent` while the field has focus, `muted` otherwise; typed text stays default."""
    prompt = _input_prompt(state)
    focused = state.input_focused or state.palette_filter_editing
    typed = state.palette_filter if state.palette_filter_editing else state.command
    if typed or focused:
        used = _put(window, y, 0, prompt, width, role="accent")
        _put(window, y, used, _display_input(state, width - cell_width(localize(prompt)))[0], width - used, role="info")
        return
    used = _put(window, y, 0, prompt, width, role="muted")
    if used < width:
        _put(window, y, used, _placeholder(state), width - used, role="dim", attr=curses.A_DIM)


def _hint_items(state: _ConsoleState) -> tuple[list[str], str]:
    """Keys that work in the current view, most useful first, plus the closing hint."""
    if state.palette_filter_editing:
        if state.palette_quick:
            return ["type to filter actions", "↑↓ choose", "Enter run", "Esc cancel"], ""
        return ["type to filter actions", "Enter or Esc finishes"], ""
    if state.search_editing:
        return ["type text to find in tasks", "Enter search", "Esc cancel", "Ctrl-U clear"], ""
    if state.input_focused:
        return ["Enter run", "Tab complete", "Ctrl-P/Ctrl-N history", "Ctrl-U clear", "Esc back to view"], "/help commands"
    views = "Tab/F1-8 views"
    # The empty command line already says "type a command or /help".
    view = state.view
    if view == "Overview":
        items = ["o objective", "Ctrl-E add task", views, "Ctrl-D exit", "Ctrl-K actions"]
        if _inside_station():
            items.insert(0, "Ctrl-] P provider")
            if not state.full:
                items.append("Ctrl-] z expand")
    elif view == "Tasks" and state.task_detail:
        items = ["Esc back", "↑↓ scroll", "v view prompt", "Ctrl-R amend", "PgUp/PgDn page", views, "Ctrl-D exit"]
    elif view == "Tasks":
        items = ["Enter inspect", "Ctrl-E add task", "Ctrl-R amend", "v view prompt", "Ctrl-F find", "↑↓ select", views, "Ctrl-D exit", "PgUp/PgDn page"]
        if state.search_query:
            items.insert(0, "Esc clear filter")
    elif view == "Providers":
        items = ["Enter mark viewed", "↑↓ select", views, "Ctrl-D exit"]
    elif view == "Activity":
        items = ["PgUp older", "PgDn newer", "Ctrl-T show all" if state.hide_plumbing else "Ctrl-T hide bookkeeping", "↑↓ scroll", views, "Ctrl-D exit"]
    elif view == "Actions":
        items = ["Enter run action", "↑↓ select", "Ctrl-K filter", views, "Ctrl-D exit", "Ctrl-G command line"]
    elif view == "Usage":
        items = ["↑↓ scroll", "l legend", "Shift-←/→ pan", views, "Ctrl-D exit"]
    else:
        items = ["↑↓ scroll", views, "Ctrl-D exit"]
    return items, "? help"


def _hint_chosen(state: _ConsoleState, width: int) -> list[str]:
    """The hint items that fit `width` (lowest-value ones dropped first), closing hint last."""
    items, closing = _hint_items(state)
    chosen = list(items)
    while chosen:
        parts = [*chosen, closing] if closing else chosen
        if cell_width(localize(_SEP.join(parts))) <= width:
            return parts
        chosen.pop()
    return [_clip(closing or items[0], width)]


def _hint(state: _ConsoleState, width: int = 100) -> str:
    """`key action · key action · ? help`, dropping the lowest-value items to fit."""
    return _SEP.join(_hint_chosen(state, width))


def _hint_segments(state: _ConsoleState, width: int) -> list[Segment]:
    """Hints as segments: key names in `key`, verbs in `muted`, separators in `dim`."""
    out: list[Segment] = []
    for index, item in enumerate(_hint_chosen(state, width)):
        if index:
            out.append((_SEP, "dim"))
        key, verb = _split_hint(item)
        if key:
            out.append((key, "key"))
            if verb:
                out.append((" " + verb, "muted"))
        else:
            out.append((item, "muted"))
    return out


def _handle_mouse_event(
    state: _ConsoleState,
    y: int,
    x: int,
    height: int,
    width: int,
    *,
    activate: bool = False,
) -> None:
    state.handle_mouse(y, x, height, width)
    if activate:
        state.handle_key("KEY_ENTER")


def _row_role(view: str, text: str) -> str:
    """Semantic color for an informational row."""
    lowered = text.lower()
    if view == "Overview":
        if text.startswith("Needs you"):
            return "warn"
        if text.startswith("Controller"):
            if any(word in lowered for word in ("stopped", "silent", "not started", "no heartbeat", "pausing", "finished")):
                return "warn"
            if "active" in lowered:
                return "ok"
    elif view == "Health" and text.startswith("Health") and ("warning" in lowered or "failed" in lowered):
        return "warn"
    return "info"


def _wrap_row(text: str, width: int, indent: int) -> list[str]:
    """Wrap one labelled row, hanging continuation lines under the value column.

    Lines break between ` · ` separated parts first, then between the
    comma-separated clauses of one part, so a duration such as `resets 6d 22h`
    is never split. Only a clause wider than the screen is broken by words.
    """
    if cell_width(text) <= width:
        return [text]
    width = max(indent + 8, width)
    pad = " " * indent
    pieces: list[tuple[str, str]] = []
    for unit_index, unit in enumerate(text.split(_SEP)):
        for clause_index, clause in enumerate(unit.split(", ")):
            pieces.append((clause, _SEP if clause_index == 0 and unit_index else ", "))
    lines: list[str] = []
    current = ""
    for piece, sep in pieces:
        if not current:
            current = piece
            continue
        candidate = current + sep + piece
        if cell_width(candidate) <= width:
            current = candidate
            continue
        lines.append(current + ("," if sep == ", " else ""))
        current = pad + piece
    lines.append(current)
    wrapped: list[str] = []
    for line in lines:
        if cell_width(line) <= width:
            wrapped.append(line)
        else:
            wrapped.extend(textwrap.wrap(line, width=width, subsequent_indent=pad, break_long_words=True, break_on_hyphens=False) or [line])
    return wrapped


def _working_text(state: _ConsoleState, now: float | None = None) -> str:
    """`⠋ working…` while a command awaits its durable result; static without colour support."""
    if not state.command_pending:
        return ""
    if bridge_theme.colors_disabled():
        return f"working{glyph('ellipsis')}"
    current = time.monotonic() if now is None else now
    bridge_redraw.note_wall(0.1)  # one spinner frame
    frames = spinner_frames()
    frame = frames[int(max(0.0, current - state.working_since) * 10) % len(frames)]
    return f"{frame} working{glyph('ellipsis')}"


def _status_line(state: _ConsoleState, width: int) -> tuple[str, str]:
    """(text, role) for the bottom status row: error, working, latest message, else key hints."""
    if state.snapshot_error:
        return state.snapshot_error, "error"
    working = _working_text(state)
    if working:
        return working, "info"
    message = state.status_message()
    if message:
        return message, _status_role(message)
    return _hint(state, width), "dim"


def _status_line_segments(state: _ConsoleState, width: int) -> list[Segment]:
    """Bottom row: an error, the working indicator, a command result, else the key hints."""
    if state.snapshot_error:
        return [(state.snapshot_error, "error")]
    working = _working_text(state)
    if working:
        return [(working, "info")]
    message = state.status_message()
    if message:
        return [(message, _status_role(message))]
    return _hint_segments(state, width)


def _draw_status_lines(window: curses.window, state: _ConsoleState, prompt_y: int, hint_y: int, width: int) -> None:
    _draw_command_line(window, prompt_y, state, width)
    _put_segments(window, hint_y, 0, _clip_segments(_status_line_segments(state, width), width), width)


def _place_cursor(window: curses.window, state: _ConsoleState, prompt_y: int, width: int, *, visible: bool = True) -> None:
    if visible and (state.input_focused or state.palette_filter_editing):
        available = max(1, width - cell_width(_input_prompt(state)))
        _, caret = _display_input(state, available)
        try:
            curses.curs_set(1)
            window.move(prompt_y, min(width - 1, cell_width(_input_prompt(state)) + caret))
        except curses.error:
            pass
    else:
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def _strip_segments(state: _ConsoleState, width: int) -> list[Segment]:
    """Line 1 of the control view: provider square, then the status text with its parts coloured."""
    snapshot = state.snapshot
    if state.snapshot_error and not snapshot:
        return [("status unavailable", "error")]
    if state.saved_notice:
        saved = f"saved as {_short_id(state.mission_id)}"
        if str(_mission(snapshot).get("status") or "").upper() == "CREATED":
            saved += f"{_SEP}type /start"
        return _inline_segments(saved, "ok")
    owner = ""
    if str(_dict(snapshot.get("current_task")).get("status") or "").upper() == "RUNNING":
        owner = _active_owner(snapshot)
    text, role = _status_strip(snapshot, width - (2 if owner else 0))
    total = snapshot.get("task_total")
    if state.view == "Tasks" and (state.search_query or (isinstance(total, int) and total > len(_items(snapshot.get("tasks"))))):
        return _inline_segments(_orders_summary(state), "muted")  # searching or paging: say what is shown
    base = "error" if role == "error" else "muted" if role in {"dim", "secondary"} else "info"
    square: list[Segment] = [(glyph("square"), _provider_role(owner)), (" ", "info")] if owner else []
    return [*square, *_inline_segments(text, base, context="strip")]


_TASK_ROW = re.compile(r"^task (?P<id>\S+)  (?P<state>(?:\S )?[A-Z]+)  (?P<title>.*?)(?P<idle>" + re.escape(_SEP) + r"no output for .*)?$")


def _task_row_segments(value: str) -> list[Segment] | None:
    """`task 2  ● RUNNING  title · no output for 2m`: id muted, state coloured, title untouched."""
    match = _TASK_ROW.match(value)
    if not match:
        return None
    word = match.group("state").split()[-1]
    look = _state_look(word)
    finished = word in _FINISHED_STATES
    out: list[Segment] = [
        ("task ", "info"), (match.group("id"), "muted"), ("  ", "info"),
        (match.group("state"), look[1] if look else "info"), ("  ", "info"),
        (match.group("title"), "muted" if finished else "info"),
    ]
    if match.group("idle"):
        out += [(_SEP, "dim"), (match.group("idle")[len(_SEP):], _idle_role(match.group("idle")))]
    return out


def _value_segments(view: str, label: str, value: str) -> list[Segment]:
    """Colour the value of one labelled Overview or Health row."""
    if not value.strip():
        return [(value, "info")]
    if view == "Overview":
        if label == "Controller":
            if value.lstrip().startswith("failed —"):
                return [(value, "error")]
            return _inline_segments(value, "info", context="controller")
        if label in {"Now", "Next"}:
            return _task_row_segments(value) or [(value, "info")]
        if label == "Tasks":
            return _inline_segments(value, "info", context="tasks")
        if label == "Needs you":
            return _inline_segments(value, "info", context="needs")
        if label == "Recent":
            match = re.match(r"^(\s*)([\d-]{2}:[\d-]{2}:[\d-]{2})  (.*)$", value)
            if match:
                return [(match.group(1), "info"), (match.group(2), "muted"), ("  ", "info"),
                        (match.group(3), "error" + QUIET if _activity_role(match.group(3)) == "error" else "info")]
            return [(value, "info")]
    base = "warn" if view == "Health" and label == "Health" and _row_role(view, "Health " + value) == "warn" else "info"
    return _inline_segments(value, base)


def _labelled_segments(view: str, line: str, label_width: int, label: str) -> tuple[list[Segment], str]:
    """Segments of one wrapped line in a labelled view, and the label it belongs to."""
    head, value = line[:label_width], line[label_width:]
    if head.strip():
        label = head.strip()
    if not line.strip():
        return [(line, "info")], label
    label_role = "state_review" if label == "Needs you" else "heading"
    # Pad outside the label so an underline or reverse attribute never runs across the gap.
    segments: list[Segment] = [(head.rstrip(), label_role), (head[len(head.rstrip()):], "info")] if head.strip() else [(head, "info")]
    segments.extend(_value_segments(view, label, value))
    return segments, label


def _framed(draw: Callable[[curses.window, _ConsoleState], None]) -> Callable[[curses.window, _ConsoleState], None]:
    """Let one frame share expensive row projections between its helpers."""
    @functools.wraps(draw)
    def wrapper(window: curses.window, state: _ConsoleState) -> None:
        state._draw_cache = {}
        try:
            draw(window, state)
        finally:
            state._draw_cache = None
    return wrapper


@_framed
def _draw_compact(window: curses.window, state: _ConsoleState) -> None:
    sync_window_size(window)
    height, width = window.getmaxyx()
    window.erase()
    _draw_header(window, state, width)
    rows = state.rows()
    if height >= 2:
        forecasts = _capacity_summaries(state.snapshot)
        if state.view == "Tasks":
            first = _inline_segments(_clip(_orders_summary(state), width), "muted")
        elif forecasts:
            first = _inline_segments(_clip(forecasts[0], width), "info")
        else:
            first = _inline_segments(_clip("Controller " + _controller_line(state.snapshot), width), "info", context="controller")
        _put_segments(window, 1, 0, first, width)
    if height >= 3:
        _draw_navigation(window, state.view_index, " ", width)
    if height >= 4:
        selected = state.selected_row() if state.view == "Providers" else state.selected
        detail = rows[min(selected, max(0, len(rows) - 1))] if rows else ""
        if rows and state.selectable:
            _put(window, 3, 0, _clip(detail, width), width, role="selected")
        else:
            _put_segments(window, 3, 0, _clip_segments(_inline_segments(detail, "info"), width), width)
    if height >= 5:
        _draw_command_line(window, 4, state, width)
    if height >= 6:
        _put_segments(window, 5, 0, _clip_segments(_status_line_segments(state, width), width), width)
    _place_cursor(window, state, 4, width, visible=height >= 6)
    window.noutrefresh()
    curses.doupdate()


@_framed
def _draw_full(window: curses.window, state: _ConsoleState) -> None:
    sync_window_size(window)
    height, width = window.getmaxyx()
    window.erase()
    _draw_header(window, state, width)
    if height >= 2:
        _put_segments(window, 1, 0, _clip_segments(_strip_segments(state, width), width), width)
    if height >= 3:
        _draw_navigation(window, state.view_index, "  ", width)
    gutter = 2
    avail = max(1, width - gutter)
    state.content_width = avail
    rows = state.rows()
    header = state.column_header()
    content_top = 3
    content_height = max(0, height - 6)
    if header is not None and content_height > 1:
        _put(window, content_top, gutter, cell_slice(header, 0, avail), avail, role="dim")
        content_top += 1
        content_height -= 1
    label_width = _LABEL_WIDTHS.get(state.view)
    display = [line for row in rows for line in _wrap_row(row, avail, label_width)] if label_width else rows
    if state.view == "Overview":
        free = content_height - len(display)
        source = state.snapshot.get("operator_events")
        events = _items(source if source is not None else state.snapshot.get("events"))
        recent = _recent_event_rows(events, avail, free - 1, _task_numbers(state.snapshot)) if state.event_offset == 0 else []
        if recent:
            display = [*display, "", *recent]
    preview_task = state.selected_task() if state.view == "Tasks" and not state.task_detail else None
    list_height = _orders_list_height(content_height, width, len(rows)) if preview_task is not None else None
    visible = list_height if list_height is not None else content_height
    if state.selectable:
        selected_row = state.detail_scroll if state.view == "Tasks" and state.task_detail else state.selected_row()
        start = _scroll_start(selected_row, len(display), visible)
    else:
        selected_row = -1
        state._scroll_limit = max(0, len(display) - content_height)
        start = max(0, min(state.selected, state._scroll_limit))
        state.selected = start
    wrapped_view = label_width is not None
    roles = state.row_roles()
    segment_rows = state.row_segments() if not wrapped_view else None
    horizontal = 0
    if state.view == "Usage":
        widest = max((cell_width(line) for line in display), default=0)
        state.usage_hscroll = horizontal = max(0, min(state.usage_hscroll, widest - avail))
    label = ""
    for before in display[:start]:  # the label a scrolled-in continuation line belongs to
        if label_width and before[:label_width].strip():
            label = before[:label_width].strip()
    for offset, value in enumerate(display[start : start + visible]):
        y = content_top + offset
        index = start + offset
        if state.view == "Usage":
            clean = "".join(char if unicodedata.category(char) not in {"Cc", "Cs"} else " " for char in value)
            if index == 0:
                role = _status_role(clean)  # the status line carries the colour
            elif _USAGE_HEADER.match(clean):
                role = "heading"
            elif clean.strip() and set(clean.strip()) <= {"─", "-", " "}:
                role = "dim"
            else:
                role = "info"
            _put(window, y, gutter, cell_slice(clean, horizontal, avail), avail, role=role)
            continue
        value = _clip(value, avail) if not wrapped_view else value
        if state.selectable and index == selected_row:
            _put(window, y, 0, "> " + value, width, role="selected")
        elif label_width:
            segments, label = _labelled_segments(state.view, value, label_width, label)
            _put_segments(window, y, gutter, segments, avail)
        elif segment_rows is not None and index < len(segment_rows):
            _put_segments(window, y, gutter, _clip_segments(segment_rows[index], avail), avail)
        else:
            role = roles[index] if roles is not None and index < len(roles) else _row_role(state.view, value)
            _put(window, y, gutter, value, avail, role=role)
    if preview_task is not None and list_height is not None:
        separator_y = content_top + list_height
        _put_segments(window, separator_y, gutter, _order_separator_segments(preview_task, avail), avail)
        for offset, (label_text, value, value_role) in enumerate(_order_preview_lines(preview_task, avail, content_height - list_height - 1)):
            y = separator_y + 1 + offset
            _put(window, y, gutter, f"{label_text:<{_BRIDGE_LABEL}}", avail, role="muted")
            _put(window, y, gutter + _BRIDGE_LABEL, value, max(0, avail - _BRIDGE_LABEL), role=value_role)
    if height >= 2:
        _draw_status_lines(window, state, height - 2, height - 1, width)
        _place_cursor(window, state, height - 2, width)
    window.noutrefresh()
    curses.doupdate()


def _response_rows(state: _ConsoleState, width: int) -> list[str]:
    available = max(1, width - 2)
    rows: list[str] = []
    for line in state.output:
        safe = "".join(
            char if unicodedata.category(char) not in {"Cc", "Cs"} else " "
            for char in line
        )
        rows.extend(textwrap.wrap(
            safe,
            width=available,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""])
    return rows or ["No command response."]


def _draw_response_overlay(window: curses.window, state: _ConsoleState) -> None:
    """Show bounded multiline help/query output without losing input context."""
    sync_window_size(window)
    height, width = window.getmaxyx()
    window.erase()
    if state.confirm_command is not None:
        _put(window, 0, 0, "Confirm", width, role="heading")
        # The "Y confirm · N or Esc cancel" hint is part of the response rows,
        # directly after the question; showing it here as well would repeat it.
        for offset, value in enumerate(_response_rows(state, width)[: max(0, height - 1)]):
            if value.startswith("Y "):  # the choice: keys in `key`, verbs `muted`
                segments: list[Segment] = []
                for index, item in enumerate(value.split(_SEP)):
                    key, verb = _split_hint(item.strip())
                    segments += ([(_SEP, "dim")] if index else []) + ([(key, "key"), (" " + verb, "muted")] if key else [(item, "muted")])
                _put_segments(window, 1 + offset, 0, segments, width)
                continue
            _put(window, 1 + offset, 0, value, width, role="warn" if offset == 0 else "info", preserve_spacing=True)
        window.noutrefresh()
        curses.doupdate()
        return
    _put(window, 0, 0, "Command response", width, role="heading")
    if height >= 2:
        _put(window, 1, 0, "Arrows/PgUp/PgDn scroll | Esc/Enter close | F1-8/Tab switch view", width, role="dim")
    rows = _response_rows(state, width)
    content_top = 2
    footer_rows = 1 if height >= 4 else 0
    content_height = max(0, height - content_top - footer_rows)
    maximum_scroll = max(0, len(rows) - content_height)
    state.response_scroll = min(maximum_scroll, max(0, state.response_scroll))
    state.response_page_size = max(1, content_height)
    for offset, value in enumerate(rows[state.response_scroll : state.response_scroll + content_height]):
        if width <= 0:
            break
        safe = "".join(
            char if unicodedata.category(char) not in {"Cc", "Cs"} else " "
            for char in value
        )
        _put(window, content_top + offset, 0, safe, width, role=_status_role(safe), preserve_spacing=True)
    if footer_rows:
        if rows:
            visible_start = state.response_scroll + 1 if content_height else 0
            visible_end = min(len(rows), state.response_scroll + content_height)
            counter = f"{visible_start}-{visible_end}/{len(rows)}" if content_height else f"0/{len(rows)}"
        else:
            counter = "0/0"
        _put(window, height - 1, 0, f"{counter} response lines", width, role="dim")
    window.noutrefresh()
    curses.doupdate()


def _draw_document(window: curses.window, state: _ConsoleState) -> None:
    viewer = state.document_viewer
    if viewer is None:
        return
    sync_window_size(window)
    height, width = window.getmaxyx()
    viewer.resize(width)
    window.erase()
    mode = "rendered" if viewer.rendered else "source"
    rich_rows = viewer.rich_lines(width) if viewer.rendered else ()
    degraded = " · plain source" if viewer.rendered and rich_rows and rich_rows[0][0][0].startswith("[plain source") else ""
    _put(window, 0, 0, f"{viewer.title} · read-only · {mode}{degraded}", width, role="heading")
    content_height = max(1, height - 2)
    if state.document_heading_list:
        headings = viewer.headings()
        rows = [f"{'>' if index == state.document_heading_selected else ' '} {number}: {label}"
                for index, (number, label, _visual) in enumerate(headings)] or ["No Markdown headings in this source."]
        first = max(0, min(state.document_heading_selected, len(rows) - content_height))
        rows = rows[first:first + content_height]
    else:
        rows = viewer.visible(content_height, width)
    for index, value in enumerate(rows):
        if state.document_heading_list:
            _put(window, index + 1, 0, value, width, role="selected" if value.startswith(">") else "info", preserve_spacing=True)
        elif viewer.rendered:
            spans = rich_rows
            row = spans[viewer.top + index] if viewer.top + index < len(spans) else ()
            x = 0
            skipped = viewer.horizontal if not viewer.wrap else 0
            for text, role in row:
                if skipped >= cell_width(text):
                    skipped -= cell_width(text)
                    continue
                text = cell_slice(text, skipped, max(0, width-x))
                skipped = 0
                amount = min(width - x, cell_width(text))
                if amount <= 0:
                    break
                _put(window, index + 1, x, text, amount, role=role, preserve_spacing=True)
                x += amount
        else:
            _put(window, index + 1, 0, value, width, role="info", preserve_spacing=True)
    if viewer.rendering:
        bridge_redraw.note_wall(0.15)  # a render or layout is running; repaint when it lands
    footer = (f"q close | {viewer.top + 1}/{max(1, viewer.layout_rows)} {viewer.source_size}B "
              f"{viewer.search_count} matches | / find m mode w wrap s export")
    if state.document_heading_list:
        footer = "Headings · Up/Down select · Enter go · Esc close"
    elif state.document_notice:
        footer = "q close | " + state.document_notice
    if state.document_export_editing:
        footer = "Export original source to new path: " + state.document_entry
        if state.document_notice:
            _put(window, max(0, height-2), 0, state.document_notice, width, role="error")
    elif state.document_search_editing:
        footer = "/" + viewer.query
    _put(window, height - 1, 0, footer, width, role="dim", preserve_spacing=True)
    try:
        editing = state.document_export_editing or state.document_search_editing
        curses.curs_set(1 if editing else 0)
        if editing:
            label = "Export: " if state.document_export_editing else "/"
            source = state.document_entry if state.document_export_editing else viewer.query
            visible, caret = input_view(source, len(source), max(1, width-cell_width(label)))
            _put(window, height-1, 0, label+visible, width, role="command", preserve_spacing=True)
            window.move(height-1, min(width-1, cell_width(label)+caret))
    except (curses.error, AttributeError):
        pass
    window.noutrefresh()
    curses.doupdate()


def _edit_task_prompt(
    window: curses.window,
    state: _ConsoleState,
    task_id: str | None = None,
    *,
    initial_text: str = "",
) -> None:
    """Edit multiline task text in a private file, then dispatch it by path."""
    if state.command_pending:
        state.output.append("Wait for the current command before opening the editor.")
        return
    try:
        from .bridge_store import default_state_dir

        draft_dir = default_state_dir() / "drafts"
        draft_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(draft_dir, 0o700)
        fd, raw_path = tempfile.mkstemp(prefix="llm-bridge-task-", suffix=".md", dir=draft_dir)
    except OSError as exc:
        state.output.append(f"Task editor could not create a private file: {_text(exc, 160)}")
        return
    path = Path(raw_path)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("task editor file is not private and owned by this user")
        os.close(fd)
        if task_id is not None:
            # Display projections are intentionally bounded and sanitized.
            # Mutation input must come from the full verbatim public record.
            record = json.loads(state.dispatch(state.mission_id, shlex.join(["task", "show", *_task_ref(state.snapshot, task_id), "--json"])))  # type: ignore[arg-type]  # the dispatcher returns object; this call expects the JSON text of `task show`
            if not isinstance(record, dict) or str(task_number(record)) != task_id or not isinstance(record.get("prompt"), str):
                raise ValueError("could not load the complete task prompt; amendment cancelled")
            initial_text = record["prompt"]
        if initial_text:
            with path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(initial_text)
        editor_setting = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        editor = shlex.split(editor_setting)
        if not editor:
            raise ValueError("VISUAL/EDITOR must name an executable")

        curses.def_prog_mode()
        curses.endwin()
        try:
            result = subprocess.run([*editor, str(path)], check=False, cwd=draft_dir)
        finally:
            curses.reset_prog_mode()
            window.clear()
            window.refresh()
        if result.returncode != 0:
            state.output.append(f"Editor exited with status {result.returncode}; task was not dispatched.")
            path.unlink(missing_ok=True)
            return
        if path.stat().st_size == 0:
            state.output.append("Empty task text was not dispatched.")
            path.unlink(missing_ok=True)
            return
        if task_id is None and state.command.strip():
            # The CLI parser owns grammar and validation. The editor only
            # supplies the file argument, preserving every typed option.
            argv = [*shlex.split(state.command), "--file", str(path)]
        else:
            argv = ["add", str(path)] if task_id is None else ["task", "amend", *_task_ref(state.snapshot, task_id), "--file", str(path)]
        if not state._dispatch(shlex.join(argv), cleanup_path=path):
            path.unlink(missing_ok=True)
        else:
            state.command = ""
    except Exception as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass
        state.output.append(f"Task editor failed: {_text(exc, 180)}")


def _compose_task_prompt(
    window: curses.window,
    state: _ConsoleState,
    *,
    task_id: str | None = None,
    initial_text: str = "",
) -> None:
    """Compose task source in Bridge, then submit it through the public CLI grammar."""
    mission = _mission(state.snapshot)
    mission_status = str(mission.get("status", "unknown"))
    if task_id is None and mission_status in FINISHED_MISSION_STATUSES:
        state.output.append(f"Cannot add a task: mission is {mission_status.lower()}. Choose another mission.")
        return
    if task_id is not None:
        task = next((item for item in _items(state.snapshot.get("tasks")) if str(task_number(item)) == task_id), {})
        if str(task.get("status", "")).upper() in {"COMPLETED", "FAILED", "CANCELLED"}:
            state.output.append(f"Cannot amend task {task_id}: it is {str(task.get('status')).lower()}. Choose an editable task.")
            return
    try:
        source = compose_document(
            initial_text,
            title=f"Amend task {task_id} prompt" if task_id else "Add task prompt",
            window=window,
        )
    finally:
        window.clear()
        window.refresh()
    if source is None:
        if LAST_RECOVERY_DRAFT is not None:
            state.output.append(f"Task source retained for recovery: {LAST_RECOVERY_DRAFT}")
        return
    from .bridge_store import default_state_dir

    draft_dir = default_state_dir() / "drafts"
    fd: int | None = None
    try:
        draft_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(draft_dir, 0o700)
        fd, name = tempfile.mkstemp(prefix="task-", suffix=".md", dir=draft_dir)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(source)
        os.chmod(name, 0o600)
    except OSError as exc:
        try:
            if fd is not None:
                os.close(fd)
        except OSError:
            pass
        state.edit_initial_text = source
        state.output.append(f"Could not save task draft: {_text(exc, 100)}. Recovery source: {LAST_RECOVERY_DRAFT}")
        return
    path = Path(name)
    if task_id is not None:
        command = ["task", "amend", *_task_ref(state.snapshot, task_id), "--file", str(path)]
    else:
        try:
            typed = shlex.split(state.command)
        except ValueError:
            typed = []
        command = typed if typed[:2] == ["task", "add"] else ["task", "add"]
        if "--file" not in command and "--prompt" not in command:
            command.extend(["--file", str(path)])
        else:
            command = ["task", "add", "--file", str(path)]
    checkpoint = LAST_RECOVERY_DRAFT
    cleanup_paths = tuple(dict.fromkeys(item for item in (path, checkpoint) if item is not None))
    if not state._dispatch(shlex.join(command), cleanup_paths=cleanup_paths):
        state.output.append(f"Task source retained for recovery: {path}")
    else:
        state.command = ""
        state.command_cursor = 0
        state.input_focused = False


def _embedded_usage_snapshot() -> dict[str, object]:
    from .bridge_usage import embedded_usage

    return embedded_usage.snapshot()


def _selected_task_id(state: _ConsoleState) -> str | None:
    tasks = _items(state.snapshot.get("tasks"))
    if not tasks:
        return None
    index = min(max(0, state.selected), len(tasks) - 1)
    value = task_number(tasks[index])
    return _text(value, 40) if value is not None else None


def _terminal_outcome(source_mission: str, action: TerminalAction) -> str:
    """Short line recorded after a terminal action returns control to this console."""
    if action.action == "detach":
        return "Detached; work keeps running."
    if action.mission_id == source_mission:
        return "Returned from the provider station."
    return f"Switched to mission {_short_id(action.mission_id, 4)}."


def _run(mission_id: str, dispatch: Dispatch, snapshot: Snapshot, *, full: bool, initial_view: str = "Overview") -> int:
    # Ignore stale exported dimensions from a detached tmux station.
    curses.use_env(False)
    state = _ConsoleState(mission_id, dispatch, snapshot, full)
    global JUST_SAVED_MISSION
    if JUST_SAVED_MISSION == mission_id:
        state.saved_notice, JUST_SAVED_MISSION = True, None
    probe: bridge_redraw.StoreChangeProbe | None = None
    if getattr(snapshot, "__name__", "") == "console_snapshot":
        state.usage_reader = _embedded_usage_snapshot  # only the real projection; test doubles stay hermetic
        from .bridge_store import store_path

        probe = bridge_redraw.StoreChangeProbe(store_path().expanduser().absolute())
        state.change_probe = probe
    paste_decoder = BracketedPaste()
    paste_candidate = bytearray()
    paste_activity = 0.0
    replay_keys: deque[object] = deque()
    escape_sequence = False
    status_publication: dict[str, object] = {"applied": None, "thread": None}
    state.choose_view(VIEWS.index(normalize_view(initial_view)))
    state.refresh(force=True)

    def complete_console_paste(source: str) -> None:
        if any(char in source for char in "\r\n") or len(source.encode("utf-8")) > _MAX_COMMAND_CHARS or any(ord(char) < 32 for char in source):
            state.paste_source = source
            state.output.append("Pasted source opened in the task composer; save it or keep its private recovery draft.")
            return
        command = TextBuffer(state.command, state.command_cursor, _MAX_COMMAND_CHARS)
        accepted, _received = command.paste(source)
        if not accepted:
            state.output.append(f"Pasted command is over the {_MAX_COMMAND_CHARS}-byte command limit; previous input is unchanged.")
            return
        state.command = command.text
        state.command_cursor = command.cursor
        state.input_focused = True

    def feed_console_paste(key: object) -> bool:
        """Consume bracketed-paste framing and atomically route completed source."""
        nonlocal paste_activity, escape_sequence
        if escape_sequence and key == "\x1b":
            escape_sequence = False  # ESC aborts an unfinished sequence and starts the next one (a paste marker may follow)
        if escape_sequence:
            if isinstance(key, str):
                paste_activity = time.monotonic()
                if any("@" <= char <= "~" for char in key):
                    escape_sequence = False
            return True
        if not isinstance(key, str):
            if paste_decoder.active:
                return True
            return False
        encoded = key.encode("utf-8")
        if paste_decoder.active:
            paste_activity = time.monotonic()
            pasted = paste_decoder.feed(encoded)
            if pasted is not None:
                complete_console_paste(pasted)
                paste_decoder.reset()
            elif paste_decoder.done and paste_decoder.error:
                state.output.append(paste_decoder.error + "; command input is unchanged")
                paste_decoder.reset()
            return True
        if paste_candidate:
            previous = bytes(paste_candidate)
            paste_candidate.extend(encoded)
            if BracketedPaste.START.startswith(paste_candidate):
                if bytes(paste_candidate) == BracketedPaste.START:
                    paste_decoder.feed(bytes(paste_candidate))
                    paste_candidate.clear()
                    paste_activity = time.monotonic()
                return True
            # A bare Escape followed by ordinary input is two keystrokes, not
            # an escape sequence. Apply Escape, then replay the following key.
            paste_candidate.clear()
            if previous == b"\x1b" and key == "O":
                escape_sequence = True
            elif previous == b"\x1b":
                state.handle_key("\x1b")
                replay_keys.append(key)
            elif previous.startswith((b"\x1b[", b"\x1bO")):
                state.output.append("Unknown escape sequence ignored.")
                if key == "\x1b":
                    paste_candidate.extend(encoded)  # ESC aborts the unfinished sequence and starts the next one
                else:
                    escape_sequence = not (len(key) == 1 and "@" <= key <= "~")
            elif not isinstance(key, str):
                replay_keys.append(key)
            else:
                state.output.append("Unknown or incomplete escape sequence ignored.")
            return True
        if encoded == b"\x1b":
            paste_candidate.extend(encoded)
            paste_activity = time.monotonic()
            return True
        if encoded.startswith(BracketedPaste.START):
            paste_decoder.feed(BracketedPaste.START)
            paste_activity = time.monotonic()
            remainder = encoded[len(BracketedPaste.START):]
            if remainder:
                pasted = paste_decoder.feed(remainder)
                if pasted is not None:
                    complete_console_paste(pasted)
                    paste_decoder.reset()
                elif paste_decoder.done and paste_decoder.error:
                    state.output.append(paste_decoder.error + "; command input is unchanged")
                    paste_decoder.reset()
            return True
        return False

    def loop(window: curses.window) -> int | TerminalAction | ControlViewAction:
        nonlocal escape_sequence
        initialize_colors()
        window.keypad(True)
        paste_keypad = True
        _set_escape_delay()
        try:
            curses.nonl()
        except curses.error:
            pass
        bridge_hangup.set_timeout(window, 100)
        last_draw = 0.0
        planner = bridge_redraw.RedrawPlanner()
        drawn_size: tuple[int, int] | None = None
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        bridge_mouse.enable_mouse()
        while True:
            wanted_keypad = not paste_decoder.active
            if wanted_keypad != paste_keypad:
                window.keypad(wanted_keypad)
                paste_keypad = wanted_keypad
            state.full = full or window.getmaxyx()[0] > 8
            if state.paste_source is not None:
                source, state.paste_source = state.paste_source, None
                _compose_task_prompt(window, state, initial_text=source)
                state.refresh(force=True)
                planner.mark()
                continue
            if state.edit_document_requested and not paste_decoder.active:
                state.edit_document_requested = False
                _compose_task_prompt(window, state, task_id=state.document_task_id, initial_text=state.edit_initial_text)
                state.edit_initial_text = ""
                state.refresh(force=True)
                planner.mark()
                continue
            if state.expand_requested and not paste_decoder.active:
                state.expand_requested = False
                if not state.full:
                    try:
                        from .bridge import _load_handle
                        from .terminal import TmuxTerminalBackend
                        TmuxTerminalBackend().focus(_load_handle(mission_id), pane="bridge", expand=True)
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                        state.output.append(f"Could not expand controls: {type(exc).__name__}")
            if not paste_decoder.active:
                state.refresh()
            if state.consume_dirty():
                planner.mark()
            sync_window_size(window)
            if window.getmaxyx() != drawn_size:
                planner.mark()  # resized (or never drawn)
            now = time.monotonic()
            paste_pacing = paste_decoder.active
            draw_due = now - last_draw >= .1 if paste_pacing else planner.should_draw(now)
            if draw_due and not paste_pacing:
                status_chip = _mission_role_of(state.snapshot, mission_id)
                status_context = f"{_station_status_context(state.snapshot)}\0{status_chip}"
                status_where, status_state = _station_status_parts(state.snapshot)
                status_segments = _station_status_segments(state.snapshot)
            else:
                status_context = ""
            if draw_due and not paste_pacing and os.environ.get("TMUX_PANE") and status_context != status_publication["applied"]:
                status_thread = status_publication["thread"]
                if not isinstance(status_thread, threading.Thread) or not status_thread.is_alive():
                    def publish_status(context: str = status_context, where: str = status_where, text: str = status_state,
                                       segments: list[Segment] = status_segments, chip: str = status_chip) -> None:
                        try:
                            from .bridge import _load_handle

                            terminal.publish_station_status(mission_id, _load_handle, where=where, text=text, chip=chip, segments=segments)
                        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                            pass
                        finally:
                            status_publication["applied"] = context

                    status_thread = threading.Thread(
                        target=publish_status,
                        name="llm-bridge-status-context",
                        daemon=True,
                    )
                    status_publication["thread"] = status_thread
                    status_thread.start()
            if state.terminal_action is not None and not paste_decoder.active:
                action, state.terminal_action = state.terminal_action, None
                return action
            if draw_due:
                if paste_decoder.active and not paste_decoder.interrupted:
                    state.output.append(f"Receiving paste · {paste_decoder.received_bytes+len(paste_decoder.pending)} bytes; no command dispatched")
                with bridge_redraw.collect() as timed:
                    if state.response_overlay:
                        _draw_response_overlay(window, state)
                    elif state.document_viewer is not None:
                        _draw_document(window, state)
                    elif state.full:
                        _draw_full(window, state)
                    else:
                        _draw_compact(window, state)
                last_draw = now
                drawn_size = window.getmaxyx()
                planner.drawn(timed.next_deadline(time.monotonic(), state.age_origin()))
            if replay_keys:
                key = replay_keys.popleft()
            else:
                wait = None
                now = time.monotonic()
                if paste_pacing or paste_decoder.active:
                    wait = 0.1
                else:
                    pending_timers = [
                        0.12 - (now - paste_activity)
                        for armed in (escape_sequence, bool(paste_candidate))
                        if armed
                    ]
                    wait = planner.wait(now, state.wake_in(now), *pending_timers)
                bridge_hangup.set_timeout(window, bridge_redraw.timeout_ms(wait))
                try:
                    key = bridge_hangup.read_wch(window)
                except curses.error:
                    key = None
            if key is not None:
                planner.mark()
                state.note_input()
            if key is None:
                if escape_sequence and time.monotonic()-paste_activity >= .12:
                    escape_sequence = False
                    state.output.append("Unknown or incomplete escape sequence ignored.")
                    planner.mark()
                if paste_candidate and time.monotonic() - paste_activity >= 0.12:
                    planner.mark()
                    if paste_candidate == b"\x1b":
                        paste_candidate.clear()
                        if not state.handle_key("\x1b"):
                            return 0
                    else:
                        paste_candidate.clear()
                        state.output.append("Unknown or incomplete escape sequence ignored.")
                    continue
                if paste_decoder.active and time.monotonic() - paste_activity >= 2.0:
                    if not paste_decoder.interrupted:
                        paste_decoder.interrupt()
                        state.output.append("Incomplete paste quarantined; input unchanged. Ctrl-D exits when input is empty.")
                        planner.mark()
                continue
            if paste_decoder.interrupted and key == "\x04" and not state.command and not state.command_pending:
                return 0
            if feed_console_paste(key):
                continue
            if key == "\x05" and state.document_viewer is None and not state.response_overlay and not state.input_focused:
                _compose_task_prompt(window, state)
                state.refresh(force=True)
                continue
            if key == "\x12" and state.document_viewer is None and not state.response_overlay and not state.input_focused:
                task = state.selected_task() if state.view == "Tasks" else None
                task_id = _selected_task_id(state) if task is not None else None
                if task_id is None or task is None:
                    state.output.append("Select a task in the Tasks view before amending it.")
                else:
                    state.request_task_document(task_id, for_edit=True)
                continue
            if key == curses.KEY_MOUSE:
                mouse = bridge_mouse.read_mouse()
                if mouse is None:
                    continue
                x, y, button_state = mouse
                wheel = bridge_mouse.wheel_delta(button_state)
                if wheel:
                    vertical, horizontal = wheel
                    if vertical:
                        state.wheel(vertical)
                    if horizontal:
                        state.hscroll(horizontal)
                elif button_state & curses.BUTTON1_CLICKED:
                    _handle_mouse_event(state, y, x, *window.getmaxyx())
                elif button_state & getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0):
                    _handle_mouse_event(state, y, x, *window.getmaxyx(), activate=True)
                continue
            semantic = {  # type: ignore[call-overload]  # projection values are typed object
                curses.KEY_UP: "KEY_UP",
                curses.KEY_DOWN: "KEY_DOWN",
                curses.KEY_LEFT: "KEY_LEFT",
                curses.KEY_RIGHT: "KEY_RIGHT",
                curses.KEY_BACKSPACE: "KEY_BACKSPACE",
                curses.KEY_ENTER: "KEY_ENTER",
                curses.KEY_BTAB: "KEY_BTAB",
                curses.KEY_RESIZE: "KEY_RESIZE",
                curses.KEY_PPAGE: "KEY_PPAGE",
                curses.KEY_NPAGE: "KEY_NPAGE",
                curses.KEY_HOME: "KEY_HOME",
                curses.KEY_END: "KEY_END",
                curses.KEY_DC: "KEY_DC",
                curses.KEY_SLEFT: "KEY_SLEFT",
                curses.KEY_SRIGHT: "KEY_SRIGHT",
            }.get(key, key)
            if isinstance(key, int) and curses.KEY_F0 < key <= curses.KEY_F0 + len(VIEWS):
                semantic = f"KEY_F{key - curses.KEY_F0}"
            if not state.handle_key(semantic):
                return 0
            if state.edit_document_requested:
                state.edit_document_requested = False
                _compose_task_prompt(window, state, task_id=state.document_task_id, initial_text=state.edit_initial_text)
                state.edit_initial_text = ""
                state.refresh(force=True)
                planner.mark()

    def terminal_loop(window: curses.window) -> int | TerminalAction | ControlViewAction:
        import termios

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
            return loop(window)
        finally:
            if original is not None:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, original)
                except (OSError, termios.error):
                    pass

    while True:
        try:
            sys.stdout.write("\x1b[?2004h")
            sys.stdout.flush()
            try:
                result = bridge_hangup.run_curses(terminal_loop)
            finally:
                sys.stdout.write("\x1b[?2004l")
                sys.stdout.flush()
        except KeyboardInterrupt:
            return 130
        if not isinstance(result, (TerminalAction, ControlViewAction)):
            return int(result)
        if isinstance(result, TerminalAction) and result.action == "pick":
            from .bridge_switcher import pick_mission_action

            try:
                picked = pick_mission_action(mission_id)
            except Exception as exc:
                state.output.append(f"Mission picker failed: {_text(exc, 180)}")
                state.last_refresh = 0.0
                continue
            if picked is None:
                state.output.append("Mission switch cancelled.")
                state.last_refresh = 0.0
                continue
            result = picked
        try:
            if isinstance(result, ControlViewAction):
                run_control(result.mission_id, dispatch, snapshot, initial_view=result.view)
            else:
                _execute_terminal_action(mission_id, result)
                state.output.append(_terminal_outcome(mission_id, result))
        except Exception as exc:
            state.output.append(f"Attach failed: {_text(exc, 180)}")
            state.last_refresh = 0.0


def run_console(mission_id: str, dispatch: Dispatch, snapshot: Snapshot) -> int:
    """Run the compact six-row command pane UI."""
    try:
        return _run(mission_id, dispatch, snapshot, full=False)
    except bridge_hangup.TerminalGone:
        return bridge_hangup.abandon_terminal()


def run_control(mission_id: str, dispatch: Dispatch, snapshot: Snapshot, *, initial_view: str = "Overview") -> int:
    """Run the full-screen terminal control UI."""
    try:
        return _run(mission_id, dispatch, snapshot, full=True, initial_view=initial_view)
    except bridge_hangup.TerminalGone:
        return bridge_hangup.abandon_terminal()
