"""Home screen and mission list queries read indexes, not whole tables."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from llm_tools import bridge_launcher, bridge_switcher
from llm_tools.bridge_store import SCHEMA_VERSION, BridgeStore


def _populate(store: BridgeStore, tmp_path: Path) -> list[str]:
    ids = []
    for index in range(3):
        folder = tmp_path / f"w{index}"
        folder.mkdir()
        mission = store.create_mission(f"mission {index}", "objective", cwd=folder, initial_prompt="first")
        mid = mission["id"]
        ids.append(mid)
        tasks = [int(mission["initial_task_id"])] + [store.add_task(mid, f"task {n}", priority=n) for n in range(1, 6)]
        store.db.execute("UPDATE tasks SET status='COMPLETED' WHERE id IN (?,?)", (tasks[0], tasks[1]))
        if index != 2:
            store.db.execute("UPDATE tasks SET status='RUNNING' WHERE id=?", (tasks[2],))
            store.db.execute("UPDATE tasks SET status='READY' WHERE id=?", (tasks[3],))
            for count, (station, route, created) in enumerate((("claude", None, "2026-01-01T00:00:00"), ("codex", "route-x" if index == 0 else "", "2026-01-02T00:00:00"))):
                store.db.execute(
                    "INSERT INTO operations(id,mission_id,task_id,iteration,station,route_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?, 'EXITED',?,?)",
                    (f"op-{index}-{count}", mid, tasks[count], count + 1, station, route, created, created),
                )
    return ids


def _selects(statements: list[str]) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith("SELECT") and ("FROM tasks" in s or "FROM operations" in s or "FROM missions m" in s)]


def _full_scans(db: sqlite3.Connection, sql: str) -> list[str]:
    plan = [str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + sql)]
    return [row for row in plan if re.fullmatch(r"SCAN (tasks|operations|o|t)( \w+)?", row)]


def test_home_and_mission_list_queries_scan_no_task_or_operation_table(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        _populate(store, tmp_path)
        statements: list[str] = []
        store.db.set_trace_callback(statements.append)
        bridge_launcher._read_home(store.db, str(tmp_path))
        bridge_switcher.collect_entries(store, station_probe=lambda _m: False, controller_probe=lambda _m: False)
        store.db.set_trace_callback(None)
        queries = _selects(statements)
        assert len(queries) >= 3
        for sql in queries:
            assert _full_scans(store.db, sql) == [], sql
        current = next(s for s in queries if "'VERIFYING'" in s)
        plan = "\n".join(str(row[-1]) for row in store.db.execute("EXPLAIN QUERY PLAN " + current))
        assert "tasks_current" in plan
        latest = next(s for s in queries if "ORDER BY o.created_at DESC" in s)
        assert "operations_mission_recent" in "\n".join(str(row[-1]) for row in store.db.execute("EXPLAIN QUERY PLAN " + latest))


def test_home_stats_and_switcher_entries_match_what_the_full_scans_reported(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        ids = _populate(store, tmp_path)
        _missions, stats = bridge_launcher._read_home(store.db, str(tmp_path))
        assert (stats[ids[0]].done, stats[ids[0]].total) == (2, 6)
        assert stats[ids[0]].route == "route-x"  # the route id wins over the station
        assert stats[ids[1]].route == "codex"
        assert stats[ids[2]].route == ""  # a mission without operations
        entries = {e.mission_id: e for e in bridge_switcher.collect_entries(store, station_probe=lambda _m: False, controller_probe=lambda _m: False)}
        assert entries[ids[0]].provider == "route-x" and entries[ids[1]].provider == "codex"
        assert entries[ids[0]].task_status == "RUNNING" and entries[ids[2]].task_status == ""


def test_home_stats_fall_back_to_task_counts_on_a_database_without_counters(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        ids = _populate(store, tmp_path)
        store.db.execute("DROP TABLE mission_task_counts")
        _missions, stats = bridge_launcher._read_home(store.db, str(tmp_path))
        assert (stats[ids[0]].done, stats[ids[0]].total) == (2, 6)


def test_schema_nine_database_gains_the_home_indexes(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    BridgeStore(path).close()
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX tasks_current")
        db.execute("DROP INDEX operations_mission_recent")
        db.execute("PRAGMA user_version=9")
    with BridgeStore(path) as store:
        names = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"tasks_current", "operations_mission_recent"} <= names
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
