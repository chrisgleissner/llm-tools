"""Bounded, read-only observation of Bridge provider operations and native agents.

Bridge supplies the mission and operation roots. Provider adapters may add children only when
they can bind them to that exact root; provider-local history is never joined by cwd or recency.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

_MAX_CHILDREN = 100
_MAX_ROOTS = 200
_MAX_LOCAL_TAIL_RECORDS = 256
_MAX_LOCAL_TAIL_BYTES = 16 * 1024 * 1024
_MAX_RPC_RESPONSE_LINE_BYTES = 64 * 1024
_MAX_RPC_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_RPC_RESPONSE_LINES = 512
_MAX_TREE_NODES = 20_000
_ACTIVITY_STALE_SECONDS = 120
_CODEX_CHILD_SOURCES = ["subAgent", "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther"]


def _epoch(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        import math

        try:
            epoch = float(value)
        except OverflowError:
            return None
        return epoch if math.isfinite(epoch) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def _duration(start: object, end: object | None, now: float) -> int | None:
    began = _epoch(start)
    finished = _epoch(end) if end is not None else None
    if began is None:
        return None
    return max(0, int((finished if finished is not None else now) - began))


def _freshness(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "unknown"
    return "fresh" if age_seconds <= _ACTIVITY_STALE_SECONDS else "stale"


def _string(value: object, default: str = "unknown", limit: int = 240) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return " ".join(value.replace("\x00", "").split())[:limit] or default


def _codex_command(env: Mapping[str, str]) -> list[str] | None:
    override = env.get("LLM_BRIDGE_CODEX_APP_SERVER_CMD")
    if override:
        import shlex

        command = shlex.split(override)
        return command or None
    binary = shutil.which("codex", path=env.get("PATH"))
    return [binary, "app-server"] if binary else None


def _codex_rpc(command: list[str], requests: list[tuple[int, str, dict[str, Any]]], env: Mapping[str, str]) -> dict[int, dict[str, Any]]:
    """Run bounded read-only app-server requests; never resumes or starts a thread."""
    timeout = 3.0
    responses: dict[int, dict[str, Any]] = {}
    wanted = {request_id for request_id, _, _ in requests}
    completed = threading.Event()
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=dict(env),
            start_new_session=True,
        )
    except OSError:
        return {}

    def read() -> None:
        consumed = 0
        count = 0
        try:
            assert proc.stdout is not None
            while count < _MAX_RPC_RESPONSE_LINES and consumed < _MAX_RPC_RESPONSE_BYTES:
                line = proc.stdout.readline(_MAX_RPC_RESPONSE_LINE_BYTES + 1)
                if not line:
                    break
                count += 1
                consumed += len(line)
                if len(line) > _MAX_RPC_RESPONSE_LINE_BYTES or consumed > _MAX_RPC_RESPONSE_BYTES:
                    break
                try:
                    item = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(item, dict) and isinstance(item.get("id"), int) and item["id"] in wanted:
                    responses[item["id"]] = item
                    if wanted.issubset(responses):
                        completed.set()
                        return
        except (OSError, ValueError):
            pass
        finally:
            completed.set()

    reader = threading.Thread(target=read, name="llm-bridge-agent-observer", daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        init = {"id": 0, "method": "initialize", "params": {
            "clientInfo": {"name": "llm-bridge", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }}
        proc.stdin.write((json.dumps(init) + "\n").encode("utf-8"))
        proc.stdin.write((json.dumps({"method": "initialized"}) + "\n").encode("utf-8"))
        for request_id, method, params in requests:
            proc.stdin.write((json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode("utf-8"))
        proc.stdin.flush()
        completed.wait(timeout)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return responses


def _observed_status(thread: Mapping[str, Any]) -> tuple[str, str, object | None]:
    status_obj = thread.get("status")
    status = status_obj.get("type") if isinstance(status_obj, dict) else None
    flags = status_obj.get("activeFlags") if isinstance(status_obj, dict) else None
    if status == "active":
        activity = ", ".join(str(item) for item in flags[:3] if isinstance(item, str)) if isinstance(flags, list) else "active"
        return "active", _string(activity, "active"), None
    if status == "idle":
        return "idle", "provider reports this thread idle", None
    if status == "completed":
        return "completed", "provider reports this thread completed", None
    if status == "notLoaded":
        return "notLoaded", "thread is not loaded; current activity is unknown", None
    if status == "systemError":
        return "systemError", "provider reported a system error", None
    return "unknown", "activity unavailable", None


def _child_record(thread: Mapping[str, Any], *, mission_id: str, root_id: str, now: float) -> dict[str, Any] | None:
    thread_id = thread.get("id")
    parent_id = thread.get("parentThreadId")
    if not isinstance(thread_id, str) or not thread_id or not isinstance(parent_id, str) or not parent_id:
        return None
    status, activity, finished_at = _observed_status(thread)
    created = thread.get("createdAt")
    if status == "completed":
        finished_at = thread.get("updatedAt")
    return {
        "id": f"bridge:{mission_id}:codex:{thread_id}",
        "provider": "codex",
        "kind": "native-child",
        "mission_id": mission_id,
        "native_id": thread_id,
        "native_parent_id": parent_id,
        "native_root_id": root_id,
        "name": _string(thread.get("name") or thread.get("agentNickname"), "Codex agent", 100),
        "role": _string(thread.get("agentRole"), "unknown", 80),
        "role_source": "observed" if isinstance(thread.get("agentRole"), str) and thread.get("agentRole") else "unknown",
        "parent_id": f"bridge:{mission_id}:codex:{parent_id}",
        "root_id": f"bridge:{mission_id}:codex:{root_id}",
        "created_at": created if isinstance(created, (int, float, str)) else None,
        "finished_at": finished_at,
        "age_seconds": None if status == "completed" and _epoch(finished_at) is None else _duration(created, finished_at, now),
        "status": status,
        "native_status": status,
        "native_status_updated_at": thread.get("updatedAt") if isinstance(thread.get("updatedAt"), (int, float, str)) else None,
        "activity": activity,
        "last_activity_at": thread.get("updatedAt") if isinstance(thread.get("updatedAt"), (int, float, str)) else None,
        "activity_age_seconds": _duration(thread.get("updatedAt"), None, now),
        "activity_freshness": _freshness(_duration(thread.get("updatedAt"), None, now)),
        "lifecycle_age_seconds": None,
        "lifecycle_freshness": "unknown",
        "model": _string(thread.get("model")) if isinstance(thread.get("model"), str) else "unknown",
        "model_source": "observed" if isinstance(thread.get("model"), str) and thread.get("model") else "unknown",
        "configured_model": "unknown",
        "reasoning_effort": _string(thread.get("reasoningEffort")) if isinstance(thread.get("reasoningEffort"), str) else "unknown",
        "effort_source": "observed" if isinstance(thread.get("reasoningEffort"), str) and thread.get("reasoningEffort") else "unknown",
        "configured_effort": "unknown",
        "freshness": "partial" if status == "notLoaded" else "fresh",
        "lifecycle_source": "Codex app-server",
        "capabilities": {"inspect": True, "navigate": False, "reason": "Codex child sessions are inspect-only; resuming would change provider state."},
    }


def _apply_local_child_lifecycle(records: list[dict[str, Any]], env: Mapping[str, str], *, now: float) -> None:
    children = [row for row in records if row.get("kind") == "native-child"][:_MAX_LOCAL_TAIL_RECORDS]
    if not children:
        return
    home = Path(env.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    sessions_root = home / "sessions"
    try:
        from .bridge_sessions import codex_session_activity_tail, codex_session_records_by_id

        native_ids = {str(row["native_id"]) for row in children if row.get("native_id")}
        session_records = codex_session_records_by_id(sessions_root, native_ids, max_files=4096)
    except (OSError, RuntimeError, ValueError, TypeError):
        return
    consumed = 0
    for child in children:
        session_id = str(child.get("native_id") or "")
        parent_id = str(child.get("native_parent_id") or "")
        session_record = session_records.get(session_id)
        if session_record is None or session_record.identity.parent_session_id != parent_id:
            continue
        try:
            size = session_record.path.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        remaining_budget = _MAX_LOCAL_TAIL_BYTES - consumed
        if size > remaining_budget:
            child["freshness"] = "partial"
            break
        consumed += size
        activity = codex_session_activity_tail(
            session_record.path, session_id=session_id, parent_id=parent_id, sessions_root=sessions_root,
            max_bytes=remaining_budget,
        )
        if activity is None:
            continue
        child["activity"] = activity["activity"]
        child["last_activity_at"] = activity["timestamp"]
        observed_epoch = _epoch(activity["timestamp"])
        activity_age = max(0, int(now - observed_epoch)) if observed_epoch is not None else None
        child["activity_age_seconds"] = activity_age
        child["activity_freshness"] = _freshness(activity_age)
        if activity_age is not None and activity_age > _ACTIVITY_STALE_SECONDS:
            child["freshness"] = "stale"
        lifecycle = activity.get("lifecycle")
        lifecycle_at = activity.get("lifecycle_timestamp")
        child["lifecycle_event"] = lifecycle
        child["lifecycle_at"] = lifecycle_at
        lifecycle_epoch = _epoch(lifecycle_at)
        lifecycle_age = max(0, int(now - lifecycle_epoch)) if lifecycle_epoch is not None else None
        child["lifecycle_age_seconds"] = lifecycle_age
        child["lifecycle_freshness"] = _freshness(lifecycle_age)
        if lifecycle_age is not None and lifecycle_age > _ACTIVITY_STALE_SECONDS:
            child["freshness"] = "stale"
        native_updated_epoch = _epoch(child.get("native_status_updated_at"))
        can_use_local_lifecycle = (
            child.get("native_status") in {"notLoaded", "unknown"}
            or native_updated_epoch is None
            or (lifecycle_epoch is not None and lifecycle_epoch > native_updated_epoch)
        )
        if can_use_local_lifecycle and lifecycle in {"task_started", "task_complete", "turn_aborted"}:
            if lifecycle == "task_started" and lifecycle_age is not None and lifecycle_age <= _ACTIVITY_STALE_SECONDS:
                child["status"] = "active"
                child["finished_at"] = None
                child["age_seconds"] = _duration(child.get("created_at"), None, now)
            elif lifecycle in {"task_complete", "turn_aborted"}:
                child["status"] = "completed" if lifecycle == "task_complete" else "interrupted"
                child["finished_at"] = lifecycle_at
                child["age_seconds"] = _duration(child.get("created_at"), lifecycle_at, now) if lifecycle_epoch is not None else None
        child["lifecycle_source"] = "bounded exact Codex session events"


def build_agent_map(
    store: Any,
    *,
    mission_id: str | None = None,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
    codex_rpc: Callable[[list[str], list[tuple[int, str, dict[str, Any]]], Mapping[str, str]], dict[int, dict[str, Any]]] = _codex_rpc,
) -> dict[str, Any]:
    """Project current operation roots plus provider children into a stable map.

    Root identity comes from Bridge's exact native session ID or its process-bound Codex
    discovery. All provider child enumeration is skipped when the exact root cannot be proved.
    """
    env_map = dict(os.environ if env is None else env)
    observed_at = time.time() if now is None else float(now)
    missions = store.list_missions()
    from .bridge_missions import station_live

    if mission_id is not None:
        from .bridge_missions import resolve_mission_ref

        mission_id = resolve_mission_ref(store, mission_id, read_only=True)
        missions = [mission for mission in missions if str(mission.get("id")) == mission_id]
    missions.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")))
    command = _codex_command(env_map)
    records: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    if not missions:
        return {"schema_version": 1, "observed_at": observed_at, "freshness": "partial", "capabilities": {}, "agents": [], "errors": {}, "total": 0}
    query = (
        "SELECT o.id,o.mission_id,o.task_id,o.station,o.route_id,o.model,o.status,o.exit_code,o.native_session_id,o.process_pid,o.process_start,o.restoration_state,o.created_at,o.updated_at,"
        "t.ordinal,t.title,t.status AS task_status,m.name AS mission_name,m.cwd,m.config_json "
        "FROM operations o JOIN missions m ON m.id=o.mission_id LEFT JOIN tasks t ON t.id=o.task_id "
        "WHERE (? IS NULL OR o.mission_id=?) AND (o.status IN ('INTENT','PREPARED','RUNNING') OR ("
        "NOT EXISTS (SELECT 1 FROM operations a WHERE a.mission_id=o.mission_id AND a.status IN ('INTENT','PREPARED','RUNNING')) "
        "AND o.id=(SELECT z.id FROM operations z WHERE z.mission_id=o.mission_id ORDER BY z.created_at DESC,z.id DESC LIMIT 1))) "
        "ORDER BY m.updated_at DESC,m.id,o.created_at DESC,o.id LIMIT ?"
    )
    mission_rows = {str(mission["id"]): mission for mission in missions}
    operation_rows = store.db.execute(query, (mission_id, mission_id, _MAX_ROOTS)).fetchall()
    rpc_roots: list[tuple[dict[str, Any], str, list[tuple[int, str, dict[str, Any]]]]] = []
    navigation_by_mission: dict[str, bool] = {}
    for operation in operation_rows:
        mid = str(operation["mission_id"])
        mission = mission_rows.get(mid, {})
        try:
            config = json.loads(operation["config_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        frozen = _frozen_config(config)
        route_id = str(operation["route_id"] or "")
        route_config = _route_config(frozen, route_id) if route_id else {}
        station = _string(operation["station"], "unknown", 60)
        if route_id:
            from .bridge_switcher import provider_of_station

            provider = _string(provider_of_station(config, route_id) if route_config else "unknown", "unknown", 60)
        else:
            provider = station
        configured_model = _string(route_config.get("model") or _configured(frozen, provider, "model") or operation["model"])
        configured_effort = _string(route_config.get("reasoning_effort") or route_config.get("effort") or _configured(frozen, provider, "reasoning_effort") or _configured(frozen, provider, "effort"))
        status_raw = str(operation["status"] or "unknown")
        is_active = status_raw in {"INTENT", "PREPARED", "RUNNING"}
        finished_at = operation["updated_at"] if status_raw == "EXITED" else None
        root_id = operation["native_session_id"] if isinstance(operation["native_session_id"], str) else None
        identity_source = "Bridge native session" if root_id else "unavailable"
        if provider == "codex" and not root_id and is_active and operation["process_pid"] and operation["process_start"]:
            try:
                from .bridge_sessions import discover_owned_codex_root_session

                identity = discover_owned_codex_root_session(
                    int(operation["process_pid"]), str(operation["process_start"]), cwd=str(operation["cwd"]),
                    started_after=_epoch(operation["created_at"]) or 0,
                )
                if identity is not None:
                    root_id = identity.session_id
                    identity_source = "process-bound Codex session metadata"
            except (OSError, ValueError, TypeError):
                pass
        if mid not in navigation_by_mission:
            navigation_by_mission[mid] = station_live(mid)
        can_navigate = navigation_by_mission[mid]
        root_agent_id = f"bridge:{mid}:codex:{root_id}" if provider == "codex" and root_id else f"bridge:{mid}:{operation['id']}"
        root_record = {
            "id": root_agent_id,
            "provider": provider,
            "kind": "bridge-root",
            "mission_id": mid,
            "mission_name": _string(operation["mission_name"], mid[:8], 100),
            "operation_id": str(operation["id"]),
            "station": station,
            "native_id": root_id,
            "identity_source": identity_source,
            "name": _string(operation["title"], f"{provider} operation", 100),
            "parent_id": None,
            "root_id": root_agent_id,
            "created_at": operation["created_at"],
            "finished_at": finished_at,
            "age_seconds": _duration(operation["created_at"], finished_at, observed_at) if is_active or status_raw == "EXITED" else None,
            "status": status_raw.lower() if is_active else ("completed" if status_raw == "EXITED" and operation["exit_code"] == 0 else ("failed" if status_raw == "EXITED" else "unknown")),
            "activity": f"task {operation['ordinal']}: {_string(operation['title'], 'provider operation', 120)} · {status_raw.lower()}",
            # A provider exit is not the task's result: say so when the task now waits for the operator.
            **({"status_label": "exited 0 · awaiting review"} if status_raw == "EXITED" and operation["exit_code"] == 0 and operation["task_status"] == "VERIFYING" else {}),
            "model": "unknown",
            "model_source": "unknown",
            "configured_model": configured_model,
            "reasoning_effort": "unknown",
            "effort_source": "unknown",
            "configured_effort": configured_effort,
            "freshness": "Bridge store",
            "capabilities": {
                "inspect": True,
                "navigate": can_navigate,
                "reason": "Switches to the existing Bridge mission station." if can_navigate else "Inspect only; no live Bridge station is available to switch to.",
            },
        }
        records.append(root_record)
        if provider != "codex" or not root_id:
            continue
        if command is None:
            errors[mid] = "Codex app-server unavailable; exact root is known but native child listing is unavailable."
            continue
        req: list[tuple[int, str, dict[str, Any]]] = [(0, "thread/read", {"threadId": root_id, "includeTurns": False}), (0, "thread/list", {
            "ancestorThreadId": root_id,
            "sourceKinds": _CODEX_CHILD_SOURCES,
            "limit": _MAX_CHILDREN,
            "sortKey": "created_at",
            "sortDirection": "asc",
        })]
        rpc_roots.append((root_record, root_id, req))
    if command is not None and rpc_roots:
        combined: list[tuple[int, str, dict[str, Any]]] = []
        request_ids: dict[int, tuple[int, int]] = {}
        for index, (_root_record, root_id, requests) in enumerate(rpc_roots):
            first_id = index * 2 + 1
            root_request_id = first_id
            child_request_id = first_id + 1
            root_request_tuple = (root_request_id, requests[0][1], requests[0][2])
            children_request_tuple = (child_request_id, requests[1][1], requests[1][2])
            combined.extend((root_request_tuple, children_request_tuple))
            request_ids[id(_root_record)] = (root_request_id, child_request_id)
        replies = codex_rpc(command, combined, env_map)
        for root_record, root_id, _requests in rpc_roots:
            root_reply_id, children_reply_id = request_ids[id(root_record)]
            root_response = replies.get(root_reply_id, {})
            root_reply = root_response.get("result", {})
            root_thread = root_reply.get("thread") if isinstance(root_reply, dict) else None
            if isinstance(root_thread, dict) and root_thread.get("id") == root_id:
                root_record["model"] = _string(root_thread.get("model"))
                root_record["model_source"] = "observed" if isinstance(root_thread.get("model"), str) and root_thread.get("model") else "unknown"
                root_record["reasoning_effort"] = _string(root_thread.get("reasoningEffort"))
                root_record["effort_source"] = "observed" if isinstance(root_thread.get("reasoningEffort"), str) and root_thread.get("reasoningEffort") else "unknown"
                root_record["freshness"] = "Codex app-server"
                root_record["native_status"] = _observed_status(root_thread)[0]
            elif root_response.get("error"):
                errors[str(root_record["mission_id"])] = "Codex app-server rejected exact root metadata inspection."
            else:
                errors[str(root_record["mission_id"])] = "Codex app-server returned no exact root metadata."
            child_response = replies.get(children_reply_id, {})
            child_reply = child_response.get("result", {})
            child_items = child_reply.get("data") if isinstance(child_reply, dict) else None
            if isinstance(child_items, list):
                children = sorted((item for item in child_items if isinstance(item, dict)), key=lambda row: (_epoch(row.get("createdAt")) or 0, str(row.get("id") or "")))
                available_ids = {str(item.get("id")) for item in children if isinstance(item.get("id"), str)}
                known = {root_id}
                pending = list(children)
                while pending:
                    before = len(pending)
                    for child in pending[:]:
                        thread_id = child.get("id")
                        parent_id = child.get("parentThreadId")
                        if not isinstance(thread_id, str) or thread_id not in available_ids or parent_id not in known:
                            continue
                        pending.remove(child)
                        if thread_id in known:
                            continue
                        record = _child_record(child, mission_id=str(root_record["mission_id"]), root_id=root_id, now=observed_at)
                        if record is not None:
                            records.append(record)
                            known.add(thread_id)
                    if len(pending) == before:
                        break
                if child_reply.get("nextCursor"):
                    errors[str(root_record["mission_id"])] = "Codex child listing reached its 100-agent display limit."
            elif child_response.get("error"):
                errors[str(root_record["mission_id"])] = "Installed Codex app-server rejected native child enumeration; child map unavailable."
            else:
                errors[str(root_record["mission_id"])] = "Codex app-server returned no native child listing."
    _apply_local_child_lifecycle(records, env_map, now=observed_at)
    records.sort(key=lambda row: (str(row.get("mission_id") or ""), str(row.get("operation_id") or ""), str(row.get("kind")), str(row.get("native_id") or row.get("id"))))
    partial_rows = any(row.get("freshness") in {"partial", "stale"} for row in records)
    return {
        "schema_version": 1,
        "observed_at": observed_at,
        "freshness": "fresh" if command and not errors and not partial_rows else ("partial" if command or errors else "unavailable"),
        "capabilities": {
            "providers": sorted({str(row.get("provider") or "unknown") for row in records}),
            "children": "Codex app-server ancestor-bound threads only",
        },
        "agents": records,
        "errors": errors,
        "total": len(records),
    }


def _configured(config: Mapping[str, Any], provider: str, field: str) -> object:
    providers = config.get("providers")
    provider_config = providers.get(provider) if isinstance(providers, dict) else None
    if isinstance(provider_config, dict) and provider_config.get(field) not in (None, ""):
        return provider_config[field]
    policies = config.get("policies")
    policy = policies.get(provider) if isinstance(policies, dict) else None
    if isinstance(policy, dict) and policy.get(field) not in (None, ""):
        return policy[field]
    routes = config.get("route_policies")
    route = routes.get(provider) if isinstance(routes, dict) else None
    return route.get(field) if isinstance(route, dict) else None


def _frozen_config(config: Mapping[str, Any]) -> dict[str, Any]:
    from .bridge_rotation import parse_frozen

    return parse_frozen(config.get("frozen_tool_config"))


def _route_config(config: Mapping[str, Any], route_id: str) -> dict[str, Any]:
    routes = config.get("routes")
    route = routes.get(route_id) if isinstance(routes, dict) else None
    return route if isinstance(route, dict) else {}


def format_agent_map(data: Mapping[str, Any], *, now: float | None = None) -> list[str]:
    """Stable compact text rows with locally advancing age and a clear unknown marker."""
    timestamp = time.time() if now is None else float(now)
    ordered = ordered_agents(data)
    if not ordered:
        return ["No provider operations or observable native agents."]
    lines = []
    for item, depth in ordered:
        age = item.get("age_seconds")
        if isinstance(age, (int, float)) and item.get("finished_at") is None:
            age = max(0, int(age + max(0, timestamp - float(data.get("observed_at", timestamp)))))
        age_text = "?" if age is None else _short_age(int(age))
        model = item.get("model") if item.get("model_source") == "observed" else f"cfg:{item.get('configured_model', 'unknown')}"
        effort = item.get("reasoning_effort") if item.get("effort_source") == "observed" else f"cfg:{item.get('configured_effort', 'unknown')}"
        line = f"{'  ' * depth}{_string(item.get('name'), 'agent', 30)} · {item.get('provider', 'unknown')} · {age_text} · {item.get('status_label') or item.get('status', 'unknown')} · {model}/{effort} · {_string(item.get('activity'), 'activity unavailable', 70)}"
        lines.append(line[:240])
    return lines


def ordered_agents(data: Mapping[str, Any]) -> list[tuple[dict[str, Any], int]]:
    """Return stable preorder rows so selection, tree depth and inspection share one order."""
    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        return []
    by_id = {str(item.get("id")): item for item in agents if isinstance(item, dict) and item.get("id")}
    children: dict[str, list[dict[str, Any]]] = {}
    roots = []
    for item in by_id.values():
        parent = item.get("parent_id")
        if isinstance(parent, str) and parent in by_id:
            children.setdefault(parent, []).append(item)
        else:
            roots.append(item)
    stable_key = lambda item: (str(item.get("mission_name") or ""), str(item.get("provider") or ""), str(item.get("operation_id") or ""), str(item.get("native_id") or item.get("id") or ""))
    roots.sort(key=stable_key)
    result: list[tuple[dict[str, Any], int]] = []
    visited: set[str] = set()
    starts = roots + sorted((item for item in by_id.values() if item not in roots), key=stable_key)
    for start in starts:
        stack = [(start, 0)]
        while stack and len(result) < _MAX_TREE_NODES:
            item, depth = stack.pop()
            item_id = str(item.get("id"))
            if item_id in visited:
                continue
            visited.add(item_id)
            result.append((item, min(depth, 64)))
            descendants = sorted(
                children.get(item_id, []),
                key=lambda row: (_epoch(row.get("created_at")) or 0, str(row.get("native_id") or "")),
                reverse=True,
            )
            stack.extend((child, depth + 1) for child in descendants)
    return result


def _short_age(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{days}d {hours}h" if days else (f"{hours}h {minutes}m" if hours else f"{minutes}m")


__all__ = ["build_agent_map", "format_agent_map", "ordered_agents"]
