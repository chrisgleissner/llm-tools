"""LLM Bridge CLI and command dispatch."""

from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .bridge_store import BridgeStore, MissionLock, TransitionError, default_state_dir
from . import config as toolconfig
from .terminal import TerminalAction

if TYPE_CHECKING:
    from .bridge_tui import ControlViewAction


def state_dir() -> Path:
    return default_state_dir()


def open_store() -> BridgeStore:
    return BridgeStore(state_dir() / "bridge.sqlite3")


def read_verbatim(path: str | Path) -> str:
    if str(path) == "-":
        return sys.stdin.read()
    with Path(path).expanduser().open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def status_snapshot(
    mission_id: str,
    *,
    task_limit: int | None = None,
    task_offset: int = 0,
    search: str = "",
    event_offset: int = 0,
    event_anchor: int | None = None,
) -> dict[str, Any]:
    from .bridge_monitor import project_snapshot

    with open_store() as store:
        return project_snapshot(store, mission_id, task_limit=task_limit, task_offset=task_offset, search=search, event_offset=event_offset, event_anchor=event_anchor)


def console_snapshot(
    mission_id: str,
    *,
    task_limit: int = 200,
    task_offset: int = 0,
    search: str = "",
    event_offset: int = 0,
    event_anchor: int | None = None,
    view: str = "Bridge",
) -> dict[str, Any]:
    """Bound the live control pane projection regardless of mission size."""
    result = status_snapshot(mission_id, task_limit=task_limit, task_offset=task_offset, search=search, event_offset=event_offset, event_anchor=event_anchor)
    if view == "Usage":
        from .bridge_usage import embedded_usage
        result["usage"] = embedded_usage.snapshot()
    return result


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
    try:
        with store._transaction():
            result = _apply_operator_effect(store, mission_id, payload)
            return store.finish_command(command["id"], status="APPLIED", result=result)
    except _RejectedOperatorCommand as exc:
        with store._transaction():
            return store.finish_command(command["id"], status="REJECTED", result={"error": str(exc)})


class _RejectedOperatorCommand(Exception):
    """A validation failure that must be acknowledged after rolling back effects."""


def _apply_operator_effect(store: BridgeStore, mission_id: str, payload: dict[str, Any]) -> dict[str, Any]:
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
                result = {"kind": kind, "task_id": task_id, "revision": revision, "status": "QUEUED_FOR_BOUNDARY"}
            elif kind == "review":
                review_prompt = str(payload.get("prompt", "")).strip() or f"Review task {task_id} ({task['title']}) after it completes. Inspect its artifacts and acceptance criteria, then report findings and any required follow-up."
                review_task = store.add_task(
                    mission_id,
                    review_prompt,
                    title=f"Review: {task['title']}",
                    priority=int(task["priority"]) + 1,
                    depends_on=[task_id],
                    acceptance=["Record review findings and identify any unmet acceptance criteria."],
                )
                result = {"kind": kind, "task_id": review_task, "depends_on": task_id, "status": "APPLIED"}
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
        return result
    except (ValueError, KeyError, OSError, TransitionError) as exc:
        raise _RejectedOperatorCommand(str(exc)) from exc


def _task_result(args: argparse.Namespace, *, preserve_receipt: bool = False) -> Any:
    """Shared standalone/interactive task parser result and durable handler."""
    with open_store() as store:
        if args.task_action == "add":
            prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
            dep_ids = [int(x) for x in args.depends_on.split(",") if x.strip()]
            command_result = _apply_operator_command(store, args.mission, {"action": "task.add", "prompt": prompt, "title": args.title, "priority": args.priority, "order": args.order, "depends_on": dep_ids, "acceptance": args.acceptance})
            result = command_result if preserve_receipt or command_result.get("status") == "REJECTED" else (command_result.get("result") or command_result)
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
    return result


class _ConsoleParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(f"{message}. Use {self.prog} --help for the required arguments.")


def dispatch_console_command(mission_id: str, command_line: str) -> str | TerminalAction | ControlViewAction:
    """Run the public CLI grammar in this mission's shell-free REPL context."""
    try:
        words = shlex.split(command_line)
        if not words:
            return ""
        command = words[0]
        if command in {"help", "?"}:
            from .bridge_help import topic_text

            if len(words) > 1:
                if words[1] in {"task", "mission", "start", "attach", "pause", "resume", "stop", "cancel", "verify"}:
                    parser = _build_parser(parser_class=_ConsoleParser, default_mission=mission_id)
                    output = io.StringIO()
                    try:
                        with redirect_stdout(output):
                            parser.parse_args([*words[1:], "--help"])
                    except SystemExit:
                        pass
                    return output.getvalue() or topic_text(" ".join(words[1:]))
                return topic_text(" ".join(words[1:]))
            return topic_text("quickstart") + "\n\n" + _build_parser().format_help()

        if command in {"control", "usage"} and "--help" not in words and "-h" not in words:
            from .bridge_tui import ControlViewAction

            parser = _build_parser(parser_class=_ConsoleParser, default_mission=mission_id)
            args = parser.parse_args(words)
            return ControlViewAction(args.mission, "Usage" if command == "usage" else "Bridge")

        # Keep the old compact commands while routing every action through the
        # exact same parser and durable command handlers as the standalone CLI.
        if command in {"attach", "detach"}:
            if "--help" in words or "-h" in words:
                return f"Usage: {command} [--mission ID] [--client /dev/pts/N]"
            args = _build_parser(parser_class=_ConsoleParser, default_mission=mission_id).parse_args(words)
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
            if len(shlex.split(command_line)) > 3:
                reason = " ".join(shlex.split(command_line)[3:])
                words.extend(["--reason", reason])
        elif command == "add" and len(words) == 2:
            words = ["task", "add", "--file", words[1]]
        elif command == "amend" and len(words) == 3:
            words = ["task", "amend", "--task", words[1], "--file", words[2]]
        elif command in {"priority", "reorder"} and len(words) == 3:
            words = ["task", command, "--task", words[1], "--value", words[2]]
        elif command == "dependencies" and len(words) == 3:
            words = ["task", "dependencies", "--task", words[1], "--add", words[2]]
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
        if "--help" not in words and "-h" not in words and words[0] not in {"control", "console", "_controller"} and "--json" not in words:
            words.append("--json")
        args = None
        if "--help" not in words and "-h" not in words:
            args = _build_parser(parser_class=_ConsoleParser, default_mission=mission_id).parse_args(words)
            if getattr(args, "prompt_file", None) == "-":
                return "error: press Ctrl-E with these options to enter multiline text; the console cannot read its own terminal as a pipe"
        if words[0] == "task" and args is not None:
            result = _task_result(args, preserve_receipt=True)
            if isinstance(result, dict) and result.get("status") == "REJECTED":
                return "error: " + str((result.get("result") or {}).get("error", "command rejected"))
            return json.dumps(result, ensure_ascii=False, indent=2)
        if args is not None and getattr(args, "mission", None):
            words.extend(["--mission", args.mission])
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, env.get("PYTHONPATH", ""))))
        result = subprocess.run(
            [sys.executable, "-m", "llm_tools.bridge", *words],
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
            detail = f"{status} · use attach to open the provider" if not mission["config"].get("headless") else f"{status} · headless work; inspect status"
            return json.dumps({"status": "APPLIED", "result": {"detail": detail, "mission_id": args.mission}})
        if result.stdout.strip():
            return result.stdout.strip()
        if result.stderr.strip():
            return result.stderr.strip()
        return f"command exited with status {result.returncode}"
    except subprocess.TimeoutExpired:
        return "error: command acknowledgement timed out; outcome unknown. Check `llm-bridge status` before retrying."
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
    path = toolconfig.config_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _build_parser(*, parser_class: type[argparse.ArgumentParser] = argparse.ArgumentParser, default_mission: str | None = None) -> argparse.ArgumentParser:
    parser = parser_class(prog="llm-bridge", description="Terminal command bridge for persistent LLM engineering missions")
    sub = parser.add_subparsers(dest="command", required=True)
    help_parser = sub.add_parser("help", help="show offline Bridge workflow and recovery guidance")
    help_parser.add_argument("topic", nargs="*", help="quickstart, keys, prompts, markdown, providers, states, verification, recovery, or a command path")
    view = sub.add_parser("view", help="read a local text artifact without starting work")
    view.add_argument("--file", required=True, help="UTF-8 text artifact to open; source stays unchanged")
    view.add_argument("--follow", action="store_true", help="follow changes explicitly; Up pauses, End resumes")

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
    create.add_argument("--log-max-bytes", type=int, default=4 * 1024 * 1024)
    create.add_argument("--log-rotations", type=int, default=3)
    create.add_argument("--log-retained-runs", type=int, default=20)
    create.add_argument("--capacity-refresh-seconds", type=int, default=60)
    create.add_argument("--command-template", help="Ralph/scheduler argv template for testing or custom launch")
    create.add_argument("--json", action="store_true")
    listing = mission_sub.add_parser("list")
    listing.add_argument("--json", action="store_true")

    status = sub.add_parser("status")
    status.add_argument("--mission", required=default_mission is None, default=default_mission)
    status.add_argument("--json", action="store_true")

    tasks = sub.add_parser("task")
    task_sub = tasks.add_subparsers(dest="task_action", required=True)
    add = task_sub.add_parser("add")
    add.add_argument("--mission", required=default_mission is None, default=default_mission)
    _read_prompt_args(add)
    add.add_argument("--title", default="")
    add.add_argument("--priority", type=int, default=0)
    add.add_argument("--order", type=int)
    add.add_argument("--depends-on", default="")
    add.add_argument("--acceptance", action="append", default=[])
    add.add_argument("--json", action="store_true")
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--mission", required=default_mission is None, default=default_mission)
    task_list.add_argument("--json", action="store_true")
    show = task_sub.add_parser("show")
    show.add_argument("--mission", required=default_mission is None, default=default_mission)
    show.add_argument("--task", type=int, required=True)
    show.add_argument("--json", action="store_true")
    amend = task_sub.add_parser("amend")
    amend.add_argument("--mission", required=default_mission is None, default=default_mission)
    amend.add_argument("--task", type=int, required=True)
    _read_prompt_args(amend)
    amend.add_argument("--json", action="store_true")
    for action in ("priority", "reorder", "dependencies", "pause", "resume", "cancel", "retry", "verify"):
        cmd = task_sub.add_parser(action)
        cmd.add_argument("--mission", required=default_mission is None, default=default_mission)
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

    proposals = sub.add_parser("proposal", help="review agent-originated task proposals")
    proposal_sub = proposals.add_subparsers(dest="proposal_action", required=True)
    proposal_create = proposal_sub.add_parser("create", help="record a proposal from an operation")
    proposal_create.add_argument("--mission", required=default_mission is None, default=default_mission)
    proposal_create.add_argument("--operation", required=True, dest="source_operation_id")
    _read_prompt_args(proposal_create)
    proposal_create.add_argument("--title", default="")
    proposal_create.add_argument("--priority", type=int, default=0)
    proposal_create.add_argument("--depends-on", default="")
    proposal_create.add_argument("--acceptance", action="append", default=[])
    proposal_create.add_argument("--json", action="store_true")
    proposal_list = proposal_sub.add_parser("list", help="list proposals")
    proposal_list.add_argument("--mission", required=default_mission is None, default=default_mission)
    proposal_list.add_argument("--status", choices=("PENDING", "ACCEPTED", "REJECTED", "ALL"), default="PENDING")
    proposal_list.add_argument("--limit", type=int, default=100)
    proposal_list.add_argument("--after", type=int, default=0)
    proposal_list.add_argument("--json", action="store_true")
    for proposal_action in ("accept", "reject"):
        proposal_decision = proposal_sub.add_parser(proposal_action, help=f"{proposal_action} a pending proposal")
        proposal_decision.add_argument("--mission", required=default_mission is None, default=default_mission)
        proposal_decision.add_argument("--task", type=int, required=True)
        if proposal_action == "reject":
            proposal_decision.add_argument("--reason", default="")
        proposal_decision.add_argument("--json", action="store_true")

    events = sub.add_parser("events")
    events.add_argument("--mission", required=default_mission is None, default=default_mission)
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--json", action="store_true")

    for action in ("start", "pause", "resume", "stop", "cancel", "attach", "detach"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--mission", required=default_mission is None, default=default_mission)
        if action in {"attach", "detach"}:
            client_help = (
                "select a listed writable source client when switching from the Bridge console"
                if action == "attach"
                else "select a writable client to detach; required only when multiple are connected"
            )
            cmd.add_argument("--client", help=client_help)
        if action == "start":
            cmd.add_argument("--headless", action="store_true", default=None)
            cmd.add_argument("--native", dest="native_tui", action="store_true", default=None)
            cmd.add_argument("--providers")
            cmd.add_argument("--routes")
        if action == "stop":
            cmd.add_argument("--force", action="store_true")
        cmd.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--mission", default=default_mission)
    doctor.add_argument("--json", action="store_true")

    console = sub.add_parser("console", help=argparse.SUPPRESS)
    console.add_argument("--mission", required=default_mission is None, default=default_mission)
    control = sub.add_parser("control", help="open the full Bridge control view")
    control.add_argument("--mission", required=default_mission is None, default=default_mission)
    usage_view = sub.add_parser("usage", help="show the full llm-usage dashboard inside Bridge")
    usage_view.add_argument("--mission", required=default_mission is None, default=default_mission)

    station = sub.add_parser("station", help="choose the station shown in the control view")
    station.add_argument("--mission", required=default_mission is None, default=default_mission)
    station.add_argument("name")
    station.add_argument("--json", action="store_true")

    steer = sub.add_parser("steer", help="queue typed steering for a task or safe boundary")
    steer.add_argument("--mission", required=default_mission is None, default=default_mission)
    steer.add_argument("--kind", required=True, choices=("replace", "review", "verify", "pause-after-iteration", "stop-after-task", "handoff"))
    steer.add_argument("--task", dest="task_id", type=int)
    steer.add_argument("--route")
    steer.add_argument("--file", "--prompt-file", dest="prompt_file")
    steer.add_argument("--prompt")
    steer.add_argument("--json", action="store_true")

    controller = sub.add_parser("_controller", help=argparse.SUPPRESS)
    controller.add_argument("--mission", required=default_mission is None, default=default_mission)
    controller.add_argument("--headless", action="store_true")
    controller.add_argument("--native", dest="native_tui", action="store_true")
    controller.add_argument("--startup-token", default="")
    controller.add_argument("--providers", default="claude,codex,opencode")
    controller.add_argument("--routes", default="")
    from .bridge_help import decorate_parser
    decorate_parser(parser)
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
    parser.add_argument("--log-max-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--log-rotations", type=int, default=3)
    parser.add_argument("--log-retained-runs", type=int, default=20)
    parser.add_argument("--capacity-refresh-seconds", type=int, default=60)
    parser.add_argument("--command-template")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
    config: dict[str, Any] = {"providers": args.providers, "routes": args.routes, "headless": args.headless, "native_tui": args.native_tui, "frozen_tool_config": _frozen_tool_config(), **_log_retention_config(args)}
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


def start_mission(mission_id: str, *, headless: bool | None = None, native_tui: bool | None = None, providers: str | None = None, routes: str | None = None) -> int:
    from .bridge_runtime import launch_controller

    return launch_controller(mission_id, headless=headless, native_tui=native_tui, providers=providers, routes=routes)


def _load_handle(mission_id: str):
    from .bridge_runtime import mission_socket_path
    from .terminal import TmuxTerminalBackend

    return TmuxTerminalBackend().open_existing(
        socket_path=mission_socket_path(mission_id),
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


def _stop_terminal_mission_station(mission_id: str) -> dict[str, Any]:
    """Close a retained station after a terminal mission has released control."""
    lock = MissionLock(state_dir() / "locks", mission_id)
    try:
        lock.acquire()
    except BlockingIOError as exc:
        raise RuntimeError(f"mission {mission_id} still has an active controller; stop is pending") from exc
    try:
        with open_store() as store:
            mission = store.get_mission(mission_id)
            if mission["status"] not in {"COMPLETED", "FAILED", "CANCELLED", "STOPPED"}:
                raise RuntimeError(f"mission {mission_id} is no longer terminal; retry stop")
            from .bridge_runtime import mission_socket_path
            from .terminal import TmuxTerminalBackend

            socket = mission_socket_path(mission_id)
            station_status = "ABSENT"
            if socket.exists():
                backend = TmuxTerminalBackend()
                handle = backend.open_existing(socket, f"mission-{mission_id}")
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


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        if sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "") not in {"", "dumb"}:
            from .bridge_launcher import run_launcher

            try:
                return run_launcher(main)
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"llm-bridge: {exc}", file=sys.stderr)
                return 2
        _build_parser().print_help()
        return 0
    if arguments[0] == "help" and not any(arg in {"--help", "-h"} for arg in arguments[1:]):
        from .bridge_help import topic_text

        print(topic_text(" ".join(arguments[1:]) or "quickstart"))
        return 0
    if arguments and arguments[0] in {"--prompt-file", "--file", "--prompt"}:
        try:
            return _legacy_prompt_mode(arguments)
        except (ValueError, KeyError, OSError, TransitionError) as exc:
            print(f"llm-bridge: {exc}", file=sys.stderr)
            return 2
    parser = _build_parser()
    args = parser.parse_args(arguments)
    try:
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
                result = _apply_operator_command(store, args.mission, {"action": "resume"})
                config = mission["config"]
            lock = MissionLock(state_dir() / "locks", args.mission)
            try:
                lock.acquire()
            except BlockingIOError:
                if not args.json:
                    print(f"mission {args.mission} resumed on its active controller")
                if not config.get("headless") and config.get("native_tui") and sys.stdin.isatty() and sys.stdout.isatty():
                    from .bridge_runtime import mission_socket_path
                    from .terminal import TmuxTerminalBackend

                    handle = TmuxTerminalBackend().open_existing(
                        mission_socket_path(args.mission),
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
            requested_headless = getattr(args, "headless", None)
            requested_native = getattr(args, "native_tui", None)
            if requested_headless is True:
                requested_native = False
            elif requested_native is True:
                requested_headless = False
            return start_mission(
                args.mission,
                headless=requested_headless,
                native_tui=requested_native,
                providers=getattr(args, "providers", None) or config.get("providers"),
                routes=getattr(args, "routes", None) or config.get("routes"),
            )
        if args.command == "attach":
            if args.client:
                print("llm-bridge: --client is only used by attach from an active Bridge console", file=sys.stderr)
                return 2
            from .terminal import TmuxTerminalBackend

            return TmuxTerminalBackend().attach(_load_handle(args.mission))
        if args.command == "detach":
            handle = _load_handle(args.mission)
            from .terminal import TmuxTerminalBackend

            TmuxTerminalBackend().detach_client(handle, client_name=args.client)
            return 0
        if args.command == "mission":
            with open_store() as store:
                if args.mission_action == "create":
                    prompt = read_verbatim(args.prompt_file) if args.prompt_file else args.prompt
                    config: dict[str, Any] = {"providers": args.providers, "routes": args.routes, "headless": args.headless, "native_tui": args.native_tui, "frozen_tool_config": _frozen_tool_config(), **_log_retention_config(args)}
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
                result = {"schema_version": 1, "events": store.events(args.mission, after=args.after, limit=args.limit)}
            _print_result(result, args.json)
            return 0
        if args.command == "proposal":
            with open_store() as store:
                if args.proposal_action == "list":
                    result = {
                        "proposals": store.list_proposals(
                            args.mission, status=args.status, limit=args.limit, after_task_id=args.after,
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
                            "depends_on": [int(value) for value in args.depends_on.split(",") if value.strip()],
                            "acceptance": args.acceptance,
                        }
                    else:
                        payload = {
                            "action": "task.proposal-decision",
                            "task_id": args.task,
                            "accept": args.proposal_action == "accept",
                            "reason": getattr(args, "reason", ""),
                        }
                    result = _apply_operator_command(store, args.mission, payload)
            if isinstance(result, dict) and result.get("status") == "REJECTED":
                detail = result.get("result") or {}
                print(f"llm-bridge: {detail.get('error', 'proposal command rejected')}", file=sys.stderr)
                return 2
            _print_result(result, bool(getattr(args, "json", False)))
            return 0
        if args.command == "task":
            result = _task_result(args)
            if isinstance(result, dict) and result.get("status") == "REJECTED":
                detail = result.get("result") or {}
                print(f"llm-bridge: {detail.get('error', 'command rejected')}", file=sys.stderr)
                return 2
            _print_result(result, bool(getattr(args, "json", False)))
            return 0
        if args.command in {"pause", "stop", "cancel"}:
            if args.command == "stop" and args.force:
                runtime = state_dir() / "runtime" / f"mission-{args.mission}"
                with open_store() as store:
                    mission = store.get_mission(args.mission)
                    if mission["status"] in {"RUNNING", "PAUSED"}:
                        store.set_mission_status(args.mission, "STOPPING")
                    elif mission["status"] == "CREATED":
                        store.set_mission_status(args.mission, "CANCELLED")
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
        if args.command == "station":
            with open_store() as store:
                result = _apply_operator_command(store, args.mission, {"action": "station.view", "station": args.name})
            if result.get("status") == "REJECTED":
                print(f"llm-bridge: {(result.get('result') or {}).get('error', 'station update rejected')}", file=sys.stderr)
                return 2
            _print_result(result, args.json)
            return 0
        if args.command == "steer":
            prompt = read_verbatim(args.prompt_file) if args.prompt_file else str(args.prompt or "")
            with open_store() as store:
                result = _apply_operator_command(store, args.mission, {
                    "action": "steer",
                    "kind": args.kind,
                    "task_id": args.task_id,
                    "route": args.route,
                    "prompt": prompt,
                })
            if result.get("status") == "REJECTED":
                print(f"llm-bridge: {(result.get('result') or {}).get('error', 'steering rejected')}", file=sys.stderr)
                return 2
            _print_result(result, args.json)
            return 0
        parser.error(f"unsupported command {args.command}")
    except (ValueError, KeyError, OSError, RuntimeError, TransitionError) as exc:
        print(f"llm-bridge: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

