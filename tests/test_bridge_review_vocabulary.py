"""CLIUX-006: one user-facing name (REVIEW) for a task that awaits the operator.

The stored status stays VERIFYING (database, JSON ``status``, protocol); every screen and CLI
table shows REVIEW, the mission shows NEEDS YOU, and the CLI verb pairing with ``task reject`` is
``task approve``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_agents, bridge_format, bridge_missions, bridge_states, bridge_tui, bridge_vocab
from llm_tools.bridge import dispatch_console_command, main
from llm_tools.bridge_glyphs import state_text
from llm_tools.bridge_store import BridgeStore


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    return directory


def _run(capsys, *argv: str, code: int = 0) -> str:
    assert main(list(argv)) == code, capsys.readouterr()
    captured = capsys.readouterr()
    return captured.out if code == 0 else captured.err


def _mission_in_review(tmp_path, state, capsys) -> tuple[str, int]:
    folder = tmp_path / "work"
    folder.mkdir()
    created = json.loads(_run(capsys, "mission", "create", "--prompt", "work objective", "--name", "work", "--cwd", str(folder), "--providers", "codex", "--json"))
    mid, task_id = created["mission"]["id"], created["task"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
        assert store.get_task(task_id)["status"] == "VERIFYING"
    return mid, task_id


def _console(mission_id: str, snapshot: dict) -> bridge_tui._ConsoleState:
    console = bridge_tui._ConsoleState(mission_id=mission_id, dispatch=lambda _command: "", snapshot_reader=lambda *args, **kwargs: snapshot, full=True)
    console.snapshot = snapshot
    return console


def test_status_label_maps_only_the_awaiting_status() -> None:
    assert bridge_vocab.status_label("VERIFYING") == "REVIEW"
    assert bridge_vocab.status_label("verifying") == "REVIEW"
    assert bridge_vocab.status_label("RUNNING") == "RUNNING"
    assert state_text("VERIFYING", unicode=True) == "◆ REVIEW"


def test_task_json_keeps_the_enum_and_adds_the_label(tmp_path, state, capsys) -> None:
    mid, _task = _mission_in_review(tmp_path, state, capsys)
    task = json.loads(_run(capsys, "task", "show", "--mission", mid, "--task", "1", "--json"))
    assert task["status"] == "VERIFYING" and task["status_label"] == "REVIEW"
    listed = json.loads(_run(capsys, "task", "list", "--mission", mid, "--json"))
    assert listed[0]["status"] == "VERIFYING" and listed[0]["status_label"] == "REVIEW"
    snapshot = json.loads(_run(capsys, "status", "--mission", mid, "--json"))
    assert snapshot["current_task"]["status"] == "VERIFYING" and snapshot["current_task"]["status_label"] == "REVIEW"


def test_cli_text_never_shows_verifying(tmp_path, state, capsys) -> None:
    mid, _task = _mission_in_review(tmp_path, state, capsys)
    listing = _run(capsys, "task", "list", "--mission", mid)
    assert "REVIEW" in listing and "VERIFYING" not in listing
    shown = _run(capsys, "task", "show", "--mission", mid, "--task", "1")
    assert "REVIEW" in shown and "VERIFYING" not in shown
    status = _run(capsys, "status", "--mission", mid)
    assert "1 REVIEW" in status and "VERIFYING" not in status
    evidence = _run(capsys, "task", "verify", "--mission", mid, "--task", "1", "--evidence", "tests passed")
    assert "VERIFYING" not in evidence and "llm-bridge task approve --task 1" in evidence


def test_task_approve_pairs_with_task_reject(tmp_path, state, capsys) -> None:
    mid, _task = _mission_in_review(tmp_path, state, capsys)
    out = _run(capsys, "task", "approve", "--mission", mid, "--task", "1", "--evidence", "reviewed")
    assert out.startswith('Approved · task 1 "work objective" · COMPLETED')
    shown = _run(capsys, "task", "show", "--mission", mid, "--task", "1")
    assert "  - reviewed (operator" in shown and "  - operator approved" in shown
    again = _run(capsys, "task", "approve", "--mission", mid, "--task", "1", code=2)
    assert "task 1 is completed, not awaiting verification" in again


def test_task_verify_approve_stays_as_a_hidden_alias(tmp_path, state, capsys) -> None:
    mid, _task = _mission_in_review(tmp_path, state, capsys)
    out = _run(capsys, "task", "verify", "--mission", mid, "--task", "1", "--approve")
    assert out.startswith('Verified · task 1 "work objective" · COMPLETED')
    parser = bridge._build_parser(default_mission=None)
    with pytest.raises(SystemExit):
        parser.parse_args(["task", "verify", "--help"])
    help_text = capsys.readouterr().out
    assert "--approve" not in help_text and "--evidence" in help_text
    with pytest.raises(SystemExit):
        parser.parse_args(["task", "--help"])
    assert "approve" in capsys.readouterr().out


def test_needs_you_hint_names_task_approve(tmp_path, state, capsys) -> None:
    mid, _task = _mission_in_review(tmp_path, state, capsys)
    snapshot = json.loads(_run(capsys, "status", "--mission", mid, "--json"))
    attention = bridge_format.needs_attention(snapshot)
    assert attention is not None and "task approve --task 1" in attention["command"]
    assert "task verify" not in attention["message"] and "--approve" not in attention["message"]


def test_console_approve_dispatches_task_approve(tmp_path, state, capsys) -> None:
    mid, _task = _mission_in_review(tmp_path, state, capsys)
    reply = json.loads(dispatch_console_command(mid, "/approve 1"))
    assert reply["status"] == "APPLIED" and reply["result"]["status"] == "COMPLETED"


def test_mission_header_and_station_strip_show_needs_you(tmp_path, state, capsys) -> None:
    mid, _task = _mission_in_review(tmp_path, state, capsys)
    snapshot = json.loads(_run(capsys, "status", "--mission", mid, "--json"))
    assert snapshot["mission"]["status"] == "RUNNING"
    header = bridge_tui._segments_text(bridge_tui._header_segments(_console(mid, snapshot), 100))
    assert "NEEDS YOU" in header and "RUNNING" not in header
    # The current task awaiting review already says REVIEW; the mission is not also reported as RUNNING.
    context = bridge_tui._station_status_context(snapshot)
    assert "Task 1 REVIEW" in context and "RUNNING" not in context and "VERIFYING" not in context
    # A running task next to one awaiting review: the mission part says NEEDS YOU.
    other = {**snapshot, "current_task": {**snapshot["current_task"], "id": 9, "ordinal": 2, "status": "RUNNING"}}
    assert bridge_tui._station_status_context(other).endswith("Task 2 RUNNING · NEEDS YOU")
    plain = {**snapshot, "task_counts": {"RUNNING": 1}, "tasks": [], "current_task": None}
    assert "NEEDS YOU" not in bridge_tui._segments_text(bridge_tui._header_segments(_console(mid, plain), 100))


def test_agents_view_says_awaiting_review_not_completed(tmp_path, state, capsys) -> None:
    mid, _task = _mission_in_review(tmp_path, state, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        agent_map = bridge_agents.build_agent_map(store, mission_id=mid, env={}, now=1_000_000, codex_rpc=lambda *args, **kwargs: {})
    root = next(row for row in agent_map["agents"] if row["kind"] == "bridge-root")
    assert root["status"] == "completed" and root["status_label"] == "exited 0 · awaiting review"
    line = bridge_agents.format_agent_map(agent_map)[0]
    assert "exited 0 · awaiting review" in line and " completed " not in line


def test_activity_phrases_use_review_wording() -> None:
    assert bridge_tui._TASK_STATE_WORDS["VERIFYING"] == "awaiting review"
    waiting = {"kind": "provider-result-diagnosed", "task_id": 1, "data": {"task_id": 1, "task_status": "VERIFYING", "failure": {"class": "success", "cause": "completed", "status": 0}}}
    assert bridge_tui._humanize_event(waiting)[0] == "Provider result recorded · exited 0 · awaiting review"
    done = {"kind": "provider-result-diagnosed", "task_id": 1, "data": {"task_id": 1, "task_status": "COMPLETED", "failure": {"class": "success", "cause": "completed", "status": 0}}}
    assert bridge_tui._humanize_event(done)[0] == "Provider result recorded · exited 0"
    moved = {"kind": "task-status-transition", "task_id": 1, "data": {"from": "RUNNING", "to": "VERIFYING"}}
    assert "verifying" not in bridge_tui._humanize_event(moved)[0].lower()
    assert bridge_tui._activity_role("Task 1 awaiting review") == "warn"


def test_follow_up_task_reopens_a_completed_mission_as_created(tmp_path, state, capsys) -> None:
    mid, _task = _mission_in_review(tmp_path, state, capsys)
    _run(capsys, "task", "approve", "--mission", mid, "--task", "1")
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "COMPLETED")
    out = _run(capsys, "task", "add", "--mission", mid, "--prompt", "follow-up")
    assert "Mission reopened" in out
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["status"] == "CREATED"
        reopened = [event for event in store.events(mid) if event["kind"] == "mission-reopened"]
    assert reopened[-1]["data"] == {"from": "COMPLETED", "reason": "task-added", "to": "CREATED"}


def test_reopen_target_is_a_declared_state_that_can_start() -> None:
    assert bridge_states.REOPENED_MISSION_STATUS == "CREATED"
    assert bridge_states.REOPENED_MISSION_STATUS in bridge_states.MISSION_STATES
    assert "RUNNING" in bridge_states.MISSION_TRANSITIONS[bridge_states.REOPENED_MISSION_STATUS]
