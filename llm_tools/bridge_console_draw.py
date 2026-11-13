"""Curses drawing for the console."""

from __future__ import annotations

import curses
import functools
import re
import textwrap
import time
import unicodedata
from typing import TYPE_CHECKING, Callable
from . import terminal
from .bridge_input import cell_width, cell_slice, input_view
from .bridge_states import ACTIVE_OPERATION_STATUSES, ENDED_MISSION_STATUSES
from . import bridge_redraw, bridge_theme
from .bridge_glyphs import ascii_mode, glyph, localize, spinner_frames
from .bridge_event_text import _SEP, _dict, _short_id, _text
from .bridge_draw import QUIET, Segment, _clip_segments, _put, _put_segments, _segments_text, _split_hint, sync_window_size
from .bridge_console_commands import _BRIDGE_LABEL, _SYSTEM_LABEL, _items, _slash_matches
from .bridge_console_style import _FINISHED_STATES, _idle_role, _inline_segments, _provider_role, _state_look, _state_text, _status_role
from .bridge_console_text import _USAGE_HEADER, _active_owner, _activity_role, _capacity_summaries, _clip, _controller_line, _mission, _mission_role_of, _mission_state_word, _nav_layout, _order_preview_lines, _order_separator_segments, _orders_list_height, _recent_event_rows, _slice_segments, _status_strip, _task_numbers

if TYPE_CHECKING:
    from .bridge_tui import _ConsoleState

_inside_station = terminal.inside_station


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
    """`LLM BRIDGE · ▌ name (ID) · ● STATE`: wordmark, mission chip, name and short ID, state glyph and word."""
    mission = _mission(state.snapshot)
    if state.snapshot:
        status = _text(mission.get("status", "unknown"), 20).upper()
    else:
        status = "UNAVAILABLE" if state.snapshot_error else "LOADING"
    context = _dict(state.snapshot.get("station_context"))
    if status == "PAUSED" and context.get("executing_operation") and str(context.get("operation_status", "")).upper() in ACTIVE_OPERATION_STATUSES:
        status = "PAUSING"
    status = _mission_state_word(state.snapshot, status)
    shown = _state_text(status)
    badge = ""
    if state.outside_station:
        badge = "outside station" if status in ENDED_MISSION_STATUSES else f"outside station{_SEP}/attach"
    room = width - len("LLM BRIDGE") - 2 * len(_SEP) - len(shown) - 2 - (len(badge) + len(_SEP) if badge else 0)
    if badge and room < 12:  # no space for the mission name next to the badge: the name wins
        badge = ""
        room = width - len("LLM BRIDGE") - 2 * len(_SEP) - len(shown) - 2
    # The short ID tells two missions with the same name apart without relying on the identity colour (CLIUX-038).
    id_text = f" ({_short_id(state.mission_id)})"
    if room - len(id_text) < 12:
        id_text = ""
    name = _clip(_text(mission.get("name") or state.mission_id, 120), max(8, room - len(id_text)))
    chip_role = _mission_role_of(state.snapshot, state.mission_id)
    # The identity bar carries colour only.  Its ASCII form is the same character as the ASCII
    # separator (`| |`), so ASCII mode leaves it out of the header.
    mark: list[Segment] = [] if ascii_mode() else [(glyph("chip"), chip_role)]
    return [
        ("LLM BRIDGE", "brand"), (_SEP, "dim"), *mark, ((" " if mark else "") + name, "info"), *([(id_text, "dim")] if id_text else []), (_SEP, "dim"),
        (shown, _mission_status_role(status)),
        *([(_SEP, "dim"), (badge, "warn")] if badge else []),
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


def _slash_popup_rows(state: _ConsoleState, width: int, room: int) -> list[list[Segment]]:
    """The rows of the slash-command list (at most `room`), or none when no command name is being typed."""
    matches = _slash_matches(state.command)
    if matches is None or room < 2:
        return []
    if not matches:
        return [_inline_segments(f"No command starts with {_clip(state.command, 24)}", "warn") + [(f"{_SEP}/help lists them", "dim")]]
    rows: list[list[Segment]] = [[("Commands", "heading"), (f"{_SEP}Tab completes{_SEP}type to narrow", "dim")]]
    shown = room - 1
    if len(matches) > shown:
        shown -= 1
    name_width = min(26, max(cell_width(name) for name, _ in matches[:shown]))
    for name, text in matches[:shown]:
        pad = " " * (name_width - min(name_width, cell_width(name)) + 2)
        rows.append([("  ", "info"), (name, "key"), (pad, "info"), (text, "muted")])
    if len(matches) > shown:
        rows.append([(f"  {len(matches) - shown} more{_SEP}keep typing to narrow the list", "dim")])
    return rows


def _draw_slash_popup(window: curses.window, state: _ConsoleState, prompt_y: int, width: int, top: int) -> None:
    """Draw the slash-command list in the rows just above the command line."""
    if not state.input_focused or state.palette_filter_editing or state.search_editing:
        return
    rows = _slash_popup_rows(state, width, min(9, prompt_y - top))
    for offset, segments in enumerate(rows):
        y = prompt_y - len(rows) + offset
        _put(window, y, 0, " " * width, width, role="info")
        _put_segments(window, y, 0, _clip_segments(segments, width), width)


def _hint_items(state: _ConsoleState) -> tuple[list[str], str]:
    """Keys that work in the current view, most useful first, plus the closing hint."""
    if state.palette_filter_editing:
        if state.palette_quick:
            return ["type to filter actions", "↑↓ choose", "Enter run", "Esc cancel"], ""
        return ["type to filter actions", "Enter or Esc finishes"], ""
    if state.search_editing:
        return ["type text to find in tasks", "Enter search", "Esc cancel", "Ctrl-U clear"], ""
    if state.input_focused:
        matches = _slash_matches(state.command) if not state.full else None
        if matches:  # a short pane has no room for the list: name the matches in the hint row
            return [name for name, _ in matches[:6]], "Tab completes"
        return ["Enter run", "Tab complete", "Ctrl-P/Ctrl-N history", "Ctrl-U clear", "Esc back to view"], "/help commands"
    views = "Tab/F1-9 views"
    # The empty command line already says "type a command or /help".
    view = state.view
    if view == "Overview":
        items = ["o objective", "Ctrl-E add task", views, "Ctrl-D exit"]  # Ctrl-K is on the prompt line
        if _inside_station():
            items.insert(0, "Ctrl-] P provider")
            if not state.full:
                items.append("Ctrl-] z expand")
    elif view == "Tasks" and state.task_detail:
        items = ["Esc back", "↑↓ scroll", "v view prompt", "Ctrl-R amend", "PgUp/PgDn page", views, "Ctrl-D exit"]
    elif view == "Tasks":
        items = ["Enter inspect", "↑↓ select", "Ctrl-E add task", "Ctrl-R amend", "v view prompt", "Ctrl-F find", views, "Ctrl-D exit", "PgUp/PgDn page"]
        if state.search_query:
            items.insert(0, "Esc clear filter")
    elif view == "Providers":
        items = ["Enter mark viewed", "↑↓ select", views, "Ctrl-D exit"]
    elif view == "Activity":
        items = ["PgUp older", "PgDn newer", "Ctrl-T show all" if state.hide_plumbing else "Ctrl-T hide bookkeeping", "↑↓ scroll", views, "Ctrl-D exit"]
    elif view == "Actions":
        items = ["Enter run action", "↑↓ select", views, "Ctrl-D exit", "Ctrl-G command line"]  # Ctrl-K is on the prompt line
    elif view == "Usage":
        items = ["↑↓ scroll", "l legend", "Shift-←/→ pan", views, "Ctrl-D exit"]
    else:
        items = ["↑↓ scroll", views, "Ctrl-D exit"]
    return items, "? help"


_QUIT_HINT = "Ctrl-D exit"


def _hint_chosen(state: _ConsoleState, width: int) -> list[str]:
    """The hint items that fit `width` (lowest-value ones dropped first), closing hint last.

    The key that leaves the console and the closing `? help` hint are never dropped.
    """
    items, closing = _hint_items(state)
    chosen = list(items)
    while chosen:
        parts = [*chosen, closing] if closing else chosen
        if cell_width(localize(_SEP.join(parts))) <= width:
            return parts
        droppable = [index for index, item in enumerate(chosen) if item != _QUIT_HINT]
        if not droppable:
            break
        chosen.pop(droppable[-1])
    return [_clip(closing or items[0], width)]  # too narrow even for the quit key: `? help` explains it


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
    action_note = state.action_note(avail) if state.view == "Actions" and content_height > 2 else ""
    if action_note:
        visible = content_height - 1  # the last content row shows the selected action's full description
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
            if segment_rows is not None and index < len(segment_rows):
                parts = _slice_segments(segment_rows[index], horizontal, avail)
                _put_segments(window, y, gutter, _clip_segments(parts, avail, mark=False), avail)
            else:
                role = roles[index] if roles is not None and index < len(roles) else "info"
                if index > 0 and _USAGE_HEADER.match(clean):
                    role = "heading"
                elif index > 0 and clean.strip() and set(clean.strip()) <= {"─", "-", " "}:
                    role = "dim"
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
    if action_note:
        _put(window, content_top + content_height - 1, gutter, _clip(action_note, avail), avail, role="muted")
    if preview_task is not None and list_height is not None:
        separator_y = content_top + list_height
        _put_segments(window, separator_y, gutter, _order_separator_segments(preview_task, avail), avail)
        for offset, (label_text, value, value_role) in enumerate(_order_preview_lines(preview_task, avail, content_height - list_height - 1)):
            y = separator_y + 1 + offset
            _put(window, y, gutter, f"{label_text:<{_BRIDGE_LABEL}}", avail, role="muted")
            _put(window, y, gutter + _BRIDGE_LABEL, value, max(0, avail - _BRIDGE_LABEL), role=value_role)
    if height >= 2:
        _draw_slash_popup(window, state, height - 2, width, 3)
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
        _put(window, 1, 0, _SEP.join(("Arrows/PgUp/PgDn scroll", "Esc/Enter close", "F1-9/Tab switch view")), width, role="dim")
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
    _put(window, 0, 0, viewer.title if viewer.plain else f"{viewer.title} · read-only · {mode}{degraded}", width, role="heading")
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
    if viewer.plain:
        footer = _SEP.join(["↑↓ scroll", "/ find", "Esc close", *([viewer.footer_extra] if viewer.footer_extra else [])])
    if state.document_heading_list:
        footer = "Headings · Up/Down select · Enter go · Esc close"
    elif state.document_notice:
        footer = ("Esc close | " if viewer.plain else "q close | ") + state.document_notice
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
