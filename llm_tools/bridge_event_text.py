"""Operator-facing wording for durable Bridge events, shared by the console and the CLI.

The Activity view and `llm-bridge events` phrase an event through the same humaniser table
(`_HUMANIZERS`), so one event reads the same on both surfaces. The module imports no curses, so a
short CLI command can use it. The console re-exports these names from `bridge_tui`.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from . import bridge_events
from .bridge_format import exit_activity_text, plain_reason
from .bridge_glyphs import glyph
from .bridge_states import MISSION_COUNT, MISSION_HUE_NAMES
from .bridge_vocab import EventKind, status_label


def _scrub(text: str, *, keep_newline: bool = False, drop_format: bool = True) -> str:
    """Replace control and surrogate characters with spaces and (optionally) drop format characters.

    Only the distinct characters of `text` are classified, so the cost does not
    grow with the length of repetitive text.
    """
    if text.isascii() and text.isprintable():
        return text  # printable ASCII has no control or format characters
    table: dict[int, str | None] = {}
    for char in set(text):
        if char.isascii() and char.isprintable():
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cs"}:
            if not (keep_newline and char == "\n"):
                table[ord(char)] = " "
        elif drop_format and category == "Cf":
            table[ord(char)] = None  # bidi overrides, zero-width marks
    return text.translate(table) if table else text


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


# What each stored restoration state means to the operator: can the provider's own session be resumed?
RESTORATION_WORDS = {
    "NATIVE_CREATED": "resumable", "RESTORED_NATIVE": "resumed", "RECONSTRUCTED": "rebuilt",
    "UNAVAILABLE": "cannot resume", "RESTORING": "restoring",
}


def restoration_word(value: object) -> str:
    """Plain words for a stored restoration state; an unknown state is shown as stored, nothing as `-`."""
    text = _text(value, 30) if _known(value) else ""
    return RESTORATION_WORDS.get(text.upper(), text) if text else "-"


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


_SEP = " · "


_NO_VALUE = {"", "none", "unknown", "null", "-", "—"}


def _known(value: object) -> bool:
    """True for a value worth showing: not None/empty/unknown placeholder."""
    return value is not None and str(value).strip().lower() not in _NO_VALUE


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
        from . import bridge_redraw

        bridge_redraw.note_age(max(0, value))
    return f"{max(0, value)}s" if value < 60 else _span(value)


def _relative_epoch(value: object, now: float | None = None) -> str | None:
    """`in 4h 59m` / `3m ago` for an epoch or ISO timestamp, else None."""
    from . import bridge_redraw, common

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


def _short_id(value: object, length: int = 8) -> str:
    return _text(value, 64)[:length] if _known(value) else "-"


def _percent_left(value: object) -> str | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return f"{int(number + 0.5)}%"


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
        return _SEP.join(part for part in (state, task, f"task now {status_label(_text(data['task_status'], 20))}" if _known(data.get("task_status")) else "") if part)
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
    if kind == "provider-orphaned":
        return _SEP.join(part for part in (f"provider process {_text(data.get('process_pid', '?'), 12)} could not be stopped", _text(data.get("reason") or "", 100)) if part)
    if kind == "provider-handoff-exhausted" and "autonomy_aborts" in data:
        return f"{task or 'task'} stopped by Bridge {_text(data['autonomy_aborts'], 6)} times · waiting for operator"
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


_TASK_STATE_WORDS = {
    "READY": "ready", "QUEUED": "waiting", "RUNNING": "running", "VERIFYING": "awaiting review",
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


@_humanizer(EventKind.MISSION_WAITING_FOR_OPERATOR)
def _h_mission_waiting_for_operator(ctx: _EventContext) -> str:
    return "Waiting for you · " + _text(ctx.data.get("summary") or ctx.data.get("reason") or "no task can run", 140)


@_humanizer(EventKind.MISSION_COLOUR)
def _h_mission_colour(ctx: _EventContext) -> str:
    value = ctx.data.get("value")
    if isinstance(value, int) and 1 <= value <= MISSION_COUNT:
        return f"Mission colour set to {value} ({MISSION_HUE_NAMES[value - 1]})"
    return "Mission colour back to automatic"


@_humanizer(EventKind.MISSION_CREATED)
def _h_mission_created(ctx: _EventContext) -> str:
    return f"Mission created · {_text(ctx.data.get('name') or '', 60)}".rstrip(" ·")


@_humanizer(EventKind.MISSION_STATUS)
def _h_mission_status(ctx: _EventContext) -> str:
    if ctx.new_state == "RUNNING":
        return "Resumed" if ctx.old_state == "PAUSED" else "Mission started"
    return f"{_MISSION_STATE_WORDS.get(ctx.new_state, 'Mission ' + (ctx.new_state.lower() or 'changed'))}{ctx.reason}"


@_humanizer(EventKind.MISSION_SUSPEND_WHEN_RATE_LIMITED)
def _h_mission_suspend_when_rate_limited(ctx: _EventContext) -> str:
    return "Capacity-wait suspend " + ("on" if ctx.data.get("value") is True else "off")


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


@_humanizer(EventKind.TASK_STATUS_TRANSITION)
def _h_task_status_transition(ctx: _EventContext) -> str:
    return f"{ctx.task} {status_label(ctx.old_state).lower()} → {status_label(ctx.new_state).lower()}" + ctx.reason


@_humanizer(EventKind.OPERATION_RUNNING)
def _h_operation_running(ctx: _EventContext) -> str:
    return f"Started {ctx.owner or 'provider'}" + (f" ({ctx.model})" if ctx.model else "") + ctx.on_task


@_humanizer(EventKind.OPERATION_EXITED)
def _h_operation_exited(ctx: _EventContext) -> str:
    code = ctx.data.get("exit_code", "?")
    synthetic = exit_activity_text(code)  # 75, 76, 130, 143: Bridge's own statuses, shown as words (CLIUX-064)
    if synthetic:
        return f"{synthetic}{ctx.on_task}"
    return f"Provider exited ({_text(code, 8)}){ctx.on_task}"


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


@_humanizer(EventKind.PROVIDER_RESULT_DIAGNOSED)
def _h_provider_result_diagnosed(ctx: _EventContext) -> str:
    failure = ctx.data.get("failure")
    cause = failure.get("cause") if isinstance(failure, dict) else None
    if isinstance(failure, dict) and failure.get("class") == "success" and cause == "completed":
        # The provider process exited 0.  That is not the task's result: say whether the task now awaits the operator.
        waiting = str(ctx.data.get("task_status") or "").upper() == "VERIFYING"
        return "Provider result recorded · exited 0" + (" · awaiting review" if waiting else "")
    return "Provider result recorded" + (f" · {_text(cause, 60)}" if _known(cause) else "")


@_humanizer(EventKind.CAPACITY_REFRESH)
def _h_capacity_refresh(ctx: _EventContext) -> str:
    brief = _text(ctx.data.get("capacity_brief") or "", 80)
    return f"Capacity: {ctx.owner or 'providers'} {brief}".rstrip() if brief else f"Capacity refreshed: {ctx.owner or 'providers'}"


@_humanizer(EventKind.CAPACITY_REFRESH_ERROR)
def _h_capacity_refresh_error(ctx: _EventContext) -> str:
    return "Capacity refresh failed" + (f" ({_text(ctx.data['reason'], 60)})" if _known(ctx.data.get("reason")) else "")


@_humanizer(EventKind.SUSPEND_DECISION, EventKind.SUSPEND_INTENT, EventKind.SUSPEND_REFUSED, EventKind.SUSPEND_OUTCOME,
            EventKind.SUSPEND_PROVIDER_STARTED,
            EventKind.SUSPEND_WORK_RESUMED, EventKind.CAPACITY_WAKE_ARMED,
            EventKind.CAPACITY_WAKE_UNAVAILABLE, EventKind.CAPACITY_WAKE_RESUMED,
            EventKind.CAPACITY_WAKE_RESUME_UNVERIFIED, EventKind.CAPACITY_WAKE_PLAN)
def _h_suspend_capacity(ctx: _EventContext) -> str:
    labels = {
        "suspend-decision": "Suspend path",
        "suspend-intent": "Capacity suspend requested",
        "suspend-refused": "Capacity suspend refused",
        "suspend-outcome": "Capacity suspend finished",
        "suspend-work-resumed": "Capacity-wait work resumed",
        "suspend-provider-started": "Provider resumed after verified sleep",
        "capacity-wake-armed": "Capacity wake armed",
        "capacity-wake-unavailable": "Capacity wake unavailable",
        "capacity-wake-resumed": "Capacity wake observed",
        "capacity-wake-resume-unverified": "Capacity wake could not be verified",
        "capacity-wake-plan": "Shared wake plan updated",
    }
    return _with_reason(labels.get(ctx.kind, "Capacity suspend updated"), ctx.data)


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
    text = f"Command {ctx.kind[len('command-'):]}: {_text(ctx.data.get('command') or '', 60)}".rstrip(": ")
    result = ctx.data.get("result")
    error = result.get("error") if isinstance(result, dict) else None
    return f"{text} · {_text(error, 160)}" if _known(error) and ctx.kind != "command-applied" else text


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


@_humanizer(EventKind.PROVIDER_ORPHANED)
def _h_provider_orphaned(ctx: _EventContext) -> str:
    return f"Provider process {_text(ctx.data.get('process_pid', '?'), 12)} could not be stopped" + (f" · {_text(ctx.data['reason'], 100)}" if _known(ctx.data.get("reason")) else "")


@_humanizer(EventKind.PROVIDER_HANDOFF_EXHAUSTED)
def _h_provider_handoff_exhausted(ctx: _EventContext) -> str:
    if "autonomy_aborts" in ctx.data:
        return f"{ctx.task} stopped by Bridge again ({_text(ctx.data['autonomy_aborts'], 6)} times), waiting for you"
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
    state = restoration_word(ctx.data.get("restoration_state"))
    return f"Session {state if _known(ctx.data.get('restoration_state')) else 'unknown'}" + (f" · {_text(ctx.data['provider'], 20)}" if _known(ctx.data.get("provider")) else "")


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
