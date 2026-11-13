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
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import bridge_commands as commands
from .bridge_protocol import MAX_DELETED_MISSIONS_PER_MESSAGE, MAX_EVENTS_PER_BATCH, PROTOCOL_VERSION, SUPPORTED_VERSIONS, Message, ProtocolError, decode
from .bridge_store import BridgeStore, default_state_dir, store_path
from .bridge_states import LIVE_MISSION_STATUSES
from .bridge_status_bound import mission_digest
from .bridge_vocab import EventKind

WORKER_FILE = "worker.json"
CA_FILE = "coordinator-ca.pem"
CLIENT_CERT_FILE = "worker-client.pem"
CLIENT_KEY_FILE = "worker-client.key"
AUDIT_FILE = "audit.jsonl"
AUDIT_MAX_BYTES = 1024 * 1024  # one rotation is kept: audit.jsonl.1
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
PING_INTERVAL = 20.0
SILENCE_LIMIT = 60.0
EVENT_BATCH = 25  # events per batch
EVENT_BATCH_BYTES = 16 * 1024  # serialized event bytes per batch (one oversized event still goes alone)
EVENT_SCAN_ROWS = 400  # database rows read per batch, including rows that are not replicated
EVENT_RATE_BYTES_PER_SECOND = 20 * 1024  # sustained upper bound for event traffic
# Bookkeeping the controller writes every few seconds. The coordinator never reads either kind, and the
# web console lists them only as hidden plumbing, so a heartbeat is not replicated and only the newest
# capacity refresh of each mission is.
UNREPLICATED_EVENT_KINDS = frozenset({EventKind.CONTROLLER_HEARTBEAT.value})
LATEST_ONLY_EVENT_KINDS = frozenset({EventKind.CAPACITY_REFRESH.value})
MISSION_QUESTION_REFRESH_SECS = 5.0
MAX_QUESTION_MISSIONS_PER_REFRESH = 64


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
    allow_create: bool = False
    allow_roots: tuple[str, ...] = ()
    allow_power: bool = False
    allow_restore: bool = False
    client_certificate: bool = False

    @property
    def terminal_view(self) -> bool:
        """Whether remote terminal viewing is allowed; control implies it."""
        return self.allow_control or self.allow_terminal_view

    def public(self) -> dict[str, Any]:
        """Configuration without the token, for status output and logs (AUTH-010)."""
        return {"worker_id": self.worker_id, "name": self.name, "coordinator_url": self.coordinator_url,
                "allow_control": self.allow_control, "allow_terminal_view": self.terminal_view, "max_generation": self.max_generation,
                "allow_create": self.allow_create, "allow_root": list(self.allow_roots), "allow_power": self.allow_power,
                "allow_restore": self.allow_restore, "mutual_tls": self.client_certificate}


def worker_dir(env: dict[str, str] | None = None) -> Path:
    return default_state_dir(env) / "worker"


OUTCOMES_FILE = "outcomes.sqlite3"
OUTCOMES_KEEP = 10_000
OUTCOMES_KEEP_SECONDS = 7 * 86_400


class OutcomeLedger:
    """Final result of every mutating request, by request id, in a private file of the worker directory.

    The coordinator sends an unanswered command again (after a lost frame or a reconnect). A request id with a
    recorded outcome is answered with the stored result and is not executed again, whatever the operation does
    internally. The outcome is recorded before ``command.result`` is sent. A crash after the effect and before
    the record can run the command once more after the restart. Bounded: the newest 10,000 rows, at most 7 days,
    pruned on every write.
    """

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        created = not self.path.exists()
        if created:
            _make_private_dir(self.path.parent)
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        os.chmod(self.path, 0o600)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS outcomes(request_id TEXT PRIMARY KEY, recorded REAL NOT NULL, result TEXT NOT NULL)")
        return db

    def get(self, request_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            row = db.execute("SELECT result FROM outcomes WHERE request_id=?", (request_id,)).fetchone()
        finally:
            db.close()
        return json.loads(row[0]) if row else None

    def record(self, request_id: str, result: dict[str, Any]) -> None:
        now = self.clock()
        db = self._connect()
        try:
            with db:
                db.execute("INSERT OR REPLACE INTO outcomes(request_id, recorded, result) VALUES(?,?,?)", (request_id, now, json.dumps(result)))
                db.execute("DELETE FROM outcomes WHERE recorded<?", (now - OUTCOMES_KEEP_SECONDS,))
                db.execute("DELETE FROM outcomes WHERE rowid NOT IN (SELECT rowid FROM outcomes ORDER BY recorded DESC, rowid DESC LIMIT ?)", (OUTCOMES_KEEP,))
        finally:
            db.close()


def _make_private_dir(path: Path) -> None:
    """Create ``path`` and its missing parents as 0700; Bridge refuses a state directory that others can enter.

    ``mkdir(mode=0o700, parents=True)`` applies the mode to the last component only, so the first
    command on a new host (``worker enroll``, ``remote login``) would otherwise leave the state
    directory with the default umask mode and every later Bridge command would fail.
    """
    from .common import make_private_dirs

    make_private_dirs(path)


def _atomic_private_write(path: Path, text: str) -> None:
    _make_private_dir(path.parent)
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
            _make_private_dir(path.parent)
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


def status_public(env: dict[str, str] | None = None, *, service_state: str | None = None) -> dict[str, Any]:
    """Worker enrollment plus best-effort service and last coordinator connection state."""
    config = load_config(env)
    if service_state is None:
        import shutil
        import subprocess

        unit = service_unit_path(env)
        if not unit.exists():
            service_state = "not-installed"
        elif not shutil.which("systemctl"):
            service_state = "unknown"
        else:
            try:
                check = subprocess.run(
                    ["systemctl", "--user", "is-active", f"{SERVICE_NAME}.service"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                service_state = "active" if check.returncode == 0 and check.stdout.strip() == "active" else "inactive"
            except (OSError, subprocess.SubprocessError):
                service_state = "unknown"
    connection_events = [
        entry for entry in read_audit(200, env)
        if entry.get("action") in {"connection.accepted", "connection.ended", "connection.refused"}
    ]
    latest = connection_events[-1] if connection_events else None
    connected = bool(latest and latest.get("action") == "connection.accepted" and service_state == "active")
    last_connected = next((entry.get("ts") for entry in reversed(connection_events) if entry.get("action") == "connection.accepted"), None)
    if connected:
        connection_state = "connected"
    elif latest and latest.get("action") in {"connection.ended", "connection.refused"}:
        connection_state = "disconnected"
    elif last_connected:
        connection_state = "last-connected"
    else:
        connection_state = "not-connected"
    return {
        **config.public(),
        "connection_state": connection_state,
        "last_connected_at": last_connected,
        "service_state": service_state,
    }


def default_worker_name() -> str:
    """A short, command-safe default derived from this machine's host name."""
    short = platform.node().split(".", 1)[0].casefold()
    name = re.sub(r"[^a-z0-9]+", "-", short).strip("-")[:63].strip("-")
    return name if _NAME.fullmatch(name) else "worker"


def _canonical_roots(roots: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Resolve allowed roots once and require real, existing directories."""
    if len(roots) > 32:
        raise WorkerConfigError("at most 32 --allow-root paths may be enrolled")
    result: list[str] = []
    for root in roots:
        try:
            canonical = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkerConfigError(f"allowed root cannot be resolved: {root!r}") from exc
        if not canonical.is_dir():
            raise WorkerConfigError(f"allowed root is not a directory: {canonical}")
        value = str(canonical)
        if len(value.encode("utf-8")) > 4096:
            raise WorkerConfigError("allowed root path exceeds 4096 UTF-8 bytes")
        if value not in result:
            result.append(value)
    return tuple(result)


def _stored_roots(value: Any) -> tuple[str, ...]:
    """Validate the serialized shape; path identity is rechecked at authorization time."""
    if not isinstance(value, list) or not all(isinstance(root, str) for root in value):
        if value in (None, ()):
            return ()
        raise WorkerConfigError("worker allowed roots must be a list of paths")
    result: list[str] = []
    for root in value:
        path = Path(root)
        if not path.is_absolute() or str(path) != root:
            raise WorkerConfigError(f"enrolled allowed root is not an absolute canonical path: {root!r}")
        if root not in result:
            result.append(root)
    return tuple(result)


def _path_allowed(path: str, roots: tuple[str, ...]) -> bool:
    """Check a path against canonical roots after resolving existing symlinks."""
    if not roots:
        return False
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
        for root in roots:
            canonical_root = Path(root).resolve(strict=True)
            if canonical_root != Path(root):
                continue  # the enrolled root has since become a symlink or moved
            candidate.relative_to(canonical_root)
            return True
    except (OSError, RuntimeError, ValueError):
        return False
    return False


def enroll(name: str, coordinator_url: str, token: str, *, ca_pem: str | None, allow_control: bool, env: dict[str, str] | None = None,
           allow_terminal_view: bool = False, allow_create: bool = False, allow_roots: tuple[str, ...] | list[str] = (),
           allow_power: bool = False, allow_restore: bool = False, client_cert_pem: str | None = None,
           client_key_pem: str | None = None) -> WorkerConfig:
    """Store the enrolment locally with private permissions; keeps an existing worker ID."""
    if not _NAME.fullmatch(name):
        raise WorkerConfigError("worker name must be lowercase letters, digits and '-' (at most 63)")
    if not coordinator_url.startswith("wss://"):
        raise WorkerConfigError("coordinator URL must start with wss:// (workers never connect without TLS)")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise WorkerConfigError("worker token must be 32-256 URL-safe characters, as printed by the coordinator")
    roots = _canonical_roots(allow_roots)
    if allow_create and not roots:
        raise WorkerConfigError("--allow-create requires at least one --allow-root")
    if allow_restore and not roots:
        raise WorkerConfigError("--allow-restore requires at least one --allow-root")
    if (client_cert_pem is None) != (client_key_pem is None):
        raise WorkerConfigError("client certificate and key must be supplied together")
    if client_cert_pem is not None and "-----BEGIN CERTIFICATE-----" not in client_cert_pem:
        raise WorkerConfigError("worker client certificate is not a PEM certificate")
    if client_key_pem is not None and "PRIVATE KEY-----" not in client_key_pem:
        raise WorkerConfigError("worker client key is not a PEM private key")
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
    if client_cert_pem is not None and client_key_pem is not None:
        _atomic_private_write(directory / CLIENT_CERT_FILE, client_cert_pem)
        _atomic_private_write(directory / CLIENT_KEY_FILE, client_key_pem)
    client_certificate = client_cert_pem is not None or (directory / CLIENT_CERT_FILE).is_file() and (directory / CLIENT_KEY_FILE).is_file()
    config = WorkerConfig(worker_id, name, coordinator_url, token, allow_control, generation, allow_terminal_view,
                          allow_create, roots, allow_power, allow_restore, client_certificate)
    _save(config, env)
    audit("enroll", env, worker_id=worker_id, name=name, coordinator_url=coordinator_url, allow_control=allow_control,
          allow_terminal_view=allow_terminal_view, allow_create=allow_create, allow_root_count=len(roots),
          allow_power=allow_power, allow_restore=allow_restore, ca_file=ca_pem is not None,
          mutual_tls=client_certificate)
    return config


def _save(config: WorkerConfig, env: dict[str, str] | None = None) -> None:
    _atomic_private_write(worker_dir(env) / WORKER_FILE, json.dumps(dataclasses.asdict(config), indent=2, sort_keys=True) + "\n")


def load_config(env: dict[str, str] | None = None) -> WorkerConfig:
    path = worker_dir(env) / WORKER_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        plain = not str(value["coordinator_url"]).startswith("wss://")  # enrolment enforces this; an edited file must not bypass it
        config = WorkerConfig(
            worker_id=str(value["worker_id"]), name=str(value["name"]), coordinator_url=str(value["coordinator_url"]),
            token=str(value["token"]), allow_control=bool(value["allow_control"]), max_generation=int(value.get("max_generation", 0)),
            allow_terminal_view=bool(value.get("allow_terminal_view", False)),
            allow_create=bool(value.get("allow_create", False)),
            allow_roots=_stored_roots(value.get("allow_roots", [])),
            allow_power=bool(value.get("allow_power", False)), allow_restore=bool(value.get("allow_restore", False)),
            client_certificate=bool(value.get("client_certificate", False)),
        )
    except FileNotFoundError:
        raise WorkerConfigError("this host is not enrolled as a worker; run llm-bridge worker enroll") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WorkerConfigError(f"worker configuration {path} is unreadable: {type(exc).__name__}") from None
    if plain:
        raise WorkerConfigError(f"worker configuration {path} must name the coordinator with wss:// (workers never connect without TLS); run llm-bridge worker enroll again")
    return config


def rotate_token(token: str, env: dict[str, str] | None = None) -> WorkerConfig:
    """Replace only this enrollment's bearer token after coordinator-side staging.

    The worker ID, coordinator CA and optional client certificate remain in place, so this does
    not re-enroll or reset the replication generation. Restart a foreground/service worker after
    writing the token so its next connection proves the staged credential to the coordinator.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise WorkerConfigError("worker token must be 32-256 URL-safe characters")
    config = load_config(env)
    updated = dataclasses.replace(config, token=token)
    _save(updated, env)
    audit("token.rotate", env, worker_id=config.worker_id, name=config.name)
    return updated


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
    if not (worker_dir(env) / WORKER_FILE).exists():
        return context  # not enrolled: no client identity to present
    config = load_config(env)
    if config.client_certificate:
        cert = worker_dir(env) / CLIENT_CERT_FILE
        key = worker_dir(env) / CLIENT_KEY_FILE
        if not cert.is_file() or not key.is_file():
            raise WorkerConfigError("worker mTLS is enabled but the enrolled client certificate or key is missing")
        context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return context


@dataclass(frozen=True)
class InFlightBatch:
    """The one events batch sent but not yet known to be processed by the coordinator."""

    seq: int
    after: int
    through: int
    message_id: str = ""
    tombstones: tuple[str, ...] = ()  # deleted mission IDs this batch carries; marked replicated once its pong arrives


class WorkerSession:
    """Protocol state for one worker; pure apart from the store and the config file.

    Event replication keeps at most one batch in flight. ``cursor`` is the last position the
    coordinator is known to have stored. After a batch is sent, ``barrier_ping`` sends a ``ping``;
    the coordinator handles one connection's messages in order, so its ``pong`` proves the batch was
    processed, and any ``events.nack`` for it arrived before that pong. No coordinator support
    beyond protocol v1 is needed.
    """

    def __init__(self, config: WorkerConfig, *, open_store: Callable[[], BridgeStore], software_version: str,
                 save: Callable[[WorkerConfig], None] = _save, terminal_launcher: "TerminalLauncher | None" = None,
                 audit: Callable[..., None] = audit, outcomes: OutcomeLedger | None = None) -> None:
        self.config = config
        self.outcomes = outcomes if outcomes is not None else OutcomeLedger(worker_dir() / OUTCOMES_FILE)
        self.audit = audit
        self.open_store = open_store
        self.software_version = software_version
        self.save = save
        self.generation = 0
        self.cursor: int | None = None  # coordinator's confirmed event cursor; None until welcome
        self.clock: Callable[[], float] = lambda: time.monotonic()
        self.inflight: InFlightBatch | None = None
        self.send_not_before = 0.0  # rate cap: no batch before this clock reading
        self.batch_seq = 0
        self.pending_pongs: deque[int] = deque(maxlen=64)  # per ping sent: the batch it fences, or 0
        self.terminal_launcher = terminal_launcher
        self.outbox: queue.Queue[Message] = queue.Queue(maxsize=1024)  # messages from terminal threads
        self.close_reason: str | None = None  # set when the connection must be dropped after the replies
        self.last_question_refresh = 0.0
        self.last_question_mission_id: str | None = None
        self.mission_questions: dict[str, dict[str, str] | None] = {}
        self.tombstones_unsupported = False  # the coordinator rejected `deleted_missions` on this connection

    def message(self, kind: str, payload: dict[str, Any]) -> Message:
        return Message(kind, self.config.worker_id, self.generation, payload)

    def hello(self) -> Message:
        with self.open_store() as store:
            missions = [self._mission_summary(store, row) for row in store.list_missions()]
            cursor = store.last_event_id()
        allowed = {
            spec["operation"] for spec in commands.operation_catalog()
            if (not spec["mutating"] or self.config.allow_control
                or (spec["operation"] == "mission.create" and self.config.allow_create)
                or (spec["operation"] == "mission.suspend_until_credits" and self.config.allow_power)
                or (spec["operation"] == "worker.restore" and self.config.allow_restore))
            and (spec["operation"] != "mission.create" or self.config.allow_create)
            and (spec["operation"] != "mission.suspend_until_credits" or self.config.allow_power)
            and (spec["operation"] != "worker.restore" or self.config.allow_restore)
        }
        payload: dict[str, Any] = {
            "worker_id": self.config.worker_id,
            "name": self.config.name,
            "software_version": self.software_version,
            "protocol_versions": list(SUPPORTED_VERSIONS),
            "platform": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
            "capabilities": [spec for spec in commands.operation_catalog() if spec["operation"] in allowed],
            "worker_policy": {"allow_control": self.config.allow_control, "allow_terminal_view": self.config.terminal_view,
                              "allow_create": self.config.allow_create, "allow_root": list(self.config.allow_roots),
                              "allow_power": self.config.allow_power, "allow_restore": self.config.allow_restore},
            "missions": missions,
            "event_cursor": cursor,
        }
        host_name = platform.node().strip()
        if host_name and len(host_name.encode("utf-8")) <= 255 and not any(ord(char) < 32 or ord(char) == 127 for char in host_name):
            payload["host_name"] = host_name
        return Message("hello", self.config.worker_id, 0, payload)

    def _live_projection(self, store: BridgeStore, mission_id: str) -> dict[str, Any] | None:
        """The existing bounded status projection with one task, or ``None`` when it cannot be read."""
        try:
            from .bridge_monitor import project_snapshot

            return dict(project_snapshot(store, mission_id, task_limit=1, prompt_chars=1))
        except Exception:  # noqa: BLE001 - observation must never stop worker replication
            return None

    @staticmethod
    def _provider_question(snapshot: dict[str, Any] | None) -> dict[str, str] | None:
        """The pending provider question in a status projection, bounded."""
        needs = snapshot.get("needs_you") if snapshot else None
        if not isinstance(needs, dict) or needs.get("kind") != "provider":
            return None
        message = needs.get("message")
        command = needs.get("command")
        if not isinstance(message, str) or not message or not isinstance(command, str) or not command:
            return None
        return {"kind": "provider", "message": message[:2048], "command": command[:512]}

    @staticmethod
    def _compact_needs(snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """What needs the operator, bounded: the kind, the message, the command and the task numbers."""
        needs = snapshot.get("needs_you")
        if not isinstance(needs, dict):
            return None
        compact: dict[str, Any] = {}
        if needs.get("kind") in {"approval", "failed", "blocked", "proposal", "capacity", "provider"}:
            compact["kind"] = needs["kind"]
        if isinstance(needs.get("message"), str):
            compact["message"] = needs["message"][:2048]
        if isinstance(needs.get("command"), str):
            compact["command"] = needs["command"][:512]
        if isinstance(needs.get("tasks"), list):
            compact["tasks"] = [task for task in needs["tasks"][:32] if isinstance(task, int) and task > 0]
        if isinstance(needs.get("hold"), bool):
            compact["hold"] = needs["hold"]
        return compact or None

    def _mission_summary(self, store: BridgeStore, row: dict[str, Any]) -> dict[str, Any]:
        mission_id = str(row["id"])
        summary: dict[str, Any] = {
            "id": mission_id,
            "name": row.get("name") or "",
            "status": row["status"],
            "folder": Path(str(row["cwd"])).name,
            "updated_at": row.get("updated_at"),
        }
        config = row.get("config")
        snapshot = self._live_projection(store, mission_id) if row.get("status") in LIVE_MISSION_STATUSES else None
        if snapshot is not None:
            # Task counts and the current task let a console draw its lists from the replicated
            # summary instead of asking the worker for the status of every active mission.
            summary.update(mission_digest(snapshot))
            compact = self._compact_needs(snapshot)
            if compact is not None:
                summary["needs_you"] = compact
        else:
            counters = store.task_counters(mission_id)
            summary["task_counts"] = {str(key): int(value) for key, value in counters["status"].items()}
        if row.get("status") == "RUNNING" and isinstance(config, dict) and config.get("native_tui") is True:
            question = self._provider_question(snapshot)
            summary["needs_you"] = question if question is not None else summary.get("needs_you")
            self.mission_questions[mission_id] = question
        elif mission_id in self.mission_questions:
            # Event-driven summaries clear a question when its mission leaves RUNNING.
            summary.setdefault("needs_you", None)
            self.mission_questions.pop(mission_id, None)
        return summary

    def _question_refresh_batch(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Select a bounded, rotating slice so large inventories are not starved."""
        ordered = sorted(rows, key=lambda row: str(row["id"]))
        if not ordered:
            return []
        if self.last_question_mission_id is not None:
            start = next(
                (index for index, row in enumerate(ordered) if str(row["id"]) > self.last_question_mission_id),
                0,
            )
            ordered = ordered[start:] + ordered[:start]
        batch = ordered[:MAX_QUESTION_MISSIONS_PER_REFRESH]
        self.last_question_mission_id = str(batch[-1]["id"])
        return batch

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
            self.inflight, self.send_not_before = None, 0.0
            self.tombstones_unsupported = False
            self.pending_pongs.clear()
            self.audit("connection.accepted", generation=generation)
            return []
        if message.type == "error":
            if self.inflight is not None and self.inflight.tombstones and message.payload.get("in_reply_to") == self.inflight.message_id:
                # A coordinator from before PRD-038 rejects the unknown field. Keep the tombstones pending
                # (the pong that follows only clears the batch) and stop offering them on this connection.
                self.tombstones_unsupported = True
            return []  # never answer an error with an error; that can loop forever
        if self.generation == 0:
            return [self.error("not-welcomed", f"{message.type} before welcome", message.id)]
        if message.type == "ping":
            return [self.message("pong", {})]
        if message.type == "pong":
            self._pong()
            return []
        if message.type == "events.nack":
            self._nack(int(message.payload["expected_after"]))
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
            return [self._terminal_refused(session_id, "Needs --allow-terminal-view or --allow-control")]
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
        mutating = spec is not None and spec.mutating
        stored = self._stored_outcome(request.request_id) if mutating else None
        if stored is not None:
            # A repeated request id: send the first result again; the operation does not run a second time.
            self.audit("command", operation=request.operation, request_id=request.request_id, mission_id=request.mission_id,
                       outcome=stored.get("outcome"), error=None, duplicate=True, replayed=True)
            return [self.message("command.ack", {"request_id": request.request_id, "status": "duplicate"}), self.message("command.result", stored)]
        policy_error = self._policy_error(request, spec)
        if policy_error is not None:
            result = commands.CommandResult(request.request_id, request.operation, "rejected", error=policy_error)
        elif spec is not None and spec.mutating and not self.config.allow_control and not self._specific_mutation_opted_in(request.operation):
            result = commands.CommandResult(request.request_id, request.operation, "rejected",
                                            error=commands.CommandError("forbidden", "this worker accepts read-only remote operations"))
        else:
            result = commands.execute(dataclasses.replace(request, target=commands.LOCAL_TARGET, remote=True))
        if spec is None or spec.mutating or result.error is not None:
            self.audit("command", operation=request.operation, request_id=request.request_id, mission_id=request.mission_id,
                       outcome=result.outcome, error=result.error.code if result.error else None, duplicate=duplicate)
        if mutating:
            self._record_outcome(request.request_id, result.to_json())
        return [ack, self.message("command.result", result.to_json())]

    def _stored_outcome(self, request_id: str) -> dict[str, Any] | None:
        try:
            return self.outcomes.get(request_id)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.audit("outcomes.unavailable", error=type(exc).__name__)
            return None

    def _record_outcome(self, request_id: str, result: dict[str, Any]) -> None:
        try:
            self.outcomes.record(request_id, result)
        except (OSError, sqlite3.Error) as exc:
            self.audit("outcomes.unavailable", error=type(exc).__name__)

    def _policy_error(self, request: commands.CommandRequest, spec: commands.OperationSpec | None) -> commands.CommandError | None:
        """Enforce local worker opt-ins even when the coordinator advertises a command."""
        operation = request.operation.casefold()
        if operation in {"mission.create", "create"}:
            if not self.config.allow_create:
                return commands.CommandError("forbidden", "this worker has not opted in to remote mission creation")
            cwd = request.params.get("cwd")
            if not isinstance(cwd, str) or not _path_allowed(cwd, self.config.allow_roots):
                return commands.CommandError("forbidden", "mission creation must use a path inside an enrolled allowed root")
        if operation in {"worker.restore", "restore"}:
            if not self.config.allow_restore:
                return commands.CommandError("forbidden", "this worker has not opted in to remote restore")
            source = request.params.get("input")
            if not isinstance(source, str) or not _path_allowed(source, self.config.allow_roots):
                return commands.CommandError("forbidden", "restore input must be inside an enrolled allowed root")
        if operation in {"mission.suspend_until_credits", "power"} or re.search(r"(?:^|[._-])(?:power|poweroff|poweron|suspend|shutdown|reboot)(?:[._-]|$)", operation):
            if not self.config.allow_power:
                return commands.CommandError("forbidden", "this worker has not opted in to remote power operations")
        # Starting an existing mission can execute provider work in its local
        # checkout. If roots were enrolled, keep that execution inside them.
        if operation == "mission.start" and self.config.allow_roots and request.mission_id:
            with self.open_store() as store:
                mission = store.get_mission(request.mission_id)
            cwd = mission.get("cwd") if mission else None
            if not isinstance(cwd, str) or not _path_allowed(cwd, self.config.allow_roots):
                return commands.CommandError("forbidden", "mission is outside this worker's enrolled allowed roots")
        return None

    def _specific_mutation_opted_in(self, operation: str) -> bool:
        """The three explicit API-016 opt-ins authorize their own mutation families."""
        return ((operation == "mission.create" and self.config.allow_create)
                or (operation == "mission.suspend_until_credits" and self.config.allow_power)
                or (operation == "worker.restore" and self.config.allow_restore))

    def ping(self, fences: int = 0) -> Message:
        """A ``ping``; ``fences`` is the batch sequence number its ``pong`` will confirm (0 for a keepalive)."""
        self.pending_pongs.append(fences)
        return self.message("ping", {})

    def barrier_ping(self) -> Message:
        """The ping that follows the batch just returned by ``next_events``."""
        return self.ping(self.inflight.seq if self.inflight else 0)

    def _pong(self) -> None:
        fenced = self.pending_pongs.popleft() if self.pending_pongs else 0
        if fenced and self.inflight is not None and self.inflight.seq == fenced:
            if self.inflight.tombstones and not self.tombstones_unsupported:
                with self.open_store() as store:
                    store.mark_tombstones_replicated(self.inflight.tombstones)
            self.cursor, self.inflight = self.inflight.through, None

    def _nack(self, expected_after: int) -> None:
        """Rewind to the coordinator's cursor, once per in-flight batch.

        A nack that arrives with no batch in flight, or that names the position the in-flight batch
        already starts from, answers an earlier batch that has since been superseded. Honouring it
        would resend data the coordinator is about to accept.
        """
        if self.inflight is None or expected_after == self.inflight.after:
            return
        self.cursor, self.inflight = expected_after, None

    def _replicable_events(self, store: BridgeStore, after: int) -> tuple[list[dict[str, Any]], int]:
        """The next bounded batch of events and the cursor it advances to.

        Heartbeats and superseded capacity refreshes are skipped but still advance the cursor, so
        the returned cursor can exceed the last returned event.
        """
        selected: list[dict[str, Any]] = []
        size, through = 0, after
        newest_refresh: dict[str, int] = {}
        for event in store.events_since(after, limit=min(EVENT_SCAN_ROWS, 1000)):
            kind, mission_id = event.get("kind"), event.get("mission_id")
            if kind in UNREPLICATED_EVENT_KINDS:
                through = int(event["id"])
                continue
            if kind in LATEST_ONLY_EVENT_KINDS and isinstance(mission_id, str) and mission_id:
                if mission_id not in newest_refresh:
                    newest_refresh[mission_id] = store.latest_event_id(mission_id, (str(kind),))
                if int(event["id"]) < newest_refresh[mission_id]:
                    through = int(event["id"])
                    continue
            weight = len(json.dumps(event, separators=(",", ":")))
            if selected and (len(selected) >= min(EVENT_BATCH, MAX_EVENTS_PER_BATCH) or size + weight > EVENT_BATCH_BYTES):
                break
            selected.append(event)
            size += weight
            through = int(event["id"])
        return selected, through

    def next_events(self) -> Message | None:
        """The next event batch after the coordinator's cursor, or ``None``.

        ``None`` while a batch is in flight, while the rate cap holds the next batch back, and when
        there is nothing new.
        """
        if self.cursor is None or self.inflight is not None:
            return None
        moment = self.clock()
        if moment < self.send_not_before:
            return None
        with self.open_store() as store:
            pending = [] if self.tombstones_unsupported else store.pending_tombstones(MAX_DELETED_MISSIONS_PER_MESSAGE)
            if pending:
                # Tombstones travel alone, in an events message that carries no events and moves no cursor, so a
                # coordinator that rejects the field loses nothing else.
                batch = self.message("events", {"after": self.cursor, "through": self.cursor, "events": [], "deleted_missions": pending})
                self.batch_seq += 1
                self.inflight = InFlightBatch(self.batch_seq, self.cursor, self.cursor, batch.id, tuple(pending))
                self.send_not_before = moment + len(batch.encode()) / EVENT_RATE_BYTES_PER_SECOND
                return batch
            events, through = self._replicable_events(store, self.cursor)
            refresh_questions = moment - self.last_question_refresh >= MISSION_QUESTION_REFRESH_SECS
            if through == self.cursor and not refresh_questions:
                return None
            after = self.cursor
            # Ordinary summaries remain event-scoped. A small, periodic scan of
            # active native missions catches provider questions, which can arrive
            # without a Bridge database event.
            event_mission_ids = {
                str(event["mission_id"]) for event in events
                if isinstance(event.get("mission_id"), str) and event["mission_id"]
            }
            candidate_rows: dict[str, dict[str, Any]] = {
                mission_id: row for mission_id in sorted(event_mission_ids)
                if (row := store.get_mission(mission_id))
            }
            if refresh_questions:
                self.last_question_refresh = moment
                active_native = [
                    row for row in store.list_missions()
                    if row.get("status") == "RUNNING" and isinstance(row.get("config"), dict)
                    and row["config"].get("native_tui") is True
                ]
                active_native = self._question_refresh_batch(active_native)
                candidate_rows.update({str(row["id"]): row for row in active_native})
            missions: list[dict[str, Any]] = []
            for mission_id, row in candidate_rows.items():
                previous = self.mission_questions.get(mission_id)
                summary = self._mission_summary(store, row)
                current = self.mission_questions.get(mission_id)
                if mission_id in event_mission_ids or current != previous:
                    missions.append(summary)
            if through == after and not missions:
                return None
        batch = self.message("events", {"after": after, "through": through, "events": events,
                                         "mission_cursor": through, "missions": missions})
        self.batch_seq += 1
        self.inflight = InFlightBatch(self.batch_seq, after, through, batch.id)
        self.send_not_before = moment + len(batch.encode()) / EVENT_RATE_BYTES_PER_SECOND
        return batch


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
            session.inflight = None
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
                    connection.send_text(session.barrier_ping().encode())
                    last_ping = now
                if now - last_heard > SILENCE_LIMIT:
                    raise TimeoutError("coordinator silent")
                if now - last_ping > PING_INTERVAL and session.generation:
                    connection.send_text(session.ping().encode())
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
