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
from typing import Any, Callable, NoReturn, Sequence, TYPE_CHECKING, cast

from . import bridge_commands as commands
from . import bridge_format as fmt
from .bridge_format import NATIVE_STOP_HINT
from .bridge_store import BridgeStore, MissionLock, SchemaTooNew, TaskNotFound, TransitionError, default_state_dir, derive_mission_name, derive_title, mission_transition_message, prune_old_drafts, rotation_label, store_path
from .bridge_store import controller_running as _controller_running
from .bridge_states import (
    ACTIVE_OPERATION_STATUSES, ENDED_MISSION_STATUSES, FINISHED_MISSION_STATUSES, FINISHED_TASK_STATUSES,
    STARTED_MISSION_STATUSES,
)
from .bridge_states import MISSION_COUNT, MISSION_HUE_NAMES, THEME_CHOICES
from .bridge_vocab import RunMode
from .bridge_missions import (  # noqa: F401  (re-exported: the CLI and tests reach these through bridge)
    MIN_MISSION_PREFIX,
    MissionAmbiguous,
    MissionNotFound,
    NoMissionInFolder,
    mission_choices as _mission_choices,
    mission_live as _mission_live,
    order_missions,
    resolve_mission_ref,
    station_live as _station_live,
)

if TYPE_CHECKING:
    import argparse

    from .bridge_cli_parser import (  # noqa: F401  (loaded on first use through ``_LAZY_ATTRIBUTES``)
        _FastParseFailed,
        _OnlyCommand,
        _RotationDefaultText,
        _SkippedParser,
        _add_json_arg,
        _add_mission_arg,
        _add_mission_create_args,
        _build_parser,
        _cli_parser_class,
        _close_names,
        _console_parser_class,
        _disable_abbreviations,
        _parse_arguments,
        _parse_arguments_core,
    )
    from .bridge_cli_worker import (  # noqa: F401  (loaded on first use through ``_LAZY_ATTRIBUTES``)
        WINDOWS_SUPPORTED_USAGE,
        _coordinator_url,
        _format_worker_status,
        _refuse_on_windows,
        _remote_command,
        _split_target,
        _worker_command,
        unsupported_on_windows,
    )
    from .bridge_cli_print import (  # noqa: F401  (loaded on first use through ``_LAZY_ATTRIBUTES``)
        _events_command,
        _print_missions,
        _print_result,
        _print_task_evidence,
        _proposal_page,
        _proposal_text,
        _task_text,
    )

    from .bridge_draw import ControlViewAction
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
    "WINDOWS_SUPPORTED_USAGE": ("llm_tools.bridge_cli_worker", "WINDOWS_SUPPORTED_USAGE"),
    "_FastParseFailed": ("llm_tools.bridge_cli_parser", "_FastParseFailed"),
    "_OnlyCommand": ("llm_tools.bridge_cli_parser", "_OnlyCommand"),
    "_RotationDefaultText": ("llm_tools.bridge_cli_parser", "_RotationDefaultText"),
    "_SkippedParser": ("llm_tools.bridge_cli_parser", "_SkippedParser"),
    "_add_json_arg": ("llm_tools.bridge_cli_parser", "_add_json_arg"),
    "_add_mission_arg": ("llm_tools.bridge_cli_parser", "_add_mission_arg"),
    "_add_mission_create_args": ("llm_tools.bridge_cli_parser", "_add_mission_create_args"),
    "_build_parser": ("llm_tools.bridge_cli_parser", "_build_parser"),
    "_cli_parser_class": ("llm_tools.bridge_cli_parser", "_cli_parser_class"),
    "_close_names": ("llm_tools.bridge_cli_parser", "_close_names"),
    "_console_parser_class": ("llm_tools.bridge_cli_parser", "_console_parser_class"),
    "_coordinator_url": ("llm_tools.bridge_cli_worker", "_coordinator_url"),
    "_disable_abbreviations": ("llm_tools.bridge_cli_parser", "_disable_abbreviations"),
    "_events_command": ("llm_tools.bridge_cli_print", "_events_command"),
    "_format_worker_status": ("llm_tools.bridge_cli_worker", "_format_worker_status"),
    "_parse_arguments": ("llm_tools.bridge_cli_parser", "_parse_arguments"),
    "_parse_arguments_core": ("llm_tools.bridge_cli_parser", "_parse_arguments_core"),
    "_print_missions": ("llm_tools.bridge_cli_print", "_print_missions"),
    "_print_result": ("llm_tools.bridge_cli_print", "_print_result"),
    "_print_task_evidence": ("llm_tools.bridge_cli_print", "_print_task_evidence"),
    "_proposal_page": ("llm_tools.bridge_cli_print", "_proposal_page"),
    "_proposal_text": ("llm_tools.bridge_cli_print", "_proposal_text"),
    "_refuse_on_windows": ("llm_tools.bridge_cli_worker", "_refuse_on_windows"),
    "_remote_command": ("llm_tools.bridge_cli_worker", "_remote_command"),
    "_split_target": ("llm_tools.bridge_cli_worker", "_split_target"),
    "_task_text": ("llm_tools.bridge_cli_print", "_task_text"),
    "_worker_command": ("llm_tools.bridge_cli_worker", "_worker_command"),
    "unsupported_on_windows": ("llm_tools.bridge_cli_worker", "unsupported_on_windows"),
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
        from .bridge_cli_parser import _console_parser_class

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


def _os_reason(exc: OSError) -> str:
    """Why a file operation failed, in words (``no such file``), not the errno text."""
    if isinstance(exc, FileNotFoundError):
        return "no such file"
    if isinstance(exc, IsADirectoryError):
        return "it is a folder"
    if isinstance(exc, NotADirectoryError):
        return "a folder in the path is a file"
    if isinstance(exc, PermissionError):
        return "permission denied"
    return (exc.strerror or type(exc).__name__).lower()


def read_verbatim(path: str | Path, option: str = "--file") -> str:
    """The text of ``path`` (``-`` is standard input); a failure names the option and the reason."""
    if str(path) == "-":
        return sys.stdin.read()
    try:
        with Path(path).expanduser().open("r", encoding="utf-8", newline="") as stream:
            return stream.read()
    except OSError as exc:
        raise ValueError(f"cannot read {option} {path}: {_os_reason(exc)}") from exc
    except UnicodeDecodeError:
        raise ValueError(f"cannot read {option} {path}: it is not UTF-8 text") from None


def prompt_from(args: argparse.Namespace) -> str:
    """The prompt text from ``--file``/``--prompt-file`` or ``--prompt``; a blank prompt is refused with its source."""
    path = getattr(args, "prompt_file", None)
    prompt = read_verbatim(path) if path else getattr(args, "prompt", None)
    if prompt is None or not str(prompt).strip():
        source = " (read from standard input)" if path == "-" else f" (read from {path})" if path else ""
        raise ValueError(f"the prompt is empty{source}")
    return str(prompt)


def controller_running(mission_id: str) -> bool:
    """Whether a live process holds this mission's controller lock; an unreadable lock raises."""
    return _controller_running(mission_id)  # type: ignore[return-value]  # the default on_error raises, so the result is a bool


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
    from .bridge_projection import ProjectionParams

    params = ProjectionParams(task_limit, task_offset, search, event_offset, event_anchor, prompt_chars)
    with open_store(readonly=True) as store:
        snapshot = project_snapshot(store, mission_id, params=params)
    lifecycle = snapshot.get("lifecycle")
    if isinstance(lifecycle, dict):
        lifecycle["controller_running"] = controller_running(mission_id)
    mission = snapshot.get("mission")
    if isinstance(mission, dict):
        from .bridge_policy import effective_settings

        mission["execution_policy"] = effective_settings(mission.get("config"))
    return cast("dict[str, Any]", snapshot)  # a TypedDict is a plain dict at run time


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
        from .bridge_usage import embedded_usage, rotation_providers
        embedded_usage.set_rotation(rotation_providers(result))
        result["usage"] = embedded_usage.snapshot()
    if view == "Agents":
        from .bridge_agents import build_agent_map

        with open_store(readonly=True) as store:
            result["agent_map"] = build_agent_map(store)
    return result


_with_task_numbers = commands.with_task_numbers  # shared with the remote client so both print the same JSON


def _json_print(value: Any, *, compact: bool = False) -> None:
    """Print ``value`` as sorted JSON; ``compact`` is one line, for streams such as ``events --follow``."""
    value = _with_task_numbers(value)
    if compact:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
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


# Exit status (CLIUX-016). Every command uses this small set; `llm-bridge --help` and
# docs/llm-bridge/operations.md list it. `--target` requests keep the remote client's own
# status 3 for "the target did not answer" (bridge_remote.EXIT_UNAVAILABLE). The text is
# bridge_help.EXIT_STATUS_TEXT.
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_WRONG_STATE = 0, 1, 2, 3, 4


class WrongState(RuntimeError):
    """The mission is not in a state that allows the command (a station that is not running, a controller that is)."""


def _split_hint(message: str) -> tuple[str, str]:
    """Split ``message`` into what went wrong and the command that fixes it, when it names one."""
    first, newline, rest = message.partition("\n")
    for separator in ("; ", " · "):
        head, found, tail = first.partition(separator)
        if found and "llm-bridge" in tail:
            return head + newline + rest, tail
    return message, ""


def _fail(message: str, code: str, exit_status: int, as_json: bool) -> int:
    """Report an error: ``llm-bridge: MESSAGE`` and a ``see:`` line on stderr, or with ``--json`` an error object on stdout.

    The ``see:`` line and the JSON ``help`` field name the troubleshooting entry for ``code``
    (``bridge_help.troubleshooting_reference``).
    """
    from .bridge_help import error_text, troubleshooting_reference

    if as_json:
        text, hint = _split_hint(message)
        error = {"code": code, "message": text, "hint": hint, "help": troubleshooting_reference(code)}
        print(json.dumps({"error": error}, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(error_text(message, code), file=sys.stderr)
    return exit_status


def _classify_error(exc: BaseException) -> tuple[str, int]:
    """The error code and exit status for an exception raised by a command."""
    if isinstance(exc, MissionAmbiguous):
        return "mission-ambiguous", EXIT_NOT_FOUND
    if isinstance(exc, (MissionNotFound, NoMissionInFolder)):
        return "mission-not-found", EXIT_NOT_FOUND
    if isinstance(exc, TaskNotFound):
        return "task-not-found", EXIT_NOT_FOUND
    if isinstance(exc, commands.CommandFailure):
        if exc.code == "not-found":
            return "not-found", EXIT_NOT_FOUND
        return exc.code, EXIT_USAGE if exc.code.startswith("invalid") else EXIT_FAILED
    if isinstance(exc, (TransitionError, WrongState)):
        return "wrong-state", EXIT_WRONG_STATE
    if isinstance(exc, SchemaTooNew):
        return "schema-too-new", EXIT_WRONG_STATE
    if isinstance(exc, KeyError):
        return "not-found", EXIT_NOT_FOUND
    if isinstance(exc, ValueError):
        return "invalid-argument", EXIT_USAGE
    return "failed", EXIT_FAILED


def _fail_with(exc: BaseException, as_json: bool) -> int:
    text = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
    code, status = _classify_error(exc)
    return _fail(text, code, status, as_json)


_REJECTION_CAUSES: dict[str, BaseException | None] = {}


def _fail_rejected(result: dict[str, Any], default: str, as_json: bool) -> int:
    """Report a command the store rejected: the exit status follows the exception that rejected it.

    A refused transition is "wrong state" (4), a task or mission that is not there is "not found"
    (3), an argument the effect could not use is a usage error (2).
    """
    detail = result.get("result") or {}
    message = str(detail.get("error", default))
    cause = _REJECTION_CAUSES.pop(str(result.get("id")), None)
    if (cause is not None and not isinstance(cause, TransitionError)) or isinstance(cause, TaskNotFound):
        code, status = _classify_error(cause)  # type: ignore[arg-type]
        return _fail(message, code, status, as_json)
    return _fail(message, "conflict" if detail.get("code") == "conflict" else "wrong-state", EXIT_WRONG_STATE, as_json)


TASK_LIST_PAGE = 200
MISSION_LIST_PAGE = 100
BUILTIN_PROVIDERS = "claude,codex,opencode"
RUN_MODES = tuple(str(mode) for mode in RunMode)


def _is_read_only(args: argparse.Namespace) -> bool:
    """Commands that only read state may resolve a folder's finished mission."""
    command = getattr(args, "command", "")
    if command in {"status", "events", "doctor", "agents"}:
        return True
    if command == "task":
        return getattr(args, "task_action", "") in {"list", "show", "output", "diff"}
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
    attribute = "agent_mission" if getattr(args, "command", "") == "agents" else "mission"
    if not hasattr(args, attribute):
        return
    reference = getattr(args, attribute)
    if reference is None and (optional or attribute == "agent_mission"):
        return
    try:
        with open_store(readonly=True) as store:
            resolved = resolve_mission_ref(
                store, reference,
                read_only=_is_read_only(args),
                prefer_station=getattr(args, "command", "") in {"attach", "detach"},
                reopen_completed=getattr(args, "command", "") == "task" and getattr(args, "task_action", "") == "add",
                station_command=getattr(args, "command", "") in {"attach", "detach", "finish", "station"},
            )
            setattr(args, attribute, resolved)
    except MissionNotFound:
        if not lenient:
            raise


def _require_task(store: BridgeStore, mission_id: str, task_id: int) -> dict[str, Any]:
    """The task with this global ID, if it belongs to the mission (the advanced ``--task-id`` form)."""
    try:
        task = store.get_task(task_id)
    except KeyError:
        raise TaskNotFound(f"no task has the global ID {task_id}; use --task N with the number shown by task list") from None
    if task["mission_id"] != mission_id:
        raise TaskNotFound(f"global task ID {task_id} belongs to another mission, not {mission_id[:8]}; use --task N with the number shown by task list")
    return task


def _resolve_task(store: BridgeStore, mission_id: str, number: int | None, by_id: int | None) -> dict[str, Any]:
    """The task a command names: ``--task N`` is the mission's own number, ``--task-id`` the global ID."""
    if by_id is not None:
        return _require_task(store, mission_id, int(by_id))
    if number is None:
        raise ValueError("name the task with --task N (the number shown by task list)")
    return store.task_by_ordinal(mission_id, int(number))


def _after_task_target(store: BridgeStore, mission_id: str, number: int) -> dict[str, Any]:
    """The task ``stop --after-task [N]`` waits for: task N, or the task that is running now."""
    if number:
        task = store.task_by_ordinal(mission_id, number)
    else:
        running = store.db.execute("SELECT id FROM tasks WHERE mission_id=? AND status='RUNNING' ORDER BY ordering,id LIMIT 1", (mission_id,)).fetchone()
        if running is None:
            raise ValueError("no task is running now; name the task to wait for, e.g. llm-bridge stop --after-task 1")
        task = store.get_task(int(running[0]))
    if task["status"] in {"COMPLETED", "CANCELLED"}:
        raise ValueError(f"task {task['ordinal']} is already {str(task['status']).lower()}; there is nothing to wait for (use llm-bridge stop)")
    return dict(task)


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


_EVENT_AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _event_age(text: str) -> int:
    """argparse type for ``--since``: ``90s``, ``10m``, ``2h``, ``1d`` or a run such as ``1h30m``, in seconds."""
    import argparse

    cleaned = text.strip().lower().replace(" ", "")
    parts = re.findall(r"(\d{1,9})([smhd])", cleaned)
    if not parts or "".join(f"{count}{unit}" for count, unit in parts) != cleaned:
        raise argparse.ArgumentTypeError(f"invalid age {text[:24]!r}; use a number and a unit, for example 90s, 10m, 2h or 1d")
    return sum(int(count) * _EVENT_AGE_UNITS[unit] for count, unit in parts)


def _db_int(text: str) -> int:
    """argparse type for a number the database stores: SQLite integers are 64-bit, and a larger one is an OverflowError."""
    import argparse

    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if not -(2 ** 63) <= value < 2 ** 63:
        raise argparse.ArgumentTypeError(f"{text[:24]!r} is outside the range of whole numbers Bridge can store")
    return value


def _positional_into(dest: str) -> type[argparse.Action]:
    """The action of an optional positional that is an alternative spelling of ``--option``: it fills ``dest``.

    A positional with ``nargs="?"`` is matched once with no value when it is absent, and a plain
    store would then overwrite what the option set; this one leaves the namespace alone.
    """
    import argparse

    class _StorePositional(argparse.Action):
        def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: Any, option_string: str | None = None) -> None:
            if values is not None:
                setattr(namespace, dest, values)

    return _StorePositional


def _add_task_selector(parser: argparse.ArgumentParser, *, required: bool = True, noun: str = "task", note: str = "") -> None:
    """``--task N`` or a positional ``N`` (the mission's own number) and the advanced ``--task-id`` (global ID)."""
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--task", "-t", metavar="N", dest="task", type=_db_int, help=f"{noun} number within the mission, as shown by task list{note}")
    group.add_argument("--task-id", metavar="ID", dest="task_by_id", type=_db_int, help=f"advanced: the {noun}'s global ID, unique across all missions (task JSON field id)")
    group.add_argument("task_number", nargs="?", metavar="N", type=_db_int, action=_positional_into("task"), help=f"the {noun} number, same as --task N")


def _apply_operator_command(store: BridgeStore, mission_id: str, payload: dict[str, Any], *, command_id: str | None = None) -> dict[str, Any]:
    """Apply one validated command and persist its idempotent result.

    ``command_id`` is the caller's durable request ID; a repeated delivery of
    the same ID returns the stored outcome without repeating the effect.
    """
    command = store.enqueue_command(mission_id, payload, command_id=command_id)
    if command["status"] != "QUEUED":
        return command  # duplicate delivery: the stored outcome, same shape as the first reply
    try:
        with store._transaction():
            # Another delivery of the same ID may have applied it since enqueue;
            # the write lock is held now, so this check cannot race.
            current = store.get_command(command["id"])
            if current["status"] != "QUEUED":
                return current
            result = _apply_operator_effect(store, mission_id, payload)
            finished = store.finish_command(command["id"], status="APPLIED", result=result)
    except _RejectedOperatorCommand as exc:
        rejection = {"error": str(exc)}
        if exc.code:
            rejection["code"] = exc.code
        # Not part of the stored result (the wire format stays as it was): this process's command line
        # uses it to pick the exit status, which depends on what kind of problem was rejected.
        _REJECTION_CAUSES[command["id"]] = exc.__cause__
        with store._transaction():
            return store.finish_command(command["id"], status="REJECTED", result=rejection)
    if payload.get("action") == "cancel":
        _end_cancelled_providers(store, mission_id, finished)
    return finished


def _end_cancelled_providers(store: BridgeStore, mission_id: str, finished: dict[str, Any]) -> None:
    """After a mission cancel is durable, end the provider processes it left running (INT-018).

    Uses the same identity-checked termination as ``stop --force``; a process whose identity was never
    recorded, or that cannot be ended, is left alone and named in the receipt.
    """
    from .bridge_runtime import _process_start_token, _terminate_owned_process_group, provider_operations_to_check

    remaining = 0
    for operation in provider_operations_to_check(store, mission_id):
        if operation["status"] not in ACTIVE_OPERATION_STATUSES:
            continue
        pid, start = operation.get("process_pid"), operation.get("process_start")
        if not pid or not start:
            remaining += 1
        elif _process_start_token(int(pid)) == str(start):
            try:
                _terminate_owned_process_group(int(pid), str(start))
            except (OSError, RuntimeError, TimeoutError):  # PermissionError is an OSError
                remaining += 1
    result = finished.get("result")
    if remaining and isinstance(result, dict):
        result["detail"] = f"cancel is durable; a provider run could not be ended automatically · end it with: llm-bridge stop --force --mission {fmt.short_id(mission_id)}"


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

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class _StaleRevision(TransitionError):
    """The caller's expected task revision no longer matches (PROTO-006)."""


def _check_expected_revision(store: BridgeStore, mission_id: str, payload: dict[str, Any]) -> None:
    """Reject a task mutation made against an outdated revision; runs under the write lock."""
    expected = payload.get("expected_revision")
    if expected is None:
        return
    current = int(_require_task(store, mission_id, int(payload["task_id"]))["revision"])
    if int(expected) != current:
        raise _StaleRevision(f"task revision is {current}, not the expected {int(expected)}; reload and retry")


def _apply_operator_effect(store: BridgeStore, mission_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", ""))
    try:
        _check_expected_revision(store, mission_id, payload)
        result: dict[str, Any]
        if action == "pause":
            current = str(store.get_mission(mission_id)["status"])
            if current not in STARTED_MISSION_STATUSES or current == "PAUSED":
                return {"mission_status": current, "noop": True, "detail": fmt.nothing_to_pause(current, mission_id)}
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
            orphaned = mission["status"] in STARTED_MISSION_STATUSES and not controller_running(mission_id)
            if orphaned:
                # Nobody is left to turn STOPPING into STOPPED, so the stop is final here. A provider run
                # the dead controller left behind is only ended by `stop --force`; say so.
                store.set_mission_status(mission_id, "STOPPED")
            elif mission["status"] in STARTED_MISSION_STATUSES:
                store.set_mission_status(mission_id, "STOPPING")
            mission = store.get_mission(mission_id)
            result = {"mission_status": mission["status"]}
            if orphaned and store.db.execute(
                "SELECT 1 FROM operations WHERE mission_id=? AND status IN ('INTENT','PREPARED','RUNNING') LIMIT 1", (mission_id,)
            ).fetchone():
                result["detail"] = f"the controller is gone and a provider run may still be active · end it with: llm-bridge stop --force --mission {fmt.short_id(mission_id)}"
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
                result["detail"] = "cancel is durable; the active provider run is being ended"
        elif action == "task.verify":
            task_id = int(payload["task_id"])
            verified = _require_task(store, mission_id, task_id)
            approved = bool(payload.get("approved", False))
            if not approved and not str(payload.get("evidence", "")).strip():
                raise ValueError(fmt.verify_needs_message(verified.get("ordinal", task_id), mission_id))
            # Without --approve, evidence is recorded and the task keeps awaiting approval.
            result = store.verify_task(task_id, evidence=str(payload.get("evidence", "")), approved=approved, record_only=not approved)
        elif action == "task.reject":
            task_id = int(payload["task_id"])
            _require_task(store, mission_id, task_id)
            reason = str(payload.get("reason", ""))
            result = {**store.reject_task(task_id, reason), "rejection_reason": reason.strip()}
            if payload.get("hold"):
                # Same transaction: the controller never sees the task as runnable.
                result = {**store.set_task_status(task_id, "PAUSED"), "rejection_reason": reason.strip(), "held": True}
            else:
                result["controller_running"] = controller_running(mission_id)
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
            task_id = int(payload["task_id"]) if payload.get("task_id") is not None else None  # type: ignore[assignment]  # name reused across the branches of this handler
            if kind in {"replace", "review", "verify", "stop-after-task"} and task_id is None:
                prompt_example = ' --prompt "check error paths"' if kind in {"replace", "review"} else ""
                raise ValueError(f"--kind {kind} needs --task N, e.g. llm-bridge steer --kind {kind} --task 1{prompt_example}")
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
                station = str(payload.get("route") or "").strip()
                mission = store.get_mission(mission_id)
                configured_names = [item.strip() for key in ("providers", "routes") for item in str(mission["config"].get(key) or "").split(",") if item.strip()]
                choices = f" (one of: {', '.join(dict.fromkeys(configured_names))})" if configured_names else ""
                if not station:
                    example = configured_names[0] if configured_names else "codex"
                    raise ValueError(f"--kind handoff needs --to PROVIDER{choices}, e.g. llm-bridge steer --kind handoff --to {example}")
                if configured_names and station not in configured_names:
                    raise ValueError(f"--to {station} is not in this mission's rotation{choices}")
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
            _attach_delivery(store, mission_id, task_id, result)
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
            awaiting = _require_task(store, mission_id, task_id)
            requeue = bool(payload.get("requeue", False))
            if awaiting["status"] == "VERIFYING" and not requeue:
                # Approving afterwards would mark a revision complete that no provider ever worked on.
                raise ValueError(fmt.amend_in_review_message(awaiting.get("ordinal", task_id), mission_id))
            revision = store.amend_task(task_id, prompt=str(payload["prompt"]))
            result = {"task_id": task_id, "revision": revision, "applied": not store.get_task(task_id)["pending"]}
            if awaiting["status"] == "VERIFYING":
                result["requeued"] = True
                result["controller_running"] = controller_running(mission_id)
            else:
                _attach_delivery(store, mission_id, task_id, result)
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
                result = {**store.get_task(task_id), "runs_after": store.runs_after(task_id)}
            elif action == "task.reorder":
                store.reorder_task(task_id, int(payload["order"]))
                result = {**store.get_task(task_id), "runs_after": store.runs_after(task_id)}
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
    except _StaleRevision as exc:
        raise _RejectedOperatorCommand(str(exc), code="conflict") from exc
    except (ValueError, KeyError, OSError, TransitionError) as exc:
        raise _RejectedOperatorCommand(str(exc)) from exc


def _attach_delivery(store: BridgeStore, mission_id: str, task_id: int, result: dict[str, Any]) -> None:
    """Add when the provider will see an added or amended task: ``delivery`` for clients, ``detail`` for the console."""
    from .bridge_task_notice import task_delivery

    try:
        live = controller_running(mission_id)
    except OSError:
        live = False
    delivery = task_delivery(store, mission_id, task_id, controller_live=live)
    if delivery is not None:
        result["delivery"] = delivery
        result.setdefault("detail", delivery["text"])


def _task_result(args: argparse.Namespace, *, preserve_receipt: bool = False) -> Any:
    """Shared standalone/interactive task parser result and durable handler."""
    with open_store(readonly=args.task_action in {"list", "show", "output", "diff"}) as store:
        if args.task_action == "add":
            prompt = prompt_from(args)
            dep_ids = _dependency_ids(store, args.mission, args.depends_on)
            command_result = commands.dispatch(commands.CommandRequest("task.add", {"prompt": prompt, "title": args.title, "priority": args.priority, "order": args.order, "depends_on": dep_ids, "acceptance": list(args.acceptance)}, mission_id=args.mission))
            result = command_result if preserve_receipt or command_result.get("status") == "REJECTED" else (command_result.get("result") or command_result)
        elif args.task_action == "list":
            if getattr(args, "all", False):
                result = fmt.TaskPage(store.list_tasks(args.mission, offset=max(0, args.offset)))
            else:
                result = fmt.TaskPage(store.list_tasks(args.mission, limit=max(0, args.limit), offset=max(0, args.offset)))
            result.total = store.task_counters(args.mission)["total"]
            result.offset = max(0, args.offset)
        elif args.task_action == "show":
            task_id = int(_resolve_task(store, args.mission, args.task, args.task_by_id)["id"])
            result = commands.dispatch(commands.CommandRequest("task.show", {"task_id": task_id}, mission_id=args.mission))
        elif args.task_action in {"output", "diff"}:
            task_id = int(_resolve_task(store, args.mission, args.task, args.task_by_id)["id"])
            result = commands.dispatch(commands.CommandRequest(f"task.{args.task_action}", {"task_id": task_id}, mission_id=args.mission))
        else:
            task = _resolve_task(store, args.mission, args.task, args.task_by_id)
            payload: dict[str, Any] = {"task_id": int(task["id"])}
            if args.task_action == "amend":
                payload.update(action="task.amend", prompt=prompt_from(args), requeue=bool(args.requeue))
            elif args.task_action == "verify":
                payload.update(action="task.verify", evidence=args.evidence, approved=args.approve)
            elif args.task_action == "approve":
                payload.update(action="task.verify", evidence=args.evidence, approved=True)
            elif args.task_action == "reject":
                payload.update(action="task.reject", reason=args.reason, hold=bool(args.hold))
            elif args.task_action == "priority":
                payload.update(action="task.priority", priority=args.value)
            elif args.task_action == "reorder":
                payload.update(action="task.reorder", order=args.value)
            elif args.task_action == "dependencies":
                payload.update(action="task.dependency", depends_on_number=args.add)
            else:
                payload["action"] = "task." + args.task_action
            params = {name: value for name, value in payload.items() if name != "action"}
            result = commands.dispatch(commands.CommandRequest(str(payload["action"]), params, mission_id=args.mission))
    return result


_CONSOLE_ONLY_COMMANDS = ("approve", "reject", "tasks", "orders", "proposals", "quit", "exit")








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
                words += ["--to", rest.split(None, 1)[0]]
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
            hint = f"tasks awaiting review: {', '.join(map(str, ids))}" if ids else "no task awaits review"
            return None, f"error: reject needs a task number · {hint}"
        task_id = str(ids[0])
    reason = " ".join(rest).strip()
    if not reason:
        return None, "error: usage: reject [TASK-NUMBER] REASON"
    if reason.startswith("/") or re.match(r"\d+/[A-Za-z]", reason):
        return None, "error: usage: reject [TASK-NUMBER] REASON · a reason cannot start with '/' (that looks like a command)"
    return ["task", "reject", "--task", task_id, f"--reason={reason}"], None


# Console commands that only read state and print text meant for people.
# The control pane's own readers (opening a task for editing) pass --json.
_CONSOLE_HUMAN_COMMANDS = frozenset({("status",), ("events",), ("theme",), ("doctor",), ("rotation",), ("missions",), ("mission", "list"), ("proposal", "list"), ("task", "list"), ("task", "show"), ("task", "output"), ("task", "diff")})


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
    from .bridge_cli_parser import _build_parser, _console_parser_class

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
            return topic_text("console")

        if command == "missions" and len(words) == 1:
            return TerminalAction(mission_id, None, "pick")
        if command == "leave":
            # Console name for "leave the provider terminal"; the shell command `detach` removes another terminal.
            words = ["detach", *words[1:]]
            command = "detach"
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
            from .bridge_draw import ControlViewAction

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
            return TerminalAction(str(args.mission), getattr(args, "client", None), command)  # type: ignore[arg-type]  # command is attach or detach in this branch
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
                    hint = f"tasks awaiting review: {', '.join(map(str, waiting))}" if waiting else "no task awaits review"
                    return f"error: approve needs a task number · {hint}"
                approve_task = str(waiting[0])
            words = ["task", "approve", "--task", approve_task]
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
        if words[0] == "cancel" and "--help" not in words and "-h" not in words and "--yes" not in words:
            words.append("--yes")  # the console asked first (its confirmation dialog); the subprocess has no terminal to ask on
        if "--help" not in words and "-h" not in words and words[0] not in {"control", "console", "_controller"} and "--json" not in words and not human:
            words.append("--json")
        args = None  # type: ignore[assignment]  # stays None when only help was asked for; later branches read args
        if "--help" not in words and "-h" not in words:
            args = _build_parser(parser_class=_console_parser_class(), default_mission=mission_id).parse_args(words)
            _resolve_parsed_mission(args)
            if getattr(args, "prompt_file", None) == "-":
                return "error: press Ctrl-E with these options to enter multiline text; the console cannot read its own terminal as a pipe"
        if words[0] == "task" and args is not None:
            result = _task_result(args, preserve_receipt=True)
            if isinstance(result, dict) and result.get("status") == "REJECTED":
                return "error: " + str((result.get("result") or {}).get("error", "command rejected"))
            if human and words[1] in {"output", "diff"} and isinstance(result, dict):
                return str(result["text"]) if result.get("available") else f"error: {result.get('message')}"
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
        env["LLM_BRIDGE_SURFACE"] = "console"  # the child words its next steps as slash commands (bridge_wording)
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
            # A deliberate suspend can include an awake wait after an early
            # resume. Keep its owner alive to finish the shared wake ledger.
            timeout=None if words[0] == "suspend-until-credits" else 15,
        )
        # `doctor` exits 1 to report "no provider can run"; its report is the answer, not an error.
        reported = words[0] == "doctor" and result.returncode == 1 and bool(result.stdout.strip())
        if result.returncode and not reported:
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


def resolved_default_rotation() -> tuple[str, str]:
    """The (providers, routes) a new mission gets with no ``--providers``/``--routes``, for help text.

    This is ``creation_rotation(None, None)`` itself, so help names exactly what creation resolves
    (providers the config disables are already removed); when nothing is usable the configured
    default is shown and creation reports the problem.
    """
    try:
        return creation_rotation(None, None)
    except ValueError:
        return default_rotation()


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
    conf = toolconfig.load_config() if resolved_routes else {}
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


def _read_prompt_args(parser: argparse.ArgumentParser, noun: str = "task", *, required: bool = True) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--file", "--prompt-file", dest="prompt_file", metavar="PATH", help=f"read the {noun} prompt from PATH; '-' reads standard input")
    group.add_argument("--prompt", help=f"the {noun} prompt as literal text; use --file for long prompts")






def _verifier_config(args: argparse.Namespace) -> dict[str, Any] | None:
    argv = list(getattr(args, "verifier_argv", []) or [])
    command = getattr(args, "verifier_command", None)
    after_dashes = list(getattr(args, "verifier_after_dashes", []) or [])
    if sum(bool(form) for form in (argv, command is not None, after_dashes)) > 1:
        raise ValueError("give one verifier form: --verifier COMMAND, -- COMMAND ARGS, or --verifier-arg once per argument")
    if command is not None:
        try:
            argv = shlex.split(command)
        except ValueError:
            raise ValueError(f"--verifier has an unclosed quote: {command}") from None
        if not argv:
            raise ValueError("--verifier is empty; give the command that checks a finished task, for example --verifier 'python -m pytest -q'")
    elif after_dashes:
        argv = after_dashes
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




















def _doctor_provider_checks(mission: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Install and sign-in checks for the providers a mission, or a new mission, would launch."""
    from . import bridge_doctor

    if mission is None:
        # Without a mission, check what the home form offers: every enabled provider, and every declared route.
        from . import bridge_launcher

        return bridge_doctor.provider_checks(bridge_launcher.offered_entries())
    config = mission["config"]
    names = fmt.rotation_names(config)
    conf = _mission_tool_config(config)
    routes_mode = bool(fmt.rotation_names({"routes": config.get("routes")}))
    entries: list[tuple[str, str | None]] = []
    for position, name in enumerate(names, 1):
        entry = _rotation_entry(conf, name, position, routes_mode=routes_mode)
        entries.append((name, entry["launch_cli"] if routes_mode else name))
    return bridge_doctor.provider_checks(entries)


def _mission_needs_you(mission: dict[str, Any] | None) -> str | None:
    """The mission's own next step, as `status` shows it under "Needs you"; None without a mission or a waiting action."""
    if not mission:
        return None
    snapshot = status_snapshot(str(mission["id"]))
    attention = snapshot.get("needs_you") or fmt.needs_attention(snapshot)
    if attention and attention.get("kind") != "capacity":
        return str(attention["message"])
    return None


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


def _resolve_mode_args(args: argparse.Namespace) -> None:
    """Fold ``--mode`` and the older ``--native`` / ``--headless`` flags into one choice.

    Afterwards ``args.headless`` and ``args.native_tui`` hold the chosen mode, and ``args.mode`` is the
    explicit choice or ``None``. On ``start`` an unspecified choice leaves both ``None`` (keep the saved
    mode); on ``mission create`` it leaves both ``False`` (Background).
    """
    headless, native = bool(getattr(args, "headless", None)), bool(getattr(args, "native_tui", None))
    if headless and native:
        raise ValueError("--headless and --native cannot be combined; use --mode native, background or headless")
    flag = "headless" if headless else ("native" if native else None)
    mode = getattr(args, "mode", None)
    if mode and flag and mode != flag:
        raise ValueError(f"--mode {mode} cannot be combined with --{flag}; use --mode alone")
    chosen = mode or flag
    args.mode = chosen
    if chosen is not None:
        args.headless, args.native_tui = chosen == "headless", chosen == "native"


CANCEL_UNDONE = "This cannot be undone"


def _confirm_cancel(args: argparse.Namespace, reference: str, name: str = "") -> bool:
    """Ask before `cancel` ends a mission for good; scripts without a terminal must pass ``--yes``.

    Returns False when nothing should be cancelled; the reason is already printed.
    """
    if getattr(args, "yes", False):
        return True
    label = f"{reference} \"{name}\"" if name else reference
    question = f"Cancel mission {label} and its unfinished tasks? {CANCEL_UNDONE}; the mission cannot be resumed."
    if sys.stdin.isatty() and sys.stderr.isatty():
        print(f"{question} Type y to cancel it, anything else keeps it: ", end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer in {"y", "yes"}:
            return True
        print(f"Kept mission {label}; nothing was cancelled", file=sys.stderr)
        return False
    from .bridge_help import error_text

    hint = f"{question} Pass --yes to confirm when there is no terminal to ask on, for example: llm-bridge cancel --mission {reference} --yes"
    print(error_text(hint, "usage"), file=sys.stderr)
    return False


DELETE_UNDONE = "This cannot be undone"


def _confirm_delete(args: argparse.Namespace, reference: str, name: str = "") -> bool:
    """Ask before `mission delete` removes a mission for good; scripts without a terminal must pass ``--yes``.

    Returns False when nothing should be deleted; the reason is already printed.
    """
    if getattr(args, "yes", False):
        return True
    label = f"{reference} \"{name}\"" if name else reference
    question = f"Delete mission {label} with its tasks, events and logs? {DELETE_UNDONE}."
    if sys.stdin.isatty() and sys.stderr.isatty():
        print(f"{question} Type y to delete it, anything else keeps it: ", end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer in {"y", "yes"}:
            return True
        print(f"Kept mission {label}; nothing was deleted", file=sys.stderr)
        return False
    from .bridge_help import error_text

    hint = f"{question} Pass --yes to confirm when there is no terminal to ask on, for example: llm-bridge mission delete {reference} --yes"
    print(error_text(hint, "usage"), file=sys.stderr)
    return False


def _mission_delete_command(args: argparse.Namespace) -> int:
    """`mission delete` on this machine: refuse a live mission first, then confirm, then delete."""
    from .bridge_cli_print import _print_result

    from .bridge_states import DELETABLE_MISSION_STATUSES
    from .bridge_store import mission_delete_refusal

    with open_store(readonly=True) as store:
        saved = store.get_mission(args.mission)
    status = str(saved["status"])
    if status not in DELETABLE_MISSION_STATUSES:
        raise TransitionError(mission_delete_refusal(args.mission, status))
    if not _confirm_delete(args, fmt.short_id(args.mission), fmt.clipped_name(saved.get("name", ""))):
        return EXIT_USAGE
    result = commands.dispatch(commands.CommandRequest("mission.delete", {"confirm": True, "force": bool(getattr(args, "force", False))}, mission_id=args.mission))
    _print_result(result, bool(getattr(args, "json", False)), lambda value: value["detail"])
    return 0


def _warn_not_signed_in(providers: Any, routes: Any, conf: dict[str, Any] | None = None) -> None:
    """Warn on stderr about rotation entries that are installed but not signed in; never fails."""
    from . import bridge_rotation
    from . import config as toolconfig

    if conf is None:
        conf = toolconfig.load_config() if str(routes or "").strip() else {}
    for line in bridge_rotation.signin_warnings(conf, providers, routes):
        print(line, file=sys.stderr)


def _warn_existing_folder_mission(store: BridgeStore, args: argparse.Namespace) -> None:
    """Warn on stderr when the folder already has an unfinished mission; ``--new`` silences it."""
    if getattr(args, "new", False):
        return
    from .bridge_missions import unfinished_in_folder

    rows = unfinished_in_folder(store, args.cwd)
    if not rows:
        return
    newest = rows[0]
    short = str(newest["id"])[:8]
    more = f" (and {len(rows) - 1} more)" if len(rows) > 1 else ""
    print(
        f"warning: this folder already has unfinished mission {short} \"{fmt.printable(str(newest['name'])[:40])}\" ({newest['status']}){more}; "
        f"llm-bridge task add --mission {short} --prompt \"...\" adds to it · pass --new to create another mission here without this warning",
        file=sys.stderr,
    )


def _mission_creation_inputs(args: argparse.Namespace, *, auto_continue: bool) -> tuple[str, dict[str, Any]]:
    """Validate the create options and build the objective text and mission config.

    Normalises ``args.cwd``, ``args.providers`` and ``args.routes`` in place.
    Raises ``ValueError`` for conflicting modes or an unusable folder before any
    store is opened, so a rejected command leaves no database behind.
    """
    from .bridge_templates import prepare_args

    auto_continue = prepare_args(args, auto_continue)
    prompt = prompt_from(args)
    _resolve_mode_args(args)
    args.cwd = _validated_cwd(args.cwd)
    args.providers, args.routes = creation_rotation(args.providers, args.routes)
    _warn_not_signed_in(args.providers, args.routes)
    from . import bridge_policy, config as toolconfig

    policy = bridge_policy.defaults_from_tool_config(toolconfig.load_config())
    if args.teams is not None:
        policy["team_policy_enabled"] = args.teams == "on"
    if args.token_spend is not None:
        policy["token_spend"] = args.token_spend
    config: dict[str, Any] = {"providers": args.providers, "routes": args.routes, "headless": args.headless, "native_tui": args.native_tui, "frozen_tool_config": _frozen_tool_config(), **policy, **_log_retention_config(args)}
    if args.command_template:
        config["command_template"] = args.command_template
        config["command_template_providers"] = args.providers
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
    name = args.name or derive_mission_name(prompt, "LLM Bridge mission")
    def create(cwd: str, config: dict[str, Any], **extra: Any) -> dict[str, Any]:
        if getattr(args, "template_tasks", None):
            from .bridge_templates import create_template_mission

            return create_template_mission(store, args, name, prompt, cwd, config, **extra)
        return store.create_mission(name, prompt, cwd=cwd, initial_prompt=prompt, initial_acceptance=args.acceptance, config=config, **extra)

    if getattr(args, "isolate", "none") != "worktree":
        mission = create(args.cwd, config)
        return {"mission": mission, "task": store.get_task(int(mission["initial_task_id"]))}
    import uuid

    from . import bridge_isolation

    mission_id = uuid.uuid4().hex
    try:
        facts = bridge_isolation.prepare(args.cwd, mission_id=mission_id, name=name, state_dir=default_state_dir())
    except bridge_isolation.IsolationError as exc:
        raise TransitionError(str(exc)) from None
    verifier = config.get("verifier")
    if isinstance(verifier, dict) and verifier.get("argv"):
        # The fingerprints were taken in the source folder; the controller checks them in the worktree.
        from .bridge_verifier_guard import verifier_fingerprints

        fingerprints = verifier_fingerprints([str(item) for item in verifier["argv"]], facts["cwd"], [str(item) for item in getattr(args, "verifier_files", []) or []])
        config = {**config, "verifier": {key: value for key, value in verifier.items() if key != "fingerprints"}}
        if fingerprints:
            config["verifier"]["fingerprints"] = fingerprints
    try:
        mission = create(facts["cwd"], config, mission_id=mission_id, isolation=facts)
    except BaseException:
        bridge_isolation.discard(facts["origin_cwd"], facts["worktree_path"], facts["branch"])
        raise
    if "claude" in str(config.get("providers") or "") and not config.get("trust_folder"):
        print("note: Claude Code asks once to trust the new worktree folder (Bridge does not edit Claude Code's trust settings); "
              "choose \"Yes, I trust this folder\", or create the mission with --trust-folder.", file=sys.stderr)
    return {"mission": mission, "task": store.get_task(int(mission["initial_task_id"]))}


def _legacy_prompt_mode(argv: list[str]) -> int:
    from .bridge_cli_parser import _add_json_arg, _add_mission_create_args

    import argparse

    parser = argparse.ArgumentParser(prog="llm-bridge", allow_abbrev=False)
    _read_prompt_args(parser, "mission objective and first task")
    _add_mission_create_args(parser, legacy=True)
    _add_json_arg(parser)
    args = parser.parse_args(argv)
    # The bare form has always ignored --auto-continue; only `mission create` honours it.
    prompt, config = _mission_creation_inputs(args, auto_continue=False)
    with open_store() as store:
        _warn_existing_folder_mission(store, args)
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
    return (f"No provider station is running for mission {short} · look at it without using capacity: llm-bridge control --mission {short}"
            f" · or start work: llm-bridge start --mission {short} (uses capacity)")


def _load_handle(mission_id: str):
    from .bridge_runtime import mission_socket_path
    from .terminal import TmuxTerminalBackend

    socket_path = mission_socket_path(mission_id)
    if not Path(socket_path).exists():
        raise WrongState(no_station_message(mission_id))
    try:
        return TmuxTerminalBackend().open_existing(
            socket_path=socket_path,
            session_name=f"mission-{mission_id}",
            bridge_rows=6,
        )
    except RuntimeError as exc:
        if str(exc).startswith("no live terminal station"):
            raise WrongState(no_station_message(mission_id)) from exc
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
    from . import bridge_rotation

    return bridge_rotation.validate_names(conf, raw, routes_allowed=routes_allowed)


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


def _set_mission_policy(store: BridgeStore, mission_id: str, *, teams: str | None, token_spend: str | None) -> dict[str, Any]:
    """Update mission execution guidance without changing any saved prompt text."""
    from . import bridge_policy

    mission = store.get_mission(mission_id)
    current = bridge_policy.effective_settings(mission.get("config"))
    resolved = {
        "team_policy_enabled": current["teams_enabled"] if teams is None else teams == "on",
        "token_spend": token_spend if token_spend is not None else (
            bridge_policy.DEFAULT_TOKEN_SPEND if current["legacy"] else current["token_spend"]
        ),
    }
    resolved["team_policy_enabled"], resolved["token_spend"] = bridge_policy.validate_settings(
        resolved["team_policy_enabled"], resolved["token_spend"]
    )
    store.set_mission_config_values(mission_id, resolved, event_kind="mission-policy-changed")
    return {
        "schema_version": 1,
        "mission_id": mission_id,
        "execution_policy": bridge_policy.effective_settings(resolved),
        "detail": f"Teams {'on' if resolved['team_policy_enabled'] else 'off'} · token spend {resolved['token_spend']}",
    }


def _set_mission_suspend_when_rate_limited(store: BridgeStore, mission_id: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable RTC-backed suspend during safe capacity waits."""
    store.set_mission_config_value(
        mission_id, "suspend_when_rate_limited", enabled, event_kind="mission-suspend-when-rate-limited",
    )
    detail = "Capacity-wait suspend " + ("on" if enabled else "off")
    detail += " · Bridge sleeps only at an idle boundary with no active operation and an armed wake" if enabled else " · Bridge waits awake"
    return {"schema_version": 1, "mission_id": mission_id, "suspend_when_rate_limited": enabled, "detail": detail}










# A controller records the terminal mission status before it exits and releases the mission lock, so a
# stop issued in that window finds a terminal mission whose lock is still held. Wait this long for the
# controller to finish exiting before reporting it as still active.
TERMINAL_STOP_LOCK_WAIT_SECONDS = 15.0


def _stop_terminal_mission_station(mission_id: str) -> dict[str, Any]:
    """Close a retained station after a terminal mission has released control."""
    lock = MissionLock(state_dir() / "locks", mission_id)
    try:
        lock.acquire(timeout=TERMINAL_STOP_LOCK_WAIT_SECONDS, poll=0.05)
    except BlockingIOError as exc:
        raise WrongState(f"mission {mission_id[:8]} still has an active controller; stop is pending") from exc
    try:
        with open_store() as store:
            mission = store.get_mission(mission_id)
            if mission["status"] not in {"COMPLETED", "FAILED", "CANCELLED", "STOPPED"}:
                raise WrongState(f"mission {mission_id[:8]} changed state while stopping; run stop again")
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


# How long `stop --force` lets a controller that is already stopping the mission finish its own cleanup.
CONTROLLER_STOP_COMPLETION_WAIT_SECONDS = 3.0


def _controller_completed_stop(mission_id: str) -> bool:
    """Whether the mission's controller has itself finished the stop that `stop --force` requested.

    The controller reacts to the STOPPING status by removing the station and recording STOPPED. When
    that happens between the force-stop's snapshot of the operations and its station lookup, the
    station is gone and the snapshot's operation has no recorded process: the controller already
    stopped the provider, so there is nothing left to terminate and nothing unverified to refuse.
    """
    deadline = time.monotonic() + CONTROLLER_STOP_COMPLETION_WAIT_SECONDS
    while True:
        with open_store() as store:
            if store.get_mission(mission_id)["status"] == "STOPPED":
                return True
        lock = MissionLock(state_dir() / "locks", mission_id)
        try:
            lock.acquire()
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        else:
            lock.release()
            return False  # no controller is running, so nobody will finish the stop


def _force_stop_mission(mission_id: str) -> dict[str, Any]:
    """Terminate only the controller and provider processes owned by this mission."""
    with open_store() as store:
        saved_status = str(store.get_mission(mission_id)["status"])
    if saved_status == "CREATED":
        # A mission that never started has nothing to force; stop never cancels.
        return {"mission_status": saved_status, "noop": True, "detail": fmt.nothing_to_stop(mission_id, saved_status)}
    provider_stopped = False
    runtime = state_dir() / "runtime" / f"mission-{mission_id}"
    with open_store() as store:
        mission = store.get_mission(mission_id)
        if mission["status"] in STARTED_MISSION_STATUSES:
            store.set_mission_status(mission_id, "STOPPING")
        from .bridge_runtime import provider_operations_to_check

        # Includes UNCERTAIN operations that kept a process identity, so a second `stop --force`
        # retries a termination that failed the first time.
        operations = provider_operations_to_check(store, mission_id)
        store.reconcile_interrupted_operations(mission_id, cause="stop-force")
    # Verifiers use their own process groups. Stop those validated
    # identities before terminating the controller that owns them.
    from .bridge_runtime import terminate_managed_processes

    with open_store() as store:
        terminate_managed_processes(store, mission_id)
    try:
        from .terminal import TmuxTerminalBackend

        backend = TmuxTerminalBackend()
        handle = _load_handle(mission_id)
    except (OSError, RuntimeError):
        backend = None
        handle = None  # type: ignore[assignment]  # checked on the next line
    if handle is not None and backend is not None:
        provider_stopped = any(operation["status"] != "UNCERTAIN" for operation in operations)
        backend.stop(handle)
    elif operations:
        from .bridge_runtime import _process_start_token, _terminate_owned_process_group

        for operation in operations:
            process_pid = operation.get("process_pid")
            process_start = operation.get("process_start")
            current_token = _process_start_token(int(process_pid)) if process_pid else ""
            if process_pid and process_start and current_token and current_token != str(process_start):
                continue  # the recorded provider is gone and its PID was reused: nothing to signal
            if not process_pid:
                # No provider process was ever recorded (the operation is still
                # waiting to launch); the controller is stopped below.
                continue
            if not process_start:
                if _controller_completed_stop(mission_id):
                    break  # the controller already stopped the provider and recorded STOPPED
                raise RuntimeError(
                    f"could not confirm that provider process {process_pid} belongs to this mission; "
                    f"it was left running. Check with: ps -p {process_pid}"
                )
            provider_stopped = True
            _terminate_owned_process_group(int(process_pid), str(process_start))
    # Stopping provider work and the tmux station is not enough:
    # the detached controller itself owns the mission lock and
    # must be terminated before STOPPED is authoritative.
    ready = runtime / "controller-ready.json"
    try:
        controller = json.loads(ready.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        controller = {}
    # The identity the controller wrote into its lock file while
    # holding it wins: the ready file can be missing or stale.
    controller = MissionLock.read_owner(state_dir() / "locks", mission_id) or controller
    controller_pid = int(controller.get("pid", 0) or 0)
    controller_start = str(controller.get("process_start", ""))
    if controller_pid > 1 and controller_start:
        from .bridge_runtime import _process_start_token, _terminate_owned_process_group

        current_start = _process_start_token(controller_pid)
        reused = bool(current_start) and current_start != controller_start
        if reused and MissionLock.is_held(state_dir() / "locks", mission_id):
            # The PID is someone else's, yet a process still owns the mission: its identity is unknown.
            raise RuntimeError("controller PID no longer matches its persisted identity; mission remains STOPPING")
        if current_start and not reused:
            _terminate_owned_process_group(controller_pid, controller_start)
    # The lock, not the ready file, is the controller-liveness
    # authority. Do not claim STOPPED until ownership is released.
    deadline = time.monotonic() + 3.0
    released = False
    while time.monotonic() < deadline:
        lock = MissionLock(state_dir() / "locks", mission_id)
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
        if store.get_mission(mission_id)["status"] == "STOPPING":
            store.set_mission_status(mission_id, "STOPPED")
        return {"mission": store.get_mission(mission_id), "no_provider_process": not provider_stopped}








MANAGED_ENV = "LLM_BRIDGE_MANAGED"
MANAGED_REFUSAL = "This command needs the operator; it is not available to the managed provider"
MANAGED_MISSION_ENV = "LLM_BRIDGE_MISSION_ID"
MANAGED_SCOPE_REFUSAL = "A managed provider may only address its own mission"
# Commands a managed provider may run: read-only inspection and recording a
# proposal (which still needs the operator's acceptance).
MANAGED_ALLOWED: frozenset[tuple[str, str]] = frozenset({
    ("status", ""), ("events", ""), ("agents", ""), ("help", ""),
    ("task", "list"), ("task", "show"),
    ("proposal", "create"), ("proposal", "list"),
    ("mission", "list"), ("missions", ""),
})


def _managed_session() -> bool:
    return os.environ.get(MANAGED_ENV) == "1"


def _managed_allows(args: argparse.Namespace) -> bool:
    """Advisory guard: a provider Bridge launched may not run operator commands.

    The provider runs as the same user, so this keeps a prompt-injected agent
    from invoking `proposal accept`, `task approve`, `cancel` or `steer`
    by habit; it is not a security boundary.
    """
    command = str(args.command)
    action_attribute = {"task": "task_action", "proposal": "proposal_action", "mission": "mission_action"}.get(command)
    action = str(getattr(args, action_attribute, "") or "") if action_attribute else ""
    if command == "missions" and getattr(args, "pick", False):
        return False
    return (command, action) in MANAGED_ALLOWED


def _installed_version_from_path() -> str | None:
    """Version recorded in an ``llm_tools-*.dist-info`` folder on ``sys.path``, or ``""`` when none exists.

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
    return ""


def _checkout_version() -> str:
    """Version in the ``pyproject.toml`` beside the ``llm_tools`` package, or ``unknown`` when it cannot be read."""
    try:
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def package_version() -> str:
    """The running llm-tools version: a source checkout's ``pyproject.toml`` first, else installed metadata.

    The checkout comes first because another, older install can leave its
    dist-info on ``sys.path`` while the code actually running is the checkout.
    """
    checkout = _checkout_version()
    if checkout != "unknown":
        return checkout
    found = _installed_version_from_path()
    if found is None:
        from importlib import metadata

        try:
            return metadata.version("llm-tools")
        except metadata.PackageNotFoundError:
            return _checkout_version()
    return found or _checkout_version()


def _managed_scope_violation(args: argparse.Namespace) -> bool:
    """Whether a managed caller names a mission other than its own (``$LLM_BRIDGE_MISSION_ID``).

    Compared after resolution, so a prefix, a folder or an omitted ``--mission``
    that would infer another mission is refused too. Like the allow-list this is
    a guardrail for a prompt-injected agent, not a security boundary.
    """
    own = os.environ.get(MANAGED_MISSION_ENV, "").strip().lower()
    named = getattr(args, "mission", None)
    if args.command == "agents":
        if getattr(args, "agent_all_missions", False):
            return True
        named = getattr(args, "agent_mission", None) or own
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


















def _home_screen_blocker() -> tuple[str, int] | None:
    """Why bare ``llm-bridge`` cannot open the home screen (reason, exit status), or None when it can.

    A missing or unusable terminal is an error (2), as for ``missions --pick``; a managed session or
    Windows never had a home screen, so showing help there is not a failure (0).
    """
    if sys.platform == "win32":
        return "a platform with curses (it is not available on Windows)", 0
    if _managed_session():
        return "your own terminal, not a Bridge-managed session (use llm-bridge status or llm-bridge help here)", 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        which = "stdin and stdout are" if not sys.stdin.isatty() and not sys.stdout.isatty() else ("stdin is" if not sys.stdin.isatty() else "stdout is")
        return f"an interactive terminal ({which} not a TTY; over ssh use ssh -t)", 2
    if os.environ.get("TERM", "") in {"", "dumb"}:
        return f"a terminal that can draw a screen (TERM is {os.environ.get('TERM') or 'empty'!r}, not a real terminal type)", 2
    return None


def _interactive_terminal() -> bool:
    """Whether stdin and stdout are both terminals, which every full-screen view needs."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _main(argv: list[str] | None = None) -> int:
    from .bridge_cli_print import _events_command, _print_missions, _print_result, _print_task_evidence, _proposal_page, _proposal_text, _task_text
    from .bridge_cli_worker import _refuse_on_windows, _remote_command, _split_target, _worker_command

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        blocker = _home_screen_blocker()
        if blocker is None:
            from .bridge_launcher import run_launcher

            from . import bridge_theme

            bridge_theme.prepare()  # query the terminal background before curses owns the tty
            prune_old_drafts()

            try:
                return run_launcher(main)
            except (OSError, RuntimeError, ValueError) as exc:
                return _fail(str(exc), "usage", EXIT_USAGE, False)
        # Say why before the help, so that a missing terminal is not mistaken for a broken home screen.
        reason, status = blocker
        print(f"llm-bridge: the home screen needs {reason}; showing help instead", file=sys.stderr)
        from .bridge_cli_parser import _build_parser

        _build_parser().print_help()
        return status
    if arguments[0] == "help" and not any(arg in {"--help", "-h"} for arg in arguments[1:]):
        from .bridge_help import is_known_topic, topic_index, topic_text

        if len(arguments) == 1:
            print(topic_text("quickstart") + "\n\n" + topic_index())
            return 0
        topic = " ".join(arguments[1:])
        if not is_known_topic(topic):
            return _fail(topic_text(topic), "usage", EXIT_USAGE, False)
        print(topic_text(topic))
        return 0
    target: str = commands.LOCAL_TARGET
    if arguments and (arguments[0] == "--target" or arguments[0].startswith("--target=")):
        parsed_target, arguments = _split_target(arguments)
        if parsed_target is None:
            return 2
        target = parsed_target
        if not arguments:
            return _fail("--target needs a command, for example: llm-bridge --target local status", "usage", EXIT_USAGE, False)
    if arguments == ["--version"]:
        print(f"llm-bridge {package_version()}")
        return 0
    if target == commands.LOCAL_TARGET:
        refused = _refuse_on_windows(arguments)
        if refused is not None:
            return refused
    if arguments and arguments[0] in {"--prompt-file", "--file", "--prompt"}:
        if _managed_session():
            return _fail(MANAGED_REFUSAL, "refused-in-managed-session", EXIT_USAGE, False)
        try:
            return _legacy_prompt_mode(arguments)
        except (ValueError, KeyError, OSError, TransitionError) as exc:
            return _fail(str(exc), "usage", EXIT_USAGE, False)
    from .bridge_cli_parser import _parse_arguments

    args = _parse_arguments(arguments)
    if args.command == "mission" and args.mission_action == "delete" and not args.mission:
        return _fail("name the mission to delete: llm-bridge mission delete ID (the current folder is never used for a deletion)", "usage", EXIT_USAGE, bool(getattr(args, "json", False)))
    if _managed_session() and not _managed_allows(args):
        return _fail(MANAGED_REFUSAL, "refused-in-managed-session", EXIT_USAGE, bool(getattr(args, "json", False)))
    if target != commands.LOCAL_TARGET:
        # Never fall back to local execution for a named target (CLI-005).
        from .bridge_remote import run_cli

        return run_cli(target, args)
    if args.command == "worker" and args.worker_action == "list":
        from .bridge_remote import run_cli as run_remote

        return run_remote(commands.ALL_TARGET, args)
    if _managed_session() and hasattr(args, "mission") and args.mission is None and os.environ.get(MANAGED_MISSION_ENV):
        args.mission = os.environ[MANAGED_MISSION_ENV].strip()  # never infer another mission from the folder
    if args.command in {"control", "usage", "console"} and not _interactive_terminal():
        # Every curses screen needs a terminal; without one, say so (or print what a script can use) instead of a traceback.
        if args.command == "usage":
            from .bridge_usage import print_usage_table

            try:
                print_usage_table()
            except (OSError, ValueError) as exc:
                return _fail(str(exc), "usage", EXIT_USAGE, False)
            return 0
        return _fail(f"{args.command} needs an interactive terminal; use llm-bridge status", "usage", EXIT_USAGE, False)
    if args.command in {"control", "usage", "console", "start", "resume", "attach", "view"}:
        from . import bridge_theme

        bridge_theme.prepare()
        if args.command in {"control", "usage", "console"}:
            prune_old_drafts()
    try:
        if args.command == "attach" and args.client:
            return _fail("--client is only used by attach from an active Bridge console", "usage", EXIT_USAGE, False)
        if args.command != "_controller":
            _resolve_parsed_mission(args, optional=args.command == "doctor")
            if _managed_session() and _managed_scope_violation(args):
                reference = getattr(args, "mission", getattr(args, "agent_mission", None))
                return _fail(f"{MANAGED_SCOPE_REFUSAL}; mission {str(reference or 'all missions')[:8]} is not yours", "refused-in-managed-session", EXIT_USAGE, bool(getattr(args, "json", False)))
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
        if args.command == "screen":
            from .terminal import TmuxTerminalBackend

            handle = _load_handle(args.mission)
            content = TmuxTerminalBackend().capture(handle, pane="provider")
            all_lines = content.splitlines()
            lines = all_lines[-200:]
            clipped = [line[:1000] for line in lines]
            captured = "\n".join(clipped)
            if args.json:
                print(json.dumps({"schema_version": 1, "mission_id": args.mission, "screen": captured, "truncated": len(all_lines) > 200 or any(len(line) > 1000 for line in lines)}, ensure_ascii=False))
            else:
                print(captured)
            return 0
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
                return run_controller(args.mission, headless=args.headless, native_tui=args.native_tui, **({"background": True} if args.background else {}), providers=args.providers, routes=args.routes, startup_token=args.startup_token)
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
            from .bridge_runtime import _LOCK_STARTUP_WAIT_SECONDS

            with open_store() as store:
                mission = store.get_mission(args.mission)
                probe = MissionLock(state_dir() / "locks", args.mission)
                try:
                    # A liveness probe (console, `mission list`) holds the lock for a moment; wait it out
                    # instead of taking a probe for a controller.
                    probe.acquire(timeout=_LOCK_STARTUP_WAIT_SECONDS)
                except BlockingIOError:
                    pass  # a controller already owns the mission; its rotation was checked when it started
                else:
                    probe.release()
                    require_launchable_rotation(mission["config"], mission["config"].get("providers"), mission["config"].get("routes"), explicit=False)
                result = _apply_operator_command(store, args.mission, {"action": "resume"})
                config = mission["config"]
            if result.get("status") == "REJECTED":
                # A finished mission is not restarted; say why instead of launching a controller.
                return _fail_rejected(result, "resume rejected", args.json)
            lock = MissionLock(state_dir() / "locks", args.mission)
            try:
                lock.acquire(timeout=_LOCK_STARTUP_WAIT_SECONDS)
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
            if args.command == "start":
                _resolve_mode_args(args)
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
                if mission["status"] in FINISHED_MISSION_STATUSES:
                    # `resume` refuses a finished mission through the same transition rule; `start` must not report it as running.
                    raise TransitionError(mission_transition_message(args.mission, str(mission["status"]), "RUNNING", verb="started"))
                explicit_rotation = getattr(args, "providers", None) is not None or getattr(args, "routes", None) is not None
                require_launchable_rotation(config, requested_providers, requested_routes, explicit=explicit_rotation)
                if explicit_rotation:
                    from . import bridge_rotation

                    _warn_not_signed_in(requested_providers, requested_routes, bridge_rotation.parse_frozen(config.get("frozen_tool_config")))
            saved_label = rotation_label(config.get("providers"), config.get("routes"))
            requested_label = rotation_label(requested_providers, requested_routes)
            if saved_label and (str(config.get("providers", "")), str(config.get("routes", ""))) != (str(requested_providers or ""), str(requested_routes or "")):
                if requested_label != saved_label and controller_running(args.mission):
                    raise WrongState(
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
                source = None  # type: ignore[assignment]  # separate branch of the command handler
                with open_store(readonly=True) as store:
                    if args.from_mission:
                        source = resolve_mission_ref(store, args.from_mission)
                return bridge_switcher.run_pick_cli(source, args.client)
            return _print_missions(args)
        if args.command == "gc":
            from . import bridge_switcher
            result = commands.dispatch(commands.CommandRequest("worker.gc"))
            if args.json:
                _json_print(result)
            else:
                print(bridge_switcher.format_gc(result["stations"]))
            return 0
        if args.command == "finish":
            from .bridge_finish import FinishError

            try:
                finished = commands.dispatch(commands.CommandRequest("mission.finish", mission_id=args.mission))
            except FinishError as exc:
                return _fail(str(exc), "wrong-state", EXIT_WRONG_STATE, args.json)
            _print_result(finished, args.json, lambda value: value["detail"])
            return 0
        if args.command == "mission":
            if args.mission_action == "list" and not args.json:
                return _print_missions(args)
            if args.mission_action == "delete":
                return _mission_delete_command(args)
            if args.mission_action == "export":
                from .bridge_templates import export_command

                return export_command(args)
            with open_store(readonly=args.mission_action == "list") as store:
                if args.mission_action == "create":
                    prompt, config = _mission_creation_inputs(args, auto_continue=bool(args.auto_continue))
                    _warn_existing_folder_mission(store, args)
                    result = _create_mission_with_task(store, args, prompt, config)
                elif args.mission_action == "set":
                    result = commands.dispatch(commands.CommandRequest("mission.settings.patch", {
                        "auto_continue": None if args.auto_continue is None else args.auto_continue == "on",
                        "teams": None if args.teams is None else args.teams == "on",
                        "token_spend": args.token_spend,
                        "suspend_when_rate_limited": None if args.suspend_when_rate_limited is None else args.suspend_when_rate_limited == "on",
                        "colour": None,
                    }, mission_id=args.mission))
                    _print_result(result, bool(getattr(args, "json", False)), lambda value: value["detail"])
                    return 0
                elif args.mission_action == "settings":
                    result = commands.dispatch(commands.CommandRequest("mission.settings.get", mission_id=args.mission))
                    _print_result(result, bool(getattr(args, "json", False)), lambda value: f"Auto-continue {'on' if value['auto_continue'] else 'off'} · capacity-wait suspend {'on' if value['suspend_when_rate_limited'] else 'off'} · teams {'on' if value['execution_policy']['teams_enabled'] else 'off'} · token spend {value['execution_policy']['token_spend']}")
                    return 0
                elif args.mission_action in {"colour", "color"}:
                    result = commands.dispatch(commands.CommandRequest("mission.settings.patch", {
                        "auto_continue": None, "teams": None, "token_spend": None,
                        "suspend_when_rate_limited": None, "colour": args.value,
                    }, mission_id=args.mission))
                    _print_result(result, bool(getattr(args, "json", False)), fmt.format_mission_colour)
                    return 0
                else:
                    result = commands.dispatch(commands.CommandRequest("mission.list", {"include_config": bool(args.include_config)}))
                    if args.limit is not None:
                        result = {**result, "missions": result["missions"][:max(0, args.limit)]}
            human = fmt.format_mission_created if args.mission_action == "create" else (lambda value: fmt.format_mission_list(value["missions"]))
            if not args.include_config:
                result = strip_frozen_config(result)
            _print_result(result, bool(getattr(args, "json", False)), human)
            return 0
        if args.command == "status":
            result = commands.dispatch(commands.CommandRequest("mission.status", {"full": True}, mission_id=args.mission))
            _print_result(result, args.json, fmt.format_status)
            return 0
        if args.command == "agents":
            from .bridge_agents import format_agent_map

            agent_mission = args.agent_mission
            if _managed_session() and not args.agent_all_missions and agent_mission is None:
                agent_mission = os.environ.get(MANAGED_MISSION_ENV)
            result = commands.dispatch(commands.CommandRequest("mission.agents", {"mission": agent_mission}))
            if args.json:
                _json_print(result)
            else:
                print(f"Live agent map · {result['total']} agent(s) · {result['freshness']}")
                for line in format_agent_map(result):
                    print(line)
                for error in result["errors"].values():
                    print(f"Unavailable: {error}")
            return 0
        if args.command == "suspend-until-credits":
            from .bridge_power import suspend_until_credits

            result = suspend_until_credits(args.mission)
            if args.json:
                _json_print(result)
            else:
                print(result.get("message") or result.get("reason") or result.get("status"))
            return EXIT_OK if result.get("ok") is True else EXIT_FAILED
        if args.command == "events":
            return _events_command(args)
        if args.command == "proposal":
            with open_store() as store:
                if args.proposal_action == "list":
                    result = commands.dispatch(commands.CommandRequest("proposal.list", {"status": args.status, "limit": args.limit, "after": args.after}, mission_id=args.mission))
                elif args.proposal_action in {"accept", "reject"}:
                    result = commands.dispatch(commands.CommandRequest(f"proposal.{args.proposal_action}", {
                        "task_number": int(args.task), "task_id": None, "reason": str(getattr(args, "reason", "") or ""),
                    }, mission_id=args.mission))
                    # Keep the established CLI JSON envelope while the shared typed
                    # operation itself returns the task result used by REST/workers.
                    result = {"status": "APPLIED", "result": result}
                else:
                    prompt = prompt_from(args)
                    result = commands.dispatch(commands.CommandRequest("proposal.create", {
                        "source_operation_id": args.source_operation_id, "prompt": prompt, "title": args.title,
                        "priority": args.priority, "depends_on": _dependency_ids(store, args.mission, args.depends_on),
                        "acceptance": args.acceptance,
                    }, mission_id=args.mission))
                    result = {"status": "APPLIED", "result": result}
            if isinstance(result, dict) and result.get("status") == "REJECTED":
                return _fail_rejected(result, "proposal command rejected", bool(getattr(args, "json", False)))
            if args.proposal_action == "list" and isinstance(result, dict):
                result = _proposal_page(args, result)
            _print_result(result, bool(getattr(args, "json", False)), _proposal_text(args))
            return 0
        if args.command == "task":
            result = _task_result(args)
            if isinstance(result, dict) and result.get("status") == "REJECTED":
                return _fail_rejected(result, "command rejected", bool(getattr(args, "json", False)))
            if args.task_action in {"output", "diff"}:
                return _print_task_evidence(args, result)
            if getattr(args, "json", False) and isinstance(result, fmt.TaskPage):
                # Every JSON list is an object, so a page can say how many tasks exist (CLIUX-015).
                result = {"tasks": list(result), "total": result.total if result.total is not None else len(result), "offset": result.offset}
            _print_result(result, bool(getattr(args, "json", False)), _task_text(args))
            return 0
        if args.command == "continue":
            result = commands.dispatch(commands.CommandRequest("mission.continue", mission_id=args.mission))
            if result.get("status") == "REJECTED":
                return _fail_rejected(result, "continue rejected", args.json)
            _print_result(result, args.json, lambda value: fmt.format_command_receipt("continue", value, args.mission))
            return 0
        after_task = getattr(args, "after_task", None)  # pause: a flag; stop: None, or a task number (0 = the running task)
        if args.command in {"pause", "stop"} and after_task is not None and after_task is not False:
            if args.command == "stop" and args.force:
                raise ValueError("--after-task and --force cannot be combined: --force ends the work now")
            with open_store() as store:
                steer_payload: dict[str, Any] = {"action": "steer", "kind": "pause-after-iteration" if args.command == "pause" else "stop-after-task"}
                if args.command == "stop":
                    task = _after_task_target(store, args.mission, int(after_task))
                    steer_payload["task_id"] = int(task["id"])
                result = _apply_operator_command(store, args.mission, steer_payload)
                if args.command == "stop" and isinstance(result.get("result"), dict):
                    result["result"]["task_ordinal"] = task["ordinal"]
            if result.get("status") == "REJECTED":
                return _fail_rejected(result, f"{args.command} --after-task rejected", args.json)
            _print_result(result, args.json, lambda value: fmt.format_command_receipt(f"{args.command}-after-task", value, args.mission))
            return 0
        if args.command in {"pause", "stop", "cancel"}:
            with open_store() as store:
                saved = store.get_mission(args.mission)
                saved_status = str(saved["status"])
            if args.command == "cancel" and saved_status not in FINISHED_MISSION_STATUSES and not _confirm_cancel(args, fmt.short_id(args.mission), fmt.clipped_name(saved.get("name", ""))):
                return 2
            if args.command == "stop" and args.force:
                result = commands.dispatch(commands.CommandRequest("mission.stop", {"force": True}, mission_id=args.mission))
            else:
                result = commands.dispatch(commands.CommandRequest(f"mission.{args.command}", mission_id=args.mission))
                if result.get("status") == "REJECTED":
                    return _fail_rejected(result, args.command + " rejected", args.json)
            _print_result(result, args.json, lambda value: fmt.format_command_receipt(args.command, value, args.mission))
            return 0
        if args.command in {"backup", "restore"}:
            from . import bridge_backup

            if args.command == "backup":
                facts = bridge_backup.backup(Path(args.output), store_path=store_path())
                _print_result(facts, args.json, lambda value: f"Backed up {value['missions']} missions to {value['path']} ({value['bytes']} bytes, sha256 {value['sha256'][:16]}).")
            else:
                facts = bridge_backup.restore(Path(args.input), store_path=store_path())
                _print_result(facts, args.json, lambda value: f"Restored {value['missions']} missions from {value['restored_from']}."
                              + (f" The replaced database is kept at {value['previous_database']}." if value["previous_database"] else ""))
            return 0
        if args.command == "worker":
            return _worker_command(args)
        if args.command == "remote":
            return _remote_command(args)
        if args.command == "theme":
            _print_theme(args)
            return 0
        if args.command == "doctor":
            with open_store() as store:
                mission = store.get_mission(args.mission) if args.mission else None  # type: ignore[assignment]  # name reused; None means no mission was given
                providers = _doctor_provider_checks(mission)
                import shutil

                tmux_available = bool(shutil.which("tmux"))
                from . import bridge_doctor

                integrity = store.db.execute("PRAGMA integrity_check").fetchone()[0]
                verdict = bridge_doctor.readiness(providers, integrity=integrity)
                report = {
                    "schema_version": 1,
                    "state_dir": str(state_dir()),
                    "database": str(store.path),
                    "sqlite_integrity": integrity,
                    "tmux_available": tmux_available,
                    "mission": mission,
                    "providers": providers,
                    **verdict,
                    "next_steps": bridge_doctor.next_steps(
                        tmux_available=tmux_available, providers=providers,
                        folder=(mission or {}).get("cwd") or Path.cwd(),
                        needs_you=_mission_needs_you(mission)),
                }
            if not args.include_config:
                report = strip_frozen_config(report)
            if args.bundle is not None:  # the archive is the answer; whether a provider can run is in its doctor report
                from . import bridge_bundle

                written = bridge_bundle.write_bundle(args.bundle or None, report, version=package_version(), state=state_dir(), store_file=store_path())
                _print_result(written, args.json, lambda value: f"Wrote {value['path']}\n{value['note']}")
                return 0
            _print_result(report, args.json, fmt.format_doctor)
            return 0 if verdict["ready"] else 1
        if args.command == "station":
            with open_store() as store:
                result = _apply_operator_command(store, args.mission, {"action": "station.view", "station": args.name})
            if result.get("status") == "REJECTED":
                return _fail_rejected(result, "station update rejected", args.json)
            _print_result(result, args.json, lambda value: fmt.format_command_receipt("station", value, args.mission))
            return 0
        if args.command == "rotation":
            params = {"set": args.set_list, "routes": args.routes, "providers": args.providers}
            operation = "mission.rotation.put" if any(value is not None for value in params.values()) else "mission.rotation.get"
            if operation == "mission.rotation.get":
                params = {}
            else:
                params = {key: value for key, value in params.items() if value is not None}
            result = commands.dispatch(commands.CommandRequest(operation, params, mission_id=args.mission))
            _print_result(result, args.json, fmt.format_rotation)
            return 0
        if args.command == "steer":
            prompt = read_verbatim(args.prompt_file) if args.prompt_file else str(args.prompt or "")
            result = commands.dispatch(commands.CommandRequest("mission.steer", {
                "kind": args.kind, "task": args.task, "task_id": args.task_by_id,
                "route": args.route, "prompt": prompt,
            }, mission_id=args.mission))
            if result.get("status") == "REJECTED":
                return _fail_rejected(result, "steering rejected", args.json)
            _print_result(result, args.json, lambda value: fmt.format_command_receipt("steer", value, args.mission))
            return 0
        from .bridge_cli_parser import _build_parser

        _build_parser().error(f"unsupported command {args.command}")
    except (ValueError, KeyError, OSError, RuntimeError, TransitionError, commands.CommandFailure) as exc:  # WorkerConfigError is a ValueError
        return _fail_with(exc, bool(getattr(args, "json", False)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

