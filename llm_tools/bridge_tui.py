"""Small curses console for LLM Bridge's tmux command pane and control view.

The UI knows no persistence or provider APIs. It asks the injected snapshot
callback for a bounded projection and sends operator text to the same command
dispatcher used by the CLI.
"""

from __future__ import annotations

import curses
import os
import shlex
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


Snapshot = Callable[[str], dict[str, object]]
Dispatch = Callable[[str, str], str]

VIEWS = ("Bridge", "Orders", "Stations", "Systems", "Log", "Sessions", "Palette")
PALETTE_COMMANDS = (
    "help",
    "status",
    "orders",
    "events",
    "pause",
    "resume",
    "stop",
    "cancel",
    "add FILE",
    "amend ID FILE",
    "priority ID N",
)
_REFRESH_SECONDS = 0.25
_MAX_COMMAND_CHARS = 4096
_MAX_OUTPUT_LINES = 80
_MAX_OUTPUT_LINE_CHARS = 2000
_MAX_TASK_ROWS = 200
_MAX_STATION_ROWS = 32
_MAX_EVENT_ROWS = 80
_NAV_HINT = "Tab/←→/F1-F7 view  Ctrl-E add  Ctrl-R amend  Enter runs  Ctrl-D exits"


def _text(value: object, limit: int = 1000) -> str:
    """Make untrusted snapshot/result text safe for direct curses rendering."""
    rendered = str(value)
    clean = "".join(
        char if unicodedata.category(char) not in {"Cc", "Cs"} else " "
        for char in rendered
    )
    return " ".join(clean.split())[:limit]


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_dict(item) for item in value if isinstance(item, dict)]


def _projected_rows(value: object, limit: int, fields: tuple[str, ...]) -> list[dict[str, object]]:
    rows = _items(value)
    return [{key: _text(row.get(key, ""), 200) for key in fields if key in row} for row in rows[:limit]]


def _bounded_snapshot(data: dict[str, object]) -> dict[str, object]:
    """Retain only the small projection needed for one UI refresh."""
    result: dict[str, object] = {}
    mission = _dict(data.get("mission"))
    result["mission"] = {
        key: _text(mission.get(key, ""), 240 if key == "objective" else 100)
        for key in ("id", "name", "objective", "status", "cwd", "updated_at")
        if key in mission
    }
    result["tasks"] = _projected_rows(
        data.get("tasks"),
        _MAX_TASK_ROWS,
        ("id", "title", "prompt", "status", "priority", "ordering", "revision"),
    )
    result["stations"] = _projected_rows(
        data.get("stations"),
        _MAX_STATION_ROWS,
        ("id", "name", "provider", "model", "observed_model", "status", "restoration_state"),
    )
    result["events"] = _projected_rows(
        data.get("events"),
        _MAX_EVENT_ROWS,
        ("id", "event_type", "type", "kind", "summary", "created_at", "payload", "data"),
    )
    result["operations"] = _projected_rows(
        data.get("operations"),
        20,
        ("id", "operation_id", "task_id", "provider", "station", "route_id", "model", "phase", "status", "created_at"),
    )
    pending = data.get("pending_commands")
    result["pending_command_count"] = len(pending) if isinstance(pending, list) else 0
    result["task_counts"] = {
        _text(key, 32): count
        for key, count in _dict(data.get("task_counts")).items()
        if isinstance(count, (int, float))
    }
    total_tasks = data.get("task_total")
    if isinstance(total_tasks, int):
        result["task_total"] = total_tasks
    for key in ("controller", "capacity", "routes", "execution", "health", "persistence"):
        if key in data:
            result[key] = _text(data[key], 240)
    return result


def _mission(snapshot: dict[str, object]) -> dict[str, object]:
    nested = _dict(snapshot.get("mission"))
    return nested or snapshot


def _task_label(task: dict[str, object]) -> str:
    identifier = _text(task.get("id", "?"), 40)
    title = _text(task.get("title") or task.get("prompt") or "Untitled task", 90)
    state = _text(task.get("status", "unknown"), 24)
    return f"{identifier}  {state.upper()}  {title}"


def _station_label(station: dict[str, object]) -> str:
    name = _text(station.get("name") or station.get("provider") or station.get("id") or "Station", 60)
    model = _text(station.get("observed_model") or station.get("model") or "model unknown", 60)
    state = _text(station.get("restoration_state") or station.get("status") or "unknown", 24)
    return f"{name}  {model}  {state}"


def _nav_labels(view_index: int) -> list[str]:
    return [
        f"[{index + 1} {name}]" if index == view_index else f"{index + 1} {name}"
        for index, name in enumerate(VIEWS)
    ]


def _nav_line(view_index: int, separator: str) -> str:
    return separator.join(_nav_labels(view_index))


def _nav_index_at(x: int, view_index: int, width: int, separator: str) -> int:
    cursor = 0
    nearest_index = 0
    nearest_distance = width
    for index, label in enumerate(_nav_labels(view_index)):
        end = cursor + len(label)
        if cursor <= x < end:
            return index
        distance = min(abs(x - cursor), abs(x - (end - 1)))
        if distance < nearest_distance:
            nearest_index, nearest_distance = index, distance
        cursor = end + len(separator)
    return nearest_index


@dataclass
class _ConsoleState:
    mission_id: str
    dispatch: Dispatch
    snapshot_reader: Snapshot
    full: bool
    view_index: int = 0
    selected: int = 0
    command: str = ""
    history: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    history_index: int | None = None
    output: deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_OUTPUT_LINES))
    snapshot: dict[str, object] = field(default_factory=dict)
    snapshot_error: str = ""
    last_refresh: float = 0.0
    _result_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _snapshot_thread: threading.Thread | None = field(default=None, repr=False)
    _snapshot_result: tuple[dict[str, object] | None, str] | None = field(default=None, repr=False)
    _command_result: str | None = field(default=None, repr=False)
    command_pending: bool = False

    @property
    def view(self) -> str:
        return VIEWS[self.view_index]

    def refresh(self, now: float | None = None, *, force: bool = False) -> None:
        current = time.monotonic() if now is None else now
        with self._result_lock:
            snapshot_result, self._snapshot_result = self._snapshot_result, None
            command_result, self._command_result = self._command_result, None
            snapshot_thread = self._snapshot_thread
        if snapshot_result is not None:
            data, error = snapshot_result
            if data is not None:
                self.snapshot = data
                self.snapshot_error = ""
            else:
                self.snapshot_error = error
        if command_result is not None:
            if command_result:
                self.output.extend(command_result.splitlines() or [command_result])
            self.command_pending = False
            self.last_refresh = 0.0
        if not force and current - self.last_refresh < _REFRESH_SECONDS:
            return

        def read_snapshot() -> None:
            try:
                data = self.snapshot_reader(self.mission_id)
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

    def _dispatch(self, line: str, *, cleanup_path: Path | None = None) -> bool:
        text = line.strip()
        if not text:
            return True
        if self.command_pending:
            self.output.append("A command is still running; wait for its result before dispatching another.")
            return False
        self.history.append(text)
        self.history_index = None
        self.command_pending = True
        self.output.append("Command submitted; waiting for the durable result.")

        def run_command() -> None:
            try:
                try:
                    result = _text(self.dispatch(self.mission_id, text), _MAX_OUTPUT_LINE_CHARS)
                except Exception as exc:
                    result = f"Command failed: {_text(exc, 180)}"
                with self._result_lock:
                    self._command_result = result
            finally:
                if cleanup_path is not None:
                    try:
                        cleanup_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass

        thread = threading.Thread(target=run_command, name="llm-bridge-command", daemon=False)
        thread.start()
        return True

    def handle_key(self, key: object) -> bool:
        """Apply one semantic key; return False when the console should exit."""
        if key == "	":
            self.view_index = (self.view_index + 1) % len(VIEWS)
            self.selected = 0
            return True
        if key == "KEY_BTAB":
            self.view_index = (self.view_index - 1) % len(VIEWS)
            self.selected = 0
            return True
        if key == "\x10":  # Ctrl-P recalls the previous command.
            self._history(-1)
            return True
        if key == "\x0e":  # Ctrl-N advances through command history.
            self._history(1)
            return True
        if isinstance(key, str) and key.startswith("KEY_F"):
            try:
                self.choose_view(int(key[5:]) - 1)
            except ValueError:
                pass
            return True
        if key in {"KEY_LEFT", "KEY_RIGHT"} and not self.command:
            direction = -1 if key == "KEY_LEFT" else 1
            self.view_index = (self.view_index + direction) % len(VIEWS)
            self.selected = 0
            return True
        if key in {"KEY_UP", "KEY_DOWN"} and not self.command:
            self.selected = max(0, self.selected - 1) if key == "KEY_UP" else self.selected + 1
            return True
        if key in {"KEY_BACKSPACE", "\x7f", "\b"}:
            self.command = self.command[:-1]
            return True
        if key == "\x15":  # Ctrl-U clears the current input line.
            self.command = ""
            return True
        if key == "\x1b":
            self.command = ""
            return True
        if key in {"\n", "\r", "KEY_ENTER"}:
            if self.view == "Palette" and not self.command:
                command = PALETTE_COMMANDS[self.selected % len(PALETTE_COMMANDS)]
                self._dispatch(command)
            else:
                if self._dispatch(self.command):
                    self.command = ""
            return True
        if key in {"KEY_RESIZE", "KEY_MOUSE"}:
            return True
        if key in {"\x03", "\x04"}:
            if self.command:
                self.command = ""
                return True
            if self.command_pending:
                self.output.append("Wait for the command result before exiting.")
                return True
            return False
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            if len(self.command) < _MAX_COMMAND_CHARS:
                self.command += key
        return True

    def _history(self, direction: int) -> None:
        if not self.history:
            return
        if self.history_index is None:
            self.history_index = len(self.history)
        self.history_index = min(len(self.history), max(0, self.history_index + direction))
        self.command = "" if self.history_index == len(self.history) else self.history[self.history_index]

    def choose_view(self, index: int) -> None:
        if 0 <= index < len(VIEWS):
            self.view_index = index
            self.selected = 0

    def rows(self) -> list[str]:
        data = self.snapshot
        mission = _mission(data)
        tasks = _items(data.get("tasks"))
        stations = _items(data.get("stations"))
        events = _items(data.get("events"))
        operations = _items(data.get("operations"))
        task_counts = _dict(data.get("task_counts"))
        if self.view == "Bridge":
            next_task = next((task for task in tasks if task.get("status") in {"READY", "QUEUED"}), None)
            counts = " ".join(
                f"{_text(state, 12).lower()} {count}"
                for state, count in task_counts.items()
                if isinstance(count, (int, float))
            )
            return [
                f"Mission: {_text(mission.get('name') or self.mission_id, 90)} [{_text(mission.get('status', 'unknown'), 24)}]",
                f"Objective: {_text(mission.get('objective', 'No objective recorded'), 180)}",
                f"Orders: {counts or f'{len(tasks)} visible'}",
                f"Next: {_task_label(next_task) if next_task else 'none ready'}",
                f"Pending commands: {data.get('pending_command_count', 0)}",
                f"Operations observed: {len(operations)} | Events loaded: {len(events)}",
            ]
        if self.view == "Orders":
            return [_task_label(task) for task in tasks] or ["No tasks recorded."]
        if self.view == "Stations":
            if stations:
                return [_station_label(item) for item in stations]
            observed = {
                (_text(item.get("station") or item.get("provider") or "unknown", 60),
                 _text(item.get("model") or "model unknown", 60),
                 _text(item.get("status") or "unknown", 24))
                for item in operations
            }
            return [f"{station}  {model}  {status}" for station, model, status in sorted(observed)] or ["No stations recorded."]
        if self.view == "Systems":
            keys = ("controller", "capacity", "routes", "execution", "health", "persistence")
            found = [f"{key}: {_text(data[key], 220)}" for key in keys if key in data]
            return found or ["System and capacity status is not available in this snapshot."]
        if self.view == "Log":
            return [
                f"{_text(event.get('id', '?'), 24)}  {_text(event.get('event_type') or event.get('type') or event.get('kind') or 'event', 36)}  {_text(event.get('payload') or event.get('data') or event.get('summary') or event.get('created_at') or '', 150)}"
                for event in events
            ] or ["No recent mission events."]
        if self.view == "Sessions":
            return [
                f"{_text(item.get('id') or item.get('operation_id') or '?', 30)}  {_text(item.get('station') or item.get('provider') or 'station unknown', 32)}  {_text(item.get('route_id') or item.get('route') or 'route unknown', 32)}  {_text(item.get('phase') or item.get('status') or 'unknown', 24)}"
                for item in operations
            ] or [_station_label(station) for station in stations] or ["No session activity recorded."]
        if self.view == "Palette":
            return [f"{'> ' if i == self.selected % len(PALETTE_COMMANDS) else '  '}{command}" for i, command in enumerate(PALETTE_COMMANDS)]
        return []

    def handle_mouse(self, y: int, x: int, height: int, width: int) -> None:
        if height <= 0 or width <= 0:
            return
        nav_y = 2
        if y == nav_y:
            x = max(0, min(width - 1, x))
            self.choose_view(_nav_index_at(x, self.view_index, width, "  " if self.full else " "))
        elif self.view == "Palette" and y >= nav_y + 1:
            self.selected = max(0, min(len(PALETTE_COMMANDS) - 1, y - nav_y - 1))
            self.command = PALETTE_COMMANDS[self.selected]
        elif self.full and y >= 3:
            rows = self.rows()
            content_height = max(1, height - 6)
            start = _scroll_start(self.selected, len(rows), content_height)
            self.selected = min(max(0, len(rows) - 1), start + y - 3)


def _put(window: curses.window, y: int, x: int, value: object, width: int) -> None:
    if width <= 0:
        return
    try:
        window.addnstr(y, x, _text(value, width), width)
    except curses.error:
        pass


def _header(state: _ConsoleState, width: int) -> str:
    mission = _mission(state.snapshot)
    name = _text(mission.get("name") or state.mission_id, 36)
    status = _text(mission.get("status", "unknown"), 20).upper()
    return f"LLM BRIDGE  {name}  {status}  {state.view}"


def _scroll_start(selected: int, row_count: int, visible_rows: int) -> int:
    return max(0, min(selected - visible_rows // 2, max(0, row_count - visible_rows)))


def _orders_summary(state: _ConsoleState) -> str:
    tasks = _items(state.snapshot.get("tasks"))
    total = state.snapshot.get("task_total")
    if isinstance(total, int):
        return f"Orders: {total} total; showing {len(tasks)}"
    return f"Orders: {len(tasks)} shown"


def _command_display(command: str, width: int) -> str:
    if width <= 0:
        return ""
    return command[-width:]


def _draw_compact(window: curses.window, state: _ConsoleState) -> None:
    height, width = window.getmaxyx()
    window.erase()
    _put(window, 0, 0, _header(state, width), width)
    rows = state.rows()
    if height >= 2:
        first = _orders_summary(state) if state.view == "Orders" else (rows[0] if rows else "Status unavailable")
        _put(window, 1, 0, first, width)
    if height >= 3:
        nav = _nav_line(state.view_index, " ")
        _put(window, 2, 0, nav, width)
    if height >= 4:
        selected = state.selected + (1 if state.view == "Bridge" else 0)
        detail = rows[min(selected, max(0, len(rows) - 1))] if rows else ""
        _put(window, 3, 0, detail, width)
    if height >= 5:
        _put(window, 4, 0, "> " + _command_display(state.command, width - 2), width - 2)
    if height >= 6:
        hint = state.snapshot_error or (state.output[-1] if state.output else _NAV_HINT)
        _put(window, 5, 0, hint, width)
    window.noutrefresh()
    curses.doupdate()


def _draw_full(window: curses.window, state: _ConsoleState) -> None:
    height, width = window.getmaxyx()
    window.erase()
    _put(window, 0, 0, _header(state, width), width)
    mission = _mission(state.snapshot)
    if height >= 2:
        if state.view == "Orders":
            context = _orders_summary(state)
        else:
            context = f"Objective: {_text(mission.get('objective', 'No objective recorded'), width - 12)}"
        _put(window, 1, 0, context, width)
    if height >= 3:
        nav = _nav_line(state.view_index, "  ")
        _put(window, 2, 0, nav, width)
    content_height = max(0, height - 6)
    rows = state.rows()
    start = _scroll_start(state.selected, len(rows), content_height)
    for offset, value in enumerate(rows[start : start + content_height]):
        marker = "> " if start + offset == state.selected else "  "
        _put(window, offset + 3, 0, marker + value, width)
    if height >= 2:
        _put(window, height - 2, 0, "> " + _command_display(state.command, width - 2), width - 2)
        status = state.snapshot_error or (state.output[-1] if state.output else _NAV_HINT)
        _put(window, height - 1, 0, status, width)
    window.noutrefresh()
    curses.doupdate()


def _edit_task_prompt(window: curses.window, state: _ConsoleState, task_id: str | None = None) -> None:
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
        argv = ["add", str(path)] if task_id is None else ["amend", task_id, str(path)]
        if not state._dispatch(shlex.join(argv), cleanup_path=path):
            path.unlink(missing_ok=True)
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


def _selected_task_id(state: _ConsoleState) -> str | None:
    tasks = _items(state.snapshot.get("tasks"))
    if not tasks:
        return None
    index = min(max(0, state.selected), len(tasks) - 1)
    value = tasks[index].get("id")
    return _text(value, 40) if value is not None else None


def _run(mission_id: str, dispatch: Dispatch, snapshot: Snapshot, *, full: bool) -> int:
    state = _ConsoleState(mission_id, dispatch, snapshot, full)
    state.refresh(force=True)

    def loop(window: curses.window) -> int:
        window.keypad(True)
        window.timeout(100)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        except curses.error:
            pass
        while True:
            state.refresh()
            if full:
                _draw_full(window, state)
            else:
                _draw_compact(window, state)
            try:
                key = window.get_wch()
            except curses.error:
                continue
            if key == "\x05":
                _edit_task_prompt(window, state)
                state.refresh(force=True)
                continue
            if key == "\x12":
                task_id = _selected_task_id(state) if state.view == "Orders" else None
                if task_id is None:
                    state.output.append("Select an order in the Orders view before amending it.")
                else:
                    _edit_task_prompt(window, state, task_id)
                state.refresh(force=True)
                continue
            if key == curses.KEY_MOUSE:
                try:
                    _mouse_id, x, y, _z, button_state = curses.getmouse()
                except curses.error:
                    continue
                if button_state & curses.BUTTON4_PRESSED:
                    state.selected = max(0, state.selected - 1)
                elif button_state & curses.BUTTON5_PRESSED:
                    state.selected += 1
                elif button_state & curses.BUTTON1_CLICKED:
                    state.handle_mouse(y, x, *window.getmaxyx())
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
            }.get(key, key)
            if isinstance(key, int) and curses.KEY_F0 < key <= curses.KEY_F0 + len(VIEWS):
                semantic = f"KEY_F{key - curses.KEY_F0}"
            if not state.handle_key(semantic):
                return 0

    try:
        return curses.wrapper(loop)
    except KeyboardInterrupt:
        return 130


def run_console(mission_id: str, dispatch: Dispatch, snapshot: Snapshot) -> int:
    """Run the compact six-row command pane UI."""
    return _run(mission_id, dispatch, snapshot, full=False)


def run_control(mission_id: str, dispatch: Dispatch, snapshot: Snapshot) -> int:
    """Run the full-screen seven-view terminal control UI."""
    return _run(mission_id, dispatch, snapshot, full=True)
