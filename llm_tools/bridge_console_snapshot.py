"""Bounded console projection of the status snapshot."""

from __future__ import annotations

import json
import math
from dataclasses import field
from . import bridge_theme
from .bridge_glyphs import glyph
from .bridge_event_text import _capacity_brief, _dict, _text
from .bridge_draw import _scrub
from .bridge_console_commands import _MAX_DETAIL_ITEMS, _MAX_DETAIL_TEXT, _MAX_EVENT_ROWS, _MAX_STATION_ROWS, _MAX_TASK_ROWS, _cut_text, _items


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
    from .bridge_policy import effective_settings

    result["mission"]["execution_policy"] = effective_settings(_dict(mission.get("config")))  # type: ignore[index]
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
        ("id", "operation_id", "task_id", "task_ordinal", "provider", "station", "route_id", "model", "native_session_id", "restoration_state", "phase", "status", "exit_code", "created_at", "updated_at"),
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
    agent_map = _dict(data.get("agent_map"))
    if agent_map:
        agents = []
        for item in _items(agent_map.get("agents"))[:200]:
            agent: dict[str, object] = {
                key: (_text(item.get(key), limit) if isinstance(item.get(key), str) else item.get(key))
                for key, limit in (
                    ("id", 160), ("provider", 60), ("kind", 40), ("mission_id", 80), ("mission_name", 100),
                    ("operation_id", 80), ("station", 60), ("native_id", 160), ("identity_source", 60),
                    ("native_parent_id", 160), ("native_root_id", 160), ("lifecycle_source", 60),
                    ("name", 100), ("parent_id", 160), ("root_id", 160), ("created_at", 80),
                    ("finished_at", 80), ("status", 40), ("activity", 180), ("model", 100),
                    ("role", 80), ("role_source", 24),
                    ("native_status", 40), ("last_activity_at", 80),
                    ("model_source", 24), ("configured_model", 100), ("reasoning_effort", 40),
                    ("effort_source", 24), ("configured_effort", 40), ("freshness", 40),
                    ("activity_freshness", 24), ("lifecycle_freshness", 24),
                ) if key in item
            }
            for key in ("age_seconds", "activity_age_seconds", "lifecycle_age_seconds"):
                age_value = item.get(key)
                agent[key] = age_value if isinstance(age_value, (int, float)) and not isinstance(age_value, bool) else None
            capabilities = _dict(item.get("capabilities"))
            agent["capabilities"] = {
                "inspect": capabilities.get("inspect") is True,
                "navigate": capabilities.get("navigate") is True,
                "reason": _text(capabilities.get("reason") or "unavailable", 160),
            }
            agents.append(agent)
        result["agent_map"] = {
            "observed_at": agent_map.get("observed_at") if isinstance(agent_map.get("observed_at"), (int, float)) else 0,
            "age_tick": 0,
            "freshness": _text(agent_map.get("freshness") or "unknown", 24),
            "agents": agents,
            "errors": {str(key): _text(value, 180) for key, value in _dict(agent_map.get("errors")).items()},
            "total": len(agents),
        }
    return result


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
