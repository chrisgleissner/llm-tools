"""Optional worker service: connects this host's Bridge to a coordinator.

The worker is an adapter. It executes coordinator requests through
``bridge_commands.execute`` (the same validation, transitions, locking and
idempotency as the local CLI), replicates durable events by cursor, and never
sends provider credentials or environment variables (WORK-001..010, SEC-005).

Nothing here runs unless the operator enrols the worker and starts it
(``llm-bridge worker enroll`` / ``llm-bridge worker run``); standalone Bridge
never imports this module (LOCAL-001, SEC-001).
"""

from __future__ import annotations

import dataclasses
import json
import os
import platform
import queue
import random
import re
import ssl
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import bridge_commands as commands
from .bridge_protocol import MAX_EVENTS_PER_BATCH, PROTOCOL_VERSION, SUPPORTED_VERSIONS, Message, ProtocolError, decode
from .bridge_store import BridgeStore, default_state_dir, store_path

WORKER_FILE = "worker.json"
CA_FILE = "coordinator-ca.pem"
AUDIT_FILE = "audit.jsonl"
AUDIT_MAX_BYTES = 1024 * 1024  # one rotation is kept: audit.jsonl.1
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
PING_INTERVAL = 20.0
SILENCE_LIMIT = 60.0
EVENT_BATCH = 200


class WorkerConfigError(ValueError):
    """The worker is not enrolled or its configuration is invalid."""


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    name: str
    coordinator_url: str
    token: str
    allow_control: bool
    max_generation: int = 0
    allow_terminal_view: bool = False  # read-only workers stream terminals only with this opt-in

    @property
    def terminal_view(self) -> bool:
        """Whether remote terminal viewing is allowed; control implies it."""
        return self.allow_control or self.allow_terminal_view

    def public(self) -> dict[str, Any]:
        """Configuration without the token, for status output and logs (AUTH-010)."""
        return {"worker_id": self.worker_id, "name": self.name, "coordinator_url": self.coordinator_url,
                "allow_control": self.allow_control, "allow_terminal_view": self.terminal_view, "max_generation": self.max_generation}


def worker_dir(env: dict[str, str] | None = None) -> Path:
    return default_state_dir(env) / "worker"


def _atomic_private_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if hasattr(os, "fchmod"):  # Windows has no permission bits; the profile directory's ACL protects the file
                os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if sys.platform != "win32":  # a directory cannot be opened for fsync on Windows
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


_AUDIT_LOCK = threading.Lock()
_AUDIT_VALUE = re.compile(r"[^\x20-\x7e]")


def _audit_value(value: Any) -> Any:
    """Bounded printable scalar; secrets never reach the audit call sites."""
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    return _AUDIT_VALUE.sub("?", str(value))[:200]


def audit(action: str, env: dict[str, str] | None = None, **detail: Any) -> None:
    """Append one security-relevant worker action to ``<state>/worker/audit.jsonl`` (SEC-007).

    Records identifiers, operations, outcomes and byte counts only: never the
    worker token, request parameters, prompts or terminal content. The file is
    0600, bounded to ``AUDIT_MAX_BYTES`` with one rotation, and a write failure
    never stops the worker.
    """
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action,
             **{key: _audit_value(value) for key, value in detail.items()}}
    path = worker_dir(env) / AUDIT_FILE
    try:
        with _AUDIT_LOCK:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > AUDIT_MAX_BYTES:
                os.replace(path, path.with_name(AUDIT_FILE + ".1"))
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def read_audit(limit: int = 50, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The newest ``limit`` audit entries, oldest first; unreadable lines are skipped."""
    entries: list[dict[str, Any]] = []
    for name in (AUDIT_FILE + ".1", AUDIT_FILE):
        try:
            lines = (worker_dir(env) / name).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                entries.append(value)
    return entries[-limit:] if limit > 0 else []


def enroll(name: str, coordinator_url: str, token: str, *, ca_pem: str | None, allow_control: bool, env: dict[str, str] | None = None,
           allow_terminal_view: bool = False) -> WorkerConfig:
    """Store the enrolment locally with private permissions; keeps an existing worker ID."""
    if not _NAME.fullmatch(name):
        raise WorkerConfigError("worker name must be lowercase letters, digits and '-' (at most 63)")
    if not coordinator_url.startswith("wss://"):
        raise WorkerConfigError("coordinator URL must start with wss:// (workers never connect without TLS)")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise WorkerConfigError("worker token must be 32-256 URL-safe characters, as printed by the coordinator")
    directory = worker_dir(env)
    try:
        previous = load_config(env)
        # A new token is a new registration on the coordinator, whose generations start again.
        worker_id, generation = previous.worker_id, previous.max_generation if previous.token == token else 0
    except WorkerConfigError:
        worker_id, generation = uuid.uuid4().hex, 0
    if ca_pem is not None:
        if "-----BEGIN CERTIFICATE-----" not in ca_pem:
            raise WorkerConfigError("CA file is not a PEM certificate")
        _atomic_private_write(directory / CA_FILE, ca_pem)
    config = WorkerConfig(worker_id, name, coordinator_url, token, allow_control, generation, allow_terminal_view)
    _save(config, env)
    audit("enroll", env, worker_id=worker_id, name=name, coordinator_url=coordinator_url, allow_control=allow_control,
          allow_terminal_view=allow_terminal_view, ca_file=ca_pem is not None)
    return config


def _save(config: WorkerConfig, env: dict[str, str] | None = None) -> None:
    _atomic_private_write(worker_dir(env) / WORKER_FILE, json.dumps(dataclasses.asdict(config), indent=2, sort_keys=True) + "\n")


def load_config(env: dict[str, str] | None = None) -> WorkerConfig:
    path = worker_dir(env) / WORKER_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return WorkerConfig(
            worker_id=str(value["worker_id"]), name=str(value["name"]), coordinator_url=str(value["coordinator_url"]),
            token=str(value["token"]), allow_control=bool(value["allow_control"]), max_generation=int(value.get("max_generation", 0)),
            allow_terminal_view=bool(value.get("allow_terminal_view", False)),
        )
    except FileNotFoundError:
        raise WorkerConfigError("this host is not enrolled as a worker; run llm-bridge worker enroll") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WorkerConfigError(f"worker configuration {path} is unreadable: {type(exc).__name__}") from None


def unenroll(env: dict[str, str] | None = None) -> bool:
    """Delete the enrolment, token and CA. The worker ID is not reused."""
    removed = False
    for name in (WORKER_FILE, CA_FILE):
        path = worker_dir(env) / name
        if path.exists():
            path.unlink()
            removed = True
    if removed:
        audit("unenroll", env)
    return removed


SERVICE_NAME = "llm-bridge-worker"


def service_unit_path(env: dict[str, str] | None = None) -> Path:
    from .common import home_dir

    return home_dir(env) / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"


def service_unit_text(env: dict[str, str] | None = None) -> str:
    """A systemd user unit for ``llm-bridge worker run`` (WORK-010, COORD-009).

    The unit bakes the operator's PATH (so missions the worker continues can
    find provider CLIs), the package location and any state-directory
    override. It restarts only on failure: ``worker run`` exits 0 after
    SIGTERM, which systemd sends on stop.
    """
    from . import common
    from .usage_service import _service_home, _service_path, _systemd_assignment, _systemd_word, _unit_value

    environ = os.environ if env is None else env
    exec_start = " ".join(_systemd_word(part, "the worker command") for part in common.python_module_argv("llm_tools.bridge", "worker", "run"))
    lines = [_systemd_assignment("PYTHONUNBUFFERED", "1"), _systemd_assignment("PATH", _unit_value(_service_path(env), "PATH")),
             _systemd_assignment("PYTHONPATH", _unit_value(common.package_root(), "the package directory"))]
    if environ.get("LLM_BRIDGE_STATE_DIR"):
        lines.append(_systemd_assignment("LLM_BRIDGE_STATE_DIR", _unit_value(Path(environ["LLM_BRIDGE_STATE_DIR"]).expanduser(), "the state directory")))
    environment = "\n".join(lines)
    return f"""[Unit]
Description=LLM Bridge worker (outbound connection to a Bridge coordinator)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
TimeoutStopSec=20
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=yes
{environment}
WorkingDirectory={_service_home(env)}

[Install]
WantedBy=default.target
"""


def install_service(env: dict[str, str] | None = None, *, run: Callable[[list[str]], int] | None = None) -> tuple[Path, list[str]]:
    """Write the unit and enable it; returns the unit path and any manual steps left."""
    import shutil
    import subprocess

    load_config(env)  # refuse to install a service that cannot start
    unit = service_unit_path(env)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(service_unit_text(env), encoding="utf-8")
    audit("service.install", env, unit=unit)
    runner = run or (lambda argv: subprocess.run(argv, check=False).returncode)
    if not shutil.which("systemctl"):
        return unit, [f"systemctl is not available; start it with: systemctl --user enable --now {SERVICE_NAME}.service"]
    if runner(["systemctl", "--user", "daemon-reload"]) or runner(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"]):
        return unit, [f"systemctl could not enable the unit; inspect: systemctl --user status {SERVICE_NAME}.service"]
    return unit, ["to keep the worker running while you are logged out, run once: loginctl enable-linger"]


def uninstall_service(env: dict[str, str] | None = None, *, run: Callable[[list[str]], int] | None = None) -> bool:
    import shutil
    import subprocess

    unit = service_unit_path(env)
    runner = run or (lambda argv: subprocess.run(argv, check=False, stderr=subprocess.DEVNULL).returncode)
    if shutil.which("systemctl") and unit.exists():
        runner(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"])
    existed = unit.exists()
    unit.unlink(missing_ok=True)
    if existed:
        if shutil.which("systemctl"):
            runner(["systemctl", "--user", "daemon-reload"])
        audit("service.uninstall", env)
    return existed


def tls_context(env: dict[str, str] | None = None) -> ssl.SSLContext:
    """Verify the coordinator against the enrolled CA, or the system store when none was given."""
    ca = worker_dir(env) / CA_FILE
    context = ssl.create_default_context(cafile=str(ca) if ca.exists() else None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class WorkerSession:
    """Protocol state for one worker; pure apart from the store and the config file."""

    def __init__(self, config: WorkerConfig, *, open_store: Callable[[], BridgeStore], software_version: str,
                 save: Callable[[WorkerConfig], None] = _save, terminal_launcher: "TerminalLauncher | None" = None,
                 audit: Callable[..., None] = audit) -> None:
        self.config = config
        self.audit = audit
        self.open_store = open_store
        self.software_version = software_version
        self.save = save
        self.generation = 0
        self.cursor: int | None = None  # coordinator's event cursor; None until welcome
        self.terminal_launcher = terminal_launcher
        self.outbox: queue.Queue[Message] = queue.Queue(maxsize=1024)  # messages from terminal threads
        self.close_reason: str | None = None  # set when the connection must be dropped after the replies

    def message(self, kind: str, payload: dict[str, Any]) -> Message:
        return Message(kind, self.config.worker_id, self.generation, payload)

    def hello(self) -> Message:
        with self.open_store() as store:
            missions = [
                {"id": row["id"], "name": row.get("name") or "", "status": row["status"], "folder": Path(str(row["cwd"])).name,
                 "updated_at": row.get("updated_at")}
                for row in store.list_missions()
            ]
            cursor = store.last_event_id()
        allowed = {spec["operation"] for spec in commands.operation_catalog() if self.config.allow_control or not spec["mutating"]}
        return Message("hello", self.config.worker_id, 0, {
            "worker_id": self.config.worker_id,
            "name": self.config.name,
            "software_version": self.software_version,
            "protocol_versions": list(SUPPORTED_VERSIONS),
            "platform": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
            "capabilities": [spec for spec in commands.operation_catalog() if spec["operation"] in allowed],
            "missions": missions,
            "event_cursor": cursor,
        })

    def error(self, code: str, text: str, in_reply_to: str | None = None) -> Message:
        payload: dict[str, Any] = {"code": code, "message": text}
        if in_reply_to:
            payload["in_reply_to"] = in_reply_to
        return self.message("error", payload)

    def handle(self, message: Message) -> list[Message]:
        """React to one decoded coordinator message."""
        if message.type == "welcome":
            generation = int(message.payload["generation"])
            if generation <= self.config.max_generation:
                self.audit("connection.refused", reason="stale-generation", generation=generation)
                self.close_reason = "stale generation in welcome"
                return [self.error("stale-generation", f"generation {generation} is not newer than {self.config.max_generation}", message.id)]
            self.config = dataclasses.replace(self.config, max_generation=generation)
            self.save(self.config)  # durable before any command of this generation is accepted
            self.generation = generation
            self.cursor = int(message.payload["event_cursor"])
            self.audit("connection.accepted", generation=generation)
            return []
        if message.type == "error":
            return []  # never answer an error with an error; that can loop forever
        if self.generation == 0:
            return [self.error("not-welcomed", f"{message.type} before welcome", message.id)]
        if message.type == "ping":
            return [self.message("pong", {})]
        if message.type == "pong":
            return []
        if message.type == "events.nack":
            self.cursor = int(message.payload["expected_after"])
            return []
        if message.type == "terminal.open":
            return self._terminal_open(dict(message.payload))
        if message.type == "terminal.close":
            if self.terminal_launcher is not None:
                self.terminal_launcher.close(str(message.payload["session_id"]))
            return []
        if message.type != "command":
            return [self.error("unsupported-message", f"workers do not accept {message.type}", message.id)]
        if message.generation != self.generation or message.generation < self.config.max_generation:
            self.audit("command.refused", reason="stale-generation", generation=message.generation)
            return [self.error("stale-generation", "command from an older coordinator connection", message.id)]
        request = commands.CommandRequest.from_json(dict(message.payload))
        return self._command(request)

    def terminal_status(self, session_id: str, state: str, mode: str, reason: str) -> None:
        """Queue a ``terminal.status`` for the run loop (called from terminal threads)."""
        self.audit(f"terminal.{state}", session_id=session_id, mode=mode, reason=reason)
        try:
            self.outbox.put_nowait(self.message("terminal.status", {"session_id": session_id, "state": state, "mode": mode, "reason": reason[:500]}))
        except queue.Full:
            pass

    def _terminal_open(self, payload: dict[str, Any]) -> list[Message]:
        session_id, mission_id, mode = str(payload["session_id"]), str(payload["mission_id"]), str(payload["mode"])
        self.audit("terminal.requested", session_id=session_id, mission_id=mission_id, mode=mode)
        if self.terminal_launcher is None:
            return [self._terminal_refused(session_id, "terminal access is not available on this worker")]
        if not self.config.terminal_view:
            return [self._terminal_refused(session_id, "this worker does not share terminals; enrol it with --allow-terminal-view or --allow-control")]
        with self.open_store() as store:
            known = store.mission_id_if_exists(mission_id)
        if known is None:
            return [self._terminal_refused(session_id, "unknown mission")]
        self.terminal_launcher.open(self, session_id, known, mode, allow_write=self.config.allow_control)
        return []

    def _terminal_refused(self, session_id: str, reason: str) -> Message:
        self.audit("terminal.refused", session_id=session_id, mode="view", reason=reason)
        return self.message("terminal.status", {"session_id": session_id, "state": "refused", "mode": "view", "reason": reason})

    def _command(self, request: commands.CommandRequest) -> list[Message]:
        spec = commands.OPERATIONS.get(request.operation)
        duplicate = False
        if spec is not None and spec.mutating:
            with self.open_store() as store:
                duplicate = store.command_exists(request.request_id)
        ack = self.message("command.ack", {"request_id": request.request_id, "status": "duplicate" if duplicate else "received"})
        if spec is not None and spec.mutating and not self.config.allow_control:
            result = commands.CommandResult(request.request_id, request.operation, "rejected",
                                            error=commands.CommandError("forbidden", "this worker accepts read-only remote operations"))
        else:
            result = commands.execute(dataclasses.replace(request, target=commands.LOCAL_TARGET))
        if spec is None or spec.mutating or result.error is not None:
            self.audit("command", operation=request.operation, request_id=request.request_id, mission_id=request.mission_id,
                       outcome=result.outcome, error=result.error.code if result.error else None, duplicate=duplicate)
        return [ack, self.message("command.result", result.to_json())]

    def next_events(self) -> Message | None:
        """The next event batch after the coordinator's cursor, or ``None`` when up to date."""
        if self.cursor is None:
            return None
        with self.open_store() as store:
            events = store.events_since(self.cursor, limit=min(EVENT_BATCH, MAX_EVENTS_PER_BATCH))
        if not events:
            return None
        after = self.cursor
        self.cursor = int(events[-1]["id"])
        return self.message("events", {"after": after, "through": self.cursor, "events": events})


Connector = Callable[[WorkerConfig], Any]


class TerminalLauncher:
    """Runs each remote terminal session in its own thread with its own WebSocket."""

    def __init__(self, connect: Callable[[WorkerConfig, str], Any], source_factory: Callable[[str], Any] | None = None) -> None:
        self.connect = connect
        self.source_factory = source_factory
        self._stops: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def open(self, session: "WorkerSession", session_id: str, mission_id: str, mode: str, *, allow_write: bool) -> None:
        stop = threading.Event()
        with self._lock:
            if session_id in self._stops or len(self._stops) >= 16:
                session.terminal_status(session_id, "refused", "view", "too many terminal sessions or duplicate session id")
                return
            self._stops[session_id] = stop
        thread = threading.Thread(target=self._run, args=(session, session_id, mission_id, mode, allow_write, stop), daemon=True)
        thread.start()

    def close(self, session_id: str) -> None:
        with self._lock:
            stop = self._stops.get(session_id)
        if stop is not None:
            stop.set()

    def _run(self, session: "WorkerSession", session_id: str, mission_id: str, mode: str, allow_write: bool, stop: threading.Event) -> None:
        from .bridge_terminal_stream import TerminalSession, TmuxPaneSource

        try:
            try:
                source = (self.source_factory or TmuxPaneSource)(mission_id)
            except Exception as exc:  # noqa: BLE001 - reported to the coordinator, not raised
                session.terminal_status(session_id, "refused", "view", str(exc)[:200] or type(exc).__name__)
                return
            try:
                connection = self.connect(session.config, session_id)
            except Exception as exc:  # noqa: BLE001
                source.close()
                session.terminal_status(session_id, "refused", "view", f"terminal connection failed: {type(exc).__name__}")
                return
            terminal = TerminalSession(session_id, mission_id, mode, source, connection, allow_write=allow_write, stop=stop)
            session.terminal_status(session_id, "opened", "write" if mode == "write" and allow_write else "view", terminal.reason)
            reason = terminal.run()
            session.audit("terminal.input", session_id=session_id, mission_id=mission_id, bytes=terminal.input_bytes, writes=terminal.input_frames)
            session.terminal_status(session_id, "closed", "view", reason)
        finally:
            with self._lock:
                self._stops.pop(session_id, None)


def run(session: WorkerSession, connect: Connector, *, stop: threading.Event, log: Callable[[str], None] = lambda text: None,
        idle: float = 0.5, max_backoff: float = 60.0) -> None:
    """Keep one coordinator connection alive until ``stop`` is set (WORK-010).

    Execution never depends on this loop: a lost connection only delays
    replication and remote commands, and the loop reconnects with jittered
    exponential backoff (ARCH-007).
    """
    backoff = 1.0
    session.audit("worker.start", allow_control=session.config.allow_control)
    while not stop.is_set():
        connection = None
        try:
            connection = connect(session.config)
            session.generation, session.cursor, session.close_reason = 0, None, None
            connection.send_text(session.hello().encode())
            last_heard = last_ping = time.monotonic()
            while not stop.is_set():
                text = connection.receive_text(timeout=idle)
                now = time.monotonic()
                if text is not None:
                    last_heard = now
                    backoff = 1.0
                    try:
                        replies = session.handle(decode(text))
                    except ProtocolError as exc:
                        replies = [session.error(exc.code, str(exc))]
                    for reply in replies:
                        connection.send_text(reply.encode())
                    if session.close_reason:
                        reason, session.close_reason = session.close_reason, None
                        raise ConnectionAbortedError(reason)
                while True:
                    try:
                        connection.send_text(session.outbox.get_nowait().encode())
                    except queue.Empty:
                        break
                batch = session.next_events()
                if batch is not None:
                    connection.send_text(batch.encode())
                if now - last_heard > SILENCE_LIMIT:
                    raise TimeoutError("coordinator silent")
                if now - last_ping > PING_INTERVAL and session.generation:
                    connection.send_text(session.message("ping", {}).encode())
                    last_ping = now
        except Exception as exc:  # noqa: BLE001 - every failure leads to a bounded reconnect
            log(f"worker connection ended: {type(exc).__name__}: {str(exc)[:200]}")
            if session.generation:  # only welcomed connections; failed attempts would flood the audit
                session.audit("connection.ended", generation=session.generation, reason=type(exc).__name__)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001
                    pass
        if stop.wait(backoff * random.uniform(0.5, 1.0)):
            break
        backoff = min(max_backoff, backoff * 2)
    session.audit("worker.stop")


def default_session(software_version: str, env: dict[str, str] | None = None) -> WorkerSession:
    config = load_config(env)
    return WorkerSession(config, open_store=lambda: BridgeStore(store_path(env), readonly=True), software_version=software_version,  # every use reads
                         save=lambda value: _save(value, env), terminal_launcher=TerminalLauncher(terminal_connector(env)))


def terminal_connector(env: dict[str, str] | None = None) -> Callable[[WorkerConfig, str], Any]:
    from .bridge_ws import WebSocketClient

    def connect(config: WorkerConfig, session_id: str) -> Any:
        return WebSocketClient.connect(config.coordinator_url.rstrip("/") + f"/api/v1/worker/terminal/{session_id}",
                                       headers={"Authorization": f"Bearer {config.token}"}, context=tls_context(env))

    return connect


def websocket_connector(env: dict[str, str] | None = None) -> Connector:
    from .bridge_ws import WebSocketClient

    def connect(config: WorkerConfig) -> Any:
        return WebSocketClient.connect(config.coordinator_url.rstrip("/") + "/api/v1/worker/connect",
                                       headers={"Authorization": f"Bearer {config.token}"}, context=tls_context(env))

    return connect


__all__ = ["AUDIT_FILE", "PROTOCOL_VERSION", "audit", "read_audit", "WorkerConfig", "WorkerConfigError", "WorkerSession", "enroll", "load_config", "run"]
