"""Worker, remote-target and Windows-support commands of the Bridge CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from . import bridge_commands as commands
import argparse
from .bridge_cli_print import _print_result
from .bridge import (
    EXIT_USAGE,
    _fail,
    package_version,
    read_verbatim,
)


def _worker_command(args: argparse.Namespace) -> int:
    """``llm-bridge worker``: enrolment and the foreground worker loop (WORK-001, WORK-010)."""
    from . import bridge_worker as worker

    if args.worker_action == "enroll":
        token = read_verbatim(args.token_file, "--token-file").strip()
        ca_pem = read_verbatim(args.ca_file, "--ca-file") if args.ca_file else None
        client_cert_pem = read_verbatim(args.client_cert_file, "--client-cert-file") if args.client_cert_file else None
        client_key_pem = read_verbatim(args.client_key_file, "--client-key-file") if args.client_key_file else None
        config = worker.enroll(args.name or worker.default_worker_name(), _coordinator_url(args.coordinator, "wss"), token, ca_pem=ca_pem, allow_control=bool(args.allow_control),
                               allow_terminal_view=bool(args.allow_terminal_view), allow_create=bool(args.allow_create),
                               allow_roots=tuple(args.allow_root), allow_power=bool(args.allow_power), allow_restore=bool(args.allow_restore),
                               client_cert_pem=client_cert_pem, client_key_pem=client_key_pem)
        _print_result(config.public(), args.json, lambda value: f"Enrolled worker {value['name']} ({value['worker_id'][:8]}) for {value['coordinator_url']}; "
                      f"remote control {'allowed' if value['allow_control'] else 'read-only'}. Start it with: llm-bridge worker run")
        return 0
    if args.worker_action == "rotate-token":
        token = Path(args.token_file).expanduser().read_text(encoding="utf-8").strip()
        config = worker.rotate_token(token)
        _print_result(config.public(), args.json, lambda value: f"Worker token updated for {value['name']}; restart the worker to complete rotation.")
        return 0
    if args.worker_action == "status":
        _print_result(worker.status_public(), args.json, _format_worker_status)
        return 0
    if args.worker_action == "audit":
        result = commands.dispatch(commands.CommandRequest("worker.audit", {"limit": args.limit}))
        _print_result(result, args.json, lambda value: "\n".join(
            " ".join([str(entry.get("ts", "?")), str(entry.get("action", "?"))] + [f"{key}={entry[key]}" for key in sorted(entry) if key not in {"ts", "action"} and entry[key] is not None])
            for entry in value["entries"]) or "No worker audit entries.")
        return 0
    if args.worker_action == "install-service":
        if args.print:
            print(worker.service_unit_text(), end="")
            return 0
        unit, steps = worker.install_service()
        print(f"Wrote {unit}.")
        for step in steps:
            print(step[:1].upper() + step[1:] + ".")
        return 0
    if args.worker_action == "uninstall-service":
        print("Worker service removed." if worker.uninstall_service() else "No worker service is installed.")
        return 0
    if args.worker_action == "unenroll":
        removed = worker.unenroll()
        print("Worker enrolment removed." if removed else "This host is not enrolled as a worker.")
        return 0
    import signal
    import threading

    stop = threading.Event()
    session = worker.default_session(package_version())
    previous = {signum: signal.signal(signum, lambda *_: stop.set()) for signum in (signal.SIGINT, signal.SIGTERM)}
    print(f"Worker {session.config.name} connecting to {session.config.coordinator_url}; Ctrl-C stops it.", flush=True)
    try:
        worker.run(session, worker.websocket_connector(), stop=stop, log=lambda text: print(text, file=sys.stderr, flush=True))
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def _coordinator_url(raw: str, scheme: str) -> str:
    """The coordinator URL with the scheme the command stores: ``wss`` for a worker, ``https`` for the CLI.

    ``https://HOST`` and ``wss://HOST`` name the same coordinator, so either is accepted for both
    commands (CLIUX-041). Any other scheme is left for the command's own validation to report.
    """
    text = raw.strip()
    head, separator, rest = text.partition("://")
    if separator and head.lower() in {"https", "wss"}:
        return f"{scheme}://{rest.rstrip('/')}"
    return text


def _format_worker_status(value: dict[str, Any]) -> str:
    connection = {
        "connected": "connected to the coordinator",
        "disconnected": "disconnected from the coordinator",
        "last-connected": "last connection ended or is unavailable",
        "not-connected": "not connected yet",
    }.get(str(value.get("connection_state")), "connection state unknown")
    lines = [f"Worker {value.get('name', 'unknown')} · {connection}", f"Coordinator: {value.get('coordinator_url', 'unknown')}"]
    last_connected = value.get("last_connected_at")
    if last_connected:
        lines.append(f"Last connected: {last_connected}")
    lines.append(f"Worker service: {value.get('service_state', 'unknown')}")
    lines.append(f"Remote access: {'control and terminal' if value.get('allow_control') else 'read-only'}")
    lines.append(f"Remote creation: {'allowed' if value.get('allow_create') else 'disabled'}")
    roots = value.get("allow_root", [])
    lines.append("Allowed roots: " + (", ".join(str(root) for root in roots) if roots else "none"))
    lines.append(f"Remote power: {'allowed' if value.get('allow_power') else 'disabled'}")
    lines.append(f"Remote restore: {'allowed' if value.get('allow_restore') else 'disabled'}")
    lines.append(f"Worker ID: {str(value.get('worker_id', ''))[:8]}")
    return "\n".join(lines)


def _remote_command(args: argparse.Namespace) -> int:
    """``llm-bridge remote``: where ``--target`` requests are sent."""
    from . import bridge_remote as remote

    if args.remote_action == "login":
        token = read_verbatim(args.token_file, "--token-file").strip()
        ca_pem = read_verbatim(args.ca_file, "--ca-file") if args.ca_file else None
        config = remote.login(_coordinator_url(args.coordinator, "https"), token, ca_pem=ca_pem)
        print(f"Remote targets now go through {config.coordinator_url}. List them with: llm-bridge worker list")
        return 0
    if args.remote_action == "status":
        try:
            config = remote.load()
        except remote.RemoteError as exc:
            return _fail(str(exc), "usage", EXIT_USAGE, False)
        _print_result(config.public(), args.json, lambda value: f"coordinator_url: {value['coordinator_url']}")
        return 0
    print("Remote configuration removed." if remote.logout() else "No coordinator is configured.")
    return 0


def _split_target(arguments: list[str]) -> tuple[str | None, list[str]]:
    """Remove a leading ``--target NAME`` and validate the name."""
    if arguments[0] == "--target":
        if len(arguments) < 2 or arguments[1].startswith("-"):
            _fail("--target needs a name: local, all, or a worker name", "usage", EXIT_USAGE, False)
            return None, arguments
        raw, rest = arguments[1], arguments[2:]
    else:
        raw, rest = arguments[0].split("=", 1)[1], arguments[1:]
    try:
        return commands.validate_target(raw), rest
    except commands.CommandFailure as exc:
        _fail(str(exc), "usage", EXIT_USAGE, False)
        return None, arguments


# Windows supports the remote client only (docs/distributed/DECISIONS.md D8): the coordinator,
# `remote login|status|logout`, `worker list` and any command sent with `--target NAME`. Everything
# else needs tmux, a POSIX terminal or POSIX file locks.
WINDOWS_SUPPORTED_USAGE = "llm-bridge --target NAME COMMAND, llm-bridge remote login|status|logout, llm-bridge worker list"


def unsupported_on_windows(arguments: list[str]) -> dict[str, Any] | None:
    """The structured refusal for a local command on Windows, or None when the command is supported."""
    if sys.platform != "win32":
        return None
    if any(word in {"-h", "--help", "--version"} for word in arguments):
        return None
    if arguments[:1] in (["remote"], ["help"]) or arguments[:2] == ["worker", "list"]:
        return None
    command = " ".join(word for word in arguments[:2] if not word.startswith("-")) or "(none)"
    message = (f"'{command}' is not supported on Windows. Windows supports the remote client only: "
               f"{WINDOWS_SUPPORTED_USAGE}. Run local commands on Linux or macOS.")
    return {"schema_version": 1, "outcome": "failed",
            "error": commands.CommandError("unsupported-platform", message, {"platform": "windows", "command": command}).to_json()}


def _refuse_on_windows(arguments: list[str]) -> int | None:
    refusal = unsupported_on_windows(arguments)
    if refusal is None:
        return None
    if "--json" in arguments:
        from .bridge_help import troubleshooting_reference

        refusal["error"]["help"] = troubleshooting_reference("unsupported-platform")
        print(json.dumps(refusal, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _fail(refusal["error"]["message"], "unsupported-platform", EXIT_USAGE, False)
    return 2
