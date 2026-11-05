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


def test_suspend_allowed_requires_opt_in_running_boundary_and_no_operation(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_power

    # Do not read the host's real sleep inhibitors: another process holding one
    # (for example device work) would make this test fail for an unrelated reason.
    monkeypatch.setattr(bridge_power, "_os_sleep_blocker", lambda: "")
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("suspend", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)

    assert hooks.suspend_allowed() is False
    store.set_mission_config_value(mission["id"], "suspend_when_rate_limited", True, event_kind="test")
    assert hooks.suspend_allowed() is True
    store.begin_operation(task_id, iteration=1, station="codex")
    assert hooks.suspend_allowed() is False


def test_suspend_allowed_uses_global_operation_and_os_sleep_blocker_gate(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_power, common

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission(
        "suspend owner", "objective", cwd=tmp_path,
        config={"suspend_when_rate_limited": True},
    )
    store.set_mission_status(mission["id"], "RUNNING")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    monkeypatch.setattr(common, "have_cmd", lambda name: name == "systemd-inhibit")
    header = "WHO                          UID  USER  PID    COMM            WHAT                                                                       WHY                                                        MODE"
    blocked = False

    def inhibitor_list(*_args, **_kwargs):
        row = "Device Writer".ljust(29) + "1000".ljust(5) + "chris".ljust(6) + "20400".ljust(7) + "firmware".ljust(16) + "sleep".ljust(75) + "Device operation in progress".ljust(59) + "block"
        return type("Proc", (), {
            "returncode": 0,
            "stdout": f"{header}\n{row}" if blocked else header,
            "stderr": "",
        })()

    monkeypatch.setattr(bridge_power.subprocess, "run", inhibitor_list)
    assert hooks.suspend_allowed() is True

    other_cwd = tmp_path.parent / "different-worktree"
    other_cwd.mkdir()
    other = store.create_mission("other mission", "objective", cwd=other_cwd)
    task_id = store.add_task(other["id"], "device work")
    store.set_mission_status(other["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    assert hooks.suspend_allowed() is False
    store.db.execute("UPDATE operations SET status='EXITED', exit_code=0 WHERE id=?", (operation_id,))

    blocked = True
    assert hooks.suspend_allowed() is False
    blocked = False
    assert hooks.suspend_allowed() is True
