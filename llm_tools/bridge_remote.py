"""Remote CLI: run typed commands on another worker through the coordinator.

``llm-bridge --target NAME ...`` maps the parsed command line to a
``CommandRequest`` and submits it to the coordinator's ``POST
/api/v1/commands``. The worker executes it through the same command layer as a
local run, so the output is the same JSON (CLI-003, TEST-003). Nothing here
falls back to local execution: an unknown, unreachable or silent target is an
explicit error (CLI-005). ``--target all`` aggregates read operations only
(CLI-004).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import bridge_commands as commands
from . import bridge_format as fmt
from .bridge_store import default_state_dir
from .bridge_worker import _atomic_private_write

REMOTE_FILE = "remote.json"
CA_FILE = "coordinator-ca.pem"
WAIT_SECONDS = 15.0
EXIT_UNAVAILABLE = 3  # the target did not answer; the request may still be delivered later


class RemoteError(Exception):
    """A coordinator or transport failure, reported to the operator as is."""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclasses.dataclass(frozen=True)
class RemoteConfig:
    coordinator_url: str
    token: str

    def public(self) -> dict[str, Any]:
        return {"coordinator_url": self.coordinator_url}


def remote_dir(env: dict[str, str] | None = None) -> Path:
    return default_state_dir(env) / "remote"


def login(coordinator_url: str, token: str, *, ca_pem: str | None, env: dict[str, str] | None = None) -> RemoteConfig:
    """Store the coordinator URL, API token and CA privately (0600)."""
    url = coordinator_url.rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.\-\[\]:]+", url):
        raise ValueError("coordinator URL must be https://host[:port] with no path")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("API token must be 32-256 URL-safe characters, as printed by bridge-coordinator token create")
    if ca_pem is not None:
        if "-----BEGIN CERTIFICATE-----" not in ca_pem:
            raise ValueError("CA file is not a PEM certificate")
        _atomic_private_write(remote_dir(env) / CA_FILE, ca_pem)
    config = RemoteConfig(url, token)
    _atomic_private_write(remote_dir(env) / REMOTE_FILE, json.dumps(dataclasses.asdict(config), indent=2) + "\n")
    return config


def logout(env: dict[str, str] | None = None) -> bool:
    removed = False
    for name in (REMOTE_FILE, CA_FILE):
        path = remote_dir(env) / name
        if path.exists():
            path.unlink()
            removed = True
    return removed


def load(env: dict[str, str] | None = None) -> RemoteConfig:
    path = remote_dir(env) / REMOTE_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return RemoteConfig(str(value["coordinator_url"]), str(value["token"]))
    except FileNotFoundError:
        raise RemoteError("no coordinator is configured; run llm-bridge remote login (known targets: local)") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RemoteError(f"remote configuration {path} is unreadable: {type(exc).__name__}") from None


class Client:
    """Minimal JSON-over-HTTPS client for the coordinator API."""

    def __init__(self, config: RemoteConfig, *, env: dict[str, str] | None = None, opener: Callable[..., Any] | None = None) -> None:
        self.config = config
        ca = remote_dir(env) / CA_FILE
        self.context = ssl.create_default_context(cafile=str(ca) if ca.exists() else None)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = opener or _open_without_redirects

    def call(self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 30.0) -> tuple[int, Any]:
        request = urllib.request.Request(
            self.config.coordinator_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener(request, context=self.context, timeout=timeout) as response:
                return int(response.status), json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return int(exc.code), json.loads(exc.read() or b"null")
            except ValueError:
                return int(exc.code), None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise RemoteError(f"cannot reach the coordinator at {self.config.coordinator_url}: {reason}") from None

    def workers(self) -> list[dict[str, Any]]:
        status, body = self.call("GET", "/api/v1/workers")
        if status != 200:
            raise RemoteError(_api_error(status, body))
        if not isinstance(body, dict) or not isinstance(body.get("workers"), list):
            raise RemoteError("coordinator returned an unexpected worker list")
        return list(body["workers"])

    def submit(self, target: str, request: commands.CommandRequest, *, wait: float = WAIT_SECONDS) -> dict[str, Any]:
        """Submit and wait for the worker's ``CommandResult``; raise when it does not arrive."""
        status, body = self.call("POST", "/api/v1/commands", {"target": target, "request": request.to_json(), "wait_ms": int(wait * 1000)},
                                 timeout=wait + 15)
        if status not in {200, 202}:
            raise RemoteError(_api_error(status, body))
        if not isinstance(body, dict) or body.get("state") != "done" or not isinstance(body.get("result"), dict):
            raise RemoteError(
                f"target {target!r} did not answer within {wait:.0f}s; request {request.request_id} stays queued and is "
                "delivered when the worker reconnects",
                exit_code=EXIT_UNAVAILABLE,
            )
        return dict(body["result"])


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would resend the bearer token to another URL."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _open_without_redirects(request: urllib.request.Request, *, context: ssl.SSLContext, timeout: float) -> Any:
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _RefuseRedirect())
    return opener.open(request, timeout=timeout)


def _api_error(status: int, body: Any) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"coordinator refused the request ({status} {error.get('code', '')}): {error.get('message', '')}".strip()
    if status == 401:
        return "coordinator refused the API token (401); run llm-bridge remote login again"
    return f"coordinator answered HTTP {status}"


# --------------------------------------------------------------------------- argument mapping


def _task_selector(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "task_by_id", None) is not None:
        return {"task_id": int(args.task_by_id)}
    if getattr(args, "task", None) is not None:
        return {"task_number": int(args.task)}
    raise ValueError("name the task with --task N or --task-id ID")


def _read_text(args: argparse.Namespace) -> str:
    from .bridge import read_verbatim

    return read_verbatim(args.prompt_file) if getattr(args, "prompt_file", None) else str(getattr(args, "prompt", "") or "")


def request_for(args: argparse.Namespace, target: str) -> tuple[commands.CommandRequest, Callable[[Any], str]]:
    """Map a parsed command line to a typed request and its human formatter."""
    command = args.command
    mission = getattr(args, "mission", None)

    def needs_mission() -> str:
        if not mission:
            raise ValueError(f"--mission is required with --target {target}; the current folder only identifies local missions")
        return str(mission)

    def make(operation: str, params: dict[str, Any] | None = None, *, with_mission: bool = True) -> commands.CommandRequest:
        return commands.CommandRequest(operation, params or {}, mission_id=needs_mission() if with_mission else None, target=target)

    if command == "status":
        return make("mission.status"), fmt.format_status
    if command == "events":
        return make("mission.events", {"after": args.after, "limit": args.limit}), lambda value: fmt.format_events(value["events"])
    if command == "mission" and args.mission_action == "list":
        return make("mission.list", {"include_config": bool(args.include_config)}, with_mission=False), lambda value: fmt.format_mission_list(value["missions"])
    if command in {"pause", "cancel", "continue"} or (command == "stop" and not getattr(args, "force", False)):
        request = make(f"mission.{command}")
        return request, lambda value: fmt.format_command_receipt(command, value, request.mission_id or "")
    if command == "proposal" and args.proposal_action == "list":
        return make("proposal.list", {"status": args.status, "limit": args.limit, "after": args.after}), lambda value: fmt.format_proposals(value["proposals"])
    if command == "proposal" and args.proposal_action in {"accept", "reject"}:
        params = {"task_number": int(args.task), "accept": args.proposal_action == "accept", "reason": str(getattr(args, "reason", "") or "")}
        return make("proposal.decide", params), lambda value: fmt.format_proposal_result(args.proposal_action, value)
    if command == "task":
        action = args.task_action
        if action == "list":
            if getattr(args, "all", False):
                raise ValueError("task list --all is not available remotely; page with --limit and --offset")
            return make("task.list", {"limit": args.limit, "offset": args.offset}), lambda value: fmt.format_task_list(value["tasks"])
        if action == "show":
            return make("task.show", _task_selector(args)), fmt.format_task_detail
        if action == "add":
            if args.depends_on:
                raise ValueError("task add --depends-on is not available remotely yet; add the dependency with task dependencies --task-id")
            params = {"prompt": _read_text(args), "title": args.title, "priority": args.priority, "order": args.order, "acceptance": list(args.acceptance)}
            return make("task.add", params), lambda value: fmt.format_task_result("add", value.get("result") or value)
        extra: dict[str, Any] = {
            "amend": lambda: {"prompt": _read_text(args)},
            "verify": lambda: {"evidence": args.evidence, "approved": bool(args.approve)},
            "approve": lambda: {"evidence": args.evidence, "approved": True},
            "reject": lambda: {"reason": args.reason},
            "priority": lambda: {"priority": args.value},
            "reorder": lambda: {"order": args.value},
        }.get(action, lambda: {})()
        if action == "dependencies":
            raise ValueError("task dependencies is not available remotely yet")
        operation = "task.verify" if action == "approve" else f"task.{action}"
        commands.lookup(operation)
        return make(operation, {**_task_selector(args), **extra}), lambda value: fmt.format_task_result(action, value)
    raise ValueError(f"{command} is not available with --target {target}; remote commands: status, events, mission list, pause, stop, cancel, continue, task, proposal list|accept|reject")


def _envelope_exit(envelope: dict[str, Any]) -> tuple[int, str | None]:
    outcome = envelope.get("outcome")
    if outcome in {"ok", "applied", "accepted"}:
        return 0, None
    error = envelope.get("error") or {}
    message = str(error.get("message") or outcome)
    if outcome == "uncertain":
        return 2, f"outcome uncertain on the worker: {message}; check the mission before retrying"
    return 2, message


def run_cli(target: str, args: argparse.Namespace, *, client: Client | None = None) -> int:
    """Execute one parsed command on ``target`` (a worker name or ``all``)."""
    as_json = bool(getattr(args, "json", False))
    try:
        client = client or Client(load())
        if args.command == "worker" and getattr(args, "worker_action", "") == "list":
            return _print(client.workers(), as_json, _format_workers)
        request, human = request_for(args, target)
        spec = commands.lookup(request.operation)
        if target != commands.ALL_TARGET:
            envelope = client.submit(target, request)
            code, message = _envelope_exit(envelope)
            if message:
                print(f"llm-bridge: {target}: {message}", file=sys.stderr)
                return code
            return _print(envelope.get("result"), as_json, human)
        if spec.mutating:
            raise ValueError("--target all supports read-only commands only")
        results: dict[str, Any] = {}
        failures = 0
        for worker in client.workers():
            name = str(worker.get("name", ""))
            if worker.get("state") != "online":
                results[name] = {"outcome": "unavailable", "error": {"code": "offline", "message": "worker is offline"}}
                failures += 1
                continue
            try:
                results[name] = client.submit(name, dataclasses.replace(request, target=name, request_id=os.urandom(16).hex()))
            except RemoteError as exc:
                results[name] = {"outcome": "unavailable", "error": {"code": "unreachable", "message": str(exc)}}
                failures += 1
        _print({"schema_version": 1, "targets": results}, as_json, lambda value: _format_all(value["targets"], human))
        return 0 if failures == 0 else EXIT_UNAVAILABLE
    except RemoteError as exc:
        print(f"llm-bridge: {exc}", file=sys.stderr)
        return exc.exit_code
    except (ValueError, commands.CommandFailure) as exc:
        print(f"llm-bridge: {exc}", file=sys.stderr)
        return 2


def _print(value: Any, as_json: bool, human: Callable[[Any], str]) -> int:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(human(value))
    return 0


def _last_seen(value: Any, now: float | None = None) -> str:
    """``last_seen`` epoch seconds as an age ("4m ago"); "never" when absent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return "never"
    from .common import fmt_duration

    age = (time.time() if now is None else now) - value
    return "just now" if age < 60 else f"{fmt_duration(age)} ago"


def _format_workers(workers: list[dict[str, Any]], now: float | None = None) -> str:
    if not workers:
        return "No workers are registered with the coordinator."
    lines = []
    for worker in workers:
        lines.append(f"{worker.get('name', '?'):<20} {worker.get('state', '?'):<8} last seen {_last_seen(worker.get('last_seen'), now)}")
    return "\n".join(lines)


def _format_all(results: dict[str, Any], human: Callable[[Any], str]) -> str:
    blocks = []
    for name in sorted(results):
        envelope = results[name]
        if envelope.get("outcome") == "ok":
            blocks.append(f"== {name}\n{human(envelope.get('result'))}")
        else:
            blocks.append(f"== {name}: {(envelope.get('error') or {}).get('message', envelope.get('outcome'))}")
    return "\n\n".join(blocks) if blocks else "No workers are registered with the coordinator."
