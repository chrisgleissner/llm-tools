"""The projection parameter object and the Snapshot type describe what the monitor really returns."""

from __future__ import annotations

from pathlib import Path

from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_projection import ProjectionParams, Snapshot
from llm_tools.bridge_store import BridgeStore


def test_params_round_trip_through_the_keyword_form() -> None:
    params = ProjectionParams(task_limit=5, task_offset=10, search="x", event_offset=20, event_anchor=7, prompt_chars=99)
    assert params.as_kwargs() == {"task_limit": 5, "task_offset": 10, "search": "x", "event_offset": 20, "event_anchor": 7, "prompt_chars": 99}
    assert ProjectionParams(**params.as_kwargs()) == params
    assert ProjectionParams().as_kwargs() == {
        "task_limit": None, "task_offset": 0, "search": "", "event_offset": 0, "event_anchor": None, "prompt_chars": None,
    }


def test_params_and_keywords_give_the_same_projection(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("projection", "objective", cwd=tmp_path)
        for index in range(3):
            store.add_task(mission["id"], f"prompt {index}", title=f"task {index}")
        params = ProjectionParams(task_limit=2, task_offset=1, search="task", prompt_chars=4)
        by_params = project_snapshot(store, mission["id"], params=params)
        by_keywords = project_snapshot(store, mission["id"], **params.as_kwargs())
        assert by_params["tasks"] == by_keywords["tasks"] and len(by_params["tasks"]) == 2
        assert by_params["task_page"] == {"limit": 2, "offset": 1, "returned": 2, "search": "task"}


def test_snapshot_type_lists_every_key_the_projection_returns(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("projection", "objective", cwd=tmp_path)
        snapshot = project_snapshot(store, mission["id"])
    assert set(snapshot) <= set(Snapshot.__annotations__)
    assert {"mission", "tasks", "lifecycle", "hold", "needs_you"} <= set(snapshot)
