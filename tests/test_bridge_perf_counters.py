"""Task page, maintained counters and batched dependency reads."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_store import BridgeStore


def _plan(db: sqlite3.Connection, sql: str) -> str:
    return "\n".join(str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + sql))


def _recomputed(db: sqlite3.Connection, mission_id: str) -> dict:
    effective = "(origin='operator' OR proposal_status='ACCEPTED')"
    status = {r[0]: r[1] for r in db.execute(f"SELECT status,COUNT(*) FROM tasks WHERE mission_id=? AND {effective} GROUP BY status", (mission_id,))}
    proposal = {r[0]: r[1] for r in db.execute("SELECT proposal_status,COUNT(*) FROM tasks WHERE mission_id=? AND origin='agent-proposal' GROUP BY proposal_status", (mission_id,))}
    total, verified = db.execute(
        "SELECT COALESCE(SUM(json_array_length(acceptance_json)),0), COALESCE(SUM(CASE WHEN status='COMPLETED' THEN json_array_length(acceptance_json) ELSE 0 END),0) "
        f"FROM tasks WHERE mission_id=? AND {effective}", (mission_id,)).fetchone()
    count = db.execute("SELECT COUNT(*) FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0]
    return {"status": status, "proposal": proposal, "total": count, "criteria_total": total, "criteria_verified": verified}


def test_maintained_counters_match_a_recount_after_every_kind_of_task_write(tmp_path: Path) -> None:
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path, initial_prompt="first", initial_acceptance=["a", "b"])
        mid = mission["id"]
        assert store.task_counters(mid) == _recomputed(store.db, mid)
        ids = [int(mission["initial_task_id"])]
        for index in range(6):
            ids.append(store.add_task(mid, f"task {index}", acceptance=[f"c{n}" for n in range(index % 3)], depends_on=[ids[-1]] if index % 2 else ()))
        store.db.execute(
            "INSERT INTO operations(id,mission_id,task_id,iteration,station,status,created_at,updated_at) VALUES('op',?,?,1,'codex','EXITED','now','now')",
            (mid, ids[0]),
        )
        proposals = [store.propose_task(mid, f"proposal {n}", source_operation_id="op", acceptance=["p"] * (n + 1)) for n in range(3)]
        assert store.task_counters(mid) == _recomputed(store.db, mid)
        store.decide_proposal(mid, proposals[0], accept=True)
        store.decide_proposal(mid, proposals[1], accept=False)
        store.amend_task(ids[2], acceptance=["x", "y", "z", "w"])
        store.db.execute("UPDATE tasks SET status='COMPLETED' WHERE id IN (?,?)", (ids[0], ids[3]))
        store.db.execute("UPDATE tasks SET status='RUNNING' WHERE id=?", (ids[1],))
        store.db.execute("UPDATE tasks SET acceptance_json='[\"only\"]' WHERE id=?", (ids[0],))
        store.db.execute("UPDATE tasks SET pending_json='{}' WHERE id=?", (ids[4],))  # not a counted column
        store.set_task_status(ids[5], "CANCELLED")
        store.db.execute("UPDATE tasks SET status='COMPLETED' WHERE id=?", (proposals[0],))
        assert store.task_counters(mid) == _recomputed(store.db, mid)
        store.db.execute("DELETE FROM tasks WHERE id=?", (ids[6],))
        assert store.task_counters(mid) == _recomputed(store.db, mid)
        other = store.create_mission("n", "o", cwd=other_dir, initial_prompt="solo", initial_acceptance=["q"])
        assert store.task_counters(other["id"]) == _recomputed(store.db, other["id"])
        assert store.task_counters("missing") == {"status": {}, "proposal": {}, "total": 0, "criteria_total": 0, "criteria_verified": 0}
        before = dict(store.task_counters(mid))
        store.rebuild_task_counts()
        assert store.task_counters(mid) == before


def test_schema_eight_database_is_backfilled_once_and_then_maintained(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    with BridgeStore(path) as store:
        mission = store.create_mission("m", "o", cwd=tmp_path, initial_prompt="first", initial_acceptance=["a", "b"])
        store.add_task(mission["id"], "second", acceptance=["c"])
    with sqlite3.connect(path) as db:
        for name in ("tasks_counts_insert", "tasks_counts_update", "tasks_counts_delete"):
            db.execute(f"DROP TRIGGER {name}")
        db.execute("DROP TABLE mission_task_counts")
        db.execute("DROP INDEX tasks_mission_page")
        db.execute("PRAGMA user_version=8")
    with BridgeStore(path) as store:
        mid = mission["id"]
        assert store.task_counters(mid) == _recomputed(store.db, mid)
        assert store.task_counters(mid)["criteria_total"] == 3
        store.add_task(mid, "third", acceptance=["d"])
        assert store.task_counters(mid) == _recomputed(store.db, mid)
        assert store.task_counters(mid)["total"] == 3


def test_task_page_queries_use_the_page_indexes(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        page = _plan(store.db, "SELECT * FROM tasks WHERE mission_id='m' ORDER BY priority DESC,ordering,id LIMIT 200 OFFSET 0")
        assert "tasks_mission_page" in page and "TEMP B-TREE" not in page
        listing = _plan(store.db, "SELECT * FROM tasks WHERE mission_id='m' ORDER BY ordering,id LIMIT 200 OFFSET 0")
        assert "tasks_mission_ordering" in listing and "TEMP B-TREE" not in listing
        assert "SCAN" not in _plan(store.db, "SELECT scope,key,n FROM mission_task_counts WHERE mission_id='m'")


def test_projection_reads_counters_not_the_task_table(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path, initial_prompt="first", initial_acceptance=["a"])
        for index in range(10):
            store.add_task(mission["id"], f"t{index}", acceptance=["x", "y"])
        store.db.execute("UPDATE tasks SET status='COMPLETED' WHERE ordinal<=4")
        statements: list[str] = []
        store.db.set_trace_callback(statements.append)
        snapshot = project_snapshot(store, mission["id"], task_limit=3)
        store.db.set_trace_callback(None)
        assert snapshot["task_total"] == 11
        assert snapshot["task_counts"] == {"COMPLETED": 4, "QUEUED": 7}
        assert snapshot["criteria"] == {"verified": 7, "total": 21, "complete": False}
        assert not [s for s in statements if "json_array_length" in s or "GROUP BY" in s or "COUNT(*) FROM tasks" in s]


def test_list_tasks_pages_in_list_order_and_batches_dependencies(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path, initial_prompt="first")
        ids = [int(mission["initial_task_id"])]
        for index in range(1, 30):
            ids.append(store.add_task(mission["id"], f"t{index}", depends_on=[ids[0], ids[-1]] if index > 2 else ()))
        everything = store.list_tasks(mission["id"])
        assert [t["id"] for t in everything] == ids
        assert [t["id"] for t in store.list_tasks(mission["id"], limit=5, offset=10)] == ids[10:15]
        assert [t["id"] for t in store.list_tasks(mission["id"], offset=27)] == ids[27:]
        assert store.list_tasks(mission["id"], limit=0) == []
        by_id = {t["id"]: t for t in everything}
        assert by_id[ids[5]]["depends_on"] == sorted([ids[0], ids[4]])
        assert by_id[ids[5]]["depends_on_ordinals"] == [1, 5]
        statements: list[str] = []
        store.db.set_trace_callback(statements.append)
        store.list_tasks(mission["id"])
        store.db.set_trace_callback(None)
        assert len([s for s in statements if "task_dependencies" in s]) == 1


def test_task_list_command_defaults_to_one_page_and_all_lists_everything(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    from llm_tools import bridge

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(mode=0o700)
    with BridgeStore(tmp_path / "state" / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path, initial_prompt="first")
        with store._transaction():
            for index in range(1, 230):
                store.add_task(mission["id"], f"t{index}")
    assert bridge.main(["task", "list", "--mission", mission["id"], "--json"]) == 0
    out = capsys.readouterr()
    assert len(json.loads(out.out)) == 200 and "listed 200 of 230 tasks" in out.err
    assert bridge.main(["task", "list", "--mission", mission["id"], "--all", "--json"]) == 0
    out = capsys.readouterr()
    assert len(json.loads(out.out)) == 230 and out.err == ""
    assert bridge.main(["task", "list", "--mission", mission["id"], "--limit", "5", "--offset", "225", "--json"]) == 0
    assert [t["ordinal"] for t in json.loads(capsys.readouterr().out)] == [226, 227, 228, 229, 230]
    assert bridge.main(["task", "list", "--mission", mission["id"]]) == 0
    text = capsys.readouterr().out
    assert "showing 200 of 230 tasks" in text and "--all" in text
    assert bridge.main(["task", "list", "--mission", mission["id"], "--offset", "500"]) == 0
    assert "No tasks at position 501; the mission has 230" in capsys.readouterr().out
