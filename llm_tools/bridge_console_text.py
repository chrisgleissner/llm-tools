"""Text and segment builders for the console views."""

from __future__ import annotations

import math
import re
import textwrap
import time
from dataclasses import dataclass
from .bridge_input import cell_width, cell_slice
from .bridge_documents import heading_label
from .bridge_states import ACTIVE_OPERATION_STATUSES, ENDED_MISSION_STATUSES, STARTED_MISSION_STATUSES
from . import bridge_events, bridge_redraw, bridge_theme
from .bridge_vocab import MISSION_NEEDS_YOU_LABEL, status_label
from . import bridge_format
from .bridge_glyphs import glyph, state_text
from .common import fmt_duration
from .bridge_wording import approval_row, controller_hint, proposals_row
from .bridge_event_text import _NO_VALUE, _SEP, _age, _decision_owner, _dict, _event_kind, _humanize_event, _int_or_none, _known, _percent_left, _relative_epoch, _span, _text, _window_brief
from .bridge_draw import QUIET, VIEWS, Segment, _clip_segments, _merge_segments, _segments_text, _segments_width
from .bridge_console_commands import _BRIDGE_LABEL, _OUTPUT_IDLE_THRESHOLD_SECONDS, _cut_text, _evidence_text, _items, task_number
from .bridge_console_style import _FINISHED_STATES, _duration_seconds, _idle_role, _inline_segments, _provider_role, _state_look, _state_segment, _state_text


def _mission(snapshot: dict[str, object]) -> dict[str, object]:
    nested = _dict(snapshot.get("mission"))
    return nested or snapshot


def _review_pending(snapshot: dict[str, object]) -> bool:
    """True when a task of this mission awaits the operator (stored status VERIFYING), whatever page of tasks is loaded."""
    counts = _dict(snapshot.get("task_counts"))
    if any(str(name).upper() == "VERIFYING" and isinstance(count, int) and count > 0 for name, count in counts.items()):
        return True
    shown = [*_items(snapshot.get("tasks")), _dict(snapshot.get("current_task"))]
    return any(str(task.get("status") or "").upper() == "VERIFYING" for task in shown)


def _mission_state_word(snapshot: dict[str, object], status: str) -> str:
    """The mission's state word: ``NEEDS YOU`` replaces RUNNING while a task awaits review."""
    return MISSION_NEEDS_YOU_LABEL if status.upper() == "RUNNING" and _review_pending(snapshot) else status


def _mission_role_of(snapshot: dict[str, object], mission_id: str = "") -> str:
    """`mission_N` identity role of the mission in a snapshot (pinned colour, else a stable hash)."""
    mission = _mission(snapshot)
    explicit = mission.get("role")
    if isinstance(explicit, str) and explicit in bridge_theme.MISSION_ROLES:
        return explicit
    return bridge_theme.mission_role(mission.get("id") or mission_id, _dict(mission.get("config")))


_ESTIMATE_MISSING = _NO_VALUE | {"no rate data"}


def _clip(text: str, width: int) -> str:
    """Clip to terminal cells, marking the cut with an ellipsis."""
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    mark = glyph("ellipsis")
    return cell_slice(text, 0, max(0, width - cell_width(mark))) + mark


def _clock_epoch() -> float:
    """Wall-clock epoch for text that is re-derived from the current time on every frame.

    The caller cannot say how soon that text changes, so the frame is repainted each second.
    """
    from . import common

    bridge_redraw.note_wall(1.0)
    return common.now_epoch()


def _local_clock(value: object) -> str:
    """Local time of an ISO/epoch timestamp: `HH:MM:SS` today, `Mon 03:13` within the last week, else `2026-11-06 03:13`."""
    from . import common

    epoch = common.parse_epoch(value, use_date=False)
    if epoch is None:
        return "--:--:--"
    try:
        moment = time.localtime(epoch)
        now = common.now_epoch()
        if time.localtime(now)[:3] == moment[:3]:
            return time.strftime("%H:%M:%S", moment)
        if 0 <= now - epoch < 7 * 86400:
            return time.strftime("%a %H:%M", moment)
        return time.strftime("%Y-%m-%d %H:%M", moment)
    except (OverflowError, OSError, ValueError):  # a timestamp outside the platform's range
        return "--:--:--"


def _percent_value(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


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


# Percent points two capacity readings of the same window may differ before they are reported as a disagreement.
_USAGE_AGREEMENT_TOLERANCE = 5.0


def _usage_disagreements(snapshot: dict[str, object]) -> list[str]:
    """One line per provider whose Usage rows contradict the controller's last capacity observation.

    The dashboard is a live llm-usage read; Overview, Providers and `status` show what the
    controller last observed. When they differ for a window, say so and give the controller's
    figures and their age instead of leaving two screens that answer differently.
    """
    usage = _dict(snapshot.get("usage"))
    if not isinstance(usage.get("rows"), list):
        return []
    capacity = _dict(snapshot.get("capacity"))
    age = _int_or_none(capacity.get("source_age_seconds"))
    now = _clock_epoch()
    lines: list[str] = []
    reported: set[str] = set()
    for decision in _items(_dict(capacity.get("selection")).get("decisions")):
        provider = str(decision.get("capacity_provider") or decision.get("provider") or "").strip().lower()
        windows = [window for window in _items(decision.get("windows")) if _percent_value(window.get("remaining_percent")) is not None]
        rows = _usage_rows_for(snapshot, provider)
        if not provider or provider in reported or not windows or not rows:
            continue

        def agrees(window: dict[str, object]) -> bool:
            """True when the dashboard has no row for this window or a row within the tolerance."""
            wanted = _percent_value(window.get("remaining_percent"))
            same = [row for row in rows if row.get("scope") == window.get("name")]
            return not same or any(
                (seen := _percent_value(row.get("remaining_percent"))) is not None and wanted is not None
                and abs(seen - wanted) <= _USAGE_AGREEMENT_TOLERANCE
                for row in same
            )

        if all(agrees(window) for window in windows):
            continue
        reported.add(provider)
        observed = _SEP.join(_window_summary(window, now).split(", resets")[0] for window in windows)
        lines.append(f"{provider} differs from the controller's last observation" + (f" ({_age(age)} ago)" if age is not None else "") + f": {observed}")
    return lines


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
    if status == "STOPPING":
        return "stopping after the current step"
    hint = controller_hint(status, "console")
    if hint:
        return hint
    if status in STARTED_MISSION_STATUSES and lifecycle.get("controller_running") is False:
        return "not running — type /resume"
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

        rows.append(waiting_message(waiting, _clock_epoch(), console=True))
    for task in tasks:
        if str(task.get("status", "")).upper() == "VERIFYING":
            number = _text(task_number(task) if task_number(task) is not None else "?", 12)
            if number == held:
                rows.append(_review_hold_text(number))
                continue
            rows.append(approval_row("console", [number]))
    budget = _dict(data.get("budget"))
    if budget.get("hold_reason"):
        from .bridge_budget import summary

        limits, used = _dict(budget.get("limits")), _dict(budget.get("used"))
        if budget["hold_reason"] == "budget-exhausted":
            rows.append(f"held: budget exhausted ({_text(summary(limits, used), 160)}){_SEP}raise a limit with: llm-bridge mission set --budget-runs N")
        else:
            rows.append(f"held: {_text(budget['hold_reason'], 40)} · {_text(budget.get('hold_detail') or '', 160)}{_SEP}fix it, then /resume")
    pending = _dict(data.get("proposal_counts")).get("PENDING")
    if isinstance(pending, (int, float)) and pending:
        rows.append(proposals_row("console", int(pending)))
    provider_wait = _dict(data.get("provider_wait"))
    question_count = provider_wait.get("question_count")
    if isinstance(question_count, (int, float)) and 1 <= question_count <= 32:
        provider = _text(provider_wait.get("provider") or "Provider", 40)
        mission_id = _text(_dict(data.get("mission")).get("id") or "?", 64)
        attach = f"llm-bridge attach --mission {mission_id}"
        questions = provider_wait.get("questions")
        first = questions[0] if isinstance(questions, list) and questions and isinstance(questions[0], dict) else None
        question = first.get("question") if first else None
        options = first.get("options") if first else None
        if isinstance(question, str) and isinstance(options, list) and options and all(isinstance(option, str) for option in options):
            choices = "; ".join(f"{index}. {_text(option, 200)}" for index, option in enumerate(options[:8], 1))
            rows.append(f"{provider} asks: {_text(question, 800)} — choices: {choices} — take control of the terminal to answer: {attach}")
        else:
            noun = "question" if question_count == 1 else "questions"
            rows.append(f"{provider} has {int(question_count)} queued {noun} — take control of the terminal and press Shift+Left to answer: {attach}")
    from .bridge_format import declined_message, declined_provider

    stuck = []
    for task in tasks:
        if str(task.get("status", "")).upper() not in {"BLOCKED", "FAILED"}:
            continue
        declined = declined_provider(data, task) if str(task.get("status", "")).upper() == "BLOCKED" else ""
        if declined:
            rows.append(declined_message(declined))
            continue
        asked = _dict(data.get("awaiting_answer"))
        if asked.get("question") and asked.get("task_id") == task.get("id") and str(task.get("status", "")).upper() == "BLOCKED":
            number = _text(task_number(task) if task_number(task) is not None else "?", 12)
            rows.append(f'task {number} asked: "{_text(asked["question"], 160)}"{_SEP}/amend {number} FILE then /retry {number}{_SEP}/retry {number}{_SEP}/cancel {number}')
            continue
        stuck.append(f"task {_text(task_number(task) if task_number(task) is not None else '?', 12)} {str(task.get('status')).upper()}")
    if stuck:
        rows.append(_SEP.join(stuck[:3]) + (f" (+{len(stuck) - 3} more)" if len(stuck) > 3 else "") + " — inspect in Tasks")
    return rows[:5]


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


def _station_status_is_implied(mission_status: str, task: dict[str, object]) -> bool:
    """True when the task text already says what the mission state would add.

    An ordinary RUNNING mission with a current task needs no mission word, and neither does
    ``NEEDS YOU`` when the current task is the one that awaits review (its own ``REVIEW`` says so).
    """
    if mission_status == "UNKNOWN":
        return True
    if not task:
        return False
    return mission_status == "RUNNING" or (mission_status == MISSION_NEEDS_YOU_LABEL and str(task.get("status") or "").upper() == "VERIFYING")


def _strip_task_status(snapshot: dict[str, object], task: dict[str, object]) -> str:
    """The task status the tmux strip shows.

    The provider's operation row turns EXITED before the controller moves the task on, so a
    RUNNING task whose newest operation exited with status 0 is shown as VERIFYING, the state
    the controller records next, in step with the "Provider exited" message in the pane.
    """
    status = str(task.get("status") or "unknown")
    if status.upper() != "RUNNING" or _known(_dict(snapshot.get("station_context")).get("executing_operation")):
        return status
    operations = _items(snapshot.get("operations"))
    last = operations[-1] if operations else {}
    same_task = str(last.get("task_id")) == str(task.get("id")) if last.get("task_id") is not None and task.get("id") is not None else True
    if same_task and str(last.get("status") or "").upper() == "EXITED" and str(last.get("exit_code")) == "0":
        return "VERIFYING"
    return status


def _station_status_parts(snapshot: dict[str, object]) -> tuple[str, str]:
    """`(mission label, task/state text)` for the tmux status line.

    The task comes first in the state text; the mission state is appended unless
    a task is running in an ordinary RUNNING mission. The label (repository
    folder or mission name, at most 14 cells) says which mission this station is.
    """
    mission_status = _mission_state_word(snapshot, _text(_mission(snapshot).get("status") or "unknown", 16).upper())
    task = _dict(snapshot.get("current_task"))
    if task:
        task_status = _strip_task_status(snapshot, task)
        idle = _station_idle_phrase(snapshot) if task_status.upper() == "RUNNING" else ""
        label = f"Task {_text(task_number(task) or '?', 8)} " + (idle or status_label(_text(task_status, 12).upper()))
    else:
        label = "No task"
    if not _station_status_is_implied(mission_status, task):
        label = f"{label}{_SEP}{mission_status}"
    from .bridge_switcher import mission_label

    mission = _mission(snapshot)
    return mission_label(mission.get("name"), mission.get("cwd")), label


def _station_status_segments(snapshot: dict[str, object]) -> list[Segment]:
    """The state text of the tmux status line with the state words in their theme colours.

    Same content as `_station_status_parts` plus the state glyph: `Task 6 ● RUNNING`.
    A silent provider shows its idle phrase (`muted`, `warn` after ten minutes) instead.
    """
    mission_status = _mission_state_word(snapshot, _text(_mission(snapshot).get("status") or "unknown", 16).upper())
    task = _dict(snapshot.get("current_task"))
    out: list[Segment] = []
    if task:
        status = _strip_task_status(snapshot, task)
        idle = _station_idle_phrase(snapshot) if status.upper() == "RUNNING" else ""
        out.append((f"Task {_text(task_number(task) or '?', 8)} ", "info"))
        out.append((idle, _idle_role(idle)) if idle else _state_segment(_text(status, 12)))
    else:
        out.append(("No task", "muted"))
    if not _station_status_is_implied(mission_status, task):
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


# Derived from the event registry (`bridge_events.EVENTS`), which holds the classification of every kind.
_COLLAPSED_EVENTS = bridge_events.collapsed_kinds()


_PLUMBING_EVENTS = bridge_events.plumbing_kinds()


def _task_numbers(snapshot: dict[str, object]) -> dict[str, str]:
    """Task id to displayed number for the tasks on this page (differs once tasks carry an ordinal)."""
    return {str(task["id"]): str(task_number(task)) for task in _items(snapshot.get("tasks")) if task.get("id") is not None}


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
    """The first task waiting to start. A READY current task counts: while nothing runs it is the next task."""
    return next(
        (task for task in _items(snapshot.get("tasks")) if str(task.get("status", "")).upper() in {"READY", "QUEUED"}),
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
        return _fit_strip([f"task {number} awaits your review{more}", f"type /approve {number}"], width), "warn"
    following = _next_task(snapshot)
    upcoming = f"task {_text(task_number(following) if task_number(following) is not None else '?', 12)} ready" if following else "no task waiting · Ctrl-E adds one"
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


def _remove_segment_range(segments: list[Segment], begin: int, end: int | None) -> list[Segment]:
    """Remove a header-indexed text range from styled usage columns."""
    output: list[Segment] = []
    offset = 0
    for text, role in segments:
        stop = offset + len(text)
        left = text[:max(0, min(len(text), begin - offset))] if offset < begin else ""
        right = text[max(0, min(len(text), (end or stop) - offset)):] if end is not None and stop > end else ""
        if end is None and stop > begin:
            right = ""
        if left:
            output.append((left, role))
        if end is not None and right:
            output.append((right, role))
        offset = stop
    return _merge_segments(output)


def _fit_usage_segments(table: list[list[Segment]], width: int) -> list[list[Segment]]:
    """Apply the usage table's existing optional-column fit to styled rows."""
    lines = [_segments_text(line) for line in table]

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
            table = [_remove_segment_range(line, begin, end) for line in table]
            lines = [_segments_text(line) for line in table]
            if widest() <= width:
                break
        if len(lines) > 1 and lines[1].strip() and set(lines[1].strip()) <= {"─", "-", " "}:
            table[1] = _remove_segment_range(table[1], len(lines[0].rstrip()), None)
    fitted: list[list[Segment]] = []
    for line in table:
        remaining = len(_segments_text(line).rstrip())
        parts: list[Segment] = []
        for text, role in line:
            piece = text[:remaining]
            remaining -= len(piece)
            if piece:
                parts.append((piece, role))
            if remaining <= 0:
                break
        fitted.append(parts)
    return fitted


def _slice_segments(segments: list[Segment], start: int, width: int) -> list[Segment]:
    """Take a terminal-cell slice while preserving each segment's role."""
    if width <= 0:
        return []
    skipped = 0
    used = 0
    result: list[Segment] = []
    for text, role in segments:
        size = cell_width(text)
        if skipped + size <= start:
            skipped += size
            continue
        local_start = max(0, start - skipped)
        piece = cell_slice(text, local_start, local_start + width - used)
        if piece:
            result.append((piece, role))
            used += cell_width(piece)
        skipped += size
        if used >= width:
            break
    return result


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
    numbers (F1-F9 and Tab still follow the same order); below about 72 columns
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
    """`id  ● STATE  prio 5  after 6  title` as segments, with the columns aligned.

    The id, priority and dependencies are secondary (`muted`); the state is the
    one coloured element; the title stays in the default foreground. Rows of
    finished tasks recede: everything but the state word is `muted`.
    """
    ids = [_text(task_number(task) if task_number(task) is not None else "?", 40) for task in tasks]
    states = [_text(task.get("status", "unknown"), 24).upper() for task in tasks]
    shown = [_state_text(state) for state in states]
    priorities = [f"prio {_text(task['priority'], 8)}" if _known(task.get("priority")) else "" for task in tasks]  # `P0` reads as "most urgent"; here a higher number runs first
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
    """`id  ● STATE  prio 5  after 6  title` with the id, state, priority and dependency columns aligned."""
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


_CLOCK_ROW = re.compile(r"^(?P<clock>[\d-]{2}:[\d-]{2}:[\d-]{2}|\w{2,4} \d{2}:\d{2}|\d{4}-\d{2}-\d{2} \d{2}:\d{2})  (?P<text>.*)$")


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
    if (lowered.startswith("provider exited (") and not lowered.startswith("provider exited (0)")) or lowered.startswith("provider stopped by bridge"):
        return "error"
    if any(word in lowered for word in ("awaiting approval", "awaiting review", "waiting for capacity", "paused", "blocked")):
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


def _finished_operation_text(data: dict[str, object]) -> str:
    """`last operation exited 0 · 1m ago · awaiting review` when the newest operation is no longer running, else ``""``.

    Reads the operation row that the Sessions view shows, so Health and Sessions agree.
    """
    if _known(_dict(data.get("station_context")).get("executing_operation")):
        return ""
    operations = _items(data.get("operations"))
    last = operations[-1] if operations else {}
    status = str(last.get("status") or "").upper()
    if not status or status in ACTIVE_OPERATION_STATUSES:
        return ""
    exit_code = last.get("exit_code")
    age = _relative_epoch(last.get("updated_at") or last.get("created_at"), _clock_epoch())
    review = any(str(task.get("status") or "").upper() == "VERIFYING" for task in (*_items(data.get("tasks")), _dict(data.get("current_task"))))
    return _SEP.join(part for part in (
        f"last operation {status.lower()}" + ("" if exit_code is None else f" {_text(exit_code, 8)}"),
        age or "",
        "awaiting review" if review else "",
    ) if part)


def _recent_operation_segments(op: dict[str, object], age: str | None) -> list[Segment]:
    """`  recent: task 3 · succeeded · exit 0 · 2m ago`: plumbing in `dim`, a failure in `error`."""
    status = _text(op.get("status") or "-", 20).lower()
    exit_code = _text(op.get("exit_code"), 16) if op.get("exit_code") is not None else "-"
    failed = status in {"failed", "error"} or exit_code not in {"-", "0"}
    return [
        (f"  recent: task {_text(op.get('task_ordinal') or op.get('task_id') or '-', 12)}{_SEP}", "dim"),
        (status, "error" + QUIET if failed else "dim"),
        (_SEP, "dim"), (bridge_format.exit_short(exit_code), "error" + QUIET if exit_code not in {"-", "0"} else "dim"),
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
    ("agent_map", "age_tick"),
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
