"""LLM Bridge CLI and command dispatch."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .bridge_store import BridgeStore, MissionLock, TransitionError, default_state_dir
from . import config as toolconfig


def state_dir() -> Path:
    return default_state_dir()


def open_store() -> BridgeStore:
    return BridgeStore(state_dir() / "bridge.sqlite3")


def read_verbatim(path: str | Path) -> str:
    if str(path) == "-":
        return sys.stdin.read()
    with Path(path).expanduser().open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def status_snapshot(mission_id: str, *, task_limit: int | None = None) -> dict[str, Any]:
    with open_store() as store:
        mission = store.get_mission(mission_id)
        counts = {str(row[0]): int(row[1]) for row in store.db.execute("SELECT status,COUNT(*) FROM tasks WHERE mission_id=? GROUP BY status", (mission_id,))}
        total_tasks = sum(counts.values())
        if task_limit is None:
            tasks = store.list_tasks(mission_id)
        else:
            rows = store.db.execute("SELECT * FROM tasks WHERE mission_id=? ORDER BY ordering,id LIMIT ?", (mission_id, max(0, task_limit)))
            tasks = [store._task_dict(row) for row in rows]
        operations = [
            dict(row)
            for row in store.db.execute(
                "SELECT id,task_id,iteration,station,route_id,model,status,exit_code,logs_path,created_at,updated_at FROM operations WHERE mission_id=? ORDER BY created_at DESC LIMIT 20",
                (mission_id,),
            )
        ]
        return {
            "schema_version": 1,
            "mission": mission,
            "tasks": tasks,
            "task_counts": counts,
            "task_total": total_tasks,
            "pending_commands": store.pending_commands(mission_id),
            "operations": operations,
            "events": store.events(mission_id, after=max(0, int(store.db.execute("SELECT COALESCE(MAX(id),0)-20 FROM events WHERE mission_id=?", (mission_id,)).fetchone()[0]))),
        }


def console_snapshot(mission_id: str) -> dict[str, Any]:
    """Bound the live control pane projection regardless of mission size."""
    return status_snapshot(mission_id, task_limit=200)


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _require_task(store: BridgeStore, mission_id: str, task_id: int) -> dict[str, Any]:
    task = store.get_task(task_id)
    if task["mission_id"] != mission_id:
        raise TransitionError(f"task {task_id} does not belong to mission {mission_id}")
    return task


def _apply_operator_command(store: BridgeStore, mission_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one validated command and persist its idempotent result."""
    command = store.enqueue_command(mission_id, payload)
    if command["status"] != "QUEUED":
        return command.get("result") or command
    with store._transaction():
        return _apply_operator_effect(store, mission_id, payload, command)


def _apply_operator_effect(store: BridgeStore, mission_id: str, payload: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", ""))
    try:
        result: dict[str, Any]
        if action == "pause":
            store.set_mission_status(mission_id, "PAUSED")
            active = store.db.execute("SELECT COUNT(*) FROM operations WHERE mission_id=? AND status IN ('INTENT','PREPARED','RUNNING')", (mission_id,)).fetchone()[0]
            result = {"mission_status": "PAUSED", "work_status": "DEFERRED" if active else "APPLIED"}
            if active:
                result["detail"] = "pause takes effect when the active provider returns"
        elif action == "resume":
            store.set_mission_status(mission_id, "RUNNING")
            result = {"mission_status": "RUNNING"}
        elif action == "stop":
            mission = store.get_mission(mission_id)
            if mission["status"] in {"RUNNING", "PAUSED"}:
                store.set_mission_status(mission_id, "STOPPING")
            elif mission["status"] == "CREATED":
                store.set_mission_status(mission_id, "CANCELLED")
            result = {"mission_status": store.get_mission(mission_id)["status"]}
        elif action == "cancel":
            active = store.db.execute("SELECT COUNT(*) FROM operations WHERE mission_id=? AND status IN ('INTENT','PREPARED','RUNNING')", (mission_id,)).fetchone()[0]
            mission = store.get_mission(mission_id)
            if mission["status"] not in {"COMPLETED", "CANCELLED"}:
                store.set_mission_status(mission_id, "CANCELLED")
            for task in store.list_tasks(mission_id):
                if task["status"] not in {"COMPLETED", "CANCELLED"}:
                    store.set_task_status(int(task["id"]), "CANCELLED")
            result = {"mission_status": "CANCELLED", "work_status": "DEFERRED" if active else "APPLIED"}
            if active:
                result["detail"] = "cancel is durable; the active provider returns at its boundary"
        elif action == "task.verify":
            task_id = int(payload["task_id"])
            _require_task(store, mission_id, task_id)
            result = store.verify_task(task_id, evidence=str(payload.get("evidence", "")), approved=bool(payload.get("approved", False)))
        elif action == "task.add":
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
        elif action == "task.amend":
            task_id = int(payload["task_id"])
            _require_task(store, mission_id, task_id)
            revision = store.amend_task(task_id, prompt=str(payload["prompt"]))
            result = {"task_id": task_id, "revision": revision}
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
                store.ready_tasks(mission_id)
                result = store.get_task(task_id)
            elif action == "task.cancel":
                result = store.set_task_status(task_id, "CANCELLED")
            elif action == "task.retry":
                result = store.set_task_status(task_id, "QUEUED")
                store.ready_tasks(mission_id)
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
        return store.finish_command(command["id"], status="APPLIED", result=result)
    except (ValueError, KeyError, OSError, TransitionError) as exc:
        return store.finish_command(command["id"], status="REJECTED", result={"error": str(exc)})


def dispatch_console_command(mission_id: str, command_line: str) -> str:
    """Dispatch one shell-free control-pane command through shared handlers."""
    try:
        words = shlex.split(command_line)
        if not words:
            return ""
        command, *args = words
        if command in {"help", "?"}:
            return "status | tasks | events | pause | resume | stop | verify ID [--approve] | add FILE | amend ID FILE | priority ID N | reorder ID N | dependencies ID DEP_ID | retry ID | cancel ID | control"
        if command == "status":
            return json.dumps(status_snapshot(mission_id), ensure_ascii=False)
        if command in {"tasks", "orders"}:
            return json.dumps(status_snapshot(mission_id)["tasks"], ensure_ascii=False)
        if command == "events":
            with open_store() as store:
                return json.dumps(store.events(mission_id, after=int(args[0]) if args else 0), ensure_ascii=False)
        if command in {"pause", "resume", "stop", "cancel"}:
            with open_store() as store:
                return json.dumps(_apply_operator_command(store, mission_id, {"action": command}), ensure_ascii=False)
        if command == "verify" and args:
            payload = {"action": "task.verify", "task_id": int(args[0]), "approved": "--approve" in args[1:]}
            evidence_arg = next((arg.split("=", 1)[1] for arg in args[1:] if arg.startswith("--evidence=")), "")
            payload["evidence"] = evidence_arg
            with open_store() as store:
                return json.dumps(_apply_operator_command(store, mission_id, payload), ensure_ascii=False)
        if command == "priority" and len(args) == 2:
            with open_store() as store:
                return json.dumps(_apply_operator_command(store, mission_id, {"action": "task.priority", "task_id": int(args[0]), "priority": int(args[1])}), ensure_ascii=False)
        if command == "reorder" and len(args) == 2:
            with open_store() as store:
                return json.dumps(_apply_operator_command(store, mission_id, {"action": "task.reorder", "task_id": int(args[0]), "order": int(args[1])}), ensure_ascii=False)
        if command == "dependencies" and len(args) == 2:
            with open_store() as store:
                return json.dumps(_apply_operator_command(store, mission_id, {"action": "task.dependency", "task_id": int(args[0]), "depends_on": int(args[1])}), ensure_ascii=False)
        if command == "retry" and args:
            with open_store() as store:
                return json.dumps(_apply_operator_command(store, mission_id, {"action": "task.retry", "task_id": int(args[0])}), ensure_ascii=False)
        if command == "cancel" and args:
            with open_store() as store:
                return json.dumps(_apply_operator_command(store, mission_id, {"action": "task.cancel", "task_id": int(args[0])}), ensure_ascii=False)
        if command == "add" and args:
            with open_store() as store:
                return json.dumps(_apply_operator_command(store, mission_id, {"action": "task.add", "prompt": read_verbatim(args[0])}), ensure_ascii=False)
        if command == "amend" and len(args) == 2:
            with open_store() as store:
                return json.dumps(_apply_operator_command(store, mission_id, {"action": "task.amend", "task_id": int(args[0]), "prompt": read_verbatim(args[1])}), ensure_ascii=False)
        return f"unknown command: {command}; type help"
    except (ValueError, KeyError, OSError, TransitionError) as exc:
        return f"error: {exc}"


def _read_prompt_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", "--prompt-file", dest="prompt_file")
    group.add_argument("--prompt")


def _verifier_config(args: argparse.Namespace) -> dict[str, Any] | None:
    argv = list(getattr(args, "verifier_argv", []) or [])
    if not argv:
        return None
    timeout = float(getattr(args, "verifier_timeout", 300))
    if timeout <= 0 or timeout > 3600:
        raise ValueError("verifier timeout must be between 0 and 3600 seconds")
    if any("\x00" in arg for arg in argv):
        raise ValueError("verifier argv contains a NUL byte")
    return {"argv": argv, "timeout_seconds": timeout}


def _frozen_tool_config() -> str:
    path = toolconfig.config_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-bridge", description="Terminal command bridge for persistent LLM engineering missions")
    sub = parser.add_subparsers(dest="command", required=True)

    mission = sub.add_parser("mission", help="create or list missions")
    mission_sub = mission.add_subparsers(dest="mission_action", required=True)
    create = mission_sub.add_parser("create", help="create a mission and its first task")
    _read_prompt_args(create)
    create.add_argument("--name")
    create.add_argument("--cwd", default=str(Path.cwd()))
    create.add_argument("--providers", default="claude,codex,opencode")
    create.add_argument("--routes", default="")
    create.add_argument("--headless", action="store_true")
    create.add_argument("--native", dest="native_tui", action="store_true", help="supervise the provider's native terminal UI")
    create.add_argument("--verifier-arg", dest="verifier_argv", action="append", default=[], help="verifier argv item; repeat for each item, supports {context}, {cwd}, {mission_id}, {task_id}, {revision}")
    create.add_argument("--verifier-timeout", type=float, default=300)
    create.add_argument("--command-template", help="Ralph/scheduler argv template for testing or custom launch")
    create.add_argument("--json", action="store_true")
    listing = mission_sub.add_parser("list")
    listing.add_argument("--json", action="store_true")

    status = sub.add_parser("status")
    status.add_argument("--mission", required=True)
    status.add_argument("--json", action="store_true")

    tasks = sub.add_parser("task")
    task_sub = tasks.add_subparsers(dest="task_action", required=True)
    add = task_sub.add_parser("add")
    add.add_argument("--mission", required=True)
    _read_prompt_args(add)
    add.add_argument("--title", default="")
    add.add_argument("--priority", type=int, default=0)
    add.add_argument("--order", type=int)
    add.add_argument("--depends-on", default="")
    add.add_argument("--acceptance", action="append", default=[])
    add.add_argument("--json", action="store_true")
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--mission", required=True)
    task_list.add_argument("--json", action="store_true")
    show = task_sub.add_parser("show")
    show.add_argument("--mission", required=True)
    show.add_argument("--task", type=int, required=True)
    show.add_argument("--json", action="store_true")
    amend = task_sub.add_parser("amend")
    amend.add_argument("--mission", required=True)
    amend.add_argument("--task", type=int, required=True)
    _read_prompt_args(amend)
    amend.add_argument("--json", action="store_true")
    for action in ("priority", "reorder", "dependencies", "pause", "resume", "cancel", "retry", "verify"):
        cmd = task_sub.add_parser(action)
        cmd.add_argument("--mission", required=True)
        cmd.add_argument("--task", type=int, required=True)
        if action == "priority":
            cmd.add_argument("--value", type=int, required=True)
        elif action == "reorder":
            cmd.add_argument("--value", type=int, required=True)
        elif action == "dependencies":
            cmd.add_argument("--add", type=int, required=True)
        elif action == "verify":
            cmd.add_argument("--evidence", default="")
            cmd.add_argument("--approve", action="store_true")
        cmd.add_argument("--json", action="store_true")

    events = sub.add_parser("events")
    events.add_argument("--mission", required=True)
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--json", action="store_true")

    for action in ("start", "pause", "resume", "stop", "cancel", "attach", "detach"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--mission", required=True)
        if action == "start":
            cmd.add_argument("--headless", action="store_true")
            cmd.add_argument("--native", dest="native_tui", action="store_true")
            cmd.add_argument("--providers")
            cmd.add_argument("--routes")
        if action == "stop":
            cmd.add_argument("--force", action="store_true")
        cmd.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--mission")
    doctor.add_argument("--json", action="store_true")

    console = sub.add_parser("console", help=argparse.SUPPRESS)
    console.add_argument("--mission", required=True)
    control = sub.add_parser("control", help="open the full Bridge control view")
    control.add_argument("--mission", required=True)

    controller = sub.add_parser("_controller", help=argparse.SUPPRESS)
    controller.add_argument("--mission", required=True)
    controller.add_argument("--headless", action="store_true")
    controller.add_argument("--native", dest="native_tui", action="store_true")
    controller.add_argument("--startup-token", default="")
    controller.add_argument("--providers", default="claude,codex,opencode")
    controller.add_argument("--routes", default="")
    return parser


def _legacy_prompt_mode(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="llm-bridge")
    _read_prompt_args(parser)
    parser.add_argument("--name")
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--providers", default="claude,codex,opencode")
    parser.add_argument("--routes", default="")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--native", dest="native_tui", action="store_true")
    parser.add_argument("--verifier-arg", dest="verifier_argv", action="append", default=[])
    parser.add_argument("--verifier-timeout", type=float, default=300)
    parser.add_argument("--command-template")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
    config: dict[str, Any] = {"providers": args.providers, "routes": args.routes, "headless": args.headless, "native_tui": args.native_tui, "frozen_tool_config": _frozen_tool_config()}
    if args.command_template:
        config["command_template"] = args.command_template
    verifier = _verifier_config(args)
    if verifier:
        config["verifier"] = verifier
    with open_store() as store:
        mission = store.create_mission(
            args.name or (prompt.splitlines()[0][:100] if prompt.splitlines() else "LLM Bridge mission"),
            prompt,
            cwd=args.cwd,
            initial_prompt=prompt,
            config=config,
        )
        result = {"mission": mission, "task": store.get_task(int(mission["initial_task_id"]))}
    if args.json or args.no_start:
        _json_print(result)
        return 0
    return start_mission(mission["id"], headless=args.headless, native_tui=args.native_tui, providers=args.providers, routes=args.routes)


def start_mission(mission_id: str, *, headless: bool = False, native_tui: bool = False, providers: str | None = None, routes: str | None = None) -> int:
    from .bridge_runtime import launch_controller

    return launch_controller(mission_id, headless=headless, native_tui=native_tui, providers=providers, routes=routes)


def _load_handle(mission_id: str):
    from .terminal import TmuxTerminalBackend

    runtime = state_dir() / "runtime" / f"mission-{mission_id}"
    return TmuxTerminalBackend().open_existing(
        socket_path=runtime / "tmux.sock",
        session_name=f"mission-{mission_id}",
        bridge_rows=6,
    )


def _print_result(result: Any, as_json: bool) -> None:
    if as_json:
        _json_print(result)
    elif isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"--prompt-file", "--file", "--prompt"}:
        try:
            return _legacy_prompt_mode(arguments)
        except (ValueError, KeyError, OSError, TransitionError) as exc:
            print(f"llm-bridge: {exc}", file=sys.stderr)
            return 2
    parser = _build_parser()
    args = parser.parse_args(arguments)
    try:
        if args.command == "_controller":
            from .bridge_runtime import run_controller

            return run_controller(args.mission, headless=args.headless, native_tui=args.native_tui, providers=args.providers, routes=args.routes, startup_token=args.startup_token)
        if args.command == "console":
            from .bridge_tui import run_console

            return run_console(args.mission, dispatch=dispatch_console_command, snapshot=console_snapshot)
        if args.command == "control":
            from .bridge_tui import run_control

            return run_control(args.mission, dispatch=dispatch_console_command, snapshot=console_snapshot)
        if args.command == "resume":
            with open_store() as store:
                mission = store.get_mission(args.mission)
                result = _apply_operator_command(store, args.mission, {"action": "resume"})
                config = mission["config"]
            lock = MissionLock(state_dir() / "locks", args.mission)
            try:
                lock.acquire()
            except BlockingIOError:
                if not args.json:
                    print(f"mission {args.mission} resumed on its active controller")
                if not config.get("headless") and config.get("native_tui") and sys.stdin.isatty() and sys.stdout.isatty():
                    from .terminal import TmuxTerminalBackend

                    handle = TmuxTerminalBackend().open_existing(
                        state_dir() / "runtime" / f"mission-{args.mission}" / "tmux.sock",
                        f"mission-{args.mission}",
                    )
                    return TmuxTerminalBackend().attach(handle)
                _print_result(result, args.json)
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
            return start_mission(
                args.mission,
                headless=bool(getattr(args, "headless", config.get("headless", False))),
                native_tui=bool(getattr(args, "native_tui", config.get("native_tui", False))),
                providers=getattr(args, "providers", None) or config.get("providers"),
                routes=getattr(args, "routes", None) or config.get("routes"),
            )
        if args.command == "attach":
            from .terminal import TmuxTerminalBackend

            return TmuxTerminalBackend().attach(_load_handle(args.mission))
        if args.command == "detach":
            handle = _load_handle(args.mission)
            proc = subprocess.run(["tmux", "-S", str(handle.socket_path), "detach-client", "-a", "-t", handle.session_name], check=False)
            return proc.returncode
        if args.command == "mission":
            with open_store() as store:
                if args.mission_action == "create":
                    prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
                    config: dict[str, Any] = {"providers": args.providers, "routes": args.routes, "headless": args.headless, "native_tui": args.native_tui, "frozen_tool_config": _frozen_tool_config()}
                    if args.command_template:
                        config["command_template"] = args.command_template
                    verifier = _verifier_config(args)
                    if verifier:
                        config["verifier"] = verifier
                    mission = store.create_mission(
                        args.name or (prompt.splitlines()[0][:100] if prompt.splitlines() else "LLM Bridge mission"),
                        prompt,
                        cwd=args.cwd,
                        initial_prompt=prompt,
                        config=config,
                    )
                    result = {"mission": mission, "task": store.get_task(int(mission["initial_task_id"]))}
                else:
                    result = {"missions": store.list_missions()}
            _print_result(result, bool(getattr(args, "json", False)))
            return 0
        if args.command == "status":
            result = status_snapshot(args.mission)
            _print_result(result, args.json)
            return 0
        if args.command == "events":
            with open_store() as store:
                result = {"schema_version": 1, "events": store.events(args.mission, after=args.after)[-max(0, args.limit):]}
            _print_result(result, args.json)
            return 0
        if args.command == "task":
            with open_store() as store:
                if args.task_action == "add":
                    prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
                    dep_ids = [int(x) for x in args.depends_on.split(",") if x.strip()]
                    command_result = _apply_operator_command(store, args.mission, {"action": "task.add", "prompt": prompt, "title": args.title, "priority": args.priority, "order": args.order, "depends_on": dep_ids, "acceptance": args.acceptance})
                    result = command_result.get("result") or command_result
                elif args.task_action == "list":
                    result = store.list_tasks(args.mission)
                elif args.task_action == "show":
                    result = _require_task(store, args.mission, args.task)
                else:
                    payload: dict[str, Any] = {"task_id": args.task}
                    if args.task_action == "amend":
                        prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
                        payload.update(action="task.amend", prompt=prompt)
                    elif args.task_action == "verify":
                        payload.update(action="task.verify", evidence=args.evidence, approved=args.approve)
                    elif args.task_action == "priority":
                        payload.update(action="task.priority", priority=args.value)
                    elif args.task_action == "reorder":
                        payload.update(action="task.reorder", order=args.value)
                    elif args.task_action == "dependencies":
                        payload.update(action="task.dependency", depends_on=args.add)
                    else:
                        payload["action"] = "task." + args.task_action
                    result = _apply_operator_command(store, args.mission, payload)
            if isinstance(result, dict) and result.get("status") == "REJECTED":
                detail = result.get("result") or {}
                print(f"llm-bridge: {detail.get('error', 'command rejected')}", file=sys.stderr)
                return 2
            _print_result(result, bool(getattr(args, "json", False)))
            return 0
        if args.command in {"pause", "stop", "cancel"}:
            if args.command == "stop" and args.force:
                with open_store() as store:
                    mission = store.get_mission(args.mission)
                    if mission["status"] in {"RUNNING", "PAUSED"}:
                        store.set_mission_status(args.mission, "STOPPING")
                    elif mission["status"] == "CREATED":
                        store.set_mission_status(args.mission, "CANCELLED")
                    operations = [
                        dict(row)
                        for row in store.db.execute(
                            "SELECT id,process_pid,process_start,pane_id FROM operations WHERE mission_id=? AND status IN ('INTENT','PREPARED','RUNNING')",
                            (args.mission,),
                        )
                    ]
                    store.reconcile_interrupted_operations(args.mission)
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
                            raise RuntimeError("no verifiable process identity; mission remains STOPPING")
                        _terminate_owned_process_group(int(process_pid), str(process_start))
                with open_store() as store:
                    if store.get_mission(args.mission)["status"] == "STOPPING":
                        store.set_mission_status(args.mission, "STOPPED")
                    result = {"mission": store.get_mission(args.mission)}
            else:
                with open_store() as store:
                    result = _apply_operator_command(store, args.mission, {"action": args.command})
            _print_result(result, args.json)
            return 0
        if args.command == "doctor":
            with open_store() as store:
                report = {
                    "schema_version": 1,
                    "state_dir": str(state_dir()),
                    "database": str(store.path),
                    "sqlite_integrity": store.db.execute("PRAGMA integrity_check").fetchone()[0],
                    "tmux_available": bool(shutil.which("tmux")),
                    "mission": store.get_mission(args.mission) if args.mission else None,
                }
            _print_result(report, args.json)
            return 0
        parser.error(f"unsupported command {args.command}")
    except (ValueError, KeyError, OSError, RuntimeError, TransitionError) as exc:
        print(f"llm-bridge: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

