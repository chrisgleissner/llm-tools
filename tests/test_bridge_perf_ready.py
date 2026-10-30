"""ready_tasks(limit=...) and the controller's per-iteration use of it."""

from __future__ import annotations

from pathlib import Path

from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore


def _queue(store: BridgeStore, tmp_path: Path, count: int = 12) -> str:
    mission = store.create_mission("m", "o", cwd=tmp_path, initial_prompt="first")
    first = int(mission["initial_task_id"])
    ids = [first]
    for index in range(1, count):
        ids.append(store.add_task(mission["id"], f"task {index}", priority=index % 3, depends_on=[ids[index - 1]] if index % 5 == 0 else ()))
    return mission["id"]


def test_limit_bounds_the_returned_tasks_but_not_the_promotion(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mid = _queue(store, tmp_path)
        everything = store.ready_tasks(mid)
        assert len(everything) > 3
        # Returned order is unchanged: highest priority first, then queue order.
        assert [t["priority"] for t in everything] == sorted((t["priority"] for t in everything), reverse=True)
        assert [t["id"] for t in store.ready_tasks(mid, limit=1)] == [everything[0]["id"]]
        assert [t["id"] for t in store.ready_tasks(mid, limit=3)] == [t["id"] for t in everything[:3]]
        assert store.ready_tasks(mid, limit=0) == []
        # With a limit, runnable queued tasks are still all marked READY.
        other = tmp_path / "other"
        other.mkdir()
        second = _queue(store, other)
        assert [t["id"] for t in store.ready_tasks(second, limit=1)] != []
        queued = store.db.execute("SELECT COUNT(*) FROM tasks WHERE mission_id=? AND status='QUEUED'", (second,)).fetchone()[0]
        ready = store.db.execute("SELECT COUNT(*) FROM tasks WHERE mission_id=? AND status='READY'", (second,)).fetchone()[0]
        assert (queued, ready) == (2, 10)  # the two tasks that wait on a dependency stay queued


def test_controller_asks_for_one_task_and_builds_one_dictionary(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mid = _queue(store, tmp_path, count=40)
        store.set_mission_status(mid, "RUNNING")
        built: list[int] = []
        original = store._task_dicts
        store._task_dicts = lambda rows: (built.append(len(rows)), original(rows))[1]  # type: ignore[method-assign]
        hook = BridgeMissionHooks(mid, handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hook.bind(store)
        context = hook.before_iteration()
        assert context["task_id"]
        # ready_tasks built one dictionary; begin_iteration / history read single tasks.
        assert max(built) == 1
