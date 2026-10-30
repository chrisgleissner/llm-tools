"""Read-only, bounded projections of durable LLM Bridge state."""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

from .bridge_format import countdown, needs_attention
from .bridge_store import BridgeStore
from .bridge_states import ACTIVE_OPERATION_STATUSES


def _decode_event(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["data"] = json.loads(item.pop("data_json"))
    return item


def _latest_event(store: BridgeStore, mission_id: str, kind: str) -> dict[str, Any] | None:
    row = store.db.execute(
        "SELECT * FROM events INDEXED BY events_mission_kind_id WHERE mission_id=? AND kind=? ORDER BY id DESC LIMIT 1",
        (mission_id, kind),
    ).fetchone()
    return _decode_event(row) if row else None


def _latest_of_kinds(store: BridgeStore, mission_id: str, kinds: tuple[str, ...]) -> dict[str, Any] | None:
    candidates = [
        event
        for kind in kinds
        if (event := _latest_event(store, mission_id, kind)) is not None
    ]
    return max(candidates, key=lambda event: int(event["id"])) if candidates else None


_SUSPEND_EVENT_KINDS = (
    "suspend_until_renewal",
    "suspend_deferred_awake",
    "suspend_unreliable",
    "suspend_fallback_awake",
)


def _latest_suspend_event(store: BridgeStore, mission_id: str) -> dict[str, Any] | None:
    candidates = []
    for kind in _SUSPEND_EVENT_KINDS:
        row = store.db.execute(
            "SELECT * FROM events INDEXED BY events_mission_kind_id "
            "WHERE mission_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (mission_id, kind),
        ).fetchone()
        if row is not None:
            candidates.append(row)
    return _decode_event(max(candidates, key=lambda row: int(row["id"]))) if candidates else None


# Events written by the controller's own bookkeeping. They drown the operator's
# view on a long mission (a heartbeat every few seconds), so the "recent
# activity" projection leaves them out. Every other kind, including kinds added
# later, counts as operator-meaningful.
PLUMBING_EVENT_KINDS = frozenset({
    "controller-heartbeat", "capacity-refresh", "operation-process", "repository-checkpoint",
    "controller-mode", "controller-config", "command-queued", "command-applied", "operation-progress",
    "events-pruned",
})
OPERATOR_EVENT_LIMIT = 20


def latest_operator_events(store: BridgeStore, mission_id: str, *, anchor: int | None = None, limit: int = OPERATOR_EVENT_LIMIT) -> list[dict[str, Any]]:
    """The newest ``limit`` events that are not controller plumbing, oldest first.

    ``kind NOT IN (...) ORDER BY id DESC`` scans every heartbeat when the last
    meaningful event is old (about 0.1 s for 100k heartbeats). Instead the
    mission's distinct kinds are enumerated with an index skip-scan, and the
    newest rows of each operator kind are read through
    ``events_mission_kind_id``; the cost depends on the number of kinds, not
    events (about 0.2 ms for the same 100k heartbeats).
    """
    kinds = [
        str(row[0]) for row in store.db.execute(
            "WITH RECURSIVE k(kind) AS (SELECT MIN(kind) FROM events WHERE mission_id=? UNION ALL "
            "SELECT (SELECT MIN(kind) FROM events WHERE mission_id=? AND kind>k.kind) FROM k WHERE kind IS NOT NULL) "
            "SELECT kind FROM k WHERE kind IS NOT NULL",
            (mission_id, mission_id),
        )
        if row[0] not in PLUMBING_EVENT_KINDS
    ]
    bound = "" if anchor is None else " AND id<=?"
    rows: list[Any] = []
    for kind in kinds:
        params: tuple[Any, ...] = (mission_id, kind) if anchor is None else (mission_id, kind, int(anchor))
        rows.extend(store.db.execute(
            f"SELECT * FROM events INDEXED BY events_mission_kind_id WHERE mission_id=? AND kind=?{bound} ORDER BY id DESC LIMIT {int(limit)}",
            params,
        ))
    newest = sorted(rows, key=lambda row: int(row["id"]), reverse=True)[:limit]
    return [_decode_event(row) for row in reversed(newest)]


EVENT_PAGE_SIZE = 20
_PAGE_CURSOR_KEYS = 64
_page_cursors: OrderedDict[tuple[str, str, int], dict[int, int]] = OrderedDict()
_page_cursor_lock = threading.Lock()


def _event_page_rows(store: BridgeStore, mission_id: str, anchor: int | None, offset: int) -> list[Any]:
    """One page of ``EVENT_PAGE_SIZE`` events, newest first, at ``offset`` rows below the anchor.

    ``LIMIT n OFFSET k`` reads and discards k rows, so a page deep in a long log
    cost time proportional to its depth (about 0.12 s at offset 900k). With an
    anchor, the rows below it never change, so each page read remembers the id
    bound where the next page starts, and the next read (the console re-reads its
    page several times a second, and pages one step at a time) starts there with a
    small offset instead of at the newest event. Without a remembered bound, for
    example a first jump to a deep offset, the read falls back to plain offsets.
    """
    if anchor is None:
        return store.db.execute(
            "SELECT * FROM events WHERE mission_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (mission_id, EVENT_PAGE_SIZE, offset),
        ).fetchall()
    key = (str(store.path), mission_id, int(anchor))
    with _page_cursor_lock:
        known = dict(_page_cursors.get(key, {}))
    start = max((at for at in known if at <= offset), default=None)
    start = 0 if start is None else start
    upper = known.get(start, int(anchor))
    rows = store.db.execute(
        "SELECT * FROM events WHERE mission_id=? AND id<=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (mission_id, upper, EVENT_PAGE_SIZE, offset - start),
    ).fetchall()
    with _page_cursor_lock:
        cursors = _page_cursors.setdefault(key, {0: int(anchor)})
        _page_cursors.move_to_end(key)
        if rows:
            cursors[offset] = int(rows[0]["id"])
        if len(rows) == EVENT_PAGE_SIZE:
            cursors[offset + EVENT_PAGE_SIZE] = int(rows[-1]["id"]) - 1
        while len(_page_cursors) > _PAGE_CURSOR_KEYS:
            _page_cursors.popitem(last=False)
    return rows


def _event_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def name_list(value: Any) -> list[str]:
    """Normalize a comma-separated string or a TOML list into unique names."""
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def _configured_stations(config: dict[str, Any]) -> dict[str, Any]:
    """Describe the mission's own rotation, not every table in the tool config.

    The rotation is the mission's ``routes`` (route mode) or ``providers``. Only
    when the mission recorded neither does the frozen ``[ralph].routes`` /
    ``[defaults].providers`` apply. Models and efforts come from the matching
    ``[providers.<name>]`` / ``[routes.<name>]`` tables.
    """
    frozen = config.get("frozen_tool_config", "")
    parsed: dict[str, Any] = {}
    models: dict[str, str] = {}
    efforts: dict[str, str] = {}
    try:
        import tomllib

        loaded = tomllib.loads(frozen) if isinstance(frozen, str) and frozen else {}
        parsed = loaded if isinstance(loaded, dict) else {}
    except (ValueError, TypeError):
        parsed = {}
    for table in ("providers", "routes"):
        section = parsed.get(table, {})
        for name, details in (section.items() if isinstance(section, dict) else ()):
            if isinstance(details, dict):
                if isinstance(details.get("model"), str):
                    models[str(name)] = details["model"]
                if isinstance(details.get("effort"), str):
                    efforts[str(name)] = details["effort"]
    routes = name_list(config.get("routes"))
    providers = name_list(config.get("providers"))
    if not routes and not providers:
        ralph = parsed.get("ralph", {})
        defaults = parsed.get("defaults", {})
        routes = name_list(ralph.get("routes") if isinstance(ralph, dict) else None)
        if not routes:
            providers = name_list(defaults.get("providers") if isinstance(defaults, dict) else None)
    if routes:
        providers = []
    return {
        "providers": providers,
        "routes": routes,
        "models": models,
        "efforts": efforts,
        "configured_mode": "native" if config.get("native_tui") else ("headless" if config.get("headless") else "background"),
    }


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def waiting_entry(decision: dict[str, Any]) -> dict[str, Any]:
    """The blocking window, or reason, of one provider that is not usable."""
    windows = [w for w in decision.get("windows", []) if isinstance(w, dict)] if isinstance(decision.get("windows"), list) else []
    measured = [w for w in windows if _number(w.get("remaining_percent")) is not None]
    blocking = min(measured, key=lambda w: float(w["remaining_percent"]), default=None)
    reset = _number(decision.get("wait_until"))
    if reset is None and blocking is not None:
        reset = _number(blocking.get("reset_epoch"))
    return {
        "name": str(decision.get("route") or decision.get("provider") or "?"),
        "provider": str(decision.get("provider") or ""),
        "window": str(blocking["name"]) if blocking is not None and blocking.get("name") else None,
        "remaining_percent": float(blocking["remaining_percent"]) if blocking is not None else None,
        "reset_epoch": int(reset) if reset is not None else None,
        "reason": str(decision.get("reason") or "") or None,
    }


def waiting_entry_text(entry: dict[str, Any], now: float) -> str:
    """``copilot monthly 0% · resets in 2d 9h`` for one waiting provider."""
    text = str(entry["name"])
    if entry.get("window"):
        text += f" {entry['window']}"
        if entry.get("remaining_percent") is not None:
            text += f" {entry['remaining_percent']:g}%"
    elif entry.get("reason"):
        text += f" {entry['reason']}"
    if entry.get("reset_epoch") is not None:
        text += f" · resets in {countdown(entry['reset_epoch'] - now)}"
    return text


def waiting_from_wait(data: dict[str, Any], now: float, since: str | None = None) -> dict[str, Any]:
    """Describe a ``provider-wait`` event: which providers block the mission and until when."""
    decisions = [d for d in data.get("decisions", []) if isinstance(d, dict) and d.get("usable") is not True] if isinstance(data.get("decisions"), list) else []
    entries = [waiting_entry(decision) for decision in decisions]
    resets = [entry["reset_epoch"] for entry in entries if entry["reset_epoch"] is not None]
    target = _number(data.get("target_epoch"))
    until = int(target) if target is not None else (min(resets) if resets else None)
    return {
        "reason": "capacity" if data.get("all_rate_limited") else "unavailable",
        "providers": [entry["name"] for entry in entries],
        "entries": entries,
        "until_epoch": until,
        "detail": "; ".join(waiting_entry_text(entry, now) for entry in entries),
        "since": since,
    }


def waiting_message(waiting: dict[str, Any], now: float) -> str:
    """The sentence shown in an empty provider pane while the mission waits."""
    parts = []
    for entry in waiting.get("entries", []):
        name = entry.get("name") or entry.get("provider") or "?"
        reset = _number(entry.get("reset_epoch"))
        if reset is not None:
            quota = f"{entry['window']} quota " if entry.get("window") else ""
            parts.append(f"{name} {quota}resets in {countdown(reset - now)}")
        else:
            parts.append(f"{name} {entry.get('reason') or 'is unavailable'}")
    until = _number(waiting.get("until_epoch"))
    if not parts and until is not None:
        parts.append(f"next provider ready in {countdown(until - now)}")
    subject = "capacity" if waiting.get("reason") == "capacity" else "a usable provider"
    return f"Waiting for {subject}: {'; '.join(parts) or 'no provider is ready yet'}. Ctrl-] then B for options."


def _attach_task_ordinals(store: BridgeStore, records: list[dict[str, Any]]) -> None:
    """Add ``task_ordinal`` (the per-mission task number) next to each record's global ``task_id``."""
    ids = sorted({int(record["task_id"]) for record in records if record.get("task_id") is not None})
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    ordinals = {int(row[0]): int(row[1]) for row in store.db.execute(f"SELECT id,ordinal FROM tasks WHERE id IN ({marks})", ids)}
    for record in records:
        if record.get("task_id") is not None:
            record["task_ordinal"] = ordinals.get(int(record["task_id"]))


def project_snapshot(
    store: BridgeStore,
    mission_id: str,
    task_limit: int | None = None,
    task_offset: int = 0,
    search: str = "",
    event_offset: int = 0,
    event_anchor: int | None = None,
    prompt_chars: int | None = None,
) -> dict[str, Any]:
    """The bounded status projection plus ``needs_you``: what the operator has to do next, or ``None``.

    ``prompt_chars`` limits the task prompt text read from SQLite to that many
    characters (the console only previews prompts; `task show` has the full text).
    """
    snapshot = _project_snapshot(store, mission_id, task_limit, task_offset, search, event_offset, event_anchor, prompt_chars)
    hold = store.review_hold(mission_id)
    # ``hold`` is set while the controller waits for the operator's decision on a
    # reviewed task before it starts the next one: {"reason": "review",
    # "task_ordinal", "tasks", "since"}; ``None`` otherwise.
    snapshot["hold"] = {key: value for key, value in hold.items() if key not in {"marker", "task_id"}} if hold else None
    snapshot["needs_you"] = needs_attention(snapshot)
    return snapshot


def _project_snapshot(
    store: BridgeStore,
    mission_id: str,
    task_limit: int | None = None,
    task_offset: int = 0,
    search: str = "",
    event_offset: int = 0,
    event_anchor: int | None = None,
    prompt_chars: int | None = None,
) -> dict[str, Any]:
    """Project monitor state using bounded queries and persisted runtime evidence only.

    `task_limit=None` still uses a bounded default page. Aggregate counts are SQL
    aggregates, so a 10k task queue does not become a 10k Python object graph.
    """
    mission = store.get_mission(mission_id)
    limit = max(1, min(500, 200 if task_limit is None else int(task_limit)))
    offset = max(0, int(task_offset))
    search = search.strip()
    where = "mission_id=?"
    params: list[Any] = [mission_id]
    if search:
        where += " AND (title LIKE ? ESCAPE '\\' OR prompt LIKE ? ESCAPE '\\')"
        needle = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params.extend((needle, needle))

    counters = store.task_counters(mission_id)
    # Only a search needs to look at task text; the unfiltered total is maintained.
    task_total = int(store.db.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", params).fetchone()[0]) if search else counters["total"]
    columns, column_params = "*", []
    if prompt_chars is not None:
        # Read only the head of each prompt: a task may hold up to 1 MiB of text.
        names = [str(row[1]) for row in store.db.execute("PRAGMA table_info(tasks)")]
        trimmed = {"prompt", "original_prompt"}
        columns = ",".join(f"substr({name},1,?) AS {name}" if name in trimmed else name for name in names)
        column_params = [max(1, int(prompt_chars))] * sum(name in trimmed for name in names)
    rows = store.db.execute(
        f"SELECT {columns} FROM tasks WHERE {where} ORDER BY priority DESC,ordering,id LIMIT ? OFFSET ?",
        (*column_params, *params, limit, offset),
    ).fetchall()
    tasks = store._task_dicts(rows)
    if tasks:
        placeholders = ",".join("?" for _ in tasks)
        amendment_rows = store.db.execute(
            "SELECT id,task_id,revision,changes_json,status,created_at,applied_at FROM ("
            "SELECT id,task_id,revision,changes_json,status,created_at,applied_at,"
            "ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY id DESC) AS rank "
            f"FROM amendments WHERE task_id IN ({placeholders})"
            ") WHERE rank<=5 ORDER BY task_id,id DESC",
            tuple(task["id"] for task in tasks),
        ).fetchall()
        amendments: dict[int, list[dict[str, Any]]] = {int(task["id"]): [] for task in tasks}
        for row in amendment_rows:
            item = dict(row)
            item["changes"] = json.loads(item.pop("changes_json"))
            amendments[int(item["task_id"])].append(item)
        for task in tasks:
            task["amendments"] = amendments[int(task["id"])]
    counts = counters["status"]
    proposal_counts = counters["proposal"]
    criterion_total, criterion_verified = counters["criteria_total"], counters["criteria_verified"]

    # Keep histories bounded regardless of mission lifetime. State projections
    # below use indexed latest-kind queries so high event churn cannot hide them.
    event_rows = _event_page_rows(store, mission_id, event_anchor, max(0, int(event_offset)))
    events = [_decode_event(row) for row in reversed(event_rows)]
    latest_selection = _latest_of_kinds(store, mission_id, ("capacity-refresh", "provider-selection"))
    latest_wait = _latest_event(store, mission_id, "provider-wait")
    latest_provider_selection = _latest_event(store, mission_id, "provider-selection")
    latest_progress = _latest_event(store, mission_id, "task-verified")
    latest_handoff = _latest_event(store, mission_id, "provider-handoff")
    latest_station_observation = _latest_event(store, mission_id, "station-observation")
    latest_controller = _latest_event(store, mission_id, "controller-ownership")
    latest_heartbeat = _latest_event(store, mission_id, "controller-heartbeat")
    latest_suspend = _latest_suspend_event(store, mission_id)

    operations = [dict(row) for row in store.db.execute(
        "SELECT id,task_id,iteration,station,route_id,model,status,exit_code,logs_path,created_at,updated_at,native_session_id,restoration_state "
        "FROM operations INDEXED BY operations_mission_history WHERE mission_id=? ORDER BY rowid DESC LIMIT 100", (mission_id,)
    )]
    for operation in operations:
        operation["configured_model"] = operation["model"] or "unknown"
        operation["model_source"] = "scheduler-configured pin"
    _attach_task_ordinals(store, [*events, *operations])
    active = next((op for op in operations if op["status"] in ACTIVE_OPERATION_STATUSES), None)
    latest_op = operations[0] if operations else None
    if active is not None:
        active_task = store.get_task(int(active["task_id"]))
    else:
        current_row = store.db.execute(
            "SELECT * FROM tasks WHERE mission_id=? AND status IN ('RUNNING','VERIFYING','READY') "
            "ORDER BY CASE status WHEN 'RUNNING' THEN 0 WHEN 'VERIFYING' THEN 1 ELSE 2 END,priority DESC,ordering,id LIMIT 1",
            (mission_id,),
        ).fetchone()
        if current_row is None and latest_op is not None:
            current_row = store.db.execute(
                "SELECT * FROM tasks WHERE id=? AND status IN ('BLOCKED','FAILED')",
                (latest_op["task_id"],),
            ).fetchone()
        active_task = store._task_dict(current_row) if current_row else None
    selected = (latest_selection or {}).get("data", {})
    capacity_event = max((event for event in (latest_selection, latest_wait) if event), key=lambda event: int(event["id"]), default=None)
    wait_is_current = bool(
        latest_wait is not None
        and (latest_selection is None or int(latest_wait["id"]) > int(latest_selection["id"]))
    )
    wait_data = (latest_wait or {}).get("data", {}) if wait_is_current else {}
    wait_target = wait_data.get("target_epoch") if isinstance(wait_data, dict) else None
    selected_decision = selected.get("decision", {}) if isinstance(selected, dict) else {}
    selected_target = selected_decision.get("wait_until") if isinstance(selected_decision, dict) else None
    selection_is_usable = isinstance(selected_decision, dict) and selected_decision.get("usable") is True
    readiness_target = wait_target if wait_is_current else (None if selection_is_usable else selected_target)
    next_eligible = int(readiness_target) if isinstance(readiness_target, (int, float)) and int(readiness_target) > int(time.time()) else None
    now = time.time()
    capacity_data = (capacity_event or {}).get("data", {})
    source_as_of = (
        capacity_data.get("observed_at")
        if isinstance(capacity_data, dict) and isinstance(capacity_data.get("observed_at"), str)
        else (capacity_event or {}).get("created_at")
    )
    source_time = _event_time(source_as_of)

    failed_streak = 0
    for operation in operations:
        if operation["status"] != "EXITED" or operation["exit_code"] in (None, 0):
            break
        failed_streak += 1
    stall_warning = None
    if failed_streak >= 3:
        stall_warning = f"{failed_streak} consecutive provider operations failed; no automatic stall claim is made for long-running work"
    from .bridge_progress import convergence_signals
    progress_boundary = int(latest_progress["id"]) if latest_progress else 0
    progress_rows = store.db.execute(
        "SELECT data_json FROM events WHERE mission_id=? AND kind='operation-progress' AND id>? ORDER BY id DESC LIMIT 32",
        (mission_id, progress_boundary),
    ).fetchall()
    convergence = convergence_signals([json.loads(row[0]) for row in reversed(progress_rows)])
    if convergence["warnings"]:
        stall_warning = "; ".join(convergence["warnings"])

    configured = _configured_stations(mission["config"])
    observation_data = (latest_station_observation or {}).get("data", {})
    observation_verified = isinstance(observation_data, dict) and observation_data.get("verified") is True
    observed_model = observation_data.get("observed_model", "unknown") if observation_verified else "unknown"
    observed_station = observation_data.get("station", "unknown") if observation_verified else "unknown"
    observed_effort = observation_data.get("observed_effort", "unknown") if observation_verified else "unknown"
    observed_model = observed_model if isinstance(observed_model, str) and observed_model else "unknown"
    observed_station = observed_station if isinstance(observed_station, str) and observed_station else "unknown"
    observed_effort = observed_effort if isinstance(observed_effort, str) and observed_effort else "unknown"
    last_launched_station = (active or latest_op or {}).get("route_id") or (active or latest_op or {}).get("station") or "unknown"
    station_names = list(dict.fromkeys([*configured["providers"], *configured["routes"]]))
    configured_names = set(station_names)
    for operation in operations:
        for name in (operation.get("route_id"), operation.get("station")):
            if name and name not in station_names:
                station_names.append(name)
    if last_launched_station != "unknown" and last_launched_station not in station_names:
        station_names.append(last_launched_station)
    stations = [
        {
            "id": name,
            "name": name,
            "provider": name,
            "model": configured["models"].get(name, "unknown"),
            "configured_model": configured["models"].get(name, "unknown"),
            "model_source": "frozen provider/route configuration",
            "effort": configured["efforts"].get(name, "unknown"),
            "observed_model": observed_model if name == observed_station else "unknown",
            "status": "configured" if name in configured_names else "historical",
            "restoration_state": next((op["restoration_state"] or "unknown" for op in operations
                                       if name in {op.get("route_id"), op.get("station")}), "unknown"),
        }
        for name in station_names[:32]
    ]
    rotations = mission["config"].get("routes") or mission["config"].get("providers") or ""
    rotation_count = len([item for item in str(rotations).split(",") if item.strip()])
    configured_failure_budget = max(2, rotation_count * 2) if rotation_count else None
    handoff_budget = (latest_handoff or {}).get("data", {}).get("max_failures")
    failure_budget = handoff_budget if isinstance(handoff_budget, int) else configured_failure_budget
    controller_data = (latest_controller or {}).get("data", {})
    controller_verified = isinstance(controller_data, dict) and controller_data.get("ownership_verified") is True
    heartbeat_data = (latest_heartbeat or {}).get("data", {})
    heartbeat_at = (
        heartbeat_data.get("observed_at")
        if isinstance(heartbeat_data, dict) and isinstance(heartbeat_data.get("observed_at"), str)
        else (latest_heartbeat or {}).get("created_at")
    )
    heartbeat_time = _event_time(heartbeat_at)
    latest_mode = _latest_event(store, mission_id, "controller-mode")
    mode_data = (latest_mode or {}).get("data", {})
    actual_mode = mode_data.get("actual_mode", "unknown") if isinstance(mode_data, dict) and mode_data.get("verified") is True else "unknown"
    if actual_mode == "autonomous":  # the name earlier controllers recorded for Background mode
        actual_mode = "background"
    configured_mode = configured["configured_mode"]
    suspend_data = (latest_suspend or {}).get("data", {})
    db = store.db
    pending_rows = store.db.execute(
        "SELECT id FROM commands INDEXED BY commands_mission_fifo WHERE mission_id=? AND status='QUEUED' ORDER BY rowid LIMIT 100", (mission_id,)
    ).fetchall()
    pending_commands = [store.get_command(str(row[0])) for row in pending_rows]
    waiting = None
    if (
        mission["status"] == "RUNNING"
        and latest_wait is not None
        and active is None
        and (latest_provider_selection is None or int(latest_wait["id"]) > int(latest_provider_selection["id"]))
        and isinstance(latest_wait["data"], dict)
    ):
        waiting = waiting_from_wait(latest_wait["data"], now, latest_wait.get("created_at"))
    latest_failure = _latest_event(store, mission_id, "controller-failed") if mission["status"] == "FAILED" else None
    failure_data = (latest_failure or {}).get("data", {})
    timed_operation = active or latest_op or {}
    began = _event_time(timed_operation.get("created_at"))
    ended = now if active else _event_time(timed_operation.get("updated_at"))
    operation_elapsed = max(0, int(ended-began)) if began is not None and ended is not None else None
    verified_at = _event_time(latest_progress.get("created_at")) if latest_progress else None
    return {
        "schema_version": 1,
        "mission": mission,
        "tasks": tasks,
        "task_counts": counts,
        "proposal_counts": proposal_counts,
        "task_total": task_total,
        "task_page": {"limit": limit, "offset": offset, "returned": len(tasks), "search": search},
        "criteria": {"verified": criterion_verified, "total": criterion_total, "complete": criterion_verified == criterion_total if criterion_total else None},
        "current_task": active_task,
        "pending_commands": pending_commands,
        "operations": list(reversed(operations[:20])),
        "events": events[-20:],
        "operator_events": latest_operator_events(store, mission_id, anchor=event_anchor),
        "event_page": {"offset": max(0, int(event_offset)), "limit": 20, "returned": len(events)},
        "stations": stations,
        "station_config": configured,
        "station_mode": {
            "configured_mode": configured_mode,
            "actual_mode": actual_mode,
            "source": "verified controller-mode event" if actual_mode != "unknown" else "unavailable",
        },
        "station_observation": {"station": observed_station, "model": observed_model, "effort": observed_effort},
        "station_context": {
            "executing_operation": active["id"] if active else None,
            "operation_status": active["status"] if active else (latest_op["status"] if latest_op else "unknown"),
            "provider_liveness": "unknown",
            "last_launched_station": last_launched_station,
            "viewed_station": mission["config"].get("viewed_station") or "unknown",
        },
        "execution": {
            "iteration": (active or latest_op or {}).get("iteration"),
            "operation_elapsed_seconds": operation_elapsed,
            "seconds_since_verified_progress": max(0, int(now-verified_at)) if verified_at is not None else None,
            "progress_source": "task-verified event" if latest_progress else "no verification evidence yet",
        },
        "capacity": {
            "selection": selected or None,
            "wait": wait_data or None,
            "next_eligible_epoch": next_eligible,
            "source": "persisted runtime event" if capacity_event else "unavailable",
            "source_event_kind": (capacity_event or {}).get("kind"),
            "source_event_id": (capacity_event or {}).get("id"),
            "source_as_of": source_as_of,
            "source_age_seconds": max(0, int(now - source_time)) if source_time is not None else None,
            "live": False,
        },
        "waiting": waiting,
        "lifecycle": {
            "status": mission["status"],
            "failure_reason": failure_data.get("reason") if isinstance(failure_data, dict) else None,
            "failed_at": (latest_failure or {}).get("created_at"),
            "controller": controller_data.get("status", "unknown") if controller_verified else "unknown",
            "controller_event_at": (latest_controller or {}).get("created_at") if controller_verified else None,
            "controller_source": "verified persisted ownership event" if controller_verified else "unavailable",
            "heartbeat": {
                "reported_state": heartbeat_data.get("controller_state", "unknown") if isinstance(heartbeat_data, dict) else "unknown",
                "observed_at": heartbeat_at,
                "age_seconds": max(0, int(now - heartbeat_time)) if heartbeat_time is not None else None,
                "owner_lock_held": heartbeat_data.get("owner_lock_held") if isinstance(heartbeat_data, dict) else None,
                "desired_mission_status": heartbeat_data.get("desired_mission_status", heartbeat_data.get("mission_status")) if isinstance(heartbeat_data, dict) else None,
                "current_liveness": "unknown",
                "source": "last persisted heartbeat" if latest_heartbeat else "unavailable",
            },
        },
        "suspend": {
            "status": (latest_suspend or {}).get("kind", "unknown"),
            "at": (latest_suspend or {}).get("created_at"),
            "target_epoch": suspend_data.get("target_epoch") if isinstance(suspend_data, dict) else None,
            "reason": suspend_data.get("reason") if isinstance(suspend_data, dict) else None,
            "source": "persisted event" if latest_suspend else "unavailable",
        },
        "health": {"status": "warning" if stall_warning else "unknown", "consecutive_failed_operations": failed_streak, "max_provider_failures": failure_budget, "failure_budget_source": "persisted provider-handoff event" if isinstance(handoff_budget, int) else ("configured mission rotation" if configured_failure_budget is not None else "unknown"), "stall_warning": stall_warning,
                   **{key: value for key, value in convergence.items() if key != "warnings"}},
        "persistence": {"database": str(store.path), "schema_version": int(db.execute("PRAGMA user_version").fetchone()[0]), "journal_mode": str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()},
        "last_meaningful_progress": {"event_id": latest_progress["id"], "kind": latest_progress["kind"], "at": latest_progress["created_at"]} if latest_progress else None,
    }
