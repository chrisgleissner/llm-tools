"""Read-only, bounded projections of durable LLM Bridge state."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from .bridge_store import BridgeStore


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


def _event_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def _configured_stations(config: dict[str, Any]) -> dict[str, Any]:
    frozen = config.get("frozen_tool_config", "")
    providers: list[str] = []
    routes: list[str] = []
    models: dict[str, str] = {}
    efforts: dict[str, str] = {}
    try:
        import tomllib

        parsed = tomllib.loads(frozen) if isinstance(frozen, str) and frozen else {}
        providers = sorted(str(key) for key in parsed.get("providers", {}))
        routes = sorted(str(key) for key in parsed.get("routes", {}))
        for provider, details in parsed.get("providers", {}).items():
            if isinstance(details, dict):
                if isinstance(details.get("model"), str):
                    models[str(provider)] = details["model"]
                if isinstance(details.get("effort"), str):
                    efforts[str(provider)] = details["effort"]
        for route, details in parsed.get("routes", {}).items():
            if isinstance(details, dict):
                if isinstance(details.get("model"), str):
                    models[str(route)] = details["model"]
                if isinstance(details.get("effort"), str):
                    efforts[str(route)] = details["effort"]
    except (ValueError, TypeError):
        pass
    return {
        "providers": providers,
        "routes": routes,
        "models": models,
        "efforts": efforts,
        "configured_mode": "native" if config.get("native_tui") else ("headless" if config.get("headless") else "autonomous"),
    }


def project_snapshot(
    store: BridgeStore,
    mission_id: str,
    task_limit: int | None = None,
    task_offset: int = 0,
    search: str = "",
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

    task_total = int(store.db.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", params).fetchone()[0])
    rows = store.db.execute(
        f"SELECT * FROM tasks WHERE {where} ORDER BY priority DESC,ordering,id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    tasks = [store._task_dict(row) for row in rows]
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
    counts = {str(row[0]): int(row[1]) for row in store.db.execute(
        "SELECT status,COUNT(*) FROM tasks WHERE mission_id=? AND (origin='operator' OR proposal_status='ACCEPTED') GROUP BY status", (mission_id,)
    )}
    proposal_counts = {str(row[0]): int(row[1]) for row in store.db.execute(
        "SELECT proposal_status,COUNT(*) FROM tasks WHERE mission_id=? AND origin='agent-proposal' GROUP BY proposal_status", (mission_id,)
    )}
    criterion_row = store.db.execute(
        "SELECT COALESCE(SUM(json_array_length(acceptance_json)),0), "
        "COALESCE(SUM(CASE WHEN status='COMPLETED' THEN json_array_length(acceptance_json) ELSE 0 END),0) "
        "FROM tasks WHERE mission_id=?", (mission_id,),
    ).fetchone()
    criterion_total, criterion_verified = int(criterion_row[0]), int(criterion_row[1])

    # Keep histories bounded regardless of mission lifetime. State projections
    # below use indexed latest-kind queries so high event churn cannot hide them.
    event_rows = store.db.execute(
        "SELECT * FROM events WHERE mission_id=? ORDER BY id DESC LIMIT 100", (mission_id,)
    ).fetchall()
    events = [_decode_event(row) for row in reversed(event_rows)]
    latest_selection = _latest_of_kinds(store, mission_id, ("capacity-refresh", "provider-selection"))
    latest_wait = _latest_event(store, mission_id, "provider-wait")
    latest_progress = _latest_event(store, mission_id, "task-verified")
    latest_handoff = _latest_event(store, mission_id, "provider-handoff")
    latest_station_observation = _latest_event(store, mission_id, "station-observation")
    latest_controller = _latest_event(store, mission_id, "controller-ownership")
    latest_heartbeat = _latest_event(store, mission_id, "controller-heartbeat")
    latest_suspend = _latest_suspend_event(store, mission_id)

    operations = [dict(row) for row in store.db.execute(
        "SELECT id,task_id,iteration,station,route_id,model,status,exit_code,logs_path,created_at,updated_at,native_session_id,restoration_state "
        "FROM operations WHERE mission_id=? ORDER BY created_at DESC,id DESC LIMIT 100", (mission_id,)
    )]
    for operation in operations:
        operation["configured_model"] = operation["model"] or "unknown"
        operation["model_source"] = "scheduler-configured pin"
    active = next((op for op in operations if op["status"] in {"INTENT", "PREPARED", "RUNNING"}), None)
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
            "status": "configured",
            "restoration_state": "unknown",
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
    configured_mode = configured["configured_mode"]
    suspend_data = (latest_suspend or {}).get("data", {})
    db = store.db
    pending_rows = store.db.execute(
        "SELECT id FROM commands WHERE mission_id=? AND status='QUEUED' ORDER BY created_at,id LIMIT 100", (mission_id,)
    ).fetchall()
    pending_commands = [store.get_command(str(row[0])) for row in pending_rows]
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
            "viewed_station": "unknown",
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
        "lifecycle": {
            "status": mission["status"],
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
        "health": {"status": "warning" if stall_warning else "unknown", "consecutive_failed_operations": failed_streak, "max_provider_failures": failure_budget, "failure_budget_source": "persisted provider-handoff event" if isinstance(handoff_budget, int) else ("configured mission rotation" if configured_failure_budget is not None else "unknown"), "stall_warning": stall_warning},
        "persistence": {"database": str(store.path), "schema_version": int(db.execute("PRAGMA user_version").fetchone()[0]), "journal_mode": str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()},
        "last_meaningful_progress": {"event_id": latest_progress["id"], "kind": latest_progress["kind"], "at": latest_progress["created_at"]} if latest_progress else None,
    }
