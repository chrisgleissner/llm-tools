"""Human-readable renderings of Bridge CLI results.

The JSON responses stay the stable machine interface. These formatters only
turn the same dictionaries into short terminal text and deliberately leave out
bulky fields such as the frozen tool configuration and full prompts.
"""

from __future__ import annotations

import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from .bridge_states import FINISHED_MISSION_STATUSES, OPERATOR_DECLINED_EXIT_CODE, STARTED_MISSION_STATUSES

# Internal rotation reasons (ralph_robin) and their plain-word display forms.
# Only display text changes; event data and JSON keep the internal reason.
_PLAIN_ROTATION_REASONS = {
    "current-usable": "has capacity",
    "advanced-to-usable": "next with capacity",
    "even-burn": "spreading use",
    "all-unusable": "all limits reached",
    "max-runs": "run limit reached",
}


def plain_reason(reason: Any) -> str:
    """Human wording for a rotation reason; unknown reasons pass through unchanged."""
    text = "" if reason is None else str(reason)
    return _PLAIN_ROTATION_REASONS.get(text, text)


NATIVE_STOP_HINT = "Stopping after the provider exits · exit the provider (e.g. /quit) or use stop --force"


def nothing_to_pause(status: str) -> str:
    return f"Nothing to pause · mission is not running ({status})"


def nothing_to_stop(mission_id: Any, status: str) -> str:
    """Stop never cancels: with no controller the mission just stays saved."""
    next_step = "resume it with: llm-bridge resume" if status == "PAUSED" else "start it with: llm-bridge start"
    return f"Nothing to stop · mission {short_id(mission_id)} is saved ({status}) · {next_step}"
_NAME_LIMIT = 40
_TITLE_LIMIT = 70


def short_id(mission_id: Any) -> str:
    return str(mission_id)[:8]


def printable(value: Any) -> str:
    """Text safe to write to a raw terminal.

    Control characters (including ESC) become spaces, and surrogates and format
    characters (bidirectional overrides such as U+202E, zero-width marks) are
    removed, so mission names, task titles and agent-written proposals cannot
    inject escape sequences or reorder what the operator reads.
    """
    out: list[str] = []
    for char in str(value):
        category = unicodedata.category(char)
        if category == "Cc":
            out.append(" ")
        elif category not in {"Cs", "Cf"}:
            out.append(char)
    return "".join(out)


def _clip(value: Any, limit: int) -> str:
    text = " ".join(printable(value).split())
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    return f"{total // 86400}d"


def countdown(seconds: float) -> str:
    """Time until an event with two units where useful: ``2d 9h``, ``3h 5m``, ``12m``, ``<1m``."""
    total = int(seconds)
    if total < 60:
        return "now" if total <= 0 else "<1m"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def age(value: Any, now: float | None = None) -> str:
    stamp = _parse_time(value)
    if stamp is None:
        return "-"
    delta = (time.time() if now is None else now) - stamp
    return "just now" if delta < 5 else duration(delta) + " ago"


def home_relative(path: Any) -> str:
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    return "~" + text[len(home):] if home != "/" and text.startswith(home + "/") else text


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Left-aligned columns separated by at least two spaces; last column unpadded."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    def render(cells: list[str]) -> str:
        return "  ".join(cell if index == len(cells) - 1 else cell.ljust(widths[index]) for index, cell in enumerate(cells)).rstrip()
    return "\n".join([render(headers), *(render(row) for row in rows)])


_CONTROLLER_EXPECTED = STARTED_MISSION_STATUSES


def no_controller(item: dict[str, Any]) -> bool:
    """A mission that should have a live controller, but none holds its lock."""
    return str(item.get("status", "")).upper() in _CONTROLLER_EXPECTED and item.get("controller_running") is False


def format_mission_list(missions: list[dict[str, Any]], now: float | None = None) -> str:
    if not missions:
        return "No missions yet · create one with: llm-bridge mission create --prompt \"...\""
    rows = [
        [short_id(item["id"]), str(item.get("status", "?")) + (" (no controller)" if no_controller(item) else ""), age(item.get("updated_at"), now), _clip(item.get("name", ""), _NAME_LIMIT), home_relative(item.get("cwd", ""))]
        for item in missions
    ]
    return table(["ID", "STATUS", "UPDATED", "NAME", "FOLDER"], rows)


def task_number(task: dict[str, Any]) -> Any:
    """The number a person uses for a task: its place within the mission, not the global ID."""
    return task.get("ordinal", task.get("id"))


def format_mission_created(result: dict[str, Any]) -> str:
    mission = result["mission"]
    task = result.get("task") or {}
    mid = short_id(mission["id"])
    return f"Created mission {mid} \"{_clip(mission.get('name', ''), _NAME_LIMIT)}\" · task {task_number(task) or 1} · start with: llm-bridge start --mission {mid}"


def format_mission_colour(result: dict[str, Any]) -> str:
    """Receipt for `mission colour`."""
    how = "pinned" if result.get("pinned") else "automatic"
    return (f"Mission {short_id(result['mission_id'])} \"{_clip(result.get('name') or '', _NAME_LIMIT)}\" "
            f"colour: {result['number']} ({result['hue']}, {how})")


def format_task_line(task: dict[str, Any]) -> str:
    return f"task {task_number(task)} \"{_clip(task.get('title', ''), _TITLE_LIMIT)}\" · {task.get('status', '?')}"


class TaskPage(list):  # type: ignore[type-arg]
    """One page of ``task list``: a plain list (so JSON output is unchanged) that remembers the mission's task total."""

    total: int | None = None
    offset: int = 0


def format_task_list(tasks: list[dict[str, Any]]) -> str:
    total = getattr(tasks, "total", None)
    offset = int(getattr(tasks, "offset", 0) or 0)
    if not tasks:
        if total and offset:
            return f"No tasks at position {offset + 1}; the mission has {total}"
        return "No tasks · add one with: llm-bridge task add --prompt \"...\""
    text = _format_task_rows(tasks)
    if total is not None and len(tasks) < total - offset:
        text += f"\nshowing {len(tasks)} of {total} tasks from position {offset + 1} · llm-bridge task list --all lists every task, --offset N continues"
    return text


def _format_task_rows(tasks: list[dict[str, Any]]) -> str:
    rows = [
        [
            str(task_number(task)), str(task.get("status", "?")), str(task.get("priority", 0)),
            ",".join(str(item) for item in task.get("depends_on_ordinals", task.get("depends_on")) or []), _clip(task.get("title", ""), _TITLE_LIMIT),
        ]
        for task in tasks
    ]
    return table(["TASK", "STATUS", "PRIORITY", "AFTER", "TITLE"], rows)


def pending_amendment_line(pending: dict[str, Any]) -> str:
    text = pending.get("prompt")
    shown = _clip(text, 100) if isinstance(text, str) and text else "changes to " + ", ".join(sorted(str(key) for key in pending))
    return f"pending amendment (applies after the current step): {shown}"


def _evidence_line(item: Any, now: float | None) -> str:
    if not isinstance(item, dict):
        return f"  - {_clip(item, 100)}"
    source = str(item.get("source") or "")
    when = age(item.get("at"), now)
    extras = " · ".join(part for part in (source, when if when != "-" else "") if part)
    return f"  - {_clip(item.get('text', ''), 100)}" + (f" ({extras})" if extras else "")


def format_task_detail(task: dict[str, Any], now: float | None = None) -> str:
    after = task.get("depends_on_ordinals", task.get("depends_on")) or []
    lines = [
        format_task_line(task),
        f"priority {task.get('priority', 0)} · order {task.get('ordering', 0)} · revision {task.get('revision', 1)}"
        + (f" · depends on {', '.join(str(item) for item in after)}" if after else ""),
    ]
    acceptance = task.get("acceptance") or []
    if acceptance:
        lines.append("acceptance:")
        lines.extend(f"  - {_clip(item, 100)}" for item in acceptance)
    if task.get("pending"):
        lines.append(pending_amendment_line(task["pending"]))
    evidence = task.get("evidence") or []
    if evidence:
        lines.append("evidence:")
        lines.extend(_evidence_line(item, now) for item in evidence)
    prompt = str(task.get("prompt", ""))
    if prompt:
        shown = prompt.splitlines()[:12]
        lines.append("prompt:")
        lines.extend("  " + printable(line) for line in shown)
        if len(prompt.splitlines()) > len(shown):
            lines.append("  …")
    return "\n".join(lines)


def format_task_result(action: str, result: dict[str, Any]) -> str:
    """Receipt for one task command (add, amend, verify, pause, ...)."""
    inner = result.get("result") if isinstance(result.get("result"), dict) and "status" in result and "payload" in result else result
    inner = inner if isinstance(inner, dict) else {}
    if action == "add":
        text = f"Added {format_task_line(inner)} · priority {inner.get('priority', 0)}"
        if inner.get("mission_reopened"):
            text += "\nMission reopened · start it with: llm-bridge start"
        return text
    if action == "amend":
        number = inner.get("task_ordinal", inner.get("task_id"))
        text = f"Amended · task {number} is now revision {inner.get('revision')}"
        return text if inner.get("applied", True) else text + " · applies after the current step"
    if action == "verify":
        if inner.get("status") == "VERIFYING":
            number = task_number(inner)
            return f"Evidence recorded · task {number} still awaits approval · llm-bridge task verify --task {number} --approve"
        return f"Verified · {format_task_line(inner)}"
    if action == "reject":
        return f"Sent back · {format_task_line(inner)} · reason recorded"
    task_id = inner.get("ordinal", inner.get("task_ordinal", (result.get("payload") or {}).get("task_id", inner.get("task_id", inner.get("id", "?")))))
    verb = {"pause": "Paused", "resume": "Resumed", "cancel": "Cancelled", "retry": "Queued for retry", "priority": "Priority set for", "reorder": "Reordered", "dependencies": "Dependency added to"}.get(action, action.capitalize())
    text = f"{verb} task {task_id}"
    if inner.get("status"):
        text += f" · {inner['status']}"
    detail = inner.get("detail")
    return text + (f" · {detail}" if detail else "")


def rotation_names(config: dict[str, Any]) -> list[str]:
    """The saved rotation in order: routes win over providers."""
    for key in ("routes", "providers"):
        names = [item.strip() for item in str(config.get(key) or "").split(",") if item.strip()]
        if names:
            return names
    return []


def format_rotation_change(before: str, after: str) -> str:
    return f"Rotation changed: {before.replace(',', ', ')} → {after.replace(',', ', ')} (saved for this mission)"


def format_rotation(result: dict[str, Any]) -> str:
    """Saved rotation table, or the receipt for a rotation change."""
    mid = short_id(result.get("mission_id", ""))
    lines: list[str] = []
    if result.get("changed"):
        lines.append(format_rotation_change(str(result.get("from", "")), str(result.get("to", ""))))
        lines.append(str(result.get("applies", "")))
    elif result.get("updated"):
        lines.append(f"Rotation unchanged: {str(result.get('to', '')).replace(',', ', ')} (already saved for this mission)")
    entries = result.get("entries") or []
    if not entries:
        lines.append("No rotation saved for this mission.")
    else:
        if lines:
            lines.append("")
        lines.append(f"Rotation for mission {mid} · {result.get('mode', 'providers')} mode · tried in this order")
        rows = [
            [str(entry.get("position")), str(entry.get("name")), str(entry.get("launch_cli") or "?"), str(entry.get("model") or "provider default"), "selected" if entry.get("selected") else ""]
            for entry in entries
        ]
        lines.append(table(["#", "NAME", "LAUNCH CLI", "MODEL", "NOW"], rows))
    if not result.get("updated"):
        lines.append(f"Change it with: llm-bridge rotation --mission {mid} --set LIST")
    return "\n".join(lines)


def format_proposals(proposals: list[dict[str, Any]]) -> str:
    if not proposals:
        return "No proposals."
    rows = [[str(task_number(item)), str(item.get("proposal_status", "?")), _clip(item.get("title", ""), _TITLE_LIMIT)] for item in proposals]
    return table(["TASK", "PROPOSAL", "TITLE"], rows)


def format_proposal_result(action: str, result: dict[str, Any]) -> str:
    inner = result.get("result") if isinstance(result.get("result"), dict) and "payload" in result else result
    inner = inner if isinstance(inner, dict) else {}
    if action == "create":
        return f"Proposed {format_task_line(inner)}"
    task_id = inner.get("ordinal", (result.get("payload") or {}).get("task_id", inner.get("id", "?")))
    verb = "accepted" if action == "accept" else "rejected"
    return f"Proposal {verb} · task {task_id}"


def format_command_receipt(command: str, result: dict[str, Any], mission_id: str) -> str:
    """Receipt for pause/resume/stop/cancel and station/steer commands."""
    inner = result.get("result") if isinstance(result.get("result"), dict) and "payload" in result else result
    inner = inner if isinstance(inner, dict) else {}
    mid = short_id(mission_id)
    if inner.get("noop") and inner.get("detail"):
        return str(inner["detail"])
    if "station_status" in inner:
        mission = inner.get("mission") or {}
        if inner["station_status"] != "STOPPED" and mission.get("status") == "STOPPED":
            return nothing_to_stop(mission_id, "STOPPED")
        closed = "Station closed" if inner["station_status"] == "STOPPED" else "No station to close"
        return f"{closed} · mission {mid} is {mission.get('status', '?')}"
    if isinstance(inner.get("mission"), dict) and "mission_status" not in inner:
        return f"Stop forced · mission {mid} is {inner['mission'].get('status', '?')}"
    if command == "station":
        viewed = (inner.get("config") or {}).get("viewed_station") or (result.get("payload") or {}).get("station") or inner.get("viewed_station", "?")
        return f"Viewing station {viewed} · mission {mid}"
    if command == "steer":
        number = inner.get("task_ordinal", inner.get("task_id"))
        target = f" task {number}" if number is not None else ""
        return f"Steering {inner.get('kind', '')}{target} · {inner.get('status', 'APPLIED')}"
    if command == "continue" and inner.get("detail"):
        return str(inner["detail"])
    verb = {"pause": "Pause", "resume": "Resume", "stop": "Stop", "cancel": "Cancel"}.get(command, command.capitalize())
    text = f"{verb} requested · mission {mid} is {inner.get('mission_status', '?')}"
    detail = inner.get("detail")
    return text + (f"\n{detail}" if detail else "")


def _event_summary(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    parts = []
    for key, value in data.items():
        if key == "task_id":
            continue  # a global ID; the task number is shown instead
        if key == "depends_on_ordinal":
            parts.append(f"after task {value}")
            continue
        if key == "depends_on" and "depends_on_ordinal" in data:
            continue
        if isinstance(value, (str, int, float, bool)) and value != "":
            parts.append(f"{key}={_clip(plain_reason(value) if key in {'reason', 'rotation_reason'} else value, 40)}")
        if len(parts) == 4:
            break
    return _clip(" ".join(parts), 100)


def format_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No events."
    rows = []
    for event in events:
        stamp = _parse_time(event.get("created_at"))
        when = datetime.fromtimestamp(stamp).strftime("%m-%d %H:%M:%S") if stamp is not None else "-"
        number = event.get("task_ordinal")
        detail = _event_summary(event.get("data"))
        rows.append([str(event.get("id", "")), when, str(event.get("kind", "")), (f"task {number} " if number is not None else "") + detail])
    return table(["ID", "TIME", "KIND", "DETAIL"], rows)


def provider_check_text(check: dict[str, Any]) -> str:
    """``codex: installed, logged in`` / ``claude: not on PATH``."""
    name = str(check.get("name"))
    if check.get("installed") is False:
        return f"{name}: not on PATH · install it, or remove it from the rotation with: llm-bridge rotation --set LIST"
    if check.get("installed") is None:
        return f"{name}: {check.get('detail') or 'not checked'}"
    login = check.get("logged_in")
    if login is True:
        return f"{name}: installed, logged in"
    if login is False:
        hint = f" ({check['detail']})" if check.get("detail") else ""
        return f"{name}: installed, not logged in{hint}"
    return f"{name}: installed, login not checked"


def format_doctor(report: dict[str, Any]) -> str:
    lines = [
        f"State directory  {report.get('state_dir')}",
        f"Database         {report.get('database')} · integrity {report.get('sqlite_integrity')}",
        f"tmux             {'available' if report.get('tmux_available') else 'NOT FOUND · native stations need tmux'}",
    ]
    for index, check in enumerate(report.get("providers") or []):
        lines.append(f"{'Providers' if index == 0 else '':<17}{provider_check_text(check)}")
    mission = report.get("mission")
    if isinstance(mission, dict):
        lines.append(f"Mission          {short_id(mission['id'])} \"{_clip(mission.get('name', ''), _NAME_LIMIT)}\" · {mission.get('status')} · {home_relative(mission.get('cwd', ''))}")
    for index, step in enumerate(report.get("next_steps") or []):
        lines.append(f"{'Next' if index == 0 else '':<17}{step}")
    return "\n".join(lines)


def _window_text(window: dict[str, Any]) -> str:
    name = str(window.get("name", "?"))
    remaining = window.get("remaining_percent")
    if isinstance(remaining, (int, float)):
        return f"{name} {remaining:g}% left"
    amount = window.get("remaining_amount")
    if isinstance(amount, (int, float)):
        return f"{name} {window.get('currency', '')}{amount:g}".strip()
    return f"{name} {window.get('kind', 'unknown')}"


def _capacity_lines(snapshot: dict[str, Any]) -> list[str]:
    selection = (snapshot.get("capacity") or {}).get("selection") or {}
    decisions = selection.get("decisions") if isinstance(selection, dict) else None
    lines = []
    for decision in decisions if isinstance(decisions, list) else []:
        name = str(decision.get("route") or decision.get("provider") or "?")
        windows = " · ".join(_window_text(w) for w in decision.get("windows", []) if isinstance(w, dict)) or "no capacity data"
        state = "usable" if decision.get("usable") is True else str(decision.get("reason") or "not usable")
        lines.append(f"  {name:<14}{windows} · {state}")
    return lines


def format_waiting(waiting: dict[str, Any], mission_short: str, now: float | None = None) -> str:
    """``for capacity · copilot resets in 2d 9h · change providers with: ...``"""
    moment = time.time() if now is None else now
    parts = []
    for entry in waiting.get("entries") or []:
        if entry.get("reset_epoch") is not None:
            parts.append(f"{entry['name']} resets in {countdown(entry['reset_epoch'] - moment)}")
        else:
            parts.append(f"{entry['name']} {entry.get('reason') or 'unavailable'}")
    if not parts and isinstance(waiting.get("until_epoch"), (int, float)):
        parts.append(f"next provider in {countdown(waiting['until_epoch'] - moment)}")
    subject = "for capacity" if waiting.get("reason") == "capacity" else "for a usable provider"
    return " · ".join([subject, *([", ".join(parts)] if parts else []), f"change providers with: llm-bridge rotation --mission {mission_short} --set LIST"])


PROVIDER_DISPLAY_NAMES = {"claude": "Claude Code", "codex": "Codex", "copilot": "GitHub Copilot"}


def declined_provider(snapshot: dict[str, Any], task: dict[str, Any]) -> str:
    """Display name of the provider the operator declined to trust for ``task``, or ``""``."""
    latest = None
    for operation in snapshot.get("operations") or []:
        if isinstance(operation, dict) and operation.get("task_id") == task.get("id"):
            latest = operation
    if latest is None or latest.get("exit_code") != OPERATOR_DECLINED_EXIT_CODE:
        return ""
    station = str(latest.get("station") or "")
    return PROVIDER_DISPLAY_NAMES.get(station, station or "The provider")


def declined_message(provider: str) -> str:
    return f"{provider} was not allowed to trust this folder · trust it and run /start again, or choose another provider with /rotation"


def needs_attention(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """What the operator has to do next, from a status snapshot, or ``None``.

    Returns ``{"kind", "message", "command", "tasks"}``. ``kind`` is one of
    ``approval``, ``failed``, ``blocked``, ``proposal`` or ``capacity``;
    ``command`` is the CLI line that resolves it and ``tasks`` the task
    numbers involved.
    """
    mission = snapshot.get("mission") or {}
    mid = short_id(mission.get("id", ""))
    tasks = [task for task in snapshot.get("tasks") or [] if isinstance(task, dict)]
    awaiting = [int(task_number(task)) for task in tasks if task.get("status") == "VERIFYING" and task_number(task) is not None]
    if awaiting:
        first = awaiting[0]
        if len(awaiting) == 1:
            subject = f"task {first} awaits approval"
        else:
            subject = f"tasks {', '.join(str(n) for n in awaiting)} await approval"
        command = f"llm-bridge task verify --task {first} --approve"
        hold = snapshot.get("hold")
        if isinstance(hold, dict) and hold.get("reason") == "review":
            held = int(hold.get("task_ordinal") or first)
            return {
                "kind": "approval", "tasks": awaiting, "command": command, "hold": True,
                "message": f"task {held} awaits your review · /approve {held} or /reject {held} reason · /continue to start the next task now",
            }
        return {
            "kind": "approval", "tasks": awaiting, "command": command,
            "message": f"{subject} · {command} (or reject: llm-bridge task reject --task {first} --reason …)",
        }
    if mission.get("status") == "FAILED":
        reason = (snapshot.get("lifecycle") or {}).get("failure_reason")
        command = f"llm-bridge events --mission {mid}"
        return {"kind": "failed", "tasks": [], "command": command, "message": f"mission failed{' · ' + _clip(reason, 120) if reason else ''} · inspect with: {command}"}
    current = snapshot.get("current_task")
    if isinstance(current, dict) and current.get("status") in {"FAILED", "BLOCKED"}:
        number = task_number(current)
        if current["status"] == "FAILED":
            command = f"llm-bridge task retry --task {number}"
            message = f"task {number} failed · {command} (inspect first: llm-bridge events)"
            kind = "failed"
        else:
            command = f"llm-bridge task retry --task {number}"
            message = f"task {number} is blocked, its provider run ended without a result · check llm-bridge events, then {command}"
            kind = "blocked"
            declined = declined_provider(snapshot, current)
            if declined:
                command = f"llm-bridge start --mission {mid}"
                message = declined_message(declined)
        return {"kind": kind, "tasks": [int(number)] if isinstance(number, int) else [], "command": command, "message": message}
    pending = int((snapshot.get("proposal_counts") or {}).get("PENDING", 0))
    if pending:
        command = "llm-bridge proposal list"
        return {"kind": "proposal", "tasks": [], "command": command, "message": f"{pending} proposed task{'s' if pending != 1 else ''} await your decision · {command}"}
    if isinstance(snapshot.get("waiting"), dict):
        command = f"llm-bridge rotation --mission {mid} --set LIST"
        return {"kind": "capacity", "tasks": [], "command": command, "message": "waiting for provider capacity · " + format_waiting(snapshot["waiting"], mid)}
    return None


def format_status(snapshot: dict[str, Any], now: float | None = None) -> str:
    mission = snapshot["mission"]
    config = mission.get("config") or {}
    mode = "native" if config.get("native_tui") else ("headless" if config.get("headless") else "background")
    if config.get("trust_folder"):
        mode += ", trusts folder"
    lifecycle = snapshot.get("lifecycle") or {}
    heartbeat = lifecycle.get("heartbeat") or {}
    controller = lifecycle.get("controller", "unknown")
    heartbeat_age = heartbeat.get("age_seconds")
    if lifecycle.get("controller_running") is True:
        # The mission lock is the authority on whether a controller is alive;
        # the persisted ownership event may not exist yet.
        controller = "active"
        heartbeat_text = f" · heartbeat {duration(heartbeat_age)} ago" if isinstance(heartbeat_age, (int, float)) else ""
    else:
        heartbeat_text = f" · heartbeat {str(heartbeat.get('reported_state', 'unknown')).lower()} {duration(heartbeat_age)} ago" if isinstance(heartbeat_age, (int, float)) else ""
    controller_text = str(controller) + heartbeat_text
    mid = short_id(mission["id"])
    state = str(mission.get("status"))
    if state == "CREATED":
        controller_text = f"not started — run: llm-bridge start --mission {mid}"
    elif state == "STOPPED":
        controller_text = f"stopped — run: llm-bridge start --mission {mid}"
    elif state == "PAUSED":
        controller_text = f"paused — run: llm-bridge resume --mission {mid}"
    elif state in FINISHED_MISSION_STATUSES:
        controller_text = "finished"
    elif state == "FAILED":
        reason = lifecycle.get("failure_reason")
        controller_text = f"failed — {_clip(reason, 160)}" if reason else f"failed — reason not recorded; inspect with: llm-bridge events --mission {mid}"
    elif no_controller({"status": mission.get("status"), "controller_running": lifecycle.get("controller_running")}):
        controller_text = f"not running — run: llm-bridge resume --mission {mid}"
    lines = [
        f"Mission     {short_id(mission['id'])} \"{_clip(mission.get('name', ''), _NAME_LIMIT)}\" · {mission.get('status')} ({mode})",
        f"Folder      {home_relative(mission.get('cwd', ''))}",
        f"Controller  {controller_text}",
    ]
    rotation = rotation_names(config)
    if rotation:
        lines.append(f"Rotation    {' → '.join(rotation)}")
    context = snapshot.get("station_context") or {}
    current = snapshot.get("current_task")
    if current:
        operation = f" · operation {context['operation_status']}" if context.get("executing_operation") else ""
        lines.append(f"Current     {format_task_line(current)}{operation}")
    else:
        lines.append("Current     none")
    upcoming = next((task for task in snapshot.get("tasks", []) if task.get("status") in {"READY", "QUEUED"} and (not current or task.get("id") != current.get("id")) and task.get("origin", "operator") == "operator"), None)
    lines.append(f"Next        {format_task_line(upcoming)}" if upcoming else "Next        none")
    counts = snapshot.get("task_counts") or {}
    total = sum(int(value) for value in counts.values())
    lines.append(f"Tasks       {total} total" + (": " + ", ".join(f"{count} {name}" for name, count in sorted(counts.items())) if counts else ""))
    attention = snapshot.get("needs_you") or needs_attention(snapshot)
    if attention and attention.get("kind") != "capacity":
        lines.append(f"Needs you   {attention['message']}")
    hold = snapshot.get("hold")
    if isinstance(hold, dict) and hold.get("reason") == "review":
        since = _parse_time(hold.get("since"))
        age = f" for {duration((time.time() if now is None else now) - since)}" if since is not None else ""
        lines.append(f"Holding     the next task has not started{age} · task {hold.get('task_ordinal')} awaits your review")
    pending = snapshot.get("pending_commands") or []
    if pending:
        lines.append(f"Pending     {len(pending)} command(s) waiting for the controller")
    if mission.get("status") == "STOPPING" and config.get("native_tui") and not config.get("headless") and context.get("executing_operation"):
        lines.append(f"Stopping    {NATIVE_STOP_HINT}")
    capacity = _capacity_lines(snapshot)
    if capacity:
        lines.append("Capacity")
        lines.extend(capacity)
    eligible = (snapshot.get("capacity") or {}).get("next_eligible_epoch")
    waiting = snapshot.get("waiting")
    if isinstance(waiting, dict):
        lines.append(f"Waiting     {format_waiting(waiting, mid, now)}")
    elif isinstance(eligible, (int, float)):
        lines.append(f"Waiting     next provider capacity in {duration(eligible - (time.time() if now is None else now))}")
    warning = (snapshot.get("health") or {}).get("stall_warning")
    if warning:
        lines.append(f"Warning     {_clip(warning, 160)}")
    return "\n".join(lines)
