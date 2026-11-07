"""The status projection that travels over the worker link is bounded (DOG-021).

``mission.status`` used to return the whole projection: up to 200 complete task records with both
prompt fields, two copies of the newest events and the frozen tool configuration. A mission with a
few dozen tasks produced results of 125 KB or more, and the console asked for one every few seconds.
"""

from __future__ import annotations

import json

import pytest

from llm_tools import bridge_commands as commands
from llm_tools import bridge_worker as worker
from llm_tools.bridge import main
from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_status_bound import STATUS_BYTE_LIMIT, bound_status
from llm_tools.bridge_store import BridgeStore
from tests.test_bridge_worker import TOKEN

PROMPT = "Implement the thing with all the details. " * 120  # about 5 KB


@pytest.fixture
def big_mission(tmp_path, monkeypatch, capsys):
    """A running mission with 40 tasks of 5 KB prompts (the first 36 finished) and 300 long events."""
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    assert main(["mission", "create", "--prompt", PROMPT, "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    with BridgeStore(directory / "bridge.sqlite3") as store:
        for index in range(39):
            store.add_task(mission_id, PROMPT, title=f"Task number {index + 2}")
        store.set_mission_status(mission_id, "RUNNING")
        ids = [row["id"] for row in store.list_tasks(mission_id, limit=100)]
        store.db.execute(f"UPDATE tasks SET status='COMPLETED' WHERE id IN ({','.join(str(i) for i in ids[:36])})")
        for index in range(300):
            store.record_runtime_event(mission_id, "provider-wait", {"note": "waiting for a provider " * 20, "i": index})
    return directory, mission_id


def _status(mission_id, **params):
    result = commands.execute(CommandRequest("mission.status", params, mission_id=mission_id))
    assert result.error is None, result.error
    return result.result


def test_status_result_is_a_few_kilobytes_without_prompts_or_event_bodies(big_mission):
    _, mission_id = big_mission
    full = _status_full(mission_id)
    assert len(json.dumps(full)) > 100_000  # the projection this replaces
    status = _status(mission_id)
    encoded = json.dumps(status)
    assert len(encoded) < 8 * 1024
    assert PROMPT[:300] not in encoded  # the objective is clipped to 240 characters and no task carries a prompt
    assert all("prompt" not in task and "original_prompt" not in task for task in status["tasks"])
    assert status["events"] == [] and status["operator_events"] == []
    assert status["task_total"] == 40 and status["task_counts"] == {"COMPLETED": 36, "QUEUED": 4}
    assert "frozen_tool_config" not in status["mission"]["config"]
    assert len(status["mission"]["objective"]) <= 240


def test_status_lists_unfinished_tasks_first_so_the_next_task_is_never_cut_off(big_mission):
    _, mission_id = big_mission
    status = _status(mission_id)
    assert [task["status"] for task in status["tasks"]] == ["QUEUED"] * 4 + ["COMPLETED"] * 4
    assert status["task_page"] == {"limit": 8, "offset": 0, "returned": 8, "search": ""}
    assert len(_status(mission_id, task_limit=2)["tasks"]) == 2


def test_status_returns_prompt_text_only_on_request_and_only_a_prefix(big_mission):
    _, mission_id = big_mission
    status = _status(mission_id, prompt_chars=50, task_limit=1)
    assert status["tasks"][0]["prompt"] == PROMPT[:50]
    with_events = _status(mission_id, event_limit=3)
    assert len(with_events["events"]) == 3 and with_events["event_page"]["returned"] == 3
    for bad in ({"prompt_chars": 5000}, {"task_limit": -1}, {"event_limit": -1}):
        result = commands.execute(CommandRequest("mission.status", bad, mission_id=mission_id))
        assert result.outcome == "rejected" and result.error.code == "invalid-argument"


def test_task_list_clips_prompts_when_asked_and_task_show_returns_the_whole_prompt(big_mission):
    _, mission_id = big_mission
    listed = commands.execute(CommandRequest("task.list", {"limit": 3, "prompt_chars": 20}, mission_id=mission_id)).result["tasks"]
    assert [len(task["prompt"]) for task in listed] == [20, 20, 20]
    unclipped = commands.execute(CommandRequest("task.list", {"limit": 3}, mission_id=mission_id)).result["tasks"]
    assert len(unclipped[0]["prompt"]) == len(PROMPT)
    shown = commands.execute(CommandRequest("task.show", {"task_id": unclipped[0]["id"]}, mission_id=mission_id)).result
    assert shown["prompt"] == PROMPT


def test_status_stays_under_the_byte_limit_whatever_the_projection_holds():
    snapshot = {"mission": {"id": "m", "config": {}}, "tasks": [], "capacity": {"wait": [{"note": "x" * 5000} for _ in range(100)]},
                "waiting": {"entries": [{"reason": "y" * 5000} for _ in range(100)]}}
    status = bound_status(snapshot)
    assert len(json.dumps(status)) <= STATUS_BYTE_LIMIT + 1024
    assert status["truncated"] is True and status["bounded"] is True


def test_remote_status_command_still_formats_with_the_bounded_result(big_mission, capsys):
    from llm_tools import bridge_format as fmt

    _, mission_id = big_mission
    text = fmt.format_status(_status(mission_id))
    assert "Tasks       40 total" in text and 'Next        task 37 "Task number 37"' in text


def test_mission_summary_carries_counts_and_current_task_for_the_console(big_mission):
    directory, mission_id = big_mission
    config = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=False)
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="t", save=lambda _c: None)
    with BridgeStore(directory / "bridge.sqlite3") as store:
        summary = session._mission_summary(store, store.get_mission(mission_id))
    assert summary["task_counts"] == {"COMPLETED": 36, "QUEUED": 4}
    assert len(json.dumps(summary)) < 1024
    with BridgeStore(directory / "bridge.sqlite3") as store:
        store.set_mission_status(mission_id, "STOPPED")
        store.set_mission_status(mission_id, "CANCELLED")
        ended = session._mission_summary(store, store.get_mission(mission_id))
    assert ended["task_counts"] == {"COMPLETED": 36, "QUEUED": 4} and "current_task" not in ended


def _status_full(mission_id):
    from llm_tools.bridge import status_snapshot

    return status_snapshot(mission_id)
