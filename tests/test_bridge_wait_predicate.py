from __future__ import annotations

import pytest

from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore


def _reference_should_interrupt_wait(store: BridgeStore, mission_id: int, task_id: int) -> bool:
    """The behaviour before wait_interrupted was shared: mission boundaries, then the task's own status."""
    mission = store.get_mission(mission_id)
    if mission["status"] in {"PAUSED", "STOPPING", "STOPPED", "CANCELLED", "COMPLETED", "FAILED"}:
        return True
    if mission["config"].get("pause_after_iteration"):
        return True
    stop_after = mission["config"].get("stop_after_task")
    if stop_after is not None and store.get_task(int(stop_after))["status"] == "COMPLETED":
        return True
    return store.get_task(task_id)["status"] in {"PAUSED", "CANCELLED"}


@pytest.mark.parametrize("pause_flag", [False, True])
@pytest.mark.parametrize("stop_after_completed", [None, False, True])
@pytest.mark.parametrize("task_status", ["READY", "RUNNING", "PAUSED", "CANCELLED"])
@pytest.mark.parametrize(
    "mission_status", ["CREATED", "RUNNING", "PAUSED", "STOPPING", "STOPPED", "CANCELLED", "COMPLETED", "FAILED"]
)
def test_should_interrupt_wait_matches_reference_over_mission_task_and_boundary_states(
    tmp_path, mission_status, task_status, stop_after_completed, pause_flag
) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("equivalence", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    other = store.add_task(mission["id"], "stop after this")
    with store._transaction():
        store.db.execute("UPDATE tasks SET status=? WHERE id=?", (task_status, task_id))
        store.db.execute("UPDATE missions SET status=? WHERE id=?", (mission_status, mission["id"]))
        if stop_after_completed is not None:
            store.db.execute(
                "UPDATE tasks SET status=? WHERE id=?", ("COMPLETED" if stop_after_completed else "READY", other)
            )
    if stop_after_completed is not None:
        store.set_mission_config_value(mission["id"], "stop_after_task", other, event_kind="test")
    if pause_flag:
        store.set_mission_config_value(mission["id"], "pause_after_iteration", True, event_kind="test")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    expected = _reference_should_interrupt_wait(store, mission["id"], task_id)
    assert hooks._should_interrupt_wait(task_id) is expected
    if hooks.wait_interrupted():
        assert hooks._should_interrupt_wait(task_id) is True
