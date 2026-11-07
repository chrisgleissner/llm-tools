"""Bounded copies of the mission status projection for the worker link.

``mission.status`` runs on a worker and its result travels over the worker connection and into the
coordinator's command journal. The full projection (``bridge_monitor.project_snapshot``) carries up
to 200 complete task records with both prompt fields, two copies of the newest events and the
frozen tool configuration, which is more than 100 KB for a mission with a few dozen tasks. The
console and ``llm-bridge --target ... status`` need none of that text. ``bound_status`` keeps the
fields they read and replaces the rest with counts. A caller that wants task text asks for it:
``prompt_chars`` returns that many characters of each prompt, and ``task.show`` returns a whole task.

``mission_digest`` is the few values the coordinator stores with each mission summary so that a
console can show counts and the current task without asking the worker.

This module imports only the standard library.
"""

from __future__ import annotations

from typing import Any

STATUS_TASK_LIMIT = 5  # tasks listed by default; unfinished ones first
STATUS_TASK_LIMIT_MAX = 50
STATUS_PROMPT_CHARS_MAX = 2000
STATUS_EVENT_LIMIT_MAX = 20
STATUS_STRING_CHARS = 2048  # longest string kept anywhere in the result (provider questions are at most this long)
STATUS_LIST_ITEMS = 20
STATUS_TITLE_CHARS = 200
STATUS_OBJECTIVE_CHARS = 160
STATUS_BYTE_LIMIT = 16 * 1024

# Task fields kept when the projection lists tasks. Prompts, acceptance text, evidence and amendment
# histories are left out; they have their own reads.
_TASK_FIELDS = (
    "id", "mission_id", "ordinal", "title", "status", "status_label", "priority", "ordering", "revision", "origin",
    "proposal_status", "proposed_by_operation", "depends_on_ordinals", "pending", "created_at", "updated_at",
)
_FINISHED_TASK_STATUSES = frozenset({"COMPLETED", "CANCELLED"})
# Parts dropped, in this order, when the result is still over STATUS_BYTE_LIMIT after the field limits.
_OPTIONAL_PARTS = ("capacity", "waiting", "stations", "operations", "pending_commands", "provider_wait", "awaiting_answer")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bound_value(value: Any, depth: int = 0) -> Any:
    """Clip every string and list in ``value``; deeply nested data is cut at depth 6."""
    if isinstance(value, str):
        return _clip(value, STATUS_STRING_CHARS)
    if depth >= 6:
        return None if isinstance(value, (dict, list)) else value
    if isinstance(value, dict):
        return {key: _bound_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_bound_value(item, depth + 1) for item in value[:STATUS_LIST_ITEMS]]
    return value


def _task(task: dict[str, Any], prompt_chars: int) -> dict[str, Any]:
    # Empty values (no dependencies, no pending change, no proposer) are left out: they are about a third of a task's bytes.
    kept = {key: task[key] for key in _TASK_FIELDS if key in task and task[key] not in (None, [], {})}
    if isinstance(kept.get("title"), str):
        kept["title"] = _clip(kept["title"], STATUS_TITLE_CHARS)
    if prompt_chars > 0:
        for key in ("prompt", "original_prompt"):
            if isinstance(task.get(key), str):
                kept[key] = _clip(task[key], prompt_chars)
    return kept


def _mission(mission: dict[str, Any]) -> dict[str, Any]:
    kept = {key: value for key, value in mission.items() if key not in {"config", "objective", "repo_identity"}}
    objective = mission.get("objective")
    if isinstance(objective, str):
        kept["objective"] = _clip(objective, STATUS_OBJECTIVE_CHARS)
    config = mission.get("config")
    if isinstance(config, dict):
        # frozen_tool_config is several KB of provider command templates; everything else is a few scalars.
        kept["config"] = {key: value for key, value in config.items() if key != "frozen_tool_config"}
    return kept


def bound_status(snapshot: dict[str, Any], *, task_limit: int = STATUS_TASK_LIMIT, prompt_chars: int = 0, event_limit: int = 0) -> dict[str, Any]:
    """A copy of ``snapshot`` that is a few KB whatever the size of the mission.

    ``tasks`` lists at most ``task_limit`` tasks, unfinished ones first and each in scheduling order,
    without prompt text unless ``prompt_chars`` asks for a prefix. ``events`` and ``operator_events``
    hold at most ``event_limit`` of the newest events (none by default). ``task_total``,
    ``task_counts`` and ``event_page`` still describe the whole mission.
    """
    task_limit = max(0, min(STATUS_TASK_LIMIT_MAX, task_limit))
    prompt_chars = max(0, min(STATUS_PROMPT_CHARS_MAX, prompt_chars))
    event_limit = max(0, min(STATUS_EVENT_LIMIT_MAX, event_limit))
    result = dict(snapshot)
    tasks = [task for task in snapshot.get("tasks") or [] if isinstance(task, dict)]
    open_tasks = [task for task in tasks if task.get("status") not in _FINISHED_TASK_STATUSES]
    finished = [task for task in tasks if task.get("status") in _FINISHED_TASK_STATUSES]
    listed = (open_tasks + finished)[:task_limit]
    result["tasks"] = [_task(task, prompt_chars) for task in listed]
    result["task_page"] = {"limit": task_limit, "offset": 0, "returned": len(listed), "search": ""}
    if isinstance(snapshot.get("mission"), dict):
        result["mission"] = _mission(snapshot["mission"])
    if isinstance(snapshot.get("current_task"), dict):
        result["current_task"] = _task(snapshot["current_task"], prompt_chars)
    for key in ("events", "operator_events"):
        if key in snapshot:
            events = snapshot.get(key) or []
            result[key] = events[-event_limit:] if event_limit else []
    if isinstance(snapshot.get("event_page"), dict):
        result["event_page"] = {**snapshot["event_page"], "returned": len(result.get("events") or [])}
    result = _bound_value(result)
    result["bounded"] = True
    for part in _OPTIONAL_PARTS:
        if len(repr(result)) <= STATUS_BYTE_LIMIT:
            break
        if part in result:
            result[part] = None
            result["truncated"] = True
    return result  # type: ignore[no-any-return]


def mission_digest(snapshot: dict[str, Any]) -> dict[str, Any]:
    """What a mission list shows about a live mission: task counts and what it is doing now.

    ``snapshot`` is a projection read with one task. The values are plain strings and numbers, a few
    hundred bytes in all, and never prompt or terminal text.
    """
    counts = snapshot.get("task_counts")
    current = snapshot.get("current_task")
    observed = snapshot.get("station_observation") or {}
    context = snapshot.get("station_context") or {}
    configuration = snapshot.get("station_config") or {}
    models = configuration.get("models") if isinstance(configuration, dict) else None

    def known(value: Any) -> str | None:
        return value if isinstance(value, str) and value.strip() and value.lower() != "unknown" else None

    station = known(observed.get("station")) or known(context.get("last_launched_station"))
    model = known(observed.get("model")) or (known(models.get(station)) if station and isinstance(models, dict) else None)
    needs = snapshot.get("needs_you")
    needs = needs if isinstance(needs, dict) else {}
    tasks = needs.get("tasks")
    digest: dict[str, Any] = {
        # What needs the operator, as a kind only (approval, capacity, provider, ...): the console
        # draws its attention list from it. The question itself travels as ``needs_you``.
        "needs_kind": needs.get("kind") if isinstance(needs.get("kind"), str) else None,
        "needs_tasks": len(tasks) if isinstance(tasks, list) else 0,
        "task_counts": {str(key): int(value) for key, value in counts.items() if isinstance(value, int)} if isinstance(counts, dict) else {},
        "current_task": {"ordinal": current.get("ordinal"), "title": _clip(str(current.get("title") or ""), STATUS_TITLE_CHARS)}
        if isinstance(current, dict) and isinstance(current.get("ordinal"), int) else None,
        "station": station,
        "model": model,
    }
    return digest
