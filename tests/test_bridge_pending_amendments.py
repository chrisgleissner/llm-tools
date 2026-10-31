"""Every path that applies a task's pending amendment leaves the same rows and events.

The golden file was recorded from the store before the five application paths
(`amend_task` with no active step, `amend_task` while verifying, `begin_iteration`,
`begin_operation` and `finish_operation` with exit code 0) shared one helper.
Each scenario below drives one path and compares the task row, the amendment
rows and the amendment events with that recording.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from llm_tools.bridge_store import BridgeStore

GOLDEN = json.loads((Path(__file__).parent / "golden" / "bridge_pending_amendments.json").read_text(encoding="utf-8"))


def _dump(store: BridgeStore, mission_id: str, task_id: int) -> dict[str, Any]:
    task = store.get_task(task_id)
    amendments = [
        [row["revision"], row["status"], row["applied_at"] is not None, json.loads(row["changes_json"])]
        for row in store.db.execute("SELECT * FROM amendments WHERE task_id=? ORDER BY id", (task_id,))
    ]
    events = [[event["kind"], event["data"]] for event in store.events(mission_id) if event["kind"].startswith("task-amend")]
    keys = ("status", "prompt", "title", "priority", "acceptance", "revision", "pending", "evidence")
    return json.loads(json.dumps({"task": {key: task[key] for key in keys}, "amendments": amendments, "events": events}))


def _fresh(tmp_path: Path) -> tuple[BridgeStore, str, int]:
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "original", title="t0", priority=1, acceptance=["a"])
    store.set_mission_status(mission["id"], "RUNNING")
    return store, mission["id"], task_id


Dumps = dict[str, dict[str, Any]]


def _amend_without_active_step(store: BridgeStore, m: str, t: int) -> Dumps:
    store.amend_task(t, prompt="new prompt", priority=5, acceptance=["x", "y"])
    return {"amend_no_active_step": _dump(store, m, t)}


def _amend_while_running_then_exit_zero(store: BridgeStore, m: str, t: int) -> Dumps:
    op = store.begin_operation(t, iteration=1, station="codex")
    store.amend_task(t, title="during run", prompt="p2")
    during = _dump(store, m, t)
    store.finish_operation(op, exit_code=0)
    return {"amend_while_running_pending": during, "finish_exit0_with_pending": _dump(store, m, t)}


def _amend_while_verifying(store: BridgeStore, m: str, t: int) -> Dumps:
    op = store.begin_operation(t, iteration=1, station="codex")
    store.finish_operation(op, exit_code=0)
    before = _dump(store, m, t)
    store.amend_task(t, prompt="after verify", acceptance=["z"])
    return {"verifying_before_amend": before, "amend_verifying": _dump(store, m, t)}


def _blocked_then_begin_iteration(store: BridgeStore, m: str, t: int) -> Dumps:
    op = store.begin_operation(t, iteration=1, station="codex")
    store.amend_task(t, title="late", priority=9)
    store.finish_operation(op, exit_code=75)
    blocked = _dump(store, m, t)
    store.set_task_status(t, "READY")
    store.begin_iteration(t)
    return {"finish_blocked_with_pending": blocked, "begin_iteration": _dump(store, m, t)}


def _failed_run_keeps_pending(store: BridgeStore, m: str, t: int) -> Dumps:
    op = store.begin_operation(t, iteration=1, station="codex")
    store.amend_task(t, prompt="carried")
    store.finish_operation(op, exit_code=1)
    return {"finish_failed_with_pending": _dump(store, m, t)}


def _blocked_then_begin_operation(store: BridgeStore, m: str, t: int) -> Dumps:
    op = store.begin_operation(t, iteration=1, station="codex")
    store.amend_task(t, title="late2", priority=7)
    store.finish_operation(op, exit_code=75)
    store.set_task_status(t, "READY")
    store.begin_operation(t, iteration=2, station="codex")
    return {"begin_operation_applies_pending": _dump(store, m, t)}


SCENARIOS: list[Callable[[BridgeStore, str, int], Dumps]] = [
    _amend_without_active_step,
    _amend_while_running_then_exit_zero,
    _amend_while_verifying,
    _blocked_then_begin_iteration,
    _failed_run_keeps_pending,
    _blocked_then_begin_operation,
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda fn: fn.__name__.lstrip("_"))
def test_pending_amendment_paths_match_the_recorded_rows_and_events(tmp_path, scenario) -> None:
    store, mission_id, task_id = _fresh(tmp_path)
    for name, observed in scenario(store, mission_id, task_id).items():
        assert observed == GOLDEN[name], name


def test_every_recorded_state_is_compared(tmp_path) -> None:
    compared: set[str] = set()
    for index, scenario in enumerate(SCENARIOS):
        store, mission_id, task_id = _fresh(tmp_path / str(index))
        compared |= set(scenario(store, mission_id, task_id))
    assert compared == set(GOLDEN)
