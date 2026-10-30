"""Transactional persistence for LLM Bridge missions and task operations."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from .bridge_states import (
    ACTIVE_OPERATION_STATUSES, ENDED_MISSION_STATUSES, FINISHED_MISSION_STATUSES, FINISHED_TASK_STATUSES,
    HALTED_MISSION_STATUSES, MISSION_STATES, MISSION_TRANSITIONS, OPERATOR_DECLINED_EXIT_CODE, TASK_STATES, TASK_TRANSITIONS,
)


if TYPE_CHECKING:
    import sqlite3

SCHEMA_VERSION = 10
# Written into every event row. It is independent of SCHEMA_VERSION: index-only
# schema changes do not change what an event row means.
EVENT_SCHEMA_VERSION = 7
REJECTION_REASON_LIMIT = 2000
MAX_PROPOSAL_BYTES = 64 * 1024
MAX_PROPOSALS_PER_OPERATION = 20

# Retention of controller bookkeeping events (see BridgeStore.prune_plumbing_events).
HEARTBEAT_RETENTION_SECONDS = 3600
CAPACITY_REFRESH_FULL_SECONDS = 3600


def rotation_label(providers: Any, routes: Any) -> str:
    """The comma-separated rotation a mission uses: routes win over providers."""
    def names(value: Any) -> list[str]:
        return [item.strip() for item in str(value or "").split(",") if item.strip()]

    return ",".join(names(routes) or names(providers))


class TransitionError(ValueError):
    """A requested mission/task transition violates the durable state model."""


def mission_transition_message(mission_id: str, old: str, new: str) -> str:
    """Operator-facing wording for a refused mission state change."""
    short = str(mission_id)[:8]
    if old == "COMPLETED":
        return f"mission {short} is finished (completed); add a task with: llm-bridge task add --prompt \"...\" to continue it"
    if old == "CANCELLED":
        return f"mission {short} was cancelled and cannot be {'resumed' if new == 'RUNNING' else 'changed'}; create a new mission with: llm-bridge mission create --prompt \"...\""
    if new == "PAUSED":
        return f"mission {short} is {old.lower()}, not running, so there is nothing to pause"
    return f"mission {short} is {old.lower()} and cannot become {new.lower()}"


_HEADING_MARKERS = re.compile(r"^\s*#{1,6}(?:\s+|$)")


def derive_title(prompt: str, fallback: str = "", limit: int = 100) -> str:
    """Title from the first non-empty prompt line without Markdown heading markers.

    The stored prompt is never altered; only this display title is derived.
    """
    for line in prompt.splitlines():
        text = _HEADING_MARKERS.sub("", line).strip()
        if text:
            return text[:limit]
    return fallback


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_state_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("LLM_BRIDGE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    state_home = Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return state_home / "llm-tools" / "llm-bridge"


def store_path(env: dict[str, str] | None = None) -> Path:
    """The mission database file inside the state directory (``default_state_dir``)."""
    return default_state_dir(env) / "bridge.sqlite3"


DRAFT_MAX_AGE_SECONDS = 14 * 24 * 3600


def prune_old_drafts(*, now: float | None = None, max_age_seconds: float = DRAFT_MAX_AGE_SECONDS) -> int:
    """Delete regular files in the private drafts directory older than `max_age_seconds`.

    Drafts hold operator-written task text kept for crash recovery. Symlinks
    and subdirectories are never followed or removed. Returns the count removed;
    any filesystem error is ignored because pruning is housekeeping.
    """
    directory = default_state_dir() / "drafts"
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


def _sqlite_uri_path(path: str | Path) -> str:
    """Escape a file path for a SQLite ``file:`` URI.

    Only ``%``, ``?`` and ``#`` have a meaning in the path part of the URI, so they are the only characters
    escaped here. This avoids importing ``urllib.parse`` for every read-only open.
    """
    return str(path).replace("%", "%25").replace("?", "%3f").replace("#", "%23")


def repository_identity(cwd: str | Path) -> str:
    """Resolve a directory to its Git worktree root, falling back to the path."""
    import subprocess  # loaded when a mission is created; the read commands never need it

    path = Path(cwd).expanduser().resolve(strict=True)
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return str(path)
    if result.returncode == 0 and result.stdout.strip():
        return str(Path(result.stdout.strip()).resolve())
    return str(path)


def _count_statements(row: str, sign: str) -> list[str]:
    """Statements that add (``sign='1'``) or remove (``'-1'``) one task row's contribution to ``mission_task_counts``.

    ``row`` is ``NEW`` or ``OLD``. A task counts toward ``status`` and ``criteria``
    when it is part of the queue: operator-created, or an accepted proposal.
    """
    effective = f"({row}.origin='operator' OR {row}.proposal_status='ACCEPTED')"
    head = "INSERT INTO mission_task_counts(mission_id,scope,key,n) SELECT "
    tail = " ON CONFLICT(mission_id,scope,key) DO UPDATE SET n=n+excluded.n"
    length = f"{sign}*json_array_length({row}.acceptance_json)"
    return [
        f"{head}{row}.mission_id,'all','total',{sign} WHERE 1{tail}",
        f"{head}{row}.mission_id,'status',{row}.status,{sign} WHERE {effective}{tail}",
        f"{head}{row}.mission_id,'proposal',{row}.proposal_status,{sign} WHERE {row}.origin='agent-proposal'{tail}",
        f"{head}{row}.mission_id,'criteria','total',{length} WHERE {effective}{tail}",
        f"{head}{row}.mission_id,'criteria','verified',{length} WHERE {effective} AND {row}.status='COMPLETED'{tail}",
    ]


def _trigger(name: str, event: str, statements: list[str], when: str = "") -> str:
    body = "".join(f" {statement};" for statement in statements)
    return f"CREATE TRIGGER {name} AFTER {event} ON tasks{(' WHEN ' + when) if when else ''} BEGIN{body} END"


_TASK_COUNT_TRIGGERS: tuple[str, ...] = (
    _trigger("tasks_counts_insert", "INSERT", _count_statements("NEW", "1")),
    _trigger(
        "tasks_counts_update", "UPDATE OF status,origin,proposal_status,acceptance_json",
        _count_statements("OLD", "-1") + _count_statements("NEW", "1"),
        "OLD.status IS NOT NEW.status OR OLD.origin IS NOT NEW.origin OR OLD.proposal_status IS NOT NEW.proposal_status "
        "OR OLD.acceptance_json IS NOT NEW.acceptance_json",
    ),
    _trigger("tasks_counts_delete", "DELETE", _count_statements("OLD", "-1")),
)


_BASE_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    cwd TEXT NOT NULL,
    repo_identity TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    original_prompt TEXT NOT NULL,
    prompt TEXT NOT NULL,
    pending_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 0,
    ordering INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    revision INTEGER NOT NULL DEFAULT 1,
    acceptance_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'operator',
    proposal_status TEXT NOT NULL DEFAULT 'NONE',
    proposed_by_operation TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on),
    CHECK (task_id != depends_on)
)""",
    """CREATE TABLE IF NOT EXISTS amendments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    changes_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    applied_at TEXT,
    UNIQUE(task_id, revision)
)""",
    """CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    iteration INTEGER NOT NULL,
    station TEXT NOT NULL,
    route_id TEXT,
    model TEXT,
    pane_id TEXT,
    process_pid INTEGER,
    process_start TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    logs_path TEXT,
    native_session_id TEXT,
    restoration_state TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    f"""CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    task_id INTEGER,
    operation_id TEXT,
    schema_version INTEGER NOT NULL DEFAULT {EVENT_SCHEMA_VERSION},
    kind TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS managed_processes (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    process_pid INTEGER,
    process_start TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS events_mission_id ON events(mission_id, id)""",
    """CREATE INDEX IF NOT EXISTS tasks_mission_state_order ON tasks(mission_id, status, priority DESC, ordering, id)""",
    """CREATE INDEX IF NOT EXISTS commands_mission_queue ON commands(mission_id,status,created_at)""",
    """CREATE INDEX IF NOT EXISTS operations_mission_logs_path ON operations(mission_id,logs_path)""",
    """CREATE INDEX IF NOT EXISTS operations_mission_history ON operations(mission_id)""",
    """CREATE INDEX IF NOT EXISTS operations_task_history ON operations(task_id)""",
    """CREATE INDEX IF NOT EXISTS commands_mission_fifo ON commands(mission_id,status)""",
    """CREATE INDEX IF NOT EXISTS managed_processes_mission_status ON managed_processes(mission_id,status,created_at)""",
)


class BridgeStore:
    """Single SQLite source of truth for missions, tasks, events and operations.

    SQLite WAL plus FULL synchronous commits make each state transition and its
    event record durable together. The enclosing state directory is private;
    callers may open independent instances from different processes.
    """

    def __init__(self, path: str | Path | None = None, *, readonly: bool = False) -> None:
        """Open the store, creating or migrating the database when it is behind.

        ``readonly=True`` is for read paths (status, events, lists, home): it opens
        the file with ``mode=ro`` so no write lock is taken and nothing is written,
        and it refuses writes. It falls back to a normal open only when the
        database is missing or its schema is behind, because that open must create
        or migrate it.
        """
        self.path = Path(path) if path is not None else store_path()
        self.path = self.path.expanduser().absolute()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = self.path.parent.stat()
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            hint = f" · fix with: chmod 700 {shlex.quote(str(self.path.parent))}" if parent_info.st_uid == os.getuid() else ""
            raise PermissionError(f"Bridge state directory must be owned by this user and private: {self.path.parent}{hint}")
        self.readonly = False
        import sqlite3  # imported on first open so commands that never open a store do not load it

        if readonly and self.path.exists():
            reader = self._open_readonly()
            if reader is not None:
                self.db = reader
                self.readonly = True
                return
        self.db = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self._initialize()
        self._secure_sqlite_files()

    def _open_readonly(self) -> sqlite3.Connection | None:
        """A read-only connection to a database at the current schema, else ``None``."""
        import sqlite3

        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(f"file:{_sqlite_uri_path(self.path)}?mode=ro", uri=True, timeout=10.0, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version == SCHEMA_VERSION:
                db.execute("PRAGMA query_only=ON")
                return db
        except sqlite3.Error:
            pass
        if db is not None:
            db.close()
        return None

    def _user_version(self) -> int:
        return int(self.db.execute("PRAGMA user_version").fetchone()[0])

    def _initialize(self) -> None:
        """Bring the schema up to date; a database already at the current version is not written to.

        Every command and every console refresh opens a store. Running the
        migration statements each time cost two write transactions (about 25 ms
        at 6k tasks, 250 ms at 60k) plus WAL traffic for no change, so the
        migration runs only when ``user_version`` is behind ``SCHEMA_VERSION``.
        """
        current_version = self._user_version()
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(f"Bridge database schema {current_version} is newer than supported schema {SCHEMA_VERSION}")
        if current_version == SCHEMA_VERSION:
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have migrated while this one waited for the write lock.
            current_version = self._user_version()
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(f"Bridge database schema {current_version} is newer than supported schema {SCHEMA_VERSION}")
            if current_version < SCHEMA_VERSION:
                self._migrate(current_version)
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        self._secure_sqlite_files()

    def _migrate(self, current_version: int) -> None:
        """Create missing tables, columns and indexes, then run the one-time data backfills."""
        for statement in _BASE_SCHEMA:
            self.db.execute(statement)
        self._ensure_column("missions", "config_json", "TEXT NOT NULL DEFAULT '{}'" )
        self._ensure_column("tasks", "original_prompt", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("tasks", "ordinal", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("tasks", "origin", "TEXT NOT NULL DEFAULT 'operator'")
        self._ensure_column("tasks", "proposal_status", "TEXT NOT NULL DEFAULT 'NONE'")
        self._ensure_column("tasks", "proposed_by_operation", "TEXT")
        self._ensure_column("operations", "route_id", "TEXT")
        self._ensure_column("operations", "model", "TEXT")
        self._ensure_column("operations", "pane_id", "TEXT")
        self._ensure_column("operations", "process_pid", "INTEGER")
        self._ensure_column("operations", "process_start", "TEXT")
        self._ensure_column("operations", "native_session_id", "TEXT")
        self._ensure_column("operations", "restoration_state", "TEXT NOT NULL DEFAULT 'unknown'")
        self._ensure_column("events", "schema_version", f"INTEGER NOT NULL DEFAULT {EVENT_SCHEMA_VERSION}")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_mission_kind_id ON events(mission_id,kind,id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS tasks_mission_proposal_status ON tasks(mission_id,origin,proposal_status,status)")
        if current_version < 7:
            self.db.execute("UPDATE tasks SET original_prompt=prompt WHERE original_prompt=''")
            # Per-mission task numbers: rows from before schema 7 are numbered by
            # creation order (the AUTOINCREMENT id) within their mission. Rows
            # that already have an ordinal keep it, so this is safe to repeat.
            self.db.execute(
                "UPDATE tasks SET ordinal=(SELECT COUNT(*) FROM tasks earlier WHERE earlier.mission_id=tasks.mission_id AND earlier.id<=tasks.id) WHERE ordinal=0"
            )
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS tasks_mission_ordinal ON tasks(mission_id,ordinal)")
        if current_version < 7:
            for mission in self.db.execute("SELECT id,cwd,repo_identity FROM missions").fetchall():
                # Historical worktrees may have been removed. Preserve
                # their durable identity during migration instead of
                # making the database impossible to open.
                try:
                    identity = repository_identity(mission["cwd"])
                except (OSError, RuntimeError, ValueError):
                    identity = str(mission["repo_identity"] or mission["cwd"])
                self.db.execute("UPDATE missions SET repo_identity=? WHERE id=?", (identity, mission["id"]))
            self.db.execute("DROP INDEX IF EXISTS one_active_mission_per_worktree")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_mission_per_worktree ON missions(repo_identity) WHERE status IN ('RUNNING','PAUSED','STOPPING')")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_operation_per_task ON operations(task_id) WHERE status IN ('INTENT','PREPARED','RUNNING')")
        if current_version < 8:
            # A task's or operation's recent history reads a bounded slice of its own
            # events, not the mission's whole log. Mission-level events (heartbeats,
            # capacity refreshes) carry neither column, so the partial indexes stay small.
            self.db.execute("CREATE INDEX IF NOT EXISTS events_task_id ON events(task_id,id) WHERE task_id IS NOT NULL")
            self.db.execute("CREATE INDEX IF NOT EXISTS events_operation_id ON events(operation_id,id) WHERE operation_id IS NOT NULL")
        if current_version < 9:
            # The status page reads one bounded slice of a mission's tasks (queue order or
            # task-list order) and maintained counters, so its cost no longer grows with
            # the length of the queue.
            self.db.execute("CREATE INDEX IF NOT EXISTS tasks_mission_page ON tasks(mission_id,priority DESC,ordering,id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS tasks_mission_ordering ON tasks(mission_id,ordering,id)")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS mission_task_counts ("
                "mission_id TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY (mission_id,scope,key)) WITHOUT ROWID"
            )
            for trigger in ("tasks_counts_insert", "tasks_counts_update", "tasks_counts_delete"):
                self.db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self.rebuild_task_counts()
            for statement in _TASK_COUNT_TRIGGERS:
                self.db.execute(statement)
        if current_version < 10:
            # Home screen and mission list: the one running/ready task of each mission,
            # and each mission's newest operation, without reading every task or operation.
            self.db.execute("CREATE INDEX IF NOT EXISTS tasks_current ON tasks(mission_id,status) WHERE status IN ('RUNNING','VERIFYING','READY')")
            self.db.execute("CREATE INDEX IF NOT EXISTS operations_mission_recent ON operations(mission_id,created_at DESC,id DESC)")

    def rebuild_task_counts(self) -> None:
        """Recompute ``mission_task_counts`` from the tasks table (the migration backfill, and a repair)."""
        effective = "(origin='operator' OR proposal_status='ACCEPTED')"
        self.db.execute("DELETE FROM mission_task_counts")
        self.db.execute("INSERT INTO mission_task_counts SELECT mission_id,'all','total',COUNT(*) FROM tasks GROUP BY mission_id")
        self.db.execute(f"INSERT INTO mission_task_counts SELECT mission_id,'status',status,COUNT(*) FROM tasks WHERE {effective} GROUP BY mission_id,status")
        self.db.execute("INSERT INTO mission_task_counts SELECT mission_id,'proposal',proposal_status,COUNT(*) FROM tasks WHERE origin='agent-proposal' GROUP BY mission_id,proposal_status")
        self.db.execute(f"INSERT INTO mission_task_counts SELECT mission_id,'criteria','total',COALESCE(SUM(json_array_length(acceptance_json)),0) FROM tasks WHERE {effective} GROUP BY mission_id")
        self.db.execute(
            "INSERT INTO mission_task_counts SELECT mission_id,'criteria','verified',COALESCE(SUM(json_array_length(acceptance_json)),0) "
            f"FROM tasks WHERE {effective} AND status='COMPLETED' GROUP BY mission_id"
        )

    def task_counters(self, mission_id: str) -> dict[str, Any]:
        """Maintained task totals for a mission, read from one primary-key range.

        ``status`` and ``proposal`` map a state to its task count (states with no
        tasks are absent, as in a ``GROUP BY``). ``total`` counts every task of the
        mission. ``criteria_total`` and ``criteria_verified`` sum acceptance
        criteria over queue tasks, the latter over completed ones.
        """
        status: dict[str, int] = {}
        proposal: dict[str, int] = {}
        other: dict[tuple[str, str], int] = {}
        for row in self.db.execute("SELECT scope,key,n FROM mission_task_counts WHERE mission_id=?", (mission_id,)):
            scope, key, count = str(row[0]), str(row[1]), int(row[2])
            if scope == "status" and count > 0:
                status[key] = count
            elif scope == "proposal" and count > 0:
                proposal[key] = count
            else:
                other[(scope, key)] = count
        return {
            "status": status,
            "proposal": proposal,
            "total": other.get(("all", "total"), 0),
            "criteria_total": other.get(("criteria", "total"), 0),
            "criteria_verified": other.get(("criteria", "verified"), 0),
        }

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _secure_sqlite_files(self) -> None:
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                if path.exists():
                    path.chmod(0o600)
            except OSError:
                pass

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self.db.in_transaction:
            savepoint = "bridge_" + uuid.uuid4().hex
            self.db.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                self._secure_sqlite_files()
                raise
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self._secure_sqlite_files()
            raise
        self._secure_sqlite_files()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def _event(
        self,
        mission_id: str,
        kind: str,
        data: dict[str, Any],
        *,
        task_id: int | None = None,
        operation_id: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO events(mission_id,task_id,operation_id,kind,data_json,created_at) VALUES(?,?,?,?,?,?)",
            (mission_id, task_id, operation_id, kind, self._json(data), utc_now()),
        )

    def _mission_row(self, mission_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown mission: {str(mission_id)[:8]}")
        return row

    def _task_row(self, task_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        return row

    def _dependencies_complete(self, task_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM task_dependencies d JOIN tasks prerequisite ON prerequisite.id=d.depends_on WHERE d.task_id=? AND prerequisite.status!='COMPLETED' LIMIT 1",
            (task_id,),
        ).fetchone() is None

    def _task_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return self._task_dicts([row])[0]

    def _task_dicts(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Task dictionaries for ``rows`` with their dependencies read in batches, not one query per task."""
        tasks = []
        for row in rows:
            task = dict(row)
            task["acceptance"] = json.loads(task.pop("acceptance_json"))
            task["evidence"] = json.loads(task.pop("evidence_json"))
            task["pending"] = json.loads(task.pop("pending_json"))
            tasks.append(task)
        dependencies: dict[int, list[tuple[int, int]]] = {int(task["id"]): [] for task in tasks}
        ids = list(dependencies)
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for task_id, depends_on, ordinal in self.db.execute(
                "SELECT d.task_id, d.depends_on, t.ordinal FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on "
                f"WHERE d.task_id IN ({marks}) ORDER BY d.task_id, d.depends_on",
                chunk,
            ):
                dependencies[int(task_id)].append((int(depends_on), int(ordinal)))
        for task in tasks:
            found = dependencies[int(task["id"])]
            task["depends_on"] = [dep for dep, _ in found]
            task["depends_on_ordinals"] = sorted(ordinal for _, ordinal in found)
        return tasks

    def _next_ordinal(self, mission_id: str) -> int:
        """The next per-mission task number. Tasks are never deleted, so numbers are never reused."""
        return int(self.db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0])

    def task_by_ordinal(self, mission_id: str, ordinal: int) -> dict[str, Any]:
        """The mission's task with this per-mission number, or a TransitionError naming the valid range."""
        self._mission_row(mission_id)
        row = self.db.execute("SELECT * FROM tasks WHERE mission_id=? AND ordinal=?", (mission_id, int(ordinal))).fetchone()
        if row is not None:
            return self._task_dict(row)
        count = int(self.db.execute("SELECT COUNT(*) FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0])
        short = str(mission_id)[:8]
        if count == 0:
            raise TransitionError(f"mission {short} has no tasks; there is no task {ordinal}")
        highest = int(self.db.execute("SELECT MAX(ordinal) FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0])
        span = "task 1" if highest == 1 else f"tasks 1\u2013{highest}"
        raise TransitionError(f"mission {short} has {span}; there is no task {ordinal}")

    def create_mission(
        self,
        name: str,
        objective: str,
        *,
        cwd: str | Path,
        mission_id: str | None = None,
        initial_prompt: str | None = None,
        initial_acceptance: list[str] | tuple[str, ...] = (),
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import sqlite3

        if not name.strip() or not objective:
            raise ValueError("mission name and objective must be non-empty")
        canonical_cwd = str(Path(cwd).expanduser().resolve(strict=True))
        repo_root = repository_identity(canonical_cwd)
        mid = mission_id or uuid.uuid4().hex
        now = utc_now()
        try:
            with self._transaction():
                active = self.db.execute("SELECT id,cwd,repo_identity FROM missions WHERE status IN ('RUNNING','PAUSED','STOPPING')").fetchall()
                wanted = Path(canonical_cwd)
                owned = next((row for row in active if row["repo_identity"] == repo_root or wanted == Path(row["cwd"]) or wanted.is_relative_to(Path(row["cwd"])) or Path(row["cwd"]).is_relative_to(wanted)), None)
                if owned:
                    raise TransitionError(f"an active mission already owns worktree: {canonical_cwd}")
                self.db.execute(
                    "INSERT INTO missions(id,name,objective,cwd,repo_identity,config_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'CREATED',?,?)",
                    (mid, name.strip(), objective, canonical_cwd, repo_root, self._json(config or {}), now, now),
                )
                self._event(mid, "mission-created", {"name": name.strip(), "cwd": canonical_cwd})
                task_id: int | None = None
                if initial_prompt is not None:
                    task_title = derive_title(initial_prompt, name.strip())
                    cur = self.db.execute(
                        "INSERT INTO tasks(mission_id,ordinal,title,original_prompt,prompt,acceptance_json,created_at,updated_at) VALUES(?,1,?,?,?,?,?,?)",
                        (mid, task_title, initial_prompt, initial_prompt, self._json(list(initial_acceptance)), now, now),
                    )
                    task_id = int(cur.lastrowid)
                    self._event(mid, "task-added", {"title": task_title, "priority": 0}, task_id=task_id)
        except sqlite3.IntegrityError as exc:
            if "one_active_mission_per_worktree" in str(exc) or "missions.cwd" in str(exc):
                raise TransitionError(f"an active mission already owns worktree: {canonical_cwd}") from exc
            raise
        result = self.get_mission(mid)
        if task_id is not None:
            result["initial_task_id"] = task_id
        return result

    def get_mission(self, mission_id: str) -> dict[str, Any]:
        result = dict(self._mission_row(mission_id))
        result["config"] = json.loads(result.pop("config_json"))
        return result

    def list_missions(self) -> list[dict[str, Any]]:
        result = []
        for row in self.db.execute("SELECT * FROM missions ORDER BY created_at,id"):
            item = dict(row)
            item["config"] = json.loads(item.pop("config_json"))
            result.append(item)
        return result

    def set_mission_status(self, mission_id: str, status: str) -> None:
        status = status.upper()
        if status not in MISSION_STATES:
            raise ValueError(f"invalid mission status: {status}")
        with self._transaction():
            mission = self._mission_row(mission_id)
            old = str(mission["status"])
            if status != old and status not in MISSION_TRANSITIONS[old]:
                raise TransitionError(mission_transition_message(mission_id, old, status))
            if status == "COMPLETED":
                counts = self.db.execute(
                    "SELECT COUNT(*) AS total, SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS completed FROM tasks WHERE mission_id=? AND (origin='operator' OR proposal_status='ACCEPTED')",
                    (mission_id,),
                ).fetchone()
                if not counts["total"] or counts["total"] != counts["completed"]:
                    raise TransitionError("mission cannot complete while tasks are not complete")
            self.db.execute("UPDATE missions SET status=?,updated_at=? WHERE id=?", (status, utc_now(), mission_id))
            self._event(mission_id, "mission-status", {"from": old, "to": status})

    def add_task(
        self,
        mission_id: str,
        prompt: str,
        *,
        title: str = "",
        priority: int = 0,
        order: int | None = None,
        depends_on: list[int] | tuple[int, ...] = (),
        acceptance: list[str] | tuple[str, ...] = (),
    ) -> int:
        if not prompt:
            raise ValueError("task prompt must be non-empty")
        now = utc_now()
        with self._transaction():
            mission = self._mission_row(mission_id)
            if mission["status"] == "CANCELLED":
                raise TransitionError(f"mission {str(mission_id)[:8]} was cancelled; create a new mission instead of adding a task")
            next_order = int(self.db.execute("SELECT COALESCE(MAX(ordering),0)+10 FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0])
            cur = self.db.execute(
                "INSERT INTO tasks(mission_id,ordinal,title,original_prompt,prompt,priority,ordering,acceptance_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (mission_id, self._next_ordinal(mission_id), title or derive_title(prompt), prompt, prompt, int(priority), next_order if order is None else int(order), self._json(list(acceptance)), now, now),
            )
            task_id = int(cur.lastrowid)
            for dep in depends_on:
                self._add_dependency_locked(mission_id, task_id, int(dep))
            self._event(mission_id, "task-added", {"title": title or derive_title(prompt), "priority": int(priority)}, task_id=task_id)
            if mission["status"] == "COMPLETED":
                # A finished mission takes follow-up work: it goes back to a
                # saved state that `start` continues from.
                self.db.execute("UPDATE missions SET status='STOPPED',updated_at=? WHERE id=?", (now, mission_id))
                self._event(mission_id, "mission-reopened", {"from": "COMPLETED", "to": "STOPPED", "reason": "task-added"}, task_id=task_id)
        return task_id

    def _add_dependency_locked(self, mission_id: str, task_id: int, depends_on: int) -> None:
        task = self._task_row(task_id)
        dep = self._task_row(depends_on)
        if task["mission_id"] != mission_id or dep["mission_id"] != mission_id:
            raise TransitionError("a task can only wait for a task of the same mission")
        if task["status"] in {"RUNNING", "VERIFYING"}:
            raise TransitionError(f"cannot add a dependency to {str(task['status']).lower()} task {task['ordinal']}; dependencies can change only while a task waits to run")
        if task["status"] in FINISHED_TASK_STATUSES:
            raise TransitionError(f"task {task['ordinal']} is {str(task['status']).lower()} and its dependencies can no longer change")
        if task_id == depends_on:
            raise TransitionError("a task cannot depend on itself")
        cycle = self.db.execute(
            "WITH RECURSIVE ancestors(id) AS (SELECT depends_on FROM task_dependencies WHERE task_id=? UNION SELECT d.depends_on FROM task_dependencies d JOIN ancestors a ON d.task_id=a.id) SELECT 1 FROM ancestors WHERE id=? LIMIT 1",
            (depends_on, task_id),
        ).fetchone()
        if cycle:
            raise TransitionError(f"that dependency would create a cycle: task {dep['ordinal']} already waits for task {task['ordinal']}")
        self.db.execute("INSERT OR IGNORE INTO task_dependencies(task_id,depends_on) VALUES(?,?)", (task_id, depends_on))

    def propose_task(
        self,
        mission_id: str,
        prompt: str,
        *,
        source_operation_id: str,
        title: str = "",
        priority: int = 0,
        depends_on: list[int] | tuple[int, ...] = (),
        acceptance: list[str] | tuple[str, ...] = (),
    ) -> int:
        """Store an agent-originated task proposal without scheduling it."""
        if not prompt.strip() or not source_operation_id:
            raise ValueError("proposal prompt and source operation are required")
        if len(prompt.encode("utf-8")) > MAX_PROPOSAL_BYTES:
            raise ValueError(f"proposal prompt is larger than {MAX_PROPOSAL_BYTES // 1024} KiB; split it into smaller proposals")
        now = utc_now()
        with self._transaction():
            mission = self._mission_row(mission_id)
            if mission["status"] in FINISHED_MISSION_STATUSES:
                raise TransitionError(f"mission {str(mission_id)[:8]} is {str(mission['status']).lower()}; it takes no new proposals")
            source = self.db.execute("SELECT mission_id FROM operations WHERE id=?", (source_operation_id,)).fetchone()
            if source is None or source["mission_id"] != mission_id:
                raise TransitionError("proposal source operation must belong to the same mission")
            made = self.db.execute("SELECT COUNT(*) FROM tasks WHERE proposed_by_operation=?", (source_operation_id,)).fetchone()[0]
            if int(made) >= MAX_PROPOSALS_PER_OPERATION:
                raise TransitionError(f"this operation already made {MAX_PROPOSALS_PER_OPERATION} proposals; the operator must review them first")
            next_order = int(self.db.execute("SELECT COALESCE(MAX(ordering),0)+10 FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0])
            task_title = title or derive_title(prompt)
            cur = self.db.execute(
                "INSERT INTO tasks(mission_id,ordinal,title,original_prompt,prompt,priority,ordering,acceptance_json,origin,proposal_status,proposed_by_operation,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mission_id, self._next_ordinal(mission_id), task_title, prompt, prompt, int(priority), next_order, self._json(list(acceptance)), "agent-proposal", "PENDING", source_operation_id, now, now),
            )
            task_id = int(cur.lastrowid)
            for dependency in depends_on:
                self._add_dependency_locked(mission_id, task_id, int(dependency))
            self._event(mission_id, "task-proposed", {
                "title": task_title,
                "priority": int(priority),
                "source_operation_id": source_operation_id,
            }, task_id=task_id, operation_id=source_operation_id)
            return task_id

    def decide_proposal(self, mission_id: str, task_id: int, *, accept: bool, reason: str = "") -> dict[str, Any]:
        """Accept an agent proposal into the operator queue or reject it."""
        with self._transaction():
            task = self._task_row(task_id)
            if task["mission_id"] != mission_id:
                raise TransitionError(f"task {task['ordinal']} is not a proposal of mission {str(mission_id)[:8]}")
            if task["origin"] != "agent-proposal" or task["proposal_status"] != "PENDING":
                raise TransitionError(f"task {task['ordinal']} is not a pending proposal")
            if accept:
                mission = self._mission_row(mission_id)
                if mission["status"] in FINISHED_MISSION_STATUSES:
                    raise TransitionError(f"mission {str(mission_id)[:8]} is {str(mission['status']).lower()}; it takes no new proposals")
                self.db.execute("UPDATE tasks SET proposal_status='ACCEPTED',updated_at=? WHERE id=?", (utc_now(), task_id))
                self._event(mission_id, "task-proposal-accepted", {"source_operation_id": task["proposed_by_operation"]}, task_id=task_id)
            else:
                self.db.execute("UPDATE tasks SET proposal_status='REJECTED',status='CANCELLED',updated_at=? WHERE id=?", (utc_now(), task_id))
                self._event(mission_id, "task-proposal-rejected", {
                    "source_operation_id": task["proposed_by_operation"],
                    "reason": reason[:1000],
                }, task_id=task_id)
        return self.get_task(task_id)

    def add_dependency(self, mission_id: str, task_id: int, depends_on: int) -> None:
        with self._transaction():
            self._add_dependency_locked(mission_id, task_id, depends_on)
            dep = self._task_row(depends_on)
            task = self._task_row(task_id)
            if dep["status"] != "COMPLETED" and task["status"] == "READY":
                self.db.execute("UPDATE tasks SET status='QUEUED',updated_at=? WHERE id=?", (utc_now(), task_id))
                self._event(mission_id, "task-status", {"from": "READY", "to": "QUEUED", "reason": "dependency-added"}, task_id=task_id)
            self._event(mission_id, "dependency-added", {"depends_on": depends_on, "depends_on_ordinal": int(dep["ordinal"])}, task_id=task_id)

    def get_task(self, task_id: int) -> dict[str, Any]:
        return self._task_dict(self._task_row(task_id))

    def list_tasks(self, mission_id: str, *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """The mission's tasks in task-list order; ``limit``/``offset`` select one page of it."""
        self._mission_row(mission_id)
        query = "SELECT * FROM tasks WHERE mission_id=? ORDER BY ordering,id"
        params: tuple[Any, ...] = (mission_id,)
        if limit is not None or offset:
            query += " LIMIT ? OFFSET ?"
            params += (-1 if limit is None else max(0, int(limit)), max(0, int(offset)))
        return self._task_dicts(self.db.execute(query, params).fetchall())

    def list_proposals(self, mission_id: str, *, status: str = "PENDING", limit: int = 100, after_task_id: int = 0) -> list[dict[str, Any]]:
        """List proposal records with a bounded, stable task-id cursor."""
        self._mission_row(mission_id)
        normalized = status.upper()
        if normalized not in {"PENDING", "ACCEPTED", "REJECTED", "ALL"}:
            raise ValueError("proposal status must be PENDING, ACCEPTED, REJECTED, or ALL")
        bounded_limit = max(1, min(500, int(limit)))
        status_clause = "" if normalized == "ALL" else " AND proposal_status=?"
        params: tuple[Any, ...] = (mission_id, after_task_id, normalized, bounded_limit) if status_clause else (mission_id, after_task_id, bounded_limit)
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE mission_id=? AND origin='agent-proposal' AND id>?" + status_clause + " ORDER BY id LIMIT ?",
            params,
        )
        return self._task_dicts(rows.fetchall())

    def ready_tasks(self, mission_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return runnable tasks in priority-descending, stable queue order.

        Every runnable queued task is promoted to READY whatever ``limit`` is; the
        limit only bounds how many task dictionaries are built and returned (the
        controller needs the first one, and building a dictionary costs a
        dependency read per task).
        """
        with self._transaction():
            self._mission_row(mission_id)
            ready = self.db.execute(
                "SELECT t.id FROM tasks t WHERE t.mission_id=? AND t.status='QUEUED' AND (t.origin='operator' OR t.proposal_status='ACCEPTED') AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=t.id AND p.status!='COMPLETED')",
                (mission_id,),
            ).fetchall()
            for row in ready:
                self.db.execute("UPDATE tasks SET status='READY',updated_at=? WHERE id=?", (utc_now(), row["id"]))
                self._event(mission_id, "task-ready", {"from": "QUEUED", "to": "READY"}, task_id=row["id"])
            rows = self.db.execute(
                "SELECT * FROM tasks t WHERE mission_id=? AND status='READY' AND (t.origin='operator' OR t.proposal_status='ACCEPTED') AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=t.id AND p.status!='COMPLETED') ORDER BY priority DESC,ordering,id LIMIT ?",
                (mission_id, -1 if limit is None else max(0, int(limit))),
            )
            return self._task_dicts(rows.fetchall())

    _AMENDABLE_COLUMNS = {"prompt": "prompt", "title": "title", "priority": "priority", "acceptance": "acceptance_json"}

    def _apply_pending_amendment(
        self,
        task: Any,
        pending: dict[str, Any],
        event_data: dict[str, Any],
        *,
        reset_to_ready: bool = False,
        require_fields: bool = False,
    ) -> None:
        """Copy a task's pending amendment into the task and record that it was applied.

        Must run inside a transaction. Writes the amended fields, clears
        ``pending_json``, marks the task's pending amendment rows APPLIED and
        emits ``task-amendment-applied`` with ``event_data``. With
        ``reset_to_ready`` the task also returns to READY with its evidence
        cleared, because verification evidence belongs to the previous text.
        With ``require_fields`` nothing is written when ``pending`` holds no
        amendable field.
        """
        task_id = task["id"]
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in pending.items():
            if key in self._AMENDABLE_COLUMNS:
                assignments.append(self._AMENDABLE_COLUMNS[key] + "=?")
                values.append(self._json(value) if key == "acceptance" else value)
        if require_fields and not assignments:
            return
        assignments.append("pending_json='{}'")
        if reset_to_ready:
            assignments.extend(["evidence_json='[]'", "status='READY'"])
        values.extend([utc_now(), task_id])
        self.db.execute(f"UPDATE tasks SET {','.join(assignments)},updated_at=? WHERE id=?", values)
        self.db.execute("UPDATE amendments SET status='APPLIED',applied_at=? WHERE task_id=? AND status='PENDING'", (utc_now(), task_id))
        self._event(task["mission_id"], "task-amendment-applied", event_data, task_id=task_id)

    def amend_task(self, task_id: int, *, prompt: str | None = None, title: str | None = None, priority: int | None = None, acceptance: list[str] | None = None) -> int:
        with self._transaction():
            task = self._task_row(task_id)
            if task["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                raise TransitionError(f"task {task['ordinal']} is {str(task['status']).lower()} and cannot be amended")
            pending = json.loads(task["pending_json"])
            for key, value in (("prompt", prompt), ("title", title), ("priority", priority), ("acceptance", acceptance)):
                if value is not None:
                    pending[key] = value
            revision = int(task["revision"]) + 1
            self.db.execute("UPDATE tasks SET pending_json=?,revision=?,updated_at=? WHERE id=?", (self._json(pending), revision, utc_now(), task_id))
            self.db.execute("INSERT INTO amendments(task_id,revision,changes_json,status,created_at) VALUES(?,?,?,'PENDING',?)", (task_id, revision, self._json(pending), utc_now()))
            running = self.db.execute("SELECT 1 FROM operations WHERE task_id=? AND status IN ('INTENT','PREPARED','RUNNING') LIMIT 1", (task_id,)).fetchone()
            if task["status"] != "VERIFYING" and running is None:
                # No provider step is using the old text, so there is no boundary
                # to wait for: the new revision takes effect now.
                self._apply_pending_amendment(task, pending, {"revision": revision, "reason": "no-active-step"}, require_fields=True)
            if task["status"] == "VERIFYING":
                self._apply_pending_amendment(task, pending, {"revision": revision, "reason": "after-verification-result"}, reset_to_ready=True)
            self._event(task["mission_id"], "task-amended", {"revision": revision, "fields": sorted(pending)}, task_id=task_id)
            return revision

    def set_task_priority(self, task_id: int, priority: int) -> None:
        with self._transaction():
            task = self._task_row(task_id)
            if task["status"] in FINISHED_TASK_STATUSES:
                raise TransitionError(f"task {task['ordinal']} is {str(task['status']).lower()} and cannot be reprioritized")
            self.db.execute("UPDATE tasks SET priority=?,updated_at=? WHERE id=?", (int(priority), utc_now(), task_id))
            self._event(task["mission_id"], "task-priority", {"priority": int(priority)}, task_id=task_id)

    def reorder_task(self, task_id: int, ordering: int) -> None:
        with self._transaction():
            task = self._task_row(task_id)
            if task["status"] in FINISHED_TASK_STATUSES:
                raise TransitionError(f"task {task['ordinal']} is {str(task['status']).lower()} and cannot be reordered")
            self.db.execute("UPDATE tasks SET ordering=?,updated_at=? WHERE id=?", (int(ordering), utc_now(), task_id))
            self._event(task["mission_id"], "task-reordered", {"ordering": int(ordering)}, task_id=task_id)

    def enqueue_command(self, mission_id: str, payload: dict[str, Any], *, command_id: str | None = None) -> dict[str, Any]:
        """Persist an idempotently keyed operator request before it is applied."""
        cid = command_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", cid):
            raise ValueError("invalid command id")
        now = utc_now()
        with self._transaction():
            self._mission_row(mission_id)
            prior = self.db.execute("SELECT * FROM commands WHERE id=?", (cid,)).fetchone()
            if prior is not None:
                if prior["mission_id"] != mission_id or prior["payload_json"] != self._json(payload):
                    raise TransitionError("command id was already used for a different request")
                return dict(prior)
            self.db.execute("INSERT INTO commands(id,mission_id,payload_json,status,created_at,updated_at) VALUES(?,?,?,'QUEUED',?,?)", (cid, mission_id, self._json(payload), now, now))
            self._event(mission_id, "command-queued", {"command_id": cid, "command": payload.get("action", "unknown")})
        return self.get_command(cid)

    def get_command(self, command_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown command: {command_id}")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["result"] = json.loads(result.pop("result_json")) if result["result_json"] else None
        result.pop("result_json", None)
        return result

    def pending_commands(self, mission_id: str) -> list[dict[str, Any]]:
        self._mission_row(mission_id)
        return [self.get_command(row[0]) for row in self.db.execute("SELECT id FROM commands INDEXED BY commands_mission_fifo WHERE mission_id=? AND status='QUEUED' ORDER BY rowid", (mission_id,))]

    def finish_command(self, command_id: str, *, status: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
        status = status.upper()
        if status not in {"APPLIED", "REJECTED", "FAILED"}:
            raise ValueError("command status must be APPLIED, REJECTED, or FAILED")
        with self._transaction():
            row = self.db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown command: {command_id}")
            if row["status"] != "QUEUED":
                return self.get_command(command_id)
            self.db.execute("UPDATE commands SET status=?,result_json=?,updated_at=? WHERE id=?", (status, self._json(result or {}), utc_now(), command_id))
            self._event(row["mission_id"], "command-" + status.lower(), {"command_id": command_id, "result": result or {}})
        return self.get_command(command_id)

    def begin_iteration(self, task_id: int) -> dict[str, Any]:
        with self._transaction():
            task = self._task_row(task_id)
            pending = json.loads(task["pending_json"])
            if pending:
                self._apply_pending_amendment(task, pending, {"revision": task["revision"]})
            return self.get_task(task_id)

    def begin_operation(self, task_id: int, *, iteration: int, station: str) -> str:
        if iteration < 1 or not station:
            raise ValueError("iteration must be positive and station must be non-empty")
        with self._transaction():
            task = self._task_row(task_id)
            mission = self._mission_row(str(task["mission_id"]))
            if mission["status"] != "RUNNING":
                raise TransitionError("mission is not running; refusing a new operation intent")
            if task["status"] not in {"QUEUED", "READY", "RUNNING"}:
                raise TransitionError(f"task is not runnable: {task['status']}")
            if task["origin"] == "agent-proposal" and task["proposal_status"] != "ACCEPTED":
                raise TransitionError("agent proposal requires operator acceptance before scheduling")
            existing_op = self.db.execute("SELECT id FROM operations WHERE task_id=? AND status IN ('INTENT','PREPARED','RUNNING') LIMIT 1", (task_id,)).fetchone()
            if existing_op:
                raise TransitionError(f"task already has unresolved operation {existing_op[0]}")
            if not self._dependencies_complete(task_id):
                raise TransitionError("task dependency is incomplete")
            pending = json.loads(task["pending_json"])
            if pending:
                self._apply_pending_amendment(task, pending, {"revision": task["revision"]})
                task = self._task_row(task_id)
            op_id = uuid.uuid4().hex
            now = utc_now()
            self.db.execute("UPDATE tasks SET status='RUNNING',updated_at=? WHERE id=?", (now, task_id))
            self.db.execute(
                "INSERT INTO operations(id,mission_id,task_id,iteration,station,status,created_at,updated_at) VALUES(?,?,?,?,?,'PREPARED',?,?)",
                (op_id, task["mission_id"], task_id, iteration, station, now, now),
            )
            self._event(task["mission_id"], "operation-prepared", {"iteration": iteration, "station": station}, task_id=task_id, operation_id=op_id)
            return op_id

    def mark_operation_running(self, operation_id: str, *, pane_id: str = "", process_pid: int | None = None, process_start: str = "", route_id: str = "", model: str = "") -> None:
        with self._transaction():
            op = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise KeyError(f"unknown operation: {operation_id}")
            if op["status"] != "PREPARED":
                raise TransitionError(f"operation cannot start from {op['status']}")
            mission = self._mission_row(str(op["mission_id"]))
            task = self._task_row(int(op["task_id"]))
            if mission["status"] != "RUNNING" or task["status"] != "RUNNING":
                raise TransitionError("operator state change won before provider launch")
            now = utc_now()
            self.db.execute(
                "UPDATE operations SET status='RUNNING',pane_id=?,process_pid=?,process_start=?,route_id=?,model=?,updated_at=? WHERE id=?",
                (pane_id or None, process_pid, process_start or None, route_id or None, model or None, now, operation_id),
            )
            self._event(op["mission_id"], "operation-running", {"pane_id": pane_id, "process_pid": process_pid, "route_id": route_id, "model": model}, task_id=op["task_id"], operation_id=operation_id)

    def abort_prepared_operation(self, operation_id: str, *, reason: str) -> dict[str, Any]:
        """Close a prepared intent when pause/cancel wins before any launch."""
        with self._transaction():
            op = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise KeyError(f"unknown operation: {operation_id}")
            if op["status"] != "PREPARED":
                raise TransitionError(f"only a prepared operation can be aborted before launch: {op['status']}")
            mission = self._mission_row(str(op["mission_id"]))
            task = self._task_row(int(op["task_id"]))
            if task["status"] in {"PAUSED", "CANCELLED"}:
                target = str(task["status"])
            elif mission["status"] in HALTED_MISSION_STATUSES:
                target = "READY"
            elif mission["status"] == "CANCELLED":
                target = "CANCELLED"
            else:
                raise TransitionError("prepared operation is still eligible to launch")
            now = utc_now()
            self.db.execute("UPDATE operations SET status='EXITED',exit_code=143,updated_at=? WHERE id=?", (now, operation_id))
            self.db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (target, now, task["id"]))
            self._event(op["mission_id"], "operation-aborted-before-launch", {
                "reason": reason,
                "exit_code": 143,
                "provider_started": False,
                "task_status": target,
            }, task_id=task["id"], operation_id=operation_id)
        return self.get_task(int(op["task_id"]))

    def record_operation_process(self, operation_id: str, *, process_pid: int, process_start: str) -> None:
        if process_pid < 1 or not process_start:
            raise ValueError("process identity requires a positive pid and start token")
        with self._transaction():
            op = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise KeyError(f"unknown operation: {operation_id}")
            if op["status"] != "RUNNING":
                raise TransitionError(f"operation is not running: {op['status']}")
            self.db.execute(
                "UPDATE operations SET process_pid=?,process_start=?,updated_at=? WHERE id=?",
                (int(process_pid), process_start, utc_now(), operation_id),
            )
            self._event(op["mission_id"], "operation-process", {"process_pid": int(process_pid), "process_start": process_start}, task_id=op["task_id"], operation_id=operation_id)

    def record_operation_session(
        self,
        operation_id: str,
        *,
        restoration_state: str,
        provider: str,
        session_id: str | None = None,
        cwd: str | None = None,
        source: str | None = None,
    ) -> None:
        """Persist an exactly confirmed provider session and its operation state."""
        allowed = {"RESTORING", "NATIVE_CREATED", "RESTORED_NATIVE", "RECONSTRUCTED", "UNAVAILABLE"}
        if restoration_state not in allowed:
            raise ValueError("unknown native session restoration state")
        has_identity = bool(session_id and cwd and source)
        if restoration_state in {"NATIVE_CREATED", "RESTORED_NATIVE"} and not has_identity:
            raise ValueError("native session state requires a confirmed session identity")
        with self._transaction():
            op = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise KeyError(f"unknown operation: {operation_id}")
            mission = self._mission_row(str(op["mission_id"]))
            config = json.loads(mission["config_json"])
            sessions = config.get("native_sessions")
            if not isinstance(sessions, dict):
                sessions = {}
            if restoration_state in {"NATIVE_CREATED", "RESTORED_NATIVE"}:
                sessions[provider] = {
                    "owned_by_bridge": True,
                    "provider": provider,
                    "session_id": session_id,
                    "cwd": cwd,
                    "source": source,
                }
                config["native_sessions"] = sessions
            self.db.execute(
                "UPDATE missions SET config_json=?,updated_at=? WHERE id=?",
                (json.dumps(config, ensure_ascii=False, sort_keys=True), utc_now(), op["mission_id"]),
            )
            self.db.execute(
                "UPDATE operations SET native_session_id=?,restoration_state=?,updated_at=? WHERE id=?",
                (session_id if has_identity else None, restoration_state, utc_now(), operation_id),
            )
            self._event(op["mission_id"], "native-session-restoration", {
                "provider": provider,
                "session_id": session_id if has_identity else None,
                "restoration_state": restoration_state,
                "cwd": cwd if has_identity else None,
                "source": source if has_identity else None,
            }, task_id=op["task_id"], operation_id=operation_id)

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown operation: {operation_id}")
        return dict(row)

    def finish_operation(self, operation_id: str, *, exit_code: int, logs_path: str | None = None) -> dict[str, Any]:
        with self._transaction():
            op = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise KeyError(f"unknown operation: {operation_id}")
            if op["status"] not in ACTIVE_OPERATION_STATUSES:
                raise TransitionError(f"operation already resolved: {op['status']}")
            task = self._task_row(int(op["task_id"]))
            mission = self._mission_row(str(op["mission_id"]))
            requested_status = str(task["status"])
            if requested_status == "CANCELLED":
                boundary_status = "CANCELLED"
            elif requested_status == "PAUSED":
                boundary_status = "PAUSED"
            else:
                boundary_status = None
            if exit_code == 0:
                pending = json.loads(task["pending_json"])
                if pending:
                    self._apply_pending_amendment(task, pending, {"revision": task["revision"], "before_verification": True}, reset_to_ready=True)
                    status = "READY"
                else:
                    status = "VERIFYING"
            elif exit_code in (75, OPERATOR_DECLINED_EXIT_CODE):
                status = "BLOCKED"
            elif exit_code == 143 and mission["status"] in HALTED_MISSION_STATUSES:
                # No provider process was launched: a long readiness wait was
                # interrupted at the operator boundary. Keep the task runnable
                # for a later resume without fabricating provider evidence.
                status = "READY"
            else:
                status = "FAILED"
            if boundary_status is not None:
                status = boundary_status
            now = utc_now()
            self.db.execute("UPDATE operations SET status='EXITED',exit_code=?,logs_path=?,updated_at=? WHERE id=?", (int(exit_code), logs_path, now, operation_id))
            self.db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, now, task["id"]))
            self._event(op["mission_id"], "operation-exited", {"exit_code": int(exit_code), "task_status": status, "logs_path": logs_path, "provider_started": exit_code not in (143, 78)}, task_id=task["id"], operation_id=operation_id)
        return self.get_task(int(op["task_id"]))

    def reconcile_interrupted_operations(self, mission_id: str) -> int:
        count = 0
        with self._transaction():
            self._mission_row(mission_id)
            rows = self.db.execute("SELECT * FROM operations WHERE mission_id=? AND status IN ('INTENT','PREPARED','RUNNING')", (mission_id,)).fetchall()
            for op in rows:
                task = self._task_row(int(op["task_id"]))
                now = utc_now()
                self.db.execute("UPDATE operations SET status='UNCERTAIN',updated_at=? WHERE id=?", (now, op["id"]))
                self.db.execute("UPDATE tasks SET status='BLOCKED',updated_at=? WHERE id=?", (now, task["id"]))
                self._event(mission_id, "operation-uncertain", {"reason": "operation outcome was not committed before controller exit"}, task_id=task["id"], operation_id=op["id"])
                count += 1
        return count

    def declined_operation(self, mission_id: str, task_id: int) -> dict[str, Any] | None:
        """The task's latest operation when the operator declined the provider's trust prompt, else ``None``."""
        row = self.db.execute(
            "SELECT id,station,exit_code,updated_at FROM operations WHERE mission_id=? AND task_id=? ORDER BY rowid DESC LIMIT 1",
            (mission_id, task_id),
        ).fetchone()
        if row is None or row["exit_code"] != OPERATOR_DECLINED_EXIT_CODE:
            return None
        return dict(row)

    def release_declined_tasks(self, mission_id: str) -> list[int]:
        """Make tasks held after a declined trust prompt runnable again; returns their ordinals.

        Called when the operator starts the mission again, which is how they say
        they have dealt with the prompt. A task is released only while its latest
        operation is the declined one, so a task that was held for another reason
        or has since been retried is not touched.
        """
        released: list[int] = []
        with self._transaction():
            self._mission_row(mission_id)
            rows = self.db.execute("SELECT id,ordinal FROM tasks WHERE mission_id=? AND status='BLOCKED'", (mission_id,)).fetchall()
            for task in rows:
                operation = self.declined_operation(mission_id, int(task["id"]))
                if operation is None:
                    continue
                self.db.execute("UPDATE tasks SET status='READY',updated_at=? WHERE id=?", (utc_now(), task["id"]))
                self._event(mission_id, "task-declined-released", {"operation_id": operation["id"], "from": "BLOCKED", "to": "READY"}, task_id=task["id"], operation_id=operation["id"])
                released.append(int(task["ordinal"]))
        return released

    def continue_task_after_provider_failure(self, operation_id: str, *, max_failures: int) -> dict[str, Any] | None:
        """Requeue known provider failures at a safe boundary, within a budget.

        This is deliberately separate from crash reconciliation: only an
        operation with a committed exit status can be handed to another route.
        An interrupted RUNNING operation remains UNCERTAIN/BLOCKED forever
        until an operator resolves it.
        """
        if max_failures < 1:
            raise ValueError("max_failures must be positive")
        with self._transaction():
            op = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise KeyError(f"unknown operation: {operation_id}")
            if op["status"] != "EXITED" or op["exit_code"] in (None, 0, OPERATOR_DECLINED_EXIT_CODE):
                # A declined trust prompt waits for the operator; another provider cannot answer it.
                return None
            task = self._task_row(int(op["task_id"]))
            mission = self._mission_row(str(op["mission_id"]))
            if mission["status"] != "RUNNING" or task["status"] not in {"BLOCKED", "FAILED"}:
                return None
            if json.loads(task["pending_json"]) or not self._dependencies_complete(int(task["id"])):
                return None
            failures = int(self.db.execute(
                "SELECT COUNT(*) FROM operations WHERE task_id=? AND status='EXITED' AND exit_code<>0 AND exit_code<>?",
                (task["id"], OPERATOR_DECLINED_EXIT_CODE),
            ).fetchone()[0])
            if failures > max_failures:
                self._event(op["mission_id"], "provider-handoff-exhausted", {
                    "operation_id": operation_id,
                    "failures": failures,
                    "max_failures": max_failures,
                }, task_id=task["id"], operation_id=operation_id)
                return None
            now = utc_now()
            old = str(task["status"])
            self.db.execute("UPDATE tasks SET status='READY',updated_at=? WHERE id=?", (now, task["id"]))
            self._event(op["mission_id"], "provider-handoff", {
                "operation_id": operation_id,
                "exit_code": int(op["exit_code"]),
                "failure_count": failures,
                "max_failures": max_failures,
                "from": old,
                "to": "READY",
            }, task_id=task["id"], operation_id=operation_id)
        return self.get_task(int(op["task_id"]))

    def verify_task(
        self,
        task_id: int,
        *,
        evidence: str = "",
        approved: bool = False,
        source: str = "operator",
        metadata: dict[str, Any] | None = None,
        record_only: bool = False,
    ) -> dict[str, Any]:
        """Record evidence and, unless ``record_only`` without approval, complete the task.

        The operator CLI passes ``record_only=True`` for ``task verify`` without
        ``--approve`` so a note never completes the task by itself.
        """
        if not evidence.strip() and not approved:
            raise TransitionError("completion requires verification evidence or explicit operator approval")
        if source not in {"operator", "verifier"}:
            raise ValueError("verification evidence source must be operator or verifier")
        completes = approved or not record_only
        with self._transaction():
            task = self._task_row(task_id)
            if task["status"] != "VERIFYING":
                raise TransitionError(f"task {task['ordinal']} is {str(task['status']).lower()}, not awaiting verification")
            if task["pending_json"] != "{}":
                raise TransitionError(f"task {task['ordinal']} has an amendment that has not been applied yet and cannot be verified")
            if not self._dependencies_complete(task_id):
                raise TransitionError(f"task {task['ordinal']} has a dependency that is not complete yet and cannot be verified")
            all_evidence = json.loads(task["evidence_json"])
            if evidence.strip():
                entry = {"text": evidence, "at": utc_now(), "source": source, "revision": int(task["revision"])}
                if metadata:
                    entry.update(metadata)
                all_evidence.append(entry)
            if approved:
                all_evidence.append({"text": "operator approved", "at": utc_now(), "source": "approval", "revision": int(task["revision"])})
            if completes:
                self.db.execute("UPDATE tasks SET status='COMPLETED',evidence_json=?,updated_at=? WHERE id=?", (self._json(all_evidence), utc_now(), task_id))
                self._event(task["mission_id"], "task-verified", {"evidence_count": len(all_evidence), "approved": bool(approved)}, task_id=task_id)
            else:
                self.db.execute("UPDATE tasks SET evidence_json=?,updated_at=? WHERE id=?", (self._json(all_evidence), utc_now(), task_id))
                self._event(task["mission_id"], "task-evidence-recorded", {"evidence_count": len(all_evidence)}, task_id=task_id)
        return self.get_task(task_id)

    def reject_task(self, task_id: int, reason: str, *, origin: str = "operator") -> dict[str, Any]:
        """Send a task awaiting verification back for another attempt.

        VERIFYING -> FAILED -> QUEUED happens in one transaction, with a
        ``task-rejected`` event that carries the operator's reason, so no
        reader can observe the intermediate FAILED state.
        """
        text = reason.strip()
        if not text:
            raise ValueError("a rejection reason is required")
        if len(text) > REJECTION_REASON_LIMIT:
            raise ValueError(f"rejection reason must be at most {REJECTION_REASON_LIMIT} characters")
        with self._transaction():
            task = self._task_row(task_id)
            if task["status"] != "VERIFYING":
                raise TransitionError(f"task {task['ordinal']} is {str(task['status']).lower()}, not awaiting verification")
            self.set_task_status(task_id, "FAILED")
            self._event(task["mission_id"], "task-rejected", {"reason": text, "origin": origin, "revision": int(task["revision"])}, task_id=task_id)
            self.set_task_status(task_id, "QUEUED")
        return self.get_task(task_id)

    def latest_rejection_reason(self, mission_id: str, task_id: int) -> str:
        """The operator's most recent rejection reason for a task, or ``""``."""
        row = self.db.execute(
            "SELECT data_json FROM events WHERE mission_id=? AND kind='task-rejected' AND task_id=? ORDER BY id DESC LIMIT 1",
            (mission_id, task_id),
        ).fetchone()
        if row is None:
            return ""
        reason = json.loads(row["data_json"]).get("reason", "")
        return reason if isinstance(reason, str) else ""

    # -- review hold -----------------------------------------------------------------
    #
    # A native mission is interactive: when a provider run ends with a task in
    # VERIFYING the controller does not start further tasks until the operator
    # decides on that task (approve, reject) or releases the hold with
    # ``continue``. The hold is derived from durable task state, so it has no
    # separate flag that could drift from the tasks.

    @staticmethod
    def auto_continue_default(config: dict[str, Any]) -> bool:
        """Whether a mission with this config starts further tasks without waiting for review."""
        if "auto_continue" in config:
            return bool(config["auto_continue"])
        return not config.get("native_tui") or bool(config.get("headless"))

    def _verifying_marker(self, task_id: int) -> tuple[int, str]:
        """The event that moved a task into VERIFYING; it identifies one review round."""
        row = self.db.execute(
            "SELECT id,created_at FROM events WHERE task_id=? AND kind='operation-exited' "
            "AND json_extract(data_json,'$.task_status')='VERIFYING' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is not None:
            return int(row["id"]), str(row["created_at"])
        fallback = self.db.execute("SELECT updated_at FROM tasks WHERE id=?", (task_id,)).fetchone()
        return 0, str(fallback["updated_at"]) if fallback else ""

    @staticmethod
    def _released_markers(config: dict[str, Any]) -> set[int]:
        return {int(value) for value in config.get("released_reviews", []) if isinstance(value, int)}

    def review_hold(self, mission_id: str) -> dict[str, Any] | None:
        """The hold that keeps the controller from starting another task, or ``None``.

        ``{"reason": "review", "task_ordinal", "task_id", "tasks": [ordinals],
        "since", "marker"}``. A hold exists only while a task awaits review, no
        task is running, another task could start, and the review has not been
        released with ``continue``.
        """
        mission = self.get_mission(mission_id)
        config = mission["config"]
        if self.auto_continue_default(config) or mission["status"] in ENDED_MISSION_STATUSES:
            return None
        counted = "(origin='operator' OR proposal_status='ACCEPTED')"
        if self.db.execute(f"SELECT 1 FROM tasks WHERE mission_id=? AND status='RUNNING' AND {counted} LIMIT 1", (mission_id,)).fetchone():
            return None
        released = self._released_markers(config)
        waiting = []
        for row in self.db.execute(f"SELECT id,ordinal FROM tasks WHERE mission_id=? AND status='VERIFYING' AND {counted} ORDER BY ordinal", (mission_id,)):
            marker, since = self._verifying_marker(int(row["id"]))
            if marker not in released:
                waiting.append((int(row["id"]), int(row["ordinal"]), marker, since))
        if not waiting:
            return None
        runnable = self.db.execute(
            "SELECT 1 FROM tasks t WHERE t.mission_id=? AND t.status IN ('QUEUED','READY') "
            "AND (t.origin='operator' OR t.proposal_status='ACCEPTED') "
            "AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=t.id AND p.status!='COMPLETED') LIMIT 1",
            (mission_id,),
        ).fetchone()
        if runnable is None:
            return None
        task_id, ordinal, marker, since = waiting[0]
        return {"reason": "review", "task_ordinal": ordinal, "task_id": task_id, "tasks": [item[1] for item in waiting], "since": since, "marker": marker}

    def release_review_hold(self, mission_id: str) -> dict[str, Any]:
        """Release the current hold once (``continue``); reviewed tasks stay VERIFYING.

        The reviewed tasks are remembered by the event that moved them into
        VERIFYING, so a task that is rejected and later reaches VERIFYING again
        holds the mission again. ``released`` lists the task numbers released and
        is empty when nothing was holding.
        """
        with self._transaction():
            hold = self.review_hold(mission_id)
            if hold is None:
                return {"released": [], "noop": True, "detail": "Nothing to continue · no task is holding the next one back"}
            config = self.get_mission(mission_id)["config"]
            markers = sorted(self._released_markers(config))
            marks = ",".join("?" for _ in hold["tasks"])
            for row in self.db.execute(f"SELECT id FROM tasks WHERE mission_id=? AND status='VERIFYING' AND ordinal IN ({marks})", (mission_id, *hold["tasks"])).fetchall():
                marker = self._verifying_marker(int(row["id"]))[0]
                if marker not in markers:
                    markers.append(marker)
            config["released_reviews"] = markers[-200:]
            self.db.execute("UPDATE missions SET config_json=?,updated_at=? WHERE id=?", (self._json(config), utc_now(), mission_id))
            self.sync_review_hold_events(mission_id)
        ordinals = hold["tasks"]
        noun = f"task {ordinals[0]}" if len(ordinals) == 1 else f"tasks {', '.join(map(str, ordinals))}"
        return {"released": ordinals, "detail": f"Continuing · {noun} stays awaiting approval while the next task starts"}

    def sync_review_hold_events(self, mission_id: str) -> dict[str, Any] | None:
        """Make ``review-hold`` / ``review-hold-released`` events match the current hold.

        Idempotent. The controller calls it on every loop and ``continue`` calls
        it at once, so each hold has exactly one start and one end event.
        Returns the current hold.
        """
        hold = self.review_hold(mission_id)
        last = self.db.execute(
            "SELECT kind,task_id,data_json FROM events WHERE mission_id=? AND kind IN ('review-hold','review-hold-released') ORDER BY id DESC LIMIT 1",
            (mission_id,),
        ).fetchone()
        open_task = int(last["task_id"]) if last is not None and last["kind"] == "review-hold" and last["task_id"] is not None else None
        open_marker = int(json.loads(last["data_json"]).get("marker") or 0) if open_task is not None else 0
        still_open = hold is not None and open_task is not None and hold["task_id"] == open_task and hold["marker"] == open_marker
        if still_open or (hold is None and open_task is None):
            return hold
        with self._transaction():
            if open_task is not None:
                self._record_hold_release(mission_id, open_task, open_marker)
            if hold is not None:
                n = hold["task_ordinal"]
                self._event(mission_id, "review-hold", {
                    "task_ordinal": n, "marker": hold["marker"],
                    "summary": f"holding before the next task · task {n} awaits your review",
                }, task_id=hold["task_id"])
        return hold

    def _record_hold_release(self, mission_id: str, task_id: int, marker: int) -> None:
        task = self._task_row(task_id)
        ordinal = int(task["ordinal"])
        status = str(task["status"])
        same_round = self._verifying_marker(task_id)[0] == marker
        if status == "VERIFYING" and same_round:
            continued = marker in self._released_markers(self.get_mission(mission_id)["config"])
            how, text = ("continue", "continuing") if continued else ("auto-continue", "auto-continue is on")
            text = f"task {ordinal} still awaits approval; {text} and the next task may start"
        elif status == "COMPLETED":
            how, text = "approved", f"task {ordinal} approved"
        elif status in {"VERIFYING", "QUEUED", "READY", "FAILED"}:
            how, text = "rejected", f"task {ordinal} sent back for another attempt"
        else:
            how, text = status.lower(), f"task {ordinal} is {status.lower()}"
        self._event(mission_id, "review-hold-released", {"task_ordinal": ordinal, "marker": marker, "how": how, "summary": f"hold released · {text}"}, task_id=task_id)

    def record_verification_attempt(self, task_id: int, result: dict[str, Any]) -> None:
        with self._transaction():
            task = self._task_row(task_id)
            if task["status"] != "VERIFYING":
                raise TransitionError(f"task is not awaiting verification: {task['status']}")
            self._event(str(task["mission_id"]), "verification-failed", result, task_id=task_id)

    def set_task_status(self, task_id: int, status: str) -> dict[str, Any]:
        status = status.upper()
        if status not in TASK_STATES:
            raise ValueError(f"invalid task status: {status}")
        with self._transaction():
            task = self._task_row(task_id)
            old = str(task["status"])
            if status != old and status not in TASK_TRANSITIONS[old]:
                raise TransitionError(f"task {task['ordinal']} is {old.lower()} and cannot become {status.lower()}")
            if status == "READY" and not self._dependencies_complete(task_id):
                raise TransitionError("task dependency is incomplete and prevents readiness")
            self.db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, utc_now(), task_id))
            self._event(task["mission_id"], "task-status", {"from": old, "to": status}, task_id=task_id)
        return self.get_task(task_id)

    def events(self, mission_id: str, *, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        self._mission_row(mission_id)
        if limit is not None and not 0 <= limit <= 1000:
            raise ValueError("event page limit must be between 0 and 1000")
        query = "SELECT e.*, t.ordinal AS task_ordinal FROM events e LEFT JOIN tasks t ON t.id=e.task_id WHERE e.mission_id=? AND e.id>? ORDER BY e.id"
        params: tuple[Any, ...] = (mission_id, after)
        if limit is not None:
            query += " LIMIT ?"
            params += (limit,)
        return [
            {**dict(row), "data": json.loads(row["data_json"])}
            for row in self.db.execute(query, params)
        ]

    def prune_plumbing_events(
        self,
        mission_id: str,
        *,
        now: datetime | None = None,
        heartbeat_keep_seconds: int = HEARTBEAT_RETENTION_SECONDS,
        capacity_keep_seconds: int = CAPACITY_REFRESH_FULL_SECONDS,
        chunk: int = 2000,
        budget_seconds: float = 1.0,
    ) -> dict[str, Any]:
        """Bound the two event kinds the controller writes forever, and nothing else.

        A heartbeat every 5 s and a capacity refresh every minute made up about
        92% of a mission's events (roughly 70 MB a week). Heartbeats older than
        ``heartbeat_keep_seconds`` are deleted. Capacity refreshes older than
        ``capacity_keep_seconds`` are thinned to the last one in each 10-minute
        bucket. The newest row of each kind is never deleted, because
        status reads the latest heartbeat and latest capacity observation. Every
        other kind (task, operation, verification, operator and error events) is
        left alone.

        Rows are deleted in ``chunk``-sized transactions so a first pass over a
        large log does not hold the write lock for long, and the pass stops after
        ``budget_seconds``; ``complete`` is false when more remains and the call
        should be repeated. Returns ``{"deleted": {kind: count}, "complete": bool,
        "heartbeat_before": iso, "capacity_before": iso}``.
        """
        self._mission_row(mission_id)
        moment = now or datetime.now(timezone.utc)
        heartbeat_before = (moment - timedelta(seconds=int(heartbeat_keep_seconds))).isoformat(timespec="seconds")
        capacity_before = (moment - timedelta(seconds=int(capacity_keep_seconds))).isoformat(timespec="seconds")
        heartbeat_sql = (
            "DELETE FROM events WHERE id IN (SELECT id FROM events WHERE mission_id=:mission AND kind='controller-heartbeat' AND created_at<:cutoff "
            "AND id<(SELECT MAX(id) FROM events WHERE mission_id=:mission AND kind='controller-heartbeat') ORDER BY id LIMIT :limit)"
        )
        deleted = {"controller-heartbeat": 0, "capacity-refresh": 0}
        deadline = time.monotonic() + max(0.0, budget_seconds)
        complete = True
        while True:
            with self._transaction():
                count = max(0, self.db.execute(heartbeat_sql, {"mission": mission_id, "cutoff": heartbeat_before, "limit": int(chunk)}).rowcount)
            deleted["controller-heartbeat"] += count
            if count < chunk:
                break
            if time.monotonic() >= deadline:
                complete = False
                break
        if complete:
            # Rows are visited in id order in windows of ``chunk`` rows. A row goes when the next
            # older-than-cutoff row falls in the same 10-minute bucket (the first 15 characters of
            # the UTC timestamp), so the last row of each bucket stays, and so does the newest row.
            last = 0
            while True:
                rows = self.db.execute(
                    "SELECT id,substr(created_at,1,15) FROM events INDEXED BY events_mission_kind_id "
                    "WHERE mission_id=? AND kind='capacity-refresh' AND id>? AND created_at<? ORDER BY id LIMIT ?",
                    (mission_id, last, capacity_before, int(chunk) + 1),
                ).fetchall()
                doomed = [(int(rows[i][0]),) for i in range(len(rows) - 1) if rows[i][1] == rows[i + 1][1]]
                if doomed:
                    with self._transaction():
                        self.db.executemany("DELETE FROM events WHERE id=?", doomed)
                    deleted["capacity-refresh"] += len(doomed)
                if len(rows) <= chunk:
                    break
                last = int(rows[-1][0]) - 1
                if time.monotonic() >= deadline:
                    complete = False
                    break
        return {"deleted": {kind: count for kind, count in deleted.items() if count}, "complete": complete,
                "heartbeat_before": heartbeat_before, "capacity_before": capacity_before}

    def record_events_pruned(self, mission_id: str, result: dict[str, Any]) -> int:
        """Audit record of a finished prune pass: which kinds lost how many rows, and the cutoffs used."""
        deleted = {str(kind): int(count) for kind, count in dict(result.get("deleted", {})).items()}
        return self.record_runtime_event(mission_id, "events-pruned", {
            "deleted": deleted,
            "total": sum(deleted.values()),
            "heartbeat_before": str(result.get("heartbeat_before", "")),
            "capacity_before": str(result.get("capacity_before", "")),
            "observed_at": utc_now(),
        })

    def refresh_runtime_event(self, event_id: int, data: dict[str, Any]) -> bool:
        """Replace an observation event's payload and time with a newer identical-state observation.

        For ``capacity-refresh`` rows only. When a refresh finds the same state as
        the previous one, the previous row is brought up to date instead of
        appending a duplicate, so the observation age stays true and the log does
        not grow.
        """
        bounded = json.loads(self._json(data))
        if len(self._json(bounded).encode("utf-8")) > 16_384:
            raise ValueError("runtime event payload exceeds 16 KiB")
        with self._transaction():
            cursor = self.db.execute(
                "UPDATE events SET data_json=?,created_at=? WHERE id=? AND kind='capacity-refresh'",
                (self._json(bounded), utc_now(), int(event_id)),
            )
            return cursor.rowcount == 1

    def latest_event_id(self, mission_id: str, kinds: tuple[str, ...]) -> int:
        """Id of the newest event of any of ``kinds`` (0 when there is none), read through the kind index."""
        newest = 0
        for kind in kinds:
            row = self.db.execute(
                "SELECT id FROM events INDEXED BY events_mission_kind_id WHERE mission_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                (mission_id, kind),
            ).fetchone()
            if row is not None:
                newest = max(newest, int(row[0]))
        return newest

    def latest_event(self, mission_id: str, kind: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM events INDEXED BY events_mission_kind_id WHERE mission_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (mission_id, kind),
        ).fetchone()
        return None if row is None else {**dict(row), "data": json.loads(row["data_json"])}

    def recent_task_history(self, mission_id: str, task_id: int, limit: int = 20) -> list[sqlite3.Row]:
        """The newest ``limit`` events of a task or of its operations, newest first.

        Two indexed slices (``events_task_id`` and ``events_operation_id``) are
        merged. The second slice skips rows already tagged with the task, so no
        event appears twice. ``INDEXED BY`` and the unary ``+`` on ``mission_id`` keep
        the planner from choosing the mission-wide index to avoid a sort. The earlier single ``task_id=? OR operation_id IN (...)``
        predicate could use neither index and walked the mission's events from the
        newest end until it found enough matches, which is the whole log (about
        0.85 s per 1M events) when the task's events are old.
        """
        bound = max(1, int(limit))
        return self.db.execute(
            "SELECT id,kind,data_json,created_at FROM ("
            "SELECT * FROM (SELECT id,kind,data_json,created_at FROM events INDEXED BY events_task_id WHERE task_id=:task AND +mission_id=:mission ORDER BY id DESC LIMIT :limit) "
            "UNION ALL "
            "SELECT * FROM (SELECT id,kind,data_json,created_at FROM events INDEXED BY events_operation_id WHERE operation_id IN (SELECT id FROM operations WHERE task_id=:task) "
            "AND +mission_id=:mission AND task_id IS NOT :task ORDER BY id DESC LIMIT :limit)"
            ") ORDER BY id DESC LIMIT :limit",
            {"task": int(task_id), "mission": mission_id, "limit": bound},
        ).fetchall()

    def record_runtime_event(self, mission_id: str, kind: str, data: dict[str, Any]) -> int:
        if not kind or len(kind) > 100:
            raise ValueError("runtime event kind must be a non-empty value up to 100 characters")
        bounded = json.loads(self._json(data))
        if len(self._json(bounded).encode("utf-8")) > 16_384:
            raise ValueError("runtime event payload exceeds 16 KiB")
        with self._transaction():
            self._mission_row(mission_id)
            self._event(mission_id, kind, bounded)
            return int(self.db.execute("SELECT last_insert_rowid()").fetchone()[0])

    def set_mission_config_value(self, mission_id: str, key: str, value: Any, *, event_kind: str) -> dict[str, Any]:
        if not key or key in {"id", "cwd", "objective"}:
            raise ValueError("invalid mutable mission config key")
        with self._transaction():
            mission = self._mission_row(mission_id)
            config = json.loads(mission["config_json"])
            config[key] = value
            self.db.execute(
                "UPDATE missions SET config_json=?,updated_at=? WHERE id=?",
                (self._json(config), utc_now(), mission_id),
            )
            self._event(mission_id, event_kind, {"key": key, "value": value})
        return self.get_mission(mission_id)

    def set_rotation(self, mission_id: str, *, providers: str, routes: str, source: str) -> dict[str, Any]:
        """Persist the mission's saved rotation and record the change.

        Returns ``{"changed", "from", "to", "mission"}``. No event is recorded
        when the rotation is already the saved one.
        """
        with self._transaction():
            mission = self._mission_row(mission_id)
            config = json.loads(mission["config_json"])
            before = rotation_label(config.get("providers"), config.get("routes"))
            after = rotation_label(providers, routes)
            if (str(config.get("providers", "")), str(config.get("routes", ""))) != (providers, routes):
                config["providers"], config["routes"] = providers, routes
                self.db.execute("UPDATE missions SET config_json=?,updated_at=? WHERE id=?", (self._json(config), utc_now(), mission_id))
            if before and before != after:
                self._event(mission_id, "rotation-changed", {"from": before, "to": after, "providers": providers, "routes": routes, "source": source})
        return {"changed": bool(before) and before != after, "from": before, "to": after, "mission": self.get_mission(mission_id)}

    def set_mission_config_values(self, mission_id: str, values: dict[str, Any], *, event_kind: str) -> dict[str, Any]:
        """Atomically persist a related set of controller configuration values."""
        if not values or any(not key or key in {"id", "cwd", "objective"} for key in values):
            raise ValueError("invalid mutable mission config values")
        with self._transaction():
            mission = self._mission_row(mission_id)
            config = json.loads(mission["config_json"])
            changed = {key: value for key, value in values.items() if config.get(key) != value}
            if changed:
                config.update(changed)
                self.db.execute(
                    "UPDATE missions SET config_json=?,updated_at=? WHERE id=?",
                    (self._json(config), utc_now(), mission_id),
                )
                self._event(mission_id, event_kind, {"values": changed})
        return self.get_mission(mission_id)

    def record_pruned_operation_log(self, mission_id: str, path: str, *, size_bytes: int, sha256: str) -> int:
        """Detach an expired raw log reference while preserving its digest."""
        with self._transaction():
            self._mission_row(mission_id)
            rows = self.db.execute(
                "SELECT id,task_id FROM operations WHERE mission_id=? AND logs_path=?",
                (mission_id, path),
            ).fetchall()
            for row in rows:
                self.db.execute("UPDATE operations SET logs_path=NULL,updated_at=? WHERE id=?", (utc_now(), row["id"]))
                self._event(mission_id, "operation-log-pruned", {
                    "operation_id": str(row["id"]),
                    "path": path,
                    "size_bytes": int(size_bytes),
                    "sha256": sha256,
                }, task_id=int(row["task_id"]), operation_id=str(row["id"]))
            return len(rows)

    def operation_logs_in_directory(self, mission_id: str, directory: str | os.PathLike[str]) -> list[dict[str, Any]]:
        """Return only operation logs beneath a directory using the logs index."""
        self._mission_row(mission_id)
        prefix = os.path.join(os.fspath(directory), "")
        # '/' sorts below '0' under SQLite's default BINARY collation, so the
        # half-open range includes all descendants but excludes similarly
        # prefixed sibling paths.
        upper = prefix[:-1] + "0"
        rows = self.db.execute(
            "SELECT id,task_id,logs_path FROM operations WHERE mission_id=? AND logs_path>=? AND logs_path<?",
            (mission_id, prefix, upper),
        ).fetchall()
        return [dict(row) for row in rows]

    def prepare_managed_process(self, mission_id: str, task_id: int, *, kind: str) -> str:
        if kind != "verifier":
            raise ValueError("unsupported managed process kind")
        process_id = uuid.uuid4().hex
        with self._transaction():
            mission = self._mission_row(mission_id)
            task = self._task_row(task_id)
            if task["mission_id"] != mission_id:
                raise TransitionError("managed process task belongs to another mission")
            if mission["status"] != "RUNNING" or task["status"] != "VERIFYING":
                raise TransitionError("verifier process requires a running mission and verifying task")
            now = utc_now()
            self.db.execute(
                "INSERT INTO managed_processes(id,mission_id,task_id,kind,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (process_id, mission_id, task_id, kind, "STARTING", now, now),
            )
            self._event(mission_id, "managed-process-prepared", {"process_id": process_id, "kind": kind}, task_id=task_id)
        return process_id

    def mark_managed_process_running(self, process_id: str, *, process_pid: int, process_start: str) -> dict[str, Any]:
        if process_pid < 2 or not process_start:
            raise ValueError("managed process requires a valid pid and start token")
        with self._transaction():
            row = self._managed_process_row(process_id)
            if row["status"] != "STARTING":
                raise TransitionError(f"managed process is not starting: {row['status']}")
            self.db.execute(
                "UPDATE managed_processes SET process_pid=?,process_start=?,status='RUNNING',updated_at=? WHERE id=?",
                (int(process_pid), process_start, utc_now(), process_id),
            )
            self._event(str(row["mission_id"]), "managed-process-started", {
                "process_id": process_id, "kind": row["kind"], "process_pid": int(process_pid),
            }, task_id=int(row["task_id"]))
        return self.get_managed_process(process_id)

    def finish_managed_process(self, process_id: str, *, exit_code: int | None, status: str = "EXITED") -> dict[str, Any]:
        if status not in {"EXITED", "TERMINATED", "FAILED", "UNCERTAIN"}:
            raise ValueError("invalid managed process result status")
        with self._transaction():
            row = self._managed_process_row(process_id)
            if row["status"] not in {"STARTING", "RUNNING"}:
                if row["status"] == status and row["exit_code"] == exit_code:
                    return self.get_managed_process(process_id)
                raise TransitionError(f"managed process is already resolved: {row['status']}")
            self.db.execute(
                "UPDATE managed_processes SET status=?,exit_code=?,updated_at=? WHERE id=?",
                (status, exit_code, utc_now(), process_id),
            )
            self._event(str(row["mission_id"]), "managed-process-exited", {
                "process_id": process_id, "kind": row["kind"], "status": status, "exit_code": exit_code,
            }, task_id=int(row["task_id"]))
        return self.get_managed_process(process_id)

    def get_managed_process(self, process_id: str) -> dict[str, Any]:
        return dict(self._managed_process_row(process_id))

    def unresolved_managed_processes(self, mission_id: str) -> list[dict[str, Any]]:
        self._mission_row(mission_id)
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM managed_processes WHERE mission_id=? AND status IN ('STARTING','RUNNING') ORDER BY created_at,id",
            (mission_id,),
        )]

    def _managed_process_row(self, process_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM managed_processes WHERE id=?", (process_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown managed process {process_id}")
        return row

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> BridgeStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class MissionLock:
    """Non-blocking lifetime flock for one mission process owner."""

    def __init__(self, directory: str | Path, mission_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", mission_id):
            raise ValueError("invalid mission id for lock")
        self.directory = Path(directory).expanduser().absolute()
        self.mission_id = mission_id
        self.path = self.directory / f"mission-{mission_id}.lock"
        self._file: Any | None = None

    def acquire(self, timeout: float = 0.0, poll: float = 0.02) -> None:
        """Take the lock without blocking indefinitely.

        With a positive ``timeout`` a busy lock is retried every ``poll``
        seconds, so a momentary liveness probe by another process is not
        mistaken for a controller that owns the mission. Raises
        ``BlockingIOError`` when the lock is still held after the timeout.
        """
        if self._file is not None:
            return
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError(f"mission lock directory must be owned by this user and private: {self.directory}")
        self._file = self.path.open("a+")
        os.chmod(self.path, 0o600)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise
                time.sleep(poll)

    @classmethod
    def is_held(cls, directory: str | Path, mission_id: str) -> bool:
        """Whether some process currently owns this mission's controller lock.

        Uses a non-blocking shared probe on a read-only descriptor so it never
        creates the lock file and concurrent probes do not mask each other.
        """
        path = cls(directory, mission_id).path
        try:
            stream = path.open("r")
        except OSError:
            return False
        try:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            except OSError:
                return False
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            stream.close()

    def release(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self) -> MissionLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


_RAISE: Any = object()


def controller_running(mission_id: str, *, on_error: bool | None = _RAISE) -> bool | None:
    """Whether a live process holds this mission's controller lock.

    The probe is non-blocking and never creates the lock file. Callers differ
    in what an unreadable lock means, so that is explicit: by default an
    ``OSError`` or ``ValueError`` from the probe propagates (the command line,
    which reports it); ``on_error=None`` returns ``None`` for "unknown" (the
    launcher, which shows the mission without a controller claim); and
    ``on_error=False`` returns ``False`` (the switcher, which lists the
    mission as not running).
    """
    try:
        return MissionLock.is_held(default_state_dir() / "locks", mission_id)
    except (OSError, ValueError):
        if on_error is _RAISE:
            raise
        return on_error
