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
import random
import re
import ssl
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

    def public(self) -> dict[str, Any]:
        """Configuration without the token, for status output and logs (AUTH-010)."""
        return {"worker_id": self.worker_id, "name": self.name, "coordinator_url": self.coordinator_url,
                "allow_control": self.allow_control, "max_generation": self.max_generation}


def worker_dir(env: dict[str, str] | None = None) -> Path:
    return default_state_dir(env) / "worker"


def _atomic_private_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def enroll(name: str, coordinator_url: str, token: str, *, ca_pem: str | None, allow_control: bool, env: dict[str, str] | None = None) -> WorkerConfig:
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
        worker_id, generation = previous.worker_id, previous.max_generation
    except WorkerConfigError:
        worker_id, generation = uuid.uuid4().hex, 0
    if ca_pem is not None:
        if "-----BEGIN CERTIFICATE-----" not in ca_pem:
            raise WorkerConfigError("CA file is not a PEM certificate")
        _atomic_private_write(directory / CA_FILE, ca_pem)
    config = WorkerConfig(worker_id, name, coordinator_url, token, allow_control, generation)
    _save(config, env)
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
    return removed


def tls_context(env: dict[str, str] | None = None) -> ssl.SSLContext:
    """Verify the coordinator against the enrolled CA, or the system store when none was given."""
    ca = worker_dir(env) / CA_FILE
    context = ssl.create_default_context(cafile=str(ca) if ca.exists() else None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class WorkerSession:
    """Protocol state for one worker; pure apart from the store and the config file."""

    def __init__(self, config: WorkerConfig, *, open_store: Callable[[], BridgeStore], software_version: str,
                 save: Callable[[WorkerConfig], None] = _save) -> None:
        self.config = config
        self.open_store = open_store
        self.software_version = software_version
        self.save = save
        self.generation = 0
        self.cursor: int | None = None  # coordinator's event cursor; None until welcome

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
                return [self.error("stale-generation", f"generation {generation} is not newer than {self.config.max_generation}", message.id)]
            self.config = dataclasses.replace(self.config, max_generation=generation)
            self.save(self.config)  # durable before any command of this generation is accepted
            self.generation = generation
            self.cursor = int(message.payload["event_cursor"])
            return []
        if self.generation == 0:
            return [self.error("not-welcomed", f"{message.type} before welcome", message.id)]
        if message.type == "ping":
            return [self.message("pong", {})]
        if message.type == "pong":
            return []
        if message.type == "events.nack":
            self.cursor = int(message.payload["expected_after"])
            return []
        if message.type == "error":
            return []
        if message.type != "command":
            return [self.error("unsupported-message", f"workers do not accept {message.type}", message.id)]
        if message.generation != self.generation or message.generation < self.config.max_generation:
            return [self.error("stale-generation", "command from an older coordinator connection", message.id)]
        request = commands.CommandRequest.from_json(dict(message.payload))
        return self._command(request)

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


def run(session: WorkerSession, connect: Connector, *, stop: threading.Event, log: Callable[[str], None] = lambda text: None,
        idle: float = 0.5, max_backoff: float = 60.0) -> None:
    """Keep one coordinator connection alive until ``stop`` is set (WORK-010).

    Execution never depends on this loop: a lost connection only delays
    replication and remote commands, and the loop reconnects with jittered
    exponential backoff (ARCH-007).
    """
    backoff = 1.0
    while not stop.is_set():
        connection = None
        try:
            connection = connect(session.config)
            session.generation, session.cursor = 0, None
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
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001
                    pass
        if stop.wait(backoff * random.uniform(0.5, 1.0)):
            break
        backoff = min(max_backoff, backoff * 2)


def default_session(software_version: str, env: dict[str, str] | None = None) -> WorkerSession:
    config = load_config(env)
    return WorkerSession(config, open_store=lambda: BridgeStore(store_path(env)), software_version=software_version,
                         save=lambda value: _save(value, env))


def websocket_connector(env: dict[str, str] | None = None) -> Connector:
    from .bridge_ws import WebSocketClient

    def connect(config: WorkerConfig) -> Any:
        return WebSocketClient.connect(config.coordinator_url.rstrip("/") + "/api/v1/worker/connect",
                                       headers={"Authorization": f"Bearer {config.token}"}, context=tls_context(env))

    return connect


__all__ = ["PROTOCOL_VERSION", "WorkerConfig", "WorkerConfigError", "WorkerSession", "enroll", "load_config", "run"]
