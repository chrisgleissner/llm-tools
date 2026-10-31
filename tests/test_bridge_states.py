"""Contract tests for the Bridge state vocabulary shared by the store and every view."""

from __future__ import annotations

import pytest

from llm_tools import bridge_states as states
from llm_tools.bridge_glyphs import STATE_LOOK
from llm_tools.bridge_store import BridgeStore, TransitionError


def test_transition_tables_cover_every_state_and_only_target_declared_states() -> None:
    assert set(states.MISSION_TRANSITIONS) == states.MISSION_STATES
    assert set(states.TASK_TRANSITIONS) == states.TASK_STATES
    for table, declared in ((states.MISSION_TRANSITIONS, states.MISSION_STATES), (states.TASK_TRANSITIONS, states.TASK_STATES)):
        for source, targets in table.items():
            assert source not in targets
            assert targets <= declared


def test_every_store_state_has_a_glyph_and_role() -> None:
    declared = states.MISSION_STATES | states.TASK_STATES | states.ACTIVE_OPERATION_STATUSES
    assert declared <= set(STATE_LOOK)


def test_named_status_sets_are_consistent() -> None:
    assert states.ACTIVE_OPERATION_STATUSES <= states.OPERATION_STATES
    assert states.FINISHED_MISSION_STATUSES < states.ENDED_MISSION_STATUSES < states.STATION_CLOSING_MISSION_STATUSES
    assert states.STATION_CLOSING_MISSION_STATUSES <= states.MISSION_STATES
    assert states.LIVE_MISSION_STATUSES.isdisjoint(states.ENDED_MISSION_STATUSES)
    assert states.MISSION_STATES == states.LIVE_MISSION_STATUSES | states.ENDED_MISSION_STATUSES | {"STOPPED"}
    assert states.CONTROLLED_MISSION_STATUSES | {"CREATED"} == states.LIVE_MISSION_STATUSES
    assert states.STARTED_MISSION_STATUSES < states.CONTROLLED_MISSION_STATUSES
    assert states.HALTED_MISSION_STATUSES <= states.MISSION_STATES
    assert states.FINISHED_TASK_STATUSES <= states.TASK_STATES
    for finished in states.FINISHED_MISSION_STATUSES:
        assert states.MISSION_TRANSITIONS[finished] == frozenset()
    for finished in states.FINISHED_TASK_STATUSES:
        assert states.TASK_TRANSITIONS[finished] == frozenset()


def _all_pairs(declared: frozenset[str]) -> list[tuple[str, str]]:
    return [(old, new) for old in sorted(declared) for new in sorted(declared) if old != new]


@pytest.mark.parametrize(("old", "new"), _all_pairs(states.MISSION_STATES))
def test_store_applies_the_mission_transition_table(tmp_path, old: str, new: str) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("transitions", "objective", cwd=tmp_path)
    with store._transaction():
        store.db.execute("UPDATE missions SET status=? WHERE id=?", (old, mission["id"]))
    if new in states.MISSION_TRANSITIONS[old] and new != "COMPLETED":
        store.set_mission_status(mission["id"], new)
        assert store.get_mission(mission["id"])["status"] == new
    else:
        with pytest.raises(TransitionError):
            store.set_mission_status(mission["id"], new)
        assert store.get_mission(mission["id"])["status"] == old


@pytest.mark.parametrize(("old", "new"), _all_pairs(states.TASK_STATES))
def test_store_applies_the_task_transition_table(tmp_path, old: str, new: str) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("transitions", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    with store._transaction():
        store.db.execute("UPDATE tasks SET status=? WHERE id=?", (old, task_id))
    if new in states.TASK_TRANSITIONS[old]:
        store.set_task_status(task_id, new)
        assert store.get_task(task_id)["status"] == new
    else:
        with pytest.raises(TransitionError):
            store.set_task_status(task_id, new)
        assert store.get_task(task_id)["status"] == old
