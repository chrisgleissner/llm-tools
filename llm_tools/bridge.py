"""LLM Bridge CLI and command dispatch."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from . import bridge_format as fmt
from .bridge_format import NATIVE_STOP_HINT
from .bridge_store import BridgeStore, MissionLock, TransitionError, default_state_dir, derive_title, prune_old_drafts, rotation_label, store_path
from .bridge_store import controller_running as _controller_running
from .bridge_states import (
    ENDED_MISSION_STATUSES, FINISHED_MISSION_STATUSES, FINISHED_TASK_STATUSES, LIVE_MISSION_STATUSES,
    STARTED_MISSION_STATUSES,
)
from .bridge_states import MISSION_COUNT, MISSION_HUE_NAMES, THEME_CHOICES

if TYPE_CHECKING:
    import argparse

    from .bridge_tui import ControlViewAction
    from .terminal import TerminalAction

# Start-up keeps this module light: the CLI parser, theme, rotation checks, config and terminal backend are
# imported inside the commands that need them. These names stay readable as module attributes (and patchable
# through the module objects they return), resolved on first access.
_LAZY_MODULES = {
    "argparse": "argparse",
    "difflib": "difflib",
    "shutil": "shutil",
    "subprocess": "subprocess",
    "bridge_rotation": "llm_tools.bridge_rotation",
    "bridge_theme": "llm_tools.bridge_theme",
    "toolconfig": "llm_tools.config",
}
_LAZY_ATTRIBUTES = {
    "TerminalAction": ("llm_tools.terminal", "TerminalAction"),
    "python_module_argv": ("llm_tools.common", "python_module_argv"),
}


def __getattr__(name: str) -> Any:
    from importlib import import_module

    if name in _LAZY_MODULES:
        value: Any = import_module(_LAZY_MODULES[name])
    elif name in _LAZY_ATTRIBUTES:
        module_name, attribute = _LAZY_ATTRIBUTES[name]
        value = getattr(import_module(module_name), attribute)
    elif name == "_ConsoleParser":
        value = _console_parser_class()
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def state_dir() -> Path:
    return default_state_dir()


def open_store(*, readonly: bool = False) -> BridgeStore:
    """The mission store; ``readonly`` for read paths, which then take no write lock and write nothing."""
    return BridgeStore(store_path(), readonly=readonly)


def read_verbatim(path: str | Path) -> str:
    if str(path) == "-":
        return sys.stdin.read()
    with Path(path).expanduser().open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def controller_running(mission_id: str) -> bool:
    """Whether a live process holds this mission's controller lock; an unreadable lock raises."""
    return _controller_running(mission_id)


def status_snapshot(
    mission_id: str,
    *,
    task_limit: int | None = None,
    task_offset: int = 0,
    search: str = "",
    event_offset: int = 0,
    event_anchor: int | None = None,
    prompt_chars: int | None = None,
) -> dict[str, Any]:
    from .bridge_monitor import project_snapshot

    with open_store(readonly=True) as store:
        snapshot = project_snapshot(store, mission_id, task_limit=task_limit, task_offset=task_offset, search=search, event_offset=event_offset, event_anchor=event_anchor, prompt_chars=prompt_chars)
    lifecycle = snapshot.get("lifecycle")
    if isinstance(lifecycle, dict):
        lifecycle["controller_running"] = controller_running(mission_id)
    return snapshot


def console_snapshot(
    mission_id: str,
    *,
    task_limit: int = 200,
    task_offset: int = 0,
    search: str = "",
    event_offset: int = 0,
    event_anchor: int | None = None,
    view: str = "Bridge",
    prompt_chars: int | None = None,
) -> dict[str, Any]:
    """Bound the live control pane projection regardless of mission size."""
    extra = {} if prompt_chars is None else {"prompt_chars": prompt_chars}
    result = status_snapshot(mission_id, task_limit=task_limit, task_offset=task_offset, search=search, event_offset=event_offset, event_anchor=event_anchor, **extra)
    if view == "Usage":
        from .bridge_usage import embedded_usage
        result["usage"] = embedded_usage.snapshot()
    return result


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def strip_frozen_config(value: Any) -> Any:
    """A copy of ``value`` without any ``frozen_tool_config`` (the whole saved llm-tools config).

    Missions keep that text so a run uses the settings it was created with. It is
    large and of no use when reading a result, so CLI output leaves it out.
    """
    if isinstance(value, dict):
        return {key: strip_frozen_config(item) for key, item in value.items() if key != "frozen_tool_config"}
    if isinstance(value, list):
        return [strip_frozen_config(item) for item in value]
    return value


TASK_LIST_PAGE = 200
BUILTIN_PROVIDERS = "claude,codex,opencode"
MIN_MISSION_PREFIX = 4


class MissionNotFound(ValueError):
    """No saved mission matches the given ID or prefix."""


def _station_live(mission_id: str) -> bool:
    """Whether a Bridge-owned provider station is running for this mission."""
    from . import bridge_switcher

    return bool(bridge_switcher.default_station_probe(mission_id))


def _mission_live(mission_id: str) -> bool:
    return controller_running(mission_id) or _station_live(mission_id)


def order_missions(rows: list[Any]) -> list[Any]:
    """Missions with a controller or station first, then unfinished before finished, newest first.

    ``mission list``, ``missions`` and the ambiguity errors share this order.
    """
    newest = sorted(rows, key=lambda row: (str(row["updated_at"]), str(row["id"])), reverse=True)
    return sorted(newest, key=lambda row: (not _mission_live(str(row["id"])), str(row["status"]) in FINISHED_MISSION_STATUSES))


def _mission_choices(rows: list[Any]) -> str:
    return "\n".join(
        f"  {str(row['id'])[:8]}  {str(row['name'])[:40]}  {row['status']}" + ("  (live)" if _mission_live(str(row["id"])) else "")
        for row in order_missions(rows)
    )


def resolve_mission_ref(
    store: BridgeStore,
    ref: str | None,
    *,
    cwd: str | Path | None = None,
    read_only: bool = False,
    prefer_station: bool = False,
) -> str:
    """Return the full mission ID for an ID, a unique ID prefix, or the current folder.

    With no ``ref`` the single unfinished mission whose folder equals or
    contains the current directory is used. A live mission (created, running,
    paused or stopping) wins over stopped or failed ones in the same folder.
    ``prefer_station`` narrows several matches to the ones with a running
    station (attach and detach). ``read_only`` lets a folder whose only
    missions are finished resolve to the finished one, so ``status`` and
    ``task list`` still work after completion.
    """
    if ref:
        exact = store.db.execute("SELECT id FROM missions WHERE id=?", (ref,)).fetchone()
        if exact is not None:
            return str(exact["id"])
        if len(ref) < MIN_MISSION_PREFIX:
            raise ValueError(f"mission ID prefix {ref!r} is too short (use at least {MIN_MISSION_PREFIX} characters); run llm-bridge mission list")
        rows = store.db.execute("SELECT id,name,status,updated_at FROM missions WHERE substr(id,1,?)=?", (len(ref), ref)).fetchall()
        if not rows:
            raise MissionNotFound(f"no mission matches {ref!r}; run llm-bridge mission list")
        if len(rows) > 1:
            raise ValueError(f"mission ID prefix {ref!r} is ambiguous; matches:\n{_mission_choices(rows)}")
        return str(rows[0]["id"])
    here = Path(cwd if cwd is not None else Path.cwd()).expanduser().resolve()
    in_folder = [
        row
        for row in store.db.execute("SELECT id,name,status,cwd,updated_at FROM missions ORDER BY updated_at DESC,id")
        if here == Path(row["cwd"]) or Path(row["cwd"]) in here.parents
    ]
    candidates = [row for row in in_folder if row["status"] not in FINISHED_MISSION_STATUSES]
    live = [row for row in candidates if row["status"] in LIVE_MISSION_STATUSES]
    pool = live or candidates
    if prefer_station and len(pool) > 1:
        with_station = [row for row in pool if _station_live(str(row["id"]))]
        pool = with_station or pool
    if len(pool) == 1:
        return str(pool[0]["id"])
    if not pool:
        finished = [row for row in in_folder if row["status"] in FINISHED_MISSION_STATUSES]
        if read_only and len(finished) == 1:
            return str(finished[0]["id"])
        if read_only and finished:
            raise ValueError(f"several finished missions match folder {here}; pass --mission ID:\n{_mission_choices(finished)}")
        if finished:
            newest = order_missions(finished)[0]
            short = str(newest["id"])[:8]
            if newest["status"] == "COMPLETED":
                follow_up = f"add follow-up work with: llm-bridge task add --mission {short} --prompt \"...\" or create a new mission with: llm-bridge mission create --prompt \"...\""
            else:
                follow_up = "create a new mission with: llm-bridge mission create --prompt \"...\""
            raise ValueError(f"mission {short} in folder {here} is finished ({str(newest['status']).lower()}); {follow_up}")
        raise ValueError(f"no unfinished mission for folder {here}; pass --mission ID (see llm-bridge mission list) or create one with: llm-bridge mission create --prompt \"...\"")
    raise ValueError(f"several missions match folder {here}; pass --mission ID:\n{_mission_choices(pool)}")


def _is_read_only(args: argparse.Namespace) -> bool:
    """Commands that only read state may resolve a folder's finished mission."""
    command = getattr(args, "command", "")
    if command in {"status", "events", "doctor"}:
        return True
    if command == "task":
        return getattr(args, "task_action", "") in {"list", "show"}
    if command == "proposal":
        return getattr(args, "proposal_action", "") == "list"
    if command == "rotation":
        return not any(getattr(args, key, None) is not None for key in ("set_list", "routes", "providers"))
    return False


def _resolve_parsed_mission(args: argparse.Namespace, *, optional: bool = False, lenient: bool = False) -> None:
    """Replace ``args.mission`` with the full ID it refers to (prefix or folder).

    ``lenient`` keeps an unknown ID unchanged so the action that uses it
    reports the failure (the console builds terminal actions before any lookup).
    """
    if not hasattr(args, "mission"):
        return
    if args.mission is None and optional:
        return
    try:
        with open_store(readonly=True) as store:
            args.mission = resolve_mission_ref(
                store, args.mission,
                read_only=_is_read_only(args),
                prefer_station=getattr(args, "command", "") in {"attach", "detach"},
            )
    except MissionNotFound:
        if not lenient:
            raise


def _require_task(store: BridgeStore, mission_id: str, task_id: int) -> dict[str, Any]:
    """The task with this global ID, if it belongs to the mission (the advanced ``--task-id`` form)."""
    try:
        task = store.get_task(task_id)
    except KeyError:
        raise TransitionError(f"no task has the global ID {task_id}; use --task N with the number shown by task list") from None
    if task["mission_id"] != mission_id:
        raise TransitionError(f"global task ID {task_id} belongs to another mission, not {mission_id[:8]}; use --task N with the number shown by task list")
    return task


def _resolve_task(store: BridgeStore, mission_id: str, number: int | None, by_id: int | None) -> dict[str, Any]:
    """The task a command names: ``--task N`` is the mission's own number, ``--task-id`` the global ID."""
    if by_id is not None:
        return _require_task(store, mission_id, int(by_id))
    if number is None:
        raise ValueError("name the task with --task N (the number shown by task list)")
    return store.task_by_ordinal(mission_id, int(number))


def _dependency_ids(store: BridgeStore, mission_id: str, raw: str) -> list[int]:
    """Global IDs for a comma-separated list of the mission's task numbers."""
    ids: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if not item.isdecimal():
            raise ValueError(f"dependency {item!r} is not a task number; give task numbers such as 1,2")
        ids.append(int(store.task_by_ordinal(mission_id, int(item))["id"]))
    return ids


def _add_task_selector(parser: argparse.ArgumentParser, *, required: bool = True, noun: str = "task", note: str = "") -> None:
    """``--task N`` (the mission's own number) and the advanced ``--task-id`` (global ID)."""
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--task", metavar="N", dest="task", type=int, help=f"{noun} number within the mission, as shown by task list{note}")
    group.add_argument("--task-id", metavar="ID", dest="task_by_id", type=int, help=f"advanced: the {noun}'s global ID, unique across all missions (task JSON field id)")


def _apply_operator_command(store: BridgeStore, mission_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one validated command and persist its idempotent result."""
    command = store.enqueue_command(mission_id, payload)
    if command["status"] != "QUEUED":
        return command.get("result") or command
    try:
        with store._transaction():
            result = _apply_operator_effect(store, mission_id, payload)
            return store.finish_command(command["id"], status="APPLIED", result=result)
    except _RejectedOperatorCommand as exc:
        with store._transaction():
            return store.finish_command(command["id"], status="REJECTED", result={"error": str(exc)})


def _native_provider_running(store: BridgeStore, mission: dict[str, Any]) -> bool:
    config = mission.get("config") or {}
    if not config.get("native_tui") or config.get("headless"):
        return False
    row = store.db.execute(
        "SELECT 1 FROM operations WHERE mission_id=? AND status IN ('INTENT','PREPARED','RUNNING') LIMIT 1",
        (mission["id"],),
    ).fetchone()
    return row is not None


class _RejectedOperatorCommand(Exception):
    """A validation failure that must be acknowledged after rolling back effects."""


def _apply_operator_effect(store: BridgeStore, mission_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", ""))
    try:
        result: dict[str, Any]
        if action == "pause":
            current = str(store.get_mission(mission_id)["status"])
            if current not in STARTED_MISSION_STATUSES:
                return {"mission_status": current, "noop": True, "detail": fmt.nothing_to_pause(current)}
            store.set_mission_status(mission_id, "PAUSED")
            active = store.db.execute("SELECT COUNT(*) FROM operations WHERE mission_id=? AND status IN ('INTENT','PREPARED','RUNNING')", (mission_id,)).fetchone()[0]
            result = {"mission_status": "PAUSED", "work_status": "DEFERRED" if active else "APPLIED"}
            if active:
                result["detail"] = "pause takes effect when the active provider returns"
        elif action == "resume":
            store.set_mission_status(mission_id, "RUNNING")
            result = {"mission_status": "RUNNING"}
        elif action == "continue":
            result = store.release_review_hold(mission_id)
        elif action == "stop":
            mission = store.get_mission(mission_id)
            if mission["status"] in {"CREATED", "STOPPED"} or (mission["status"] == "PAUSED" and not controller_running(mission_id)):
                # Stop never cancels. With no controller there is nothing to stop;
                # only `cancel` ends a mission for good.
                return {"mission_status": mission["status"], "noop": True, "detail": fmt.nothing_to_stop(mission_id, mission["status"])}
            if mission["status"] in STARTED_MISSION_STATUSES:
                store.set_mission_status(mission_id, "STOPPING")
            mission = store.get_mission(mission_id)
            result = {"mission_status": mission["status"]}
            if mission["status"] == "STOPPING" and _native_provider_running(store, mission):
                # A native provider waits for operator input and never exits on
                # its own, so a plain stop would otherwise look hung.
                result["work_status"] = "DEFERRED"
                result["detail"] = NATIVE_STOP_HINT
        elif action == "cancel":
            active = store.db.execute("SELECT COUNT(*) FROM operations WHERE mission_id=? AND status IN ('INTENT','PREPARED','RUNNING')", (mission_id,)).fetchone()[0]
            mission = store.get_mission(mission_id)
            if mission["status"] not in FINISHED_MISSION_STATUSES:
                store.set_mission_status(mission_id, "CANCELLED")
            for task in store.list_tasks(mission_id):
                if task["status"] not in FINISHED_TASK_STATUSES:
                    store.set_task_status(int(task["id"]), "CANCELLED")
            result = {"mission_status": "CANCELLED", "work_status": "DEFERRED" if active else "APPLIED"}
            if active:
                result["detail"] = "cancel is durable; the active provider returns at its boundary"
        elif action == "task.verify":
            task_id = int(payload["task_id"])
            _require_task(store, mission_id, task_id)
            approved = bool(payload.get("approved", False))
            # Without --approve, evidence is recorded and the task keeps awaiting approval.
            result = store.verify_task(task_id, evidence=str(payload.get("evidence", "")), approved=approved, record_only=not approved)
        elif action == "task.reject":
            task_id = int(payload["task_id"])
            _require_task(store, mission_id, task_id)
            reason = str(payload.get("reason", ""))
            result = {**store.reject_task(task_id, reason), "rejection_reason": reason.strip()}
        elif action == "station.view":
            station = str(payload.get("station", "")).strip()
            if not station or len(station) > 100:
                raise ValueError("station name must be between 1 and 100 characters")
            mission = store.get_mission(mission_id)
            configured = {
                item.strip()
                for key in ("providers", "routes")
                for item in str(mission["config"].get(key, "")).split(",")
                if item.strip()
            }
            if configured and station not in configured:
                raise ValueError(f"station is not configured for this mission: {station}")
            result = store.set_mission_config_value(mission_id, "viewed_station", station, event_kind="station-viewed")
        elif action == "steer":
            kind = str(payload.get("kind", "")).strip().lower()
            if kind not in {"replace", "review", "verify", "pause-after-iteration", "stop-after-task", "handoff"}:
                raise ValueError("steering kind must be replace, review, verify, pause-after-iteration, stop-after-task, or handoff")
            task_id = int(payload["task_id"]) if payload.get("task_id") is not None else None
            if kind in {"replace", "review", "verify", "stop-after-task"} and task_id is None:
                raise ValueError(f"{kind} steering requires --task")
            task = _require_task(store, mission_id, task_id) if task_id is not None else None
            if kind == "replace":
                prompt = str(payload.get("prompt", ""))
                if not prompt:
                    raise ValueError("replace steering requires prompt text")
                revision = store.amend_task(task_id, prompt=prompt)
                applied = not store.get_task(task_id)["pending"]
                result = {"kind": kind, "task_id": task_id, "revision": revision, "status": "APPLIED" if applied else "QUEUED_FOR_BOUNDARY"}
            elif kind == "review":
                review_prompt = str(payload.get("prompt", "")).strip() or f"Review task {task['ordinal']} ({task['title']}) after it completes. Inspect its artifacts and acceptance criteria, then report findings and any required follow-up."
                review_task = store.add_task(
                    mission_id,
                    review_prompt,
                    title=f"Review: {task['title']}",
                    priority=int(task["priority"]) + 1,
                    depends_on=[task_id],
                    acceptance=["Record review findings and identify any unmet acceptance criteria."],
                )
                result = {"kind": kind, "task_id": review_task, "depends_on": task_id, "depends_on_ordinal": task["ordinal"], "status": "APPLIED"}
            elif kind == "verify":
                mission = store.get_mission(mission_id)
                verifier = mission["config"].get("verifier")
                if not isinstance(verifier, dict) or not verifier.get("argv"):
                    raise ValueError("verify steering requires a configured mission verifier")
                if task["status"] not in {"RUNNING", "VERIFYING"}:
                    raise TransitionError("verify steering requires a running or verifying task")
                requests = [int(value) for value in mission["config"].get("verification_requests", []) if int(value) != task_id]
                requests.append(task_id)
                store.set_mission_config_value(mission_id, "verification_requests", requests, event_kind="steering-verify-requested")
                result = {"kind": kind, "task_id": task_id, "status": "QUEUED_FOR_EXIT"}
            elif kind == "pause-after-iteration":
                store.set_mission_config_value(mission_id, "pause_after_iteration", True, event_kind="steering-pause-requested")
                result = {"kind": kind, "status": "QUEUED_FOR_BOUNDARY"}
            elif kind == "stop-after-task":
                store.set_mission_config_value(mission_id, "stop_after_task", task_id, event_kind="steering-stop-task-requested")
                result = {"kind": kind, "task_id": task_id, "status": "QUEUED_FOR_VERIFICATION"}
            else:
                station = str(payload.get("route", "")).strip()
                if not station:
                    raise ValueError("handoff steering requires --route")
                mission = store.get_mission(mission_id)
                configured = {item.strip() for key in ("providers", "routes") for item in str(mission["config"].get(key, "")).split(",") if item.strip()}
                if configured and station not in configured:
                    raise ValueError(f"handoff station is not configured for this mission: {station}")
                store.set_mission_config_value(mission_id, "handoff_preference", station, event_kind="steering-handoff-requested")
                result = {"kind": kind, "route": station, "status": "QUEUED_FOR_SELECTION"}
        elif action == "task.add":
            was_completed = store.get_mission(mission_id)["status"] == "COMPLETED"
            task_id = store.add_task(
                mission_id,
                str(payload["prompt"]),
                title=str(payload.get("title", "")),
                priority=int(payload.get("priority", 0)),
                order=int(payload["order"]) if payload.get("order") is not None else None,
                depends_on=[int(x) for x in payload.get("depends_on", [])],
                acceptance=[str(x) for x in payload.get("acceptance", [])],
            )
            result = store.get_task(task_id)
            if was_completed:
                result["mission_reopened"] = True
        elif action == "task.propose":
            task_id = store.propose_task(
                mission_id,
                str(payload.get("prompt", "")),
                source_operation_id=str(payload.get("source_operation_id", "")),
                title=str(payload.get("title", "")),
                priority=int(payload.get("priority", 0)),
                depends_on=[int(value) for value in payload.get("depends_on", [])],
                acceptance=[str(value) for value in payload.get("acceptance", [])],
            )
            result = store.get_task(task_id)
        elif action == "task.proposal-decision":
            task_id = int(payload["task_id"])
            _require_task(store, mission_id, task_id)
            result = store.decide_proposal(
                mission_id,
                task_id,
                accept=bool(payload.get("accept", False)),
                reason=str(payload.get("reason", "")),
            )
        elif action == "task.amend":
            task_id = int(payload["task_id"])
            _require_task(store, mission_id, task_id)
            revision = store.amend_task(task_id, prompt=str(payload["prompt"]))
            result = {"task_id": task_id, "revision": revision, "applied": not store.get_task(task_id)["pending"]}
        elif action in {"task.pause", "task.resume", "task.cancel", "task.retry", "task.priority", "task.reorder", "task.dependency"}:
            task_id = int(payload["task_id"])
            _require_task(store, mission_id, task_id)
            if action == "task.pause":
                result = store.set_task_status(task_id, "PAUSED")
                active = store.db.execute("SELECT COUNT(*) FROM operations WHERE task_id=? AND status IN ('INTENT','PREPARED','RUNNING')", (task_id,)).fetchone()[0]
                if active:
                    result["work_status"] = "DEFERRED"
                    result["detail"] = "pause takes effect when the active provider returns"
            elif action == "task.resume":
                result = store.set_task_status(task_id, "QUEUED")
                store.ready_tasks(mission_id, limit=0)  # promote runnable tasks; the list is not needed
                result = store.get_task(task_id)
            elif action == "task.cancel":
                result = store.set_task_status(task_id, "CANCELLED")
                active = store.db.execute(
                    "SELECT id FROM operations WHERE task_id=? AND status IN ('INTENT','PREPARED','RUNNING') LIMIT 1",
                    (task_id,),
                ).fetchone()
                if active:
                    result["work_status"] = "DEFERRED"
                    result["active_operation_id"] = str(active["id"])
                    result["detail"] = "task cancellation is durable; the active provider returns at its boundary"
                else:
                    result["work_status"] = "APPLIED"
            elif action == "task.retry":
                result = store.set_task_status(task_id, "QUEUED")
                store.ready_tasks(mission_id, limit=0)  # promote runnable tasks; the list is not needed
                result = store.get_task(task_id)
            elif action == "task.priority":
                store.set_task_priority(task_id, int(payload["priority"]))
                result = store.get_task(task_id)
            elif action == "task.reorder":
                store.reorder_task(task_id, int(payload["order"]))
                result = store.get_task(task_id)
            else:
                dependency_id = int(payload["depends_on"])
                _require_task(store, mission_id, dependency_id)
                store.add_dependency(mission_id, task_id, dependency_id)
                result = store.get_task(task_id)
        else:
            raise ValueError(f"unsupported Bridge command: {action}")
        if "ordinal" not in result and result.get("task_id") is not None:
            # Receipts show the mission's own task number, not the global ID.
            row = store.db.execute("SELECT ordinal FROM tasks WHERE id=?", (int(result["task_id"]),)).fetchone()
            if row is not None:
                result["task_ordinal"] = int(row[0])
        return result
    except (ValueError, KeyError, OSError, TransitionError) as exc:
        raise _RejectedOperatorCommand(str(exc)) from exc


def _task_result(args: argparse.Namespace, *, preserve_receipt: bool = False) -> Any:
    """Shared standalone/interactive task parser result and durable handler."""
    with open_store(readonly=args.task_action in {"list", "show"}) as store:
        if args.task_action == "add":
            prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
            dep_ids = _dependency_ids(store, args.mission, args.depends_on)
            command_result = _apply_operator_command(store, args.mission, {"action": "task.add", "prompt": prompt, "title": args.title, "priority": args.priority, "order": args.order, "depends_on": dep_ids, "acceptance": args.acceptance})
            result = command_result if preserve_receipt or command_result.get("status") == "REJECTED" else (command_result.get("result") or command_result)
        elif args.task_action == "list":
            if getattr(args, "all", False):
                result = fmt.TaskPage(store.list_tasks(args.mission, offset=max(0, args.offset)))
            else:
                result = fmt.TaskPage(store.list_tasks(args.mission, limit=max(0, args.limit), offset=max(0, args.offset)))
            result.total = store.task_counters(args.mission)["total"]
            result.offset = max(0, args.offset)
        elif args.task_action == "show":
            result = _resolve_task(store, args.mission, args.task, args.task_by_id)
        else:
            task = _resolve_task(store, args.mission, args.task, args.task_by_id)
            payload: dict[str, Any] = {"task_id": int(task["id"])}
            if args.task_action == "amend":
                prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
                payload.update(action="task.amend", prompt=prompt)
            elif args.task_action == "verify":
                payload.update(action="task.verify", evidence=args.evidence, approved=args.approve)
            elif args.task_action == "reject":
                payload.update(action="task.reject", reason=args.reason)
            elif args.task_action == "priority":
                payload.update(action="task.priority", priority=args.value)
            elif args.task_action == "reorder":
                payload.update(action="task.reorder", order=args.value)
            elif args.task_action == "dependencies":
                payload.update(action="task.dependency", depends_on=int(store.task_by_ordinal(args.mission, args.add)["id"]))
            else:
                payload["action"] = "task." + args.task_action
            result = _apply_operator_command(store, args.mission, payload)
    return result


_CONSOLE_ONLY_COMMANDS = ("approve", "reject", "tasks", "orders", "proposals", "quit", "exit")


def _console_parser_class() -> type[argparse.ArgumentParser]:
    """The console's parser class, created on first use so ``--version`` and similar never import argparse."""
    existing = globals().get("_ConsoleParser")
    if existing is not None:
        return existing
    import argparse
    import difflib

    class _ConsoleParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            unknown = re.search(r"invalid choice: '([^']*)'", message)
            if unknown:
                word = unknown.group(1)
                top_level = " " not in self.prog
                names = [
                    name
                    for action in self._actions
                    if isinstance(action, argparse._SubParsersAction)
                    for name in action.choices
                    if not name.startswith("_") and name != "console"
                ]
                if top_level:
                    names.extend(name for name in _CONSOLE_ONLY_COMMANDS if name not in names)
                close = difflib.get_close_matches(word, names, n=3, cutoff=0.5)
                if word:
                    close += [name for name in names if name.startswith(word) and name not in close]
                kind = "command" if top_level else f"{self.prog.split()[-1]} command"
                hint = f" · did you mean {', '.join(close[:3])}?" if close else ""
                raise ValueError(f"Unknown {kind} '{word}'{hint} · /help lists commands")
            raise ValueError(f"{message}. Use {self.prog} --help for the required arguments.")

    globals()["_ConsoleParser"] = _ConsoleParser
    return _ConsoleParser


def _verifying_task_ids(mission_id: str) -> list[int]:
    with open_store(readonly=True) as store:
        rows = store.db.execute("SELECT ordinal FROM tasks WHERE mission_id=? AND status='VERIFYING' ORDER BY ordering,id", (mission_id,))
        return [int(row[0]) for row in rows]


_STEER_KINDS = ("replace", "review", "verify", "pause-after-iteration", "stop-after-task", "handoff")
_STEER_TEXT_KINDS = frozenset({"replace", "review"})
_STEER_TASK_KINDS = frozenset({"replace", "review", "verify", "stop-after-task"})


def console_words(command_line: str) -> list[str]:
    """Split a console line into words, taking free text at the end literally.

    The reason of ``reject``, the evidence of ``verify N`` and the text of
    ``steer`` and ``proposal reject`` are prose: an apostrophe or a stray quote
    in it must not be read as shell quoting. Everything else is split like a
    shell command line.
    """
    tokens = command_line.split(None, 4)
    head = tokens[0] if tokens else ""
    number = len(tokens) > 1 and tokens[1].isdecimal()
    if head == "reject" and len(tokens) > 1:
        return [item.strip() for item in command_line.split(None, 2 if number else 1)]
    if head == "verify" and number and len(tokens) > 2 and not tokens[2].startswith("-"):
        return [item.strip() for item in command_line.split(None, 2)]
    if head == "proposal" and len(tokens) > 3 and tokens[1] == "reject" and tokens[2].isdecimal() and not tokens[3].startswith("-"):
        return [item.strip() for item in command_line.split(None, 3)]
    if head == "steer" and len(tokens) > 1 and tokens[1] in _STEER_KINDS:
        kind = tokens[1]
        words = ["steer", "--kind", kind]
        rest = command_line.split(None, 2)[2] if len(tokens) > 2 else ""
        if kind == "handoff":
            if rest:
                words += ["--route", rest.split(None, 1)[0]]
        elif kind in _STEER_TASK_KINDS and rest.split(None, 1)[:1] and rest.split(None, 1)[0].isdecimal():
            first, *remainder = rest.split(None, 1)
            words += ["--task", first]
            rest = remainder[0].strip() if remainder else ""
        if kind in _STEER_TEXT_KINDS and rest.strip():
            words.append("--prompt=" + rest.strip())
        return words
    return shlex.split(command_line)


def expand_reject_alias(words: list[str], waiting: Callable[[], list[int]]) -> tuple[list[str] | None, str | None]:
    """Expand ``reject [N] REASON...`` to ``task reject --task N --reason REASON``.

    A leading number is the task. Without one, the single task awaiting
    verification is used. Returns ``(words, None)`` or ``(None, error)``.
    """
    rest = words[1:]
    if not rest:
        return None, "error: usage: reject [TASK-NUMBER] REASON"
    if rest[0].isdecimal():
        task_id, rest = rest[0], rest[1:]
    else:
        ids = waiting()
        if len(ids) != 1:
            hint = f"tasks awaiting approval: {', '.join(map(str, ids))}" if ids else "no task awaits approval"
            return None, f"error: reject needs a task number · {hint}"
        task_id = str(ids[0])
    reason = " ".join(rest).strip()
    if not reason:
        return None, "error: usage: reject [TASK-NUMBER] REASON"
    return ["task", "reject", "--task", task_id, f"--reason={reason}"], None


# Console commands that only read state and print text meant for people.
# The control pane's own readers (opening a task for editing) pass --json.
_CONSOLE_HUMAN_COMMANDS = frozenset({("status",), ("events",), ("theme",), ("doctor",), ("rotation",), ("missions",), ("mission", "list"), ("proposal", "list"), ("task", "list"), ("task", "show")})


def _console_human(words: list[str]) -> bool:
    if "--help" in words or "-h" in words:
        return False
    if words[0] == "rotation" and any(flag in words for flag in ("--set", "--routes", "--providers")):
        return False
    return (words[0],) in _CONSOLE_HUMAN_COMMANDS or tuple(words[:2]) in _CONSOLE_HUMAN_COMMANDS


def _without_frozen_config(text: str) -> str:
    """JSON text without the saved llm-tools config; any other text unchanged."""
    if not text.startswith(("{", "[")):
        return text
    try:
        return json.dumps(strip_frozen_config(json.loads(text)), ensure_ascii=False, indent=2, sort_keys=True)
    except ValueError:
        return text


def dispatch_console_command(mission_id: str, command_line: str) -> str | TerminalAction | ControlViewAction:
    """Run the public CLI grammar in this mission's shell-free REPL context."""
    import subprocess

    from .common import python_module_argv
    from .terminal import TerminalAction

    try:
        stripped = command_line.strip()
        if stripped.startswith("/"):
            # Slash commands, as in Codex and Claude Code: /pause, /help keys.
            command_line = stripped[1:].strip() or "help"
        words = console_words(command_line)
        if not words:
            return ""
        command = words[0]
        typed_json = "--json" in words
        if typed_json:
            words = [word for word in words if word != "--json"]  # re-added once the aliases are expanded
            if not words:
                return ""
        words_before_alias = list(words)
        if command in {"quit", "exit"}:
            return "press Ctrl-D to leave controls · the mission keeps running"
        if command in {"help", "?"}:
            from .bridge_help import topic_text

            if len(words) > 1:
                from .bridge_help import is_known_topic

                if not is_known_topic(" ".join(words[1:])):
                    return "error: " + topic_text(" ".join(words[1:])).splitlines()[0]
                if words[1] in {"task", "mission", "missions", "start", "attach", "pause", "resume", "continue", "stop", "cancel", "verify", "finish"}:
                    parser = _build_parser(parser_class=_console_parser_class(), default_mission=mission_id)
                    output = io.StringIO()
                    try:
                        with redirect_stdout(output):
                            parser.parse_args([*words[1:], "--help"])
                    except SystemExit:
                        pass
                    return output.getvalue() or topic_text(" ".join(words[1:]))
                return topic_text(" ".join(words[1:]))
            return topic_text("quickstart") + "\n\n" + _build_parser().format_help()

        if command == "missions" and len(words) == 1:
            return TerminalAction(mission_id, None, "pick")
        if command == "finish" and "--help" not in words and "-h" not in words:
            from .bridge_finish import FinishError, finish_mission

            args = _build_parser(parser_class=_console_parser_class(), default_mission=mission_id).parse_args(words)
            _resolve_parsed_mission(args)
            try:
                finished = finish_mission(args.mission)
            except FinishError as exc:
                return f"error: {exc}"
            return json.dumps({"status": "APPLIED", "result": {"detail": finished.detail, "mission_id": args.mission}})

        if command in {"control", "usage"} and "--help" not in words and "-h" not in words:
            from .bridge_tui import ControlViewAction

            parser = _build_parser(parser_class=_console_parser_class(), default_mission=mission_id)
            args = parser.parse_args(words)
            _resolve_parsed_mission(args, lenient=True)
            return ControlViewAction(args.mission, "Usage" if command == "usage" else "Bridge")

        # Keep the old compact commands while routing every action through the
        # exact same parser and durable command handlers as the standalone CLI.
        if command in {"attach", "detach"}:
            if "--help" in words or "-h" in words:
                return f"Usage: {command} [--mission ID] [--client /dev/pts/N]"
            args = _build_parser(parser_class=_console_parser_class(), default_mission=mission_id).parse_args(words)
            _resolve_parsed_mission(args, lenient=True)
            return TerminalAction(str(args.mission), getattr(args, "client", None), command)
        if command in {"tasks", "orders"}:
            words = ["task", "list"]
        elif command == "proposals":
            words = ["proposal", "list", *( ["--status", words[1].upper()] if len(words) > 1 else [] )]
        elif (
            command == "proposal"
            and len(words) >= 3
            and words[1] in {"accept", "reject"}
            and not words[2].startswith("-")
        ):
            words = ["proposal", words[1], "--task", words[2]]
            if len(words_before_alias) > 3:
                words.append("--reason=" + " ".join(words_before_alias[3:]))
        elif command == "add" and len(words) == 2:
            words = ["task", "add", "--file", words[1]]
        elif command == "amend" and len(words) == 3:
            words = ["task", "amend", "--task", words[1], "--file", words[2]]
        elif command in {"priority", "reorder"} and len(words) == 3:
            words = ["task", command, "--task", words[1], "--value", words[2]]
        elif command == "dependencies" and len(words) == 3:
            words = ["task", "dependencies", "--task", words[1], "--add", words[2]]
        elif command == "approve":
            if len(words) > 2 or (len(words) == 2 and not words[1].isdecimal()):
                return "error: usage: approve [TASK-NUMBER]"
            if len(words) == 2:
                approve_task = words[1]
            else:
                waiting = _verifying_task_ids(mission_id)
                if len(waiting) != 1:
                    hint = f"tasks awaiting approval: {', '.join(map(str, waiting))}" if waiting else "no task awaits approval"
                    return f"error: approve needs a task number · {hint}"
                approve_task = str(waiting[0])
            words = ["task", "verify", "--task", approve_task, "--approve"]
        elif command == "reject":
            expanded, problem = expand_reject_alias(words, lambda: _verifying_task_ids(mission_id))
            if expanded is None:
                return str(problem)
            words = expanded
        elif command == "verify" and len(words) == 3 and words[1].isdecimal() and not words[2].startswith("-"):
            words = ["task", "verify", "--task", words[1], "--evidence=" + words[2]]
        elif command in {"retry", "verify"} and len(words) >= 2:
            words = ["task", command, "--task", words[1], *words[2:]]
        elif command in {"pause", "resume", "cancel"} and len(words) == 2 and words[1].isdecimal():
            words = ["task", command, "--task", words[1]]
        elif command == "verify" and len(words) >= 2:
            words = ["task", "verify", "--task", words[1], *words[2:]]
        elif command == "propose" and len(words) == 3:
            words = ["proposal", "create", "--operation", words[1], "--file", words[2]]
        elif command == "events" and len(words) == 2 and words[1].isdecimal():
            words = ["events", "--after", words[1]]

        if words[0] in {"console", "_controller"}:
            return "error: this REPL cannot recursively start a console/controller"
        # The public parser is the REPL grammar. JSON mode gives the control
        # pane a stable response string; console/controller recursion is rejected.
        # Read-only commands answer with the same text as the shell CLI unless
        # the operator asked for --json; commands that change state keep the
        # JSON receipt the control pane turns into a one-line acknowledgement.
        human = not typed_json and _console_human(words)
        if "--help" not in words and "-h" not in words and words[0] not in {"control", "console", "_controller"} and "--json" not in words and not human:
            words.append("--json")
        args = None
        if "--help" not in words and "-h" not in words:
            args = _build_parser(parser_class=_console_parser_class(), default_mission=mission_id).parse_args(words)
            _resolve_parsed_mission(args)
            if getattr(args, "prompt_file", None) == "-":
                return "error: press Ctrl-E with these options to enter multiline text; the console cannot read its own terminal as a pipe"
        if words[0] == "task" and args is not None:
            result = _task_result(args, preserve_receipt=True)
            if isinstance(result, dict) and result.get("status") == "REJECTED":
                return "error: " + str((result.get("result") or {}).get("error", "command rejected"))
            if human:
                return fmt.format_task_detail(result) if words[:2] == ["task", "show"] and isinstance(result, dict) else fmt.format_task_list(result)
            return json.dumps(result, ensure_ascii=False, indent=2)
        if args is not None and getattr(args, "mission", None):
            words.extend(["--mission", args.mission])
        rotation_before = ""
        if words[0] == "start" and args is not None:
            with open_store() as store:
                saved = store.get_mission(args.mission)["config"]
            rotation_before = rotation_label(saved.get("providers"), saved.get("routes"))
        env = os.environ.copy()
        env.pop(MANAGED_ENV, None)  # the console is the operator, whatever process started it
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, env.get("PYTHONPATH", ""))))
        result = subprocess.run(
            python_module_argv("llm_tools.bridge", *words),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            check=False,
            timeout=15,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
            return f"error: {detail}"
        if words[0] == "start" and args is not None:
            with open_store() as store:
                mission = store.get_mission(args.mission)
            status = str(mission["status"]).capitalize()
            config = mission["config"]
            headless = bool(config.get("headless"))
            if config.get("native_tui") and not headless and mission["status"] in STARTED_MISSION_STATUSES:
                # A native mission: take the operator to the provider, as the home screen does.
                return TerminalAction(str(args.mission), None, "attach")
            detail = f"{status} · use attach to open the provider" if not headless else f"{status} · headless work; inspect status"
            rotation_after = rotation_label(config.get("providers"), config.get("routes"))
            if rotation_before and rotation_after != rotation_before:
                detail = f"{fmt.format_rotation_change(rotation_before, rotation_after)} · {detail}"
            return json.dumps({"status": "APPLIED", "result": {"detail": detail, "mission_id": args.mission}})
        if result.stdout.strip():
            return _without_frozen_config(result.stdout.strip())
        if result.stderr.strip():
            return result.stderr.strip()
        return f"command exited with status {result.returncode}"
    except subprocess.TimeoutExpired:
        return "error: command acknowledgement timed out; outcome unknown. Check `llm-bridge status` before retrying."
    except (ValueError, KeyError, OSError, TransitionError) as exc:
        return f"error: {exc}"


def default_rotation() -> tuple[str, str]:
    """Return the effective default (providers, routes) for new missions.

    The user's llm-tools config decides when it names a rotation
    (``[ralph]`` wins over ``[defaults]``); otherwise the built-in providers
    apply and no routes are used.
    """
    import tomllib

    from . import config as toolconfig

    providers, routes = BUILTIN_PROVIDERS, ""
    try:
        parsed = tomllib.loads(toolconfig.config_path().read_text(encoding="utf-8"))
        merged = toolconfig.merged_tool_config(parsed, "ralph")
    except (OSError, ValueError, TypeError, AttributeError):
        return providers, routes
    from .bridge_monitor import name_list

    configured_providers = name_list(merged.get("providers"))
    configured_routes = name_list(merged.get("routes"))
    return ",".join(configured_providers) or providers, ",".join(configured_routes)


def resolve_rotation(providers: str | None, routes: str | None) -> tuple[str, str]:
    """Resolve an explicit ``--providers`` / ``--routes`` pair against the config defaults.

    ``None`` means the option was not given. An explicit ``--routes`` wins; an
    explicit ``--providers`` alone clears the configured routes so the providers
    are what actually runs; with neither the configured rotation applies.
    """
    default_providers, default_routes = default_rotation()
    if routes is not None:
        return providers if providers is not None else default_providers, routes
    if providers is not None:
        return providers, ""
    return default_providers, default_routes


def _config_label() -> str:
    from . import config as toolconfig

    return fmt.home_relative(toolconfig.config_path())


def creation_rotation(providers: str | None, routes: str | None) -> tuple[str, str]:
    """Resolve a new mission's rotation and refuse one that can never launch.

    The current llm-tools config decides which providers are enabled. A rotation
    the operator typed is refused when any entry is disabled; the default
    rotation just loses its disabled entries and is refused only when none are left.
    """
    from . import bridge_rotation
    from . import config as toolconfig

    explicit = providers is not None or routes is not None
    resolved_providers, resolved_routes = resolve_rotation(providers, routes)
    enabled = bridge_rotation.current_enabled()
    conf = toolconfig.load_config() if resolved_routes and enabled.names is not None else {}
    problem = bridge_rotation.check_rotation(enabled, conf, resolved_providers, resolved_routes, config_label=_config_label(), explicit=explicit)
    if problem:
        raise ValueError(problem)
    if not explicit:
        return bridge_rotation.usable_entries(enabled, conf, resolved_providers, resolved_routes)
    return resolved_providers, resolved_routes


def require_launchable_rotation(config: dict[str, Any], providers: Any, routes: Any, *, explicit: bool) -> None:
    """Refuse a start, resume or rotation change whose entries the mission's frozen config disables."""
    from . import bridge_rotation

    conf = bridge_rotation.parse_frozen(config.get("frozen_tool_config"))
    enabled = bridge_rotation.frozen_enabled(conf)
    problem = bridge_rotation.check_rotation(enabled, conf, providers, routes, config_label=_config_label(), explicit=explicit)
    if problem:
        raise ValueError(problem)


def _read_prompt_args(parser: argparse.ArgumentParser, noun: str = "task") -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", "--prompt-file", dest="prompt_file", metavar="PATH", help=f"read the {noun} prompt from PATH; '-' reads standard input")
    group.add_argument("--prompt", help=f"the {noun} prompt as literal text; use --file for long prompts")


def _add_mission_arg(parser: argparse.ArgumentParser, default_mission: str | None, *, required: bool = False, inferred: bool = True) -> None:
    help_text = "mission ID or a unique prefix of 4+ characters"
    if inferred:
        help_text += "; omit it inside a mission's folder to use that mission"
    parser.add_argument("--mission", metavar="ID", required=required and default_mission is None, default=default_mission, help=help_text)


def _add_json_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the stable machine-readable JSON response instead of text")


def _verifier_config(args: argparse.Namespace) -> dict[str, Any] | None:
    argv = list(getattr(args, "verifier_argv", []) or [])
    if not argv:
        return None
    timeout = float(getattr(args, "verifier_timeout", 300))
    if timeout <= 0 or timeout > 3600:
        raise ValueError("verifier timeout must be between 0 and 3600 seconds")
    if any("\x00" in arg for arg in argv):
        raise ValueError("verifier argv contains a NUL byte")
    from .bridge_verifier_guard import verifier_fingerprints

    files = [str(item) for item in getattr(args, "verifier_files", []) or []]
    config: dict[str, Any] = {"argv": argv, "timeout_seconds": timeout}
    fingerprints = verifier_fingerprints(argv, getattr(args, "cwd", None) or Path.cwd(), files)
    if fingerprints:
        config["fingerprints"] = fingerprints
    return config


def _log_retention_config(args: argparse.Namespace) -> dict[str, int]:
    max_bytes = int(getattr(args, "log_max_bytes", 4 * 1024 * 1024))
    rotations = int(getattr(args, "log_rotations", 3))
    retained_runs = int(getattr(args, "log_retained_runs", 20))
    refresh_seconds = int(getattr(args, "capacity_refresh_seconds", 60))
    if max_bytes < 1024 or max_bytes > 1024 * 1024 * 1024:
        raise ValueError("log max bytes must be between 1024 and 1073741824")
    if rotations < 0 or rotations > 20:
        raise ValueError("log rotations must be between 0 and 20")
    if retained_runs < 1 or retained_runs > 1000:
        raise ValueError("retained run count must be between 1 and 1000")
    if refresh_seconds < 5 or refresh_seconds > 3600:
        raise ValueError("capacity refresh seconds must be between 5 and 3600")
    return {"log_max_bytes": max_bytes, "log_rotations": rotations, "log_retained_runs": retained_runs, "capacity_refresh_seconds": refresh_seconds}


def _frozen_tool_config() -> str:
    from . import config as toolconfig

    path = toolconfig.config_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


class _RotationDefaultText:
    """Help-text fragment naming the configured default rotation, read only when help is formatted.

    Reading the config file at parser construction would load the config and provider modules for every
    command; argparse converts a ``%(name)s`` value with ``str()`` at ``--help`` time instead.
    """

    def __init__(self, render: Callable[[str, str], str]) -> None:
        self._render = render

    def __str__(self) -> str:
        return self._render(*default_rotation())


def _add_mission_create_args(create: argparse.ArgumentParser, *, legacy: bool = False) -> None:
    """Options shared by ``mission create`` and the legacy bare ``--prompt`` form."""
    create.add_argument("--name", metavar="NAME", help="mission name shown in lists (default: first line of the prompt)")
    create.add_argument("--cwd", metavar="DIR", default=str(Path.cwd()), help="folder the providers work in (default: the current folder)")
    # argparse validates a help string when the option is added, so the lazy placeholders are set afterwards.
    providers_help = "comma-separated providers to rotate through, in order; an explicit list without --routes disables configured routes"
    providers_action = create.add_argument("--providers", metavar="LIST", default=None, help=providers_help)
    providers_action.help = providers_help + " (default: %(rotation_providers)s)"
    providers_action.rotation_providers = _RotationDefaultText(lambda providers, routes: providers)  # type: ignore[attr-defined]
    routes_help = "comma-separated route IDs from the llm-tools config to rotate through instead of providers; wins over --providers"
    routes_action = create.add_argument("--routes", metavar="LIST", default=None, help=routes_help)
    routes_action.help = routes_help + "%(rotation_routes)s"
    routes_action.rotation_routes = _RotationDefaultText(lambda providers, routes: f" (default: {routes})" if routes else "")  # type: ignore[attr-defined]
    create.add_argument("--acceptance", metavar="TEXT", action="append", default=[], help="acceptance criterion for the first task; repeat for several")
    create.add_argument("--headless", action="store_true", help="Headless mode: the provider runs without a terminal and there is no station, so nothing to attach to; without --headless or --native the mission runs in Background mode (a station shows the activity log)")
    create.add_argument("--native", dest="native_tui", action="store_true", help="Native mode: the provider's own interactive terminal runs in a station; open it with attach")
    create.add_argument("--verifier-arg", dest="verifier_argv", metavar="ARG", action="append", default=[], help="one argument of the command that checks a finished task; repeat per argument; supports {context}, {cwd}, {mission_id}, {task_id}, {revision}")
    create.add_argument("--verifier-file", dest="verifier_files", metavar="PATH", action="append", default=[], help="a file the verifier depends on, relative to --cwd; repeat per file; if it changes after the mission is created a passing verifier no longer completes a task without your approval (the verifier program and the script it interprets are tracked automatically)")
    create.add_argument("--verifier-timeout", metavar="SECONDS", type=float, default=300, help="seconds before a verifier run is treated as failed (default: 300)")
    create.add_argument("--log-max-bytes", metavar="BYTES", type=int, default=4 * 1024 * 1024, help="size at which one provider log file rotates (default: 4194304, 4 MiB)")
    create.add_argument("--log-rotations", metavar="COUNT", type=int, default=3, help="rotated log files kept per provider run (default: 3)")
    create.add_argument("--log-retained-runs", metavar="COUNT", type=int, default=20, help="newest provider-run log directories kept; older ones are pruned (default: 20)")
    create.add_argument("--capacity-refresh-seconds", metavar="SECONDS", type=int, default=60, help="seconds between background provider-capacity observations (default: 60)")
    create.add_argument("--auto-continue", action="store_true", help="native missions: start the next task as soon as a provider run ends, instead of holding until you approve or reject the reviewed task (background and headless missions never hold)")
    create.add_argument("--trust-folder", action="store_true", help="answer the provider's workspace-trust dialog for the mission folder automatically in Headless and Background modes; without it a run that stops at that dialog is aborted (trust the folder in the provider CLI once instead)")
    create.add_argument("--command-template", metavar="ARGV", help="launch command template for tests or custom providers, in Ralph/scheduler syntax")
    create.add_argument("--include-config", action="store_true", help="with --json, include the mission's saved copy of the llm-tools config (large; left out by default)")
    if legacy:
        create.add_argument("--no-start", action="store_true", help="save the mission without starting it")


class _SkippedParser:
    """Stand-in for a command's parser when only another command is being parsed; every call is ignored."""

    def __getattr__(self, name: str) -> "_SkippedParser":
        return self

    def __call__(self, *args: Any, **kwargs: Any) -> "_SkippedParser":
        return self


class _OnlyCommand:
    """The top-level sub-parser group, building the parser of one command and skipping the rest.

    Building all fifty command parsers costs more than running a short command, so the
    normal path builds just the requested one (see :func:`_parse_arguments`).
    """

    def __init__(self, group: Any, command: str) -> None:
        self._group = group
        self._command = command

    def add_parser(self, name: str, **kwargs: Any) -> Any:
        if name == self._command or self._command in kwargs.get("aliases", ()):
            return self._group.add_parser(name, **kwargs)
        return _SkippedParser()


def _build_parser(
    *,
    parser_class: type[argparse.ArgumentParser] | None = None,
    default_mission: str | None = None,
    only: str | None = None,
) -> argparse.ArgumentParser:
    """The full command grammar, or with ``only`` just that command's (for parsing, not for help)."""
    import argparse

    if parser_class is None:
        parser_class = argparse.ArgumentParser
    parser = parser_class(prog="llm-bridge", description="Terminal command bridge for persistent LLM engineering missions")
    sub: Any = parser.add_subparsers(dest="command", required=True)
    if only is not None:
        sub = _OnlyCommand(sub, only)
    help_parser = sub.add_parser("help", help="show offline Bridge workflow and recovery guidance")
    help_parser.add_argument("topic", nargs="*", help="quickstart, keys, prompts, markdown, providers, states, verification, recovery, or a command path")
    view = sub.add_parser("view", help="read a local text artifact without starting work")
    view.add_argument("--file", required=True, metavar="PATH", help="UTF-8 text artifact to open; the source stays unchanged")
    view.add_argument("--follow", action="store_true", help="follow changes explicitly; Up pauses, End resumes")

    mission = sub.add_parser("mission", help="create or list missions")
    mission_sub = mission.add_subparsers(dest="mission_action", required=True)
    create = mission_sub.add_parser("create", help="create a mission and its first task")
    _read_prompt_args(create, "mission objective and first task")
    _add_mission_create_args(create)
    _add_json_arg(create)
    listing = mission_sub.add_parser("list")
    listing.add_argument("--include-config", action="store_true", help="with --json, include each mission's saved copy of the llm-tools config (large; left out by default)")
    _add_json_arg(listing)
    mission_set = mission_sub.add_parser("set", help="change a mission setting")
    _add_mission_arg(mission_set, default_mission)
    mission_set.add_argument("--auto-continue", dest="auto_continue", choices=("on", "off"), required=True, help="on: start the next task without waiting for review of the previous one; off: hold until you approve, reject or continue (the default for native missions)")
    _add_json_arg(mission_set)
    colour = mission_sub.add_parser("colour", aliases=["color"], help="pin the mission's identity colour (1-8) or return to the automatic one")
    _add_mission_arg(colour, default_mission)
    colour.add_argument("value", metavar="N|auto", help=f"colour number 1-{MISSION_COUNT} ({', '.join(f'{n} {name}' for n, name in enumerate(MISSION_HUE_NAMES, 1))}) or auto")
    _add_json_arg(colour)

    status = sub.add_parser("status")
    _add_mission_arg(status, default_mission)
    _add_json_arg(status)

    tasks = sub.add_parser("task")
    task_sub = tasks.add_subparsers(dest="task_action", required=True)
    add = task_sub.add_parser("add")
    _add_mission_arg(add, default_mission)
    _read_prompt_args(add)
    add.add_argument("--title", metavar="TEXT", default="", help="short task title shown in lists (default: first line of the prompt)")
    add.add_argument("--priority", metavar="N", type=int, default=0, help="scheduling priority; higher runs first (default: 0)")
    add.add_argument("--order", metavar="N", type=int, help="ordering value among equal priorities; lower runs first (default: appended last)")
    add.add_argument("--depends-on", metavar="IDS", default="", help="comma-separated task numbers that must complete first")
    add.add_argument("--acceptance", metavar="TEXT", action="append", default=[], help="acceptance criterion for the task; repeat for several")
    _add_json_arg(add)
    task_list = task_sub.add_parser("list")
    _add_mission_arg(task_list, default_mission)
    task_list.add_argument("--all", action="store_true", help=f"list every task, however many (default: the first {TASK_LIST_PAGE})")
    task_list.add_argument("--limit", metavar="N", type=int, default=TASK_LIST_PAGE, help=f"list at most N tasks (default: {TASK_LIST_PAGE})")
    task_list.add_argument("--offset", metavar="N", type=int, default=0, help="skip the first N tasks of the list")
    _add_json_arg(task_list)
    show = task_sub.add_parser("show")
    _add_mission_arg(show, default_mission)
    _add_task_selector(show)
    _add_json_arg(show)
    amend = task_sub.add_parser("amend")
    _add_mission_arg(amend, default_mission)
    _add_task_selector(amend)
    _read_prompt_args(amend, "replacement task")
    _add_json_arg(amend)
    task_help = {
        "priority": "change scheduling priority",
        "reorder": "change the ordering value",
        "dependencies": "add a dependency",
        "pause": "hold the task",
        "resume": "make the task eligible again",
        "cancel": "cancel the task",
        "retry": "queue another attempt",
        "verify": "record verification evidence",
        "reject": "send a task awaiting verification back for another attempt",
    }
    for action in ("priority", "reorder", "dependencies", "pause", "resume", "cancel", "retry", "verify", "reject"):
        cmd = task_sub.add_parser(action, help=task_help[action])
        _add_mission_arg(cmd, default_mission)
        _add_task_selector(cmd)
        if action == "priority":
            cmd.add_argument("--value", metavar="N", type=int, required=True, help="new priority; higher runs first")
        elif action == "reorder":
            cmd.add_argument("--value", metavar="N", type=int, required=True, help="new ordering value; lower runs first")
        elif action == "dependencies":
            cmd.add_argument("--add", metavar="N", type=int, required=True, help="number of the task this task must wait for")
        elif action == "verify":
            cmd.add_argument("--evidence", metavar="TEXT", default="", help="verification evidence, for example a test summary; shown by task show")
            cmd.add_argument("--approve", action="store_true", help="approve the task as complete; without it, --evidence is only recorded and the task keeps awaiting approval")
        elif action == "reject":
            cmd.add_argument("--reason", metavar="TEXT", required=True, help="why the result is not accepted (up to 2000 characters); the next attempt's prompt quotes it")
        _add_json_arg(cmd)

    proposals = sub.add_parser("proposal", help="review agent-originated task proposals")
    proposal_sub = proposals.add_subparsers(dest="proposal_action", required=True)
    proposal_create = proposal_sub.add_parser("create", help="record a proposal from an operation")
    _add_mission_arg(proposal_create, default_mission)
    proposal_create.add_argument("--operation", metavar="OPERATION_ID", required=True, dest="source_operation_id", help="ID of the provider operation that made the proposal")
    _read_prompt_args(proposal_create, "proposed task")
    proposal_create.add_argument("--title", metavar="TEXT", default="", help="short title for the proposed task")
    proposal_create.add_argument("--priority", metavar="N", type=int, default=0, help="priority the task gets if accepted; higher runs first (default: 0)")
    proposal_create.add_argument("--depends-on", metavar="IDS", default="", help="comma-separated task numbers the proposed task depends on")
    proposal_create.add_argument("--acceptance", metavar="TEXT", action="append", default=[], help="acceptance criterion for the proposed task; repeat for several")
    _add_json_arg(proposal_create)
    proposal_list = proposal_sub.add_parser("list", help="list proposals")
    _add_mission_arg(proposal_list, default_mission)
    proposal_list.add_argument("--status", choices=("PENDING", "ACCEPTED", "REJECTED", "ALL"), default="PENDING", help="only proposals in this state (default: PENDING)")
    proposal_list.add_argument("--limit", metavar="N", type=int, default=100, help="maximum proposals returned (default: 100)")
    proposal_list.add_argument("--after", metavar="N", type=int, default=0, help="only proposals with a task number above this (default: 0)")
    _add_json_arg(proposal_list)
    for proposal_action in ("accept", "reject"):
        proposal_decision = proposal_sub.add_parser(proposal_action, help=f"{proposal_action} a pending proposal")
        _add_mission_arg(proposal_decision, default_mission)
        proposal_decision.add_argument("--task", metavar="N", type=int, required=True, help="task number of the proposal, as shown by proposal list")
        if proposal_action == "reject":
            proposal_decision.add_argument("--reason", metavar="TEXT", default="", help="why the proposal is rejected")
        _add_json_arg(proposal_decision)

    events = sub.add_parser("events")
    _add_mission_arg(events, default_mission)
    events.add_argument("--after", metavar="EVENT", type=int, default=0, help="only events with an ID above this (default: 0)")
    events.add_argument("--limit", metavar="N", type=int, default=100, help="maximum events returned (default: 100)")
    _add_json_arg(events)

    for action in ("start", "pause", "resume", "continue", "stop", "cancel", "attach", "detach"):
        cmd = sub.add_parser(action)
        _add_mission_arg(cmd, default_mission)
        if action in {"attach", "detach"}:
            client_help = (
                "select a listed writable source client when switching from the Bridge console"
                if action == "attach"
                else "select a writable client to detach; required only when multiple are connected"
            )
            cmd.add_argument("--client", metavar="TTY", help=client_help)
        if action == "start":
            cmd.add_argument("--headless", action="store_true", default=None, help="Headless mode: run the provider without a terminal and without a station (default: the mission's saved mode)")
            cmd.add_argument("--native", dest="native_tui", action="store_true", default=None, help="Native mode: supervise the provider's own interactive terminal in a station (default: the mission's saved mode)")
            cmd.add_argument("--providers", metavar="LIST", help="comma-separated providers to rotate through; changes the mission's saved rotation for this and later starts, and the receipt says so (default: the saved rotation)")
            cmd.add_argument("--routes", metavar="LIST", help="comma-separated route IDs to rotate through; changes the mission's saved rotation for this and later starts, and the receipt says so (default: the saved rotation)")
        if action == "stop":
            cmd.add_argument("--force", action="store_true", help="terminate owned provider work and the controller now instead of waiting for the provider to exit")
        _add_json_arg(cmd)

    missions = sub.add_parser("missions", help="list every mission, live ones first, or pick one to switch to")
    missions.add_argument("--pick", action="store_true", help="open the interactive mission picker (needs a terminal)")
    missions.add_argument("--from", dest="from_mission", metavar="ID", help="mission whose station the picker was opened from (used by Ctrl-] then M)")
    missions.add_argument("--client", metavar="TTY", help="station client to switch; default: the most recently active one")
    _add_json_arg(missions)

    gc = sub.add_parser("gc", help="close the idle tmux stations of finished missions (stations with a live provider or an attached terminal are kept)")
    _add_json_arg(gc)

    finish = sub.add_parser("finish", help="ask the native provider to exit so its task moves to verification")
    _add_mission_arg(finish, default_mission)
    _add_json_arg(finish)

    doctor = sub.add_parser("doctor")
    _add_mission_arg(doctor, default_mission, inferred=False)
    doctor.add_argument("--include-config", action="store_true", help="with --json, include the mission's saved copy of the llm-tools config (large; left out by default)")
    _add_json_arg(doctor)

    theme = sub.add_parser("theme", help="show the detected terminal background, the active colour theme and a swatch per role")
    theme.add_argument("--theme", dest="theme_name", choices=THEME_CHOICES, default="auto",
                       help="preview another theme instead of the active one")
    theme.add_argument("--colors", type=int, choices=(8, 16, 256), help="preview a terminal with this many colours")
    theme.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="render swatches with colour (default: when stdout is a terminal)")
    _add_json_arg(theme)

    console = sub.add_parser("console", help=argparse.SUPPRESS)
    _add_mission_arg(console, default_mission, required=True, inferred=False)
    control = sub.add_parser("control", help="open the full Bridge control view")
    _add_mission_arg(control, default_mission)
    usage_view = sub.add_parser("usage", help="show the full llm-usage dashboard inside Bridge")
    _add_mission_arg(usage_view, default_mission)

    station = sub.add_parser("station", help="choose the station shown in the control view")
    _add_mission_arg(station, default_mission)
    station.add_argument("name", help="provider or route name from the mission's rotation")
    _add_json_arg(station)

    rotation = sub.add_parser("rotation", help="show or change the mission's saved provider rotation")
    _add_mission_arg(rotation, default_mission)
    rotation_change = rotation.add_mutually_exclusive_group()
    rotation_change.add_argument("--set", dest="set_list", metavar="LIST", help="comma-separated provider or route names, tried in this order; provider names alone replace the saved rotation, any route name selects route mode")
    rotation_change.add_argument("--routes", metavar="LIST", help="comma-separated route IDs from the frozen tool config (or provider names) to rotate through")
    rotation_change.add_argument("--providers", metavar="LIST", help="comma-separated providers to rotate through; clears saved routes")
    _add_json_arg(rotation)

    steer = sub.add_parser("steer", help="queue typed steering for a task or safe boundary")
    _add_mission_arg(steer, default_mission)
    steer.add_argument("--kind", required=True, choices=("replace", "review", "verify", "pause-after-iteration", "stop-after-task", "handoff"), help="steering to queue")
    _add_task_selector(steer, required=False, note="; required for replace, review, verify and stop-after-task")
    steer.add_argument("--route", metavar="NAME", help="station to hand over to (required for handoff)")
    steer.add_argument("--file", "--prompt-file", dest="prompt_file", metavar="PATH", help="read the replacement or review prompt from PATH")
    steer.add_argument("--prompt", help="the replacement or review prompt as literal text")
    _add_json_arg(steer)

    station_gc = sub.add_parser("_station-gc", help=argparse.SUPPRESS)
    station_gc.add_argument("--mission", required=True)
    station_gc.add_argument("--exclude-client", action="append", default=[])
    controller = sub.add_parser("_controller", help=argparse.SUPPRESS)
    _add_mission_arg(controller, default_mission, required=True, inferred=False)
    controller.add_argument("--headless", action="store_true")
    controller.add_argument("--native", dest="native_tui", action="store_true")
    controller.add_argument("--startup-token", default="")
    controller.add_argument("--providers", default=None)
    controller.add_argument("--routes", default=None)
    if only is None:  # decoration only changes help text
        from .bridge_help import decorate_parser

        decorate_parser(parser)
    return parser


class _FastParseFailed(Exception):
    """Raised instead of exiting when the single-command parser rejects the arguments."""


def _parse_arguments(arguments: list[str]) -> argparse.Namespace:
    """Parse a command line, building only the requested command's parser when that is enough.

    Anything the single-command parser cannot accept (an unknown command, a request for help, a
    usage error) is handed to the full parser, which then produces the usual output.
    """
    command = arguments[0] if arguments else ""
    if command and not command.startswith("-") and not any(word in {"-h", "--help"} for word in arguments):
        import argparse

        class _FastParser(argparse.ArgumentParser):
            def error(self, message: str) -> Any:
                raise _FastParseFailed(message)

        try:
            parser = _build_parser(parser_class=_FastParser, only=command)
            if command in {name for action in parser._actions if isinstance(action, argparse._SubParsersAction) for name in action.choices}:
                return parser.parse_args(arguments)
        except _FastParseFailed:
            pass
    return _build_parser().parse_args(arguments)


def _doctor_provider_checks(mission: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Install and sign-in checks for the providers a mission, or a new mission, would launch."""
    from . import bridge_doctor

    if mission is not None:
        config = mission["config"]
        names = fmt.rotation_names(config)
        conf = _mission_tool_config(config)
        routes_mode = bool(fmt.rotation_names({"routes": config.get("routes")}))
    else:
        providers, routes = default_rotation()
        names = fmt.rotation_names({"routes": routes, "providers": providers})
        routes_mode = bool(routes)
        try:
            from . import config as toolconfig

            conf = toolconfig.load_config() if routes_mode else {}
        except (OSError, ValueError):
            conf = {}
    entries: list[tuple[str, str | None]] = []
    for position, name in enumerate(names, 1):
        entry = _rotation_entry(conf, name, position, routes_mode=routes_mode)
        entries.append((name, entry["launch_cli"] if routes_mode else name))
    return bridge_doctor.provider_checks(entries)


def _validated_cwd(raw: str) -> str:
    """The folder a mission works in, or a plain error naming it."""
    path = Path(raw).expanduser()
    if not path.exists():
        raise ValueError(f"Folder does not exist: {raw}")
    if not path.is_dir():
        raise ValueError(f"Not a folder: {raw}")
    if any(marker in text for text in (raw, str(path.resolve())) for marker in ("#(", "#{")):
        raise ValueError(
            "Folder path contains '#(' or '#{', which tmux would run or expand as a format; "
            f"rename the folder or choose another: {raw}"
        )
    return str(path)


def _reject_conflicting_modes(args: argparse.Namespace) -> None:
    """A mission is either Native (``--native``), Headless (``--headless``) or, with neither, Background."""
    if args.headless and args.native_tui:
        raise ValueError("--headless and --native cannot be combined: Native runs the provider's own terminal in a station, "
                         "Headless runs it without a terminal. Pass one of them, or neither for Background.")


def _mission_creation_inputs(args: argparse.Namespace, *, auto_continue: bool) -> tuple[str, dict[str, Any]]:
    """Validate the create options and build the objective text and mission config.

    Normalises ``args.cwd``, ``args.providers`` and ``args.routes`` in place.
    Raises ``ValueError`` for conflicting modes or an unusable folder before any
    store is opened, so a rejected command leaves no database behind.
    """
    prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
    _reject_conflicting_modes(args)
    args.cwd = _validated_cwd(args.cwd)
    args.providers, args.routes = creation_rotation(args.providers, args.routes)
    config: dict[str, Any] = {"providers": args.providers, "routes": args.routes, "headless": args.headless, "native_tui": args.native_tui, "frozen_tool_config": _frozen_tool_config(), **_log_retention_config(args)}
    if args.command_template:
        config["command_template"] = args.command_template
    if args.trust_folder:
        config["trust_folder"] = True
    if auto_continue:
        config["auto_continue"] = True
    verifier = _verifier_config(args)
    if verifier:
        config["verifier"] = verifier
    return prompt, config


def _create_mission_with_task(store: BridgeStore, args: argparse.Namespace, prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    """Create the mission and its first task; returns ``{"mission": ..., "task": ...}``."""
    mission = store.create_mission(
        args.name or (derive_title(prompt, "LLM Bridge mission")),
        prompt,
        cwd=args.cwd,
        initial_prompt=prompt,
        initial_acceptance=args.acceptance,
        config=config,
    )
    return {"mission": mission, "task": store.get_task(int(mission["initial_task_id"]))}


def _legacy_prompt_mode(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="llm-bridge")
    _read_prompt_args(parser, "mission objective and first task")
    _add_mission_create_args(parser, legacy=True)
    _add_json_arg(parser)
    args = parser.parse_args(argv)
    # The bare form has always ignored --auto-continue; only `mission create` honours it.
    prompt, config = _mission_creation_inputs(args, auto_continue=False)
    with open_store() as store:
        result = _create_mission_with_task(store, args, prompt, config)
    mission = result["mission"]
    if args.json:
        _json_print(result if args.include_config else strip_frozen_config(result))
        return 0
    if args.no_start:
        print(fmt.format_mission_created(result))
        return 0
    return start_mission(mission["id"], headless=args.headless, native_tui=args.native_tui, providers=args.providers, routes=args.routes)


def start_mission(mission_id: str, *, headless: bool | None = None, native_tui: bool | None = None, providers: str | None = None, routes: str | None = None) -> int:
    from .bridge_runtime import launch_controller

    return launch_controller(mission_id, headless=headless, native_tui=native_tui, providers=providers, routes=routes)


def no_station_message(mission_id: str) -> str:
    short = str(mission_id)[:8]
    try:
        with open_store() as store:
            state = str(store.get_mission(mission_id)["status"])
    except (KeyError, ValueError, OSError, TransitionError):
        state = ""
    if state in ENDED_MISSION_STATUSES:
        return f"Mission {short} is {state.lower()} and its station was closed · see what happened with: llm-bridge status --mission {short}"
    return f"No provider station is running for mission {short} · start it with: llm-bridge start --mission {short}"


def _load_handle(mission_id: str):
    from .bridge_runtime import mission_socket_path
    from .terminal import TmuxTerminalBackend

    socket_path = mission_socket_path(mission_id)
    if not Path(socket_path).exists():
        raise RuntimeError(no_station_message(mission_id))
    try:
        return TmuxTerminalBackend().open_existing(
            socket_path=socket_path,
            session_name=f"mission-{mission_id}",
            bridge_rows=6,
        )
    except RuntimeError as exc:
        if str(exc).startswith("no live terminal station"):
            raise RuntimeError(no_station_message(mission_id)) from exc
        raise


def _print_theme(args: argparse.Namespace) -> None:
    """Report the detected background and active theme, with a swatch per role."""
    from . import bridge_theme

    base = bridge_theme.resolve_cached()
    resolved = base if args.theme_name == "auto" else bridge_theme.resolve(
        theme_name=args.theme_name, reference=base)
    colors = args.colors or bridge_theme.terminal_colors()
    if args.json:
        _json_print(bridge_theme.describe(resolved, colors=colors))
        return
    color = args.color == "always" or (
        args.color == "auto" and sys.stdout.isatty() and not bridge_theme.colors_disabled())
    print(bridge_theme.format_theme_report(resolved, colors=colors, color=color))


def _split_names(raw: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def _mission_tool_config(config: dict[str, Any]) -> dict[str, Any]:
    """The mission's frozen llm-tools config as a plain dict (empty when absent or unreadable)."""
    frozen = config.get("frozen_tool_config", "")
    if not isinstance(frozen, str) or not frozen:
        return {}
    import tomllib

    try:
        parsed = tomllib.loads(frozen)
    except tomllib.TOMLDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _rotation_entry(conf: dict[str, Any], name: str, position: int, *, routes_mode: bool) -> dict[str, Any]:
    """Launch CLI and pinned model for one rotation member, from the frozen config."""
    launch_cli: str | None = None
    model: str | None = None
    try:
        if routes_mode:
            from .routes import route_or_implicit_provider

            policy = route_or_implicit_provider(conf, name)
            if policy is not None:
                launch_cli, model = policy.provider, policy.model
        else:
            from . import config as toolconfig

            launch_cli, model = name, toolconfig.provider_policy(conf, name).model
    except (ValueError, TypeError, AttributeError, KeyError):
        pass
    return {"position": position, "name": name, "launch_cli": launch_cli, "model": model}


def _validated_rotation_names(conf: dict[str, Any], raw: str, *, routes_allowed: bool) -> list[str]:
    import difflib

    from .capacity import ALL_PROVIDERS

    names = _split_names(raw)
    if not names:
        raise ValueError("a rotation needs at least one provider or route name")
    declared = list(conf.get("routes", {})) if routes_allowed and isinstance(conf.get("routes"), dict) else []
    known = [*declared, *(name for name in ALL_PROVIDERS if name not in declared)]
    for name in names:
        if name not in known:
            close = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
            hint = f" · did you mean {', '.join(close)}?" if close else ""
            raise ValueError(f"unknown rotation entry '{name}'{hint} · available: {', '.join(known)}")
    return names


def _rotation_result(args: argparse.Namespace) -> dict[str, Any]:
    """Show the mission's saved rotation, or change it when an option names one."""
    requested = [value for value in (args.set_list, args.routes, args.providers) if value is not None]
    with open_store() as store:
        mission = store.get_mission(args.mission)
        conf = _mission_tool_config(mission["config"])
        change: dict[str, Any] = {}
        if requested:
            saved_providers = str(mission["config"].get("providers", ""))
            if args.providers is not None:
                providers, routes = ",".join(_validated_rotation_names(conf, args.providers, routes_allowed=False)), ""
            elif args.routes is not None:
                providers, routes = saved_providers, ",".join(_validated_rotation_names(conf, args.routes, routes_allowed=True))
            else:
                names = _validated_rotation_names(conf, args.set_list, routes_allowed=True)
                declared = conf.get("routes", {}) if isinstance(conf.get("routes"), dict) else {}
                if any(name in declared for name in names):
                    providers, routes = saved_providers, ",".join(names)
                else:
                    providers, routes = ",".join(names), ""
            require_launchable_rotation(mission["config"], providers, routes, explicit=True)
            change = store.set_rotation(args.mission, providers=providers, routes=routes, source="rotation")
            mission = change["mission"]
        config = mission["config"]
        routes_mode = bool(fmt.rotation_names({"routes": config.get("routes")}))
        names = fmt.rotation_names(config)
        selection = store.db.execute(
            "SELECT data_json FROM events WHERE mission_id=? AND kind='provider-selection' ORDER BY id DESC LIMIT 1",
            (args.mission,),
        ).fetchone()
    selected_data = json.loads(selection["data_json"]) if selection else {}
    selected = str(selected_data.get("route") or selected_data.get("provider") or "")
    entries = [{**_rotation_entry(conf, name, index, routes_mode=routes_mode), "selected": name == selected} for index, name in enumerate(names, 1)]
    running = controller_running(args.mission)
    result: dict[str, Any] = {
        "schema_version": 1,
        "mission_id": args.mission,
        "mode": "routes" if routes_mode else "providers",
        "providers": _split_names(str(config.get("providers", ""))),
        "routes": _split_names(str(config.get("routes", ""))),
        "entries": entries,
        "selected": selected if selected in names else None,
        "controller_running": running,
        "updated": bool(requested),
        "changed": bool(change.get("changed")),
    }
    if requested:
        result["from"], result["to"] = change["from"], change["to"]
        result["applies"] = (
            "Applies from the next provider selection; a provider that is running now is not interrupted."
            if running
            else "Takes effect the next time the mission starts."
        )
    return result


def _set_mission_colour(store: BridgeStore, mission_id: str, value: str) -> dict[str, Any]:
    """Pin the mission's identity colour (1-8) or clear the pin (`auto`); records a `mission-colour` event."""
    text = str(value).strip().lower()
    if text == "auto":
        pinned: int | None = None
    elif text.isdigit() and 1 <= int(text) <= MISSION_COUNT:
        pinned = int(text)
    else:
        raise ValueError(f"colour must be a number from 1 to {MISSION_COUNT}, or auto (got {value!r})")
    from . import bridge_theme

    mission = store.set_mission_config_value(mission_id, "colour", pinned, event_kind="mission-colour")
    number = bridge_theme.mission_number(mission_id, mission["config"])
    return {
        "schema_version": 1,
        "mission_id": mission_id,
        "name": mission.get("name"),
        "colour": pinned,
        "pinned": pinned is not None,
        "role": f"mission_{number}",
        "number": number,
        "hue": MISSION_HUE_NAMES[number - 1],
    }


def _set_mission_auto_continue(store: BridgeStore, mission_id: str, enabled: bool) -> dict[str, Any]:
    """Turn the native review hold off (``enabled``) or on; records a ``mission-auto-continue`` event."""
    store.set_mission_config_value(mission_id, "auto_continue", enabled, event_kind="mission-auto-continue")
    detail = (
        "Auto-continue on · the next task starts as soon as a provider run ends, without waiting for review"
        if enabled else
        "Auto-continue off · the mission holds after a provider run until you /approve, /reject or /continue"
    )
    return {"schema_version": 1, "mission_id": mission_id, "auto_continue": enabled, "detail": detail}


def _print_result(result: Any, as_json: bool, human: Callable[[Any], str] | None = None) -> None:
    """Print ``result``: stable sorted JSON for ``--json``, otherwise readable text."""
    if as_json:
        _json_print(result)
    elif isinstance(result, str):
        print(result)
    elif human is not None:
        print(human(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _stop_terminal_mission_station(mission_id: str) -> dict[str, Any]:
    """Close a retained station after a terminal mission has released control."""
    lock = MissionLock(state_dir() / "locks", mission_id)
    try:
        lock.acquire()
    except BlockingIOError as exc:
        raise RuntimeError(f"mission {mission_id[:8]} still has an active controller; stop is pending") from exc
    try:
        with open_store() as store:
            mission = store.get_mission(mission_id)
            if mission["status"] not in {"COMPLETED", "FAILED", "CANCELLED", "STOPPED"}:
                raise RuntimeError(f"mission {mission_id[:8]} changed state while stopping; run stop again")
            from .bridge_runtime import mission_socket_path
            from .terminal import DeadStationError, TmuxTerminalBackend

            socket = mission_socket_path(mission_id)
            station_status = "ABSENT"
            if socket.exists():
                backend = TmuxTerminalBackend()
                try:
                    handle = backend.open_existing(socket, f"mission-{mission_id}")
                except DeadStationError:
                    handle = None
                if handle is not None:
                    backend.stop(handle)
                    station_status = "STOPPED"
                    store.record_runtime_event(
                        mission_id,
                        "station-stopped",
                        {"mission_status": mission["status"], "source": "operator"},
                    )
            return {"mission": store.get_mission(mission_id), "station_status": station_status}
    finally:
        lock.release()


def _task_text(args: argparse.Namespace) -> Callable[[Any], str]:
    action = args.task_action
    if action == "list":
        return fmt.format_task_list
    if action == "show":
        return fmt.format_task_detail
    return lambda value: fmt.format_task_result(action, value)


def _proposal_text(args: argparse.Namespace) -> Callable[[Any], str]:
    if args.proposal_action == "list":
        return lambda value: fmt.format_proposals(value["proposals"])
    return lambda value: fmt.format_proposal_result(args.proposal_action, value)


MANAGED_ENV = "LLM_BRIDGE_MANAGED"
MANAGED_REFUSAL = "This command needs the operator; it is not available to the managed provider"
MANAGED_MISSION_ENV = "LLM_BRIDGE_MISSION_ID"
MANAGED_SCOPE_REFUSAL = "A managed provider may only address its own mission"
# Commands a managed provider may run: read-only inspection and recording a
# proposal (which still needs the operator's acceptance).
MANAGED_ALLOWED: frozenset[tuple[str, str]] = frozenset({
    ("status", ""), ("events", ""), ("help", ""),
    ("task", "list"), ("task", "show"),
    ("proposal", "create"), ("proposal", "list"),
    ("mission", "list"), ("missions", ""),
})


def _managed_session() -> bool:
    return os.environ.get(MANAGED_ENV) == "1"


def _managed_allows(args: argparse.Namespace) -> bool:
    """Advisory guard: a provider Bridge launched may not run operator commands.

    The provider runs as the same user, so this keeps a prompt-injected agent
    from invoking `proposal accept`, `task verify --approve`, `cancel` or `steer`
    by habit; it is not a security boundary.
    """
    command = str(args.command)
    action_attribute = {"task": "task_action", "proposal": "proposal_action", "mission": "mission_action"}.get(command)
    action = str(getattr(args, action_attribute, "") or "") if action_attribute else ""
    if command == "missions" and getattr(args, "pick", False):
        return False
    return (command, action) in MANAGED_ALLOWED


def _installed_version_from_path() -> str | None:
    """Version recorded in an ``llm_tools-*.dist-info`` folder on ``sys.path``, or ``unknown`` when none exists.

    ``None`` means the answer needs the full metadata machinery (a zipped or unreadable path entry, or a
    record without a version). ``importlib.metadata`` is a heavy import for a one-line answer.
    """
    for entry in sys.path:
        try:
            with os.scandir(entry or ".") as listing:
                records = [item.name for item in listing if item.name.endswith((".dist-info", ".egg-info"))]
        except NotADirectoryError:
            return None  # a zip or egg file: leave it to importlib.metadata
        except OSError:
            continue  # a missing or unreadable path entry holds no distributions
        for record in sorted(records):
            name = record.rsplit(".", 1)[0].split("-", 1)[0]
            if name.lower().replace("-", "_").replace(".", "_") != "llm_tools":
                continue
            for filename in ("METADATA", "PKG-INFO"):
                try:
                    with open(os.path.join(entry or ".", record, filename), encoding="utf-8") as stream:
                        for line in stream:
                            if line.startswith("Version:"):
                                return line.split(":", 1)[1].strip()
                            if not line.strip():
                                break
                except OSError:
                    continue
            return None
    return "unknown"


def package_version() -> str:
    """Installed llm-tools version, or ``unknown`` when run from an uninstalled checkout."""
    found = _installed_version_from_path()
    if found is not None:
        return found
    from importlib import metadata

    try:
        return metadata.version("llm-tools")
    except metadata.PackageNotFoundError:
        return "unknown"


def _managed_scope_violation(args: argparse.Namespace) -> bool:
    """Whether a managed caller names a mission other than its own (``$LLM_BRIDGE_MISSION_ID``).

    Compared after resolution, so a prefix, a folder or an omitted ``--mission``
    that would infer another mission is refused too. Like the allow-list this is
    a guardrail for a prompt-injected agent, not a security boundary.
    """
    own = os.environ.get(MANAGED_MISSION_ENV, "").strip().lower()
    named = getattr(args, "mission", None)
    if not own or not named:
        return False
    return str(named).lower() != own


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except BaseException as error:
        # bridge_hangup imports curses; it is loaded only by commands that draw a screen,
        # so short commands do not pay for it.
        hangup = sys.modules.get(f"{__package__}.bridge_hangup")
        if hangup is not None and isinstance(error, hangup.TerminalGone):
            return hangup.abandon_terminal()  # the terminal was hung up under a curses screen; it has already been restored
        raise


def _main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        if sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "") not in {"", "dumb"} and not _managed_session():
            from .bridge_launcher import run_launcher

            from . import bridge_theme

            bridge_theme.prepare()  # query the terminal background before curses owns the tty
            prune_old_drafts()

            try:
                return run_launcher(main)
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"llm-bridge: {exc}", file=sys.stderr)
                return 2
        _build_parser().print_help()
        return 0
    if arguments[0] == "help" and not any(arg in {"--help", "-h"} for arg in arguments[1:]):
        from .bridge_help import is_known_topic, topic_text

        topic = " ".join(arguments[1:]) or "quickstart"
        if not is_known_topic(topic):
            print(f"llm-bridge: {topic_text(topic)}", file=sys.stderr)
            return 2
        print(topic_text(topic))
        return 0
    if arguments == ["--version"]:
        print(f"llm-bridge {package_version()}")
        return 0
    if arguments and arguments[0] in {"--prompt-file", "--file", "--prompt"}:
        if _managed_session():
            print(f"llm-bridge: {MANAGED_REFUSAL}", file=sys.stderr)
            return 2
        try:
            return _legacy_prompt_mode(arguments)
        except (ValueError, KeyError, OSError, TransitionError) as exc:
            print(f"llm-bridge: {exc}", file=sys.stderr)
            return 2
    args = _parse_arguments(arguments)
    if _managed_session() and not _managed_allows(args):
        print(f"llm-bridge: {MANAGED_REFUSAL}", file=sys.stderr)
        return 2
    if _managed_session() and hasattr(args, "mission") and args.mission is None and os.environ.get(MANAGED_MISSION_ENV):
        args.mission = os.environ[MANAGED_MISSION_ENV].strip()  # never infer another mission from the folder
    if args.command in {"control", "usage", "console", "start", "resume", "attach", "view"}:
        from . import bridge_theme

        bridge_theme.prepare()
        if args.command in {"control", "usage", "console"}:
            prune_old_drafts()
    try:
        if args.command == "attach" and args.client:
            print("llm-bridge: --client is only used by attach from an active Bridge console", file=sys.stderr)
            return 2
        if args.command != "_controller":
            _resolve_parsed_mission(args, optional=args.command == "doctor")
            if _managed_session() and _managed_scope_violation(args):
                print(f"llm-bridge: {MANAGED_SCOPE_REFUSAL}; mission {str(args.mission)[:8]} is not yours", file=sys.stderr)
                return 2
        if args.command == "view":
            from .bridge_viewer import read_source, run_document
            path = Path(args.file).expanduser()
            source = read_source(path)
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                if args.follow:
                    raise ValueError("--follow needs an interactive terminal; omit it to print original source")
                sys.stdout.write(source)
                return 0
            return run_document(source, title=f"Artifact · {path.name}", path=path, follow=args.follow)
        if args.command == "_controller":
            from .bridge_runtime import run_controller
            from .bridge_logs import capture_process_output

            with open_store() as store:
                mission = store.get_mission(args.mission)
                retention = mission["config"]
            controller_log = state_dir() / "runtime" / f"mission-{args.mission}" / "controller.log"
            with capture_process_output(
                controller_log,
                max_bytes=int(retention.get("log_max_bytes", 4 * 1024 * 1024)),
                rotations=int(retention.get("log_rotations", 3)),
            ):
                return run_controller(args.mission, headless=args.headless, native_tui=args.native_tui, providers=args.providers, routes=args.routes, startup_token=args.startup_token)
        if args.command == "_station-gc":
            from .bridge_runtime import gc_finished_station

            print(gc_finished_station(args.mission, tuple(args.exclude_client)))
            return 0
        if args.command == "console":
            from .bridge_tui import run_console

            return run_console(args.mission, dispatch=dispatch_console_command, snapshot=console_snapshot)
        if args.command in {"control", "usage"}:
            from .bridge_tui import run_control

            return run_control(args.mission, dispatch=dispatch_console_command, snapshot=console_snapshot,
                               initial_view="Usage" if args.command == "usage" else "Bridge")
        if args.command == "resume":
            with open_store() as store:
                mission = store.get_mission(args.mission)
                probe = MissionLock(state_dir() / "locks", args.mission)
                try:
                    probe.acquire()
                except BlockingIOError:
                    pass  # a controller already owns the mission; its rotation was checked when it started
                else:
                    probe.release()
                    require_launchable_rotation(mission["config"], mission["config"].get("providers"), mission["config"].get("routes"), explicit=False)
                result = _apply_operator_command(store, args.mission, {"action": "resume"})
                config = mission["config"]
            if result.get("status") == "REJECTED":
                # A finished mission is not restarted; say why instead of launching a controller.
                if args.json:
                    _json_print(result)
                    return 0
                print(f"llm-bridge: {(result.get('result') or {}).get('error', 'resume rejected')}", file=sys.stderr)
                return 2
            lock = MissionLock(state_dir() / "locks", args.mission)
            try:
                lock.acquire()
            except BlockingIOError:
                if not config.get("headless") and config.get("native_tui") and sys.stdin.isatty() and sys.stdout.isatty():
                    from .bridge_runtime import mission_socket_path
                    from .terminal import TmuxTerminalBackend

                    handle = TmuxTerminalBackend().open_existing(
                        mission_socket_path(args.mission),
                        f"mission-{args.mission}",
                    )
                    return TmuxTerminalBackend().attach(handle)
                _print_result(result, args.json, lambda value: fmt.format_command_receipt("resume", value, args.mission))
                return 0
            else:
                lock.release()
            return start_mission(
                args.mission,
                headless=bool(config.get("headless", False)),
                native_tui=bool(config.get("native_tui", False)),
                providers=config.get("providers"),
                routes=config.get("routes"),
            )
        if args.command in {"start", "resume"}:
            with open_store() as store:
                mission = store.get_mission(args.mission)
                config = mission["config"]
            requested_headless = getattr(args, "headless", None)
            requested_native = getattr(args, "native_tui", None)
            if requested_headless is True:
                requested_native = False
            elif requested_native is True:
                requested_headless = False
            requested_providers = getattr(args, "providers", None)
            requested_routes = getattr(args, "routes", None)
            if requested_providers is None and requested_routes is None:
                requested_providers, requested_routes = config.get("providers"), config.get("routes")
            elif requested_routes is None:
                requested_routes = ""
            elif requested_providers is None:
                requested_providers = config.get("providers")
            if not controller_running(args.mission):
                require_launchable_rotation(
                    config, requested_providers, requested_routes,
                    explicit=getattr(args, "providers", None) is not None or getattr(args, "routes", None) is not None,
                )
            saved_label = rotation_label(config.get("providers"), config.get("routes"))
            requested_label = rotation_label(requested_providers, requested_routes)
            if saved_label and (str(config.get("providers", "")), str(config.get("routes", ""))) != (str(requested_providers or ""), str(requested_routes or "")):
                if requested_label != saved_label and controller_running(args.mission):
                    raise RuntimeError(
                        f"mission {fmt.short_id(args.mission)} already has a controller; change its rotation with: "
                        f"llm-bridge rotation --mission {fmt.short_id(args.mission)} --set LIST"
                    )
                with open_store() as store:
                    store.set_rotation(args.mission, providers=str(requested_providers or ""), routes=str(requested_routes or ""), source="start")
                if requested_label != saved_label:
                    print(fmt.format_rotation_change(saved_label, requested_label))
            return start_mission(
                args.mission,
                headless=requested_headless,
                native_tui=requested_native,
                providers=requested_providers,
                routes=requested_routes,
            )
        if args.command == "attach":
            from .terminal import TmuxTerminalBackend

            return TmuxTerminalBackend().attach(_load_handle(args.mission))
        if args.command == "detach":
            handle = _load_handle(args.mission)
            from .terminal import TmuxTerminalBackend

            detached = TmuxTerminalBackend().detach_client(handle, client_name=args.client)
            print(f"Detached {detached} from mission {fmt.short_id(args.mission)}")
            return 0
        if args.command == "missions":
            from . import bridge_switcher

            if args.pick:
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise ValueError("missions --pick needs an interactive terminal; use llm-bridge missions to list")
                source = None
                with open_store(readonly=True) as store:
                    if args.from_mission:
                        source = resolve_mission_ref(store, args.from_mission)
                return bridge_switcher.run_pick_cli(source, args.client)
            with open_store(readonly=True) as store:
                entries = bridge_switcher.collect_entries(store)
            if args.json:
                _json_print(bridge_switcher.missions_json(entries))
            else:
                print(bridge_switcher.format_missions(entries))
            return 0
        if args.command == "gc":
            from . import bridge_switcher

            with open_store() as store:
                entries = bridge_switcher.collect_entries(store)
            results = bridge_switcher.gc_stations(entries)
            if args.json:
                _json_print(bridge_switcher.gc_json(results))
            else:
                print(bridge_switcher.format_gc(results))
            return 0
        if args.command == "finish":
            from .bridge_finish import FinishError, finish_mission

            try:
                finished = finish_mission(args.mission)
            except FinishError as exc:
                print(f"llm-bridge: {exc}", file=sys.stderr)
                return 2
            _print_result(finished.as_json(), args.json, lambda value: value["detail"])
            return 0
        if args.command == "mission":
            with open_store(readonly=args.mission_action == "list") as store:
                if args.mission_action == "create":
                    prompt, config = _mission_creation_inputs(args, auto_continue=bool(args.auto_continue))
                    result = _create_mission_with_task(store, args, prompt, config)
                elif args.mission_action == "set":
                    result = _set_mission_auto_continue(store, args.mission, args.auto_continue == "on")
                    _print_result(result, bool(getattr(args, "json", False)), lambda value: value["detail"])
                    return 0
                elif args.mission_action in {"colour", "color"}:
                    result = _set_mission_colour(store, args.mission, args.value)
                    _print_result(result, bool(getattr(args, "json", False)), fmt.format_mission_colour)
                    return 0
                else:
                    missions = order_missions(store.list_missions())
                    for item in missions:
                        item["controller_running"] = controller_running(str(item["id"]))
                    result = {"missions": missions}
            human = fmt.format_mission_created if args.mission_action == "create" else (lambda value: fmt.format_mission_list(value["missions"]))
            if not args.include_config:
                result = strip_frozen_config(result)
            _print_result(result, bool(getattr(args, "json", False)), human)
            return 0
        if args.command == "status":
            result = status_snapshot(args.mission)
            _print_result(result, args.json, fmt.format_status)
            return 0
        if args.command == "events":
            with open_store(readonly=True) as store:
                result = {"schema_version": 1, "events": store.events(args.mission, after=args.after, limit=args.limit)}
            _print_result(result, args.json, lambda value: fmt.format_events(value["events"]))
            return 0
        if args.command == "proposal":
            with open_store() as store:
                if args.proposal_action == "list":
                    # Task numbers rise with creation, so "above number N" is "above the highest ID numbered N or less".
                    after_id = int(store.db.execute("SELECT COALESCE(MAX(id),0) FROM tasks WHERE mission_id=? AND ordinal<=?", (args.mission, max(0, args.after))).fetchone()[0])
                    result = {
                        "proposals": store.list_proposals(
                            args.mission, status=args.status, limit=args.limit, after_task_id=after_id,
                        ),
                    }
                else:
                    if args.proposal_action == "create":
                        prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
                        payload = {
                            "action": "task.propose",
                            "source_operation_id": args.source_operation_id,
                            "prompt": prompt,
                            "title": args.title,
                            "priority": args.priority,
                            "depends_on": _dependency_ids(store, args.mission, args.depends_on),
                            "acceptance": args.acceptance,
                        }
                    else:
                        payload = {
                            "action": "task.proposal-decision",
                            "task_id": int(store.task_by_ordinal(args.mission, args.task)["id"]),
                            "accept": args.proposal_action == "accept",
                            "reason": getattr(args, "reason", ""),
                        }
                    result = _apply_operator_command(store, args.mission, payload)
            if isinstance(result, dict) and result.get("status") == "REJECTED":
                detail = result.get("result") or {}
                print(f"llm-bridge: {detail.get('error', 'proposal command rejected')}", file=sys.stderr)
                return 2
            _print_result(result, bool(getattr(args, "json", False)), _proposal_text(args))
            return 0
        if args.command == "task":
            result = _task_result(args)
            if isinstance(result, dict) and result.get("status") == "REJECTED":
                detail = result.get("result") or {}
                print(f"llm-bridge: {detail.get('error', 'command rejected')}", file=sys.stderr)
                return 2
            if getattr(args, "json", False) and isinstance(result, fmt.TaskPage) and result.total is not None and len(result) < result.total - result.offset:
                print(f"llm-bridge: listed {len(result)} of {result.total} tasks from position {result.offset + 1}; use --all for every task", file=sys.stderr)
            _print_result(result, bool(getattr(args, "json", False)), _task_text(args))
            return 0
        if args.command == "continue":
            with open_store() as store:
                result = _apply_operator_command(store, args.mission, {"action": "continue"})
            if result.get("status") == "REJECTED" and not args.json:
                print(f"llm-bridge: {(result.get('result') or {}).get('error', 'continue rejected')}", file=sys.stderr)
                return 2
            _print_result(result, args.json, lambda value: fmt.format_command_receipt("continue", value, args.mission))
            return 0
        if args.command in {"pause", "stop", "cancel"}:
            with open_store() as store:
                saved_status = str(store.get_mission(args.mission)["status"])
            if args.command == "stop" and args.force and saved_status == "CREATED":
                # A mission that never started has nothing to force; stop never cancels.
                result = {"mission_status": saved_status, "noop": True, "detail": fmt.nothing_to_stop(args.mission, saved_status)}
            elif args.command == "stop" and args.force:
                runtime = state_dir() / "runtime" / f"mission-{args.mission}"
                with open_store() as store:
                    mission = store.get_mission(args.mission)
                    if mission["status"] in STARTED_MISSION_STATUSES:
                        store.set_mission_status(args.mission, "STOPPING")
                    operations = [
                        dict(row)
                        for row in store.db.execute(
                            "SELECT id,process_pid,process_start,pane_id,status FROM operations WHERE mission_id=? AND status IN ('INTENT','PREPARED','RUNNING')",
                            (args.mission,),
                        )
                    ]
                    store.reconcile_interrupted_operations(args.mission)
                # Verifiers use their own process groups. Stop those validated
                # identities before terminating the controller that owns them.
                from .bridge_runtime import terminate_managed_processes

                with open_store() as store:
                    terminate_managed_processes(store, args.mission)
                try:
                    from .terminal import TmuxTerminalBackend

                    backend = TmuxTerminalBackend()
                    handle = _load_handle(args.mission)
                except (OSError, RuntimeError):
                    backend = None
                    handle = None
                if handle is not None and backend is not None:
                    backend.stop(handle)
                elif operations:
                    from .bridge_runtime import _terminate_owned_process_group

                    for operation in operations:
                        process_pid = operation.get("process_pid")
                        process_start = operation.get("process_start")
                        if not process_pid or not process_start:
                            if operation.get("status") in {"INTENT", "PREPARED"}:
                                continue
                            raise RuntimeError("no verifiable process identity; mission remains STOPPING")
                        _terminate_owned_process_group(int(process_pid), str(process_start))
                # Stopping provider work and the tmux station is not enough:
                # the detached controller itself owns the mission lock and
                # must be terminated before STOPPED is authoritative.
                ready = runtime / "controller-ready.json"
                try:
                    controller = json.loads(ready.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    controller = {}
                controller_pid = int(controller.get("pid", 0) or 0)
                controller_start = str(controller.get("process_start", ""))
                if controller_pid > 1 and controller_start:
                    from .bridge_runtime import _process_start_token, _terminate_owned_process_group

                    current_start = _process_start_token(controller_pid)
                    if current_start and current_start != controller_start:
                        raise RuntimeError("controller PID no longer matches its persisted identity; mission remains STOPPING")
                    if current_start:
                        _terminate_owned_process_group(controller_pid, controller_start)
                # The lock, not the ready file, is the controller-liveness
                # authority. Do not claim STOPPED until ownership is released.
                deadline = time.monotonic() + 3.0
                released = False
                while time.monotonic() < deadline:
                    lock = MissionLock(state_dir() / "locks", args.mission)
                    try:
                        lock.acquire()
                    except BlockingIOError:
                        time.sleep(0.05)
                        continue
                    else:
                        lock.release()
                        released = True
                        break
                if not released:
                    raise RuntimeError("controller still owns the mission lock; mission remains STOPPING")
                with open_store() as store:
                    if store.get_mission(args.mission)["status"] == "STOPPING":
                        store.set_mission_status(args.mission, "STOPPED")
                    result = {"mission": store.get_mission(args.mission)}
            else:
                with open_store() as store:
                    current_status = store.get_mission(args.mission)["status"]
                if args.command == "stop" and current_status in {"COMPLETED", "FAILED", "CANCELLED", "STOPPED"}:
                    result = _stop_terminal_mission_station(args.mission)
                else:
                    with open_store() as store:
                        result = _apply_operator_command(store, args.mission, {"action": args.command})
                        if result.get("status") == "REJECTED" and not args.json:
                            print(f"llm-bridge: {(result.get('result') or {}).get('error', args.command + ' rejected')}", file=sys.stderr)
                            return 2
            _print_result(result, args.json, lambda value: fmt.format_command_receipt(args.command, value, args.mission))
            return 0
        if args.command == "theme":
            _print_theme(args)
            return 0
        if args.command == "doctor":
            with open_store() as store:
                mission = store.get_mission(args.mission) if args.mission else None
                providers = _doctor_provider_checks(mission)
                import shutil

                tmux_available = bool(shutil.which("tmux"))
                from . import bridge_doctor

                report = {
                    "schema_version": 1,
                    "state_dir": str(state_dir()),
                    "database": str(store.path),
                    "sqlite_integrity": store.db.execute("PRAGMA integrity_check").fetchone()[0],
                    "tmux_available": tmux_available,
                    "mission": mission,
                    "providers": providers,
                    "next_steps": bridge_doctor.next_steps(
                        tmux_available=tmux_available, providers=providers,
                        folder=(mission or {}).get("cwd") or Path.cwd()),
                }
            if not args.include_config:
                report = strip_frozen_config(report)
            _print_result(report, args.json, fmt.format_doctor)
            return 0
        if args.command == "station":
            with open_store() as store:
                result = _apply_operator_command(store, args.mission, {"action": "station.view", "station": args.name})
            if result.get("status") == "REJECTED":
                print(f"llm-bridge: {(result.get('result') or {}).get('error', 'station update rejected')}", file=sys.stderr)
                return 2
            _print_result(result, args.json, lambda value: fmt.format_command_receipt("station", value, args.mission))
            return 0
        if args.command == "rotation":
            result = _rotation_result(args)
            _print_result(result, args.json, fmt.format_rotation)
            return 0
        if args.command == "steer":
            prompt = read_verbatim(args.prompt_file) if args.prompt_file else str(args.prompt or "")
            with open_store() as store:
                named = args.task is not None or args.task_by_id is not None
                steer_task_id = int(_resolve_task(store, args.mission, args.task, args.task_by_id)["id"]) if named else None
                result = _apply_operator_command(store, args.mission, {
                    "action": "steer",
                    "kind": args.kind,
                    "task_id": steer_task_id,
                    "route": args.route,
                    "prompt": prompt,
                })
            if result.get("status") == "REJECTED":
                print(f"llm-bridge: {(result.get('result') or {}).get('error', 'steering rejected')}", file=sys.stderr)
                return 2
            _print_result(result, args.json, lambda value: fmt.format_command_receipt("steer", value, args.mission))
            return 0
        _build_parser().error(f"unsupported command {args.command}")
    except (ValueError, KeyError, OSError, RuntimeError, TransitionError) as exc:
        print(f"llm-bridge: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

