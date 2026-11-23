"""Remote CLI: run typed commands on another worker through the coordinator.

``llm-bridge --target NAME ...`` maps the parsed command line to a
``CommandRequest``. The client uses the matching resource endpoint when one is
available, otherwise the low-level ``POST /api/v1/commands`` adapter. The worker
executes every operation through the same command layer as a local run, so the
output is the same JSON (CLI-003, TEST-003). Nothing here falls back to local
execution: an unknown, unreachable or silent target is an explicit error
(CLI-005). ``--target all`` aggregates read operations only (CLI-004).
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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import bridge_commands as commands
from . import bridge_format as fmt
from .bridge_help import error_text
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
        config = RemoteConfig(str(value["coordinator_url"]), str(value["token"]))
    except FileNotFoundError:
        raise RemoteError("no coordinator is configured; run llm-bridge remote login (known targets: local)") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RemoteError(f"remote configuration {path} is unreadable: {type(exc).__name__}") from None
    if not config.coordinator_url.startswith("https://"):  # login enforces this; an edited file must not bypass it
        raise RemoteError(f"remote configuration {path} must name the coordinator with https://; run llm-bridge remote login again")
    return config


class Client:
    """Minimal JSON-over-HTTPS client for the coordinator API."""

    def __init__(self, config: RemoteConfig, *, env: dict[str, str] | None = None, opener: Callable[..., Any] | None = None) -> None:
        self.config = config
        ca = remote_dir(env) / CA_FILE
        self.context = ssl.create_default_context(cafile=str(ca) if ca.exists() else None)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = opener or _open_without_redirects

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        request = urllib.request.Request(
            self.config.coordinator_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
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

    def submit_resource(self, target: str, request: commands.CommandRequest, *, wait: float = WAIT_SECONDS) -> dict[str, Any] | None:
        """Use the resource route when one exists; return ``None`` for legacy-only operations."""
        mission = request.mission_id
        op = request.operation
        if mission is None and op not in {"mission.list", "mission.create", "worker.restore", "mission.agents", "worker.doctor", "worker.audit", "worker.gc", "session.list", "usage.snapshot"}:
            return None
        base = f"/api/v1/missions/{urllib.parse.quote(mission, safe='')}" if mission is not None else ""
        params = dict(request.params)
        worker_query = urllib.parse.urlencode({"worker": target})
        headers = {"Idempotency-Key": request.request_id}
        wait_ms = int(wait * 1000)
        if op == "mission.create":
            status, body = self.call("POST", "/api/v1/missions", {**params, "worker": target, "wait_ms": wait_ms}, timeout=wait + 15, headers=headers)
        elif op == "mission.list":
            query = urllib.parse.urlencode({"include_config": "true" if params.get("include_config") else "false", "wait_ms": wait_ms})
            status, body = self.call("GET", f"/api/v1/workers/{urllib.parse.quote(target, safe='')}/missions?{query}", timeout=wait + 15)
        elif op == "worker.restore":
            status, body = self.call("POST", f"/api/v1/workers/{urllib.parse.quote(target, safe='')}/restore", {**params, "wait_ms": wait_ms}, timeout=wait + 15, headers=headers)
        elif op in {"mission.settings.get", "mission.rotation.get", "mission.rotation.put"}:
            resource = "settings" if op.startswith("mission.settings") else "rotation"
            method = "GET" if op.endswith(".get") else "PUT"
            query = urllib.parse.urlencode({"worker": target, "wait_ms": wait_ms})
            status, body = self.call(method, f"{base}/{resource}?{query}", None if method == "GET" else {**params, "wait_ms": wait_ms}, timeout=wait + 15, headers=headers)
        elif op == "mission.settings.patch":
            query = urllib.parse.urlencode({"worker": target, "wait_ms": wait_ms})
            status, body = self.call("PATCH", f"{base}/settings?{query}", {**params, "wait_ms": wait_ms}, timeout=wait + 15, headers=headers)
        elif op in {"mission.steer", "mission.finish"}:
            action = "steer" if op == "mission.steer" else "finish"
            status, body = self.call("POST", f"{base}/{action}?{worker_query}", {**params, "wait_ms": wait_ms}, timeout=wait + 15, headers=headers)
        elif op == "mission.agents":
            query_values = {"worker": target, "wait_ms": wait_ms}
            if mission is not None:
                status, body = self.call("GET", f"{base}/agents?{urllib.parse.urlencode(query_values)}", timeout=wait + 15)
            else:
                if params.get("mission"):
                    query_values["mission"] = params["mission"]
                query_values.pop("worker")
                status, body = self.call("GET", f"/api/v1/workers/{urllib.parse.quote(target, safe='')}/agents?{urllib.parse.urlencode(query_values)}", timeout=wait + 15)
        elif op in {"proposal.list", "proposal.create"}:
            if op == "proposal.list":
                query = urllib.parse.urlencode({"worker": target, "status": params.get("status", "PENDING"), "limit": params.get("limit", 100), "after": params.get("after", 0), "wait_ms": wait_ms})
                status, body = self.call("GET", f"{base}/proposals?{query}", timeout=wait + 15)
            else:
                status, body = self.call("POST", f"{base}/proposals?{urllib.parse.urlencode({'worker': target})}", {**params, "wait_ms": wait_ms}, timeout=wait + 15, headers=headers)
        elif op in {"proposal.accept", "proposal.reject"}:
            number = int(params["task_number"])
            body_params = {"reason": params.get("reason", ""), "wait_ms": wait_ms}
            status, body = self.call("POST", f"{base}/proposals/{number}/{op.rsplit('.', 1)[1]}?{urllib.parse.urlencode({'worker': target})}", body_params, timeout=wait + 15, headers=headers)
        elif op in {"worker.doctor", "worker.audit", "worker.gc"}:
            name = urllib.parse.quote(target, safe="")
            if op == "worker.gc":
                status, body = self.call("POST", f"/api/v1/workers/{name}/gc", {"wait_ms": wait_ms}, timeout=wait + 15, headers=headers)
            else:
                query_values = {"wait_ms": wait_ms}
                if op == "worker.audit":
                    query_values["limit"] = params.get("limit", 50)
                elif params.get("mission"):
                    query_values["mission"] = params["mission"]
                path = "audit" if op == "worker.audit" else "doctor"
                status, body = self.call("GET", f"/api/v1/workers/{name}/{path}?{urllib.parse.urlencode(query_values)}", timeout=wait + 15)
        elif op == "mission.status":
            query = urllib.parse.urlencode({"worker": target, "wait_ms": wait_ms})
            status, body = self.call("GET", f"{base}?{query}", timeout=wait + 15)
        elif op == "mission.delete":
            status, body = self.call("DELETE", f"{base}?{worker_query}", {**params, "wait_ms": wait_ms}, timeout=wait + 15, headers=headers)
        elif op in {"mission.start", "mission.resume", "mission.pause", "mission.stop", "mission.cancel", "mission.continue", "mission.suspend_until_credits"}:
            action = "suspend-until-credits" if op == "mission.suspend_until_credits" else op.removeprefix("mission.")
            status, body = self.call("POST", f"{base}/{action}?{worker_query}", {**params, "wait_ms": wait_ms}, timeout=wait + 15, headers=headers)
        elif op == "task.list":
            initial_offset = int(params.get("offset", 0))
            if params.get("all"):
                tasks: list[dict[str, Any]] = []
                total: int | None = None
                offset = initial_offset
                while total is None or offset < total:
                    query = urllib.parse.urlencode({"worker": target, "limit": 1000, "offset": offset, "wait_ms": wait_ms})
                    page_status, page_body = self.call("GET", f"{base}/tasks?{query}", timeout=wait + 15)
                    if page_status != 200 or not isinstance(page_body, dict) or page_body.get("state") != "done" or not isinstance(page_body.get("result"), dict):
                        raise RemoteError(_api_error(page_status, page_body) if page_status not in {200, 202} else "worker did not complete a paginated task-list request")
                    page = page_body["result"]
                    rows = page.get("tasks")
                    if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
                        raise RemoteError("coordinator returned an unexpected task page")
                    tasks.extend(rows)
                    total = int(page.get("total", len(tasks)))
                    next_offset = offset + len(rows)
                    if not rows or next_offset <= offset:
                        break
                    offset = next_offset
                body = {"state": "done", "result": {"tasks": tasks, "total": total or len(tasks), "offset": initial_offset}}
                status = 200
            else:
                query = urllib.parse.urlencode({"worker": target, "limit": params.get("limit", 50), "offset": initial_offset, "wait_ms": wait_ms})
                status, body = self.call("GET", f"{base}/tasks?{query}", timeout=wait + 15)
        elif op == "mission.events":
            if {"newest", "since_seconds", "hide_bookkeeping"} & params.keys():
                return None  # the events resource pages by cursor only; these filters go through the typed command route
            query = urllib.parse.urlencode({"worker": target, "after": params.get("after", 0), "limit": params.get("limit", 50), "wait_ms": wait_ms})
            status, body = self.call("GET", f"{base}/events?{query}", timeout=wait + 15)
        elif op == "session.list" and mission is None:
            query_values = {"limit": params.get("limit", 100), "offset": params.get("offset", 0), "wait_ms": wait_ms}
            status, body = self.call("GET", f"/api/v1/workers/{urllib.parse.quote(target, safe='')}/sessions?{urllib.parse.urlencode(query_values)}", timeout=wait + 15)
        elif op == "usage.snapshot":
            query = urllib.parse.urlencode({"wait_ms": wait_ms})
            status, body = self.call("GET", f"/api/v1/workers/{urllib.parse.quote(target, safe='')}/usage?{query}", timeout=wait + 15)
        elif op == "mission.station":
            query = urllib.parse.urlencode({"worker": target, "wait_ms": wait_ms})
            status, body = self.call("GET", f"{base}/station?{query}", timeout=wait + 15)
        elif op == "session.list":
            query = urllib.parse.urlencode({"worker": target, "limit": params.get("limit", 100), "wait_ms": wait_ms})
            status, body = self.call("GET", f"{base}/operations?{query}", timeout=wait + 15)
        elif op == "task.show":
            task_number = params.get("task_number")
            if task_number is None or "task_id" in params:
                return None
            query = urllib.parse.urlencode({"worker": target, "wait_ms": wait_ms})
            status, body = self.call("GET", f"{base}/tasks/{int(task_number)}?{query}", timeout=wait + 15)
        elif op == "task.add":
            body_params = dict(params)
            if "depends_on_numbers" in body_params:
                body_params["depends_on"] = body_params.pop("depends_on_numbers")
            status, body = self.call("POST", f"{base}/tasks?{worker_query}", {**body_params, "wait_ms": wait_ms}, timeout=wait + 15, headers=headers)
        elif op.startswith("task."):
            task_selector = params.get("task_number")
            if task_selector is None or "task_id" in params:
                return None
            task_base = f"{base}/tasks/{int(task_selector)}"
            body_params = {key: value for key, value in params.items() if key not in {"task_number", "task_id"}}
            body_params["wait_ms"] = wait_ms
            if op in {"task.verify", "task.reject"}:
                if op == "task.reject":
                    body_params["approved"] = False
                status, body = self.call("POST", f"{task_base}/verify?{worker_query}", body_params, timeout=wait + 15, headers=headers)
            elif op in {"task.amend", "task.priority", "task.reorder", "task.dependency"}:
                if op == "task.dependency" and "depends_on_number" in body_params:
                    body_params["depends_on"] = body_params.pop("depends_on_number")
                status, body = self.call("PATCH", f"{task_base}?{worker_query}", body_params, timeout=wait + 15, headers=headers)
            elif op in {"task.pause", "task.resume", "task.cancel", "task.retry"}:
                status, body = self.call("POST", f"{task_base}/{op.removeprefix('task.')}?{worker_query}", body_params, timeout=wait + 15, headers=headers)
            else:
                return None
        else:
            return None
        if status not in {200, 202}:
            raise RemoteError(_api_error(status, body))
        if not isinstance(body, dict) or body.get("state") != "done" or not isinstance(body.get("result"), dict):
            request_id = body.get("request_id", request.request_id) if isinstance(body, dict) else request.request_id
            raise RemoteError(
                f"target {target!r} did not answer within {wait:.0f}s; request {request_id} stays queued and is "
                "delivered when the worker reconnects",
                exit_code=EXIT_UNAVAILABLE,
            )
        result = dict(body["result"])
        if op == "task.list":
            # The coordinator adds a cursor to the command result; the local task list has none (page with --offset and --limit).
            for page in (result, result.get("result")):
                if isinstance(page, dict):
                    page.pop("next_cursor", None)
        return result


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


def _evidence_text(value: Any) -> str:
    """`task output` or `task diff` of a worker: the text, or the reason there is none."""
    if isinstance(value, dict) and value.get("available"):
        return str(value.get("text", ""))
    return f"unavailable: {value.get('message') if isinstance(value, dict) else value}"


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
        if args.follow:
            raise ValueError("events --follow is not available remotely; run events again to see newer events")
        # The same request the local command sends: the newest events unless --after names a cursor.
        params: dict[str, Any] = {"limit": args.limit}
        if args.after is None:
            params["newest"] = True
        else:
            params["after"] = args.after
        if args.since:
            params["since_seconds"] = args.since
        if not args.verbose:
            params["hide_bookkeeping"] = True
        return make("mission.events", params), lambda value: fmt.format_events(
            value["events"], total=value.get("total"), newest=args.after is None, verbose=args.verbose, after=args.after or 0
        )
    if command == "mission" and args.mission_action == "list":
        return make("mission.list", {"include_config": bool(args.include_config)}, with_mission=False), lambda value: fmt.format_mission_list(value["missions"])
    if command == "mission" and args.mission_action == "create":
        from .bridge import _resolve_mode_args

        _resolve_mode_args(args)  # --mode and the older --native/--headless flags become one choice
        if getattr(args, "template", None):
            raise ValueError("mission create --template reads a file on this computer and is not available with --target; create the mission on the worker, or pass the objective with --prompt")
        unsupported_options: list[str] = []
        if getattr(args, "isolate", "none") != "none":
            unsupported_options.append("--isolate")
        for flag in ("commit", "push", "budget_runs", "budget_minutes", "budget_provider_changes", "budget_spend", "budget_currency"):
            if getattr(args, flag, None) is not None:
                unsupported_options.append("--" + flag.replace("_", "-"))
        if getattr(args, "push_default_branch", False):
            unsupported_options.append("--push-default-branch")
        if args.verifier_command is not None:
            unsupported_options.append("--verifier")
        if getattr(args, "verifier_after_dashes", None):
            unsupported_options.append("-- COMMAND")
        if args.command_template:
            unsupported_options.append("--command-template")
        if args.verifier_argv:
            unsupported_options.append("--verifier-arg")
        if args.verifier_files:
            unsupported_options.append("--verifier-file")
        params = {
            "prompt": _read_text(args), "cwd": args.cwd, "name": args.name or "",
            "acceptance": list(args.acceptance), "providers": args.providers, "routes": args.routes,
            "headless": bool(args.headless), "native_tui": bool(args.native_tui),
            "teams": args.teams, "token_spend": args.token_spend, "trust_folder": bool(args.trust_folder),
            "unsupported_options": unsupported_options,
            "auto_continue": bool(args.auto_continue), "include_config": bool(args.include_config),
        }
        if args.verifier_name:
            params["verifier_name"] = args.verifier_name
        if args.verifier_timeout is not None:
            params["verifier_timeout"] = args.verifier_timeout
        for option, default in (("log_max_bytes", 4 * 1024 * 1024), ("log_rotations", 3),
                                ("log_retained_runs", 20), ("capacity_refresh_seconds", 60)):
            if getattr(args, option) != default:
                params[option] = getattr(args, option)
        return make("mission.create", params, with_mission=False), fmt.format_mission_created
    if command == "mission" and args.mission_action == "delete":
        request = make("mission.delete", {"confirm": True})
        return request, lambda value: str(value.get("detail") or f"Deleted mission {fmt.short_id(request.mission_id or '')}")
    if command == "mission" and args.mission_action == "set":
        params = {
            "auto_continue": None if args.auto_continue is None else args.auto_continue == "on",
            "teams": None if args.teams is None else args.teams == "on",
            "token_spend": args.token_spend,
            "suspend_when_rate_limited": None if args.suspend_when_rate_limited is None else args.suspend_when_rate_limited == "on",
            "colour": None,
        }
        from .bridge import orchestration_patch_params

        params.update({name: value for name, value in orchestration_patch_params(args).items() if value is not None})  # only the given ones go to the worker
        if all(value is None for value in params.values()):
            raise ValueError("mission set requires at least one setting")
        return make("mission.settings.patch", params), lambda value: str(value.get("detail", "Mission settings updated"))
    if command == "mission" and args.mission_action == "settings":
        return make("mission.settings.get"), lambda value: fmt.format_mission_settings(value) + f" · colour {value.get('colour') or 'auto'}"
    if command == "mission" and args.mission_action in {"colour", "color"}:
        params = {"auto_continue": None, "teams": None, "token_spend": None,
                  "suspend_when_rate_limited": None, "colour": str(args.value)}
        return make("mission.settings.patch", params), lambda value: fmt.format_mission_colour(value)
    if command == "rotation":
        params = {"set": args.set_list, "routes": args.routes, "providers": args.providers}
        operation = "mission.rotation.put" if any(value is not None for value in params.values()) else "mission.rotation.get"
        return make(operation, params), lambda value: fmt.format_rotation(value)
    if command == "agents":
        request = commands.CommandRequest("mission.agents", {"mission": args.agent_mission}, mission_id=args.agent_mission, target=target)
        return request, lambda value: "\n".join(__import__("llm_tools.bridge_agents", fromlist=["format_agent_map"]).format_agent_map(value))
    if command == "start":
        from .bridge import _resolve_mode_args

        _resolve_mode_args(args)
        params = {
            "headless": getattr(args, "headless", None),
            "native_tui": getattr(args, "native_tui", None),
            "providers": getattr(args, "providers", None),
            "routes": getattr(args, "routes", None),
        }
        request = make("mission.start", params)
        return request, lambda value: fmt.format_command_receipt("start", value, request.mission_id or "")
    if command == "suspend-until-credits":
        request = make("mission.suspend_until_credits")
        return request, lambda value: str(value.get("message") or value.get("status") or "Suspend request completed")
    if command == "restore":
        request = make("worker.restore", {"input": str(args.input)}, with_mission=False)
        return request, lambda value: (f"Restored {value.get('missions', 0)} missions "
                                       f"(schema {value.get('schema_version', 'unknown')}, "
                                       f"last event {value.get('last_event_id', 0)}).")
    if command == "resume":
        request = make("mission.resume")
        return request, lambda value: fmt.format_command_receipt("resume", value, request.mission_id or "")
    if command in {"pause", "cancel", "continue", "stop"}:
        if getattr(args, "after_task", None) not in (None, False):
            raise ValueError(f"{command} --after-task is not available remotely; it would act now. Run it on the worker, or use steer")
        params = {"force": bool(getattr(args, "force", False))} if command == "stop" else {}
        request = make(f"mission.{command}", params)
        return request, lambda value: fmt.format_command_receipt(command, value, request.mission_id or "")
    if command == "proposal" and args.proposal_action == "create":
        depends_on = [int(value.strip()) for value in args.depends_on.split(",") if value.strip()]
        request = make("proposal.create", {"source_operation_id": args.source_operation_id, "prompt": _read_text(args),
                                            "title": args.title, "priority": args.priority, "depends_on": depends_on,
                                            "acceptance": list(args.acceptance)})
        return request, lambda value: fmt.format_proposal_result("create", value)
    if command == "proposal" and args.proposal_action == "list":
        return make("proposal.list", {"status": args.status, "limit": args.limit, "after": args.after}), lambda value: fmt.format_proposals(value["proposals"])
    if command == "proposal" and args.proposal_action in {"accept", "reject"}:
        params = {"task_number": int(args.task), "reason": str(getattr(args, "reason", "") or "")}
        return make(f"proposal.{args.proposal_action}", params), lambda value: fmt.format_proposal_result(args.proposal_action, value)
    if command == "worker" and args.worker_action == "audit":
        return make("worker.audit", {"limit": args.limit}, with_mission=False), lambda value: "\n".join(
            " ".join([str(entry.get("ts", "?")), str(entry.get("action", "?"))] + [f"{key}={entry[key]}" for key in sorted(entry) if key not in {"ts", "action"} and entry[key] is not None])
            for entry in value["entries"]) or "No worker audit entries."
    if command == "gc":
        return make("worker.gc", with_mission=False), lambda value: __import__("llm_tools.bridge_switcher", fromlist=["format_gc"]).format_gc(value["stations"])
    if command == "doctor":
        if args.include_config:
            raise ValueError("doctor --include-config is not available with --target; run doctor locally")
        return make("worker.doctor", {"mission": args.mission}, with_mission=False), lambda value: (
            f"SQLite integrity {value.get('sqlite_integrity', 'unknown')} · "
            f"tmux {'available' if value.get('tmux_available') else 'not found'} · "
            f"{len(value.get('providers') or [])} provider checks")
    if command == "finish":
        request = make("mission.finish")
        return request, lambda value: str(value.get("detail", "Finish request completed"))
    if command == "steer":
        params = {"kind": args.kind, "task": getattr(args, "task", None),
                  "task_id": getattr(args, "task_by_id", None), "route": args.route, "prompt": _read_text(args)}
        return make("mission.steer", params), lambda value: fmt.format_command_receipt("steer", value, request.mission_id or "")
    if command == "mission" and args.mission_action == "export":
        raise ValueError("mission export --target is unavailable: a template holds prompts and the coordinator has no file artifact route; run it on the worker")
    if command == "backup":
        raise ValueError("backup --target is unavailable: backup contains prompts and evidence, and the coordinator has no streaming artifact route; run backup locally on the worker")
    if command == "task":
        action = args.task_action
        if action == "list":
            params = {"limit": args.limit, "offset": args.offset}
            if args.all:
                params["all"] = True
            return make("task.list", params), lambda value: fmt.format_task_list(value["tasks"])
        if action == "show":
            return make("task.show", _task_selector(args)), fmt.format_task_detail
        if action in {"output", "diff"}:
            if getattr(args, "follow", False):
                raise ValueError("task output --follow is not available remotely; the worker returns the end of the log")
            return make(f"task.{action}", _task_selector(args)), _evidence_text
        if action == "add":
            try:
                depends_on = [int(value.strip()) for value in args.depends_on.split(",") if value.strip()]
            except ValueError:
                raise ValueError("task add --depends-on must be comma-separated task numbers") from None
            params = {"prompt": _read_text(args), "title": args.title, "priority": args.priority, "order": args.order, "acceptance": list(args.acceptance)}
            if depends_on:
                params["depends_on_numbers"] = depends_on
            return make("task.add", params), lambda value: fmt.format_task_result("add", value.get("result") or value)
        extra: dict[str, Any] = {
            "amend": lambda: {"prompt": _read_text(args), **({"requeue": True} if args.requeue else {})},
            "verify": lambda: {"evidence": args.evidence, "approved": bool(args.approve)},
            "approve": lambda: {"evidence": args.evidence, "approved": True},
            "reject": lambda: {"reason": args.reason, **({"hold": True} if args.hold else {})},
            "priority": lambda: {"priority": args.value},
            "reorder": lambda: {"order": args.value},
        }.get(action, lambda: {})()
        if action == "dependencies":
            extra["depends_on_number"] = args.add
        operation = "task.dependency" if action == "dependencies" else "task.verify" if action == "approve" else f"task.{action}"
        commands.lookup(operation)
        return make(operation, {**_task_selector(args), **extra}), lambda value: fmt.format_task_result(action, value)
    raise ValueError(f"{command} is not available with --target {target}; remote commands: mission create|list|delete, restore, suspend-until-credits, status, events, start, resume, pause, stop, cancel, continue, task, proposal list|accept|reject")


def _envelope_exit(envelope: dict[str, Any]) -> tuple[int, str | None]:
    outcome = envelope.get("outcome")
    if outcome in {"ok", "applied", "accepted"}:
        return 0, None
    error = envelope.get("error") or {}
    message = str(error.get("message") or outcome)
    if outcome == "uncertain":
        return 2, f"outcome uncertain on the worker: {message}; check the mission before retrying"
    return 2, message


def _cancel_confirmed(target: str, args: argparse.Namespace, client: Client) -> bool:
    """The local `cancel` confirmation, for a mission on ``target``: `--yes` skips it, a finished mission needs none.

    The saved status and name come from the worker. When the worker cannot answer, the question is asked
    with the mission reference alone, which is the safer choice.
    """
    from .bridge import _confirm_cancel
    from .bridge_states import FINISHED_MISSION_STATUSES

    if getattr(args, "yes", False):
        return True
    status, name = "", ""
    try:
        envelope = client.submit_resource(target, commands.CommandRequest("mission.status", mission_id=args.mission, target=target))
        mission = ((envelope or {}).get("result") or {}).get("mission") or {}
        status, name = str(mission.get("status") or ""), str(mission.get("name") or "")
    except (RemoteError, ValueError, OSError):
        pass
    if status in FINISHED_MISSION_STATUSES:
        return True
    return _confirm_cancel(args, fmt.short_id(args.mission), fmt.clipped_name(name))


def _delete_confirmed(target: str, args: argparse.Namespace, client: Client) -> bool:
    """The local `mission delete` confirmation for a mission on ``target``; `--yes` skips it.

    The mission's name comes from the worker. A mission the worker would refuse (running, paused) gets no
    question: the worker's refusal is the answer. When the worker cannot answer, the question names the
    reference alone.
    """
    from .bridge import _confirm_delete
    from .bridge_states import DELETABLE_MISSION_STATUSES

    if getattr(args, "yes", False):
        return True
    status, name = "", ""
    try:
        envelope = client.submit_resource(target, commands.CommandRequest("mission.status", mission_id=args.mission, target=target))
        mission = ((envelope or {}).get("result") or {}).get("mission") or {}
        status, name = str(mission.get("status") or ""), str(mission.get("name") or "")
    except (RemoteError, ValueError, OSError):
        pass
    if status and status not in DELETABLE_MISSION_STATUSES:
        return True
    return _confirm_delete(args, fmt.short_id(args.mission), fmt.clipped_name(name))


def run_cli(target: str, args: argparse.Namespace, *, client: Client | None = None) -> int:
    """Execute one parsed command on ``target`` (a worker name or ``all``)."""
    as_json = bool(getattr(args, "json", False))
    try:
        client = client or Client(load())
        if args.command == "worker" and getattr(args, "worker_action", "") == "list":
            return _print({"workers": client.workers()}, as_json, lambda value: _format_workers(value["workers"]))  # --json lists are objects
        request, human = request_for(args, target)
        spec = commands.lookup(request.operation)
        if target != commands.ALL_TARGET:
            if args.command == "cancel" and not _cancel_confirmed(target, args, client):
                return 2
            if args.command == "mission" and getattr(args, "mission_action", "") == "delete" and not _delete_confirmed(target, args, client):
                return 2
            resource_result = client.submit_resource(target, request)
            envelope = client.submit(target, request) if resource_result is None else resource_result
            code, message = _envelope_exit(envelope)
            if message:
                print(error_text(f"{target}: {message}", str((envelope.get("error") or {}).get("code") or "failed")), file=sys.stderr)
                return code
            value = envelope.get("result")
            if request.operation in {"proposal.create", "proposal.accept", "proposal.reject"}:
                # Preserve the public CLI's historical mutation receipt shape while
                # the typed worker operation returns the domain task directly.
                value = {"status": "APPLIED", "result": value}
            return _print(value, as_json, human)
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
                per_worker = dataclasses.replace(request, target=name, request_id=os.urandom(16).hex())
                resource_result = client.submit_resource(name, per_worker)
                results[name] = client.submit(name, per_worker) if resource_result is None else resource_result
            except RemoteError as exc:
                results[name] = {"outcome": "unavailable", "error": {"code": "unreachable", "message": str(exc)}}
                failures += 1
        _print({"schema_version": 1, "targets": results}, as_json, lambda value: _format_all(value["targets"], human))
        return 0 if failures == 0 else EXIT_UNAVAILABLE
    except RemoteError as exc:
        print(error_text(str(exc), "unreachable"), file=sys.stderr)
        return exc.exit_code
    except (ValueError, commands.CommandFailure) as exc:
        print(error_text(str(exc), "usage"), file=sys.stderr)
        return 2


def _print(value: Any, as_json: bool, human: Callable[[Any], str]) -> int:
    if as_json:
        print(json.dumps(commands.with_task_numbers(value), ensure_ascii=False, indent=2, sort_keys=True))
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
        location = str(worker.get("host_name") or "unknown host")
        address = str(worker.get("remote_address") or "offline")
        lines.append(f"{worker.get('name', '?'):<20} {location} ({address}) · {worker.get('state', '?')}, last seen {_last_seen(worker.get('last_seen'), now)}")
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
