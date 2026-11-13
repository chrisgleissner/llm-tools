"""Small curses console for LLM Bridge's tmux command pane and control view.

The UI knows no persistence or provider APIs. It asks the injected snapshot
callback for a bounded projection and sends operator text to the same command
dispatcher used by the CLI.
"""

from __future__ import annotations

import ast
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
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .bridge_input import BracketedPaste, TextBuffer, cell_width, cell_slice
from .bridge_documents import DocumentViewer
from .bridge_states import ENDED_MISSION_STATUSES, FINISHED_MISSION_STATUSES
from . import bridge_hangup, bridge_mouse, bridge_redraw, bridge_theme
from .bridge_projection import ProjectionParams
from .bridge_glyphs import glyph, localize
from . import terminal
from .terminal import TerminalAction
from .bridge_actions import Action, actions_for
from .bridge_event_text import (  # noqa: F401  (moved to bridge_event_text so the CLI can phrase events; other modules and tests reach these through bridge_tui)
    _HUMANIZERS,
    _SEP,
    _TASK_STATE_WORDS,
    _age,
    _capacity_brief,
    _compact_pairs,
    _dict,
    _event_summary,
    _humanize_event,
    _int_or_none,
    _relative_epoch,
    _short_id,
    _span,
    _text,
)
from . import bridge_draw
from .bridge_draw import (  # noqa: F401  (re-exported: other modules and tests reach these through bridge_tui)
    VIEWS,
    ControlViewAction,
    Segment,
    _put,
    _put_segments,
    _segments_text,
    _split_hint,
    initialize_colors,
    normalize_view,
    role_attr,
    sync_window_size,
)


from . import bridge_composer
from .bridge_console_rows import _ViewRows
from .bridge_composer import (  # noqa: F401  (moved out of bridge_tui; re-exported so other modules and tests reach them here)
    _WHEEL_ROWS,
    _caret_view,
    _clipboard_argv,
    _composer_status,
    _set_escape_delay,
    compose_document,
)
from .bridge_console_commands import (  # noqa: F401  (moved out of bridge_tui; re-exported so other modules and tests reach them here)
    PALETTE_COMMANDS,
    _ACTIVITY_POLL_SECONDS,
    _BRIDGE_LABEL,
    _CONSOLE_ALIASES,
    _CommandResult,
    _FILE_VIEWER_KEYS,
    _FULL_REFRESH_SECONDS,
    _IDLE_BACKOFF_SECONDS,
    _IDLE_REFRESH_SECONDS,
    _MAX_COMMAND_CHARS,
    _MAX_DETAIL_ITEMS,
    _MAX_DETAIL_TEXT,
    _MAX_OUTPUT_LINES,
    _MAX_RESPONSE_LINES,
    _OUTPUT_IDLE_THRESHOLD_SECONDS,
    _RECALL_HINT,
    _REFRESH_SECONDS,
    _SELECTABLE_VIEWS,
    _STATUS_SECONDS,
    _TASK_PAGE_SIZE,
    _VIEW_COMMANDS,
    _approval_summary,
    _approval_task,
    _command_completions,
    _command_feedback,
    _confirmation_reason,
    _cut_text,
    _evidence_text,
    _expand_console_alias,
    _is_reject_command,
    _items,
    _slash_table,
    _strip_slash,
    _task_ref,
    task_number,
)
from .bridge_console_snapshot import (  # noqa: F401  (moved out of bridge_tui; re-exported so other modules and tests reach them here)
    _bounded_event_data,
    _bounded_snapshot,
    _bounded_task_value,
    _project_capacity,
    _project_events,
    _station_projection,
)
from .bridge_console_style import (  # noqa: F401  (moved out of bridge_tui; re-exported so other modules and tests reach them here)
    _duration_seconds,
    _idle_role,
    _inline_segments,
    _percent_role,
    _plain_words,
    _status_role,
)
from .bridge_console_text import (  # noqa: F401  (moved out of bridge_tui; re-exported so other modules and tests reach them here)
    _COLLAPSED_EVENTS,
    _REFLOW_CACHE,
    _activity_entries,
    _activity_role,
    _activity_row_segments,
    _age_paths_for,
    _aged_snapshot,
    _capacity_summaries,
    _clip,
    _controller_line,
    _count_phrase,
    _idle_phrase,
    _local_clock,
    _mission,
    _mission_role_of,
    _nav_index_at,
    _nav_labels,
    _nav_layout,
    _nav_line,
    _needs_you_rows,
    _order_list_rows,
    _order_list_segments,
    _order_preview_lines,
    _order_preview_rows,
    _order_separator,
    _orders_list_height,
    _owner_label,
    _provider_state,
    _recent_event_rows,
    _recent_operation_segments,
    _reflow_markdown,
    _reflow_markdown_rows,
    _review_hold_task,
    _station_idle_phrase,
    _station_label,
    _station_status_context,
    _station_status_parts,
    _station_status_segments,
    _status_strip,
    _task_numbers,
    _waiting_on,
    _window_summary,
)
from .bridge_console_draw import (  # noqa: F401  (moved out of bridge_tui; re-exported so other modules and tests reach them here)
    _command_display,
    _draw_compact,
    _draw_document,
    _draw_full,
    _draw_navigation,
    _draw_response_overlay,
    _handle_mouse_event,
    _header,
    _header_segments,
    _hint,
    _hint_items,
    _labelled_segments,
    _mission_status_role,
    _orders_summary,
    _placeholder,
    _response_rows,
    _scroll_start,
    _status_line,
    _strip_segments,
    _value_segments,
    _working_text,
    _wrap_row,
)


def __getattr__(name: str) -> object:
    """Answer reads of the colour state, which lives in ``bridge_draw``, so ``bridge_tui._CURRENT_THEME`` still resolves."""
    if name in {"_ROLE_ATTRS", "_ROLE_PAIRS", "_CURRENT_THEME", "_COLOR_ROLES"}:
        return getattr(bridge_draw, name)
    if name == "LAST_RECOVERY_DRAFT":  # lives in ``bridge_composer``, which assigns it
        return bridge_composer.LAST_RECOVERY_DRAFT
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


Snapshot = Callable[..., dict[str, object]]
Dispatch = Callable[[str, str], object]
# Set by the home screen when it opens the controls of a mission it just saved,
# so the console can say once which id to type on the command line.
JUST_SAVED_MISSION: str | None = None
















































































































































# --- tmux policy lives in ``terminal``; these thin names keep the console's call sites and the
# tests that replace them (``_execute_terminal_action``, ``_read_provider_activity``) unchanged.
_ACTIVITY_SEEN = terminal.ACTIVITY_SEEN
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

























































































































@dataclass
class _ConsoleState(_ViewRows):
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
    agent_inspector: bool = False
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
    usage_all: bool = False
    saved_notice: bool = False  # show "saved as ID" in the status strip until the first key
    outside_station: bool = False  # opened by `llm-bridge control` in the user's own terminal, so Ctrl-] keys do not apply
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
    document_kind: str = "prompt"  # what the pending read returns: prompt, output or diff
    _confirm_overlay_hidden: bool = field(default=False, repr=False)  # the approval dialog sits behind a pager
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
            self.document_task_id = None if task_id == "objective" or self.document_kind != "prompt" else task_id
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
                if self.document_kind != "prompt":
                    title = f"{self.mission_id[:12]} · task {task_id} · {self.document_kind}"
                    if self.confirm_command is not None and self.response_overlay:
                        self.response_overlay = False  # the pager draws below the response overlay
                        self._confirm_overlay_hidden = True
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
                    if bridge_composer.LAST_RECOVERY_DRAFT in cleanup_paths:
                        bridge_composer.LAST_RECOVERY_DRAFT = None
                self.pending_cleanup_path = None
                self.pending_cleanup_paths = ()
            recall_hint = ""
            if failed and self.submitted_draft:
                # Like Codex CLI and Claude Code, the command line stays empty after a
                # failure; the failed command is the newest history entry (Up / Ctrl-P).
                self.rejected_submission = self.submitted_draft
                recall_hint = _RECALL_HINT
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

    def _complete_command(self) -> None:
        """Tab: a single match completes; several complete their common prefix, and Tab again lists them."""
        candidates = _command_completions(self.command)
        if not candidates:
            return
        common = candidates[0] if len(candidates) == 1 else os.path.commonprefix(candidates)
        if len(common) > len(self.command) or (len(candidates) == 1 and common != self.command):
            self.command = common
            self.command_cursor = len(common)
            return
        if len(candidates) > 1:
            shown = ", ".join(candidates[:12])
            line = f"Matches: {shown}" + (f" and {len(candidates) - 12} more" if len(candidates) > 12 else "")
            if not self.output or self.output[-1] != line:
                self.output.append(line)

    def open_help(self, topic: str) -> None:
        """Show a help topic, or with `view:NAME` the short help of one view, in the document viewer."""
        from .bridge_help import topic_text, view_help

        by_view = topic.startswith("view:")
        text = view_help(topic[len("view:"):]) if by_view else topic_text(topic)
        self.close_document()
        self.document_task_id = None
        self.document_notice = ""
        self.document_viewer = DocumentViewer(f"Bridge help · {topic[len('view:'):] if by_view else topic}", text, rendered=True, plain=True)

    def _confirm_or_dispatch(self, command: str) -> bool:
        typed = command
        command, problem = _expand_console_alias(command, self.snapshot)
        if problem is not None:
            # Like a command that fails after dispatch: the line is cleared (the caller does that when
            # this returns True) and the refused command is the newest history entry for Up / Ctrl-P.
            self.history.append(typed.strip())
            self.history_index = None
            self.output.append(f"{problem}{_SEP}{_RECALL_HINT}")
            return True
        reason = _confirmation_reason(command)
        if reason:
            self.confirm_command = command
            self.output.clear()
            approving = _approval_task(command)
            lines = [f"{reason} · mission {_short_id(self.mission_id)}"]
            if approving is not None:
                lines += _approval_summary(self.snapshot, approving)
            if approving is not None:
                footer = "Y approve · N or Esc cancel · o output · d diff"
            elif _is_reject_command(command):
                footer = "Y Reject and retry (uses capacity) · H Reject and hold · N or Esc cancel"
            elif reason.startswith("Cancel this mission"):
                footer = "Y Cancel mission · N or Esc Keep mission"  # "cancel" must not be both the question and the way out
            else:
                footer = "Y confirm · N or Esc cancel"
            self.output.extend([*lines, footer])
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
            if viewer.plain and key in _FILE_VIEWER_KEYS:
                return True  # a help page has no mode, wrap, export, copy, edit or heading controls
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
                elif isinstance(key, str) and key.lower() == "h" and _is_reject_command(self.confirm_command):
                    # Reject and hold: the same rejection, with the task paused in the same step (CLIUX-025).
                    command, self.confirm_command = self.confirm_command + " --hold", None
                    self.response_overlay = False
                    self._dispatch(command)
                elif isinstance(key, str) and key.lower() in {"o", "d"} and (ref := self._approval_task_ref()) is not None:
                    self.request_task_evidence(ref[-1], "output" if key.lower() == "o" else "diff", ref=ref)
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
            self._complete_command()
            return True
        if key == "KEY_BTAB" and not self.input_focused:
            self.choose_view((self.view_index - 1) % len(VIEWS))
            return True
        if key in {"KEY_UP", "KEY_DOWN"} and self.input_focused and not self.search_editing:
            self._history(-1 if key == "KEY_UP" else 1)  # Up/Down walk the history while the command line has focus
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
        if key == "a" and self.view == "Usage" and not self.input_focused and not self.command and "all_text" in _dict(self.snapshot.get("usage")):
            self.usage_all = not self.usage_all
            return True
        if key == "v" and self.view == "Tasks" and not self.input_focused:
            task = self.selected_task()
            if task is not None:
                self.request_task_document(str(task_number(task) or ""))
            return True
        if key in {"o", "d"} and self.view == "Tasks" and not self.input_focused:
            task = self.selected_task()
            if task is not None and task_number(task) is not None:
                self.request_task_evidence(str(task_number(task)), "output" if key == "o" else "diff")
            return True
        if key == "o" and self.view == "Overview" and not self.input_focused:
            if not self.document_pending:
                self._invalidate_document_read()
                generation = self._document_generation
                self.document_pending = True
                self.document_kind = "prompt"
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
            if self.view == "Agents" and self.agent_inspector:
                self.agent_inspector = False
                return True
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
            elif self.view == "Agents" and not self.input_focused:
                from .bridge_agents import ordered_agents

                ordered = ordered_agents(_dict(self.snapshot.get("agent_map")))
                if ordered:
                    agent, _depth = ordered[min(max(0, self.selected), len(ordered) - 1)]
                    navigable = _dict(agent.get("capabilities")).get("navigate") is True
                    if navigable and agent.get("kind") == "bridge-root" and agent.get("mission_id") != self.mission_id:
                        self._dispatch("attach --mission " + shlex.quote(str(agent.get("mission_id"))))
                    elif navigable and agent.get("kind") == "bridge-root" and agent.get("station"):
                        self._dispatch("station " + shlex.quote(str(agent.get("station"))))
                    else:
                        self.agent_inspector = True
                        self.detail_scroll = 0
            else:
                command = self.command
                if self._confirm_or_dispatch(command):
                    self.command = ""
                    self.command_cursor = 0
                    self.input_focused = False
            return True
        if key in {"i", "I"} and self.view == "Agents" and not self.input_focused:
            self.agent_inspector = not self.agent_inspector
            self.detail_scroll = 0
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
            has_terminal=str(_dict(self.snapshot.get("station_mode")).get("configured_mode") or "").lower() != "headless",
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
        if self._confirm_overlay_hidden:
            self._confirm_overlay_hidden = False
            self.response_overlay = self.confirm_command is not None  # back to the approval dialog

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
        self.document_kind = "prompt"
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

    def _approval_task_ref(self) -> list[str] | None:
        """CLI arguments naming the task the open approval dialog names, taken from the approval command itself.

        The loaded task list is not consulted: it can be a different page, or be replaced by a
        refresh, while the dialog is open, and `task output` / `task diff` resolve the task
        themselves, exactly as the approval will.
        """
        target = _approval_task(self.confirm_command or "")
        if target is None:
            return None
        kind, value = target
        return ["--task-id" if kind == "id" else "--task", value]

    def request_task_evidence(self, number: str, kind: str, *, ref: list[str] | None = None) -> None:
        """Read `task output` or `task diff` of task ``number`` off the input thread and open it in the pager.

        ``ref`` overrides the CLI arguments that name the task; without it they come from the loaded list.
        """
        if self.document_pending:
            self.output.append("A full task read is already in progress.")
            return
        self._invalidate_document_read()
        generation = self._document_generation
        self.document_pending = True
        self.document_kind = kind

        def read_evidence() -> None:
            try:
                text = str(self.dispatch(self.mission_id, shlex.join(["task", kind, *(ref if ref is not None else _task_ref(self.snapshot, number))])))
                result = (number, "", None, text) if text.startswith("error:") else (number, "", text or f"(task {number} has no {kind})\n", "")
            except Exception as exc:
                result = (number, "", None, f"Could not read task {kind}: {_text(exc, 160)}")  # type: ignore[assignment]  # None stands for a missing value in this row
            with self._result_lock:
                if generation == self._document_generation:
                    self._document_result = result

        threading.Thread(target=read_evidence, name="llm-bridge-task-evidence", daemon=True).start()

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
        if self.view == "Agents":
            from .bridge_agents import ordered_agents

            return list(range(len(ordered_agents(_dict(self.snapshot.get("agent_map"))))))
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

    def action_note(self, avail: int) -> str:
        """The selected action's full description, when its row is cut at ``avail`` columns; else ``""``."""
        actions = self.palette_actions()
        if not actions:
            return ""
        index = min(max(0, self.selected), len(actions) - 1)
        row = self._palette_rows()[index]
        if cell_width(row) <= avail:
            return ""
        action = actions[index]
        unavailable = self.action_unavailable(action)
        return f"{action.effect}{_SEP}unavailable: {unavailable}" if unavailable else action.effect

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
            note_rows = 1 if self.action_note(max(1, width - 2)) else 0
            start = _scroll_start(self.selected, max(1, count), max(1, height - 6 - note_rows))
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


TASK_DRAFT_PREFIX = "composer-recovery-task-"
TASK_PLACEHOLDER = 'First line = title. Describe the change. Optional: "Acceptance:" followed by "- criterion" lines.'
_RESTORE_MAX_AGE_SECONDS = 24 * 3600


def retained_composer_draft(now: float | None = None) -> tuple[Path, str] | None:
    """The newest non-empty composer draft kept in the last 24 hours, with its text.

    Esc in the composer keeps the draft in the private drafts directory;
    a saved or discarded draft is deleted, so what remains is what was kept.
    """
    from .bridge_store import default_state_dir

    draft_dir = default_state_dir() / "drafts"
    current = time.time() if now is None else now
    newest: tuple[float, Path] | None = None
    try:
        names = list(draft_dir.glob(TASK_DRAFT_PREFIX + "*.md"))
    except OSError:
        return None
    for path in names:
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size == 0 or info.st_size > 1_048_576:
            continue
        if current - info.st_mtime > _RESTORE_MAX_AGE_SECONDS:
            continue
        if newest is None or info.st_mtime > newest[0]:
            newest = (info.st_mtime, path)
    if newest is None:
        return None
    try:
        text = newest[1].read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return (newest[1], text) if text.strip() else None


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
    restored: Path | None = None
    if task_id is None and not initial_text:
        kept = retained_composer_draft()
        if kept is not None:
            restored, initial_text = kept
    options: dict[str, object] = {}
    if task_id is None:
        options["placeholder"] = TASK_PLACEHOLDER
        options["draft_prefix"] = TASK_DRAFT_PREFIX
    if restored is not None:
        options["restored_from"] = restored
    try:
        source = compose_document(
            initial_text,
            title=f"Amend task {task_id} prompt" if task_id else "Add task prompt",
            window=window,
            **options,  # type: ignore[arg-type]  # only the keyword names above are ever set
        )
    finally:
        window.clear()
        window.refresh()
    if source is None:
        if bridge_composer.LAST_RECOVERY_DRAFT is not None and bridge_composer.LAST_RECOVERY_DRAFT.is_file() and bridge_composer.LAST_RECOVERY_DRAFT.stat().st_size:
            state.output.append("Draft kept · Ctrl-E reopens it" if task_id is None else "Task unchanged; your edit is kept in the private drafts folder")
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
        state.output.append(f"Could not save task draft: {_text(exc, 100)}. Recovery source: {bridge_composer.LAST_RECOVERY_DRAFT}")
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
    checkpoint = bridge_composer.LAST_RECOVERY_DRAFT
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


def _run(mission_id: str, dispatch: Dispatch, snapshot: Snapshot, *, full: bool, initial_view: str = "Overview", outside_station: bool = False) -> int:
    # Ignore stale exported dimensions from a detached tmux station.
    curses.use_env(False)
    state = _ConsoleState(mission_id, dispatch, snapshot, full)
    state.outside_station = outside_station
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
        return _run(mission_id, dispatch, snapshot, full=True, initial_view=initial_view, outside_station=True)
    except bridge_hangup.TerminalGone:
        return bridge_hangup.abandon_terminal()
