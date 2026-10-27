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
import stat
import subprocess
import tempfile
import threading
import textwrap
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .terminal import TerminalAction


Snapshot = Callable[..., dict[str, object]]
Dispatch = Callable[[str, str], object]


@dataclass(frozen=True)
class ControlViewAction:
    """Open a parsed control command after restoring the current terminal."""

    mission_id: str
    view: str

VIEWS = ("Bridge", "Orders", "Stations", "Systems", "Log", "Sessions", "Palette", "Usage")
PALETTE_COMMANDS = (
    "help",
    "status",
    "orders",
    "events",
    "pause",
    "resume",
    "stop",
    "cancel",
    "steer --help",
    "attach",
    "detach",
    "usage",
    "control",
    "task add --help",
    "proposal list",
)
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
    active = str(selection.get("route") or selection.get("provider") or "")
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
        label = "ACTIVE " if owner == active else "READY " if str(row.get("usable")) in {"True", "true"} else "ALT "
        summaries.append(label + _text(owner, 40) + (" / " + _text(row["selected_model"], 40) if row.get("selected_model") else "") + " | " + (" | ".join(scopes) or "capacity unavailable"))
    return sorted(summaries, key=lambda text: not text.startswith("ACTIVE "))


def _task_label(task: dict[str, object]) -> str:
    identifier = _text(task.get("id", "?"), 40)
    title = _text(task.get("title") or task.get("prompt") or "Untitled task", 90)
    state = _text(task.get("status", "unknown"), 24)
    return f"{identifier}  {state.upper()}  {title}"


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
    task_offset: int = 0
    event_offset: int = 0
    event_anchor: int | None = None
    command: str = ""
    search_query: str = ""
    search_editing: bool = False
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
    _command_result: str | TerminalAction | ControlViewAction | None = field(default=None, repr=False)
    terminal_action: TerminalAction | ControlViewAction | None = field(default=None, repr=False)
    command_pending: bool = False
    expand_requested: bool = False
    response_overlay: bool = False
    response_scroll: int = 0
    response_page_size: int = 10

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
            if command_result:
                response_lines = command_result.splitlines() or [command_result]
                if len(response_lines) > 1:
                    self.output.clear()
                    self.output.extend(response_lines)
                    self.response_overlay = True
                    self.response_scroll = 0
                    self.expand_requested = True
                else:
                    self.output.append(response_lines[0])
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

    def _dispatch(self, line: str, *, cleanup_path: Path | None = None) -> bool:
        text = line.strip()
        if not text:
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
        self.output.append("Command submitted; waiting for the durable result.")

        def run_command() -> None:
            try:
                try:
                    raw_result = self.dispatch(self.mission_id, text)
                    result = raw_result if isinstance(raw_result, (TerminalAction, ControlViewAction)) else _command_feedback(text, str(raw_result))
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
        if self.response_overlay:
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
        if key == "	":
            self.view_index = (self.view_index + 1) % len(VIEWS)
            self.selected = 0
            return True
        if key == "KEY_BTAB":
            self.view_index = (self.view_index - 1) % len(VIEWS)
            self.selected = 0
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
        if key == "\x06":  # Ctrl-F edits the Orders filter.
            self.command = self.search_query
            self.search_editing = True
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
            if self.view == "Orders" and self.task_detail:
                self.detail_scroll = max(0, self.detail_scroll - 1) if key == "KEY_UP" else self.detail_scroll + 1
            else:
                self.selected = max(0, self.selected - 1) if key == "KEY_UP" else self.selected + 1
            return True
        if key in {"KEY_PPAGE", "KEY_NPAGE"} and not self.command and not self.search_editing:
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
                self.selected = max(0, self.selected - delta) if key == "KEY_PPAGE" else self.selected + delta
            return True
        if key in {"KEY_BACKSPACE", "\x7f", "\b"}:
            self.command = self.command[:-1]
            return True
        if key == "\x15":  # Ctrl-U clears the current input line.
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
            self.command = ""
            self.search_editing = False
            return True
        if key in {"\n", "\r", "KEY_ENTER"}:
            if self.search_editing:
                self.search_query = self.command.strip()
                self.command = ""
                self.search_editing = False
                self.task_offset = 0
                self.selected = 0
                self.last_refresh = 0.0
                return True
            if self.view == "Palette" and not self.command:
                command = PALETTE_COMMANDS[self.selected % len(PALETTE_COMMANDS)]
                self._dispatch(command)
            elif self.view == "Orders" and not self.command:
                self.task_detail = not self.task_detail
                self.detail_scroll = 0
            elif self.view == "Stations" and not self.command:
                station = self.selected_station_name()
                if station:
                    self._dispatch("station " + shlex.quote(station))
            else:
                if self._dispatch(self.command):
                    self.command = ""
            return True
        if key in {"KEY_RESIZE", "KEY_MOUSE"}:
            return True
        if key in {"\x03", "\x04"}:
            if self.search_editing:
                self.command = ""
                self.search_editing = False
                return True
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
            return [
                f"Mission: {_text(mission.get('name') or self.mission_id, 90)} [{_text(mission.get('status', 'unknown'), 24)}]",
                f"Objective: {_text(mission.get('objective', 'No objective recorded'), 180)}",
                f"Orders: {counts or f'{len(tasks)} visible'}" + (f" | {proposal_summary}" if proposal_summary else ""),
                f"Executing: {_task_label(current_task) if current_task else 'none'}",
                f"Next: {_task_label(next_task) if next_task else 'none ready'}",
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
                    "Prompt: " + _text(task.get("prompt") or "", 800),
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
            return [f"{'> ' if i == self.selected % len(PALETTE_COMMANDS) else '  '}{command}" for i, command in enumerate(PALETTE_COMMANDS)]
        return []

    def handle_mouse(self, y: int, x: int, height: int, width: int) -> None:
        if self.response_overlay:
            return
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
            row_offset = self._selection_row_offset()
            selected_row = self.detail_scroll if self.view == "Orders" and self.task_detail else self.selected + row_offset
            start = _scroll_start(selected_row, len(rows), content_height)
            row_index = min(max(0, len(rows) - 1), start + y - 3)
            if self.view == "Orders" and self.task_detail:
                self.detail_scroll = row_index
            else:
                self.selected = max(0, row_index - row_offset)


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
    filtered = f" matching /{_text(state.search_query, 40)}/" if state.search_query else ""
    if isinstance(total, int):
        first = state.task_offset + 1 if tasks else 0
        last = state.task_offset + len(tasks)
        return f"Orders: {total} total{filtered}; showing {first}-{last}"
    return f"Orders: {len(tasks)} shown{filtered}"


def _command_display(command: str, width: int) -> str:
    if width <= 0:
        return ""
    return command[-width:]


def _input_prompt(state: _ConsoleState) -> str:
    return "/? " if state.search_editing else "> "


def _hint(state: _ConsoleState) -> str:
    if state.view == "Orders":
        return "Enter inspect Ctrl-R amend Ctrl-E add Ctrl-F find PgUp/Dn pages Ctrl-D exit"
    if state.view == "Log":
        return "PgUp older events PgDn newer events | arrows/wheel scroll | Ctrl-D exit"
    if state.view == "Stations":
        return "Enter choose station; recent history below | F1-8/Tab views | Ctrl-D exit"
    if state.view == "Palette":
        return "Enter run selected command | type command | F1-8/Tab views | Ctrl-D exit"
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
    height, width = window.getmaxyx()
    window.erase()
    _put(window, 0, 0, _header(state, width), width)
    rows = state.rows()
    if height >= 2:
        first = _orders_summary(state) if state.view == "Orders" else (rows[0] if rows else "Status unavailable")
        if state.view == "Bridge":
            forecasts = _capacity_summaries(state.snapshot)
            if forecasts:
                first = forecasts[0]
        _put(window, 1, 0, first, width)
    if height >= 3:
        nav = _nav_line(state.view_index, " ")
        _put(window, 2, 0, nav, width)
    if height >= 4:
        selected = state.selected + (1 if state.view == "Bridge" else 2 if state.view == "Stations" else 0)
        detail = rows[min(selected, max(0, len(rows) - 1))] if rows else ""
        _put(window, 3, 0, detail, width)
    if height >= 5:
        prompt = _input_prompt(state)
        _put(window, 4, 0, prompt + _command_display(state.command, width - len(prompt)), width - len(prompt))
    if height >= 6:
        hint = state.snapshot_error or (state.output[-1] if state.output else _hint(state))
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
            try:
                window.addnstr(offset+3, 0, marker+clean, width)
            except curses.error:
                pass
        else:
            _put(window, offset + 3, 0, marker + value, width)
    if height >= 2:
        prompt = _input_prompt(state)
        _put(window, height - 2, 0, prompt + _command_display(state.command, width - len(prompt)), width - len(prompt))
        status = state.snapshot_error or (state.output[-1] if state.output else _hint(state))
        _put(window, height - 1, 0, status, width)
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
    height, width = window.getmaxyx()
    window.erase()
    _put(window, 0, 0, "Command response", width)
    if height >= 2:
        _put(window, 1, 0, "Arrows/PgUp/PgDn scroll | Esc/Enter close | F1-8/Tab switch view", width)
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
        try:
            window.addnstr(content_top + offset, 0, safe, width)
        except curses.error:
            pass
    if footer_rows:
        if rows:
            visible_start = state.response_scroll + 1 if content_height else 0
            visible_end = min(len(rows), state.response_scroll + content_height)
            counter = f"{visible_start}-{visible_end}/{len(rows)}" if content_height else f"0/{len(rows)}"
        else:
            counter = "0/0"
        _put(window, height - 1, 0, f"{counter} response lines", width)
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
            backend.focus(destination, pane="provider")
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
        backend.focus(source, pane="provider")
        return

    backend.switch_client_to(source, destination, client_name=action.client)


def _run(mission_id: str, dispatch: Dispatch, snapshot: Snapshot, *, full: bool, initial_view: str = "Bridge") -> int:
    state = _ConsoleState(mission_id, dispatch, snapshot, full)
    state.choose_view(VIEWS.index(initial_view))
    state.refresh(force=True)

    def loop(window: curses.window) -> int | TerminalAction | ControlViewAction:
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
            state.full = full or window.getmaxyx()[0] > 8
            if state.expand_requested:
                state.expand_requested = False
                if not state.full:
                    try:
                        from .bridge import _load_handle
                        from .terminal import TmuxTerminalBackend
                        TmuxTerminalBackend().focus(_load_handle(mission_id), pane="bridge", expand=True)
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                        state.output.append(f"Could not expand controls: {type(exc).__name__}")
            state.refresh()
            if state.terminal_action is not None:
                action, state.terminal_action = state.terminal_action, None
                return action
            if state.response_overlay:
                _draw_response_overlay(window, state)
            elif state.full:
                _draw_full(window, state)
            else:
                _draw_compact(window, state)
            try:
                key = window.get_wch()
            except curses.error:
                continue
            if key == "\x05" and not state.response_overlay:
                _edit_task_prompt(window, state)
                state.refresh(force=True)
                continue
            if key == "\x12" and not state.response_overlay:
                task = state.selected_task() if state.view == "Orders" else None
                task_id = _selected_task_id(state) if task is not None else None
                if task_id is None or task is None:
                    state.output.append("Select an order in the Orders view before amending it.")
                else:
                    _edit_task_prompt(window, state, task_id, initial_text=str(task.get("prompt") or ""))
                state.refresh(force=True)
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
            }.get(key, key)
            if isinstance(key, int) and curses.KEY_F0 < key <= curses.KEY_F0 + len(VIEWS):
                semantic = f"KEY_F{key - curses.KEY_F0}"
            if not state.handle_key(semantic):
                return 0

    while True:
        try:
            result = curses.wrapper(loop)
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
