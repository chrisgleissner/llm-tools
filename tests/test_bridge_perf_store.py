"""Store open cost: migrations run once, read paths take no write lock, the heartbeat keeps one connection."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from llm_tools import bridge_refresh
from llm_tools.bridge_refresh import CapacityRefreshWorker
from llm_tools.bridge_store import SCHEMA_VERSION, BridgeStore
from tests.conftest import wait_for


def _data_version(db: sqlite3.Connection) -> int:
    """Changes whenever another connection commits a write."""
    return int(db.execute("PRAGMA data_version").fetchone()[0])


def _legacy_database(path: Path, tmp_path: Path) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE missions(id TEXT PRIMARY KEY,name TEXT NOT NULL,objective TEXT NOT NULL,cwd TEXT NOT NULL,repo_identity TEXT NOT NULL,config_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,mission_id TEXT NOT NULL,title TEXT NOT NULL,original_prompt TEXT NOT NULL DEFAULT '',prompt TEXT NOT NULL,pending_json TEXT NOT NULL DEFAULT '{}',priority INTEGER NOT NULL DEFAULT 0,ordering INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'QUEUED',revision INTEGER NOT NULL DEFAULT 1,acceptance_json TEXT NOT NULL DEFAULT '[]',evidence_json TEXT NOT NULL DEFAULT '[]',origin TEXT NOT NULL DEFAULT 'operator',proposal_status TEXT NOT NULL DEFAULT 'NONE',proposed_by_operation TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            PRAGMA user_version=6;
            """
        )
        db.execute("INSERT INTO missions(id,name,objective,cwd,repo_identity,status,created_at,updated_at) VALUES('a','a','o',?,?,'CREATED','now','now')", (str(tmp_path), str(tmp_path)))
        for _ in range(3):
            db.execute("INSERT INTO tasks(mission_id,title,prompt,created_at,updated_at) VALUES('a','t','p','now','now')")


def test_older_database_migrates_once_and_a_current_open_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "private" / "bridge.sqlite3"
    _legacy_database(path, tmp_path)
    watcher = sqlite3.connect(path)  # keeps the WAL file alive between opens
    watcher.execute("PRAGMA journal_mode=WAL")
    try:
        before = _data_version(watcher)
        first = BridgeStore(path)  # migrates
        assert first.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [r["ordinal"] for r in first.db.execute("SELECT ordinal FROM tasks ORDER BY id")] == [1, 2, 3]
        first.close()
        migrated = _data_version(watcher)
        assert migrated != before

        wal = Path(str(path) + "-wal")
        wal_size = wal.stat().st_size
        for _ in range(5):
            with BridgeStore(path) as store:
                assert store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3
            with BridgeStore(path, readonly=True) as reader:
                assert reader.readonly
        assert _data_version(watcher) == migrated
        assert wal.stat().st_size == wal_size
    finally:
        watcher.close()


def test_current_open_runs_no_write_statements(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    BridgeStore(path).close()
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    sqlite3.connect = traced_connect  # type: ignore[assignment]
    try:
        BridgeStore(path).close()
    finally:
        sqlite3.connect = real_connect  # type: ignore[assignment]
    forbidden = ("BEGIN", "UPDATE", "INSERT", "CREATE", "DROP", "ALTER", "user_version=")
    assert not [s for s in statements if any(word in s for word in forbidden)], statements


def test_a_second_process_that_lost_the_migration_race_does_not_migrate_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "bridge.sqlite3"
    BridgeStore(path).close()
    calls: list[int] = []
    monkeypatch.setattr(BridgeStore, "_migrate", lambda self, version: calls.append(version))
    # Pretend the version read before the lock was stale: it is re-read under the lock.
    versions = iter([SCHEMA_VERSION - 1])
    original = BridgeStore._user_version
    monkeypatch.setattr(BridgeStore, "_user_version", lambda self: next(versions, None) or original(self))
    BridgeStore(path).close()
    assert calls == []


def test_newer_schema_is_refused_by_both_open_paths(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    BridgeStore(path).close()
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer"):
        BridgeStore(path)
    with pytest.raises(RuntimeError, match="newer"):
        BridgeStore(path, readonly=True)


def test_readonly_store_reads_but_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    with BridgeStore(path) as store:
        mission = store.create_mission("m", "o", cwd=tmp_path, initial_prompt="do it")
    with BridgeStore(path, readonly=True) as reader:
        assert reader.readonly
        assert reader.get_mission(mission["id"])["name"] == "m"
        assert len(reader.list_tasks(mission["id"])) == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.set_mission_status(mission["id"], "RUNNING")
        with pytest.raises(sqlite3.OperationalError):
            reader.db.execute("UPDATE missions SET name='x'")


def test_readonly_open_creates_a_missing_database_and_migrates_an_old_one(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh" / "bridge.sqlite3"
    with BridgeStore(fresh, readonly=True) as store:
        assert not store.readonly
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    old = tmp_path / "old" / "bridge.sqlite3"
    _legacy_database(old, tmp_path)
    with BridgeStore(old, readonly=True) as store:
        assert not store.readonly
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_heartbeat_worker_keeps_one_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
    opened: list[int] = []
    real = bridge_refresh.BridgeStore

    class Counting(real):  # type: ignore[valid-type, misc]
        def __init__(self, *args, **kwargs) -> None:
            opened.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(bridge_refresh, "BridgeStore", Counting)
    worker = CapacityRefreshWorker(
        mission["id"], database, interval_seconds=5, heartbeat_seconds=1,
        initial_refresh_delay_seconds=0, selector=lambda _m: {"provider": "codex", "observed_at": "now"},
    )
    worker.start()

    def heartbeats() -> int:
        with BridgeStore(database) as store:
            return [event["kind"] for event in store.events(mission["id"])].count("controller-heartbeat")

    wait_for(lambda: heartbeats() >= 3, 15)  # one heartbeat per second
    worker.stop()
    with BridgeStore(database) as store:
        kinds = [event["kind"] for event in store.events(mission["id"])]
    assert kinds.count("controller-heartbeat") >= 3
    assert "capacity-refresh" in kinds
    assert len(opened) == 1
    assert worker._store is None


def test_heartbeat_worker_reopens_after_a_database_error(tmp_path: Path) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
    worker = CapacityRefreshWorker(mission["id"], database, interval_seconds=5, heartbeat_seconds=1, selector=lambda _m: {})
    first = worker._connection()
    first.db.close()  # a dead connection raises sqlite3.ProgrammingError, not sqlite3.Error subclasses of OperationalError
    with pytest.raises(sqlite3.Error):
        worker._record("controller-heartbeat", {"observed_at": "now"})
    assert worker._store is None
    worker._record("controller-heartbeat", {"observed_at": "now"})
    assert worker._store is not None and worker._store is not first
    worker._discard_connection()
