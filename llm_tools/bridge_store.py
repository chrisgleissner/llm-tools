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
SCHEMA_VERSION = 3


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
            CREATE INDEX IF NOT EXISTS events_mission_id ON events(mission_id, id);
            CREATE INDEX IF NOT EXISTS tasks_mission_state_order ON tasks(mission_id, status, priority DESC, ordering, id);
            CREATE INDEX IF NOT EXISTS commands_mission_queue ON commands(mission_id,status,created_at);
            COMMIT;
            """
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_column("missions", "config_json", "TEXT NOT NULL DEFAULT '{}'" )
            self._ensure_column("tasks", "original_prompt", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("operations", "route_id", "TEXT")
            self._ensure_column("operations", "model", "TEXT")
            self._ensure_column("operations", "pane_id", "TEXT")
            self._ensure_column("operations", "process_pid", "INTEGER")
            self._ensure_column("operations", "process_start", "TEXT")
            self._ensure_column("events", "schema_version", f"INTEGER NOT NULL DEFAULT {SCHEMA_VERSION}")
            self.db.execute("UPDATE tasks SET original_prompt=prompt WHERE original_prompt=''")
            for mission in self.db.execute("SELECT id,cwd FROM missions").fetchall():
                self.db.execute("UPDATE missions SET repo_identity=? WHERE id=?", (repository_identity(mission["cwd"]), mission["id"]))
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
                counts = self.db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS completed FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()
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
            self._mission_row(mission_id)
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

    def ready_tasks(self, mission_id: str) -> list[dict[str, Any]]:
        """Return runnable tasks in priority-descending, stable queue order."""
        with self._transaction():
            self._mission_row(mission_id)
            ready = self.db.execute(
                "SELECT t.id FROM tasks t WHERE t.mission_id=? AND t.status='QUEUED' AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=t.id AND p.status!='COMPLETED')",
                (mission_id,),
            ).fetchall()
            for row in ready:
                self.db.execute("UPDATE tasks SET status='READY',updated_at=? WHERE id=?", (utc_now(), row["id"]))
                self._event(mission_id, "task-ready", {"from": "QUEUED", "to": "READY"}, task_id=row["id"])
            rows = self.db.execute(
                "SELECT * FROM tasks t WHERE mission_id=? AND status='READY' AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=t.id AND p.status!='COMPLETED') ORDER BY priority DESC,ordering,id",
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
        return [self.get_command(row[0]) for row in self.db.execute("SELECT id FROM commands WHERE mission_id=? AND status='QUEUED' ORDER BY created_at,id", (mission_id,))]

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
            now = utc_now()
            self.db.execute(
                "UPDATE operations SET status='RUNNING',pane_id=?,process_pid=?,process_start=?,route_id=?,model=?,updated_at=? WHERE id=?",
                (pane_id or None, process_pid, process_start or None, route_id or None, model or None, now, operation_id),
            )
            self._event(op["mission_id"], "operation-running", {"pane_id": pane_id, "process_pid": process_pid, "route_id": route_id, "model": model}, task_id=op["task_id"], operation_id=operation_id)

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
            else:
                status = "FAILED"
            if boundary_status is not None:
                status = boundary_status
            now = utc_now()
            self.db.execute("UPDATE operations SET status='EXITED',exit_code=?,logs_path=?,updated_at=? WHERE id=?", (int(exit_code), logs_path, now, operation_id))
            self.db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, now, task["id"]))
            self._event(op["mission_id"], "operation-exited", {"exit_code": int(exit_code), "task_status": status, "logs_path": logs_path}, task_id=task["id"], operation_id=operation_id)
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
            "COMPLETED": set(), "FAILED": {"QUEUED"}, "CANCELLED": set(),
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

    def events(self, mission_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        self._mission_row(mission_id)
        return [
            {**dict(row), "data": json.loads(row["data_json"])}
            for row in self.db.execute("SELECT * FROM events WHERE mission_id=? AND id>? ORDER BY id", (mission_id, after))
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
