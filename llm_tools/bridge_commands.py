"""Typed application command layer shared by the CLI, TUI and remote transports.

Every Bridge operation that may be invoked from more than one interface is
declared here once, as an :class:`OperationSpec` with a stable operation ID,
typed parameters and a handler. A handler delegates to the authoritative
implementation (store transitions, ``_apply_operator_command``,
``status_snapshot``); it never re-implements a business rule.

Two entry points exist:

* :func:`dispatch` validates a :class:`CommandRequest` and runs the handler in
  this process. Domain exceptions propagate unchanged, so the local CLI keeps
  its existing messages and exit codes.
* :func:`execute` does the same but always returns a :class:`CommandResult`
  envelope with a machine-readable :class:`CommandError`. Remote transports
  (the worker service) use this form; it never raises for domain failures.

Mutating operations carry the request ID into the durable ``commands`` table as
the command ID. A duplicate delivery of the same request therefore returns the
stored outcome instead of repeating the effect (WORK-006, PROTO-004).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .bridge_missions import MissionNotFound
from .bridge_store import BridgeStore, TransitionError

COMMAND_SCHEMA_VERSION = 1
LOCAL_TARGET = "local"
ALL_TARGET = "all"
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_TARGET_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")

# Outcome vocabulary (WORK-007). ``ok`` is the outcome of a read.
OUTCOMES = ("ok", "accepted", "applied", "rejected", "failed", "uncertain")


class CommandFailure(Exception):
    """A structured failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class CommandError:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


@dataclass(frozen=True)
class CommandRequest:
    operation: str
    params: Mapping[str, Any] = field(default_factory=dict)
    mission_id: str | None = None
    target: str = LOCAL_TARGET
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": COMMAND_SCHEMA_VERSION,
            "operation": self.operation,
            "params": dict(self.params),
            "mission_id": self.mission_id,
            "target": self.target,
            "request_id": self.request_id,
        }

    @classmethod
    def from_json(cls, value: Any) -> "CommandRequest":
        if not isinstance(value, dict):
            raise CommandFailure("invalid-request", "request must be a JSON object")
        version = value.get("schema_version")
        if version != COMMAND_SCHEMA_VERSION:
            raise CommandFailure("unsupported-version", f"unsupported command schema version {version!r}", details={"supported": [COMMAND_SCHEMA_VERSION]})
        unknown = set(value) - {"schema_version", "operation", "params", "mission_id", "target", "request_id"}
        if unknown:
            raise CommandFailure("invalid-request", f"unknown request fields: {', '.join(sorted(unknown))}")
        operation, params = value.get("operation"), value.get("params", {})
        mission_id, target, request_id = value.get("mission_id"), value.get("target", LOCAL_TARGET), value.get("request_id")
        if not isinstance(operation, str) or not isinstance(params, dict):
            raise CommandFailure("invalid-request", "operation must be a string and params an object")
        if mission_id is not None and not isinstance(mission_id, str):
            raise CommandFailure("invalid-request", "mission_id must be a string")
        if not isinstance(target, str) or not isinstance(request_id, str):
            raise CommandFailure("invalid-request", "target and request_id must be strings")
        return cls(operation=operation, params=params, mission_id=mission_id, target=target, request_id=request_id)


@dataclass(frozen=True)
class CommandResult:
    request_id: str
    operation: str
    outcome: str
    result: dict[str, Any] | None = None
    error: CommandError | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": COMMAND_SCHEMA_VERSION,
            "request_id": self.request_id,
            "operation": self.operation,
            "outcome": self.outcome,
            "result": self.result,
            "error": self.error.to_json() if self.error else None,
        }


@dataclass(frozen=True)
class Param:
    kind: type
    required: bool = False
    default: Any = None
    item: type | None = None  # element type of a list parameter


Handler = Callable[[BridgeStore | None, "CommandRequest", dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class OperationSpec:
    operation: str
    summary: str
    mutating: bool
    needs_mission: bool
    params: Mapping[str, Param]
    handler: Handler


def _validate_request_id(request_id: str) -> None:
    if not _REQUEST_ID.fullmatch(request_id):
        raise CommandFailure("invalid-request", "request_id must be 1-128 characters of letters, digits, '.', '_' or '-'")


def validate_target(name: str) -> str:
    """Return a syntactically valid target name, or raise ``invalid-target``."""
    if name in {LOCAL_TARGET, ALL_TARGET} or _TARGET_NAME.fullmatch(name):
        return name
    raise CommandFailure("invalid-target", f"invalid target name {name!r}: use lowercase letters, digits and '-'")


def unknown_target_message(name: str) -> str:
    """Explain why a non-local target cannot be reached from this host."""
    if name == ALL_TARGET:
        return "target 'all' needs a configured coordinator; distributed mode is not configured on this host (known targets: local)"
    return f"unknown target {name!r}: distributed mode is not configured on this host (known targets: local)"


def _is_kind(value: Any, kind: type) -> bool:
    # bool is an int subclass; never accept True where a number is expected.
    return isinstance(value, kind) and not (kind is int and isinstance(value, bool))


def _validated_params(spec: OperationSpec, raw: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(raw) - set(spec.params)
    if unknown:
        raise CommandFailure("invalid-argument", f"{spec.operation}: unknown parameters: {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for name, param in spec.params.items():
        if name not in raw or raw[name] is None:
            if param.required:
                raise CommandFailure("invalid-argument", f"{spec.operation}: parameter {name!r} is required")
            values[name] = param.default
            continue
        value = raw[name]
        if not _is_kind(value, param.kind):
            raise CommandFailure("invalid-argument", f"{spec.operation}: parameter {name!r} must be {param.kind.__name__}")
        if param.item is not None and not all(_is_kind(element, param.item) for element in value):
            raise CommandFailure("invalid-argument", f"{spec.operation}: parameter {name!r} must be a list of {param.item.__name__}")
        values[name] = list(value) if isinstance(value, list) else value
    return values


# --------------------------------------------------------------------------- handlers
# Each handler calls the CLI module's implementation through ``bridge`` so
# existing tests that patch those names keep observing a single code path.


def _cli() -> Any:
    from . import bridge

    return bridge


def _mission_list(store: BridgeStore | None, request: CommandRequest, params: dict[str, Any]) -> dict[str, Any]:
    cli = _cli()
    assert store is not None
    missions = cli.order_missions(store.list_missions())
    for item in missions:
        item["controller_running"] = cli.controller_running(str(item["id"]))
    result: dict[str, Any] = {"missions": missions}
    return result if params["include_config"] else cli.strip_frozen_config(result)


def _mission_status(store: BridgeStore | None, request: CommandRequest, params: dict[str, Any]) -> dict[str, Any]:
    assert request.mission_id is not None
    return dict(_cli().status_snapshot(request.mission_id))


def _mission_events(store: BridgeStore | None, request: CommandRequest, params: dict[str, Any]) -> dict[str, Any]:
    assert store is not None and request.mission_id is not None
    if params["limit"] < 1 or params["limit"] > 1000:
        raise CommandFailure("invalid-argument", "mission.events: limit must be between 1 and 1000")
    return {"schema_version": 1, "events": store.events(request.mission_id, after=params["after"], limit=params["limit"])}


def _mission_control(action: str) -> Handler:
    def handler(store: BridgeStore | None, request: CommandRequest, params: dict[str, Any]) -> dict[str, Any]:
        cli = _cli()
        mission_id = request.mission_id
        assert mission_id is not None
        if action == "stop":
            with cli.open_store() as probe:
                current = str(probe.get_mission(mission_id)["status"])
            if current in {"COMPLETED", "FAILED", "CANCELLED", "STOPPED"}:
                return dict(cli._stop_terminal_mission_station(mission_id))
        with cli.open_store() as writer:
            return dict(cli._apply_operator_command(writer, mission_id, {"action": action}, command_id=request.request_id))

    return handler


def _task_show(store: BridgeStore | None, request: CommandRequest, params: dict[str, Any]) -> dict[str, Any]:
    assert store is not None and request.mission_id is not None
    try:
        return dict(_cli()._require_task(store, request.mission_id, params["task_id"]))
    except TransitionError as exc:
        raise CommandFailure("not-found", str(exc)) from None


def _task_list(store: BridgeStore | None, request: CommandRequest, params: dict[str, Any]) -> dict[str, Any]:
    assert store is not None and request.mission_id is not None
    limit, offset = params["limit"], params["offset"]
    if not 0 <= limit <= 1000 or offset < 0:
        raise CommandFailure("invalid-argument", "task.list: limit must be 0-1000 and offset non-negative")
    return {
        "tasks": store.list_tasks(request.mission_id, limit=limit, offset=offset),
        "total": store.task_counters(request.mission_id)["total"],
        "offset": offset,
    }


def _operator(action: str) -> Handler:
    """Apply an operator payload ``{"action": action, **params}`` through the durable command path."""

    def handler(store: BridgeStore | None, request: CommandRequest, params: dict[str, Any]) -> dict[str, Any]:
        cli = _cli()
        assert request.mission_id is not None
        payload: dict[str, Any] = {"action": action}
        payload.update((name, value) for name, value in params.items() if not (name == "expected_revision" and value is None))
        with cli.open_store() as writer:
            return dict(cli._apply_operator_command(writer, request.mission_id, payload, command_id=request.request_id))

    return handler


def _task_op(operation: str, summary: str, extra: Mapping[str, Param] | None = None) -> OperationSpec:
    params = {"task_id": Param(int, required=True), **(extra or {}), "expected_revision": Param(int)}
    return OperationSpec(operation, summary, True, True, params, _operator(operation))


_NO_PARAMS: Mapping[str, Param] = {}

OPERATIONS: dict[str, OperationSpec] = {
    spec.operation: spec
    for spec in (
        OperationSpec("mission.list", "List missions on this worker", False, False, {"include_config": Param(bool, default=False)}, _mission_list),
        OperationSpec("mission.status", "Show one mission's status projection", False, True, _NO_PARAMS, _mission_status),
        OperationSpec(
            "mission.events",
            "Page a mission's durable events after a cursor",
            False,
            True,
            {"after": Param(int, default=0), "limit": Param(int, default=50)},
            _mission_events,
        ),
        OperationSpec("mission.pause", "Pause autonomous scheduling", True, True, _NO_PARAMS, _mission_control("pause")),
        OperationSpec("mission.stop", "Stop a mission after the current step", True, True, _NO_PARAMS, _mission_control("stop")),
        OperationSpec("mission.cancel", "Cancel a mission", True, True, _NO_PARAMS, _mission_control("cancel")),
        OperationSpec("mission.continue", "Release a held checkpoint", True, True, _NO_PARAMS, _mission_control("continue")),
        OperationSpec(
            "task.list",
            "Page a mission's tasks in scheduling order",
            False,
            True,
            {"limit": Param(int, default=50), "offset": Param(int, default=0)},
            _task_list,
        ),
        OperationSpec("task.show", "Show one task by global ID", False, True, {"task_id": Param(int, required=True)}, _task_show),
        OperationSpec(
            "task.add",
            "Add a task",
            True,
            True,
            {
                "prompt": Param(str, required=True),
                "title": Param(str, default=""),
                "priority": Param(int, default=0),
                "order": Param(int),
                "depends_on": Param(list, default=[], item=int),
                "acceptance": Param(list, default=[], item=str),
            },
            _operator("task.add"),
        ),
        _task_op("task.amend", "Amend a task prompt; creates a new revision", {"prompt": Param(str, required=True)}),
        _task_op("task.verify", "Record verification evidence; approve completes the task",
                 {"evidence": Param(str, default=""), "approved": Param(bool, default=False)}),
        _task_op("task.reject", "Reject a task result", {"reason": Param(str, required=True)}),
        _task_op("task.priority", "Set task priority", {"priority": Param(int, required=True)}),
        _task_op("task.reorder", "Set task order among equal priorities", {"order": Param(int, required=True)}),
        _task_op("task.dependency", "Add a prerequisite task", {"depends_on": Param(int, required=True)}),
        _task_op("task.pause", "Pause a task"),
        _task_op("task.resume", "Resume a paused task"),
        _task_op("task.cancel", "Cancel a task"),
        _task_op("task.retry", "Retry a failed task"),
    )
}


def operation_catalog() -> list[dict[str, Any]]:
    """Describe the supported operations for capability negotiation (PROTO-007)."""
    return [
        {
            "operation": spec.operation,
            "summary": spec.summary,
            "mutating": spec.mutating,
            "needs_mission": spec.needs_mission,
            "params": {name: {"type": param.kind.__name__, "required": param.required} for name, param in spec.params.items()},
        }
        for spec in OPERATIONS.values()
    ]


def lookup(operation: str) -> OperationSpec:
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise CommandFailure("unsupported-operation", f"unsupported operation {operation!r}", details={"supported": sorted(OPERATIONS)})
    return spec


def dispatch(request: CommandRequest) -> dict[str, Any]:
    """Validate and run ``request`` in this process; domain exceptions propagate."""
    spec = lookup(request.operation)
    _validate_request_id(request.request_id)
    if request.target != LOCAL_TARGET:
        raise CommandFailure("invalid-target", f"dispatch runs local requests only, not target {request.target!r}")
    params = _validated_params(spec, request.params)
    if spec.needs_mission and not request.mission_id:
        raise CommandFailure("invalid-argument", f"{spec.operation}: mission_id is required")
    cli = _cli()
    if spec.mutating:
        return spec.handler(None, request, params)
    with cli.open_store(readonly=True) as store:
        if spec.needs_mission and request.mission_id is not None and store.mission_id_if_exists(request.mission_id) is None:
            raise MissionNotFound(f"no mission matches {request.mission_id!r}")
        return spec.handler(store, request, params)


def outcome_of(spec: OperationSpec, result: Mapping[str, Any]) -> str:
    """Map a handler result to the WORK-007 outcome vocabulary."""
    if not spec.mutating:
        return "ok"
    status = str(result.get("status", "")).upper()
    if status == "QUEUED":
        return "accepted"
    if status in {"REJECTED", "FAILED"}:
        return status.lower()
    return "applied"


def error_for(exc: BaseException) -> CommandError:
    """Translate a domain exception into a stable error code without leaking internals."""
    if isinstance(exc, CommandFailure):
        return CommandError(exc.code, str(exc), exc.details)
    if isinstance(exc, MissionNotFound):
        return CommandError("not-found", str(exc))
    if isinstance(exc, KeyError):
        return CommandError("not-found", str(exc.args[0]) if exc.args else "not found")
    if isinstance(exc, TransitionError):
        return CommandError("conflict", str(exc))
    if isinstance(exc, ValueError):
        return CommandError("invalid-argument", str(exc))
    return CommandError("internal", f"{type(exc).__name__} while executing the command")


def execute(request: CommandRequest) -> CommandResult:
    """Run ``request`` locally and always return a structured envelope."""
    try:
        spec = lookup(request.operation)
        result = dispatch(request)
    except CommandFailure as exc:
        return CommandResult(request.request_id, request.operation, "rejected", error=error_for(exc))
    except (MissionNotFound, KeyError, TransitionError, ValueError) as exc:
        return CommandResult(request.request_id, request.operation, "rejected", error=error_for(exc))
    except Exception as exc:  # noqa: BLE001 - the envelope must report, not hide, an uncertain mutation
        spec_or_none = OPERATIONS.get(request.operation)
        outcome = "uncertain" if spec_or_none is not None and spec_or_none.mutating else "failed"
        return CommandResult(request.request_id, request.operation, outcome, error=error_for(exc))
    outcome = outcome_of(spec, result)
    error = None
    if outcome == "rejected":
        detail = result.get("result") or {}
        error = CommandError(str(detail.get("code", "rejected")), str(detail.get("error", "rejected")))
    return CommandResult(request.request_id, request.operation, outcome, result=result, error=error)
