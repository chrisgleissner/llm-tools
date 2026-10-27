"""Transactional persistence for LLM Bridge missions and task operations."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


TASK_STATES = frozenset({"QUEUED", "READY", "RUNNING", "PAUSED", "BLOCKED", "VERIFYING", "COMPLETED", "FAILED", "CANCELLED"})
MISSION_STATES = frozenset({"CREATED", "RUNNING", "PAUSED", "STOPPING", "STOPPED", "COMPLETED", "FAILED", "CANCELLED"})
UNRESOLVED_OPERATION_STATES = frozenset({"INTENT", "PREPARED", "RUNNING"})
SCHEMA_VERSION = 6


class TransitionError(ValueError):
    """A requested mission/task transition violates the durable state model."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_state_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("LLM_BRIDGE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    state_home = Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return state_home / "llm-tools" / "llm-bridge"


def repository_identity(cwd: str | Path) -> str:
    """Resolve a directory to its Git worktree root, falling back to the path."""
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


class BridgeStore:
    """Single SQLite source of truth for missions, tasks, events and operations.

    SQLite WAL plus FULL synchronous commits make each state transition and its
    event record durable together. The enclosing state directory is private;
    callers may open independent instances from different processes.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_state_dir() / "bridge.sqlite3"
        self.path = self.path.expanduser().absolute()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = self.path.parent.stat()
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise PermissionError(f"Bridge state directory must be owned by this user and private: {self.path.parent}")
        self.db = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self._initialize()
        self._secure_sqlite_files()

    def _initialize(self) -> None:
        current_version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(f"Bridge database schema {current_version} is newer than supported schema {SCHEMA_VERSION}")
        self.db.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS missions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                objective TEXT NOT NULL,
                cwd TEXT NOT NULL,
                repo_identity TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{{}}',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                original_prompt TEXT NOT NULL,
                prompt TEXT NOT NULL,
                pending_json TEXT NOT NULL DEFAULT '{{}}',
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
            );
            CREATE TABLE IF NOT EXISTS task_dependencies (
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                depends_on INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                PRIMARY KEY (task_id, depends_on),
                CHECK (task_id != depends_on)
            );
            CREATE TABLE IF NOT EXISTS amendments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,
                changes_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                applied_at TEXT,
                UNIQUE(task_id, revision)
            );
            CREATE TABLE IF NOT EXISTS operations (
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
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
                task_id INTEGER,
                operation_id TEXT,
                schema_version INTEGER NOT NULL DEFAULT {SCHEMA_VERSION},
                kind TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'QUEUED',
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS managed_processes (
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
            );
            CREATE INDEX IF NOT EXISTS events_mission_id ON events(mission_id, id);
            CREATE INDEX IF NOT EXISTS tasks_mission_state_order ON tasks(mission_id, status, priority DESC, ordering, id);
            CREATE INDEX IF NOT EXISTS commands_mission_queue ON commands(mission_id,status,created_at);
            CREATE INDEX IF NOT EXISTS operations_mission_logs_path ON operations(mission_id,logs_path);
            CREATE INDEX IF NOT EXISTS operations_mission_history ON operations(mission_id);
            CREATE INDEX IF NOT EXISTS operations_task_history ON operations(task_id);
            CREATE INDEX IF NOT EXISTS commands_mission_fifo ON commands(mission_id,status);
            CREATE INDEX IF NOT EXISTS managed_processes_mission_status ON managed_processes(mission_id,status,created_at);
            COMMIT;
            """
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_column("missions", "config_json", "TEXT NOT NULL DEFAULT '{}'" )
            self._ensure_column("tasks", "original_prompt", "TEXT NOT NULL DEFAULT ''")
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
            self._ensure_column("events", "schema_version", f"INTEGER NOT NULL DEFAULT {SCHEMA_VERSION}")
            self.db.execute("CREATE INDEX IF NOT EXISTS events_mission_kind_id ON events(mission_id,kind,id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS tasks_mission_proposal_status ON tasks(mission_id,origin,proposal_status,status)")
            self.db.execute("CREATE INDEX IF NOT EXISTS operations_mission_logs_path ON operations(mission_id,logs_path)")
            self.db.execute("UPDATE tasks SET original_prompt=prompt WHERE original_prompt=''")
            if current_version < SCHEMA_VERSION:
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
            self.db.execute("CREATE UNIQUE INDEX one_active_mission_per_worktree ON missions(repo_identity) WHERE status IN ('RUNNING','PAUSED','STOPPING')")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_operation_per_task ON operations(task_id) WHERE status IN ('INTENT','PREPARED','RUNNING')")
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        self._secure_sqlite_files()

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
            raise KeyError(f"unknown mission: {mission_id}")
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
        task = dict(row)
        task["acceptance"] = json.loads(task.pop("acceptance_json"))
        task["evidence"] = json.loads(task.pop("evidence_json"))
        task["pending"] = json.loads(task.pop("pending_json"))
        task["depends_on"] = [
            int(r[0]) for r in self.db.execute("SELECT depends_on FROM task_dependencies WHERE task_id=? ORDER BY depends_on", (task["id"],))
        ]
        return task

    def create_mission(
        self,
        name: str,
        objective: str,
        *,
        cwd: str | Path,
        mission_id: str | None = None,
        initial_prompt: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                    task_title = initial_prompt.splitlines()[0][:100] if initial_prompt.splitlines() else name.strip()
                    cur = self.db.execute(
                        "INSERT INTO tasks(mission_id,title,original_prompt,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (mid, task_title, initial_prompt, initial_prompt, now, now),
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
        allowed = {
            "CREATED": {"RUNNING", "CANCELLED"},
            "RUNNING": {"PAUSED", "STOPPING", "STOPPED", "COMPLETED", "FAILED", "CANCELLED"},
            "PAUSED": {"RUNNING", "STOPPING", "CANCELLED"},
            "STOPPING": {"STOPPED", "RUNNING", "FAILED", "CANCELLED"},
            "STOPPED": {"RUNNING", "CANCELLED"},
            "COMPLETED": set(), "FAILED": {"RUNNING", "CANCELLED"}, "CANCELLED": set(),
        }
        with self._transaction():
            mission = self._mission_row(mission_id)
            old = str(mission["status"])
            if status != old and status not in allowed[old]:
                raise TransitionError(f"invalid mission transition: {old} -> {status}")
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
            if mission["status"] in {"COMPLETED", "CANCELLED"}:
                raise TransitionError("cannot add a task to a terminal mission")
            next_order = int(self.db.execute("SELECT COALESCE(MAX(ordering),0)+10 FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0])
            cur = self.db.execute(
                "INSERT INTO tasks(mission_id,title,original_prompt,prompt,priority,ordering,acceptance_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (mission_id, title or prompt.splitlines()[0][:100], prompt, prompt, int(priority), next_order if order is None else int(order), self._json(list(acceptance)), now, now),
            )
            task_id = int(cur.lastrowid)
            for dep in depends_on:
                self._add_dependency_locked(mission_id, task_id, int(dep))
            self._event(mission_id, "task-added", {"title": title or prompt.splitlines()[0][:100], "priority": int(priority)}, task_id=task_id)
        return task_id

    def _add_dependency_locked(self, mission_id: str, task_id: int, depends_on: int) -> None:
        task = self._task_row(task_id)
        dep = self._task_row(depends_on)
        if task["mission_id"] != mission_id or dep["mission_id"] != mission_id:
            raise TransitionError("dependencies must refer to tasks in the same mission")
        if task["status"] in {"RUNNING", "VERIFYING"}:
            raise TransitionError(f"cannot add a dependency to a {str(task['status']).lower()} task")
        if task["status"] in {"COMPLETED", "CANCELLED"}:
            raise TransitionError(f"cannot change dependencies of terminal task: {task['status']}")
        if task_id == depends_on:
            raise TransitionError("task cannot depend on itself")
        cycle = self.db.execute(
            "WITH RECURSIVE ancestors(id) AS (SELECT depends_on FROM task_dependencies WHERE task_id=? UNION SELECT d.depends_on FROM task_dependencies d JOIN ancestors a ON d.task_id=a.id) SELECT 1 FROM ancestors WHERE id=? LIMIT 1",
            (depends_on, task_id),
        ).fetchone()
        if cycle:
            raise TransitionError("dependency would create a cycle")
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
        now = utc_now()
        with self._transaction():
            mission = self._mission_row(mission_id)
            if mission["status"] in {"COMPLETED", "CANCELLED"}:
                raise TransitionError("cannot add a proposal to a terminal mission")
            source = self.db.execute("SELECT mission_id FROM operations WHERE id=?", (source_operation_id,)).fetchone()
            if source is None or source["mission_id"] != mission_id:
                raise TransitionError("proposal source operation must belong to the same mission")
            next_order = int(self.db.execute("SELECT COALESCE(MAX(ordering),0)+10 FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0])
            task_title = title or prompt.splitlines()[0][:100]
            cur = self.db.execute(
                "INSERT INTO tasks(mission_id,title,original_prompt,prompt,priority,ordering,acceptance_json,origin,proposal_status,proposed_by_operation,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (mission_id, task_title, prompt, prompt, int(priority), next_order, self._json(list(acceptance)), "agent-proposal", "PENDING", source_operation_id, now, now),
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
                raise TransitionError("proposal belongs to another mission")
            if task["origin"] != "agent-proposal" or task["proposal_status"] != "PENDING":
                raise TransitionError("task is not a pending agent proposal")
            if accept:
                mission = self._mission_row(mission_id)
                if mission["status"] in {"COMPLETED", "CANCELLED"}:
                    raise TransitionError("cannot accept a proposal on a terminal mission")
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
            self._event(mission_id, "dependency-added", {"depends_on": depends_on}, task_id=task_id)

    def get_task(self, task_id: int) -> dict[str, Any]:
        return self._task_dict(self._task_row(task_id))

    def list_tasks(self, mission_id: str) -> list[dict[str, Any]]:
        self._mission_row(mission_id)
        return [self._task_dict(row) for row in self.db.execute("SELECT * FROM tasks WHERE mission_id=? ORDER BY ordering,id", (mission_id,))]

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
        return [self._task_dict(row) for row in rows]

    def ready_tasks(self, mission_id: str) -> list[dict[str, Any]]:
        """Return runnable tasks in priority-descending, stable queue order."""
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
                "SELECT * FROM tasks t WHERE mission_id=? AND status='READY' AND (t.origin='operator' OR t.proposal_status='ACCEPTED') AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=t.id AND p.status!='COMPLETED') ORDER BY priority DESC,ordering,id",
                (mission_id,),
            )
            return [self._task_dict(row) for row in rows]

    def amend_task(self, task_id: int, *, prompt: str | None = None, title: str | None = None, priority: int | None = None, acceptance: list[str] | None = None) -> int:
        with self._transaction():
            task = self._task_row(task_id)
            if task["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                raise TransitionError(f"cannot amend terminal task: {task['status']}")
            pending = json.loads(task["pending_json"])
            for key, value in (("prompt", prompt), ("title", title), ("priority", priority), ("acceptance", acceptance)):
                if value is not None:
                    pending[key] = value
            revision = int(task["revision"]) + 1
            self.db.execute("UPDATE tasks SET pending_json=?,revision=?,updated_at=? WHERE id=?", (self._json(pending), revision, utc_now(), task_id))
            self.db.execute("INSERT INTO amendments(task_id,revision,changes_json,status,created_at) VALUES(?,?,?,'PENDING',?)", (task_id, revision, self._json(pending), utc_now()))
            if task["status"] == "VERIFYING":
                mapping = {"prompt": "prompt", "title": "title", "priority": "priority", "acceptance": "acceptance_json"}
                assignments, values = [], []
                for key, value in pending.items():
                    if key in mapping:
                        assignments.append(mapping[key] + "=?")
                        values.append(self._json(value) if key == "acceptance" else value)
                assignments.extend(["pending_json='{}'", "evidence_json='[]'", "status='READY'"])
                values.extend([utc_now(), task_id])
                self.db.execute(f"UPDATE tasks SET {','.join(assignments)},updated_at=? WHERE id=?", values)
                self.db.execute("UPDATE amendments SET status='APPLIED',applied_at=? WHERE task_id=? AND status='PENDING'", (utc_now(), task_id))
                self._event(task["mission_id"], "task-amendment-applied", {"revision": revision, "reason": "after-verification-result"}, task_id=task_id)
            self._event(task["mission_id"], "task-amended", {"revision": revision, "fields": sorted(pending)}, task_id=task_id)
            return revision

    def set_task_priority(self, task_id: int, priority: int) -> None:
        with self._transaction():
            task = self._task_row(task_id)
            if task["status"] in {"COMPLETED", "CANCELLED"}:
                raise TransitionError(f"cannot reprioritize terminal task: {task['status']}")
            self.db.execute("UPDATE tasks SET priority=?,updated_at=? WHERE id=?", (int(priority), utc_now(), task_id))
            self._event(task["mission_id"], "task-priority", {"priority": int(priority)}, task_id=task_id)

    def reorder_task(self, task_id: int, ordering: int) -> None:
        with self._transaction():
            task = self._task_row(task_id)
            if task["status"] in {"COMPLETED", "CANCELLED"}:
                raise TransitionError(f"cannot reorder terminal task: {task['status']}")
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
                columns: list[str] = []
                values: list[Any] = []
                mapping = {"prompt": "prompt", "title": "title", "priority": "priority", "acceptance": "acceptance_json"}
                for key, value in pending.items():
                    if key not in mapping:
                        continue
                    columns.append(mapping[key] + "=?")
                    values.append(self._json(value) if key == "acceptance" else value)
                values.extend([utc_now(), task_id])
                self.db.execute(f"UPDATE tasks SET {','.join(columns)},pending_json='{{}}',updated_at=? WHERE id=?", values)
                self.db.execute("UPDATE amendments SET status='APPLIED',applied_at=? WHERE task_id=? AND status='PENDING'", (utc_now(), task_id))
                self._event(task["mission_id"], "task-amendment-applied", {"revision": task["revision"]}, task_id=task_id)
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
                mapping = {"prompt": "prompt", "title": "title", "priority": "priority", "acceptance": "acceptance_json"}
                assignments, values = [], []
                for key, value in pending.items():
                    if key in mapping:
                        assignments.append(mapping[key] + "=?")
                        values.append(self._json(value) if key == "acceptance" else value)
                values.extend([utc_now(), task_id])
                self.db.execute(f"UPDATE tasks SET {','.join(assignments)},pending_json='{{}}',updated_at=? WHERE id=?", values)
                self.db.execute("UPDATE amendments SET status='APPLIED',applied_at=? WHERE task_id=? AND status='PENDING'", (utc_now(), task_id))
                task = self._task_row(task_id)
                self._event(task["mission_id"], "task-amendment-applied", {"revision": task["revision"]}, task_id=task_id)
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
            elif mission["status"] in {"PAUSED", "STOPPING", "STOPPED"}:
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
            if op["status"] not in UNRESOLVED_OPERATION_STATES:
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
                    mapping = {"prompt": "prompt", "title": "title", "priority": "priority", "acceptance": "acceptance_json"}
                    assignments, values = [], []
                    for key, value in pending.items():
                        if key in mapping:
                            assignments.append(mapping[key] + "=?")
                            values.append(self._json(value) if key == "acceptance" else value)
                    assignments.append("evidence_json='[]'")
                    values.extend([utc_now(), task["id"]])
                    self.db.execute(f"UPDATE tasks SET {','.join(assignments)},pending_json='{{}}',status='READY',updated_at=? WHERE id=?", values)
                    self.db.execute("UPDATE amendments SET status='APPLIED',applied_at=? WHERE task_id=? AND status='PENDING'", (utc_now(), task["id"]))
                    self._event(op["mission_id"], "task-amendment-applied", {"revision": task["revision"], "before_verification": True}, task_id=task["id"])
                    status = "READY"
                else:
                    status = "VERIFYING"
            elif exit_code == 75:
                status = "BLOCKED"
            elif exit_code == 143 and mission["status"] in {"PAUSED", "STOPPING", "STOPPED"}:
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
            self._event(op["mission_id"], "operation-exited", {"exit_code": int(exit_code), "task_status": status, "logs_path": logs_path, "provider_started": exit_code != 143}, task_id=task["id"], operation_id=operation_id)
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
            if op["status"] != "EXITED" or op["exit_code"] in (None, 0):
                return None
            task = self._task_row(int(op["task_id"]))
            mission = self._mission_row(str(op["mission_id"]))
            if mission["status"] != "RUNNING" or task["status"] not in {"BLOCKED", "FAILED"}:
                return None
            if json.loads(task["pending_json"]) or not self._dependencies_complete(int(task["id"])):
                return None
            failures = int(self.db.execute(
                "SELECT COUNT(*) FROM operations WHERE task_id=? AND status='EXITED' AND exit_code<>0",
                (task["id"],),
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
    ) -> dict[str, Any]:
        if not evidence.strip() and not approved:
            raise TransitionError("completion requires verification evidence or explicit operator approval")
        if source not in {"operator", "verifier"}:
            raise ValueError("verification evidence source must be operator or verifier")
        with self._transaction():
            task = self._task_row(task_id)
            if task["status"] != "VERIFYING":
                raise TransitionError(f"task is not awaiting verification: {task['status']}")
            if task["pending_json"] != "{}":
                raise TransitionError("task has unapplied amendments and cannot be verified")
            if not self._dependencies_complete(task_id):
                raise TransitionError("task dependency is incomplete and prevents verification")
            all_evidence = json.loads(task["evidence_json"])
            if evidence.strip():
                entry = {"text": evidence, "at": utc_now(), "source": source, "revision": int(task["revision"])}
                if metadata:
                    entry.update(metadata)
                all_evidence.append(entry)
            if approved:
                all_evidence.append({"text": "operator approved", "at": utc_now(), "source": "approval", "revision": int(task["revision"])})
            self.db.execute("UPDATE tasks SET status='COMPLETED',evidence_json=?,updated_at=? WHERE id=?", (self._json(all_evidence), utc_now(), task_id))
            self._event(task["mission_id"], "task-verified", {"evidence_count": len(all_evidence), "approved": bool(approved)}, task_id=task_id)
        return self.get_task(task_id)

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
        allowed = {
            "QUEUED": {"READY", "PAUSED", "BLOCKED", "CANCELLED"},
            "READY": {"QUEUED", "RUNNING", "PAUSED", "BLOCKED", "CANCELLED"},
            "RUNNING": {"PAUSED", "BLOCKED", "CANCELLED"},
            "PAUSED": {"QUEUED", "READY", "CANCELLED"},
            "BLOCKED": {"QUEUED", "READY", "CANCELLED"},
            "VERIFYING": {"FAILED", "CANCELLED"},
            "COMPLETED": set(), "FAILED": {"QUEUED", "CANCELLED"}, "CANCELLED": set(),
        }
        with self._transaction():
            task = self._task_row(task_id)
            old = str(task["status"])
            if status != old and status not in allowed[old]:
                raise TransitionError(f"invalid task transition: {old} -> {status}")
            if status == "READY" and not self._dependencies_complete(task_id):
                raise TransitionError("task dependency is incomplete and prevents readiness")
            self.db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, utc_now(), task_id))
            self._event(task["mission_id"], "task-status", {"from": old, "to": status}, task_id=task_id)
        return self.get_task(task_id)

    def events(self, mission_id: str, *, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        self._mission_row(mission_id)
        if limit is not None and not 0 <= limit <= 1000:
            raise ValueError("event page limit must be between 0 and 1000")
        query = "SELECT * FROM events WHERE mission_id=? AND id>? ORDER BY id"
        params: tuple[Any, ...] = (mission_id, after)
        if limit is not None:
            query += " LIMIT ?"
            params += (limit,)
        return [
            {**dict(row), "data": json.loads(row["data_json"])}
            for row in self.db.execute(query, params)
        ]

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

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError(f"mission lock directory must be owned by this user and private: {self.directory}")
        self._file = self.path.open("a+")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            raise

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
