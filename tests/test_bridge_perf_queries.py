"""Indexed query shapes: controller history, task page, home screen, activity paging."""

from __future__ import annotations

import sqlite3
from pathlib import Path


from llm_tools.bridge_store import SCHEMA_VERSION, BridgeStore

OLD_HISTORY = (
    "SELECT id,kind,data_json,created_at FROM events WHERE mission_id=? AND (task_id=? OR operation_id IN "
    "(SELECT id FROM operations WHERE task_id=?)) ORDER BY id DESC LIMIT 20"
)


def _plan(db: sqlite3.Connection, sql: str, params: tuple = ()) -> str:
    return "\n".join(str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + sql, params))


def _add_operation(store: BridgeStore, mission_id: str, task_id: int, operation_id: str) -> None:
    store.db.execute(
        "INSERT INTO operations(id,mission_id,task_id,iteration,station,status,created_at,updated_at) VALUES(?,?,?,1,'codex','EXITED','now','now')",
        (operation_id, mission_id, task_id),
    )


def _event(store: BridgeStore, mission_id: str, kind: str, *, task_id: int | None = None, operation_id: str | None = None) -> None:
    store.db.execute(
        "INSERT INTO events(mission_id,task_id,operation_id,kind,data_json,created_at) VALUES(?,?,?,?,'{}','now')",
        (mission_id, task_id, operation_id, kind),
    )


def test_task_history_matches_the_single_predicate_query_it_replaces(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path, initial_prompt="one")
        first = int(mission["initial_task_id"])
        second = store.add_task(mission["id"], "two")
        _add_operation(store, mission["id"], first, "op-a")
        _add_operation(store, mission["id"], first, "op-b")
        _add_operation(store, mission["id"], second, "op-c")
        with store._transaction():
            for index in range(70):
                _event(store, mission["id"], "heartbeat-noise")
                if index % 3 == 0:
                    _event(store, mission["id"], "task-only", task_id=first)
                if index % 4 == 0:
                    _event(store, mission["id"], "operation-only", operation_id="op-a")
                if index % 5 == 0:
                    _event(store, mission["id"], "both", task_id=first, operation_id="op-b")
                if index % 7 == 0:
                    _event(store, mission["id"], "other-task", task_id=second, operation_id="op-c")
        for task_id in (first, second):
            expected = [tuple(row) for row in store.db.execute(OLD_HISTORY, (mission["id"], task_id, task_id))]
            actual = [tuple(row) for row in store.recent_task_history(mission["id"], task_id)]
            assert actual == expected and len(set(row[0] for row in actual)) == len(actual)
        shorter = store.recent_task_history(mission["id"], first, limit=5)
        assert [row["id"] for row in shorter] == [row[0] for row in store.db.execute(OLD_HISTORY, (mission["id"], first, first))][:5]
        assert store.recent_task_history(mission["id"], 99999) == []


def test_task_history_reads_the_indexes_not_the_event_log(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path, initial_prompt="one")
        statements: list[str] = []
        store.db.set_trace_callback(statements.append)
        store.recent_task_history(mission["id"], int(mission["initial_task_id"]))
        store.db.set_trace_callback(None)
        history = next(s for s in statements if "UNION ALL" in s)
        plan = _plan(store.db, history)  # the trace callback reports the statement with its values bound
        assert "events_task_id" in plan and "events_operation_id" in plan
        assert "SCAN events" not in plan and "events_mission_id" not in plan


def test_schema_seven_database_gains_the_history_indexes_once(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    BridgeStore(path).close()
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX events_task_id")
        db.execute("DROP INDEX events_operation_id")
        db.execute("PRAGMA user_version=7")
    with BridgeStore(path) as store:
        names = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"events_task_id", "events_operation_id"} <= names
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # New event rows keep the event schema number, independent of the database schema number.
        mission = store.create_mission("m", "o", cwd=tmp_path)
        assert store.db.execute("SELECT schema_version FROM events WHERE mission_id=?", (mission["id"],)).fetchone()[0] == 7


def test_controller_prompt_history_uses_the_store_query(tmp_path: Path) -> None:
    from llm_tools.bridge_runtime import BridgeMissionHooks

    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path, initial_prompt="work")
        store.set_mission_status(mission["id"], "RUNNING")
        hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hook.bind(store)
        context = hook.before_iteration()
    assert "task-added" in context["prompt"] or "Durable checkpoint" in context["prompt"]
