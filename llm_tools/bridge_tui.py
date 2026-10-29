"""Small curses console for LLM Bridge's tmux command pane and control view.

The UI knows no persistence or provider APIs. It asks the injected snapshot
callback for a bounded projection and sends operator text to the same command
dispatcher used by the CLI.
"""

from __future__ import annotations

import curses
import inspect
import json
import os
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
from .bridge_documents import DocumentViewer, render_markdown, render_markdown_segments
from .bridge_theme import colors_disabled, fit_role_color, resolve_theme, role_sgr, sgr_foreground
from .common import ANSI_COLOR_ROLES
from .terminal import TerminalAction
from .bridge_actions import ACTIONS, Action


Snapshot = Callable[..., dict[str, object]]
Dispatch = Callable[[str, str], object]
LAST_RECOVERY_DRAFT: Path | None = None


def sync_window_size(window: curses.window) -> None:
    """Use the owned tty geometry even when ncurses cached a detached size."""
    try:
        size = os.get_terminal_size(sys.stdin.fileno())
        if window.getmaxyx() != (size.lines, size.columns):
            curses.resizeterm(size.lines, size.columns)
            window.resize(size.lines, size.columns)
    except (OSError, ValueError, AttributeError, curses.error):
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
    notice = "Enter newline · Ctrl-S/F2 save · Esc keeps recovery · Ctrl-D discards"
    draft_dir = default_state_dir() / "drafts"
    draft_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(draft_dir, 0o700)
    checkpoint_fd, checkpoint_name = tempfile.mkstemp(prefix="composer-recovery-", suffix=".md", dir=draft_dir)
    os.close(checkpoint_fd)
    checkpoint_path = Path(checkpoint_name)
    os.chmod(checkpoint_path, 0o600)
    LAST_RECOVERY_DRAFT = checkpoint_path
    last_checkpoint = 0.0

    def checkpoint() -> None:
        """Atomically refresh the bounded private crash-recovery source."""
        fd, name = tempfile.mkstemp(prefix=".composer-", suffix=".tmp", dir=draft_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(buffer.text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o600)
            os.replace(name, checkpoint_path)
            os.chmod(checkpoint_path, 0o600)
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise

    def retain_draft() -> Path:
        checkpoint()
        return checkpoint_path

    checkpoint()

    def loop(window: curses.window) -> str | None:
        nonlocal preferred_column, notice, escape_pending, escape_at, paste_activity, buffer, last_checkpoint
        global LAST_RECOVERY_DRAFT
        window.keypad(True)
        paste_keypad = True
        try:
            curses.nonl()
        except curses.error:
            pass
        window.timeout(30)
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
                if now - last_checkpoint >= 0.8:
                    try:
                        checkpoint()
                        last_checkpoint = now
                    except OSError as exc:
                        notice = f"Recovery checkpoint failed: {_text(exc, 100)}"
                sync_window_size(window)
                height, width = window.getmaxyx()
                if not paste_decoder.active or paste_decoder.interrupted:
                    lines = buffer.text.split("\n")
                    before = buffer.text[:buffer.cursor]
                    line_no = before.count("\n")
                    column = len(before.rsplit("\n", 1)[-1])
                    top = max(0, min(line_no, max(0, len(lines) - max(1, height - 3))))
                    window.erase()
                    _put(window, 0, 0, f"{title} · full source", width, role="heading")
                    for row, value in enumerate(lines[top:top + max(1, height - 3)], 1):
                        if row - 1 + top == line_no:
                            caret_cells = cell_width(value[:column])
                            start_cell = max(0, caret_cells - max(0, width - 2))
                            value = _slice_display_cells(value, start_cell, max(1, width - 1))
                        _put(window, row, 0, value, width, preserve_spacing=True)
                    _put(window, height - 2, 0, notice, width,
                         role="warn" if notice.startswith(("Paste rejected", "Input rejected", "Incomplete", "Recovery")) else "dim")
                    status = f"F2/Ctrl-S Save · Esc Recover · Ctrl-D Discard · {buffer.size_bytes}/{buffer.max_bytes} bytes · {len(lines)} lines"
                    _put(window, height - 1, 0, status, width, role="dim")
                    visible_row = line_no - top + 1
                    cursor_cells = cell_width(lines[line_no][:column])
                    start_cell = max(0, cursor_cells - max(0, width - 2))
                    try:
                        curses.curs_set(1)
                        window.move(min(height - 2, visible_row), min(max(0, width - 1), cursor_cells - start_cell))
                    except curses.error:
                        pass
                    window.refresh()
                try:
                    key = window.get_wch()
                except curses.error:
                    if paste_decoder.active and time.monotonic() - paste_activity >= 2.0:
                        paste_decoder.interrupt()
                        notice = "Incomplete paste quarantined; source unchanged. Ctrl-D keeps recovery and closes."
                    if paste_decoder.waiting and not paste_decoder.active and not escape_pending:
                        paste_decoder.reset()
                        notice = "Unknown or incomplete key sequence ignored."
                    if escape_pending and time.monotonic() - escape_at >= 0.06:
                        escape_pending = False
                        if paste_decoder.active:
                            paste_decoder.reset()
                            notice = "Incomplete paste discarded; source buffer unchanged."
                        else:
                            retain_draft()
                            return None
                    continue
                if paste_decoder.interrupted and key == "\x04":
                    checkpoint()
                    return None
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
                        edited = subprocess.run([*editor, editor_path], check=False, timeout=300)
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
                    preferred_column = cell_width(lines[line_no][:column]) if preferred_column is None else preferred_column
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
                    notice = "Enter inserts a newline · Ctrl-S saves · Esc keeps a private recovery draft"
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
        return curses.wrapper(loop) if window is None else loop(window)
    except (KeyboardInterrupt, EOFError, curses.error, OSError):
        retain_draft()
        return None
    finally:
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()


def _slice_display_cells(value: str, start: int, width: int) -> str:
    """Display a cell-width slice without splitting a wide UTF-8 character."""
    output: list[str] = []
    cells = 0
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


@dataclass(frozen=True)
class ControlViewAction:
    """Open a parsed control command after restoring the current terminal."""

    mission_id: str
    view: str

VIEWS = ("Bridge", "Orders", "Stations", "Systems", "Log", "Sessions", "Palette", "Usage")
PALETTE_COMMANDS = tuple(action.command for action in ACTIONS)
_REFRESH_SECONDS = 0.25
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
_NAV_HINT = "F1-8/Tab views PgUp/Dn pages Ctrl-E add Ctrl-R edit Ctrl-] z expand Ctrl-D exit"
_COLOR_ROLES = tuple(ANSI_COLOR_ROLES)
_ROLE_PAIRS: dict[str, int] = {}
_ROLE_ATTRS: dict[str, int] = {}
_CURRENT_THEME = "dark"


def initialize_colors(env: dict[str, str] | None = None, *, is_tty: bool | None = None) -> bool:
    """Initialize the Bridge semantic palette for the active curses terminal.

    Colors fall back to monochrome when the output is not a terminal, the
    terminal is dumb, or NO_COLOR / LLM_USAGE_NO_COLOR is set. The default
    terminal background is preserved whenever curses supports it.
    """
    global _CURRENT_THEME, _ROLE_PAIRS, _ROLE_ATTRS
    values = os.environ if env is None else env
    _CURRENT_THEME = resolve_theme(values)
    _ROLE_PAIRS = {}
    _ROLE_ATTRS = {}
    if is_tty is None:
        is_tty = sys.stdout.isatty()
    if not is_tty or colors_disabled(values):
        return False
    try:
        if not curses.has_colors():
            return False
        curses.start_color()
    except (AttributeError, curses.error):
        return False

    default_colors = False
    try:
        curses.use_default_colors()
        default_colors = True
    except (AttributeError, curses.error):
        pass

    fallback_bg = -1
    if not default_colors:
        fallback_bg = curses.COLOR_WHITE if _CURRENT_THEME == "light" else curses.COLOR_BLACK
    for pair, role in enumerate(_COLOR_ROLES, start=1):
        if pair >= curses.COLOR_PAIRS:
            break
        sgr = role_sgr(role, theme=_CURRENT_THEME, env=values)
        foreground, bold, dim, underline = sgr_foreground(sgr)
        if foreground is None and default_colors:
            foreground_color = -1
        else:
            foreground_color = fit_role_color(foreground, curses.COLORS, _CURRENT_THEME)
        try:
            curses.init_pair(pair, foreground_color, fallback_bg)
        except curses.error:
            continue
        _ROLE_PAIRS[role] = pair
        attr = curses.color_pair(pair)
        # Eight-color terminals commonly render bold as a brighter ANSI slot.
        # On a light background that can turn readable dark red into low-
        # contrast bright red. Selection retains its own reverse/bold cue.
        if bold and not (_CURRENT_THEME == "light" and curses.COLORS <= 8 and foreground_color >= 0):
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        if underline:
            attr |= curses.A_UNDERLINE
        _ROLE_ATTRS[role] = attr
    return bool(_ROLE_PAIRS)


def role_attr(role: str) -> int:
    """Return the current curses attribute for a semantic Bridge color role."""
    if role == "selected":
        base = _ROLE_ATTRS.get("brand", 0)
        return base | curses.A_BOLD | curses.A_REVERSE
    if role == "secondary":
        role = "dim"
    return _ROLE_ATTRS.get(role, 0)


def _text(value: object, limit: int = 1000) -> str:
    """Make untrusted snapshot/result text safe for direct curses rendering."""
    rendered = str(value)
    clean = "".join(
        char if unicodedata.category(char) not in {"Cc", "Cs"} else " "
        for char in rendered
    )
    return " ".join(clean.split())[:limit]


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
            label = _text(command, 120)
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


def _confirmation_reason(command: str) -> str | None:
    """Return the consequence that needs a deliberate second keystroke."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if words and words[0] == "cancel":
        return "Cancel this mission and its unfinished orders?"
    if words and words[0] == "stop" and "--force" in words:
        return "Force stop this mission's owned active work?"
    if words[:2] == ["task", "cancel"]:
        return "Cancel this order and discard its remaining work?"
    if words[:2] == ["task", "verify"] and "--approve" in words:
        return f"Approve verification and mark task {words[words.index('--task')+1] if '--task' in words and words.index('--task')+1 < len(words) else '(select a task)'} complete?"
    return None


def _command_completions(command: str) -> list[str]:
    """Complete public command/option names from the authoritative parser."""
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
    names = list(dict.fromkeys(str(name) for name in [*providers, *routes]))
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
    return rows, context


def _projected_rows(value: object, limit: int, fields: tuple[str, ...]) -> list[dict[str, object]]:
    rows = _items(value)
    return [{key: _text(row.get(key, ""), 200) for key in fields if key in row} for row in rows[:limit]]


def _bounded_task_value(value: object, *, limit: int = _MAX_DETAIL_ITEMS, preserve_newlines: bool = False) -> object:
    """Keep useful task detail while preventing arbitrary nested payload growth."""
    if isinstance(value, str):
        if preserve_newlines:
            return "".join(
                char if char == "\n" or unicodedata.category(char) not in {"Cc", "Cs"} else " "
                for char in value
            )[:_MAX_DETAIL_TEXT]
        return _text(value, _MAX_DETAIL_TEXT)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_bounded_task_value(item) for item in value[:limit]]
    if isinstance(value, dict):
        allowed = {"prompt", "title", "priority", "acceptance", "evidence", "revision", "status", "changes", "created_at"}
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
        for key in ("id", "title", "status", "priority", "ordering", "revision", "updated_at", "origin", "proposal_status", "proposed_by_operation"):
            if key in task:
                item[key] = _text(task[key], 120)
        for key in ("prompt", "acceptance", "evidence", "pending", "depends_on", "amendments"):
            if key in task:
                item[key] = _bounded_task_value(task[key], preserve_newlines=key == "prompt")
        output.append(item)
    return output


def _project_capacity(value: object) -> dict[str, object]:
    capacity = _dict(value)
    selection = _dict(capacity.get("selection"))
    result: dict[str, object] = {
        _text(key, 48): _text(item, 180)
        for key, item in capacity.items()
        if isinstance(item, (str, int, float, bool, type(None)))
    }
    if selection:
        selected: dict[str, object] = {
            _text(key, 48): _text(item, 180)
            for key, item in selection.items()
            if key != "decisions" and isinstance(item, (str, int, float, bool, type(None)))
        }
        raw_decision = _dict(selection.get("decision"))
        selected["decision"] = {
            _text(key, 48): _text(item, 120)
            for key, item in raw_decision.items()
            if isinstance(item, (str, int, float, bool, type(None)))
        }
        decisions: list[dict[str, object]] = []
        raw_decisions = selection.get("decisions")
        for raw in raw_decisions[:8] if isinstance(raw_decisions, list) else []:
            row = _dict(raw)
            decision: dict[str, object] = {
                _text(key, 48): _text(item, 120)
                for key, item in row.items()
                if key != "windows" and isinstance(item, (str, int, float, bool, type(None)))
            }
            windows: list[dict[str, object]] = []
            raw_windows = row.get("windows")
            for raw_window in raw_windows[:8] if isinstance(raw_windows, list) else []:
                window = _dict(raw_window)
                windows.append({
                    _text(key, 48): _text(item, 120)
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
            _text(key, 48): _text(item, 120)
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
    result["tasks"] = _project_tasks(data.get("tasks"))
    current_task = _dict(data.get("current_task"))
    if current_task:
        result["current_task"] = {
            key: _text(current_task.get(key, ""), 180)
            for key in ("id", "title", "status", "revision")
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
            "operation_status": _text(raw_station_context.get("operation_status") or "unknown", 24),
        }
    result["station_context"] = station_context
    result["events"] = _projected_rows(
        data.get("events"),
        _MAX_EVENT_ROWS,
        ("id", "event_type", "type", "kind", "summary", "created_at", "payload", "data"),
    )
    result["operations"] = _projected_rows(
        data.get("operations"),
        20,
        ("id", "operation_id", "task_id", "provider", "station", "route_id", "model", "native_session_id", "restoration_state", "phase", "status", "exit_code", "created_at"),
    )
    pending = data.get("pending_commands")
    result["pending_command_count"] = len(pending) if isinstance(pending, list) else 0
    result["task_counts"] = {
        _text(key, 32): count
        for key, count in _dict(data.get("task_counts")).items()
        if isinstance(count, (int, float))
    }
    result["proposal_counts"] = {
        _text(key, 32): count
        for key, count in _dict(data.get("proposal_counts")).items()
        if isinstance(count, (int, float))
    }
    total_tasks = data.get("task_total")
    if isinstance(total_tasks, int):
        result["task_total"] = total_tasks
    for key in ("controller", "routes", "execution"):
        if key in data:
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
            _text(field, 48): _text(value, 180)
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
                if isinstance(heartbeat.get(field), (str, int, float, bool, type(None)))
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


def _capacity_summaries(snapshot: dict[str, object]) -> list[str]:
    """Compact, explicitly estimated scope forecasts for active and alternate routes."""
    from . import common

    capacity = _dict(snapshot.get("capacity"))
    selection = _dict(capacity.get("selection"))
    station_context = _dict(snapshot.get("station_context"))
    operation_id = str(station_context.get("executing_operation") or "")
    operation_status = str(station_context.get("operation_status") or "").upper()
    active_owner = ""
    if operation_id and operation_status == "RUNNING":
        active_owner = str(station_context.get("last_launched_station") or "")
    summaries = []
    for row in _items(selection.get("decisions")):
        owner = str(row.get("route") or row.get("provider") or "unknown")
        scopes = []
        for window in _items(row.get("windows")):
            name = _text(window.get("name") or "scope", 20)
            if window.get("kind") == "opaque":
                scopes.append(f"{name}: quota unmeasured")
                continue
            estimate = window.get("estimated_remaining")
            estimate = str(estimate) if estimate and estimate != "None" else "unknown"
            reset = common.parse_epoch(window.get("reset_epoch"))
            reset_text = common.fmt_duration(max(0, reset-common.now_epoch())) if reset is not None else "unknown"
            remaining = window.get("remaining_percent")
            remaining_text = f"{remaining}%" if remaining not in (None, "None", "") else "unknown"
            scopes.append(f"{name} est {estimate} ({remaining_text}; reset {reset_text})")
        selected_owner = str(selection.get("route") or selection.get("provider") or "")
        label = "ACTIVE " if active_owner and owner == active_owner else "Last selected " if owner == selected_owner else "READY " if str(row.get("usable")).lower() == "true" else "ALT "
        summaries.append(label + _text(owner, 40) + (" / " + _text(row["selected_model"], 40) if row.get("selected_model") else "") + " | " + (" | ".join(scopes) or "capacity unavailable"))
    return sorted(summaries, key=lambda text: not text.startswith("ACTIVE "))


def _task_label(task: dict[str, object]) -> str:
    identifier = _text(task.get("id", "?"), 40)
    title = _text(task.get("title") or task.get("prompt") or "Untitled task", 90)
    state = _text(task.get("status", "unknown"), 24)
    return f"{identifier}  {state.upper()}  {title}"


def _station_status_context(snapshot: dict[str, object]) -> str:
    """Compact current task/mission state for the native station status row."""
    mission_status = _text(_mission(snapshot).get("status") or "unknown", 16).upper()
    task = _dict(snapshot.get("current_task"))
    if not task:
        return f"No active task / Mission {mission_status}"
    identifier = _text(task.get("id") or "?", 8)
    task_status = _text(task.get("status") or "unknown", 12).upper()
    return f"Task {identifier} {task_status} / Mission {mission_status}"


def _station_label(station: dict[str, object]) -> str:
    name = _text(station.get("name") or station.get("provider") or station.get("id") or "Station", 60)
    configured_model = _text(station.get("model") or "unknown", 50)
    observed_model = _text(station.get("observed_model") or "unknown", 50)
    state = _text(station.get("restoration_state") or station.get("status") or "unknown", 24)
    return f"{name}  configured model {configured_model}  observed model {observed_model}  {state}"


def _nav_labels(view_index: int) -> list[str]:
    return [
        f"[{index + 1} {name}]" if index == view_index else f"{index + 1} {name}"
        for index, name in enumerate(VIEWS)
    ]


def _nav_line(view_index: int, separator: str) -> str:
    return separator.join(_nav_labels(view_index))


def _nav_index_at(x: int, view_index: int, width: int, separator: str) -> int:
    _, entries = _nav_layout(view_index, width, separator)
    return min(entries, key=lambda item: min(abs(x-item[1]), abs(x-item[1]-len(item[2])+1)))[0] if entries else view_index


def _nav_layout(view_index: int, width: int, separator: str) -> tuple[str, list[tuple[int, int, str]]]:
    labels = _nav_labels(view_index)
    if len(separator.join(labels)) > width:
        separator = " "
    first = 0
    while first < view_index and len(separator.join(labels[first:view_index+1])) + (2 if first else 0) + 2 > width:
        first += 1
    text = "< " if first else ""
    entries: list[tuple[int, int, str]] = []
    for index in range(first, len(labels)):
        label = labels[index]
        suffix = 2 if index < len(labels)-1 else 0
        if entries and len(text) + len(separator) + len(label) + suffix > width:
            text += " >"
            break
        if entries:
            text += separator
        entries.append((index, len(text), label))
        text += label
    return text, entries


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
    pending_cleanup_path: Path | None = None
    pending_cleanup_paths: tuple[Path, ...] = ()
    submitted_draft: str | None = None
    rejected_draft_path: Path | None = None
    rejected_draft_paths: tuple[Path, ...] = ()
    rejected_submission: str | None = None
    expand_requested: bool = False
    response_overlay: bool = False
    confirm_command: str | None = None
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

    @property
    def view(self) -> str:
        return VIEWS[self.view_index]

    def refresh(self, now: float | None = None, *, force: bool = False) -> None:
        global LAST_RECOVERY_DRAFT
        current = time.monotonic() if now is None else now
        with self._result_lock:
            snapshot_result, self._snapshot_result = self._snapshot_result, None
            command_result, self._command_result = self._command_result, None
            document_result, self._document_result = self._document_result, None
            snapshot_thread = self._snapshot_thread
        if document_result is not None:
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
                self.snapshot = data
                self.snapshot_error = ""
            else:
                self.snapshot_error = error
        if isinstance(command_result, ControlViewAction) and command_result.mission_id == self.mission_id:
            self.choose_view(VIEWS.index(command_result.view))
            self.expand_requested = True
            self.command_pending = False
            self.last_refresh = 0.0
        elif isinstance(command_result, (TerminalAction, ControlViewAction)):
            self.terminal_action = command_result
            self.command_pending = False
            self.last_refresh = 0.0
        elif command_result is not None:
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
            if failed and self.submitted_draft:
                self.rejected_submission = self.submitted_draft
                if not self.command:
                    self.command = self.submitted_draft
                    self.command_cursor = len(self.command)
                    self.input_focused = True
            self.submitted_draft = None
            if result_text:
                response_lines = result_text.splitlines() or [result_text]
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
        if not force and current - self.last_refresh < _REFRESH_SECONDS:
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
                projection = {
                    "task_offset": self.task_offset,
                    "event_offset": self.event_offset,
                    "event_anchor": self.event_anchor,
                    "task_limit": _TASK_PAGE_SIZE,
                    "search": self.search_query,
                    "view": self.view,
                }
                kwargs = {
                    key: value
                    for key, value in projection.items()
                    if accepts_kwargs or key in parameter_names
                }
                data = self.snapshot_reader(self.mission_id, **kwargs)
                if not isinstance(data, dict):
                    raise TypeError("status snapshot must be a mapping")
                result = (_bounded_snapshot(data), "")
            except Exception as exc:  # UI remains usable while a projection is unavailable.
                result = (None, _text(exc, 160) or type(exc).__name__)
            with self._result_lock:
                self._snapshot_result = result

        thread = threading.Thread(target=read_snapshot, name="llm-bridge-snapshot", daemon=True)
        with self._result_lock:
            if self._snapshot_result is not None:
                return
            if snapshot_thread is not None and snapshot_thread.is_alive():
                return
            self.last_refresh = current
            self._snapshot_thread = thread
        thread.start()

    def _dispatch(
        self,
        line: str,
        *,
        cleanup_path: Path | None = None,
        cleanup_paths: tuple[Path, ...] = (),
    ) -> bool:
        text = line.strip()
        if not text:
            return True
        try:
            help_words = shlex.split(text)
        except ValueError:
            help_words = []
        if help_words and help_words[0] in {"help", "?"} and "--help" not in help_words:
            from .bridge_help import topic_text
            topic = " ".join(help_words[1:]) or "quickstart"
            self.close_document()
            self.document_task_id = None
            self.document_notice = ""
            self.document_viewer = DocumentViewer(f"Bridge help · {topic}", topic_text(topic))
            return True
        if text in {"control", "usage"}:
            self.choose_view(VIEWS.index("Usage" if text == "usage" else "Bridge"))
            self.expand_requested = True
            self.last_refresh = 0.0
            return True
        if self.command_pending:
            self.output.append("A command is still running; wait for its result before dispatching another.")
            return False
        self.history.append(text)
        self.history_index = None
        self.command_pending = True
        paths = tuple(dict.fromkeys((*cleanup_paths, *((cleanup_path,) if cleanup_path is not None else ()))))
        self.pending_cleanup_paths = paths
        self.pending_cleanup_path = paths[0] if paths else None
        self.submitted_draft = text
        self.output.append("Command submitted; waiting for the durable result.")

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
                    outcome = {"APPLIED": "applied", "REJECTED": "rejected", "FAILED": "failed"}.get(status, "unknown")
                    if raw_text.lstrip().lower().startswith("error:"):
                        outcome = "rejected"
                    result = _CommandResult(_command_feedback(text, raw_text), outcome)
            except Exception as exc:
                result = _CommandResult(f"Outcome unknown: {_text(exc, 180)}", "unknown")
            with self._result_lock:
                self._command_result = result
        thread = threading.Thread(target=run_command, name="llm-bridge-command", daemon=False)
        thread.start()
        return True

    def _confirm_or_dispatch(self, command: str) -> bool:
        reason = _confirmation_reason(command)
        if reason:
            self.confirm_command = command
            self.output.clear()
            self.output.extend([f"{reason} · mission {self.mission_id}", "Press Y to confirm or N/Esc to cancel."])
            self.response_overlay = True
            return True
        return self._dispatch(command)

    def handle_key(self, key: object) -> bool:
        """Apply one semantic key; return False when the console should exit."""
        if self.palette_filter_editing:
            if key in {"\x1b", "\n", "\r", "KEY_ENTER"}:
                self.palette_filter_editing = False
            elif key in {"KEY_BACKSPACE", "\x7f", "\b"}:
                self.palette_filter = self.palette_filter[:-1]
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                self.palette_filter += key
            self.selected = 0
            return True
        if self.view == "Palette" and not self.input_focused and key == "/":
            self.palette_filter_editing = True
            return True
        viewer = self.document_viewer
        if viewer is not None:
            headings = viewer.headings()
            if self.document_heading_list:
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
                if self.document_task_id is not None and str(_mission(self.snapshot).get("status", "")).upper() not in {"COMPLETED", "CANCELLED", "FAILED"}:
                    self.edit_document_requested = True
                    self.close_document()
                else:
                    self.document_notice = "Task is not editable in a terminal mission; export its source." if self.document_task_id is not None else "Objective is read-only; export its source to reuse it in a new mission."
                    self.output.append(self.document_notice)
            elif key == "m":
                viewer.toggle_rendered()
            elif key == "w":
                viewer.toggle_wrap()
            elif key == "h":
                self.document_heading_list = True
                self.document_heading_selected = min(self.document_heading_selected, max(0, len(headings) - 1))
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
            topic = "prompts" if self.view == "Orders" else "providers" if self.view == "Stations" else "keys"
            self._dispatch("help " + topic)
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
        if key == "\x07":  # Ctrl-G focuses the command draft.
            self.input_focused = True
            self.command_cursor = min(len(self.command), self.command_cursor)
            return True
        if key == "v" and self.view == "Orders" and not self.input_focused:
            task = self.selected_task()
            if task is not None:
                task_id = str(task.get("id", ""))
                self.request_task_document(task_id)
            return True
        if key == "o" and self.view == "Bridge" and not self.input_focused:
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
                        result = ("objective", "?", None, f"Could not read full objective: {_text(exc, 160)}")
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
        if key in {"KEY_LEFT", "KEY_RIGHT"} and not self.input_focused:
            direction = -1 if key == "KEY_LEFT" else 1
            self.view_index = (self.view_index + direction) % len(VIEWS)
            self.selected = 0
            return True
        if key in {"KEY_UP", "KEY_DOWN"} and not self.input_focused:
            if self.view == "Orders" and self.task_detail:
                self.detail_scroll = max(0, self.detail_scroll - 1) if key == "KEY_UP" else self.detail_scroll + 1
            else:
                self.selected = min(max(0, len(self.rows())-1-self._selection_row_offset()), max(0, self.selected + (-1 if key == "KEY_UP" else 1)))
            return True
        if key in {"KEY_HOME", "KEY_END"} and not self.input_focused:
            self.selected = 0 if key == "KEY_HOME" else max(0, len(self.rows())-1-self._selection_row_offset())
            return True
        if key in {"KEY_PPAGE", "KEY_NPAGE"} and not self.input_focused and not self.search_editing:
            if self.view == "Orders" and self.task_detail:
                delta = 10 if self.full else 5
                self.detail_scroll = max(0, self.detail_scroll - delta) if key == "KEY_PPAGE" else self.detail_scroll + delta
            elif self.view == "Log":
                if key == "KEY_PPAGE" and self.event_anchor is None:
                    ids = [int(str(event["id"])) for event in _items(self.snapshot.get("events"))
                           if str(event.get("id", "")).isdigit()]
                    self.event_anchor = max(ids) if ids else 0
                self.event_offset = self.event_offset + 20 if key == "KEY_PPAGE" else max(0, self.event_offset-20)
                if self.event_offset == 0:
                    self.event_anchor = None
                self.selected = 0
                self.last_refresh = 0.0
            elif self.view == "Orders":
                total = self.snapshot.get("task_total")
                if key == "KEY_PPAGE":
                    self.task_offset = max(0, self.task_offset - _TASK_PAGE_SIZE)
                elif not isinstance(total, int) or self.task_offset + _TASK_PAGE_SIZE < total:
                    self.task_offset += _TASK_PAGE_SIZE
                self.selected = 0
                self.last_refresh = 0.0
            else:
                delta = 10 if self.full else 5
                self.selected = min(max(0, len(self.rows())-1-self._selection_row_offset()), max(0, self.selected + (-delta if key == "KEY_PPAGE" else delta)))
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
            else:
                self.input_focused = False
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
            if self.view == "Palette" and not self.input_focused:
                actions = self.palette_actions()
                if actions:
                    self.selected = min(max(0, self.selected), len(actions)-1)
                    self.activate_action(actions[self.selected])
            elif self.view == "Orders" and not self.input_focused:
                self.task_detail = not self.task_detail
                self.detail_scroll = 0
            elif self.view == "Stations" and not self.input_focused:
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

    def choose_view(self, index: int) -> None:
        if 0 <= index < len(VIEWS):
            if self.view == "Orders" and self.selected_task() is not None:
                self.selected_task_anchor = str(self.selected_task()["id"])
            self.view_index = index
            self.selected = 0

    def palette_actions(self) -> list[Action]:
        query = self.palette_filter.casefold()
        return [action for action in ACTIONS if query in f"{action.label} {action.effect} {action.command}".casefold()]

    def action_unavailable(self, action: Action) -> str:
        status = str(_mission(self.snapshot).get("status", "")).upper()
        if action.mutable and status in {"COMPLETED", "CANCELLED", "FAILED"}:
            return f"mission {status.lower()}"
        if action.task and self.selected_task_anchor is None:
            return "select a task in Orders first"
        return ""

    def activate_action(self, action: Action) -> None:
        unavailable = self.action_unavailable(action)
        if unavailable:
            self.output.append(f"{action.label} unavailable: {unavailable}")
        elif action.label == "Add task":
            self.document_task_id = None
            self.edit_initial_text = ""
            self.edit_document_requested = True
        elif action.label == "Amend task":
            self.request_task_document(str(self.selected_task_anchor), for_edit=True)
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
        task = next((item for item in _items(self.snapshot.get("tasks")) if str(item.get("id")) == task_id), {})
        self._invalidate_document_read()
        generation = self._document_generation
        self.document_pending = True
        self.document_edit_pending = for_edit

        def read_document() -> None:
            try:
                raw = self.dispatch(self.mission_id, shlex.join(["task", "show", "--task", task_id]))
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

    def selected_task(self) -> dict[str, object] | None:
        tasks = _items(self.snapshot.get("tasks"))
        if not tasks:
            return None
        return tasks[min(max(0, self.selected), len(tasks) - 1)]

    def _selection_row_offset(self) -> int:
        if self.view == "Stations" and self.full:
            return 2
        return 0

    def rows(self) -> list[str]:
        data = self.snapshot
        mission = _mission(data)
        tasks = _items(data.get("tasks"))
        stations = _items(data.get("stations"))
        events = _items(data.get("events"))
        operations = _items(data.get("operations"))
        task_counts = _dict(data.get("task_counts"))
        if self.view == "Usage":
            usage = _dict(data.get("usage"))
            return [f"Usage: {usage.get('status', 'refreshing')} | age {usage.get('age_seconds', 'unknown')}s | {usage.get('error') or 'background refresh'}",
                    *str(usage.get("text") or "Loading full llm-usage dashboard...").splitlines()]
        if self.view == "Bridge":
            next_task = next((task for task in tasks if task.get("status") in {"READY", "QUEUED"}), None)
            current_task = _dict(data.get("current_task"))
            counts = " ".join(
                f"{_text(state, 12).lower()} {count}"
                for state, count in task_counts.items()
                if isinstance(count, (int, float))
            )
            proposal_counts = _dict(data.get("proposal_counts"))
            proposal_summary = " ".join(
                f"proposal {str(state).lower()} {count}"
                for state, count in proposal_counts.items()
                if isinstance(count, (int, float)) and count
            )
            pause_notice = ""
            if str(mission.get("status", "")).upper() == "PAUSED":
                context = _dict(data.get("station_context"))
                if context.get("executing_operation") and str(context.get("operation_status", "")).upper() in {"INTENT", "PREPARED", "RUNNING"}:
                    pause_notice = "Pausing after current provider invocation"
            return [
                pause_notice or f"Controller: {_text(_dict(data.get('lifecycle')).get('observation') or 'inspect Systems for observation', 100)}",
                f"Executing: {_task_label(current_task) if current_task else 'none'}",
                f"Next: {_task_label(next_task) if next_task else 'none ready'}",
                f"Orders: {counts or f'{len(tasks)} visible'}" + (f" | {proposal_summary}" if proposal_summary else ""),
                f"Pending commands: {data.get('pending_command_count', 0)}",
                f"Criteria verified: {_dict(data.get('criteria')).get('verified', '?')}/{_dict(data.get('criteria')).get('total', '?')} | ops {len(operations)} | events {len(events)}",
                *_capacity_summaries(data),
            ]
        if self.view == "Orders":
            if self.task_detail:
                task = self.selected_task()
                if task is None:
                    return ["No task selected."]
                dependencies = task.get("depends_on", [])
                acceptance = task.get("acceptance", [])
                evidence = task.get("evidence", [])
                pending = _dict(task.get("pending"))
                amendments = task.get("amendments")
                details = [
                    f"Order {_text(task.get('id', '?'), 32)}: {_text(task.get('title') or 'Untitled', 100)}",
                    f"Status {_text(task.get('status', 'unknown'), 24)} | priority {_text(task.get('priority', '?'), 16)} | revision {_text(task.get('revision', '?'), 16)}",
                    f"Origin {_text(task.get('origin') or 'operator', 24)} | proposal {_text(task.get('proposal_status') or 'not a proposal', 32)} | proposed by {_text(task.get('proposed_by_operation') or 'unknown', 32)}",
                    "Dependencies: " + (", ".join(_text(item, 32) for item in dependencies) if isinstance(dependencies, list) and dependencies else "none"),
                    "Acceptance criteria:",
                    *([f"  {index + 1}. {_text(item, 240)}" for index, item in enumerate(acceptance[:_MAX_DETAIL_ITEMS])] if isinstance(acceptance, list) and acceptance else ["  none recorded"]),
                    "Verification evidence:",
                    *([f"  {index + 1}. {_text(item, 240)}" for index, item in enumerate(evidence[:_MAX_DETAIL_ITEMS])] if isinstance(evidence, list) and evidence else ["  none recorded"]),
                    "Pending amendment: " + (json.dumps(pending, ensure_ascii=False, sort_keys=True)[:400] if pending else "none"),
                    "Amendment history:",
                    *([
                        f"  revision {_text(item.get('revision', '?'), 16)} {_text(item.get('status', 'unknown'), 20)}: {_text(json.dumps(item.get('changes', {}), ensure_ascii=False, sort_keys=True), 240)}"
                        for item in amendments[:5]
                    ] if isinstance(amendments, list) and amendments else ["  none recorded"]),
                    "Prompt preview (v opens full prompt): " + _text(task.get("prompt") or "", 800),
                ]
                return details
            return [_task_label(task) for task in tasks] or ["No tasks recorded."]
        if self.view == "Stations":
            if stations:
                context = _dict(data.get("station_context"))
                viewed = _text(context.get("viewed_station") or "unknown", 80)
                last_launched = _text(context.get("last_launched_station") or "unknown", 80)
                rows: list[str] = []
                for item in stations:
                    name = _text(item.get("name") or item.get("provider") or item.get("id") or "Station", 80)
                    flags = []
                    if name == viewed:
                        flags.append("VIEWED")
                    if name == last_launched:
                        flags.append("LAST LAUNCHED")
                    flags_text = f" [{' '.join(flags)}]" if flags else ""
                    rows.append(f"{name}  configured {item.get('model', 'unknown')}  observed {item.get('observed_model', 'unknown')}  config {item.get('status', 'unknown')}{flags_text}")
                selected_name = self.selected_station_name()
                matching_operations = [
                    item for item in operations
                    if selected_name and selected_name in {
                        str(item.get("station") or ""), str(item.get("route_id") or "")
                    }
                ]
                return [
                    f"Viewed: {viewed} | last launched: {last_launched} | op record: {_text(context.get('executing_operation', 'none'), 32)} ({_text(context.get('operation_status', 'unknown'), 20)})",
                    f"Provider liveness: {_text(context.get('provider_liveness', 'unknown'), 20)} | mode: {_text(context.get('mode', 'unknown'), 20)} | model: {_text(context.get('observed_model', 'unknown'), 40)}",
                    *rows,
                    f"Recent history for {selected_name or 'selected station'}:",
                    *([
                        f"  op {_text(item.get('id') or item.get('operation_id') or '?', 24)} task {_text(item.get('task_id') or '?', 12)} {_text(item.get('status') or 'unknown', 20)} model {_text(item.get('model') or 'unknown', 40)} exit {_text(item.get('exit_code') if item.get('exit_code') is not None else 'unknown', 16)}"
                        for item in matching_operations[:8]
                    ] or ["  no recent operations"]),
                ]
            observed = {
                (_text(item.get("station") or item.get("provider") or "unknown", 60),
                 _text(item.get("model") or "model unknown", 60),
                 _text(item.get("status") or "unknown", 24))
                for item in operations
            }
            return [f"{station}  {model}  {status}" for station, model, status in sorted(observed)] or ["No stations recorded."]
        if self.view == "Systems":
            keys = ("controller", "station_mode", "lifecycle", "routes", "execution", "health", "persistence", "criteria", "last_meaningful_progress", "suspend")
            found: list[str] = []
            for key in keys:
                if key not in data:
                    continue
                section = _dict(data.get(key))
                if key == "lifecycle" and isinstance(data.get(key), dict):
                    heartbeat = _dict(section.get("heartbeat"))
                    lifecycle_values = {field: value for field, value in section.items() if field != "heartbeat"}
                    if lifecycle_values:
                        found.append("lifecycle: " + " | ".join(f"{field}={value}" for field, value in lifecycle_values.items()))
                    if heartbeat:
                        found.append(
                            "Controller heartbeat (last observed): "
                            + " | ".join(f"{field}={value}" for field, value in heartbeat.items())
                        )
                    continue
                if isinstance(data.get(key), dict):
                    found.append(f"{key}: " + " | ".join(f"{field}={value}" for field, value in section.items()))
                else:
                    found.append(f"{key}: {_text(data[key], 220)}")
            capacity = _dict(data.get("capacity"))
            selection = _dict(capacity.get("selection"))
            if capacity:
                selected_fields = ("provider", "route", "selected_model", "rotation_reason", "all_rate_limited")
                selection_summary = " | ".join(
                    f"{field}={selection[field]}" for field in selected_fields if selection.get(field) is not None
                )
                found.append(
                    "capacity: " + " | ".join(
                        part for part in (f"source={capacity.get('source', 'unknown')}", selection_summary) if part
                    )
                )
            decisions = selection.get("decisions")
            if isinstance(decisions, list):
                for decision in decisions[:8]:
                    row = _dict(decision)
                    owner = _text(row.get("route") or row.get("provider") or "provider", 60)
                    windows = row.get("windows")
                    if isinstance(windows, list):
                        for window in windows[:8]:
                            item = _dict(window)
                            name = _text(item.get("name") or item.get("kind") or "window", 36)
                            kind = _text(item.get("kind") or "unknown", 24)
                            remaining = item.get("remaining_percent")
                            if remaining is None:
                                amount = item.get("remaining_amount")
                                currency = _text(item.get("currency") or "", 16)
                                remaining_text = f"remaining {amount} {currency}".strip() if amount is not None else "remaining unknown"
                            else:
                                remaining_text = f"remaining {remaining}%"
                            reset = item.get("reset_epoch")
                            reset_text = f"reset epoch {reset}" if reset not in (None, "None", "") else "no reset"
                            reason = item.get("reason")
                            suffix = f" ({_text(reason, 80)})" if reason else ""
                            estimate = item.get("estimated_remaining")
                            estimate_text = f"est. runout {estimate}" if estimate and estimate != "None" else "est. runout unknown"
                            details = "; ".join(part for part in (remaining_text, estimate_text if kind == "reset_window" else "", reset_text) if part)
                            found.append(f"Quota/budget {owner} {name} [{kind}]: {details}{suffix}")
            return found or ["System and capacity status is not available in this snapshot."]
        if self.view == "Log":
            return [
                f"{_text(event.get('id', '?'), 24)}  {_text(event.get('event_type') or event.get('type') or event.get('kind') or 'event', 36)}  {_text(event.get('payload') or event.get('data') or event.get('summary') or event.get('created_at') or '', 150)}"
                for event in events
            ] or ["No recent mission events."]
        if self.view == "Sessions":
            return [
                f"op {_text(item.get('id') or item.get('operation_id') or '?', 24)} task {_text(item.get('task_id') or '?', 12)}  {_text(item.get('station') or item.get('provider') or 'station unknown', 28)} route {_text(item.get('route_id') or item.get('route') or 'unknown', 24)} pinned model {_text(item.get('model') or 'unknown', 28)}  {_text(item.get('status') or 'unknown', 20)}"
                f" | native session {_text(item.get('native_session_id') or 'unknown', 36)} restoration {_text(item.get('restoration_state') or 'unknown', 24)}"
                for item in operations
            ] or [_station_label(station) for station in stations] or ["No session activity recorded."]
        if self.view == "Palette":
            actions = self.palette_actions()
            self.selected = min(max(0, self.selected), max(0, len(actions)-1))
            return [f"{action.label} — {action.effect} · {'task '+str(self.selected_task_anchor) if action.task else 'mission '+self.mission_id}" + (f" · unavailable: {self.action_unavailable(action)}" if self.action_unavailable(action) else "") for action in actions] or ["No actions match; / edits the filter, Backspace clears it."]
        return []

    def handle_mouse(self, y: int, x: int, height: int, width: int) -> None:
        if self.response_overlay:
            return
        if height <= 0 or width <= 0:
            return
        if y == height - 2:
            self.input_focused = True
            prompt = _input_prompt(self)
            self.command_cursor = len(self.command)
            return
        nav_y = 2
        if y == nav_y:
            x = max(0, min(width - 1, x))
            self.choose_view(_nav_index_at(x, self.view_index, width, "  " if self.full else " "))
        elif self.view == "Palette" and y >= nav_y + 1:
            count = len(self.palette_actions())
            start = _scroll_start(self.selected, max(1, count), max(1, height-6))
            self.selected = max(0, min(count-1, start + y - nav_y - 1))
        elif self.full and y >= 3:
            rows = self.rows()
            content_height = max(1, height - 6)
            row_offset = self._selection_row_offset()
            selected_row = self.detail_scroll if self.view == "Orders" and self.task_detail else self.selected + row_offset
            start = _scroll_start(selected_row, len(rows), content_height)
            row_index = min(max(0, len(rows) - 1), start + y - 3)
            if self.view == "Orders" and self.task_detail:
                self.detail_scroll = row_index
            else:
                self.selected = max(0, row_index - row_offset)


def _put(
    window: curses.window,
    y: int,
    x: int,
    value: object,
    width: int,
    *,
    role: str | None = None,
    attr: int = 0,
    preserve_spacing: bool = False,
) -> None:
    if width <= 0:
        return
    if preserve_spacing:
        rendered = cell_slice("".join(char if unicodedata.category(char) not in {"Cc", "Cs"} else " " for char in str(value).expandtabs(8)), 0, width)
    else:
        rendered = _text(value, width)
    style = attr | (role_attr(role) if role else 0)
    try:
        window.addnstr(y, x, rendered, width, style)
    except TypeError:
        # Keep lightweight recording windows compatible with curses' optional
        # attr argument; real curses windows always accept it.
        try:
            window.addnstr(y, x, rendered, width)
        except curses.error:
            pass
    except curses.error:
        pass


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
    if any(token in lowered for token in ("applied:", "completed", "success", "usable", "ready", "passed")):
        return "ok"
    return "dim"


def _draw_navigation(window: curses.window, index: int, separator: str, width: int) -> None:
    text, entries = _nav_layout(index, width, separator)
    _put(window, 2, 0, text, width, role="dim", preserve_spacing=True)
    for item, offset, label in entries:
        if item == index:
            _put(window, 2, offset, label, min(len(label), width-offset), role="selected")


def _header(state: _ConsoleState, width: int) -> str:
    mission = _mission(state.snapshot)
    name = _text(mission.get("name") or state.mission_id, 36)
    status = _text(mission.get("status", "unknown"), 20).upper()
    context = _dict(state.snapshot.get("station_context"))
    if status == "PAUSED" and context.get("executing_operation") and str(context.get("operation_status", "")).upper() in {"INTENT", "PREPARED", "RUNNING"}:
        status = "PAUSING"
    return f"LLM BRIDGE  {name}  {status}  {state.view}"


def _scroll_start(selected: int, row_count: int, visible_rows: int) -> int:
    return max(0, min(selected - visible_rows // 2, max(0, row_count - visible_rows)))


def _orders_summary(state: _ConsoleState) -> str:
    tasks = _items(state.snapshot.get("tasks"))
    total = state.snapshot.get("task_total")
    filtered = f" matching /{_text(state.search_query, 40)}/" if state.search_query else ""
    if isinstance(total, int):
        first = state.task_offset + 1 if tasks else 0
        last = state.task_offset + len(tasks)
        return f"Orders: {total} total{filtered}; showing {first}-{last}"
    return f"Orders: {len(tasks)} shown{filtered}"


def _command_display(command: str, width: int) -> str:
    if width <= 0:
        return ""
    start = len(command)
    while start > 0 and cell_width(command[start - 1:]) <= width:
        start -= 1
    return command[start:]


def _input_prompt(state: _ConsoleState) -> str:
    return "Filter: " if state.palette_filter_editing else "/? " if state.search_editing else "> "


def _display_input(state: _ConsoleState, width: int) -> tuple[str, int]:
    source = state.palette_filter if state.palette_filter_editing else state.command
    cursor = len(source) if state.palette_filter_editing else state.command_cursor
    return input_view(source, cursor, width)


def _hint(state: _ConsoleState) -> str:
    if state.view == "Orders":
        return "Enter inspect Ctrl-R amend Ctrl-E add Ctrl-F find PgUp/Dn pages Ctrl-D exit"
    if state.view == "Log":
        return "PgUp older events PgDn newer events | arrows/wheel scroll | Ctrl-D exit"
    if state.view == "Stations":
        return "Enter choose station; recent history below | F1-8/Tab views | Ctrl-D exit"
    if state.view == "Palette":
        return "/ filter actions · Enter activate · click selects · F1-8 views · Ctrl-D exit"
    return _NAV_HINT


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


def _draw_compact(window: curses.window, state: _ConsoleState) -> None:
    sync_window_size(window)
    height, width = window.getmaxyx()
    window.erase()
    _put(window, 0, 0, _header(state, width), width, role="brand")
    rows = state.rows()
    if height >= 2:
        first = _orders_summary(state) if state.view == "Orders" else (rows[0] if rows else "Status unavailable")
        if state.view == "Bridge":
            forecasts = _capacity_summaries(state.snapshot)
            if forecasts:
                first = forecasts[0]
        _put(window, 1, 0, first, width, role="info")
    if height >= 3:
        _draw_navigation(window, state.view_index, " ", width)
    if height >= 4:
        selected = state.selected + (1 if state.view == "Bridge" else 2 if state.view == "Stations" else 0)
        detail = rows[min(selected, max(0, len(rows) - 1))] if rows else ""
        _put(window, 3, 0, detail, width, role="selected" if rows else "info")
    if height >= 5:
        prompt = _input_prompt(state)
        _put(window, 4, 0, prompt + _display_input(state, width - cell_width(prompt))[0], width, role="command", preserve_spacing=True)
    if height >= 6:
        hint = state.snapshot_error or (state.output[-1] if state.output else _hint(state))
        _put(window, 5, 0, hint, width, role=_status_role(hint) if state.snapshot_error or state.output else "dim")
    if (state.input_focused or state.palette_filter_editing) and height >= 6:
        available = max(1, width - cell_width(_input_prompt(state)))
        _, caret = _display_input(state, available)
        try:
            curses.curs_set(1)
            window.move(4, min(width - 1, cell_width(_input_prompt(state)) + caret))
        except curses.error:
            pass
    else:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
    window.noutrefresh()
    curses.doupdate()


def _draw_full(window: curses.window, state: _ConsoleState) -> None:
    sync_window_size(window)
    height, width = window.getmaxyx()
    window.erase()
    _put(window, 0, 0, _header(state, width), width, role="brand")
    mission = _mission(state.snapshot)
    if height >= 2:
        if state.view == "Orders":
            context = _orders_summary(state)
        else:
            context = "o Open full objective · Ctrl-] P Provider · ? Help" if state.view == "Bridge" else f"Objective preview: {_text(mission.get('objective', 'No objective recorded'), width - 20)}"
        _put(window, 1, 0, context, width, role="secondary")
    if height >= 3:
        _draw_navigation(window, state.view_index, "  ", width)
    content_height = max(0, height - 6)
    rows = state.rows()
    if state.view == "Usage":
        rows = [part for line in rows for part in (textwrap.wrap(line, max(1, width-2),
                replace_whitespace=False, drop_whitespace=False) or [""])]
    row_offset = state._selection_row_offset()
    selected_row = state.detail_scroll if state.view == "Orders" and state.task_detail else state.selected + row_offset
    start = _scroll_start(selected_row, len(rows), content_height)
    for offset, value in enumerate(rows[start : start + content_height]):
        marker = "> " if start + offset == selected_row else "  "
        if state.view == "Usage":
            clean = "".join(char if unicodedata.category(char) not in {"Cc", "Cs"} else " " for char in value)
            role = "selected" if start + offset == selected_row else _status_role(clean)
            _put(window, offset + 3, 0, marker + clean, width, role=role, preserve_spacing=True)
        else:
            role = "selected" if start + offset == selected_row else "info"
            _put(window, offset + 3, 0, marker + value, width, role=role)
    if height >= 2:
        prompt = _input_prompt(state)
        _put(window, height - 2, 0, prompt + _display_input(state, width - cell_width(prompt))[0], width, role="command", preserve_spacing=True)
        status = state.snapshot_error or (state.output[-1] if state.output else _hint(state))
        _put(window, height - 1, 0, status, width, role=_status_role(status) if state.snapshot_error or state.output else "dim")
    if (state.input_focused or state.palette_filter_editing) and height >= 2:
        available = max(1, width - cell_width(_input_prompt(state)))
        _, caret = _display_input(state, available)
        try:
            curses.curs_set(1)
            window.move(height - 2, min(width - 1, cell_width(_input_prompt(state)) + caret))
        except curses.error:
            pass
    else:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
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
    headings = viewer.headings()
    if state.document_heading_list:
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
    footer = (f"q close | {viewer.top + 1}/{max(1, len(viewer.lines))} {len(viewer.source.encode('utf-8'))}B "
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
        fd, raw_path = tempfile.mkstemp(prefix="llm-bridge-task-", suffix=".md")
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
            record = json.loads(state.dispatch(state.mission_id, shlex.join(["task", "show", "--task", task_id])))
            if not isinstance(record, dict) or str(record.get("id")) != task_id or not isinstance(record.get("prompt"), str):
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
            result = subprocess.run([*editor, str(path)], check=False)
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
            argv = ["add", str(path)] if task_id is None else ["amend", task_id, str(path)]
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
    if task_id is None and mission_status in {"COMPLETED", "CANCELLED"}:
        state.output.append(f"Cannot add a task: mission is {mission_status.lower()}. Choose another mission.")
        return
    if task_id is not None:
        task = next((item for item in _items(state.snapshot.get("tasks")) if str(item.get("id")) == task_id), {})
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
        command = ["task", "amend", "--task", task_id, "--file", str(path)]
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


def _selected_task_id(state: _ConsoleState) -> str | None:
    tasks = _items(state.snapshot.get("tasks"))
    if not tasks:
        return None
    index = min(max(0, state.selected), len(tasks) - 1)
    value = tasks[index].get("id")
    return _text(value, 40) if value is not None else None


def _execute_terminal_action(source_mission: str, action: TerminalAction) -> None:
    """Perform a typed terminal action after curses restores the user's terminal."""
    from .bridge import _load_handle
    from .terminal import TmuxTerminalBackend

    backend = TmuxTerminalBackend()
    destination = _load_handle(action.mission_id)
    tmux_env = os.environ.get("TMUX", "")
    pane_env = os.environ.get("TMUX_PANE", "")
    if not tmux_env:
        if action.action == "detach":
            backend.detach_client(destination, client_name=action.client)
            return
        if action.mission_id == source_mission:
            backend.focus(destination, pane="provider", expand=True)
        backend.attach(destination)
        return

    source = _load_handle(source_mission)
    source_socket = tmux_env.split(",", 1)[0]
    try:
        same_socket = Path(source_socket).expanduser().absolute() == source.socket_path.expanduser().absolute()
    except OSError:
        same_socket = False
    if not same_socket or pane_env != source.bridge_pane:
        raise PermissionError("terminal actions are available only from this mission's owned Bridge pane")
    if action.action == "detach":
        if action.mission_id != source_mission:
            raise PermissionError("detach from an embedded console is limited to its active mission")
        backend.detach_client(source, client_name=action.client)
        return
    if action.mission_id == source_mission:
        backend.focus(source, pane="provider", expand=True)
        return

    backend.switch_client_to(source, destination, client_name=action.client)


def _run(mission_id: str, dispatch: Dispatch, snapshot: Snapshot, *, full: bool, initial_view: str = "Bridge") -> int:
    # Ignore stale exported dimensions from a detached tmux station.
    curses.use_env(False)
    state = _ConsoleState(mission_id, dispatch, snapshot, full)
    paste_decoder = BracketedPaste()
    paste_candidate = bytearray()
    paste_activity = 0.0
    replay_keys: deque[object] = deque()
    escape_sequence = False
    status_publication: dict[str, object] = {"applied": None, "thread": None}
    state.choose_view(VIEWS.index(initial_view))
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
                escape_sequence = not (len(key) == 1 and "@" <= key <= "~")
                state.output.append("Unknown escape sequence ignored.")
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
        try:
            curses.nonl()
        except curses.error:
            pass
        window.timeout(100)
        last_draw = 0.0
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        except curses.error:
            pass
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
                continue
            if state.edit_document_requested and not paste_decoder.active:
                state.edit_document_requested = False
                _compose_task_prompt(window, state, task_id=state.document_task_id, initial_text=state.edit_initial_text)
                state.edit_initial_text = ""
                state.refresh(force=True)
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
            status_context = _station_status_context(state.snapshot)
            if os.environ.get("TMUX_PANE") and status_context != status_publication["applied"]:
                status_thread = status_publication["thread"]
                if not isinstance(status_thread, threading.Thread) or not status_thread.is_alive():
                    def publish_status(context: str = status_context) -> None:
                        try:
                            from .bridge import _load_handle
                            from .terminal import TmuxTerminalBackend

                            handle = _load_handle(mission_id)
                            if os.environ.get("TMUX_PANE") == handle.bridge_pane:
                                TmuxTerminalBackend().set_status_context(handle, context)
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
            now = time.monotonic()
            if not paste_decoder.active or now-last_draw >= .1:
                if paste_decoder.active and not paste_decoder.interrupted:
                    state.output.append(f"Receiving paste · {paste_decoder.received_bytes+len(paste_decoder.pending)} bytes; no command dispatched")
                if state.response_overlay:
                    _draw_response_overlay(window, state)
                elif state.document_viewer is not None:
                    _draw_document(window, state)
                elif state.full:
                    _draw_full(window, state)
                else:
                    _draw_compact(window, state)
                last_draw = now
            if replay_keys:
                key = replay_keys.popleft()
            else:
                try:
                    key = window.get_wch()
                except curses.error:
                    key = None
            if key is None:
                if escape_sequence and time.monotonic()-paste_activity >= .12:
                    escape_sequence = False
                    state.output.append("Unknown or incomplete escape sequence ignored.")
                if paste_candidate and time.monotonic() - paste_activity >= 0.12:
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
                task = state.selected_task() if state.view == "Orders" else None
                task_id = _selected_task_id(state) if task is not None else None
                if task_id is None or task is None:
                    state.output.append("Select an order in the Orders view before amending it.")
                else:
                    state.request_task_document(task_id, for_edit=True)
                continue
            if key == curses.KEY_MOUSE:
                try:
                    _mouse_id, x, y, _z, button_state = curses.getmouse()
                except curses.error:
                    continue
                if button_state & curses.BUTTON4_PRESSED:
                    state.handle_key("KEY_UP")
                elif button_state & curses.BUTTON5_PRESSED:
                    state.handle_key("KEY_DOWN")
                elif button_state & curses.BUTTON1_CLICKED:
                    _handle_mouse_event(state, y, x, *window.getmaxyx())
                elif button_state & getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0):
                    _handle_mouse_event(state, y, x, *window.getmaxyx(), activate=True)
                continue
            semantic = {
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
                result = curses.wrapper(terminal_loop)
            finally:
                sys.stdout.write("\x1b[?2004l")
                sys.stdout.flush()
        except KeyboardInterrupt:
            return 130
        if not isinstance(result, (TerminalAction, ControlViewAction)):
            return int(result)
        try:
            if isinstance(result, ControlViewAction):
                run_control(result.mission_id, dispatch, snapshot, initial_view=result.view)
            else:
                _execute_terminal_action(mission_id, result)
        except Exception as exc:
            state.output.append(f"Attach failed: {_text(exc, 180)}")
            state.last_refresh = 0.0


def run_console(mission_id: str, dispatch: Dispatch, snapshot: Snapshot) -> int:
    """Run the compact six-row command pane UI."""
    return _run(mission_id, dispatch, snapshot, full=False)


def run_control(mission_id: str, dispatch: Dispatch, snapshot: Snapshot, *, initial_view: str = "Bridge") -> int:
    """Run the full-screen terminal control UI."""
    return _run(mission_id, dispatch, snapshot, full=True, initial_view=initial_view)
